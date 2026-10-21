"""
PT 站点 CDP 签到插件（MoviePilot V3 专用版）。
通过 Chrome DevTools Protocol (CDP) 直连真实浏览器或远程容器完成签到。
"""

from __future__ import annotations

import datetime
from datetime import datetime as dt
import logging
import threading
from typing import Any, List, Tuple

from app.db.oper.site import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
try:
    from .cdp import CDPSession
    from .core import PTCDPSigner, SignResult
except (ImportError, ValueError):
    from cdp import CDPSession
    from core import PTCDPSigner, SignResult
ALL_SITES = "__all__"


class PTCDPSignIn(_PluginBase):
    plugin_name = "PT站点CDP签到"
    plugin_desc = "通过 Chrome DevTools Protocol (CDP) 连接真实浏览器/远程容器进行 PT 站点自动签到与 Cookie 同步。"
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/media302.png"
    plugin_version = "1.0"
    plugin_author = "jianxcao"
    plugin_config_prefix = "ptcdp_"
    plugin_order = 30
    auth_level = 1

    # 配置属性
    _enabled: bool = False
    _notify: bool = True
    _cron: str = "20 8 * * *"
    _cdp_url: str = "http://127.0.0.1:9222"
    _timeout: int = 30
    _site_ids: List[Any] = []
    _all_sites: bool = False

    _lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        """加载配置"""
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cron = str(config.get("cron") or "20 8 * * *").strip()
        self._cdp_url = str(config.get("cdp_url") or "http://127.0.0.1:9222").strip()
        self._timeout = int(config.get("timeout") or 30)

        raw_sites = config.get("site_ids") or []
        self._all_sites = ALL_SITES in raw_sites
        self._site_ids = [s for s in raw_sites if s != ALL_SITES]

        if config.get("run_once"):
            logger.info("PT站点CDP签到：检测到保存后立即执行一次指令")
            threading.Thread(target=self.run_sign_in, daemon=True).start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return []
    def get_service(self) -> list[dict]:
        """注册定时服务"""
        if not self._enabled or not self._cron:
            return []
        from apscheduler.triggers.cron import CronTrigger
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as e:
            logger.error(f"PT站点CDP签到：Cron 表达式无效 ({self._cron}): {e}")
            return []
        return [
            {
                "id": "PTCDPSignIn.Run",
                "name": "PT站点CDP签到",
                "trigger": trigger,
                "func": self.run_sign_in,
                "kwargs": {}
            }
        ]

    def get_api(self) -> list[dict]:
        return []

    def get_form(self) -> Tuple[List[dict], dict]:
        """渲染插件设置表单"""
        try:
            sites = SiteOper().list_order_by_pri() or []
        except Exception:
            sites = []

        options = [{"title": "全部站点（含后续新增）", "value": ALL_SITES}]
        for site in sites:
            name = getattr(site, "name", None) or getattr(site, "url", "未知站点")
            sid = getattr(site, "id", None)
            if sid is not None:
                options.append({"title": name, "value": sid})

        alert_msg = {
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "density": "compact",
                "class": "mb-2",
                "text": "本插件通过 CDP 协议直连本地或远程 Chrome 浏览器（例如日常电脑上的 Chrome 或 NAS 上带持久化配置的桌面容器）。Chrome 启动时需添加参数：--remote-debugging-port=9222 --remote-allow-origins=*"
            }
        }

        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "保存后立即执行一次"}}]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cdp_url",
                                            "label": "Chrome CDP 调试地址",
                                            "placeholder": "http://127.0.0.1:9222",
                                            "hint": "远程 Chrome/Edge 地址与端口，跨机或容器通常填 http://宿主IP:9222",
                                            "persistent-hint": True
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "20 8 * * *"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "超时时间（秒）",
                                            "type": "number",
                                            "min": 10,
                                            "max": 120
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "site_ids",
                                            "label": "签到站点",
                                            "items": options,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "选择需要签到的站点，选「全部站点」则自动包括后续新增",
                                            "persistent-hint": True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [{"component": "VCol", "props": {"cols": 12}, "content": [alert_msg]}]
                    }
                ]
            }
        ]

        defaults = {
            "enabled": self._enabled,
            "notify": self._notify,
            "run_once": False,
            "cdp_url": self._cdp_url,
            "cron": self._cron,
            "timeout": self._timeout,
            "site_ids": [ALL_SITES] if self._all_sites else self._site_ids
        }

        return form, defaults

    def get_page(self) -> List[dict]:
        """展示签到历史记录列表与统计信息"""
        latest: List[dict] = self.get_data("latest") or []
        if not latest:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无签到记录。可进入插件设置开启后勾选「保存后立即执行一次」触发。"
                    }
                }
            ]

        total = len(latest)
        success = sum(1 for r in latest if r.get("status") in {"success", "already"})
        failed = total - success

        rows = []
        for r in latest:
            color = "success" if r.get("status") in {"success", "already"} else "error"
            badge = "已签到" if r.get("status") == "already" else ("成功" if r.get("status") == "success" else "失败")
            rows.append({
                "site": r.get("site", "未知"),
                "status": f"<span style='color: var(--v-theme-{color}, {color}); font-weight: bold;'>{badge}</span>",
                "message": r.get("message", ""),
                "time": r.get("time", "")
            })

        headers = [
            {"title": "站点", "key": "site"},
            {"title": "状态", "key": "status"},
            {"title": "说明", "key": "message"},
            {"title": "时间", "key": "time"}
        ]

        return [
            {
                "component": "VCard",
                "props": {"class": "mb-4", "variant": "tonal", "color": "primary"},
                "content": [
                    {
                        "component": "VCardText",
                        "text": f"总计执行站点：{total} | 成功：{success} | 失败：{failed}"
                    }
                ]
            },
            {
                "component": "VDataTable",
                "props": {
                    "headers": headers,
                    "items": rows,
                    "density": "comfortable"
                }
            }
        ]

    def stop_service(self) -> None:
        pass

    def run_sign_in(self) -> None:
        """运行一轮 CDP 签到"""
        if not self._lock.acquire(blocking=False):
            logger.warning("PT站点CDP签到：上一轮签到任务正在执行中，跳过本次触发")
            return

        try:
            self._do_sign_in()
        except Exception as e:
            logger.error(f"PT站点CDP签到：执行异常: {e}")
        finally:
            self._lock.release()

    def _do_sign_in(self) -> None:
        logger.info(f"PT站点CDP签到：开始执行签到任务，CDP 地址: {self._cdp_url}")

        # 1. 测试连通性
        session = CDPSession(self._cdp_url, timeout=float(self._timeout))
        ok, msg = session.test_connection()
        if not ok:
            logger.error(f"PT站点CDP签到：无法连接至 Chrome CDP: {msg}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="PT站点CDP签到失败",
                    text=f"无法连接到 Chrome 调试地址 ({self._cdp_url})：\n{msg}"
                )
            return

        # 2. 获取站点列表
        try:
            all_sites = SiteOper().list_order_by_pri() or []
        except Exception as e:
            logger.error(f"PT站点CDP签到：读取站点列表失败: {e}")
            return

        target_sites = []
        for site in all_sites:
            sid = getattr(site, "id", None)
            if self._all_sites or (sid in self._site_ids):
                target_sites.append(site)

        if not target_sites:
            logger.info("PT站点CDP签到：未选定任何要签到的站点")
            return

        signer = PTCDPSigner(self._cdp_url, timeout=float(self._timeout))
        results = []

        now_str = dt.now().strftime("%Y-%m-%d %H:%M:%S")

        for site in target_sites:
            name = getattr(site, "name", None) or getattr(site, "url", "未知站点")
            site_info = {
                "id": getattr(site, "id", None),
                "name": name,
                "url": getattr(site, "url", None),
                "cookie": getattr(site, "cookie", None),
                "ua": getattr(site, "ua", None)
            }
            try:
                res: SignResult = signer.sign_site(site_info)
                results.append({
                    "site": name,
                    "status": res.status,
                    "message": res.message,
                    "time": now_str
                })
                logger.info(f"PT站点CDP签到：[{name}] -> {res.status}: {res.message}")
            except Exception as e:
                logger.error(f"PT站点CDP签到：[{name}] 出现异常: {e}")
                results.append({
                    "site": name,
                    "status": "failed",
                    "message": f"执行异常: {e}",
                    "time": now_str
                })

        # 3. 保存签到数据
        self.save_data("latest", results)

        # 4. 发送通知
        if self._notify and results:
            success_count = sum(1 for r in results if r["status"] in {"success", "already"})
            fail_count = len(results) - success_count
            lines = [f"执行完成：共 {len(results)} 个站点，成功 {success_count}，失败 {fail_count}"]
            for r in results:
                status_label = "✅ 成功" if r["status"] == "success" else ("⚡️ 已签" if r["status"] == "already" else "❌ 失败")
                lines.append(f"• [{r['site']}] {status_label}: {r['message']}")

            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="PT站点CDP签到结果",
                text="\n".join(lines)
            )
