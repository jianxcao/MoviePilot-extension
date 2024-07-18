import random
from datetime import datetime, timedelta

import pytz
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.db.subscribe_oper import SubscribeOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.schemas import NotificationType, MediaType


class SubscribeNotification(_PluginBase):
    # 插件名称
    plugin_name = "提醒订阅"
    # 插件描述
    plugin_desc = "推送当天订阅更新内容。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/sub-alert.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "jianxcao,thsrite"
    # 加载顺序
    plugin_order = 33
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _time = None
    _img_link = ''
    tmdb = None
    media = None
    subscribe_oper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.subscribe_oper = SubscribeOper()
        self.tmdb = TmdbChain()
        self.media = MediaChain()

        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._time = config.get("time")
            self._img_link = config.get('img_link')

            if self._enabled or self._onlyonce:
                # 周期运行
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                if self._time and str(self._time).isdigit():
                    cron = f"0 {int(self._time)} * * *"
                    try:
                        self._scheduler.add_job(func=self.__send_notify,
                                                trigger=CronTrigger.from_crontab(
                                                    cron),
                                                name="订阅提醒")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"订阅提醒服务启动，立即运行一次")
                    self._scheduler.add_job(self.__send_notify, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="订阅提醒")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "time": self._time,
            "img_link": self._img_link
        })

    def __send_notify(self):
        # 查询所有订阅
        subscribes = self.subscribe_oper.list()
        if not subscribes:
            logger.error("当前没有订阅，跳过处理")
            return

        # 当前日期
        current_date = datetime.now().date().strftime("%Y-%m-%d")

        current_tv_subscribe = []
        current_movie_subscribe = []
        imgs = []
        img_url = "https://raw.githubusercontent.com/jianxcao/MoviePilot-extension/main/img/default.png"
        # 遍历订阅，查询tmdb
        for subscribe in subscribes:
            # 电视剧
            if subscribe.type == "电视剧":
                if not subscribe.tmdbid or not subscribe.season:
                    continue

                # 电视剧某季所有集
                episodes_info = self.tmdb.tmdb_episodes(
                    tmdbid=subscribe.tmdbid, season=subscribe.season)
                if not episodes_info:
                    continue

                episodes = []
                # 遍历集，筛选当前日期发布的剧集
                for episode in episodes_info:
                    if episode and episode.air_date and str(episode.air_date) == current_date:
                        episodes.append(episode.episode_number)

                if episodes:
                    if isinstance(subscribe.backdrop, str) and subscribe.backdrop != "":
                        imgs.append(subscribe.backdrop)
                    elif isinstance(subscribe.poster, str) and subscribe.poster != "":
                        imgs.append(subscribe.poster)
                    current_tv_subscribe.append({
                        'name': f"📺 {subscribe.name}",
                        'season': f"第{str(subscribe.season).rjust(2, '0')}季",
                        'episode': f"第{str(episodes[0]).rjust(2, '0')}-{str(episodes[-1]).rjust(2, '0')}集" if len(
                            episodes) > 1 else f"第{str(episodes[0]).rjust(2, '0')}集"
                    })

            # 电影
            else:
                if not subscribe.tmdbid:
                    continue
                mediainfo = self.media.recognize_media(
                    tmdbid=subscribe.tmdbid, mtype=MediaType.MOVIE)
                if not mediainfo:
                    continue
                if str(mediainfo.release_date) == current_date:
                    if isinstance(subscribe.backdrop, str) and subscribe.backdrop != "":
                        imgs.append(subscribe.backdrop)
                    elif isinstance(subscribe.poster, str) and subscribe.poster != "":
                        imgs.append(subscribe.poster)
                    current_movie_subscribe.append({
                        'name': f"🎬 {subscribe.name} ({subscribe.year})"
                    })
        if len(imgs):
            img_url = random.choice(imgs)

        if isinstance(self._img_link, str) and len(self._img_link) > 0:
            links = list(filter(lambda url: url.startswith(
                "http"), self._img_link.split("\n")))
            if len(links) > 0:
                img_url = random.choice(links)
        # 如当前日期匹配到订阅，则发送通知
        text = ""
        for sub in current_tv_subscribe:
            text += sub.get("name") + " "
            text += sub.get("season") + " " + sub.get("episode")
            text += "\n"

        for sub in current_movie_subscribe:
            text += sub.get("name")
            text += "\n"

        if text:
            self.post_message(mtype=NotificationType.Subscribe,
                              title=f"今天剧集更新 共 {len(current_tv_subscribe) + len(current_movie_subscribe)} 部",
                              image=img_url,
                              text=text)

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
                            },
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'time',
                                            'label': '时间',
                                            'placeholder': '默认9点'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'img_link',
                                            'label': '头图',
                                            'placeholder': '头图配置请用,分割，每次随机取一个,地址以http开始'
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
                                            'text': '默认每天9点推送，需开启（订阅）通知类型。'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '图片配置说明'
                                                    '如果不配置图片列表，则会取所有的 backdrop 或者 poster 去做头图，如果取不到则取默认'
                                                    '头图请用,分割，可以设置多个，会随机一个'
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
            "onlyonce": False,
            "time": 9,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
