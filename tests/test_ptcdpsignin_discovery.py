"""CDP 发现端点保留加密、认证及反向代理前缀。"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cdp():
    """独立加载传输模块，不接触用户浏览器。"""
    path = Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin/cdp.py"
    spec = importlib.util.spec_from_file_location("cdp_discovery_cases", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_tls_proxy_does_not_downgrade_or_drop_auth(cdp, monkeypatch):
    """代理公布内部 localhost 时，仍通过原 TLS 入口携带认证连接。"""
    class Discovery:
        """仅模拟 CDP HTTP 发现响应；断言最终可达端点语义。"""

        def open(self, url, timeout):
            """公布不能直接访问的浏览器内部地址。"""
            return io.BytesIO(json.dumps({
                "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/isolated",
            }).encode())

    monkeypatch.setattr(cdp.urllib.request, "build_opener", lambda *_args: Discovery())
    session = cdp.CDPSession("https://browser.example/cdp?token=isolated")
    assert session.get_browser_ws_url() == "wss://browser.example/cdp/devtools/browser/isolated?token=isolated"


def test_full_secure_websocket_never_runs_http_discovery(cdp):
    """可直接配置带授权路径的完整浏览器 WebSocket，不损坏 scheme。"""
    endpoint = "wss://browser.example/secure/browser/isolated?token=local-test"
    assert cdp.CDPSession(endpoint).get_browser_ws_url() == endpoint
