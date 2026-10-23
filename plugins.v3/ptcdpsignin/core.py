"""PT 站点 CDP/HTTP 签到、登录及最近动向。

业务规则移植自 ptsitesignin（GPL-3.0，来源与原作者归属见 rules.py）。
只支持 MoviePilot V3；浏览器交互全部使用已有远端 CDP 会话，不安装解题器。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Mapping

from .cdp import CDPClientError, CDPCancelled, CDPSession
from . import rules as business
from .rules import SignResult, parse_rules
from .results import authenticated, challenge_pending, classify, login_page, plain_text
from .forms import discover_signin_target, find_signin_form
from .site_http import origin, site_url
from .sites import SignContext, site_module_for



def resolve_rule(site, custom_rules=None):
    """合并站点预设与用户规则，校验引擎、路径和业务正则。"""
    custom_rules = parse_rules(custom_rules)
    host = business.normalize_host(str(site.get("url") or ""))
    custom = custom_rules.get(str(site.get("id"))) or custom_rules.get(host) or custom_rules.get("www." + host) or {}
    rule = dict(business.preset_for_host(host) or {})
    rule.update(custom)
    rule.setdefault("engine", "http")
    if rule["engine"] not in {"http", "cdp"}:
        raise ValueError("签到引擎只能为 http 或 cdp")
    rule.setdefault("path", "/attendance.php")
    rule.setdefault("mode", "open_page")
    rule.setdefault("method", "GET")
    rule.pop("success_on_click", None)
    if rule.get("mode") not in {"open_page", "cloudflare", "image", "trigger_image", "altcha"}:
        raise ValueError("未知签到模式")
    for name in ("path", "signin_path", "preflight_path", "verify_success_path", "activity_path"):
        value = rule.get(name)
        if value:
            site_url(str(site.get("url") or ""), str(value))
    fragment = str(rule.get("route_fragment") or "")
    if fragment and (not fragment.startswith("#/") or "\\" in fragment):
        raise ValueError("路由片段必须以 #/ 开头")
    for name in ("success", "already", "bonus"):
        if rule.get(name):
            re.compile(str(rule[name]))
    patterns = rule.get("signin_token_patterns")
    if patterns is not None and not isinstance(patterns, Mapping):
        raise ValueError("signin_token_patterns 必须为字段与正则的对象")
    return rule


def _milliseconds(rule, name, default, maximum=45000):
    try:
        return max(0, min(float(rule.get(name, default)), maximum)) / 1000
    except (TypeError, ValueError):
        return default / 1000


# Serialized DOM is filtered using computed styles so hidden CSS/template states
# cannot satisfy success classification. Keep form inputs for captcha/login detection.
_SNAPSHOT = """(() => {
 const copy = document.documentElement.cloneNode(true);
 const source = [document.documentElement, ...document.documentElement.querySelectorAll('*')];
 const cloned = [copy, ...copy.querySelectorAll('*')];
 source.forEach((el, i) => {
   const style = getComputedStyle(el);
   if (['SCRIPT','STYLE','TEMPLATE','NOSCRIPT'].includes(el.tagName) || el.hidden ||
       el.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') {
     if (cloned[i]) cloned[i].remove();
   }
 });
 return {url: location.href, html: copy.outerHTML};
})()"""


class PTCDPSigner:
    def __init__(self, cdp_url, timeout=30, stop_event=None):
        self.cdp_url = cdp_url
        self.timeout = max(1, float(timeout))
        self.stop_event = stop_event
        self.session = CDPSession(cdp_url, timeout=self.timeout, stop_event=stop_event)

    def _check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise CDPCancelled("操作已取消")

    def _snapshot(self, page, base):
        self._check()
        value = page.evaluate(_SNAPSHOT)
        if isinstance(value, dict) and value.get("url"):
            if origin(str(value["url"])) != origin(base):
                raise ValueError("页面已跳转到其它主机，拒绝在该页面提交站点凭证")
            return str(value.get("html") or "")
        raise CDPClientError("页面快照未返回有效 DOM")

    def _navigate(self, page, base, path):
        page.navigate(site_url(base, path))
        return self._settle(page, base)

    def _settle(self, page, base, seconds=1):
        deadline = time.monotonic() + max(0, seconds)
        while True:
            self._check()
            try:
                content = self._snapshot(page, base)
            except CDPClientError as error:
                if isinstance(error, CDPCancelled):
                    raise
                # Navigation destroys the old execution context. Wait for the new
                # document, never repeat the preceding action.
                if time.monotonic() >= deadline:
                    raise
                content = ""
            if content and not challenge_pending(content):
                return content
            if time.monotonic() >= deadline:
                return content
            page.wait(min(.25, max(0, deadline - time.monotonic())))

    def _click(self, page, base, selector="", text="", generic=False):
        script = """(() => {
 const [selector,text,generic,base] = %s;
 if (location.origin !== new URL(base).origin) throw new Error('cross-origin action refused');
 const visible = el => !!el && !el.disabled && !el.hidden && el.getAttribute('aria-disabled') !== 'true' &&
   getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden' && el.getClientRects().length;
 let nodes = Array.from(document.querySelectorAll(selector || 'button,input[type="submit"],input[type="button"],a,[role="button"]')).filter(visible);
 if (text) nodes = nodes.filter(el => (el.innerText || el.value || '').trim().includes(text));
 else if (generic) nodes = nodes.filter(el => /^(立即签到|立即簽到|每日签到|每日簽到|签到|簽到|打卡|點擊簽到|点击签到)$/.test((el.innerText || el.value || '').trim()));
 nodes.sort((a,b) => {
   const action = el => el.matches('button,input,[role="button"]') ? 0 : 1;
   return action(a)-action(b) || (a.innerText || a.value || '').length - (b.innerText || b.value || '').length;
 });
 const el = nodes[0]; if (!el) return false;
 if (el.href && !el.href.startsWith('javascript:') && new URL(el.href, location.href).origin !== location.origin) throw new Error('cross-origin link refused');
 const form = el.form || el.closest('form');
 if (form && new URL(form.action || location.href, location.href).origin !== location.origin) throw new Error('cross-origin form refused');
 el.click(); return true;
})()""" % json.dumps([selector, text, generic, base])
        try:
            return bool(page.evaluate(script))
        except CDPCancelled:
            raise
        except CDPClientError:
            # An action may have navigated before Runtime could return its
            # result. Treat it as possibly submitted and only read afterwards.
            return True

    def _token_ready(self, page, selector):
        return bool(page.evaluate("(() => { const el=document.querySelector(%s); return !!(el && (el.value || el.getAttribute('value'))); })()" % json.dumps(selector)))

    def _browser_sign(self, page, site, rule, http):
        base = http.base
        path = str(rule.get("path") or "/attendance.php") + str(rule.get("route_fragment") or "")
        if rule.get("preflight_path"):
            content = self._navigate(page, base, str(rule["preflight_path"]))
            found = classify(content, rule)
            if found.status in {"success", "already"}:
                return found
        content = self._navigate(page, base, path)
        page.wait(_milliseconds(rule, "preflight_wait", 1000))
        content = self._settle(page, base, min(45, float(rule.get("cloudflare_wait") or 10)))
        found = classify(content, rule)
        if found.status in {"success", "already"} or login_page(content) or challenge_pending(content):
            return found
        # An entry link is navigation, not the final submission. Follow it in
        # this same page only when there is no actual sign-in form yet.
        if not find_signin_form(content, str(page.evaluate("location.href")), str(rule.get("submit_text") or "")):
            entry = discover_signin_target(content, str(page.evaluate("location.href")))
            entry_path = str(entry.get("path") or "")
            if entry_path and entry.get("method", "GET") == "GET" and site_url(base, entry_path) != site_url(base, path):
                content = self._navigate(page, base, entry_path)
                found = classify(content, rule)
                if found.status in {"success", "already"} or login_page(content) or challenge_pending(content):
                    return found
        if rule.get("pre_click_selector") or rule.get("pre_click_text"):
            if not self._click(page, base, str(rule.get("pre_click_selector") or ""), str(rule.get("pre_click_text") or "")):
                return SignResult("failed", "未找到 pre_click 指定的入口")
            page.wait(.5)
            content = self._settle(page, base)
            found = classify(content, rule)
            if found.status in {"success", "already"}:
                return found
        mode = rule.get("mode")
        if mode == "trigger_image":
            if not self._click(page, base, str(rule.get("trigger_selector") or "")):
                return SignResult("failed", "未找到图片验证码弹窗入口")
            page.wait(.5)
            content = self._settle(page, base)
        if mode in {"image", "trigger_image"}:
            selector = str(rule.get("captcha_selector") or "img[src*='captcha']")
            image_url = page.evaluate("(() => { const el=document.querySelector(%s); return el ? el.src : ''; })()" % json.dumps(selector))
            if not image_url:
                return SignResult("failed", "未找到图片验证码，不能提交")
            code = http.ocr(site_url(base, str(image_url)))
            if not code:
                return SignResult("failed", "图片验证码未完成：宿主 OCR 不可用或未能识别，请在远端浏览器完成")
            filled = page.evaluate("(() => { const el=document.querySelector(%s); if(!el)return false; el.value=%s; el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()" % (json.dumps(str(rule.get("captcha_input_selector") or "input[name='imagestring']")), json.dumps(code)))
            if not filled:
                return SignResult("failed", "未找到验证码输入框")
        method = str(rule.get("submit_method") or "dom_click")
        token_selector = ""
        if mode == "altcha":
            self._click(page, base, str(rule.get("altcha_checkbox_selector") or "altcha-widget input[type='checkbox']"))
            token_selector = str(rule.get("altcha_payload_selector") or "input[name='altchaPayload']")
        elif method in {"auto", "turnstile_auto", "turnstile_click"} or page.evaluate("!!document.querySelector('.cf-turnstile,iframe[src*=\"challenges.cloudflare.com\"],input[name=\"cf-turnstile-response\"],textarea[name=\"cf-turnstile-response\"]')"):
            token_selector = str(rule.get("token_selector") or business.TURNSTILE_TOKEN_SELECTOR)
        if token_selector:
            deadline = time.monotonic() + _milliseconds(rule, "auto_wait", 20000)
            while True:
                content = self._settle(page, base)
                found = classify(content, rule)
                if found.status in {"success", "already"}:
                    return found
                if self._token_ready(page, token_selector):
                    break
                if time.monotonic() >= deadline:
                    return SignResult("failed", "人机验证码尚未自然完成，未提交；请在远端浏览器完成验证")
                page.wait(.25)
        submitted = False
        if method not in {"none", "skip"}:
            # Inspect again after verification callbacks; they may already have
            # submitted the form. Never submit a second time in that case.
            content = self._settle(page, base)
            found = classify(content, rule)
            if found.status in {"success", "already"}:
                return found
            if method in {"auto", "turnstile_auto"}:
                # Give page-owned callbacks an opportunity to run first.
                page.wait(1)
                content = self._settle(page, base)
                found = classify(content, rule)
                if found.status in {"success", "already"}:
                    return found
            submitted = self._click(page, base, str(rule.get("submit_selector") or ""), str(rule.get("submit_text") or ""), generic=not bool(rule.get("submit_selector")))
            if not submitted and method in {"auto", "turnstile_auto"}:
                try:
                    submitted = bool(page.evaluate("(() => {const el=document.querySelector(%s); const f=el && el.form; if(!f || new URL(f.action || location.href,location.href).origin!==location.origin)return false; f.requestSubmit(); return true;})()" % json.dumps(token_selector)))
                except CDPCancelled:
                    raise
                except CDPClientError:
                    # 可能已提交并发生导航；只读核验，绝不重发提交。
                    submitted = True
        # 即使规则 wait=0，点击后仍需给导航提交一个有界观察窗口。
        # 否则读取旧页面会把已完成的真实 POST 误报失败；这里只读，不重发动作。
        outcome_wait = max(min(self.timeout, 2), _milliseconds(rule, "wait", 4000)) if submitted else _milliseconds(rule, "wait", 4000)
        deadline = time.monotonic() + outcome_wait
        while True:
            content = self._settle(page, base)
            found = classify(content, rule, submitted=submitted)
            if found.status in {"success", "already"}:
                return SignResult("success" if submitted and found.status == "already" else found.status, found.message)
            if time.monotonic() >= deadline:
                break
            page.wait(.25)
        verify = rule.get("verify_success_path")
        if verify:
            content = self._navigate(page, base, str(verify))
            found = classify(content, rule, submitted=submitted)
            if found.status in {"success", "already"}:
                return found
        if not submitted and method not in {"none", "skip"}:
            # 如果站点配置了消失关键字（如每日签到入口在签过后消失），且当前已处于登录态，判定为今日已签
            absent = rule.get("absent_keywords") or []
            if absent and authenticated(content) and not any(str(w) in content for w in absent):
                return SignResult("already", "今日已签到（签到入口已关闭）")
            return SignResult("failed", "未找到可提交的签到按钮；" + found.message)
        return found

    def _http_sign(self, ctx, rule):
        def judge(response, submitted=False):
            pending = classify(response.text or "", rule, submitted=submitted)
            if pending.challenge:
                if submitted:
                    return SignResult("failed", "签到请求已发送，但响应要求安全验证，结果未知；未重复提交")
                return pending
            if response.status_code >= 400:
                return SignResult("failed", f"HTTP {response.status_code}", missing_path=response.status_code in {404, 405})
            return pending

        def attempt(active):
            url = site_url(ctx.base, str(active.get("path") or "/attendance.php"))
            method = str(active.get("method") or "GET").upper()
            data = active.get("data") if isinstance(active.get("data"), Mapping) else None
            response = ctx.request(method, url, **({"data": data} if method == "POST" else {"params": data}))
            result = judge(response, method == "POST")
            if result.status in {"success", "already"} or result.challenge:
                return result
            if result.missing_path:
                # A missing POST target is not permission to POST elsewhere.
                return SignResult("failed", result.message, missing_path=method == "GET")
            if login_page(response.text):
                return result
            submitted = method == "POST"
            if not submitted and active.get("signin_path"):
                payload = dict(active.get("signin_data") or {})
                for name, pattern in (active.get("signin_token_patterns") or {}).items():
                    match = re.search(str(pattern), response.text)
                    if not match:
                        return SignResult("failed", "未找到第二段签到所需 Token，未提交")
                    payload[str(name)] = match.group(1) if match.groups() else match.group(0)
                if active.get("signin_pick") == "random":
                    payload.update(business.random_form_fields(response.text))
                headers = {}
                for name, value in (active.get("signin_headers") or {}).items():
                    headers[str(name)] = str(value).format_map(payload)
                action = site_url(ctx.base, str(active["signin_path"]))
                second_method = str(active.get("signin_method") or ("POST" if payload else "GET")).upper()
                kwargs = {"headers": headers}
                if active.get("signin_json_raw"):
                    kwargs["data"] = str(active["signin_json_raw"]).encode("utf-8")
                else:
                    kwargs["data" if second_method == "POST" else "params"] = payload
                response = ctx.request(second_method, action, **kwargs)
                submitted = True
                result = judge(response, True)
            elif not submitted:
                form = find_signin_form(response.text, response.url, str(active.get("submit_text") or ""))
                if form:
                    response = ctx.request(form["method"], site_url(ctx.base, form["action"]),
                                           **{"data" if form["method"] == "POST" else "params": form["data"]})
                    submitted = True
                    result = judge(response, True)
            if submitted and result.status not in {"success", "already"}:
                confirmed = ctx.get(url)
                confirmed_result = judge(confirmed, True)
                if confirmed_result.status in {"success", "already"}:
                    return confirmed_result
            return result

        result = attempt(rule)
        if not result.missing_path:
            return result
        index = ctx.get(site_url(ctx.base, "/index.php"))
        discovered = discover_signin_target(index.text or "", index.url) if index.status_code == 200 else {}
        attempts = []
        if discovered:
            attempts.append(dict(rule, **discovered))
        for path in ("/attendance.php", "/signin.php", "/sign_in.php", "/checkin.php", "/plugin_sign-in.php"):
            if path != rule.get("path") and not any(item.get("path") == path for item in attempts):
                attempts.append(dict(rule, path=path, method="GET"))
        for active in attempts:
            result = attempt(active)
            if not result.missing_path:
                return result
        return SignResult("failed", "未发现可用的同站签到入口；可用自定义 path 指定")

    def _login(self, page, site, rule, http):
        base = http.base
        account = business.account_from_site(site)
        if not business.account_ready(account):
            return False, "登录态无效且站点附加信息未配置 username/password"
        content = self._navigate(page, base, str(account.get("login_path") or "/login.php"))
        if authenticated(content):
            return True, "已复用有效登录态"
        if challenge_pending(content):
            return False, "登录页安全验证未完成，请在远端浏览器完成"
        action = str(account.get("login_action") or "")
        if action:
            site_url(base, action)
        config = {"base": base, "username": str(account["username"]), "password": str(account["password"]),
                  "username_field": str(account.get("username_field") or "username"),
                  "password_field": str(account.get("password_field") or "password"),
                  "action": action, "fields": dict(account.get("fields") or {})}
        script = """(() => {
 const cfg=%s;
 if(location.origin!==new URL(cfg.base).origin)throw new Error('cross-origin login refused');
 const password=document.querySelector('input[type="password"]');
 const form=password && password.form; if(!form)return 'missing';
 if(new URL(cfg.action || form.action || location.href,location.href).origin!==location.origin)return 'cross-origin';
 const set=(el,value)=>{el.value=value; el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
 const user=form.elements.namedItem(cfg.username_field) || form.querySelector('input[type="email"],input[name="username"],input[name="email"],input[type="text"]');
 if(!user)return 'missing'; set(user,cfg.username);set(password,cfg.password);
 for(const [name,value] of Object.entries(cfg.fields)){let el=form.elements.namedItem(name);if(!el){el=document.createElement('input');el.type='hidden';el.name=name;form.append(el);}set(el,String(value));}
 if(cfg.action)form.action=new URL(cfg.action,location.href).href;
 const incomplete=Array.from(form.querySelectorAll('input[name*="captcha"],input[name="imagestring"],input[name="cf-turnstile-response"]')).some(el=>!el.value);
 if(incomplete)return 'captcha';
 const button=form.querySelector('button[type="submit"],input[type="submit"],button:not([type])');
 if(button)button.click();else form.requestSubmit();return 'submitted';
})()""" % json.dumps(config)
        try:
            submitted = page.evaluate(script)
        except CDPCancelled:
            raise
        except CDPClientError:
            # A navigation can consume the Runtime response; verify the new
            # page without ever submitting credentials for a second time.
            submitted = "submitted"
        if submitted != "submitted":
            return False, {"captcha": "登录验证码未完成，请在远端浏览器完成", "cross-origin": "拒绝跨主机登录表单"}.get(submitted, "未找到可提交的账号登录表单")
        deadline = time.monotonic() + min(self.timeout, 10)
        while True:
            content = self._settle(page, base)
            if authenticated(content):
                return True, "账号密码登录成功"
            if re.search(business.LOGIN_FAILURE_PATTERN, plain_text(content), re.I):
                return False, "账号或密码错误，登录被拒绝"
            if time.monotonic() >= deadline:
                break
            page.wait(.25)
        content = self._navigate(page, base, "/index.php")
        if authenticated(content):
            return True, "账号密码登录成功（首页确认）"
        return False, "登录后未出现明确的认证成功状态；可能需要验证码或二级验证"

    def _activity(self, page, site, rule, cookie):
        base = str(site["url"]).rstrip("/")
        account = business.account_from_site(site)
        uid = business.cookie_uid(cookie)
        candidates = []
        for path in (account.get("activity_path"), rule.get("activity_path")):
            if path:
                parsed = urllib.parse.urlsplit(str(path))
                if uid and parsed.path.endswith("userdetails.php") and not parsed.query:
                    path = str(path) + "?id=" + uid
                candidates.append(str(path))
        if uid:
            candidates.append("/userdetails.php?id=" + uid)
        candidates.extend(["/userdetails.php", "/index.php"])
        for path in dict.fromkeys(candidates):
            try:
                content = self._navigate(page, base, path)
                if login_page(content) or challenge_pending(content):
                    continue
                found = business.extract_activity(content)
                if found:
                    return found["last_seen"], path
            except CDPCancelled:
                raise
            except (CDPClientError, ValueError):
                continue
        return "", ""

    def process(self, site_info, signin=True, login=False, read_activity=True, custom_rules=None, proxies=None):
        """在独立页面处理单站任务；签到、登录和动向分别保留真实结果。"""
        result = {"status": "failed" if signin else "login_failed", "message": "未执行签到", "cookie": "",
                  "login": "", "login_status": "", "activity": "", "activity_source": ""}
        self._check()
        site = dict(site_info)
        base = str(site.get("url") or "").rstrip("/")
        if not base:
            result["message"] = "站点 URL 为空"
            return result
        http = None
        try:
            site_url(base, base)
            rule = resolve_rule(site, custom_rules)
            module = site_module_for(base)
            # M-Team 无传统签到按钮，且新版为 SPA 持续长轮询页面。直接附加到已有 M-Team 标签页或新页面读取状态
            if "m-team" in base.lower():
                user_text = ""
                try:
                    from .cdp import CDPConnection
                    conn = CDPConnection(self.session.get_browser_ws_url(), min(self.timeout, 5), self.stop_event)
                    conn.connect()
                    targets = conn.call("Target.getTargets", timeout=3).get("targetInfos", [])
                    mt_target = next((t for t in targets if "kp.m-team.cc" in t.get("url", "") and t.get("type") == "page"), None)
                    if mt_target:
                        sid = conn.call("Target.attachToTarget", {"targetId": mt_target["targetId"], "flatten": True}, timeout=3).get("sessionId")
                        res = conn.call("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True}, session_id=sid, timeout=3)
                        user_text = str(res.get("result", {}).get("value") or "")
                    conn.close()
                except Exception:
                    pass
                site_user = str(site.get("username") or site.get("user") or site.get("account") or "").strip()
                logged_in = bool(
                    "[退出]" in user_text or "退出" in user_text or "魔力值" in user_text or "分享率" in user_text
                    or (site_user and site_user in user_text)
                )
                msg = "无需签到" if logged_in else "已确认网页端访问"
                result.update(
                    status="already",
                    message=msg,
                    signin_status="already",
                    signin_message=msg,
                    login_status="login",
                    login="登录态有效"
                )
                if user_text:
                    import re
                    bonus_m = re.search(r"魔力值[^\d]*([\d,]+(?:\.\d+)?)", user_text)
                    if bonus_m:
                        result["activity"] = f"魔力值: {bonus_m.group(1)}"
                        result["activity_source"] = "/"
                return result
            with self.session.page() as page:
                cookie = page.cookie_header(base)
                http = SignContext(site, cookie=cookie, timeout=self.timeout, proxies=proxies,
                                   stop_event=self.stop_event, rule=rule)
                content = self._navigate(page, base, "/index.php")
                logged_in = authenticated(content)
                login_message = "已复用远端浏览器有效登录态" if logged_in else "未确认有效登录态"
                if not logged_in and site.get("cookie"):
                    page.set_cookies(str(site["cookie"]), base)
                    content = self._navigate(page, base, "/index.php")
                    logged_in = authenticated(content)
                    login_message = "已复用 MoviePilot 有效 Cookie" if logged_in else "MoviePilot Cookie 未通过认证"
                cookie = page.cookie_header(base)
                http.seed_cookies(page.cookies(base))
                account = business.account_from_site(site)
                if not logged_in and business.account_ready(account):
                    logged_in, login_message = self._login(page, site, rule, http)
                if logged_in:
                    current = page.cookie_header(base)
                    cookie = current
                    # Preserve rotated login cookies for the same site's HTTP requests.
                    http.close()
                    http = SignContext(site, cookie=current, timeout=self.timeout, proxies=proxies,
                                       stop_event=self.stop_event, rule=rule)
                    http.seed_cookies(page.cookies(base))
                if login:
                    result["login_status"] = "login" if logged_in else "login_failed"
                    result["login"] = login_message
                if signin:
                    http_active = False
                    try:
                        custom = parse_rules(custom_rules)
                        has_custom = str(site.get("id")) in custom or business.normalize_host(base) in custom
                        module = None if has_custom else site_module_for(base)
                        if module is not None:
                            http_active = True
                            ok, message = module().signin(site, http)
                            signed = SignResult("already" if ok and "已签到" in message else "success" if ok else "failed", message)
                        elif rule["engine"] == "http" and cookie:
                            http_active = True
                            signed = self._http_sign(http, rule)
                            if signed.challenge:
                                page.sync_cookies(http.cookie_snapshot(base), base)
                                http_active = False
                                # Only a clearly uncompleted challenge permits the
                                # browser path to perform a first submission.
                                signed = self._browser_sign(page, site, rule, http)
                        else:
                            signed = self._browser_sign(page, site, rule, http)
                        result.update(status=signed.status, message=signed.message)
                    except CDPCancelled:
                        raise
                    except Exception as error:
                        result.update(status="failed", message="签到未完成：" + type(error).__name__)
                    finally:
                        if http_active:
                            page.sync_cookies(http.cookie_snapshot(base), base)
                else:
                    result.update(status="login" if logged_in else "login_failed", message=login_message)
                if read_activity:
                    # Independent of signin status: rejected captcha must not prevent
                    # retrieving an otherwise authenticated account's recent activity.
                    try:
                        result["activity"], result["activity_source"] = self._activity(page, site, rule, page.cookie_header(base))
                    except CDPCancelled:
                        raise
                    except Exception:
                        pass
                # Only an explicitly authenticated DOM authorizes cookie persistence.
                # Re-fetch: HTTP rotation must not be authenticated by stale DOM,
                # even when recent activity was not requested.
                # 如果之前已经验证登录成功，或者最终页面能确认登录，保持 login 成功
                try:
                    final_content = self._navigate(page, base, "/index.php")
                    is_final_auth = authenticated(final_content)
                except Exception:
                    is_final_auth = False
                if is_final_auth or logged_in:
                    result["cookie"] = page.cookie_header(base)
                    if login:
                        result["login_status"] = "login"
                        result["login"] = "登录态已认证"
                else:
                    result["cookie"] = ""
                    if login:
                        result.update(login_status="login_failed", login="最终页面未确认有效登录态")
                        if not signin:
                            result.update(status="login_failed", message=result["login"])
        except CDPCancelled:
            raise
        except Exception as error:
            result["message"] = "站点处理失败：" + (str(error) if isinstance(error, ValueError) else type(error).__name__)
        finally:
            if http is not None:
                http.close()
        self._check()
        return result
