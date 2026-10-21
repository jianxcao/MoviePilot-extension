"""M-Team 最后访问 API；移植自 ptsitesignin/sites/mteam.py（GPL-3.0）。"""
from __future__ import annotations

from ..rules import note_object


class MTeam:
    """通过 M-Team 官方 API 刷新最后访问状态。"""

    site_url = "m-team.cc"

    @staticmethod
    def token(site):
        """从站点字段或附加信息读取 API 凭证。"""
        note = note_object(site.get("note"))
        for source in (site, note):
            for key in ("token", "apikey", "api_key"):
                if source.get(key):
                    return str(source[key]).strip()
        for line in str(site.get("note") or "").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {"token", "apikey", "api_key"}:
                return value.strip()
        return ""

    def signin(self, site, ctx):
        """仅向受支持注册域名的 API 提交访问刷新请求。"""
        from urllib.parse import urlsplit
        token = self.token(site)
        if not token:
            return False, "M-Team 需要在站点 token/apikey 或附加信息中配置 API Token"
        host = (urlsplit(ctx.base).hostname or "").lower()
        registered = next((domain for domain in ("m-team.cc", "m-team.io", "m-team.xyz")
                           if host == domain or host.endswith("." + domain)), "")
        if not registered:
            return False, "未识别的 M-Team 注册域名，拒绝发送 Token"
        response = ctx.post("https://api." + registered + "/api/member/updateLastBrowse",
                            api_token=token)
        if response.status_code != 200:
            return False, f"刷新最后访问失败（HTTP {response.status_code}）"
        try:
            body = response.json()
        except ValueError:
            return False, "API 未返回可确认身份的 JSON 响应"
        if isinstance(body, dict) and str(body.get("code", "")).upper() in {"0", "SUCCESS", "200"}:
            return True, "模拟登录成功（API 已确认最后访问刷新）"
        return False, "API 未确认最后访问刷新成功"
