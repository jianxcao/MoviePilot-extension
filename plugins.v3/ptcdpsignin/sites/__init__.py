"""站点模块上下文；移植自 ptsitesignin（GPL-3.0，原始归属见 ../rules.py）。"""
from __future__ import annotations

from ..site_http import SiteHTTP, origin
from ..results import classify, login_page
from ..cdp import CDPCancelled


class SignContext(SiteHTTP):
    """为站点模块提供受主机凭证边界约束的请求和识别能力。"""

    def __init__(self, site, cookie="", timeout=30, proxies=None, stop_event=None, rule=None, logger=None):
        """初始化站点 HTTP 会话、规则和可选日志入口。"""
        super().__init__(site, cookie, timeout, proxies, stop_event)
        self.rule = rule or {}
        self.logger = logger

    def page(self, url, **kwargs):
        """读取成功响应的页面文本，失败时返回空字符串。"""
        response = self.get(url, **kwargs)
        return response.text if response.status_code == 200 else ""

    def image(self, url, referer="", public=False):
        """读取验证码或公开海报的图片字节。"""
        response = self.get(url, headers={"Referer": referer}, public=public)
        return response.content if response.status_code == 200 else b""

    def ocr(self, image_url):
        """调用宿主已有 OCR，仅向同源验证码地址传递站点凭证。"""
        # 使用宿主已有 OCR，只允许给同源图像服务传本站凭证。
        if origin(image_url) != origin(self.base):
            return ""
        self.check()
        try:
            from app.helper.ocr import OcrHelper
            result = OcrHelper().get_captcha_text(image_url=image_url,
                                                 cookie=self.cookie_header(image_url),
                                                 ua=self.session.headers["User-Agent"])
            self.check()
            return str(result or "").strip()
        except CDPCancelled:
            raise
        except Exception:
            return ""

    def classify(self, content):
        """按本站规则识别签到结果。"""
        return classify(content, self.rule)

    login_page = staticmethod(login_page)


def site_module_for(url):
    """按站点域名选择专用模块，未知站点不匹配。MTeam 不走此 API 模块。"""
    from urllib.parse import urlsplit
    from .hdsky import Hdsky
    from .htpt import Htpt
    from .tjupt import Tjupt
    host = (urlsplit(url).hostname or "").lower()
    for module in (Hdsky, Htpt, Tjupt):
        domain = module.site_url
        if host == domain or host.endswith("." + domain):
            return module
    return None
