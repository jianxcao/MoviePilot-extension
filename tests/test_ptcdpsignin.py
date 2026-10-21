"""用独立 Chrome Profile 和本地站点验证签到，绝不读取日常浏览器资料。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins.v3" / "ptcdpsignin"


@pytest.fixture(scope="module")
def modules():
    """加载纯业务模块，不初始化 MoviePilot 的数据库或运行时。"""
    name = "ptcdp_regression"
    package = types.ModuleType(name)
    package.__path__ = [str(PLUGIN)]
    previous = set(sys.modules)
    sys.modules[name] = package
    loaded = {}
    try:
        for child in ("cdp", "core"):
            spec = importlib.util.spec_from_file_location(f"{name}.{child}", PLUGIN / f"{child}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[child] = module
        yield loaded
    finally:
        for key in set(sys.modules) - previous:
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    """启动仅监听回环的测试浏览器，结束时删除整个临时 Profile。"""
    chrome = os.environ.get("CHROME_BIN") or shutil.which("chromium") or shutil.which("google-chrome")
    if not chrome and Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not chrome:
        pytest.skip("需要 CHROME_BIN 指定 Chrome/Chromium")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"http://127.0.0.1:{port}"
    profile = tmp_path_factory.mktemp("chrome")
    process = subprocess.Popen([
        chrome, "--headless=new", f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}", "--no-first-run",
        "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urllib.request.urlopen(address + "/json/version", timeout=.3) as response:
                    discovery = json.load(response)
                break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("测试 Chrome 未启动")
                time.sleep(.1)
        yield address, discovery["webSocketDebuggerUrl"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
def site():
    """本地签到服务记录真实提交，区分入口、待签到、已签到和登录。"""
    state = {"posts": 0, "signed": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        """在本机提供具备真实状态转换的表单，不连接 PT 站。"""

        def do_GET(self):
            """提供含同名导航的签到表单以及独立登录和动向页。"""
            state["requests"].append(self.path)
            blocked = self.path.startswith("/challenge") and "Chrome/" not in self.headers.get("User-Agent", "")
            if blocked:
                body = '<title>Just a moment...</title><p>Just a moment...</p>'
            elif self.path.startswith("/redirect"):
                body = '<script>location.replace("/userdetails.php")</script>'
            elif self.path.startswith("/login"):
                body = '<form action="/takelogin.php" method="POST"><input name="username"><input type="password" name="password"><input type="submit" value="登录"></form>'
            elif self.path.startswith("/userdetails"):
                body = '<a href="logout.php">退出</a><table><tr><td>最近动向</td><td>2026-10-21 08:30:00 ( &lt; 1分前 )</td></tr></table>'
            elif self.path.startswith("/entry"):
                body = '<a href="/attendance.php">每日签到</a>'
            elif state["signed"]:
                body = '<a href="logout.php">退出</a><div>今日已签到</div>'
            else:
                body = '<a href="logout.php">退出</a><a href="attendance.php">签到入口</a><p>今日未签到，连续签到 0 天</p><script>const successMessage="签到成功";</script><form action="/submit" method="POST"><input type="hidden" name="csrf" value="isolated"><input type="submit" value="立即签到"></form>'
            data = body.encode()
            self.send_response(403 if blocked else 200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            """实际提交才改变签到状态，登录仅设置独立会话 Cookie。"""
            payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/takelogin.php":
                self.send_response(302)
                self.send_header("Set-Cookie", "uid=isolated; Path=/; HttpOnly")
                self.send_header("Location", "/index.php")
                self.end_headers()
                return
            state["posts"] += 1
            assert b"csrf=isolated" in payload
            state["signed"] = True
            data = '签到成功，获得 50 个魔力值'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            """保持测试输出不包含无关访问日志。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_page_session_cookie_and_owner_cleanup(modules, browser):
    """页面 Network 域可读 HttpOnly Cookie，且退出不关闭其他页面。"""
    cdp = modules["cdp"]
    session = cdp.CDPSession(browser[0], timeout=5)
    with session.page() as unrelated:
        unrelated.navigate("data:text/html,<title>unrelated</title>")
        with session.page() as page:
            page.set_cookies("uid=isolated", "https://cookie-review.test/")
            assert page.cookie_header("https://cookie-review.test/") == "uid=isolated"
        assert session.get_cookie_header_for_url("https://cookie-review.test/") == "uid=isolated"
        assert unrelated.evaluate("document.title") == "unrelated"
        with pytest.raises(cdp.CDPClientError, match="脚本"):
            unrelated.evaluate("throw new Error('isolated')")


def test_cookie_snapshot_deletes_only_applicable_scope(modules, browser):
    """HTTP 删除与轮换同步浏览器，但不能删除其它路径登录态。"""
    cdp = modules["cdp"]
    with cdp.CDPSession(browser[0], timeout=5).page() as page:
        page._call("Network.setCookies", {"cookies": [
            {"name": "session", "value": "A", "url": "https://snapshot-review.test/", "path": "/", "httpOnly": True},
            {"name": "removed", "value": "old", "url": "https://snapshot-review.test/", "path": "/"},
            {"name": "private", "value": "keep", "url": "https://snapshot-review.test/private/", "path": "/private"},
        ]})
        snapshot = [item for item in page.cookies("https://snapshot-review.test/") if item["name"] != "removed"]
        snapshot[0]["value"] = "B"
        page.sync_cookies(snapshot, "https://snapshot-review.test/")
        root = {item["name"]: item for item in page.cookies("https://snapshot-review.test/")}
        assert root["session"]["value"] == "B"
        assert root["session"]["httpOnly"] is True
        assert "removed" not in root
        private = {item["name"]: item["value"] for item in page.cookies("https://snapshot-review.test/private/")}
        assert private["private"] == "keep"


def test_direct_websocket_and_cancellation(modules, browser):
    """完整 WebSocket 地址可连接，取消后仍清理自有页面。"""
    cdp = modules["cdp"]
    stopped = threading.Event()
    session = cdp.CDPSession(browser[1], timeout=5, stop_event=stopped)
    assert session.test_connection()[0]
    with session.page() as page:
        stopped.set()
        with pytest.raises(cdp.CDPCancelled):
            page.wait(30)
    with pytest.raises(cdp.CDPCancelled):
        session.get_cookies_for_urls(["https://cookie-review.test/"])


def test_long_page_and_script_exceptions(modules, browser):
    """结果位于长页尾部仍可读取，JS 错误必须向调用者传播。"""
    cdp = modules["cdp"]
    with cdp.CDPSession(browser[0], timeout=5).page() as page:
        page.navigate("data:text/html;charset=utf-8,<title>long</title><body>" + "x" * 4000 + "<div>结果标记</div>")
        assert "结果标记" in page.html()
        with pytest.raises(cdp.CDPClientError, match="脚本"):
            page.evaluate("Promise.reject(new Error('isolated'))")


def test_client_redirect_completes_new_document(modules, browser, site, monkeypatch):
    """客户端重定向换 loader 时等待最终文档，而不锁死在初始 loader。"""
    url, _state = site
    cdp = modules["cdp"]
    with cdp.CDPSession(browser[0], timeout=2).page() as page:
        call = page._call

        def delayed_navigation(method, params=None, timeout=None):
            result = call(method, params, timeout)
            if method == "Page.navigate":
                time.sleep(.15)
            return result

        monkeypatch.setattr(page, "_call", delayed_navigation)
        page.navigate(url + "/redirect")
        assert page.evaluate("location.pathname") == "/userdetails.php"
        assert "最近动向" in page.html()


def test_connection_loss_reclaims_owned_page(modules, browser):
    """即使失去创建响应后的连接，session-bound 页面也不会遗留。"""
    cdp = modules["cdp"]
    observer = cdp.CDPConnection(browser[1], timeout=3)
    try:
        with cdp.CDPSession(browser[0], timeout=3).page() as page:
            target_id = page._call("Target.getTargetInfo")["targetInfo"]["targetId"]
            page.connection.close()
        deadline = time.monotonic() + 3
        while True:
            ids = {target["targetId"] for target in observer.call("Target.getTargets")["targetInfos"]}
            if target_id not in ids:
                break
            assert time.monotonic() < deadline, "连接关闭后插件页面遗留"
            time.sleep(.05)
    finally:
        observer.close()


def test_browser_submits_instead_of_reopening_navigation(modules, browser, site):
    """同页有签到入口时必须真正提交，不能被提示脚本或连续天数误判。"""
    url, state = site
    signer = modules["core"].PTCDPSigner(browser[0], timeout=10)
    result = signer.process(
        {"id": 1, "name": "本地测试", "url": url, "cookie": "uid=isolated"},
        read_activity=False,
        custom_rules={"1": {"engine": "cdp", "path": "/attendance.php", "submit_text": "立即签到"}},
    )
    assert result["status"] in {"success", "already"}, result
    assert state["posts"] == 1
    assert state["signed"]


def test_http_form_cannot_succeed_from_script_text(modules, browser, site):
    """HTTP 引擎必须提交隐藏 CSRF 字段，脚本提示不构成成功。"""
    url, state = site
    result = modules["core"].PTCDPSigner(browser[0], timeout=10).process(
        {"id": 3, "name": "HTTP测试", "url": url, "cookie": "uid=isolated"},
        read_activity=False,
        custom_rules={"3": {"engine": "http", "path": "/attendance.php"}},
    )
    assert result["status"] in {"success", "already"}, result
    assert state["posts"] == 1


def test_http_challenge_403_allows_first_browser_submission(modules, browser, site):
    """HTTP 被明确挑战阻挡时转浏览器，且整轮只提交一次。"""
    url, state = site
    result = modules["core"].PTCDPSigner(browser[0], timeout=10).process(
        {"id": 5, "name": "挑战测试", "url": url, "cookie": "uid=isolated", "ua": "isolated-http"},
        read_activity=False,
        custom_rules={"5": {"engine": "http", "path": "/challenge"}},
    )
    assert result["status"] in {"success", "already"}, result
    assert state["posts"] == 1


def test_browser_follows_entry_then_submits(modules, browser, site):
    """入口页没有提交按钮时，在同一个页面继续导航并完成提交。"""
    url, state = site
    result = modules["core"].PTCDPSigner(browser[0], timeout=10).process(
        {"id": 4, "name": "入口测试", "url": url, "cookie": "uid=isolated"},
        read_activity=False,
        custom_rules={"4": {"engine": "cdp", "path": "/entry"}},
    )
    assert result["status"] in {"success", "already"}, result
    assert state["posts"] == 1


def test_login_only_reads_activity_without_signing(modules, browser, site):
    """独立登录任务返回实际站点动向，不执行签到表单。"""
    url, state = site
    signer = modules["core"].PTCDPSigner(browser[0], timeout=10)
    result = signer.process(
        {"id": 2, "name": "登录测试", "url": url,
         "note": {"username": "isolated", "password": "isolated", "activity_path": "/userdetails.php"}},
        signin=False, login=True, read_activity=True,
    )
    assert result["status"] == "login", result
    assert "2026-10-21 08:30:00" in result["activity"]
    assert state["posts"] == 0
