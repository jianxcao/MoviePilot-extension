"""签到结果必须来自可见终态，不能使用脚本、模板或未提交表单。"""

import importlib
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def classify():
    """加载结果解析器，不初始化 MP 宿主或浏览器。"""
    name = "ptcdp_result_cases"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins.v3/ptcdpsignin")]
    sys.modules[name] = package
    try:
        yield importlib.import_module(name + ".results").classify
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


@pytest.mark.parametrize("content", [
    '<button>立即签到</button><script>const successMessage="签到成功";</script>',
    '<p>今日未签到，连续签到 0 天，请点击按钮。</p>',
    '<template><div>签到成功</div></template><p>请选择签到方式</p>',
    '<div hidden>今日已签到</div><p>请选择签到方式</p>',
    '<div style="display: none">签到成功</div><p>请开始</p>',
    '<p>奖励说明：签到成功可获得魔力值。</p>',
    '<p>如果已经签到的用户重复操作，会显示错误。</p>',
    '<script>state="success";</script>',
])
def test_nonterminal_or_invisible_text_never_succeeds(classify, content):
    """历史说明和隐藏成功状态不能结束一次尚未完成的签到。"""
    assert classify(content, {}).status == "failed"


def test_confirmed_success_preserves_reward(classify):
    """实际成功页保留站点奖励数量和单位，不截成无意义的“50 个”。"""
    result = classify('<div>签到成功，获得 50 个魔力值</div>', {}, submitted=True)
    assert result.status == "success"
    assert "50" in result.message and "魔力值" in result.message


def test_verified_already_today_is_not_new_signin(classify):
    """明确当天终态不被误记成新提交成功。"""
    assert classify('<div>今日已签到</div>', {}).status == "already"


def test_explicit_api_failure_beats_success_phrase(classify):
    """失败接口的文案即使提到成功，也不能伪装成成功结果。"""
    result = classify('{"success":false,"message":"签到成功后才可领取"}',
                      {"success_keywords": ["签到成功"]}, submitted=True)
    assert result.status == "failed"


@pytest.mark.parametrize(("content", "status"), [
    ('<p>今日已签到，连续签到 12 天</p>', "already"),
    ('<p>签到成功，连续签到 12 天，获得 50 个魔力值</p>', "success"),
    ('<p>今天已簽到，連續簽到 12 天</p>', "already"),
])
def test_current_terminal_state_survives_streak_on_same_line(classify, content, status):
    assert classify(content, {}).status == status


@pytest.mark.parametrize("submitted", [False, True])
def test_plain_streak_is_not_a_default_success_signal(classify, submitted):
    assert classify('<p>您已连续签到 12 天</p>', {}, submitted=submitted).status == "failed"


@pytest.mark.parametrize(("field", "status"), [("success_keywords", "success"),
                                               ("already_keywords", "already")])
def test_explicit_streak_keyword_requires_submission_or_current_authenticated_state(classify, field, status):
    rule = {field: ["您已连续签到"], "strict_status_keywords": True}
    streak = '<p>您已连续签到 12 天</p>'
    logout = '<a href="logout.php">退出</a>'
    assert classify(streak, rule).status == "failed"
    assert classify(logout + streak, rule).status == "failed"
    assert classify(streak, rule, submitted=True).status == status
    assert classify(logout + '<p>今日已签到</p>' + streak, rule).status == status
    assert classify(logout + '<b>已签到</b>' + streak, rule).status == status


@pytest.mark.parametrize("content", [
    '<p>奖励说明：您已连续签到 12 天可获得奖励</p>',
    '<p>规则：今日已签到，连续签到 12 天可领奖</p>',
    '<p>奖励说明：<b>已签到</b>，您已连续签到 12 天可领奖</p>',
    '<p>如果您已连续签到 12 天，请领取奖励</p>',
    '<p>今日未签到，您已连续签到 12 天</p>',
    '<button>立即签到</button><p>您已连续签到 12 天</p>',
])
def test_explicit_streak_never_overrides_rules_or_pending(classify, content):
    rule = {"success_keywords": ["您已连续签到"]}
    assert classify(content, rule, submitted=True).status == "failed"


def test_explicit_streak_json_respects_submission_and_failure(classify):
    rule = {"success_keywords": ["您已连续签到"]}
    assert classify('{"message":"您已连续签到12天"}', rule).status == "failed"
    assert classify('{"message":"您已连续签到12天"}', rule, submitted=True).status == "success"
    assert classify('{"success":false,"message":"您已连续签到12天"}', rule,
                    submitted=True).status == "failed"


def test_unsubmitted_streak_requires_both_authentication_and_current_state(classify):
    rule = {"success_keywords": ["您已连续签到"], "strict_status_keywords": True}
    streak = '<p>您已连续签到 12 天</p>'
    assert classify('<b>已签到</b>' + streak, rule).status == "failed"
    content = '<a href="logout.php">退出</a><p>规则：<b>已签到</b>后可领奖</p>' + streak
    assert classify(content, rule).status == "failed"


def test_json_streak_cannot_override_explicit_pending_status(classify):
    rule = {"success_keywords": ["您已连续签到"]}
    result = classify('{"message":"今日未签到，您已连续签到12天"}', rule, submitted=True)
    assert result.status == "failed"
