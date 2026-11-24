"""只解码公开页面中的数据，不执行脚本或读取子资源。"""

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .model import Detail


def obj(value):
    return value if isinstance(value, dict) else {}


def seq(value):
    return value if isinstance(value, list) else []


def field(data, *keys):
    for key in keys:
        data = obj(data).get(key)
    return data


def text(value):
    if type(value) not in (str, int, float):
        return ''
    return ' '.join(str(value).split())


def stats(data, **labels):
    return {label: data[key] for label, key in labels.items()
            if type(data.get(key)) in (int, str) and text(data[key])}


def date(value):
    s = text(value)
    if s in ('0', '-1'):
        return ''
    if re.fullmatch(r'[0-9]{10}(?:[0-9]{3})?', s):
        return datetime.fromtimestamp(int(s[:10]), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return s


def clock(value, scale=1):
    s = text(value)
    if not re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,6})?', s):
        return ''
    seconds = int(float(s) / scale)
    if not seconds:
        return ''
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02}:{s:02}' if h else f'{m}:{s:02}'


def decode(source):
    if not isinstance(source, str):
        return None
    try:
        return json.loads(source)
    except (ValueError, RecursionError):
        return None


def assigned(script, name):
    """只认变量赋值后的 JSON 字面量；忽略余下的脚本。"""
    match = re.search(r'(?<![\w.])(?:window\.)?' + re.escape(name) + r'\s*=\s*', script)
    if match:
        try:
            return json.JSONDecoder().raw_decode(script, match.end())[0]
        except (ValueError, RecursionError):
            pass
    return None


class Page(HTMLParser):
    """记录 meta、内联脚本以及选择的文本节点（NGA 主楼 / 推文引用）。"""

    _void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
             'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__()
        self.meta = {}
        self.nodes = {}
        self.scripts = []
        self.auth_challenge = False
        self._script = None
        self._stack = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form' and attrs.get('id') in ('challenge-form', 'captcha-form', 'login_form'):
            self.auth_challenge = True
        if tag == 'meta':
            self.meta[attrs.get('property') or attrs.get('name')] = attrs.get('content') or ''
        if tag == 'script':
            self._script = (attrs.get('id'), attrs.get('type'), [])
            self.scripts.append(self._script)
        if tag in self._void:
            return
        parent = self._stack[-1][1] if self._stack else ''
        key = parent
        if tag == 'title':
            key = 'title'
        elif attrs.get('id') in ('postsubject0', 'postcontent0', 'postauthor0', 'postdate0'):
            key = attrs['id']
        elif tag == 'blockquote' and 'twitter-tweet' in (attrs.get('class') or '').split():
            key = 'tweet'
        elif parent == 'tweet' and tag == 'p':
            key = 'tweet_text'
        elif tag in ('script', 'style'):
            key = ''
        self._stack.append((tag, key))

    def handle_endtag(self, tag):
        if tag == 'script':
            self._script = None
        # 只在栈顶附近修复未闭合标签，畸形页面不反复倒扫整棵树。
        for i in range(len(self._stack) - 1, max(-1, len(self._stack) - 12), -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._script is not None:
            self._script[2].append(data)
        elif self._stack and (key := self._stack[-1][1]):
            self.nodes.setdefault(key, []).append(data)

    def node(self, key):
        return text(' '.join(self.nodes.get(key, [])))

    def json_id(self, key):
        for script_id, _, chunks in self.scripts:
            if script_id == key:
                return decode(''.join(chunks))
        return None

    def variable(self, key):
        for _, _, chunks in self.scripts:
            value = assigned(''.join(chunks), key)
            if value is not None:
                return value
        return None

    def blocked(self):
        return self.auth_challenge or bool(re.search(r'^(?:just a moment|security check|access denied|verify (?:you|that)|'
                              r'captcha|log[ -]?in|sign[ -]?in|登录|请登录|安全验证|访问验证|新浪访客系统|'
                              r'sina visitor system|验证中心|验证码|知乎 - 安全中心)',
                              self.node('title'), re.I))


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    p = _Plain()
    p.feed(text(value))
    p.close()
    return text(' '.join(p.parts))


_WALL = re.compile(r'^(?:请(?:先)?登录(?:后|以|查看|继续)|登录后(?:可见|查看)|扫码登录|访问受限|安全验证|'
                   r'人机验证|作品(?:不存在|已删除)|verify you are human|captcha|sign in to (?:continue|view)|'
                   r'log in to (?:continue|view)|please enable javascript)', re.I)


def detail(title, *, author=None, cover=None, duration='', stats=None, description=None, published=None):
    title, author, description = text(title), text(author), text(description)
    if _WALL.match(title) or _WALL.match(description):
        return None
    published = date(published)
    if not title or title == description:
        description, title = '', title or description
    cover = text(cover)
    if cover.startswith('//'):
        cover = 'https:' + cover
    parsed = urlsplit(cover)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
        cover = ''
    if not title or not any((author, cover, duration, stats, description, published)):
        return None
    return Detail(title=title, author=author, cover=cover, duration=duration, stats=stats or {},
                  description=description, published=published)
