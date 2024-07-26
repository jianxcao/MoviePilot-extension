from app.chain import ChainBase
from functools import wraps
from app.schemas import Notification
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.chain.tmdb import TmdbChain
from app.utils.web import WebUtils
from app.core.config import settings


def add_default_attr(method, img_link: str | None):
    @wraps(method)
    def wrapper(self, message: Notification):
        if not hasattr(message, "image") or getattr(message, "image") is None:
            img_url = img_link
            if not isinstance(img_url, str) or not len(img_url) > 0:
                if settings.WALLPAPER == "tmdb":
                    url = TmdbChain().get_random_wallpager()
                else:
                    url = WebUtils.get_bing_wallpaper()
                logger.info(f"tmdb_url:{url}, {url}")
                img_url = url if isinstance(url, str) and url else \
                    "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/mp.jpg"
            setattr(message, "image", img_url)
            logger.info(f"image_url: {img_url}")
        return method(self, message)

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
    plugin_version = "1.1"
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

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
