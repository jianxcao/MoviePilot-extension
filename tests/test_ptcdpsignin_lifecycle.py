"""入口生命周期与日缓存业务回归：只替换宿主边界，不启动真实宿主。"""

from __future__ import annotations

import copy
import importlib.util
import sys
import threading
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins.v3" / "ptcdpsignin"


@pytest.fixture
def lifecycle(monkeypatch):
    state = types.SimpleNamespace(sites=[], visits=[], jobs={}, removed=[], updates=[], outcome=None)

    class Base:
        def __init__(self):
            self.data = {}
            self.saved = []
            self.configs = []
            self.notifications = []

        def get_data(self, key=None):
            return copy.deepcopy(self.data.get(key)) if key is not None else copy.deepcopy(self.data)

        def save_data(self, key, value):
            self.saved.append((key, copy.deepcopy(value)))
            self.data[key] = copy.deepcopy(value)

        def del_data(self, key):
            self.data.pop(key, None)

        def update_config(self, config):
            self.configs.append(copy.deepcopy(config))

        def post_message(self, **kwargs):
            self.notifications.append(kwargs)

    class SiteOper:
        def list_order_by_pri(self):
            return list(state.sites)

        def get(self, sid):
            return next((site for site in state.sites if site.id == sid), None)

        def update(self, sid, payload):
            state.updates.append((sid, dict(payload)))
            current = self.get(sid)
            if current:
                for key, value in payload.items():
                    setattr(current, key, value)
            return current

    class Cancelled(Exception):
        pass

    class Signer:
        def __init__(self, cdp_url, timeout=30, stop_event=None):
            self.stop_event = stop_event

        def process(self, info, **kwargs):
            state.visits.append((dict(info), dict(kwargs)))
            if callable(state.outcome):
                return state.outcome(info, kwargs, self.stop_event)
            return dict(state.outcome or {
                "status": "success" if kwargs["signin"] else "login", "message": "完成",
                "cookie": "", "login_status": "login" if kwargs["login"] else "",
                "login": "认证成功" if kwargs["login"] else "", "activity": "刚刚", "activity_source": "主页",
            })

    def add_job(plugin_id, job_id, func, name, delay_seconds=0, func_kwargs=None):
        state.jobs[job_id] = {"func": func, "kwargs": dict(func_kwargs or {}), "delay": delay_seconds}
        return True

    def remove_job(plugin_id, job_id):
        state.removed.append(job_id)
        state.jobs.pop(job_id, None)

    def module(name, **attrs):
        value = types.ModuleType(name)
        value.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    for name in ("app", "app.db", "app.db.oper", "app.sdk", "apscheduler",
                 "apscheduler.triggers"):
        package = module(name)
        package.__path__ = []
    module("app.db.oper.site", SiteOper=SiteOper)
    module("app.schemas", NotificationType=types.SimpleNamespace(SiteMessage="SiteMessage"))
    module("app.sdk.plugin", _PluginBase=Base)
    module("app.sdk.config", settings=types.SimpleNamespace(TZ="Asia/Shanghai", PROXY_HOST=""))
    logger = types.SimpleNamespace(**{level: lambda *a, **k: None for level in ("info", "error", "warning")})
    module("app.sdk.logging", logger=logger)
    scheduler = module("app.sdk.scheduler", add_plugin_once_job=add_job, remove_plugin_once_job=remove_job)
    sys.modules["app.sdk"].scheduler = scheduler
    module("apscheduler.triggers.cron", CronTrigger=types.SimpleNamespace(from_crontab=lambda *a, **k: None))
    name = "ptcdp_lifecycle"
    module(name + ".cdp", CDPCancelled=Cancelled)
    module(name + ".core", PTCDPSigner=Signer, parse_rules=lambda raw: {})
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    entry = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, entry)
    spec.loader.exec_module(entry)
    state.entry = entry
    state.instance = entry.PTCDPSignIn()
    state.now = datetime(2026, 10, 21, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(entry.PTCDPSignIn, "_now", staticmethod(lambda: state.now))

    def site(sid, url=None, name="同名站点", cookie="old=1"):
        return types.SimpleNamespace(id=sid, url=url or f"https://site{sid}.test/", name=name, cookie=cookie)

    state.site = site
    yield state
    state.instance.stop_service()


def test_run_once_consumed_before_schedule_and_reload(lifecycle):
    state = lifecycle
    plugin = state.instance
    plugin.init_plugin({"run_once": True, "site_ids": [1]})
    assert plugin.configs[-1]["run_once"] is False
    assert plugin.configs[-1]["clear_today"] is False
    old_callback = state.jobs["run-once"]
    plugin.init_plugin(plugin.configs[-1])
    assert "run-once" not in state.jobs
    old_callback["func"](**old_callback["kwargs"])
    assert state.visits == []
    assert plugin.saved == []
    assert plugin.notifications == []


def test_stop_waits_cleanup_and_cancels_remaining_sites(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1), state.site(2)]
    plugin.init_plugin({"site_ids": ["__all__"], "concurrency": 1})
    entered, release, cancelled, cleaned, stopped = [threading.Event() for _ in range(5)]

    def blocked(info, kwargs, event):
        entered.set()
        assert event.wait(5), "stop 必须先向正在执行的站点发取消信号"
        cancelled.set()
        assert release.wait(5), "测试应释放资源清理步骤"
        cleaned.set()
        raise state.entry.CDPCancelled("取消")

    state.outcome = blocked
    runner = threading.Thread(target=plugin.run_sign_in)
    stopper = threading.Thread(target=lambda: (plugin.stop_service(), stopped.set()))
    runner.start()
    try:
        assert entered.wait(5)
        stopper.start()
        assert cancelled.wait(5)
        assert not stopped.is_set(), "stop 不能在当前站点资源释放前返回"
    finally:
        release.set()
        runner.join(5)
        if stopper.ident is not None:
            stopper.join(5)
    assert not runner.is_alive()
    assert not stopper.is_alive()
    assert cleaned.is_set() and stopped.is_set()
    assert len(state.visits) == 1, "取消后的队列站点不可继续访问"
    assert plugin.saved == []
    assert plugin.notifications == []
    assert state.updates == []


def test_reload_does_not_publish_previous_lifecycle(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    plugin.init_plugin({"site_ids": [1]})
    old_event = plugin._stop_event
    plugin.init_plugin({"site_ids": [1], "notify": False})
    plugin.run_sign_in(_event=old_event)
    assert state.visits == []
    assert plugin.saved == []
    plugin.run_sign_in()
    assert len(state.visits) == 1
    assert plugin.get_data("latest")[0]["status"] == "success"
    assert plugin.notifications == []


def test_instances_share_batch_exclusion(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    plugin.init_plugin({"site_ids": [1]})
    other = state.entry.PTCDPSignIn()
    other.init_plugin({"site_ids": [1]})
    entered, release = threading.Event(), threading.Event()

    def blocked(info, kwargs, event):
        entered.set()
        assert release.wait(5)
        return {"status": "success", "message": "完成", "cookie": ""}

    state.outcome = blocked
    runner = threading.Thread(target=plugin.run_sign_in)
    runner.start()
    try:
        assert entered.wait(5)
        other.run_sign_in()
        assert len(state.visits) == 1
        assert other.saved == []
    finally:
        release.set()
        runner.join(5)
        other.stop_service()
    assert not runner.is_alive()


def test_worker_stop_does_not_wait_for_itself(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    plugin.init_plugin({"site_ids": [1]})

    def stop_inside_process(info, kwargs, event):
        plugin.stop_service()
        assert event.is_set()
        return {"status": "success", "message": "取消后不得发布", "cookie": ""}

    state.outcome = stop_inside_process
    runner = threading.Thread(target=plugin.run_sign_in, daemon=True)
    runner.start()
    runner.join(5)
    assert not runner.is_alive()
    assert plugin.saved == []
    assert plugin.notifications == []


def test_daily_cache_isolated_by_id_and_url_not_name(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    plugin.init_plugin({"site_ids": ["__all__"], "retry_count": 0})
    plugin.run_sign_in()
    state.sites.append(state.site(2))
    plugin.run_sign_in()
    assert [info["id"] for info, _ in state.visits] == [1, 2]
    assert len(plugin.get_data("history_20261021")) == 2
    state.sites[0].url = "https://changed.test/"
    plugin.run_sign_in()
    assert [info["id"] for info, _ in state.visits] == [1, 2, 1]


def test_dual_selection_runs_once_and_only_failure_retries(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    calls = []

    def outcome(info, kwargs, event):
        calls.append((kwargs["signin"], kwargs["login"]))
        return {"status": "success" if kwargs["signin"] else "login",
                "message": "完成", "cookie": "", "login": "登录结果",
                "login_status": "login_failed" if len(calls) == 1 else "login"}

    state.outcome = outcome
    plugin.init_plugin({"site_ids": [1], "login_site_ids": [1], "retry_count": 1})
    plugin.run_sign_in()
    assert calls == [(True, True)]
    retry = state.jobs["retry"]
    retry["func"](**retry["kwargs"])
    assert calls == [(True, True), (False, True)]
    assert plugin.get_data("latest")[0]["signin_status"] == "success"
    assert plugin.get_data("latest")[0]["login_status"] == "login"
    assert "retry" not in state.jobs or state.jobs["retry"] is retry


def test_new_round_invalidates_old_retry(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    state.outcome = {"status": "failed", "message": "失败", "cookie": ""}
    plugin.init_plugin({"site_ids": [1], "retry_count": 1})
    plugin.run_sign_in()
    old_retry = state.jobs["retry"]
    state.outcome = {"status": "success", "message": "完成", "cookie": ""}
    plugin.run_sign_in()
    old_retry["func"](**old_retry["kwargs"])
    assert len(state.visits) == 2
    assert plugin.get_data("latest")[0]["signin_status"] == "success"


def test_clear_today_is_consumed_once_and_preserves_other_days(lifecycle):
    state = lifecycle
    plugin = state.instance
    plugin.data.update(latest=[{"old": True}], history_20261021=[{"old": True}],
                       history_20261020=[{"previous": True}])
    plugin.init_plugin({"clear_today": True})
    assert plugin.get_data("latest") is None
    assert plugin.get_data("history_20261021") is None
    assert plugin.get_data("history_20261020") == [{"previous": True}]
    assert plugin.configs[-1]["clear_today"] is False
    plugin.save_data("history_20261021", [{"new": True}])
    plugin.init_plugin(plugin.configs[-1])
    assert plugin.get_data("history_20261021") == [{"new": True}]


@pytest.mark.parametrize("mutated", ["cookie", "url", None])
def test_authenticated_cookie_compare_before_write(lifecycle, mutated):
    state = lifecycle
    plugin = state.instance
    original = state.site(1)
    state.sites = [original]
    plugin.init_plugin({"site_ids": [1], "retry_count": 0})

    def outcome(info, kwargs, event):
        if mutated == "cookie":
            original.cookie = "user-update=1"
        elif mutated == "url":
            original.url = "https://user-changed.test/"
        # core 认证确凿但签到失败时也可以返回有效 Cookie。
        return {"status": "failed", "message": "签到失败", "cookie": "authenticated=1"}

    state.outcome = outcome
    plugin.run_sign_in()
    assert state.updates == ([] if mutated else [(1, {"cookie": "authenticated=1"})])


def test_history_retained_for_thirty_days(lifecycle):
    state = lifecycle
    plugin = state.instance
    state.sites = [state.site(1)]
    plugin.data.update(history_20260921=[{}], history_20260922=[{}], unrelated={"keep": True})
    plugin.init_plugin({"site_ids": [1]})
    plugin.run_sign_in()
    assert plugin.get_data("history_20260921") is None
    assert plugin.get_data("history_20260922") == [{}]
    assert plugin.get_data("unrelated") == {"keep": True}


@pytest.mark.parametrize("note", [
    {"username": "private-user", "password": "private-pass", "token": "private-token"},
    '{"username":"private-user","password":"private-pass","token":"private-token"}',
])
def test_result_redacts_note_credentials_without_hiding_normal_message(lifecycle, note):
    """附加信息对象和 JSON 中的独立凭证不进入详情与通知。"""
    plugin = lifecycle.instance
    plugin.init_plugin({"cdp_url": "wss://browser.test/devtools/browser/id?token=cdp-secret"})
    text = plugin._safe_text(
        "登录失败 private-user private-pass private-token "
        "wss://browser.test/devtools/browser/id?token=cdp-secret；正常站点说明",
        {"note": note},
    )
    for secret in ("private-user", "private-pass", "private-token", "cdp-secret"):
        assert secret not in text
    assert "登录失败" in text
    assert "正常站点说明" in text
