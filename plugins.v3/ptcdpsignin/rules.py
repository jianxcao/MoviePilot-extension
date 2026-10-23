"""PT 站点规则与账号/最近动向解析。

移植自 qingyukb/MoviePilot-Plugins 的 plugins.v3/ptsitesignin/core.py（GPL-3.0），
参考提交 c9a298fd898ea4803b7455a633372bc1d2f631af。
站点预设还参考 tmzg0000/moviepilot-plugins captchasignin（原仓库 AGPL/MIT）及
thsrite/MoviePilot-Plugins 站点自动签到；保留原作者归属，授权以各原仓库为准。
此模块只保留与传输无关的业务规则，不包含远程解题器或本机浏览器驱动。
"""
from __future__ import annotations
import base64
import json
import random
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

def plain_text(content: str) -> str:
    # Lazy import avoids a cycle: result classification consumes the constants
    # below, while activity and reward extraction consume filtered visible text.
    from .results import plain_text as visible_text
    return visible_text(content)

DEFAULT_HTTP_PATH = '/attendance.php'

DEFAULT_SUCCESS_PATTERN = '(state[\'\\"]?\\s*[:=]\\s*[\'\\"]?success|签到成功|簽到成功|签到 成功|成功签到|签到完成|簽到完成|领取成功|領取成功|签到已成功)'

DEFAULT_ALREADY_PATTERN = '(今日已签|今天已签|已经签到|已签到过|已签到|已簽到|签到已得|已领取过|不要重复签到|无需重复签到|already|signed in today)'

CLOUDFLARE_PATTERN = '(just a moment|cf-chl-widget|cf_chl_opt|challenge-platform)'

WAF_PATTERN = '(正在进行安全检测|安全检测能力由|雷池\\s*WAF|SafeLine)'

SECOND_FACTOR_PATTERN = '(异地登录安全验证|異地登錄安全驗證|二级验证|二級驗證|两步验证|兩步驗證|忘记二级验证|忘記二級驗證|2FA)'

CF_INTERSTITIAL_PATTERN = '(just a moment|_cf_chl_opt|chl_page)'

TURNSTILE_PATTERN = '(cf-turnstile|turnstile/v0|turnstile\\.min\\.js|turnstile\\.render)'

TURNSTILE_TOKEN_SELECTOR = 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'

CAPTCHA_PATTERN = '(captcha\\.php|<img[^>]+captcha|id\\s*=\\s*[\\"\']?(?:captcha|imagestring|imagehash|verifycode|imagecode)[\\"\']?|name\\s*=\\s*[\\"\']?(?:imagestring|imagehash|verifycode|imagecode|answer|captcha)[\\"\']?|验证码|驗證碼)'

BONUS_UNITS = '魔力值|魔力|爆米花|积分|做种积分|金币|憨豆|金贝|鲸币|奖励'

BONUS_AMOUNT = '[\\d,]+(?:\\.\\d+)?'

BONUS_COUNTERS = '个|粒|枚|点|個|顆'

BONUS_VERBS = '获得|獲得|獲取'

BONUS_PATTERNS = (f'((?:{BONUS_VERBS})\\s*{BONUS_AMOUNT}\\s*(?:{BONUS_COUNTERS})?\\s*(?:{BONUS_UNITS}))', f'({BONUS_AMOUNT}\\s*(?:{BONUS_COUNTERS})\\s*(?:{BONUS_UNITS}))', f'((?:{BONUS_UNITS})\\s*[:：]?\\s*\\+?\\s*{BONUS_AMOUNT})', f'\\+({BONUS_AMOUNT})\\s*(?:{BONUS_UNITS})', f'(?:签到已得|簽到已得)\\s*({BONUS_AMOUNT})')

DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

DEFAULT_RULES: Dict[str, Dict[str, Any]] = {'open.cd': {'engine': 'cdp',
             'mode': 'image',
             'path': '/plugin_sign-in.php',
             'preflight_path': '/',
             'verify_success_path': '/',
             'activity_path': '/userdetails.php',
             'captcha_selector': '#frmSignin img',
             'captcha_input_selector': '#imagestring',
             'submit_selector': '#ok',
             'submit_method': 'ajax',
             'success_json': {'field': '/state', 'values': ['success']},
             'success_keywords': ['查看簽到記錄', '簽到成功', '签到成功'],
             'already_keywords': ['查看簽到記錄', '已簽到', '已签到', '今日已签', '今日已簽'],
             'strict_status_keywords': True},
 'p.t-baozi.cc': {'engine': 'cdp',
                  'mode': 'image',
                  'path': '/attendance.php',
                  'captcha_selector': "form:has(input[name='imagestring']) img",
                  'captcha_input_selector': "input[name='imagestring']",
                  'submit_selector': "input[type='submit']",
                  'already_keywords': ['签到成功']},
 'pt.muxuege.org': {'engine': 'cdp',
                    'mode': 'open_page',
                    'path': '/attendance.php',
                    'submit_selector': "input[type='submit'], button[type='submit']",
                    'submit_method': 'dom_click',
                    'submit_text': '立即签到'},
 'oshen.win': {'engine': 'cdp',
               'mode': 'image',
               'path': '/attendance.php',
               'captcha_selector': "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
               'captcha_input_selector': "form input[name='imagestring']",
               'submit_selector': "form input[type='submit'][value='立即签到']",
               'submit_method': 'ajax'},
 'ptsbao.club': {'engine': 'cdp',
                 'mode': 'image',
                 'path': '/attendance.php',
                 'captcha_selector': "form:has(input[name='imagestring']) img[alt='CAPTCHA']",
                 'captcha_input_selector': "form input[name='imagestring']",
                 'submit_selector': "form input[type='submit'][value='立即签到']",
                 'submit_method': 'ajax',
                 'success_keywords': ['签到成功', '簽到成功', '本次获得', '本次獲得'],
                 'already_keywords': ['已经签到', '已經簽到', '已签到', '已簽到', '今日已签到']},
 'pt.luckpt.de': {'engine': 'cdp',
                  'mode': 'open_page',
                  'path': '/attendance.php',
                  'submit_selector': "input[type='submit'], button[type='submit'], "
                                     "a[href*='attendance']",
                  'submit_method': 'dom_click',
                  'submit_text': '签到',
                  'success_keywords': ['签到成功', '已获得', '本次获得'],
                  'already_keywords': ['已签到', '今天已签到', '今日已签到']},
 'yemapt.org': {'engine': 'cdp',
                'mode': 'altcha',
                'path': '/',
                'route_fragment': '#/user/growth?tab=checkIn',
                'altcha_checkbox_selector': "altcha-widget input[type='checkbox']",
                'altcha_payload_selector': "altcha-widget .altcha[data-state='verified'] "
                                           "input[name='altchaPayload']",
                'submit_selector': 'button.ant-btn-primary.ant-btn-lg.ant-btn-block:not([disabled])',
                'submit_method': 'altcha_click',
                'submit_text': '立即签到',
                'already_keywords': ['已签到，明日继续', '你的今日状态：已签到'],
                'preflight_wait': 8000,
                'wait': 5000},
 'hdsky.me': {'engine': 'cdp',
              'mode': 'trigger_image',
              'path': '/index.php',
              'trigger_selector': '#showup',
              'captcha_selector': '#showupimg',
              'captcha_input_selector': '#imagestring',
              'submit_selector': '#showupbutton',
              'submit_method': 'dom_click',
              'submit_text': "Let's Go",
              'success_keywords': ['已签到']},
 'dstudio.me': {'engine': 'cdp', 'mode': 'cloudflare', 'path': '/attendance.php'},
 'agsvpt.com': {'engine': 'http',
                'method': 'POST',
                'path': '/attendance.php',
                'submit_selector': "input[type='submit'], button[type='submit'], button",
                'submit_method': 'dom_click',
                'submit_text': '立即签到'},
 'mua.xloli.cc': {'engine': 'cdp',
                  'mode': 'cloudflare',
                  'path': '/attendance.php',
                  'submit_selector': "form[action*='attendance.php'] input[type='submit']"},
 'share.ilolicon.com': {'engine': 'cdp',
                        'mode': 'cloudflare',
                        'path': '/attendance.php',
                        'submit_selector': "form[action*='attendance.php'] input[type='submit']"},
 'totheglory.im': {'engine': 'http',
                   'method': 'GET',
                   'path': '/index.php',
                   'already': '<b[^>]*>\\s*已签到\\s*</b>',
                   'already_keywords': ['今天已签到过', '不要太贪', '已連續簽到', '您已連續簽到'],
                   'signin_path': '/signed.php',
                   'signin_method': 'POST',
                   'signin_token_patterns': {'signed_timestamp': 'signed_timestamp:\\s*"(\\d{10})"',
                                             'signed_token': 'signed_token:\\s*"([^"]+)"'},
                   'success_keywords': ['您已连续签到', '已連續簽到', '签到成功']},
 'hdkyl.in': {'engine': 'cdp',
              'mode': 'open_page',
              'path': '/index.php',
              'submit_selector': "a, input[type='submit'], button, [role='button'], span",
              'submit_method': 'dom_click',
              'submit_text': '签到',
              'wait': 12000,
              'pending_keywords': ['正在进行安全检测', '安全检测'],
              'success_keywords': ['签到成功', '簽到成功', '签到完成', '已签到', '本次签到'],
              'already_keywords': ['今日已签到', '今天已签到', '已签到', '已簽到', '已领取']},
 'piggo.me': {'engine': 'cdp',
              'method': 'GET',
              'cloudflare_wait': 40,
              'path': '/index.php',
              'submit_method': 'dom_click',
              'submit_selector': "a, input[type='submit'], button, [role='button'], span",
              'submit_text': '签到',
              'wait': 15000,
              'pending_keywords': ['正在进行安全检测', '安全检测', '正在验证您是否是真人'],
              'success_keywords': ['签到成功', '簽到成功', '签到完成', '已签到', '本次签到'],
              'already_keywords': ['今日已签到', '今天已签到', '已签到', '已簽到', '已领取']},
 'hdarea.club': {'engine': 'cdp',
                 'method': 'GET',
                 'path': '/index.php',
                 'submit_selector': '#sign_in, a[href*="sign_in"]',
                 'submit_text': '签到',
                 'success_keywords': ['此次签到您获得', '[已签到]', '已签到'],
                 'already_keywords': ['请不要重复签到哦', '[已签到]', '已签到']},
 'nexushd.org': {'engine': 'http',
                 'method': 'POST',
                 'path': '/signin.php',
                 'data': {'action': 'post', 'content': ''},
                 'success_keywords': ['本次签到获得'],
                 'already_keywords': ['你今天已经签到过了']},
 'hdcity.city': {'engine': 'http',
                 'method': 'GET',
                 'path': '/sign',
                 'success_keywords': ['本次签到获得魅力'],
                 'already_keywords': ['已签到']},
 'hdupt.com': {'engine': 'http',
               'method': 'GET',
               'path': '/added.php?action=qiandao',
               'success_keywords': ['本次签到获得魅力'],
               'already': '<span[^>]*id="yiqiandao"'},
 'btschool.club': {'engine': 'cdp',
                   'method': 'GET',
                   'path': '/index.php',
                   'submit_selector': 'a[href*="addbonus"]',
                   'submit_text': '每日签到',
                   'absent_keywords': ['每日签到'],
                   'already_keywords': ['每日签到', '签到成功', '连续签到'],
                   'success_keywords': ['每日签到', '签到成功']},
 'audiences.me': {'engine': 'cdp',
                  'method': 'GET',
                  'path': '/attendance.php',
                  'submit_selector': 'button, a[role="button"], [role="button"], input[type="submit"]',
                  'submit_text': '签到',
                  'success_keywords': ['已签到', '签到成功', '获得'],
                  'already_keywords': ['已签到', '今日已签到', '获得']},
 'pttime.org': {'engine': 'cdp',
                'method': 'GET',
                'path': '/attendance.php',
                'submit_selector': 'a[href*="attendance.php"]',
                'submit_text': '签到',
                'success_keywords': ['签到成功', '签到领魔力', '已签到'],
                'already_keywords': ['今日已签到', '今天已签到', '已签到', '签到领魔力', '拒绝访问：已签到']},
 'hares.top': {'engine': 'http',
               'method': 'GET',
               'path': '/attendance.php?action=sign',
               'success_keywords': ['签到成功', '"success":true'],
               'already_keywords': ['已签到', '已经签到']},
 'pterclub.com': {'engine': 'http',
                  'method': 'GET',
                  'path': '/attendance-ajax.php',
                  'success_keywords': ['"status":"1"', '签到已成功', '本次签到获得'],
                  'already_keywords': ['您今天已经签到过了', '请勿重复刷新']},
 'haidan.video': {'engine': 'http',
                  'method': 'GET',
                  'path': '/index.php',
                  'already_keywords': ['已经打卡', '已打卡'],
                  'signin_path': '/signin.php',
                  'signin_method': 'GET',
                  'signin_headers': {'X-Requested-With': 'XMLHttpRequest'},
                  'success_keywords': ['已经打卡', '已打卡', '打卡成功', '签到成功']},
 'hdchina.org': {'engine': 'http',
                 'method': 'GET',
                 'path': '/index.php',
                 'already': '<a[^>]*class="label label-default"[^>]*>已签到</a>',
                 'signin_path': '/plugin_sign-in.php?cmd=signin',
                 'signin_method': 'POST',
                 'signin_token_patterns': {'csrf': '<meta name="x-csrf" content="([^"]+)"'},
                 'success_keywords': ['"state":"success"', '"state": "success"']},
 '52pt.site': {'engine': 'http',
               'method': 'GET',
               'path': '/bakatest.php',
               'already_keywords': ['今天已经签过到了'],
               'signin_path': '/bakatest.php',
               'signin_method': 'POST',
               'signin_pick': 'random',
               'signin_data': {'usercomment': '太难了！', 'wantskip': '不会'},
               'success': '\\d+\\s*点魔力值'},
 'ptchdbits.co': {'engine': 'http',
                  'method': 'GET',
                  'path': '/bakatest.php',
                  'already_keywords': ['今天已经签过到了'],
                  'signin_path': '/bakatest.php',
                  'signin_method': 'POST',
                  'signin_pick': 'random',
                  'signin_data': {'usercomment': '太难了！', 'wantskip': '不会'},
                  'success': '\\d+\\s*点魔力值'},
 'u2.dmhy.org': {'engine': 'http',
                 'method': 'GET',
                 'path': '/showup.php',
                 'already': '<a[^>]*href="showup.php"[^>]*>已签到</a>',
                 'signin_path': '/showup.php?action=show',
                 'signin_method': 'POST',
                 'signin_pick': 'random',
                 'signin_data': {'message': '一切随缘~'},
                 'success_keywords': ["window.location.href = 'showup.php'"]},
 'zhuque.in': {'engine': 'http',
               'method': 'GET',
               'path': '/',
               'signin_path': '/api/gaming/fireGenshinCharacterMagic',
               'signin_method': 'POST',
               'signin_token_patterns': {'csrf': '<meta name="x-csrf-token" content="([^"]+)"'},
               'signin_headers': {'x-csrf-token': '{csrf}',
                                  'Content-Type': 'application/json; charset=utf-8'},
               'signin_json_raw': '{"all": 1, "resetModal": "true"}',
               'success_keywords': ['"status":200', 'FIRE_GENSHIN_CHARACTER_MAGIC_SUCCESS']},
 'hddolby.com': {'engine': 'http',
                 'method': 'GET',
                 'path': '/attendance.php',
                 'success_keywords': ['签到成功', '签到完成', '领取成功'],
                 'already_keywords': ['签到已得', '今日已签到', '已经签到', '请不要重复签到']},
 'sunnypt.top': {'engine': 'cdp',
                 'method': 'GET',
                 'path': '/user/attendance',
                 'submit_method': 'dom_click',
                 'submit_selector': "button, [role='button'], a",
                 'submit_text': '立即签到',
                 'wait': 8000,
                 'success_keywords': ['本次获得', '签到成功'],
                 'already_keywords': ['今日已签到', '今日签到已完成', '已连续签到', '已签到']},
 'htpt.cc': {'engine': 'http',
             'method': 'GET',
             'path': '/attendance.php',
             'activity_path': '/userdetails.php',
             'already_keywords': ['今日已签到', '今日已经签到', '今天已签到'],
             'success_keywords': ['签到成功', '簽到成功'],
             'bonus': '(今日获得\\s*[\\d,]+\\s*魔力值)'},
 'hhanclub.net': {'engine': 'cdp',
                  'mode': 'open_page',
                  'path': '/attendance.php',
                  'submit_selector': "button, a[role='button'], div[role='button'], "
                                     "input[type='submit']",
                  'submit_method': 'dom_click',
                  'submit_text': '签到',
                  'success_keywords': ['签到成功', '簽到成功', '领取成功', '簽到完成', '签到完成'],
                  'already_keywords': ['今日已签到', '今日已经签到', '今天已签到', '今日已領取', '本次签到获得', '已连续签到'],
                  'bonus': '(本次签到获得\\s*[\\d,]+\\s*个?憨豆)'},
 'btschool.club': {'engine': 'cdp',
                   'method': 'GET',
                   'path': '/index.php',
                   'submit_selector': 'a[href*="addbonus"]',
                   'submit_text': '每日签到',
                   'absent_keywords': ['每日签到'],
                   'already_keywords': ['每日签到', '签到成功', '连续签到'],
                   'success_keywords': ['每日签到', '签到成功']},
 'audiences.me': {'engine': 'cdp',
                  'mode': 'cloudflare',
                  'path': '/attendance.php',
                  'submit_method': 'auto',
                  'auto_wait': 30000,
                  'cloudflare_wait': 15,
                  'wait': 15000,
                  'verify_success_path': '/attendance.php',
                  'verify_wait': 15000,
                  'success_keywords': ['签到成功', '簽到成功', '成功签到', '签到完成', '领取成功'],
                  'already_keywords': ['今日已签到',
                                       '今天已经签到',
                                       '您今天已经签到',
                                       '已经签到',
                                       '已签到',
                                       '今日已领取',
                                       '已连续签到',
                                       '明日再来',
                                       '明天再来'],
                  'pending_keywords': ['验证通过后将自动完成签到', '完成人机验证即可领取今日爆米花奖励']}}


@dataclass(frozen=True)
class SignResult:
    status: str
    message: str
    challenge: str = ''
    transient: bool = False
    missing_path: bool = False

def normalize_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or '').lower().removeprefix('www.').rstrip('.')

def parse_rules(raw: Any) -> Dict[str, Dict[str, Any]]:
    """解析自定义规则；键为域名或站点 ID。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as error:
        raise ValueError(f'站点规则必须是有效 JSON：{error}') from error
    if not isinstance(value, Mapping):
        raise ValueError('站点规则必须是以域名或站点 ID 为键的 JSON 对象')
    result: Dict[str, Dict[str, Any]] = {}
    for key, rule in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(rule, Mapping):
            raise ValueError('每条站点规则必须使用非空字符串键和 JSON 对象值')
        result[key.lower().strip()] = dict(rule)
    return result

def preset_for_host(host: str) -> Optional[Dict[str, Any]]:
    """按域名取内置预设：先精确匹配，再按「以 .<预设域名> 结尾」兜底。

    站点常用 `pt.` / `www.` / `bbs.` 前缀（例如学校是 `pt.btschool.club`，预设键写的是
    `btschool.club`），这样不必为每个子域各写一条；`open.cd` 的子域也一并覆盖。
    """
    preset = DEFAULT_RULES.get(host)
    if preset is not None:
        return preset
    for key, value in DEFAULT_RULES.items():
        if host.endswith('.' + key):
            return value
    return None

def random_form_fields(html: str) -> Dict[str, str]:
    """从签到页表单里取隐藏字段 + 随机挑一个选项/按钮。

    对应两类「答题式」签到：

    * 52pt / CHDBits 的 bakatest：隐藏字段 ``questionid`` + 若干 ``choice[]``，随便选一个提交；
    * U2 的 showup：隐藏字段 ``req`` / ``hash`` / ``form`` + 若干提交按钮（选项），随机点一个。
    """
    hidden: Dict[str, str] = {}
    options: List[tuple] = []
    for tag in re.findall('<input\\b[^>]*>', html or '', re.IGNORECASE):
        attrs = _tag_attrs(tag)
        name = str(attrs.get('name') or '').strip()
        if not name:
            continue
        kind = str(attrs.get('type') or 'text').lower()
        value = str(attrs.get('value') or '')
        if kind in {'submit', 'button', 'image'}:
            options.append((name, value))
        elif kind in {'radio', 'checkbox'} or 'choice' in name:
            options.append((name, value))
        elif kind == 'hidden':
            hidden[name] = value
    if not options:
        return hidden
    name, value = random.choice(options)
    hidden[name] = value
    return hidden

def extract_reward_sentence(content: str) -> str:
    """只认「获得 X 点魔力值」这种明确的奖励句子。

    静默成功的站点（返回的是首页）侧栏也有「魔力值 [使用]: 242,141.0」这类统计数字，
    用宽泛的 extract_bonus 会把它们当成奖励，所以这里只匹配带「获得 / 签到已得」的说法。
    """
    match = re.search(REWARD_SENTENCE_PATTERN, str(content or ''), re.IGNORECASE)
    if not match:
        return ''
    return re.sub('\\s+', ' ', match.group(1)).strip()

def extract_bonus(content: str, rule: Mapping[str, Any]) -> str:
    """提取签到奖励描述（如「获得 22 粒爆米花」）。规则里的 bonus 字段可覆盖默认模板。"""
    custom = str(rule.get('bonus') or '').strip()
    patterns = (custom,) if custom else BONUS_PATTERNS
    for candidate in (content, plain_text(content)):
        for pattern in patterns:
            try:
                found = re.search(pattern, candidate, re.IGNORECASE)
            except re.error:
                continue
            if not found:
                continue
            values = [group for group in found.groups() if group]
            text = values[-1] if values else found.group(0)
            return re.sub('\\s+', ' ', text).strip().strip(',，、;；')
    return ''

LOGIN_FAILURE_PATTERN = '(用户名或密码错误|用户名或密碼錯誤|账号或密码错误|密碼錯誤|登录失败|登入失敗|invalid (?:username|password)|incorrect password)'

ACTIVITY_KEYWORDS = ('最近动向', '最近動向', '最新动向', '最新動向', '最近活动', '最近活動', '最近访问', '最近訪問', '上次访问', '上次訪問', '本次访问', '上次签到', '上次簽到', '上次活动', '上次活動', '上次登入', '上次登录', '上次造访', '上次造訪', '最后访问', '最後訪問', '最后活动', '最後活動', '最后上线', '最後上線', '最后活跃', '最後活躍', '最后造访', '最後造訪', '最近登入', '最近登錄', '最近上线', '最近上線', '最近活跃', '最近活躍', '最后在线', '最後在線')

RULE_CONTEXT_PATTERN = '(规则|規則|說明|说明|首次|连续签到第|若|如果|条件|條件|奖励说明|奖励规则)'

DATETIME_PATTERN = '(\\d{4}[-/.]\\d{1,2}[-/.]\\d{1,2}(?:[ T]\\d{1,2}:\\d{2}(?::\\d{2})?)?)'

ACTIVITY_WINDOW = 160

RELATIVE_TIME_PATTERN = '\\s*([（(]\\s*[^（()）]{0,16}?前\\s*[)）])'

_DATETIME_RE = re.compile(DATETIME_PATTERN)

_RELATIVE_TIME_RE = re.compile(RELATIVE_TIME_PATTERN)

_ATTR_RE = re.compile('([a-zA-Z_:][-a-zA-Z0-9_:.]*)\\s*=\\s*(\\"[^\\"]*\\"|\'[^\']*\'|[^\\s>]+)')

REWARD_SENTENCE_PATTERN = f'((?:{BONUS_VERBS}|签到已得|簽到已得)\\s*{BONUS_AMOUNT}\\s*(?:{BONUS_COUNTERS})?\\s*(?:{BONUS_UNITS}))'

SITE_ACCOUNT_FIELDS = ('username', 'user', 'account', 'password', 'passwd', 'pwd', 'login_path', 'login_action', 'username_field', 'password_field', 'fields', 'activity_path')

def note_object(note: Any) -> Dict[str, Any]:
    """解析站点管理附加信息对象或 JSON 文本。"""
    if isinstance(note, Mapping):
        return dict(note)
    if isinstance(note, str):
        raw = note.strip()
        if raw.startswith('{'):
            try:
                value = json.loads(raw)
            except ValueError:
                return {}
            if isinstance(value, Mapping):
                return dict(value)
    return {}

def account_from_site(site: Mapping[str, Any]) -> Dict[str, Any]:
    """从 MoviePilot「站点管理」的站点记录里取登录凭证与登录选项（取不到时返回空字典）。"""
    account: Dict[str, Any] = {}
    for source in (note_object(site.get('note')), site):
        if not isinstance(source, Mapping):
            continue
        for key in SITE_ACCOUNT_FIELDS:
            value = source.get(key)
            if value not in (None, '') and key not in account:
                account[key] = value
    username = str(account.get('username') or account.get('user') or account.get('account') or '').strip()
    password = str(account.get('password') or account.get('passwd') or account.get('pwd') or '')
    if username:
        account['username'] = username
    if password:
        account['password'] = password
    return account

def account_ready(account: Optional[Mapping[str, Any]]) -> bool:
    """凭证是否够做一次账号密码登录。"""
    if not account:
        return False
    return bool(str(account.get('username') or '').strip() and str(account.get('password') or ''))

def _tag_attrs(tag: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        value = match.group(2)
        if len(value) >= 2 and value[0] in '"\'' and (value[-1] == value[0]):
            value = value[1:-1]
        attrs[match.group(1).lower()] = value
    return attrs

def _normalize_time(value: str) -> str:
    return re.sub('\\s+', ' ', re.sub('[/.]', '-', str(value or '').strip()))

def relative_suffix(text: str, start: int) -> str:
    """取出紧跟在时间后面的相对时间括号，例如「( < 1分前 )」。"""
    match = _RELATIVE_TIME_RE.match(text[start:start + 32])
    return re.sub('\\s+', ' ', match.group(1)).strip() if match else ''

def is_zero_time(value: str) -> bool:
    """判断时间是不是被站点补成整零点（``2026-05-06 00:00:00``）。

    这种值多半是「只有日期」的字段（注册日期、加入日期、模板默认值）被补了零，
    不是真的最近动向；皇后那条假时间就是它。
    """
    moments = str(value or '').strip()
    return moments.endswith('00:00:00') or moments.endswith('00:00')

def activity_value(text: str, match: Any) -> str:
    """时间 + 站点自带的相对时间，例如「2026-10-16 23:44:21 ( < 1分前 )」。"""
    value = _normalize_time(match.group(1))
    suffix = relative_suffix(text, match.end())
    return f'{value} {suffix}' if suffix else value

def extract_activity(html: str) -> Dict[str, str]:
    """从用户页里抓「最近动向」片段与其中的时间，返回 keyword / snippet / last_seen。

    两个防呆点（都踩过坑）：

    1. 关键词后面必须紧跟时间才算命中。以前关键词附近没时间时会退回「全页第一个日期」，
       而种子列表 / 公告里的裸日期到处都是，于是「最近动向」被填成 `2026-09-18` 这种假值；
    2. 没有关键词命中就直接返回空，宁可页面提示「未读取到」（可以配 activity_path），
       也不把公告 / 活动通知里的日期当成最近动向；整零点时间同样不算——那是被补零的
       日期字段（注册日期、模板默认值）。
    """
    text = plain_text(html or '')
    best: tuple[int, int, int, int, str, re.Match[str]] | None = None
    best_rank = (2, ACTIVITY_WINDOW + 1, len(ACTIVITY_KEYWORDS))
    for order, keyword in enumerate(ACTIVITY_KEYWORDS):
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            start = index + 1
            match = _DATETIME_RE.search(text, index, index + ACTIVITY_WINDOW)
            if match is None:
                continue
            has_suffix = 0 if relative_suffix(text, match.end()) else 1
            if is_zero_time(match.group(1)):
                continue
            gap = match.start() - (index + len(keyword))
            rank = (has_suffix, gap, order)
            if rank < best_rank:
                best_rank = rank
                best = (has_suffix, gap, order, index, keyword, match)
    if best is not None:
        _, _, _, index, keyword, match = best
        snippet = text[max(0, index - 12):index + 140].strip()
        return {'keyword': keyword, 'snippet': snippet, 'last_seen': activity_value(text, match)}
    return {}

def _uid_from_jwt(value: str) -> str:
    """从 JWT 或 `base64(payload.签名)` 这类凭据里取 user_id（烧包的 c_secure_pass 是后者）。"""
    token = str(value or '').strip()
    if not token:
        return ''
    candidates = [token.split('.')[0], token]
    for candidate in candidates:
        candidate = candidate.strip()
        if len(candidate) < 8:
            continue
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(candidate + '=' * (-len(candidate) % 4)).decode('utf-8', 'ignore')
            except Exception:
                continue
            match = re.search('"?user_?id"?\\s*[:=]\\s*"?(\\d+)', decoded, re.IGNORECASE)
            if match:
                return match.group(1)
            if decoded.strip().isdigit():
                return decoded.strip()
    return ''

def cookie_uid(cookie: str) -> str:
    """从 Cookie 里解出自己的用户 ID（NexusPHP 惯例：``c_secure_uid`` 是 base64 过的 uid）。

    自定义主题的站点首页常常不挂 ``userdetails.php?id=`` 链接，这时插件拿不到 uid、也就打不开
    用户页（「最近动向」自然读不到）——所以优先用 Cookie 里这份 uid。
    Cookie 名不全统一，``c_secure_uid`` 之外还认 ``uid`` / ``c_uid`` / ``user_id`` 等；
    值可能是 base64（带或不带 ``=``、可能被 URL 编码一到两次），也可能直接是数字。
    """
    text = str(cookie or '')
    for name in ('c_secure_uid', 'uid', 'c_uid', 'userid', 'user_id', 'c_secure_pass'):
        match = re.search(f'(?:^|;\\s*){name}=([^;]+)', text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        for _ in range(3):
            unquoted = urllib.parse.unquote(value)
            if unquoted == value:
                break
            value = unquoted
        if value.isdigit():
            return value
        jwt_uid = _uid_from_jwt(value)
        if jwt_uid:
            return jwt_uid
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(value + '=' * (-len(value) % 4)).decode('utf-8', 'ignore')
            except Exception:
                continue
            decoded = decoded.strip()
            if decoded.isdigit():
                return decoded
            jwt_uid = _uid_from_jwt(decoded)
            if jwt_uid:
                return jwt_uid
    return ''
