from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger

class Media302(_PluginBase):
    # 插件名称
    plugin_name = "Media302"
    # 插件描述
    plugin_desc = "Media302"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/media302.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jianxcao"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 2
    # 需要包含的目录
    _include_dirs = ""
    _media302_host = ""
    _media302_token = ""
    _enabled = False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        if config:
            self._include_dirs = config.get('include_dirs')
            self._media302_host = config.get('media302_host')
            self._media302_token = config.get('media302_token')
            self._enabled = config.get('enabled')
            self.__update_config()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "include_dirs": self._include_dirs,
            "media302_host": self._media302_host,
            "media302_token": self._media302_token
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
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
                                    'cols': 6,
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
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'media302_host',
                                            'label': 'Host',
                                            'placeholder': 'Media302 host'
                                        }
                                    }
                                ]
                            }, 
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'media302_token',
                                            'label': 'token',
                                            'placeholder': 'Media302 Token'
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
                                            'model': 'include_dirs',
                                            'label': '头图',
                                            'placeholder': '默认通知头图配置'
                                        }
                                    }
                                ]
                            },
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
        pass
