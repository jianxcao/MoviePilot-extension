import re
import time
import os
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas import TransferInfo
from app.schemas.types import  EventType
from app.core.event import  eventmanager, Event
from pathlib import Path
import requests

class Pan302(_PluginBase):
    # 插件名称
    plugin_name = "pan302"
    # 插件描述
    plugin_desc = "pan302"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/pan302.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "jianxcao"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 2
    # 需要包含的目录
    _include_dirs = ""
    _pan302_host = ""
    _pan302_token = ""
    _transfer_folder = ""
    _enabled = False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        if config:
            self._include_dirs = config.get('include_dirs')
            self._pan302_host = config.get('pan302_host')
            self._pan302_token = config.get('pan302_token')
            self._transfer_folder = config.get('transfer_folder')
            self._enabled = config.get('enabled')
            if self._enabled:
                logger.info(f"启用pan302: {self._pan302_host} {self._pan302_token}")
            self.__update_config()

    # 入库调用
    @eventmanager.register(EventType.TransferComplete)
    def evt_file_insert(self, event: Event):
        if not event.event_data:
            return
        if not self._enabled:
            return
        transferinfo: TransferInfo = event.event_data.get("transferinfo")
        if not transferinfo.success:
            return
        target_item = transferinfo.target_item
        if not target_item:
            return
        if target_item.storage != "local" or target_item.type != "file":
            return
        if not target_item.path:
            return
        logger.info(f"pan302触发事件前: {target_item}")
        if self._include_dirs:
            include_dirs = self._include_dirs.split("\n")
            for include_dir in include_dirs:
                if target_item.path.startswith(include_dir):
                    break
            else:
                logger.info(f"pan302触发事件: {target_item.path} 不在包含目录中")
                return
        
        time.sleep(1)
        info = os.stat(target_item.path)
        logger.info(f"pan302触发事件: {target_item.path} {info}")
        res = requests.get(
            f"{self._pan302_host}/api/sync/upload-by-path",
            headers={"Authorization": f"Bearer {self._pan302_token}"},
            params={"path": target_item.path},
            timeout=30,
        )

        try:
            body = res.json()
        except ValueError:
            body = res.text

        logger.info(f"pan302触发事件结果 : {res.status_code} {body}")
        res.raise_for_status()

    def parse_share_url(self, share_url: str):
        pattern = re.compile(r'(?:115|anxia|115cdn)\.com/s/([^?]+)(?:\?password=([^&#]+))?')
        matches = pattern.search(share_url)
        logger.info(f"pan302触发事件: {matches}")
        if not matches:
            raise ValueError("无效的分享链接")
        return matches.groups()

    @eventmanager.register(EventType.UserMessage)
    def msg(self, event: Event):
        if not event.event_data:
            return
        if not self._enabled:
            return
        # logger.info(f"pan302触发事件: {event.event_data}")
        message = event.event_data.get("text")
        if not message:
            return
        message = message.strip()
        message = message[1:]
        logger.info(f"pan302触发事件: {message} {message.startswith('http')}")
        if not self._transfer_folder:
            logger.error(f"pan302触发事件: 未设置转移文件夹")
            return
        if message.startswith('http'):
            try:
                res = self.parse_share_url(message)
                logger.info(f"pan302触发事件: res")
                if res:
                    res = requests.get(f"{self._pan302_host}/strm/api/task/save-share", headers={"Authorization": f"{self._pan302_token}"}, params={"url": message, "folder": self._transfer_folder})
                    logger.info(f"pan302触发事件结果 : {res.status_code} {res.json()}")
            except Exception as e:
                logger.error(f"pan302触发事件: {e}")
           

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "include_dirs": self._include_dirs,
            "pan302_host": self._pan302_host,
            "pan302_token": self._pan302_token,
            "transfer_folder": self._transfer_folder
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
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {'model': 'transfer_folder', 'label': '转移文件夹'}
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
                                            'model': 'pan302_host',
                                            'label': 'Host',
                                            'placeholder': 'pan302 host'
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
                                            'model': 'pan302_token',
                                            'label': 'token',
                                            'placeholder': 'pan302 Token'
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
                                            'label': '包含目录',
                                            'placeholder': '包含目录'
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
    
    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def stop_service(self):
        pass
