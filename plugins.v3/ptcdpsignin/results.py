"""只按可见、非说明性的状态证据判定签到，不能因点击成功而判成功。"""
from __future__ import annotations

import json
import re
from html import escape
from html.parser import HTMLParser

from .rules import (SignResult, DEFAULT_SUCCESS_PATTERN, DEFAULT_ALREADY_PATTERN,
                    CF_INTERSTITIAL_PATTERN, WAF_PATTERN, SECOND_FACTOR_PATTERN,
                    RULE_CONTEXT_PATTERN, extract_bonus)


STREAK_PATTERN = r"连续签到|連續簽到"
TODAY_SIGNED_PATTERN = r"(?:今日|今天)(?:已经|已經|已)(?:签到|簽到|签|簽)|(?:今日|今天)(?:的)?状态\s*[:：]\s*已(?:签到|簽到)"


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.controls = []
        self.markup = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = re.sub(r"\s+", "", attributes.get("style") or "").lower()
        hidden = (tag in {"script", "style", "template", "noscript", "head"}
                  or "hidden" in attributes or attributes.get("aria-hidden") == "true"
                  or attributes.get("type", "").lower() == "hidden"
                  or "display:none" in style or "visibility:hidden" in style)
        hidden = hidden or bool(self.stack and self.stack[-1][1])
        if tag not in {"input", "img", "br", "hr", "meta", "link", "area", "source", "wbr", "embed"}:
            self.stack.append((tag, hidden))
        if not hidden:
            self.markup.append(self.get_starttag_text())
            if tag in {"p", "div", "tr", "li", "br", "section", "h1", "h2", "h3"}:
                self.parts.append("\n")
            if tag == "input" and attributes.get("type", "text").lower() in {"submit", "button", "image"} and "disabled" not in attributes:
                self.controls.append(attributes.get("value", ""))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {"p", "div", "tr", "li", "section", "button", "a", "b"}:
            self.markup.append("</" + tag + ">")
            if tag != "b":
                self.parts.append("\n")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)
            self.markup.append(escape(data))
            if any(tag == "button" for tag, hidden in self.stack if not hidden):
                self.controls.append(data)


def visible(content):
    parser = VisibleHTML()
    parser.feed(content or "")
    return parser


def plain_text(content):
    return re.sub(r"\s+", " ", " ".join(visible(content).parts)).strip()


def challenge_pending(content):
    return bool(re.search(CF_INTERSTITIAL_PATTERN + "|" + WAF_PATTERN, content or "", re.I))


def authenticated(content):
    parser = visible(content)
    markup = " ".join(parser.markup)
    return bool(re.search(r"(?:href\s*=\s*['\"][^'\"]*(?:logout|signout)|退出登录|退出登錄|登出|注销|我的控制面板|我的面板)", markup, re.I)) and not login_page(content)


def login_page(content):
    parser = visible(content)
    markup = " ".join(parser.markup)
    return bool(re.search(r"<input\b[^>]*type\s*=\s*['\"]?password", markup, re.I)
                or re.search(r"请先登录|請先登入|session expired", " ".join(parser.parts), re.I))


def _json_result(content, rule):
    try:
        body = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    state = str(body.get("state") or body.get("status") or "").lower()
    if body.get("success") is False or state in {"fail", "failed", "error", "false"}:
        return SignResult("failed", "站点接口明确返回签到失败")
    definition = rule.get("success_json") or {}
    if definition:
        try:
            value = body
            for key in str(definition.get("field") or "").strip("/").split("/"):
                value = value[int(key)] if isinstance(value, list) else value[key]
            if str(value).lower() in {str(item).lower() for item in definition.get("values", [])}:
                return SignResult("success", "签到成功")
        except (KeyError, IndexError, TypeError, ValueError):
            pass
    compact = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    for key, status in (("already_keywords", "already"), ("success_keywords", "success")):
        # 连续签到关键词留给下方带提交/认证及未签优先保护的可见状态判定。
        if any(str(word) in compact and not re.search(STREAK_PATTERN, str(word))
               for word in rule.get(key, []) if word):
            return SignResult(status, "今日已签到" if status == "already" else "签到成功")
    if body.get("success") is True or body.get("state") == "success":
        return SignResult("success", "签到成功")
    return None


def classify(content, rule, submitted=False):
    if not content:
        return SignResult("failed", "页面内容为空，未确认签到结果")
    json_result = _json_result(content, rule)
    if json_result:
        return json_result
    parser = visible(content)
    text = " ".join(parser.parts)
    if re.search(SECOND_FACTOR_PATTERN, text, re.I):
        return SignResult("failed", "站点要求二级验证，请在远端浏览器完成验证")
    if challenge_pending(content):
        return SignResult("failed", "安全挑战尚未完成，请在远端浏览器完成验证", challenge="cloudflare")
    if login_page(content):
        return SignResult("failed", "登录态无效，站点要求登录")
    # 未签到控件/明确未完成状态优先于历史统计、奖励说明与泛化成功关键词。
    controls = " ".join(parser.controls)
    if any(re.fullmatch(r"立即签到|立即簽到|点击签到|點擊簽到|每日签到|每日簽到|签到|簽到|打卡", item.strip(), re.I) for item in parser.controls) or re.search(r"今日未签|今天未签|尚未签到|尚未簽到|未完成签到", text):
        return SignResult("failed", "页面仍有未签到状态或可提交签到控件")
    if any(str(word) in text for word in rule.get("pending_keywords", []) if word):
        return SignResult("failed", "验证码或签到提交尚未完成", challenge="captcha")
    lines = [line.strip() for line in re.split(r"[\n。！!；;]", text) if line.strip()]
    status_lines = [line for line in lines if not re.search(RULE_CONTEXT_PATTERN, line)]
    # 连续天数本身只是统计；同一句明确的今天终态/成功信息不能随统计一起丢弃。
    safe = "\n".join(line for line in status_lines if not re.search(STREAK_PATTERN, line)
                     or re.search(TODAY_SIGNED_PATTERN, line)
                     or re.search(DEFAULT_SUCCESS_PATTERN, line, re.I))
    streak_safe = "\n".join(status_lines)
    safe_markup = " ".join(parser.markup)
    # 预设的连续签到关键词只在真实提交后，或认证页面有当天状态/已签入口时使用。
    signed_entry = re.search(r"<(a|b)\b[^>]*>\s*(已签到|已簽到)\s*</\1>", safe_markup, re.I)
    already_pattern = str(rule.get("already") or "")
    configured_entry = re.search(already_pattern, safe_markup, re.I) if "<" in already_pattern else None
    confirmed_today = bool(re.search(TODAY_SIGNED_PATTERN, safe)
                           or (signed_entry and signed_entry.group(2) in safe)
                           or (configured_entry and plain_text(configured_entry.group(0))
                               and plain_text(configured_entry.group(0)) in safe))
    allow_streak = submitted or (confirmed_today and authenticated(content))
    for status, field, default in (("success", "success_keywords", DEFAULT_SUCCESS_PATTERN),
                                    ("already", "already_keywords", DEFAULT_ALREADY_PATTERN)):
        words = rule.get(field) or []
        matched = any(str(word).lower() in (streak_safe if re.search(STREAK_PATTERN, str(word)) else safe).lower()
                      for word in words if word and (not re.search(STREAK_PATTERN, str(word)) or allow_streak))
        pattern = str(rule.get("success" if status == "success" else "already") or default)
        if not rule.get("strict_status_keywords"):
            matched = matched or bool(re.search(pattern, safe, re.I))
        elif rule.get("success" if status == "success" else "already"):
            matched = matched or bool(re.search(pattern, safe, re.I))
        if "<" in pattern and re.search(pattern, safe_markup, re.I):
            matched = True
        if matched:
            bonus = extract_bonus(safe, rule)
            label = "签到成功" if status == "success" else "今日已签到"
            return SignResult(status, label + (f"（{bonus}）" if bonus else ""))
    # 消失判定仅可用于已认证页面，并且必须确实提交过；空页/登录页都不算成功。
    absent = rule.get("absent_keywords") or []
    if submitted and absent and authenticated(content) and not any(str(word) in text + controls for word in absent):
        return SignResult("success", "签到入口已消失（已认证页面回读确认）")
    return SignResult("failed", "未识别到可信的签到成功或今日已签到状态")
