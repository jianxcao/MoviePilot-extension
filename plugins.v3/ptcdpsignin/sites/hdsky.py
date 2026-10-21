# 移植自 ptsitesignin/sites/hdsky.py；GPL-3.0，原始来源/归属参见 ../rules.py。
"""HDSky（hdsky.me）：AJAX 取 imagehash → OCR 验证码 → POST showup.php。"""
from __future__ import annotations

import json
from typing import Any, Mapping, Tuple

BASE = "https://hdsky.me"


class Hdsky:
    """通过站点图片验证码和 AJAX 接口执行 HDSky 签到。"""

    site_url = "hdsky.me"

    def signin(self, site: Mapping[str, Any], ctx: Any) -> Tuple[bool, str]:
        """获取验证码、调用宿主 OCR 并识别签到接口响应。"""
        base = str(site.get("url") or BASE).rstrip("/")
        page = ctx.page(f"{base}/")
        if not page:
            return False, "签到失败：打不开首页"
        if ctx.login_page(page):
            return False, "签到失败：Cookie 已失效"
        if ctx.classify(page).status == "already":
            return True, "今日已签到"

        img_hash = ""
        for _ in range(4):
            response = ctx.post(f"{base}/image_code_ajax.php", data={"action": "new"},
                                headers={"Referer": f"{base}/index.php"})
            if response is not None and response.status_code == 200:
                try:
                    payload = json.loads(response.text or "{}")
                except ValueError:
                    payload = {}
                if payload.get("success"):
                    img_hash = str(payload.get("code") or "")
                    break
            ctx.wait(1)
        if not img_hash:
            return False, "签到失败：取不到验证码"

        image_url = f"{base}/image.php?action=regimage&imagehash={img_hash}"
        code = ""
        for _ in range(4):
            code = ctx.ocr(image_url)
            if code and len(code) == 6:
                break
            ctx.wait(1)
        if not code:
            return False, "签到失败：OCR 没识别出验证码"

        response = ctx.post(f"{base}/showup.php",
                            data={"action": "showup", "imagehash": img_hash,
                                  "imagestring": code},
                            headers={"Referer": f"{base}/index.php"})
        if response is None or response.status_code != 200:
            return False, "签到失败：签到接口请求失败"
        try:
            payload = json.loads(response.text or "{}")
        except ValueError:
            return False, "签到失败：签到接口返回异常"
        if payload.get("success"):
            return True, "签到成功"
        message = str(payload.get("message") or "")
        if message == "date_unmatch":
            return True, "今日已签到"
        if message == "invalid_imagehash":
            return False, "签到失败：验证码错误"
        return False, f"签到失败：{message or '未知原因'}"
