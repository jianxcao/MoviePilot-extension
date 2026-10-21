"""站点内保留 Cookie 的 HTTP 会话；公共海报请求不会携带站点凭证。"""
from __future__ import annotations

import time
import ipaddress
import urllib.parse

import requests

from .cdp import CDPCancelled


def origin(url: str) -> tuple:
    value = urllib.parse.urlsplit(url)
    return value.scheme.lower(), (value.hostname or "").lower(), value.port or (443 if value.scheme == "https" else 80)


def site_url(base: str, path: str = "/") -> str:
    result = urllib.parse.urljoin(base.rstrip("/") + "/", path)
    target = urllib.parse.urlsplit(result)
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or "\\" in result:
        raise ValueError("站点地址必须是无凭据的 HTTP(S) 地址")
    if origin(result) != origin(base):
        raise ValueError("拒绝跨站点地址：登录及签到只能使用同源 URL")
    return result


class SiteHTTP:
    def __init__(self, site, cookie="", timeout=30, proxies=None, stop_event=None):
        self.base = str(site.get("url") or "").rstrip("/")
        site_url(self.base, self.base)
        self.timeout = timeout
        self.proxies = proxies
        self.stop_event = stop_event
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": str(site.get("ua") or "Mozilla/5.0"),
                                     "Accept": "text/html,application/json,*/*;q=0.8"})
        host = urllib.parse.urlsplit(self.base).hostname
        for item in str(cookie or "").split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name:
                self.session.cookies.set(name, value, domain=host, path="/")

    def seed_cookies(self, cookies):
        """以浏览器实际 Cookie 替代无作用域的 Header，保留后续删除匹配。"""
        self.session.cookies.clear()
        host = (urllib.parse.urlsplit(self.base).hostname or "").lower()
        for item in cookies:
            domain = str(item.get("domain") or host).lower()
            bare = domain.lstrip(".")
            if host != bare and not (domain.startswith(".") and host.endswith("." + bare)):
                continue
            expires = item.get("expires")
            rest = {}
            if "httpOnly" in item:
                rest["HttpOnly"] = bool(item["httpOnly"])
            if item.get("sameSite"):
                rest["SameSite"] = item["sameSite"]
            # CookieJar sends single-label host cookies using its .local
            # representation; this internal domain must not reach Chromium.
            jar_domain = host + ".local" if "." not in host and not domain.startswith(".") else domain
            cookie = requests.cookies.create_cookie(
                str(item["name"]), str(item.get("value") or ""), domain=jar_domain,
                path=str(item.get("path") or "/"), secure=bool(item.get("secure")),
                expires=int(expires) if expires is not None and expires > 0 else None, rest=rest)
            cookie.domain_specified = domain.startswith(".")
            self.session.cookies.set_cookie(cookie)

    def cookie_snapshot(self, url):
        """返回同站所有未过期 jar Cookie（含其它 path），不泄漏其它主机。"""
        target = site_url(self.base, url)
        host = (urllib.parse.urlsplit(target).hostname or "").lower()
        now = time.time()
        cookies = []
        for cookie in self.session.cookies:
            if cookie.is_expired(now):
                continue
            domain = cookie.domain.lower()
            bare = domain.lstrip(".")
            if bare == host + ".local" and "." not in host:
                domain = host
            elif bare == host:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    pass
                else:
                    domain = host
            elif not (cookie.domain_specified and domain.startswith(".") and host.endswith("." + bare)):
                continue
            item = {"name": cookie.name, "value": cookie.value, "domain": domain,
                    "path": cookie.path or "/", "secure": bool(cookie.secure),
                    "expires": cookie.expires}
            rest = {str(key).lower(): value for key, value in cookie._rest.items()}
            if "httponly" in rest:
                item["httpOnly"] = rest["httponly"] is None or str(rest["httponly"]).lower() not in {"false", "0"}
            if "samesite" in rest:
                item["sameSite"] = rest["samesite"]
            cookies.append(item)
        return cookies

    def check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise CDPCancelled("操作已取消")

    def wait(self, seconds):
        self.check()
        if self.stop_event is not None:
            if self.stop_event.wait(seconds):
                self.check()
        else:
            time.sleep(seconds)
        self.check()

    def request(self, method, url, public=False, api_token="", **kwargs):
        """请求同源地址；公共图片不带凭证，M-Team API 仅带明确授权的 Token。"""
        self.check()
        resolved = urllib.parse.urljoin(self.base + "/", url)
        target = urllib.parse.urlsplit(resolved)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
            raise ValueError("请求地址必须是无凭据的 HTTP(S) 地址")
        internal = origin(resolved) == origin(self.base)
        registered = next((domain for domain in ("m-team.cc", "m-team.io", "m-team.xyz")
                           if (urllib.parse.urlsplit(self.base).hostname or "").endswith("." + domain)
                           or (urllib.parse.urlsplit(self.base).hostname or "") == domain), "")
        trusted_api = bool(api_token and registered and target.scheme == "https"
                           and target.hostname == "api." + registered and method.upper() == "POST"
                           and target.path == "/api/member/updateLastBrowse")
        if not internal and not public and not trusted_api:
            raise ValueError("拒绝向其它主机发送站点凭证")
        if public and (method.upper() != "GET" or "data" in kwargs or "json" in kwargs):
            raise ValueError("公共海报服务只允许无凭据 GET 请求")
        isolated = not internal or trusted_api
        client = requests.Session() if isolated else self.session
        if isolated:
            client.headers.update({"User-Agent": self.session.headers["User-Agent"]})
            kwargs.pop("headers", None)
            if trusted_api:
                kwargs["headers"] = {"Authorization": api_token, "Content-Type": "application/json"}
        try:
            for _ in range(6):
                self.check()
                response = client.request(method, resolved, timeout=self.timeout,
                                          proxies=self.proxies, allow_redirects=False, **kwargs)
                self.check()
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return response
                location = response.headers.get("Location")
                if not location:
                    return response
                destination = urllib.parse.urljoin(resolved, location)
                if origin(destination) != origin(resolved):
                    raise ValueError("拒绝跨主机重定向（避免 Cookie、账号或 API Token 泄漏）")
                resolved = destination
                if response.status_code == 303 or response.status_code in {301, 302} and method.upper() == "POST":
                    method = "GET"
                    for key in ("data", "json", "params"):
                        kwargs.pop(key, None)
            raise ValueError("重定向次数过多")
        finally:
            if isolated:
                client.close()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def cookie_header(self, url=None):
        request = requests.Request("GET", site_url(self.base, url or self.base)).prepare()
        return requests.cookies.get_cookie_header(self.session.cookies, request) or ""

    def close(self):
        self.session.close()
