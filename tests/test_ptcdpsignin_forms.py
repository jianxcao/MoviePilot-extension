"""签到表单沿用文档 URL 与真实提交字段，拒绝跨 origin 目标。"""

import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def forms():
    """独立加载表单模块，不初始化 MoviePilot。"""
    name = "ptcdp_form_cases"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin")]
    sys.modules[name] = package
    try:
        yield importlib.import_module(name + ".forms")
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.mark.parametrize(("base", "href", "expected"), [
    ("https://tracker.example/account/index.php?view=home", "attendance.php", "/account/attendance.php"),
    ("https://tracker.example/account/", "attendance.php", "/account/attendance.php"),
    ("https://tracker.example/account/index.php?view=home", "?action=sign", "/account/index.php?action=sign"),
    ("https://tracker.example/account/index.php?view=home", "", "/account/index.php?view=home"),
    ("https://tracker.example/account/index.php", "https://tracker.example:443/sign", "/sign"),
    ("https://tracker.example/account/index.php", "http://tracker.example/sign", ""),
    ("https://tracker.example/account/index.php", "https://tracker.example:8443/sign", ""),
    ("https://tracker.example:8443/index.php", "//tracker.example/sign", ""),
    ("https://tracker.example/index.php", "https://other.example/sign", ""),
    ("https://tracker.example/index.php", "https://tracker.example:bad/sign", ""),
    ("https://tracker.example/index.php", "#sign", ""),
    ("https://tracker.example/index.php", "javascript:sign()", ""),
])
def test_same_site_path_uses_document_url_and_origin(forms, base, href, expected):
    assert forms.same_site_path(base, href) == expected


@pytest.mark.parametrize("action", ["", "attendance.php"])
def test_discovery_reuses_full_form_payload(forms, action):
    html = ('<form action="' + action + '" method="post">'
            '<input type="hidden" name="csrf" value="token-123">'
            '<input type="hidden" name="signed_timestamp" value="12345">'
            '<button type="submit" name="checkin" value="yes">每日签到</button>'
            '</form>')
    base = "https://tracker.example/account/index.php?tab=sign"
    form = forms.find_signin_form(html, base)
    target = forms.discover_signin_target(html, base)
    assert form["action"] == ("/account/attendance.php" if action else "/account/index.php?tab=sign")
    assert target == {"path": form["action"], "method": form["method"], "data": form["data"]}
    assert target["data"] == {"csrf": "token-123", "signed_timestamp": "12345", "checkin": "yes"}


def test_only_selected_submit_control_is_sent(forms):
    html = ('<form action="attendance.php"><input type="hidden" name="csrf" value="isolated">'
            '<button type="button" name="help" value="help">帮助</button>'
            '<input type="reset" name="reset" value="清空">'
            '<button name="sign" value="go">每日签到</button>'
            '<input type="submit" name="cancel" value="取消"></form>')
    assert forms.discover_signin_target(html, "https://tracker.example/index.php")["data"] == {
        "csrf": "isolated", "sign": "go",
    }


@pytest.mark.parametrize("action", ["https://other.example/sign", "http://tracker.example/sign",
                                    "https://tracker.example:8443/sign"])
def test_cross_origin_forms_never_expose_payload(forms, action):
    html = ('<form action="' + action + '"><input type="hidden" name="csrf" value="secret">'
            '<input type="submit" name="sign" value="每日签到"></form>')
    assert forms.find_signin_form(html, "https://tracker.example/index.php") is None
    assert forms.discover_signin_target(html, "https://tracker.example/index.php") == {}
