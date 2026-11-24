"""认领公开作品路由；每次读取均经宿主，不请求页面给出的资源地址。"""

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

from rubenbot.linkparse.fetch import FetchError

from .model import Detail
from .primary_bili import extract_bili
from .primary_data import api, clock, counts, date, detail, first, http_failure, obj, seq, states, text

_SHORT = {
    'bilibili': ('b23.tv', 'bili2233.cn'), 'douyin': ('v.douyin.com', 'jx.douyin.com'),
    'xiaohongshu': ('xhslink.com', 'xhslink.cn'), 'pixiv': (),
}
_PAGES = {
    'bilibili': ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 't.bilibili.com', 'live.bilibili.com'),
    'douyin': ('douyin.com', 'www.douyin.com', 'm.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com', 'jingxuan.douyin.com'),
    'xiaohongshu': ('xiaohongshu.com', 'www.xiaohongshu.com'), 'pixiv': ('www.pixiv.net', 'pixiv.net'),
}
HOSTS = {key: hosts + _SHORT[key] for key, hosts in _PAGES.items()}
HOSTS['bilibili'] += ('api.bilibili.com', 'api.live.bilibili.com')


def _route(key, url):
    try:
        p = urlsplit(url)
        if (p.scheme not in ('http', 'https') or '@' in p.netloc or p.port not in (None, 80, 443)
                or '\\' in url or any(ord(c) < 32 for c in url)):
            return None
    except ValueError:
        return None
    host, path = p.hostname, p.path.removesuffix('/')
    if host in _SHORT.get(key, ()):
        pattern = r'/(?:[a-zA-Z0-9_-]+/)?[a-zA-Z0-9_-]+' if key == 'xiaohongshu' else r'/[a-zA-Z0-9_-]+'
        return ('short', '', 1) if re.fullmatch(pattern, path) else None
    if host not in _PAGES.get(key, ()):
        return None
    if key == 'bilibili':
        if host in ('live.bilibili.com', 't.bilibili.com'):
            if m := re.fullmatch(r'/([1-9][0-9]*)', path):
                return ('live' if host.startswith('live.') else 'dynamic', m[1], 1)
        elif m := re.fullmatch(r'/(?:video/)?(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)', path):
            part = parse_qs(p.query).get('p', ['1'])[0]
            if re.fullmatch(r'[1-9][0-9]{0,3}', part):
                return ('video', m[1], int(part))
        elif m := re.fullmatch(r'/(opus/|dynamic/|read/cv)([1-9][0-9]*)', path):
            return ('article' if m[1] == 'read/cv' else 'dynamic', m[2], 1)
    elif key == 'douyin':
        if m := re.fullmatch(r'/(?:m/|share/)?(video|note|slides)/([1-9][0-9]*)', path):
            return ('note' if m[1] == 'slides' else m[1], m[2], 1)
    elif key == 'xiaohongshu':
        if m := re.fullmatch(r'/(?:explore|discovery/item)/([a-zA-Z0-9]+)', path):
            return ('note', m[1], 1)
    elif key == 'pixiv':
        if m := re.fullmatch(r'/(?:[a-z]{2}/)?artworks/([1-9][0-9]*)', path):
            return ('artwork', m[1], 1)
    return None


def matches(key: str, url: str) -> bool:
    return _route(key, url) is not None


def _douyin(source, ident, kind):
    for state in states(source, '_ROUTER_DATA', render=True):
        route = obj(obj(state.get('loaderData')).get(f'{kind}_(id)/page'))
        info = obj(route.get('videoInfoRes'))
        if info.get('status_code', 0) != 0:
            continue
        items = list(seq(info.get('item_list')))
        # 桌面公开页有时以 RENDER_DATA 编码，键为路由槽位而非作品 ID。
        for slot in state.values():
            aweme = obj(obj(slot).get('aweme'))
            if aweme:
                items.append(aweme.get('detail') or aweme)
        for raw in items:
            item = obj(raw)
            if str(item.get('aweme_id') or item.get('awemeId')) != ident:
                continue
            status = obj(item.get('status'))
            if status.get('is_delete') or status.get('is_private') or status.get('private_status', 0) != 0:
                continue
            video = obj(item.get('video'))
            cover = obj(video.get('cover'))
            pic = obj(first(item.get('images')))
            title = item.get('item_title') or item.get('desc')
            return detail(title=title, description=item.get('desc'), author=obj(item.get('author')).get('nickname'),
                          cover=first(cover.get('url_list') or cover.get('urlList')) or first(pic.get('url_list')),
                          duration=clock(video.get('duration'), millis=True), published=date(item.get('create_time') or item.get('createTime')),
                          stats=counts(item.get('statistics'), play_count='播放', digg_count='点赞', comment_count='评论', share_count='分享', collect_count='收藏'))
    return None


def _xhs(source, ident):
    for state in states(source, '__INITIAL_STATE__'):
        notes = obj(obj(state.get('note')).get('noteDetailMap'))
        item = obj(obj(notes.get(ident)).get('note'))
        # 移动页用单独的结构，没有作品 ID 作键，必须验证正文自带的 ID。
        if not item:
            item = obj(obj(obj(state.get('noteData')).get('data')).get('noteData'))
            if not (item.get('noteId') or item.get('id')):
                continue
        if any(str(item[k]) != ident for k in ('noteId', 'id') if k in item):
            continue
        author = obj(item.get('user'))
        pic = obj(first(item.get('imageList')))
        return detail(title=item.get('title') or item.get('desc'), description=item.get('desc'),
                      author=author.get('nickname') or author.get('nickName'), cover=pic.get('urlDefault') or pic.get('url'),
                      duration=clock(obj(obj(item.get('video')).get('capa')).get('duration')),
                      published=date(item.get('time'), millis=True), stats=counts(item.get('interactInfo'), likedCount='点赞', collectedCount='收藏', commentCount='评论', shareCount='分享'))
    return None


async def _pixiv(read, ident):
    payload = await api(read, 'https://www.pixiv.net/ajax/illust/' + ident)
    item = obj(payload.get('body'))
    # bool 是 int 的子类，不能把 false 当成明确的全年龄/公开标记 0。
    if payload.get('error') is not False or not all(type(item.get(k)) is int and item[k] == 0 for k in ('xRestrict', 'restrict')):
        return None
    ids = [item[k] for k in ('id', 'illustId') if k in item]
    if not ids or any(str(i) != ident for i in ids):
        return None
    if (item.get('isMasked') or item.get('isUnlisted') or item.get('isPrivate')
            or item.get('isDeleted') or item.get('visible') is False or item.get('isLoginOnly')):
        return None
    tags = seq(obj(item.get('tags')).get('tags'))
    if any(text(obj(tag).get('tag')).upper() in ('R-18', 'R-18G') for tag in tags):
        return None
    images = obj(item.get('urls'))
    desc = unescape(re.sub(r'<[^>]+>', ' ', text(item.get('description')))).strip()
    return detail(title=item.get('title') or item.get('illustTitle'), author=item.get('userName'),
                  description=desc, cover=images.get('small') or images.get('regular') or images.get('thumb'),
                  published=date(item.get('createDate')), stats=counts(item, viewCount='浏览', likeCount='点赞', bookmarkCount='收藏', commentCount='评论'))


async def extract(platform: str, url: str, read) -> Detail | None:
    route = _route(platform, url)
    if not route:
        return None
    page = None
    if route[0] == 'short':
        page = await read(url)
        if not page or not (route := _route(platform, page.final_url)) or route[0] == 'short':
            return None
        url = page.final_url
    kind, ident, page_no = route

    async def html():
        result = page or await read(url)
        return result.text if result and _route(platform, result.final_url) == route else ''

    if platform == 'bilibili':
        return await extract_bili(kind, ident, page_no, read, html)
    if platform == 'pixiv':
        return await _pixiv(read, ident)
    if platform == 'xiaohongshu':
        return _xhs(await html(), ident)
    if platform == 'douyin':
        try:
            result = _douyin(await html(), ident, kind)
        except FetchError as exc:
            if not http_failure(exc):
                return None
            result = None
        share = f'https://www.iesdouyin.com/share/{kind}/{ident}/'
        if result is None and urlsplit(url).hostname not in ('iesdouyin.com', 'www.iesdouyin.com'):
            fallback = await read(share)
            if fallback and _route(platform, fallback.final_url) == route:
                result = _douyin(fallback.text, ident, kind)
        return result
    return None
