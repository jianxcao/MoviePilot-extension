"""站点凭证不能随跨源跳转或公共图片请求泄漏。"""

import importlib
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def http_module():
    """加载插件 HTTP 边界，不初始化宿主数据库。"""
    name = "ptcdp_http_cases"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin")]
    sys.modules[name] = package
    try:
        yield importlib.import_module(name + ".site_http")
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def test_redirect_and_public_image_do_not_export_site_cookie(http_module):
    """API/登录凭证只留在原源，跳转不能发送第二个请求。"""
    observations = []

    class Public(BaseHTTPRequestHandler):
        """记录公共图片服务器收到的实际请求头。"""

        def do_GET(self):
            """返回无敏感内容的本地图片占位数据。"""
            observations.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"isolated-image")

        def log_message(self, *_args):
            """测试不输出访问日志。"""

    remote = ThreadingHTTPServer(("127.0.0.1", 0), Public)
    remote_url = f"http://127.0.0.1:{remote.server_port}/image"

    class Original(BaseHTTPRequestHandler):
        """原站点返回跨端口重定向，端口不同即不同源。"""

        def do_GET(self):
            """尝试令客户端携带原站点 Cookie 跳向公共服务器。"""
            self.send_response(302)
            self.send_header("Location", remote_url)
            self.end_headers()

        def log_message(self, *_args):
            """测试不输出访问日志。"""

    original = ThreadingHTTPServer(("127.0.0.1", 0), Original)
    workers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (original, remote)]
    for worker in workers:
        worker.start()
    context = http_module.SiteHTTP({"url": f"http://127.0.0.1:{original.server_port}"}, cookie="uid=secret-test", timeout=2)
    try:
        with pytest.raises(ValueError, match="重定向"):
            context.get(context.base + "/redirect")
        assert observations == []
        response = context.request("GET", remote_url, public=True,
                                   headers={"Authorization": "must-not-export", "Cookie": "must-not-export"})
        assert response.status_code == 200
        assert "Cookie" not in observations[0]
        assert "Authorization" not in observations[0]
    finally:
        context.close()
        for server in (original, remote):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join()
