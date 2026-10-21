"""可取消的 CDP 客户端；所有页面命令通过浏览器连接的独立附加会话执行。"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import websocket

logger = logging.getLogger(__name__)


class CDPClientError(RuntimeError):
    """浏览器通信或页面脚本未能完成。"""


class CDPCancelled(CDPClientError):
    """插件生命周期结束，当前浏览器操作已取消。"""


def _check_stop(event: threading.Event | None) -> None:
    """在浏览器操作边界检查生命周期。"""
    if event is not None and event.is_set():
        raise CDPCancelled("签到任务已停止")


class CDPConnection:
    """串行关联 CDP 请求与响应，不在日志中暴露带凭据的端点。"""

    def __init__(self, ws_url: str, timeout: float = 30,
                 stop_event: threading.Event | None = None):
        """保存浏览器端点、超时和取消信号，初始化请求串行化状态。"""
        self.ws_url = ws_url
        self.timeout = max(0.1, float(timeout))
        self.stop_event = stop_event
        self.ws: websocket.WebSocket | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def connect(self) -> None:
        """建立有界连接；省略 Origin 而不是要求服务允许任意来源。"""
        _check_stop(self.stop_event)
        if self.ws is not None and self.ws.connected:
            return
        try:
            self.ws = websocket.create_connection(
                self.ws_url, timeout=min(self.timeout, 5), suppress_origin=True,
            )
        except (OSError, websocket.WebSocketException, ValueError) as error:
            raise CDPClientError("无法建立 CDP WebSocket，请检查地址、认证和网络") from error

    def close(self) -> None:
        """仅断开客户端，不关闭远端浏览器。"""
        connection, self.ws = self.ws, None
        if connection is not None:
            try:
                connection.close(timeout=1)
            except (OSError, websocket.WebSocketException):
                logger.debug("CDP 连接已断开")

    def call(self, method: str, params: dict[str, Any] | None = None,
             timeout: float | None = None, session_id: str | None = None,
             *, cleanup: bool = False) -> dict[str, Any]:
        """发送一个命令，忽略异步事件但不混淆请求 ID 或页面会话。"""
        if not cleanup:
            _check_stop(self.stop_event)
        if self.ws is None or not self.ws.connected:
            if cleanup:
                raise CDPClientError("清理页面时 CDP 连接已断开")
            self.connect()
        with self._lock:
            self._request_id += 1
            message: dict[str, Any] = {
                "id": self._request_id, "method": method, "params": params or {},
            }
            if session_id:
                message["sessionId"] = session_id
            deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
            assert self.ws is not None
            try:
                self.ws.settimeout(max(0.1, min(self.timeout, 5)))
                self.ws.send(json.dumps(message))
                while True:
                    if not cleanup:
                        _check_stop(self.stop_event)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CDPClientError(f"CDP {method} 超时；动作结果可能未知，请先核对站点状态")
                    self.ws.settimeout(min(remaining, 0.25))
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise CDPClientError(f"CDP {method} 响应前连接关闭；动作结果未知")
                    response = json.loads(raw)
                    if response.get("id") != message["id"]:
                        continue
                    if "error" in response:
                        code = response["error"].get("code", "unknown")
                        raise CDPClientError(f"CDP {method} 被浏览器拒绝（{code}）")
                    return response.get("result", {})
            except (OSError, websocket.WebSocketException, ValueError) as error:
                raise CDPClientError(f"CDP {method} 通信失败；动作结果可能未知") from error


class CDPPage:
    """只操作插件创建的页面，命令始终携带该页面的 sessionId。"""

    def __init__(self, connection: CDPConnection, session_id: str):
        """绑定当前插件页面的浏览器连接和独立附加会话标识。"""
        self.connection = connection
        self.session_id = session_id

    def _call(self, method: str, params: dict[str, Any] | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        """在当前页面会话发送命令。"""
        return self.connection.call(method, params, timeout, self.session_id)

    def wait(self, seconds: float) -> None:
        """短等待可被 stop_service 立即中断。"""
        event = self.connection.stop_event
        if event is not None:
            if event.wait(max(0, seconds)):
                raise CDPCancelled("签到任务已停止")
        else:
            time.sleep(max(0, seconds))

    def navigate(self, url: str) -> None:
        """等待本次导航的文档可交互，避免读取上一页或空白页。"""
        previous = self._call("Page.getFrameTree").get("frameTree", {}).get("frame", {})
        response = self._call("Page.navigate", {"url": url})
        if response.get("errorText"):
            raise CDPClientError("浏览器导航失败，请检查站点地址和远端网络")
        loader_id = response.get("loaderId")
        deadline = time.monotonic() + self.connection.timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            tree = self._call("Page.getFrameTree", timeout=remaining)
            frame = tree.get("frameTree", {}).get("frame", {})
            current_loader = frame.get("loaderId")
            committed = current_loader and current_loader != previous.get("loaderId")
            if not loader_id or current_loader == loader_id or committed:
                try:
                    state = self.evaluate("document.readyState", await_promise=False)
                except CDPCancelled:
                    raise
                except CDPClientError:
                    state = "loading"
                if state in {"interactive", "complete"}:
                    return
            self.wait(0.1)
        raise CDPClientError("页面加载超时，未执行签到提交")

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        """执行页面脚本；JS 异常不能伪装成成功的空结果。"""
        result = self._call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise,
            "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise CDPClientError("页面脚本执行失败，未能确认动作结果")
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise CDPClientError("页面脚本返回错误，未能确认动作结果")
        return remote.get("value")

    def html(self) -> str:
        """读取完整 DOM，不截断签到结果或最近动向。"""
        return str(self.evaluate("document.documentElement ? document.documentElement.outerHTML : ''",
                                 await_promise=False) or "")

    def cookies(self, url: str) -> list[dict[str, Any]]:
        """由页面 Network 域过滤指定 URL 适用的 Cookie。"""
        return self._call("Network.getCookies", {"urls": [url]}).get("cookies", [])

    def cookie_header(self, url: str) -> str:
        """按路径从长到短序列化 Cookie，保留浏览器的 URL 适用范围。"""
        cookies = sorted(self.cookies(url), key=lambda item: len(item.get("path", "")), reverse=True)
        return "; ".join(f"{item['name']}={item['value']}" for item in cookies)

    def set_cookies(self, cookie_header: str, url: str) -> None:
        """仅向指定站点注入 MP 保存的 Cookie，不清理浏览器现有登录态。"""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Cookie 目标必须为 HTTP(S) 站点")
        cookies = []
        for part in cookie_header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                cookies.append({"name": name, "value": value, "url": url, "path": "/"})
        if cookies:
            self._call("Network.setCookies", {"cookies": cookies})

    def sync_cookies(self, cookies: list[dict[str, Any]], url: str) -> None:
        """同步本 URL 的 HTTP 会话快照，保留作用域及未变化浏览器属性。"""
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError("Cookie 同步目标必须为 HTTP(S) 站点")

        def key(item: dict[str, Any]) -> tuple[str, str, str]:
            """按名称、域名和路径标识同一作用域的 Cookie。"""
            return item["name"], item.get("domain", host).lstrip(".").lower(), item.get("path") or "/"

        incoming = {}
        for item in cookies:
            domain = str(item.get("domain") or host).lstrip(".").lower()
            if domain != host and not host.endswith("." + domain):
                raise ValueError("拒绝向浏览器同步其它站点 Cookie")
            if item.get("expires", -1) not in (None, -1) and item["expires"] <= time.time():
                continue
            incoming[key(item)] = item
        existing = {key(item): item for item in self.cookies(url)}
        for identity, item in existing.items():
            if identity not in incoming:
                parameters = {"name": item["name"], "domain": item["domain"], "path": item["path"]}
                if item.get("partitionKey"):
                    parameters["partitionKey"] = item["partitionKey"]
                self._call("Network.deleteCookies", parameters)
        changes = []
        for identity, item in incoming.items():
            previous = existing.get(identity, {})
            merged = {**previous, **item}
            if previous and all(previous.get(field) == merged.get(field) for field in
                                ("value", "secure", "httpOnly", "sameSite", "expires")):
                continue
            cookie = {name: merged[name] for name in
                      ("name", "value", "path", "secure", "httpOnly", "sameSite", "partitionKey")
                      if name in merged}
            cookie["url"] = url
            domain = str(merged.get("domain") or host)
            if domain.startswith("."):
                cookie["domain"] = domain
            expiry = merged.get("expires")
            if expiry is not None and expiry > 0:
                cookie["expires"] = expiry
            changes.append(cookie)
        if changes:
            self._call("Network.setCookies", {"cookies": changes})


class CDPSession:
    """连接普通 Chrome 或受认证代理保护的 CDP，所有页面归插件独占。"""

    def __init__(self, cdp_url: str, timeout: float = 30,
                 stop_event: threading.Event | None = None):
        """规范化 CDP 地址并保存本轮操作的超时和取消信号。"""
        self.cdp_url = self._normalize_url(cdp_url)
        self.timeout = max(0.1, float(timeout))
        self.stop_event = stop_event

    @staticmethod
    def _normalize_url(url: str) -> str:
        """接受 HTTP 发现地址或完整浏览器 WebSocket 地址。"""
        value = str(url or "").strip()
        if not value:
            raise ValueError("CDP 地址不能为空")
        if "://" not in value:
            value = "http://" + value
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError("CDP 地址必须为 http、https、ws 或 wss")
        if parsed.fragment:
            raise ValueError("CDP 地址不能包含片段")
        return value

    def get_browser_ws_url(self) -> str:
        """发现浏览器端点，保留 TLS、代理路径和认证查询参数。"""
        _check_stop(self.stop_event)
        source = urllib.parse.urlsplit(self.cdp_url)
        if source.scheme in {"ws", "wss"}:
            return self.cdp_url
        path = source.path.rstrip("/")
        if not path.endswith("/json/version"):
            path += "/json/version"
        discovery = urllib.parse.urlunsplit(source._replace(path=path))
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(discovery, timeout=min(self.timeout, 5)) as response:
                value = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise CDPClientError("CDP 发现失败，请检查地址、认证和网络") from error
        _check_stop(self.stop_event)
        endpoint = urllib.parse.urlsplit(str(value.get("webSocketDebuggerUrl") or ""))
        if endpoint.scheme not in {"ws", "wss"} or not endpoint.netloc:
            raise CDPClientError("CDP 发现响应没有浏览器 WebSocket 地址")
        # 浏览器通常公布自身的 localhost 地址；代理入口才是客户端可达地址。
        prefix = path.removesuffix("/json/version")
        endpoint_path = endpoint.path
        if prefix and endpoint_path.startswith("/devtools/"):
            endpoint_path = prefix + endpoint_path
        query = dict(urllib.parse.parse_qsl(source.query, keep_blank_values=True))
        query.update(urllib.parse.parse_qsl(endpoint.query, keep_blank_values=True))
        return urllib.parse.urlunsplit((
            "wss" if source.scheme == "https" else "ws", source.netloc,
            endpoint_path, urllib.parse.urlencode(query), "",
        ))

    @contextmanager
    def page(self, url: str = "about:blank", cookie: str = "") -> Iterator[CDPPage]:
        """创建后台标签并附加；无论异常或取消，都只关闭本次创建的 Target。"""
        connection = CDPConnection(self.get_browser_ws_url(), self.timeout, self.stop_event)
        target_id = None
        try:
            connection.connect()
            # 创建请求发出后收回 Target ID，才能在并发取消时关闭自有页面。
            _check_stop(self.stop_event)
            target_id = connection.call("Target.createTarget", {
                "url": "about:blank", "background": True, "hidden": True,
            }, timeout=min(self.timeout, 5), cleanup=True).get("targetId")
            if not target_id:
                raise CDPClientError("浏览器没有返回新页面 ID")
            session_id = connection.call("Target.attachToTarget", {
                "targetId": target_id, "flatten": True,
            }).get("sessionId")
            if not session_id:
                raise CDPClientError("无法附加签到页面")
            page = CDPPage(connection, session_id)
            page._call("Page.enable")
            page._call("Runtime.enable")
            if cookie:
                page.set_cookies(cookie, url)
            if url != "about:blank":
                page.navigate(url)
            yield page
        finally:
            if target_id:
                try:
                    connection.call("Target.closeTarget", {"targetId": target_id},
                                    timeout=3, cleanup=True)
                except CDPClientError:
                    logger.warning("无法关闭本次签到标签；远端浏览器连接可能已断开")
            connection.close()

    def get_cookies_for_urls(self, urls: list[str]) -> list[dict[str, Any]]:
        """通过附加页面读取 Cookie，而非向浏览器目标发送 Network 命令。"""
        with self.page() as page:
            return page._call("Network.getCookies", {"urls": urls}).get("cookies", [])

    def get_cookie_header_for_url(self, url: str) -> str:
        """取得单个站点的 Cookie 请求头。"""
        with self.page() as page:
            return page.cookie_header(url)

    def test_connection(self) -> tuple[bool, str]:
        """验证 WebSocket 真正可用，而不只是 HTTP 发现成功。"""
        connection = None
        try:
            connection = CDPConnection(self.get_browser_ws_url(), self.timeout, self.stop_event)
            version = connection.call("Browser.getVersion")
            return True, f"连接正常（{version.get('product', 'Chromium')}）"
        except CDPCancelled:
            raise
        except CDPClientError as error:
            return False, str(error)
        finally:
            if connection is not None:
                connection.close()
