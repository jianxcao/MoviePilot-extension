"""API-only 站点不应强制打开浏览器页面或回写无关 Cookie。"""

import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize("signin,login", [(False, True), (True, True)])
def test_mteam_login_and_dual_refresh_only_once(monkeypatch, signin, login):
    """M-Team 登录/双选只调用一次真实 API 流程，不访问浏览器登录页。"""
    name = "ptcdp_api_cases"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin")]
    monkeypatch.setitem(sys.modules, name, package)
    try:
        core = importlib.import_module(name + ".core")
        calls = []

        class MTeam:
            """隔离外部 API 边界，断言完整 process 的路径选择和 Cookie 授权。"""

            def signin(self, site, context):
                """模拟已由专用模块确认的 API 业务成功。"""
                calls.append(site["url"])
                return True, "API 已确认最后访问刷新"

        monkeypatch.setattr(core, "site_module_for", lambda _url: MTeam)
        signer = core.PTCDPSigner("http://127.0.0.1:1", timeout=1)
        result = signer.process({"id": 1, "url": "https://kp.m-team.cc", "token": "isolated"},
                                signin=signin, login=login)
        assert calls == ["https://kp.m-team.cc"]
        assert result["status"] == ("success" if signin else "login")
        assert result["login_status"] == "login"
        assert result["cookie"] == ""
    finally:
        for key in list(sys.modules):
            if key.startswith(name + "."):
                sys.modules.pop(key, None)
