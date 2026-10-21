"""CDP 取消和错误关联的独立传输回归。"""

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


@pytest.fixture
def transport():
    """仅加载传输模块，避免引入宿主数据库。"""
    path = Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin/cdp.py"
    spec = importlib.util.spec_from_file_location("cdp_transport_regression", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_cancel_during_pending_command_does_not_wait_for_full_timeout(transport):
    """发送后取消必须退出等待，而非等满配置中的命令超时。"""
    stopped = threading.Event()

    class PendingSocket:
        """收到请求后模拟停止，不生成任何浏览器响应。"""

        connected = True

        def settimeout(self, _timeout):
            """接受传输分片读超时。"""

        def send(self, _payload):
            """请求已经发出时触发生命周期停止。"""
            stopped.set()

        def recv(self):
            """若实现忽略取消，直接令测试失败而非永久阻塞。"""
            pytest.fail("取消后不应该继续读取 CDP socket")

    connection = transport.CDPConnection("ws://localhost/isolated", 60, stopped)
    connection.ws = PendingSocket()
    with pytest.raises(transport.CDPCancelled):
        connection.call("Runtime.evaluate", {"expression": "isolated"})


def test_error_or_async_event_cannot_be_mistaken_for_command_success(transport):
    """异步事件不构成响应，协议错误必须携带失败语义。"""
    class EventSocket:
        """先推送页面事件，再返回当前请求的真实协议错误。"""

        connected = True

        def settimeout(self, _timeout):
            """接受读写超时。"""

        def send(self, payload):
            """根据实际请求 ID 生成事件和失败响应。"""
            message = json.loads(payload)
            self.responses = iter([
                json.dumps({"method": "Page.loadEventFired", "params": {}}),
                json.dumps({"id": message["id"], "error": {"code": -32601, "message": "unknown"}}),
            ])

        def recv(self):
            """按浏览器事件顺序返回数据。"""
            return next(self.responses)

    connection = transport.CDPConnection("ws://localhost/isolated")
    connection.ws = EventSocket()
    with pytest.raises(transport.CDPClientError, match="-32601"):
        connection.call("Network.getCookies")
