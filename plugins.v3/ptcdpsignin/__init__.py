"""MoviePilot V3 PT 站点 CDP 签到、登录与最近动向。"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from app.db.oper.site import SiteOper
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .cdp import CDPCancelled
from .core import PTCDPSigner, parse_rules

ALL_SITES = "__all__"
ALL_SITE_VALUES = {ALL_SITES, "all"}
SUCCESS = {"success", "already"}
DEFAULTS = {
    "enabled": False,
    "notify": True,
    "run_once": False,
    "cdp_url": "http://127.0.0.1:9222",
    "cron": "20 8 * * *",
    "timeout": 30,
    "site_ids": [],
    "login_site_ids": [],
    "read_activity": True,
    "site_rules": "",
    "concurrency": 2,
    "retry_count": 1,
    "retry_delay_minutes": 10,
    "clear_today": False,
}


class PTCDPSignIn(_PluginBase):
    """管理 V3 签到任务的配置、生命周期、日缓存和宿主展示。"""

    plugin_name = "PT站点CDP签到"
    plugin_desc = "通过 CDP 连接真实浏览器，自动签到、登录刷新 Cookie 并读取最近动向（仅 MoviePilot V3）。"
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/ptcdpsignin.png"
    plugin_version = "1.2"
    plugin_author = "jianxcao"
    plugin_config_prefix = "ptcdp_"
    plugin_order = 30
    auth_level = 1

    # 所有实例共享：重载或分身不能与上一轮同时操作浏览器和站点。
    _batch_lock = threading.Lock()

    def __init__(self) -> None:
        """初始化实例独立的取消信号、运行状态和一次性任务集合。"""
        super().__init__()
        self._condition = threading.Condition(threading.RLock())
        self._stop_event = threading.Event()
        self._active = False
        self._active_threads: set[int] = set()
        self._once_jobs: set[str] = set()
        self._round_id = 0
        self._config = dict(DEFAULTS, site_ids=[], login_site_ids=[])
        self._enabled = False

    @staticmethod
    def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
        """将数值配置限制在允许范围内，无效输入使用默认值。"""
        try:
            return max(minimum, min(maximum, int(value)))
        except (ValueError, TypeError):
            return default

    def init_plugin(self, config: dict = None) -> None:
        """先取消并等待旧轮释放资源，再应用新的配置和一次性操作。"""
        self.stop_service()
        values = dict(DEFAULTS)
        values.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
        for name in ("enabled", "notify", "read_activity", "run_once", "clear_today"):
            values[name] = bool(values[name])
        for name in ("site_ids", "login_site_ids"):
            selected = values[name] if isinstance(values[name], (list, tuple)) else []
            values[name] = [ALL_SITES if str(item) in ALL_SITE_VALUES else item for item in selected]
        values["cdp_url"] = str(values["cdp_url"] or DEFAULTS["cdp_url"]).strip()
        values["cron"] = str(values["cron"] or "").strip()
        values["timeout"] = self._integer(values["timeout"], 30, 5, 300)
        values["concurrency"] = self._integer(values["concurrency"], 2, 1, 10)
        values["retry_count"] = self._integer(values["retry_count"], 1, 0, 10)
        values["retry_delay_minutes"] = self._integer(values["retry_delay_minutes"], 10, 1, 1440)
        run_once, clear_today = values["run_once"], values["clear_today"]
        values["run_once"] = values["clear_today"] = False
        with self._condition:
            self._config = values
            self._enabled = values["enabled"]
            self._stop_event = threading.Event()
            if clear_today:
                self.del_data("history_" + self._now().strftime("%Y%m%d"))
                self.del_data("latest")
            # 必须在调度前消费操作开关，防止宿主重载时重复触发。
            if run_once or clear_today:
                self.update_config(dict(values))
            if run_once:
                threading.Thread(
                    target=self.run_sign_in,
                    kwargs={"force": True},
                    daemon=True,
                    name="PTCDPSignIn-RunOnce"
                ).start()

    @staticmethod
    def _now() -> datetime:
        """返回宿主时区的当前时间，统一调度和日缓存边界。"""
        return datetime.now(ZoneInfo(settings.TZ))

    def get_state(self) -> bool:
        """返回插件是否启用，供宿主展示和注册服务。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict]:
        """声明支持远程和前端命令执行。"""
        return [{
            "cmd": "/pt_cdp_signin",
            "event": EventType.PluginAction,
            "desc": "PT站点CDP签到",
            "category": "站点",
            "data": {"action": "pt_cdp_signin"}
        }]

    def get_api(self) -> list[dict]:
        """声明 HTTP 触发 API。"""
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET", "POST"],
            "summary": "立即运行CDP签到",
            "description": "立即触发一次PT站点CDP签到任务"
        }]

    def api_run(self) -> dict:
        """API 触发立即执行。"""
        threading.Thread(
            target=self.run_sign_in,
            kwargs={"force": True},
            daemon=True,
            name="PTCDPSignIn-Manual"
        ).start()
        return {"code": 0, "message": "已触发PT站点CDP签到"}

    def get_api(self) -> list[dict]:
        """声明本插件不注册额外接口。"""
        return []

    def get_service(self) -> list[dict]:
        """按宿主时区声明启用状态下的周期签到服务。"""
        if not self._enabled or not self._config["cron"]:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._config["cron"], timezone=settings.TZ)
        except (ValueError, TypeError):
            logger.error("PT站点CDP签到：Cron 表达式无效")
            return []
        return [{"id": "PTCDPSignIn.Run", "name": "PT站点CDP签到", "trigger": trigger,
                 "func": self.run_sign_in, "kwargs": {}}]

    def _schedule_once(self, job_id: str, delay: float, event: threading.Event,
                       retry_keys: list[str] = None, attempt: int = 0,
                       round_id: int = None) -> None:
        """向宿主登记带生命周期标记的一次性执行或失败重试任务。"""
        # 调度和 stop 的移除操作串行化，避免 stop 后又登记新的重试。
        with self._condition:
            if event.is_set() or event is not self._stop_event:
                return
            self._once_jobs.add(job_id)
            try:
                registered = scheduler_sdk.add_plugin_once_job(
                    self.__class__.__name__, job_id, self.run_sign_in,
                    name="PT站点CDP签到" if not attempt else "PT站点CDP签到失败重试",
                    delay_seconds=delay,
                    func_kwargs={"retry_keys": retry_keys, "attempt": attempt,
                                 "_event": event, "_job_id": job_id, "_round_id": round_id},
                )
                if not registered:
                    self._once_jobs.discard(job_id)
                    logger.warning("PT站点CDP签到：宿主调度器未接受一次性任务")
            except Exception:
                self._once_jobs.discard(job_id)
                logger.error("PT站点CDP签到：登记宿主一次性任务失败")

    def stop_service(self) -> None:
        """取消当前生命周期和待执行任务，外部调用等待运行轮释放资源。"""
        # 先发取消信号；网络等待由 CDP 层响应，不再启动后续站点。
        self._stop_event.set()
        with self._condition:
            jobs = tuple(self._once_jobs)
            self._once_jobs.clear()
            for job_id in jobs:
                try:
                    scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, job_id)
                except Exception:
                    logger.warning("PT站点CDP签到：移除宿主一次性任务失败")
            # 回调或工作线程中停止不能等待自己；外部停止必须等待页面清理完毕。
            if threading.get_ident() not in self._active_threads:
                self._condition.wait_for(lambda: not self._active)

    @staticmethod
    def _site_key(info: dict) -> str:
        """以站点主键和规范 URL 生成跨轮稳定且不混同名站点的缓存键。"""
        url = str(info.get("url") or "").strip()
        parts = urlsplit(url)
        canonical = urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                               parts.path.rstrip("/"), "", ""))
        return f"{info.get('id')}|{canonical}"

    @staticmethod
    def _selected(site_id: Any, selected: list) -> bool:
        """判断站点是否被显式选择或被全部站点选项涵盖。"""
        values = {str(value) for value in selected}
        return bool(values & ALL_SITE_VALUES) or str(site_id) in values

    @staticmethod
    def _site_info(site: Any) -> dict:
        """提取签到核心需要的站点配置，包含登录凭证和访问选项。"""
        fields = ("id", "name", "domain", "url", "cookie", "ua", "proxy", "rule", "apikey",
                  "token", "note", "timeout", "username", "user", "account", "password", "passwd", "pwd")
        return {key: getattr(site, key, None) for key in fields}

    @staticmethod
    def _proxies_for(info: dict) -> dict | None:
        """仅为开启代理的站点读取宿主代理或环境代理配置。"""
        if not info.get("proxy"):
            return None
        proxy = str(getattr(settings, "PROXY_HOST", "") or "").strip()
        if not proxy:
            proxy = next((os.environ[key].strip() for key in
                          ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                          if os.environ.get(key, "").strip()), "")
        return {"http": proxy, "https": proxy} if proxy else None

    def _safe_text(self, value: Any, info: dict, cookie: str = "") -> str:
        """隐藏结果中的已知凭证和敏感参数，并限制展示文本长度。"""
        text = str(value or "")
        sensitive = ("cookie", "password", "passwd", "pwd", "token", "apikey", "api_key",
                     "username", "user", "account")
        note = info.get("note")
        if isinstance(note, str):
            try:
                note = json.loads(note)
            except (ValueError, TypeError):
                note = {}
        note = note if isinstance(note, Mapping) else {}
        secrets = [self._config["cdp_url"], cookie, info.get("note")]
        secrets.extend(source.get(key) for source in (info, note) for key in sensitive)
        for secret in secrets:
            if secret and len(str(secret)) >= 3:
                text = text.replace(str(secret), "[已隐藏]")
        text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1***@", text)
        text = re.sub(r"((?:token|password|passwd|pwd|apikey|api_key|cookie)\s*[=:]\s*)[^\s,;]+",
                      r"\1***", text, flags=re.I)
        return text[:2000]

    def run_sign_in(self, retry_keys: list[str] = None, attempt: int = 0,
                    _event: threading.Event = None, _job_id: str = None,
                    _round_id: int = None, force: bool = False, event: Any = None) -> None:
        """在跨实例互斥下运行有效生命周期的任务并拒绝过期重试。"""
        if event:
            event_data = getattr(event, "event_data", None) or {}
            if event_data.get("action") != "pt_cdp_signin":
                return
            force = True
        event = _event if _event is not None else self._stop_event
        with self._condition:
            if event.is_set() or event is not self._stop_event:
                return
            if _round_id is not None and _round_id != self._round_id:
                return
            if _job_id:
                self._once_jobs.discard(_job_id)
            if not self._batch_lock.acquire(blocking=False):
                logger.info("PT站点CDP签到：已有一轮任务运行，跳过本次触发")
                return
            if not attempt:
                self._round_id += 1
                if "retry" in self._once_jobs:
                    try:
                        scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "retry")
                    except Exception:
                        logger.warning("PT站点CDP签到：移除过期重试任务失败")
                    self._once_jobs.discard("retry")
            self._active = True
            self._active_threads.add(threading.get_ident())
        try:
            self._run_batch(event, retry_keys, attempt, force=force)
        except CDPCancelled:
            pass
        except Exception:
            # 异常可能携带远端 URL、Cookie 或密码，禁止原样记录。
            logger.error("PT站点CDP签到：本轮执行异常，已停止")
        finally:
            with self._condition:
                self._active_threads.discard(threading.get_ident())
                self._active = False
                self._batch_lock.release()
                self._condition.notify_all()

    def _check_current(self, event: threading.Event) -> None:
        """在取消或生命周期已被替换时终止后续访问和结果发布。"""
        if event.is_set() or event is not self._stop_event:
            raise CDPCancelled("任务已取消")

    def _run_batch(self, event: threading.Event, retry_keys: list[str] | None, attempt: int) -> None:
        """并发执行未完成的站点任务，串行提交结果并调度失败重试。"""
        self._check_current(event)
        config = self._config
        now = self._now()
        day = now.strftime("%Y%m%d")
        history_key = "history_" + day
        history = self.get_data(history_key) or []
        records = {row["key"]: dict(row) for row in history
                   if isinstance(row, dict) and row.get("key")}
        custom_rules = parse_rules(config["site_rules"])
        targets = []
        retry_set = set(retry_keys) if retry_keys is not None else None
        for site in SiteOper().list_order_by_pri() or []:
            self._check_current(event)
            info = self._site_info(site)
            key = self._site_key(info)
            if retry_set is not None and key not in retry_set:
                continue
            signin = self._selected(info["id"], config["site_ids"])
            login = self._selected(info["id"], config["login_site_ids"])
            previous = records.get(key, {})
            # 两种任务独立缓存，失败重试不会再次执行已成功的另一种操作。
            signin = signin and previous.get("signin_status") not in SUCCESS
            login = login and previous.get("login_status") != "login"
            if signin or login:
                targets.append((info, key, signin, login))
        if not targets:
            logger.info("PT站点CDP签到：无待执行站点（未选择或今日已完成）")
            return

        def process(target: tuple) -> tuple:
            """为单个站点创建独立核心实例并保持取消异常的传播。"""
            info, key, signin, login = target
            ident = threading.get_ident()
            with self._condition:
                self._active_threads.add(ident)
            try:
                self._check_current(event)
                # 每站独立 signer/process/page，不共享页面或登录上下文。
                signer = PTCDPSigner(config["cdp_url"], timeout=config["timeout"], stop_event=event)
                result = signer.process(info, signin=signin, login=login,
                                        read_activity=config["read_activity"], custom_rules=custom_rules,
                                        proxies=self._proxies_for(info))
                self._check_current(event)
                return target, result
            except CDPCancelled:
                raise
            except Exception:
                return target, {"status": "failed" if signin else "login_failed",
                                "message": "站点执行异常", "login_status": "login_failed" if login else "",
                                "login": "登录异常" if login else "", "cookie": ""}
            finally:
                with self._condition:
                    self._active_threads.discard(ident)

        completed = []
        with ThreadPoolExecutor(max_workers=config["concurrency"], thread_name_prefix="ptcdp") as executor:
            futures = [executor.submit(process, target) for target in targets]
            try:
                for future in as_completed(futures):
                    self._check_current(event)
                    completed.append(future.result())
            except CDPCancelled:
                for future in futures:
                    future.cancel()
                raise
        self._check_current(event)
        failed = []
        changed = []
        # 结果和持久化在主轮串行提交，取消/重载后不写入旧轮结果或通知。
        with self._condition:
            self._check_current(event)
            for (info, key, signin, login), result in completed:
                self._check_current(event)
                previous = records.get(key, {})
                cookie = str(result.get("cookie") or "")
                clean = lambda value: self._safe_text(value, info, cookie)
                row = dict(previous)
                row.update({"key": key, "site_id": info["id"], "site": str(info.get("name") or "未知站点"),
                            "time": self._now().strftime("%Y-%m-%d %H:%M:%S"),
                            "status": str(result.get("status") or "failed"),
                            "message": clean(result.get("message")),
                            "signin": previous.get("signin", False) or signin,
                            "login_requested": previous.get("login_requested", False) or login})
                if signin:
                    row["signin_status"] = result.get("status") if result.get("status") in SUCCESS else "failed"
                    row["signin_message"] = clean(result.get("message"))
                if login or result.get("login_status"):
                    row["login_status"] = str(result.get("login_status") or
                                              ("login" if result.get("status") == "login" else "login_failed"))
                    row["login"] = clean(result.get("login") or result.get("message"))
                if config["read_activity"]:
                    row["activity"] = clean(result.get("activity"))
                    row["activity_source"] = clean(result.get("activity_source"))
                records[key] = row
                changed.append(row)
                if (signin and row["signin_status"] not in SUCCESS) or (login and row.get("login_status") != "login"):
                    failed.append(key)
                # core 合约：非空 Cookie 只在认证确凿时返回，即使签到本身失败。
                # 失败签到不影响已认证 Cookie 的有效性，空值永远不覆盖原值。
                if cookie:
                    self._check_current(event)
                    self._persist_cookie(info, cookie)
            self._check_current(event)
            rows = list(records.values())
            self.save_data(history_key, rows)
            self._check_current(event)
            self.save_data("latest", rows)
            self._prune_history(now)
            if config["notify"]:
                self._check_current(event)
                self._notify_results(changed)
            if failed and attempt < config["retry_count"]:
                self._schedule_once("retry", delay=config["retry_delay_minutes"] * 60,
                                    event=event, retry_keys=failed, attempt=attempt + 1,
                                    round_id=self._round_id)

    def _persist_cookie(self, info: dict, cookie: str) -> None:
        """写前比较原 Cookie 和 URL，避免覆盖已观察到的并发用户修改。"""
        try:
            oper = SiteOper()
            current = oper.get(info["id"])
            if not current:
                return
            if (str(getattr(current, "cookie", None) or "") != str(info.get("cookie") or "")
                    or str(getattr(current, "url", None) or "") != str(info.get("url") or "")):
                logger.info("PT站点CDP签到：站点配置已变更，跳过本轮 Cookie 回写")
                return
            if cookie != str(info.get("cookie") or ""):
                oper.update(info["id"], {"cookie": cookie})
        except Exception:
            logger.warning("PT站点CDP签到：保存已认证 Cookie 失败")

    def _prune_history(self, now: datetime) -> None:
        """读取宿主数据键并删除三十天保留窗口之外的按日历史。"""
        oldest = (now.date() - timedelta(days=29)).strftime("%Y%m%d")
        # 宿主 get_data(None) 返回 PluginData ORM 列表。
        data = self.get_data() or []
        keys = data.keys() if isinstance(data, dict) else (
            item.get("key", "") if isinstance(item, dict) else getattr(item, "key", "") for item in data)
        for key in list(keys):
            if re.fullmatch(r"history_\d{8}", str(key)) and str(key)[8:] < oldest:
                self.del_data(key)

    def _notify_results(self, rows: list[dict]) -> None:
        """精简通知文案，直接输出核心状态，避免文本过长被渠道截断。"""
        lines = []
        for row in rows:
            site = row.get("site", "未知站点")
            parts = []
            if row.get("signin"):
                st = row.get("signin_status")
                label = "签到成功" if st == "success" else ("今日已签" if st == "already" else "签到失败")
                parts.append(label)
            if row.get("login_requested"):
                label = "登录成功" if row.get("login_status") == "login" else "登录失败"
                parts.append(label)
            status_text = "，".join(parts) if parts else "已完成"
            lines.append(f"{site}: {status_text}")
        self.post_message(mtype=NotificationType.SiteMessage, title="【PT站点CDP签到】", text="\n".join(lines))

    def get_form(self) -> tuple[list[dict], dict]:
        """生成基础设置和可折叠高级设置，并返回全部配置默认值。"""
        try:
            sites = SiteOper().list_order_by_pri() or []
        except Exception:
            sites = []
        options = [{"title": "全部站点（含后续新增）", "value": ALL_SITES}]
        options += [{"title": getattr(site, "name", "未知站点"), "value": site.id}
                    for site in sites if getattr(site, "id", None) is not None]

        def field(component: str, model: str, label: str, **props: Any) -> dict:
            """构造绑定配置字段的标准表单组件。"""
            return {"component": component, "props": {"model": model, "label": label, **props}}

        def row(*fields: dict) -> dict:
            """将表单组件放入响应式栅格行。"""
            return {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 12 // len(fields)}, "content": [item]}
                for item in fields]}

        form = [{"component": "VForm", "content": [
            row(field("VSwitch", "enabled", "启用插件"), field("VSwitch", "notify", "发送通知"),
                field("VSwitch", "run_once", "保存后立即执行一次")),
            row(field("VTextField", "cdp_url", "Chrome CDP 地址",
                      hint="唯一的 CDP 地址；支持 http(s)://调试地址 或完整 ws(s)://browser 端点。无需单独 Token。",
                      **{"persistent-hint": True})),
            row(field("VCronField", "cron", "执行周期", placeholder=DEFAULTS["cron"])),
            row(field("VSelect", "site_ids", "签到站点", items=options, multiple=True, chips=True, clearable=True)),
            row(field("VSelect", "login_site_ids", "登录站点", items=options, multiple=True, chips=True, clearable=True,
                      hint="登录凭证读取站点管理的附加信息；同站同时选择签到和登录只访问一轮。",
                      **{"persistent-hint": True})),
            {"component": "VExpansionPanels", "content": [{"component": "VExpansionPanel", "content": [
                {"component": "VExpansionPanelTitle", "text": "高级配置"},
                {"component": "VExpansionPanelText", "content": [
                    row(field("VTextField", "timeout", "请求超时（秒）", type="number", min=5, max=300),
                        field("VTextField", "concurrency", "站点并发数", type="number", min=1, max=10)),
                    row(field("VTextField", "retry_count", "失败重试次数", type="number", min=0, max=10),
                        field("VTextField", "retry_delay_minutes", "重试间隔（分钟）", type="number", min=1, max=1440)),
                    row(field("VSwitch", "read_activity", "读取站点最近动向"),
                        field("VSwitch", "clear_today", "保存时清除今日缓存（仅一次）")),
                    row(field("VTextarea", "site_rules", "自定义站点规则（JSON）", rows=8,
                              hint="按域名指定签到、登录和动向规则；格式与 PT 站点签到规则一致。",
                              **{"persistent-hint": True})),
                ]},
            ]}]},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
             "text": "请使用可信网络中的持久化 Chrome/Edge CDP 浏览器。仅关闭本插件创建的标签页，不关闭浏览器。成功任务按日缓存；失败任务由宿主调度器重试。"}},
        ]}]
        return form, dict(self._config)

    @staticmethod
    def _table(rows: list[dict], headers: list[tuple[str, str]]) -> dict:
        """构造以普通文本展示结果的标准数据表格。"""
        return {"component": "VDataTable", "props": {
            "headers": [{"title": title, "key": key} for title, key in headers],
            "items": rows, "density": "compact", "items-per-page": 10,
            "no-data-text": "暂无记录", "class": "mb-4",
        }}

    def get_page(self) -> list[dict]:
        """分区展示签到、登录、最近动向和最近三十天历史。"""
        latest = self.get_data("latest") or []
        signin_rows = []
        login_rows = []
        activity_rows = []
        for record in latest:
            base = {"site": record.get("site", "未知站点"), "time": record.get("time", "")}
            if record.get("signin", "signin_status" not in record and record.get("status") in SUCCESS | {"failed"}):
                status = record.get("signin_status", record.get("status"))
                signin_rows.append(dict(base, status={"success": "成功", "already": "今日已签"}.get(status, "失败"),
                                        message=record.get("signin_message", record.get("message", ""))))
            if record.get("login_requested") or record.get("login_status"):
                login_rows.append(dict(base, status="成功" if record.get("login_status") == "login" else "失败",
                                       message=record.get("login", "")))
            if record.get("activity"):
                activity_rows.append(dict(base, activity=record["activity"], source=record.get("activity_source", "")))
        headers = [("站点", "site"), ("状态", "status"), ("说明", "message"), ("时间", "time")]
        content = [
            {"component": "VCardTitle", "text": "签到结果"}, self._table(signin_rows, headers),
            {"component": "VCardTitle", "text": "登录结果"}, self._table(login_rows, headers),
            {"component": "VCardTitle", "text": "最近动向"},
            self._table(activity_rows, [("站点", "site"), ("最近动向", "activity"), ("来源", "source"), ("读取时间", "time")]),
        ]
        panels = []
        today = self._now().date()
        for offset in range(30):
            date = today - timedelta(days=offset)
            records = self.get_data("history_" + date.strftime("%Y%m%d")) or []
            if not records:
                continue
            rows = []
            for record in records:
                rows.append({"site": record.get("site", "未知站点"),
                             "signin": {"success": "成功", "already": "今日已签", "failed": "失败"}.get(record.get("signin_status"), "未执行"),
                             "login": {"login": "成功", "login_failed": "失败"}.get(record.get("login_status"), "未执行"),
                             "message": record.get("signin_message", record.get("message", "")),
                             "activity": record.get("activity", ""), "time": record.get("time", "")})
            panels.append({"component": "VExpansionPanel", "content": [
                {"component": "VExpansionPanelTitle", "text": f"{date.isoformat()} · {len(rows)} 个站点"},
                {"component": "VExpansionPanelText", "content": [self._table(rows, [
                    ("站点", "site"), ("签到", "signin"), ("登录", "login"), ("说明", "message"),
                    ("最近动向", "activity"), ("时间", "time")])]},
            ]})
        content += [{"component": "VCardTitle", "text": "最近 30 天历史"},
                    {"component": "VExpansionPanels", "content": panels}]
        return [{"component": "VCard", "props": {"variant": "flat"}, "content": content}]
