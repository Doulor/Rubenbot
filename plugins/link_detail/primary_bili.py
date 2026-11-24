"""B 站四种详情只取目标对象，直播人气不冒充播放量。"""

from rubenbot.linkparse.fetch import FetchError

from .primary_data import api, clock, counts, date, detail, first, http_failure, obj, states, text


_API = 'https://api.bilibili.com/x/'


def _video(record, ident, page_no):
    key, expected = ('bvid', ident) if ident.startswith('BV') else ('aid', ident[2:])
    if str(record.get(key)) != expected or record.get('state', 0) not in (0, None):
        return None
    title, duration, cover = record.get('title'), record.get('duration'), record.get('pic')
    if page_no > 1:
        pages = record.get('pages')
        page = next((obj(p) for p in pages if obj(p).get('page') == page_no), {}) if isinstance(pages, list) else {}
        if not text(page.get('part')):
            return None
        title = f'{text(title)} · P{page_no} {page["part"]}'.strip()
        duration, cover = page.get('duration'), page.get('first_frame') or cover
    return detail(title=title, author=obj(record.get('owner')).get('name'), cover=cover,
                  duration=clock(duration), published=date(record.get('pubdate')),
                  stats=counts(record.get('stat'), view='播放', like='点赞', coin='投币',
                               favorite='收藏', reply='评论', danmaku='弹幕', share='分享'),
                  description=record.get('desc'))


def _dynamic(data, ident):
    post = obj(data.get('item'))
    if str(post.get('id_str')) != ident or post.get('visible') is False:
        return None
    mods = obj(post.get('modules'))
    author, body = obj(mods.get('module_author')), obj(mods.get('module_dynamic'))
    major = obj(body.get('major'))
    opus, archive, draw = (obj(major.get(k)) for k in ('opus', 'archive', 'draw'))
    stats = {k: obj(v).get('count') for k, v in obj(mods.get('module_stat')).items()}
    return detail(title=opus.get('title') or archive.get('title'), author=author.get('name'),
                  description=obj(body.get('desc')).get('text') or obj(opus.get('summary')).get('text') or archive.get('desc'),
                  cover=obj(first(opus.get('pics'))).get('url') or archive.get('cover') or obj(first(draw.get('items'))).get('src'),
                  published=date(author.get('pub_ts')), stats=counts(stats, like='点赞', comment='评论', forward='转发'))


def _article(data, ident):
    # viewinfo 有些版本不回显 id；只认无跳转的精确查询响应和专栏自身字段。
    if 'id' in data and str(data['id']) != ident:
        return None
    if 'id' not in data and not (text(data.get('author_name')) and isinstance(data.get('stats'), dict)):
        return None
    return detail(title=data.get('title'), author=data.get('author_name') or obj(data.get('author')).get('name'),
                  description=data.get('summary'), cover=data.get('banner_url') or first(data.get('image_urls')),
                  published=date(data.get('publish_time')), stats=counts(data.get('stats'), view='阅读', like='点赞', reply='评论', favorite='收藏'))


def _live(data, ident):
    room = obj(data.get('room_info'))
    if ident not in {str(room.get('room_id')), str(room.get('short_id'))}:
        return None
    anchor = obj(obj(data.get('anchor_info')).get('base_info'))
    area = ' / '.join(filter(None, (text(room.get('parent_area_name')), text(room.get('area_name')))))
    return detail(title=room.get('title'), author=anchor.get('uname'), cover=room.get('cover') or room.get('keyframe'),
                  description=area, published=date(room.get('live_start_time')), stats=counts(room, online='人气'))


async def extract_bili(kind, ident, page_no, read, html):
    if kind == 'video':
        atom = f'bvid={ident}' if ident.startswith('BV') else f'aid={ident[2:]}'
        endpoint = _API + 'web-interface/view?' + atom
    elif kind == 'dynamic':
        endpoint = _API + 'polymer/web-dynamic/v1/detail?id=' + ident
    elif kind == 'article':
        endpoint = _API + 'article/viewinfo?id=' + ident
    else:
        endpoint = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id=' + ident
    try:
        payload = await api(read, endpoint)
    except FetchError as exc:
        if not http_failure(exc):
            return None
        payload = {}
    if type(payload.get('code')) is int and payload['code'] == 0:
        data = obj(payload.get('data'))
        if kind == 'video':
            if result := _video(data, ident, page_no):
                return result
        else:
            return {'dynamic': _dynamic, 'article': _article, 'live': _live}[kind](data, ident)
    if kind != 'video':
        return None
    for state in states(await html(), '__INITIAL_STATE__'):
        record = obj(state.get('videoData')) or obj(obj(state.get('video')).get('viewInfo'))
        if result := _video(record, ident, page_no):
            return result
    return None
