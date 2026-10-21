"""
PT 站点 CDP 签到核心逻辑与多引擎驱动。
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
try:
    from .cdp import CDPSession
except (ImportError, ValueError):
    from cdp import CDPSession
logger = logging.getLogger(__name__)

# 常见成功与已签到标识
SUCCESS_KEYWORDS = [
    "签到成功", "簽到成功", "本次获得", "本次獲得", "获得魔力", "獲得魔力",
    "签到得", "簽到得", "打卡成功", "连续签到", "連續簽到", "已成功签到",
    "查看签到记录", "查看簽到記錄"
]

ALREADY_KEYWORDS = [
    "已经签到", "已經簽到", "已签到", "已簽到", "今日已签", "今日已簽",
    "今日已打卡", "请不要重复签到", "請不要重複簽到", "您今天已经签到过了",
    "您今天已經簽到過了", "明天再来", "明天再來"
]

CLOUDFLARE_PATTERNS = [
    "just a moment", "cf-chl-widget", "cf_chl_opt", "challenge-platform", "turnstile"
]


@dataclass
class SignResult:
    status: str  # "success" | "already" | "failed"
    message: str


class PTCDPSigner:
    """PT 站点 CDP 驱动签到器"""

    def __init__(self, cdp_url: str, timeout: float = 30.0):
        self.cdp_url = cdp_url
        self.timeout = timeout
        self.session = CDPSession(cdp_url, timeout=timeout)

    def sign_site(self, site_info: dict[str, Any]) -> SignResult:
        """
        对单个站点执行签到：
        1. 尝试从 Chrome CDP 提取最新鲜活 Cookie
        2. 如果拿到 Cookie，优先走轻量 HTTP 快速签到
        3. 若 HTTP 遇到 Cloudflare 盾、403、防爬拦截或未确认，触发 CDP 浏览器原生打开 Tab 访问
        4. 在真实浏览器环境里等待页面加载并尝试点击签到按钮
        """
        site_name = site_info.get("name") or site_info.get("url") or "未知站点"
        base_url = (site_info.get("url") or "").rstrip("/")
        if not base_url:
            return SignResult("failed", "站点 URL 为空")

        logger.info(f"[{site_name}] 开始 CDP 驱动签到流程...")

        # 1. 尝试从 Chrome 抓取最新的 Cookie
        cdp_cookie = ""
        try:
            cdp_cookie = self.session.get_cookie_header_for_url(base_url)
            if cdp_cookie:
                logger.info(f"[{site_name}] 成功通过 CDP 从 Chrome 同步最新 Cookie")
        except Exception as e:
            logger.warning(f"[{site_name}] 尝试通过 CDP 同步 Cookie 失败: {e}")

        # 如果 Chrome 没有该站点 Cookie，回退使用 MoviePilot「站点管理」配置的 Cookie
        effective_cookie = cdp_cookie or site_info.get("cookie") or ""

        # 2. 尝试走轻量 HTTP 快速签到（若有有效 Cookie）
        if effective_cookie:
            http_res = self._try_http_sign(base_url, effective_cookie, site_info.get("ua"))
            if http_res.status in {"success", "already"}:
                return http_res
            # 如果是明确的未登录或者需要 Cloudflare 挑战，则回退到真实浏览器 CDP 操作
            logger.info(f"[{site_name}] HTTP 签到未完成 ({http_res.message})，转入 CDP 真实浏览器执行...")

        # 3. 真实浏览器 CDP 页面签到
        return self._sign_via_cdp_browser(base_url, site_name)

    def _try_http_sign(self, base_url: str, cookie: str, user_agent: str | None = None) -> SignResult:
        """尝试通过 HTTP 访问经典 attendance.php 签到"""
        target_url = urllib.parse.urljoin(base_url + "/", "attendance.php")
        headers = {
            "User-Agent": user_agent or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": base_url + "/",
        }

        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                return self._parse_html_result(html)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 某些站点首页即可完成或入口不是 attendance.php
                return SignResult("failed", f"HTTP 404 (无 attendance.php 入口)")
            return SignResult("failed", f"HTTP 请求失败 (Code {e.code})")
        except Exception as e:
            return SignResult("failed", f"HTTP 访问异常: {e}")

    def _sign_via_cdp_browser(self, base_url: str, site_name: str) -> SignResult:
        """在 Chrome 后台新建标签页，打开页面并模拟点击或等待签到"""
        # 常见签到页面候选路径
        paths = ["attendance.php", "index.php", ""]
        last_error = ""

        # 点击签到按钮的常见选择器脚本
        click_script = """
        (function() {
            // 常见的 PT 站点签到按钮选择器或文本
            const selectors = [
                '#sign_in', '#signIn', '.sign_in', '.signin',
                'a[href*="attendance.php"]',
                'input[value*="签到"]', 'input[value*="打卡"]',
                'button:contains("签到")', 'a:contains("签到")'
            ];
            for (let sel of selectors) {
                try {
                    let el = document.querySelector(sel);
                    if (el) {
                        el.click();
                        return "clicked: " + sel;
                    }
                } catch(e) {}
            }
            // 遍历所有按钮与链接
            const elements = Array.from(document.querySelectorAll('a, button, input[type="button"], input[type="submit"]'));
            for (let el of elements) {
                const text = (el.innerText || el.value || '').trim();
                if (/^(签到|打卡|立即签到|每日签到|點擊簽到)$/i.test(text)) {
                    el.click();
                    return "clicked by text: " + text;
                }
            }
            return "no sign button found";
        })()
        """

        for p in paths:
            target_url = urllib.parse.urljoin(base_url + "/", p)
            try:
                res = self.session.navigate_and_evaluate(
                    url=target_url,
                    script=click_script,
                    wait_seconds=4.0,
                    page_timeout=30.0
                )
                text = res.get("text", "")
                parsed = self._parse_html_result(text)
                if parsed.status in {"success", "already"}:
                    return parsed

                # 检查是否遇到了 Cloudflare 挑战
                if any(k in text.lower() for k in CLOUDFLARE_PATTERNS):
                    return SignResult("failed", "页面处于 Cloudflare 质询拦截中，请在 Chrome 界面手动通过一次")

                last_error = f"未检测到成功标识 (页面标题: {res.get('title', '')})"
            except Exception as e:
                last_error = str(e)

        return SignResult("failed", last_error or "未完成签到")

    @staticmethod
    def _parse_html_result(content: str) -> SignResult:
        """从页面内容或文本中匹配签到状态与获得的魔力值/奖励"""
        if not content:
            return SignResult("failed", "页面内容为空")

        for kw in ALREADY_KEYWORDS:
            if kw in content:
                return SignResult("already", f"今日已签到 ({kw})")

        for kw in SUCCESS_KEYWORDS:
            if kw in content:
                # 提取可能包含的奖励数字（如：获得 50 个魔力值）
                match = re.search(r"(获得|獲得|得)\s*([0-9\.]+)\s*(点|個|个|魔力|分)", content)
                bonus = f"，获得 {match.group(2)} {match.group(3)}" if match else ""
                return SignResult("success", f"签到成功{bonus}")

        return SignResult("failed", "未匹配到签到成功或已签到信息")
