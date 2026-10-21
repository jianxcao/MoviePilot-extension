"""真实站点状态机覆盖 HTTP 提交边界和浏览器会话交接。"""

from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
import requests

from test_ptcdpsignin import modules as _modules_fixture, browser as _browser_fixture


@pytest.fixture(scope="module", name="modules")
def shared_modules():
    """为会话回归注册同一套隔离插件加载器。"""
    yield from _modules_fixture.__wrapped__()


@pytest.fixture(scope="module", name="browser")
def shared_browser(tmp_path_factory):
    """为会话回归启动专用临时 Profile，而非用户浏览器。"""
    yield from _browser_fixture.__wrapped__(tmp_path_factory)


@contextmanager
def session_site(challenge=False, initial_challenge=False, revoke=False):
    state = {"posts": 0, "activity": [], "index": []}

    class Handler(BaseHTTPRequestHandler):
        def cookies(self):
            parsed = SimpleCookie(self.headers.get("Cookie", ""))
            return {name: item.value for name, item in parsed.items()}

        def respond(self, body, cookies=(), status=200):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            cookies = self.cookies()
            authorized = cookies.get("sid") in {"A", "B"}
            if self.path == "/index.php":
                state["index"].append(cookies)
                self.respond('<a href="logout.php">退出</a>' if authorized else '<form><input type="password"></form>')
            elif self.path == "/member/activity":
                state["activity"].append(cookies)
                self.respond('<a href="logout.php">退出</a><table><tr><td>最近动向</td><td>2026-10-21 09:45:00</td></tr></table>'
                             if cookies.get("sid") == "B" and "old" not in cookies else '<form><input type="password"></form>')
            elif self.path == "/attendance.php":
                if initial_challenge and "Chrome/" not in self.headers.get("User-Agent", ""):
                    self.respond('<title>Just a moment...</title><p>Just a moment...</p>', status=403)
                else:
                    self.respond('<a href="logout.php">退出</a><p>今日未签到</p><form action="/submit" method="POST"><input name="csrf" value="session"><input type="submit" value="立即签到"></form>')
            elif self.path.startswith("/submit"):
                self.do_POST()
            else:
                self.respond('<form><input type="password"></form>')

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["posts"] += 1
            if challenge:
                self.respond('<title>Just a moment...</title><p>Just a moment...</p>')
            else:
                self.respond('签到成功，获得 10 个魔力值', (
                    'sid=revoked; Path=/; HttpOnly; SameSite=Lax' if revoke else 'sid=B; Path=/; HttpOnly; SameSite=Lax',
                    'old=; Path=/member; Max-Age=0',
                ))

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@contextmanager
def isolated_signer(modules, browser):
    signer = modules["core"].PTCDPSigner(browser[0], timeout=5)
    # Reuse the module fixture's throwaway Chrome profile, never a user's CDP.
    original_page = signer.session.page

    @contextmanager
    def page_factory():
        with original_page() as page:
            page._call("Network.clearBrowserCookies")
            try:
                yield page
            finally:
                page._call("Network.clearBrowserCookies")

    signer.session.page = page_factory
    yield signer


@pytest.mark.parametrize("rule", [
    {"path": "/attendance.php"},
    {"path": "/post-submit", "method": "POST"},
    {"path": "/attendance.php", "signin_path": "/submit", "signin_method": "GET"},
])
def test_http_200_challenge_after_post_never_submits_in_browser(modules, browser, rule):
    with session_site(challenge=True) as (url, state), isolated_signer(modules, browser) as signer:
        result = signer.process({"id": 201, "url": url, "cookie": "sid=A"}, read_activity=False,
                                custom_rules={"201": dict(rule, engine="http")})
    assert state["posts"] == 1
    assert result["status"] == "failed", result
    assert "未知" in result["message"]


def test_theme_cookie_does_not_mask_valid_moviepilot_session(modules, browser):
    with session_site() as (url, state), isolated_signer(modules, browser) as signer:
        original_page = signer.session.page

        @contextmanager
        def themed_page():
            with original_page() as page:
                page.set_cookies("theme=dark", url)
                yield page

        signer.session.page = themed_page
        result = signer.process({"id": 202, "url": url, "cookie": "sid=A"},
                                signin=False, login=True, read_activity=False)
    assert result["login_status"] == "login", result
    assert "sid=A" in result["cookie"]
    assert "sid" not in state["index"][0]
    assert any(cookies.get("sid") == "A" for cookies in state["index"][1:])


@pytest.mark.parametrize("read_activity", [False, True])
def test_rotated_http_cookie_and_scoped_deletion_reach_browser(modules, browser, read_activity):
    with session_site() as (url, state), isolated_signer(modules, browser) as signer:
        original_page = signer.session.page

        @contextmanager
        def seeded_page():
            with original_page() as page:
                page.sync_cookies([
                    {"name": "sid", "value": "A", "domain": "127.0.0.1", "path": "/", "httpOnly": True, "sameSite": "Lax"},
                    {"name": "old", "value": "stale", "domain": "127.0.0.1", "path": "/member"},
                    {"name": "unrelated", "value": "preserved", "domain": "127.0.0.1", "path": "/unrelated"},
                ], url + "/member")
                yield page
                state["saved"] = page.cookies(url + "/member")
                state["other_path"] = page.cookies(url + "/unrelated")

        signer.session.page = seeded_page
        result = signer.process({"id": 203, "url": url + "/member"}, read_activity=read_activity,
                                custom_rules={"203": {"engine": "http", "path": "/attendance.php", "activity_path": "/member/activity"}})
    assert result["status"] == "success", result
    assert "sid=B" in result["cookie"] and "old=" not in result["cookie"]
    assert state["index"][-1].get("sid") == "B"
    saved = next(cookie for cookie in state["saved"] if cookie["name"] == "sid")
    assert saved["httpOnly"] is True and saved["sameSite"] == "Lax"
    assert not any(cookie["name"] == "old" for cookie in state["saved"])
    assert any(cookie["name"] == "unrelated" for cookie in state["other_path"])
    if read_activity:
        assert result["activity"] == "2026-10-21 09:45:00", result
        assert state["activity"][-1].get("sid") == "B"
        assert "old" not in state["activity"][-1]


def test_initial_http_403_challenge_allows_first_browser_submission(modules, browser):
    with session_site(initial_challenge=True) as (url, state), isolated_signer(modules, browser) as signer:
        result = signer.process({"id": 204, "url": url, "cookie": "sid=A"}, read_activity=False,
                                custom_rules={"204": {"engine": "http", "path": "/attendance.php", "submit_text": "立即签到", "wait": 0}})
    assert state["posts"] == 1
    assert result["status"] == "success", result


def test_http_revocation_cannot_export_cookie_using_stale_dom(modules, browser):
    with session_site(revoke=True) as (url, state), isolated_signer(modules, browser) as signer:
        result = signer.process({"id": 205, "url": url, "cookie": "sid=A"}, read_activity=False,
                                custom_rules={"205": {"engine": "http", "path": "/attendance.php"}})
    assert state["index"][1].get("sid") == "A"
    assert state["index"][-1].get("sid") == "revoked"
    assert result["cookie"] == "", result


def test_snapshot_preserves_cookie_scope_and_excludes_other_hosts(modules):
    http_class = __import__(modules["core"].__package__ + ".site_http", fromlist=["SiteHTTP"]).SiteHTTP
    context = http_class({"url": "https://tracker.example.test"})
    try:
        context.seed_cookies([
            {"name": "sid", "value": "A", "domain": "tracker.example.test", "path": "/", "httpOnly": True, "secure": True, "sameSite": "Strict"},
            {"name": "scoped", "value": "B", "domain": ".example.test", "path": "/member", "expires": 4102444800},
        ])
        context.session.cookies.set("foreign", "secret", domain="other.test", path="/")
        context.session.cookies.set_cookie(requests.cookies.create_cookie("expired", "gone", domain="tracker.example.test", expires=1))
        snapshot = context.cookie_snapshot(context.base)
        assert {item["name"] for item in snapshot} == {"sid", "scoped"}
        sid = next(item for item in snapshot if item["name"] == "sid")
        assert sid["secure"] and sid["httpOnly"] and sid["sameSite"] == "Strict"
        scoped = next(item for item in snapshot if item["name"] == "scoped")
        assert scoped["domain"] == ".example.test" and scoped["path"] == "/member"
        assert scoped["expires"] == 4102444800
        with pytest.raises(ValueError, match="跨"):
            context.cookie_snapshot("https://other.test")
    finally:
        context.close()
