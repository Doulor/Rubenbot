"""十三个平台的公开作品入口。所有读取（含短链与 API）均经宿主 read。"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from . import public_api, public_pages
from .model import Detail
from .public_common import Page, decode, obj

HOSTS: dict[str, tuple[str, ...]] = {
    'xiaoheihe': ('xiaoheihe.cn',), 'acfun': ('acfun.cn',),
    'instagram': ('instagram.com', 'instagr.am'), 'iwara': ('iwara.tv',),
    'kuaishou': ('kuaishou.com', 'v.m.chenzhongtech.com'),
    'netease_music': ('music.163.com', '163cn.tv'),
    'nga': ('ngabbs.com', 'nga.178.com', 'bbs.nga.cn'), 'shipinhao': ('weixin.qq.com',),
    'tiktok': ('tiktok.com',), 'x': ('x.com', 'twitter.com'),
    'weibo': ('weibo.com', 'm.weibo.cn', 'mapp.api.weibo.cn'),
    'youtube': ('youtube.com', 'youtu.be'), 'zhihu': ('zhihu.com',),
}


def _route(key, url):
    p = urlsplit(url)
    h, path = p.hostname, p.path.rstrip('/')
    if (key not in HOSTS or not h or p.scheme not in ('http', 'https') or '@' in p.netloc
            or p.port not in (80, 443, None) or '\\' in url or any(ord(c) < 32 for c in url)
            or not any(h == d or h.endswith('.' + d) for d in HOSTS[key])):
        return None
    q = parse_qs(p.query)

    def param(name, pattern):
        values = q.get(name, [])
        return values[0] if len(values) == 1 and re.fullmatch(pattern, values[0], re.ASCII) else ''

    def route(hosts, pattern, kind):
        if h in hosts and (m := re.fullmatch(pattern, path, re.ASCII)):
            return kind, m[1]
        return None

    if key == 'nga':
        if path == '/read.php' and (i := param('tid', r'\d+')):
            return 'thread', i
    elif key == 'shipinhao':
        if h == 'channels.weixin.qq.com' and path in ('/finder-preview/pages/sph', '/finder-preview/pages/feed'):
            for name in ('eid', 'exportId', 'id'):
                if i := param(name, r'[A-Za-z0-9_+/=%-]+'):
                    return 'feed', i
        return route(('weixin.qq.com',), r'/sph/([A-Za-z0-9]+)', 'share')
    elif key == 'netease_music':
        if not path and p.fragment.startswith('/song?'):
            path, _, query = p.fragment.partition('?')
            q = parse_qs(query)
        if h in ('music.163.com', 'y.music.163.com') and path in ('/song', '/m/song') and (i := param('id', r'\d+')):
            return 'song', i
        return route(('163cn.tv',), r'/([\w-]+)', 'share')
    elif key == 'xiaoheihe':
        if h == 'api.xiaoheihe.cn' and path == '/v3/bbs/app/api/web/share' and (i := param('link_id', '[a-z0-9]+')):
            return 'post', i
        if h == 'api.xiaoheihe.cn' and path == '/game/share_game_detail' and param('game_type', '[a-z]+') and (i := param('appid', '[a-z0-9]+')):
            return 'game', i
        return (route(('www.xiaoheihe.cn', 'xiaoheihe.cn'), r'/app/bbs/link/([a-z0-9]+)', 'post') or
                route(('www.xiaoheihe.cn', 'xiaoheihe.cn'), r'/app/topic/game/[a-z]+/([a-z0-9]+)', 'game'))
    elif key == 'youtube':
        hosts = ('www.youtube.com', 'm.youtube.com', 'youtube.com')
        if h in hosts and path == '/watch' and (i := param('v', '[A-Za-z0-9_-]{11}')):
            return 'video', i
        return (route(('youtu.be',), r'/([\w-]{11})', 'video') or
                route(hosts, r'/(?:shorts|live)/([\w-]{11})', 'video'))
    elif key == 'acfun':
        hosts = ('www.acfun.cn', 'acfun.cn', 'm.acfun.cn')
        if h in hosts and path == '/v' and (i := param('ac', r'\d+')):
            return 'video', i
        return route(hosts, r'/v/ac(\d+)(?:_\d+)?', 'video')
    elif key == 'instagram':
        hosts = ('instagram.com', 'www.instagram.com', 'instagr.am', 'www.instagr.am')
        return (route(hosts, r'/(?:p|reels?|tv)/([\w-]+)', 'post') or
                route(hosts, r'/share/(?:(?:p|reel)/)?([\w-]+)', 'share'))
    elif key == 'iwara':
        for kind in ('video', 'image'):
            if result := route(('www.iwara.tv', 'iwara.tv'), f'/{kind}/([a-zA-Z0-9]+)(?:/[^/]+)?', kind):
                return result
    elif key == 'kuaishou':
        return (route(('v.kuaishou.com',), r'/([\w-]+)', 'share') or
                route(('www.kuaishou.com', 'kuaishou.com', 'v.kuaishou.com', 'v.m.chenzhongtech.com'),
                      r'/(?:short-video|fw/(?:photo|long-video))/([\w-]+)', 'photo'))
    elif key == 'tiktok':
        return (route(('vm.tiktok.com', 'vt.tiktok.com'), r'/([\w-]+)', 'share') or
                route(('www.tiktok.com', 'tiktok.com'), r'/t/([\w-]+)', 'share') or
                route(('www.tiktok.com', 'm.tiktok.com', 'tiktok.com'), r'/@[\w.]+/(?:video|photo)/(\d+)', 'post'))
    elif key == 'x':
        return route(('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'),
                     r'/(?:\w+|i/web)/status/(\d+)(?:/(?:photo|video)/\d+)?', 'status')
    elif key == 'weibo':
        if h in ('weibo.com', 'www.weibo.com') and re.fullmatch(r'/tv/show/[0-9]+:[0-9]+', path) and (i := param('mid', r'\d+')):
            return 'status', i
        return (route(('weibo.com', 'www.weibo.com', 'm.weibo.cn'), r'/(?:\d+|status|detail)/([A-Za-z0-9]+)(?:/qq)?', 'status') or
                route(('mapp.api.weibo.cn',), r'/fx/([A-Za-z0-9]+)\.html', 'share'))
    elif key == 'zhihu':
        hosts = ('www.zhihu.com', 'zhihu.com')
        return (route(('zhuanlan.zhihu.com',), r'/p/(\d+)', 'articles') or
                route(hosts, r'/question/\d+/answer/(\d+)', 'answers') or
                route(hosts, r'/question/(\d+)', 'questions') or route(hosts, r'/pin/(\d+)', 'pins'))
    return None


def matches(key: str, url: str) -> bool:
    try:
        return bool(_route(key, url))
    except ValueError:
        return False


def _api_url(platform, kind, ident):
    if platform == 'iwara':
        return f'https://api.iwara.tv/{kind}/{ident}'
    if platform == 'weibo' and kind == 'status':
        return 'https://m.weibo.cn/statuses/show?id=' + ident
    if platform == 'netease_music' and kind == 'song':
        return 'https://music.163.com/api/song/detail/?' + urlencode({'id': ident, 'ids': f'[{ident}]'})
    if platform == 'x':
        return 'https://publish.twitter.com/oembed?' + urlencode({'url': f'https://twitter.com/i/web/status/{ident}', 'omit_script': 'true'})
    if platform == 'zhihu' and kind == 'pins':
        return f'https://www.zhihu.com/api/v4/pins/{ident}?include=content,content_html,created_time,author'
    return ''


async def extract(platform: str, url: str, read) -> Detail | None:
    try:
        if not matches(platform, url):
            return None
        kind, ident = _route(platform, url)
        result = None
        if not _api_url(platform, kind, ident):
            result = await read(url)
            if not matches(platform, result.final_url):
                return None
            final_kind, final_id = _route(platform, result.final_url)
            # 视频号短链偶尔直接给预览 HTML，而不是 302。其余分享票据本身没有内容结构。
            if final_kind == 'share' and platform != 'shipinhao':
                return None
            if kind != 'share' and (kind, ident) != (final_kind, final_id):
                return None
            url, kind, ident = result.final_url, final_kind, final_id
        if api := _api_url(platform, kind, ident):
            result = await read(api)
            # 公开 API 的落点必须仍是所请求的端点，不能把登录回跳或别的作品当成本条。
            a, b = urlsplit(api), urlsplit(result.final_url)
            if (a.scheme, a.netloc, a.path, parse_qs(a.query)) != (b.scheme, b.netloc, b.path, parse_qs(b.query)):
                return None
            return public_api.parse(platform, kind, ident, obj(decode(result.text)))
        page = Page(result.text)
        if page.blocked():
            return None
        return public_pages.parse(platform, kind, ident, page, url)
    except (ValueError, OSError, TimeoutError, RecursionError):
        return None
