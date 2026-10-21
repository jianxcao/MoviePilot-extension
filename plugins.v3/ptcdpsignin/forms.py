"""表单与入口发现，移植自 ptsitesignin/core.py（归属参见 rules.py）。"""
from __future__ import annotations
import re
import urllib.parse
from typing import Any, Dict, Optional
from .rules import _tag_attrs
from .results import plain_text
SIGNIN_LINK_WORDS = ("签到", "簽到", "打卡")

def find_signin_form(html: str, base_url: str,
                     submit_text: str = "") -> Optional[Dict[str, Any]]:
    """从签到页里找签到表单，返回 ``{"action", "method", "data"}``；找不到返回 None。

    NexusPHP 的签到是「点表单按钮」触发的，光 GET 页面只会渲染按钮、不产生签到记录。
    这里优先找提交按钮文字含「签到 / 打卡 / 领取」（或规则的 submit_text）的表单，
    实在没有就退到「带提交按钮且 action 像签到」的那个。
    """
    wanted = str(submit_text or "").strip()
    best: Optional[Dict[str, Any]] = None
    fallback: Optional[Dict[str, Any]] = None
    for raw_attrs, inner in _FORM_RE.findall(html or ""):
        control = _submit_control(inner)
        if not control:
            continue
        attrs = _tag_attrs("<form " + raw_attrs + ">")
        raw_action = attrs.get("action") or ""
        action = same_site_path(base_url, raw_action)
        if not action:
            continue
        method = (attrs.get("method") or "POST").strip().upper()
        label = re.sub(r"\s+", "", plain_text(inner) + str(control.get("value") or ""))
        fields: Dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", inner, re.IGNORECASE):
            item = _tag_attrs(tag)
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            kind = (item.get("type") or "text").lower()
            if kind in {"submit", "button", "image", "reset"}:
                continue
            fields[name] = str(item.get("value") or "")
        if str(control.get("name") or "").strip():
            fields[str(control["name"])] = str(control.get("value") or "")
        form = {"action": action, "method": "POST" if method != "GET" else "GET",
                "data": fields}
        if wanted and wanted in label:
            return form
        if any(word in label for word in SIGNIN_LINK_WORDS):
            best = best or form
        elif "attendance" in action or "signin" in action or "checkin" in action:
            fallback = fallback or form
    return best or fallback

_SIGNIN_LINK_RE = re.compile(
    r"<a\b[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)

_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)

_SUBMIT_RE = re.compile(r"<(?:input|button)\b[^>]*>", re.IGNORECASE)


def same_site_path(base_url: str, href: str) -> str:
    """按实际文档 URL 解析同 origin 路径；空 action 保留当前查询参数。"""
    value = str(href or "").strip()
    if value.startswith(("#", "javascript:", "mailto:", "data:")):
        return ""
    try:
        target = urllib.parse.urljoin(base_url, value)
        parsed, base = urllib.parse.urlparse(target), urllib.parse.urlparse(base_url)
        def origin(url):
            port = url.port
            return (url.scheme.lower(), url.hostname,
                    port if port is not None else {"http": 80, "https": 443}.get(url.scheme.lower()))
        if (base.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or origin(parsed) != origin(base)):
            return ""
    except ValueError:
        return ""
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path if path.startswith("/") else ""

def discover_signin_path(html: str, base_url: str) -> str:
    """从首页 HTML 里找文字含「签到 / 打卡」的链接，返回同站路径（找不到返回空串）。

    链接文字里的空白先全部去掉，兼容「每 日 签 到」这类带空格的排版。
    """
    for href, label in _SIGNIN_LINK_RE.findall(html or ""):
        text = re.sub(r"\s+", "", plain_text(label))
        if not any(word in text for word in SIGNIN_LINK_WORDS):
            continue
        path = same_site_path(base_url, href)
        if path:
            return path
    return ""

def _submit_control(inner: str) -> Dict[str, str]:
    """表单里的提交控件属性（``<input type=submit>`` / ``<button>``），没有就返回空字典。"""
    for tag in _SUBMIT_RE.findall(inner or ""):
        attributes = _tag_attrs(tag)
        kind = attributes.get("type", "").lower()
        if ((tag.lower().startswith("<button") and kind in {"", "submit"})
                or (tag.lower().startswith("<input") and kind == "submit")):
            attributes.setdefault("type", "submit")
            return attributes
    return {}

def discover_signin_target(html: str, base_url: str) -> Dict[str, Any]:
    """找签到入口，返回 ``{"path", "method", "data"}``（找不到返回空字典）。

    先复用签到表单的 action / method 和完整提交字段（包括 hidden CSRF 与提交按钮）；
    表单里找不到再退回「找链接」。
    """
    body = html or ""
    form = find_signin_form(body, base_url)
    if form:
        return {"path": form["action"], "method": form["method"], "data": form["data"]}
    found = discover_signin_path(body, base_url)
    return {"path": found, "method": "GET"} if found else {}
