"""详情解析共用的数据边界：只读 JSON 与页面状态，不执行脚本。"""

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .model import Detail


def obj(value):
    return value if isinstance(value, dict) else {}


def seq(value):
    return value if isinstance(value, list) else []


def text(value):
    return value.strip() if isinstance(value, str) else ''


def first(values):
    return next(iter(seq(values)), None)


def image(value):
    url = text(value)
    if url.startswith('//'):
        url = 'https:' + url
    try:
        parts = urlsplit(url)
        if parts.scheme in {'http', 'https'} and parts.hostname and parts.username is None:
            return url
    except ValueError:
        pass
    return ''


def counts(values, **labels):
    result = {}
    for key, label in labels.items():
        val = obj(values).get(key)
        if type(val) is int and val >= 0 or isinstance(val, str) and val.strip():
            result[label] = val
    return result


def date(value, *, millis=False):
    if isinstance(value, str) and not value.isdecimal():
        return value.strip()
    try:
        ts = int(value)
        if ts > 0:
            return datetime.fromtimestamp(ts / (1000 if millis else 1), UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return ''


def clock(value, *, millis=False):
    if type(value) is int and value > 0:
        mins, sec = divmod(value // (1000 if millis else 1), 60)
        return f'{mins}:{sec:02d}'
    return ''


_SHELL = re.compile(r'^(?:登录|登陆|请登录|请先登录|扫码登录|安全验证|验证码|访问受限|作品不存在|'
                    r'captcha|sign in|log in|login|verify you are human)(?:$|[\s_-]|后|以)', re.I)


def detail(*, title='', author='', description='', cover='', duration='', published='', stats=None):
    title, author, desc = text(title), text(author), text(description)
    if not (title or desc) or _SHELL.match(title) or _SHELL.match(desc):
        return None
    cover = image(cover)
    if not (author or cover or duration or stats or published or (title and desc and title != desc)):
        return None
    # 纯文字动态没有独立标题，截取自己的首行而非编造标题。
    if not title:
        title = desc.splitlines()[0][:80]
    return Detail(title=title, author=author, cover=cover, duration=duration, stats=stats or {},
                  description=desc, published=published)


# 小红书状态偶尔把空值输出为 JS 的 undefined；跳过带引号的字符串，不改正文。
_JS_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|(?<=:)\s*undefined\b')


def decode(raw, *, js=False):
    try:
        return obj(json.JSONDecoder().raw_decode(raw.lstrip())[0])
    except (ValueError, RecursionError):
        if js and 'undefined' in raw:
            raw = _JS_TOKEN.sub(lambda m: m[0] if m[0].startswith('"') else 'null', raw)
            return decode(raw)
        return {}


class _States(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tag = self.sid = self.title = ''
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'title'):
            self.tag = tag
            self.sid = dict(attrs).get('id')

    def handle_endtag(self, tag):
        if tag == self.tag:
            self.tag = ''

    def handle_data(self, data):
        if self.tag == 'title':
            self.title += data
        elif self.tag == 'script':
            self.scripts.append((self.sid, data))


def states(html, name, *, render=False):
    parser = _States()
    parser.feed(html)
    if _SHELL.match(parser.title.strip()):
        return
    pattern = re.compile(r'^\s*(?:window\s*\.\s*)?' + re.escape(name) + r'\s*=\s*')
    for sid, script in parser.scripts:
        if match := pattern.match(script):
            yield decode(script[match.end():], js=True)
        elif render and sid == 'RENDER_DATA':
            yield decode(unquote(script))


def http_failure(exc):
    # 宿主目前以 FetchError 文案区分目标站 HTTP 与安全/限额/网络故障。
    return re.fullmatch(r'站点返回 HTTP [45][0-9]{2}', str(exc)) is not None


async def api(read, url):
    """拒绝改了接口、参数或主机的跳转；响应必须是 JSON 对象。"""
    page = await read(url)
    if not page or page.final_url != url:
        return {}
    try:
        return obj(json.loads(page.text))
    except (ValueError, RecursionError):
        return {}
