"""
轻量级 Chrome DevTools Protocol (CDP) 客户端。
仅依赖 Python 内置库与 websocket-client（MoviePilot 自带），无需安装 Playwright。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import websocket

logger = logging.getLogger(__name__)


class CDPClientError(Exception):
    """CDP 通信与执行异常"""
    pass


class CDPConnection:
    """与单一 Target (页面 Tab 或浏览器自身) 的 WebSocket 连接包装"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self.ws: Optional[websocket.WebSocket] = None
        self._req_id = 0
        self._lock = threading.Lock()

    def connect(self) -> None:
        if self.ws and self.ws.connected:
            return
        try:
            self.ws = websocket.create_connection(self.ws_url, timeout=self.timeout)
        except Exception as e:
            raise CDPClientError(f"连接 CDP WebSocket 失败 ({self.ws_url}): {e}")

    def close(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """同步发送 CDP 方法并等待匹配 ID 的结果"""
        self.connect()
        with self._lock:
            self._req_id += 1
            msg_id = self._req_id
            payload = {
                "id": msg_id,
                "method": method,
                "params": params or {}
            }
            assert self.ws is not None
            self.ws.send(json.dumps(payload))

            deadline = time.monotonic() + (timeout or self.timeout)
            old_timeout = self.ws.gettimeout()
            try:
                while time.monotonic() < deadline:
                    remain = max(0.5, deadline - time.monotonic())
                    self.ws.settimeout(remain)
                    raw = self.ws.recv()
                    if not raw:
                        continue
                    msg = json.loads(raw)
                    if msg.get("id") == msg_id:
                        if "error" in msg:
                            raise CDPClientError(f"CDP 方法 {method} 报错: {msg['error']}")
                        return msg.get("result", {})
                    # 其他 id 或事件通知忽略
            except websocket.WebSocketTimeoutException:
                raise CDPClientError(f"CDP 方法 {method} 执行超时")
            finally:
                if self.ws:
                    self.ws.settimeout(old_timeout)
            raise CDPClientError(f"CDP 方法 {method} 未在超时时间内收到响应")


class CDPSession:
    """
    负责与 Chrome 交互的管理器：
    1. 连通性测试
    2. 获取站点 Cookies
    3. 后台开 Tab 导航并执行签到动作
    """

    def __init__(self, cdp_url: str, timeout: float = 30.0):
        self.cdp_url = self._normalize_url(cdp_url)
        self.timeout = timeout

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            raise ValueError("CDP 地址不能为空")
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "http://" + url
        return url

    def test_connection(self) -> Tuple[bool, str]:
        """测试与远程 Chrome 的连通性"""
        try:
            req_url = f"{self.cdp_url}/json/version"
            req = urllib.request.Request(req_url, headers={"User-Agent": "MoviePilot-CDP"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                browser = data.get("Browser", "Unknown")
                proto = data.get("Protocol-Version", "Unknown")
                return True, f"连接正常 ({browser}, CDP {proto})"
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return False, "403 Forbidden：请检查 Chrome 启动参数是否包含 --remote-allow-origins=*"
            return False, f"HTTP 错误 {e.code}: {e.reason}"
        except Exception as e:
            return False, f"连接失败: {e}"

    def get_browser_ws_url(self) -> str:
        """获取浏览器主实例的 WebSocket 地址"""
        req_url = f"{self.cdp_url}/json/version"
        req = urllib.request.Request(req_url, headers={"User-Agent": "MoviePilot-CDP"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
            ws_url = data.get("webSocketDebuggerUrl")
            if not ws_url:
                raise CDPClientError("未能从 /json/version 获取 webSocketDebuggerUrl")
            return ws_url

    def get_cookies_for_urls(self, urls: List[str]) -> List[Dict[str, Any]]:
        """通过 Network.getCookies 获取指定 URL 列表在 Chrome 中当前保存的 Cookie"""
        ws_url = self.get_browser_ws_url()
        conn = CDPConnection(ws_url, timeout=self.timeout)
        try:
            conn.connect()
            res = conn.call("Network.getCookies", {"urls": urls})
            return res.get("cookies", [])
        finally:
            conn.close()

    def get_cookie_header_for_url(self, url: str) -> str:
        """获取指定 URL 的单行 Cookie 请求头字符串 (name=value; name2=value2)"""
        cookies = self.get_cookies_for_urls([url])
        if not cookies:
            return ""
        pairs = [f"{c['name']}={c['value']}" for c in cookies if 'name' in c and 'value' in c]
        return "; ".join(pairs)

    def navigate_and_evaluate(
        self,
        url: str,
        script: Optional[str] = None,
        wait_seconds: float = 3.0,
        page_timeout: float = 30.0
    ) -> Dict[str, Any]:
        """
        在 Chrome 中创建一个全新的后台 Tab，导航到 url，等待加载，可选执行 JS，随后关闭该 Tab。
        返回包含执行结果、最终 URL、页面标题、HTML 片段等信息。
        """
        ws_url = self.get_browser_ws_url()
        browser_conn = CDPConnection(ws_url, timeout=self.timeout)
        target_id = None
        page_conn = None
        try:
            # 1. 创建新 Tab 目标
            browser_conn.connect()
            target_res = browser_conn.call("Target.createTarget", {"url": "about:blank"})
            target_id = target_res.get("targetId")
            if not target_id:
                raise CDPClientError("Target.createTarget 未返回 targetId")

            # 2. 连接到新建的 Tab
            # 部分 Chrome 可以直接通过 /devtools/page/<targetId> 连接
            parsed = urllib.parse.urlparse(self.cdp_url)
            ws_host = parsed.netloc
            tab_ws_url = f"ws://{ws_host}/devtools/page/{target_id}"

            page_conn = CDPConnection(tab_ws_url, timeout=page_timeout)
            page_conn.connect()

            # 3. 启用必要域
            page_conn.call("Page.enable")
            page_conn.call("Runtime.enable")

            # 4. 导航到目标 URL
            page_conn.call("Page.navigate", {"url": url})

            # 等待网络与 DOM 加载
            time.sleep(wait_seconds)

            # 获取页面基础信息
            doc_info = page_conn.call(
                "Runtime.evaluate",
                {
                    "expression": "JSON.stringify({title: document.title, url: window.location.href, text: document.body ? document.body.innerText.slice(0, 2000) : ''})",
                    "returnByValue": True
                }
            )
            parsed_info = {}
            raw_val = doc_info.get("result", {}).get("value")
            if raw_val:
                try:
                    parsed_info = json.loads(raw_val)
                except Exception:
                    pass

            eval_result = None
            if script:
                js_res = page_conn.call(
                    "Runtime.evaluate",
                    {
                        "expression": script,
                        "awaitPromise": True,
                        "returnByValue": True
                    }
                )
                eval_result = js_res.get("result", {}).get("value")

            # 再稍微等一下可能触发的异步请求
            if script:
                time.sleep(2.0)
                # 重新刷新一次页面标题和文本
                doc_info_after = page_conn.call(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify({title: document.title, url: window.location.href, text: document.body ? document.body.innerText.slice(0, 2000) : ''})",
                        "returnByValue": True
                    }
                )
                after_val = doc_info_after.get("result", {}).get("value")
                if after_val:
                    try:
                        parsed_info = json.loads(after_val)
                    except Exception:
                        pass

            return {
                "success": True,
                "title": parsed_info.get("title", ""),
                "final_url": parsed_info.get("url", url),
                "text": parsed_info.get("text", ""),
                "script_result": eval_result,
            }

        finally:
            if page_conn:
                page_conn.close()
            # 严格关闭该后台 Target，不留僵尸 Tab
            if target_id and browser_conn.ws and browser_conn.ws.connected:
                try:
                    browser_conn.call("Target.closeTarget", {"targetId": target_id})
                except Exception as e:
                    logger.warning(f"关闭 CDP Target {target_id} 异常: {e}")
            browser_conn.close()
