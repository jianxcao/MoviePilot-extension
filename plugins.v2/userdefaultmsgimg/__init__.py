from app.chain import ChainBase
from functools import wraps
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.chain.tmdb import TmdbChain
import importlib
import inspect
import requests
from app.core.config import settings
from app import schemas
from app.schemas import Notification, NotificationType

# Dynamic imports with fallback
WebUtils = None
WallpaperHelper = None

try:
    WebUtils = getattr(importlib.import_module('app.utils.web'), 'WebUtils')
except (ImportError, AttributeError):
    pass

try:
    WallpaperHelper = getattr(importlib.import_module('app.application.image'), 'WallpaperHelper')
except (ImportError, AttributeError):
    try:
        WallpaperHelper = getattr(importlib.import_module('app.helper.wallpaper'), 'WallpaperHelper')
    except (ImportError, AttributeError):
        pass



def add_default_attr(method, img_link: str | None):
    # 获取原始方法的签名和类型注解
    sig = inspect.signature(method)
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        # 将位置参数转换为关键字参数
        bound_args = sig.bind(self, *args, **kwargs)
        bound_args.apply_defaults()
        # 获取 message 参数
        message = bound_args.arguments.get('message')
        
        # 处理图片逻辑
        if message and (not hasattr(message, "image") or getattr(message, "image") is None):
            img_url = img_link
            if not isinstance(img_url, str) or not img_url.strip():
                url = None
                if settings.WALLPAPER == "tmdb":
                    try:
                        url = TmdbChain().get_random_wallpager()
                    except Exception as err:
                        logger.warning(f"获取TMDB壁纸失败: {err}")
                else:
                    # 1. 尝试从已装配的系统 WallpaperHelper / WebUtils 获取
                    try:
                        if WallpaperHelper and hasattr(WallpaperHelper, 'get_bing_wallpaper'):
                            url = WallpaperHelper().get_bing_wallpaper()
                        elif WebUtils and hasattr(WebUtils, 'get_bing_wallpaper'):
                            url = WebUtils.get_bing_wallpaper()
                    except Exception:
                        url = None
                    
                    # 2. 如果系统辅助模块由于未初始化或改版返回 None，直接请求 Bing 壁纸接口兜底
                    if not url:
                        try:
                            bing_resp = requests.get(
                                "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1",
                                timeout=3,
                                headers={"User-Agent": "Mozilla/5.0"}
                            )
                            if bing_resp.status_code == 200:
                                images = bing_resp.json().get("images") or []
                                if images and "url" in images[0]:
                                    url = f"https://cn.bing.com{images[0]['url']}"
                        except Exception as e:
                            logger.debug(f"Direct bing fetch error: {e}")

                logger.info(f"Wallpaper URL: {url}")
                img_url = url if isinstance(url, str) and url.strip() else \
                    "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/mp.jpg"
            setattr(message, "image", img_url)
            logger.info(f"image_url: {img_url}")
        # 调用原始方法
        return method(self, *args, **kwargs)
    
    return wrapper


old_post_message = ChainBase.post_message


class UserDefaultMsgImg(_PluginBase):
    # 插件名称
    plugin_name = "通知默认图片设置"
    # 插件描述
    plugin_desc = "可以将通知设置一个默认的图片"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/img.png"
    # 插件版本
    plugin_version = "1.5"
    # 插件作者
    plugin_author = "jianxcao"
    # 加载顺序
    plugin_order = 33
    # 可使用的用户级别
    auth_level = 2
    # 私有属性
    _img_link = ''
    _enabled = False
    __post_message_width_img = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        if config:
            self._img_link = config.get('img_link')
            self._enabled = config.get('enabled')
            self.__update_config()
            self.__post_message_width_img = add_default_attr(old_post_message, self._img_link)
            self.__post_message_re_define()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "img_link": self._img_link
        })

    def __post_message_re_define(self):
        if self._enabled:
            ChainBase.post_message = self.__post_message_width_img

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def notify(self, apikey: str, title: str = None,
               text: str = None, image: str = None, link: str = None, userid: str = None):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        if not link:
            link = settings.MP_DOMAIN("/")
        self.chain.post_message(message=Notification(
            mtype=NotificationType.Other, title=title, text=text,
            image=image, link=link, userid=userid
        ))

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/send_msg",  # API路径，必须以/开始
            "endpoint": self.notify,  # API响应方法
            "methods": ["GET"],  # 请求方式：GET/POST/PUT/DELETE
            "summary": "通知",  # API名称
            "description": "通知",  # API描述
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'img_link',
                                            'label': '头图',
                                            'placeholder': '默认通知头图配置'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '图片配置说明'
                                                    '请配置一张图片即可'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
        }

    def get_page(self) -> List[dict]:
        pass
    
    def stop_service(self):
        ChainBase.post_message = old_post_message
