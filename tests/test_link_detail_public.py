"""缩小的第一方响应形状，只用假 read；短链、API 与页面均不联网。"""

import asyncio
import json
from urllib.parse import urlsplit

import pytest

from plugins.link_detail import public
from rubenbot.linkparse.fetch import FetchResult


def script(name, value, variable=False):
    body = json.dumps(value, ensure_ascii=False)
    if variable:
        return f'<script>window.{name} = {body};</script>'
    return f'<script type="application/json" id="{name}">{body}</script>'


CASES = {
    'xiaoheihe': ('https://www.xiaoheihe.cn/app/topic/game/pc/123',
                  script('__NUXT_DATA__', [{'data': 1}, {'game': 2}, {'appid': 3, 'name': 4, 'score': 5},
                                            '123', '航行日志', '9.2']), '航行日志', 'stats', {'评分': '9.2'}),
    'acfun': ('https://www.acfun.cn/v/ac123', script('videoInfo', {
        'dougaId': 123, 'title': '灯塔', 'user': {'name': '海风'}, 'currentVideoInfo': {'durationMillis': 71000}}, True),
        '灯塔', 'duration', '1:11'),
    'instagram': ('https://www.instagram.com/p/ABC_12/', '''<meta property="og:url" content="https://www.instagram.com/p/ABC_12/">
        <meta property="og:description" content='124 likes, 2 comments - diver on May 1: "日落后出海".'>
        <meta property="og:image" content="https://cdninstagram.com/photo.jpg">''', '日落后出海', 'cover', 'https://cdninstagram.com/photo.jpg'),
    'iwara': ('https://www.iwara.tv/video/abcd/ship', {'id': 'abcd', 'rating': 'general', 'title': '白色的船',
        'user': {'name': '制片人'}, 'file': {'id': 'thumb-file', 'duration': 90}, 'thumbnail': 2},
        '白色的船', 'cover', 'https://i.iwara.tv/image/thumbnail/thumb-file/thumbnail-02.jpg'),
    'kuaishou': ('https://v.m.chenzhongtech.com/fw/photo/3xy', script('INIT_STATE', {'abc': {'result': 1,
        'photo': {'photoId': '3xy', 'caption': '海边散步', 'userName': '海螺', 'coverUrls': [{'url': 'https://img.example/1.jpg'}]}}}, True),
        '海边散步', 'author', '海螺'),
    'netease_music': ('https://music.163.com/#/song?id=42', {'code': 200, 'songs': [
        {'id': 41, 'name': '非本曲'}, {'id': 42, 'name': '浪花', 'artists': [{'name': '甲'}, {'name': '乙'}],
                                   'album': {'name': '回归', 'picUrl': 'https://img.example/c.jpg'}}]},
        '浪花', 'author', '甲 / 乙'),
    'nga': ('https://bbs.nga.cn/read.php?tid=123', '''<h3 id="postsubject0">船的结构</h3><span id="postcontent0">甲板和<b>船舱</b></span>
        <span id="postcontent1">别人的回复</span>''', '船的结构', 'description', '甲板和 船舱'),
    'shipinhao': ('https://channels.weixin.qq.com/finder-preview/pages/feed?eid=abcd',
                  script('__INITIAL_STATE__', {'data': {'feedInfo': {'description': '破冰', 'videoDuration': 90},
                                                       'authorInfo': {'nickname': '冰船'}}}, True), '破冰', 'duration', '1:30'),
    'tiktok': ('https://www.tiktok.com/@wave/video/12345', script('__UNIVERSAL_DATA_FOR_REHYDRATION__', {
        '__DEFAULT_SCOPE__': {'webapp.video-detail': {'statusCode': 0, 'itemInfo': {'itemStruct': {
            'id': '12345', 'desc': '风声', 'author': {'nickname': '风'}, 'stats': {'playCount': 0}}}}}}),
        '风声', 'stats', {'播放': 0}),
    'x': ('https://x.com/wave/status/123', {'type': 'rich', 'url': 'https://twitter.com/wave/status/123',
          'author_name': 'Wave', 'html': '<blockquote class="twitter-tweet"><p>原帖 <a href="/">原文</a></p> — 别抄署名</blockquote>'},
          '原帖 原文', 'author', 'Wave'),
    'weibo': ('https://weibo.com/123/abCD', {'ok': 1, 'data': {'id': 123, 'bid': 'abCD', 'text': '轮船<br>到港',
              'user': {'screen_name': '小鱼'}, 'attitudes_count': 0}}, '轮船 到港', 'stats', {'点赞': 0}),
    'youtube': ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', script('ytInitialPlayerResponse', {
        'playabilityStatus': {'status': 'OK'}, 'videoDetails': {'videoId': 'dQw4w9WgXcQ', 'title': '录音',
        'author': '摄影师', 'lengthSeconds': '3662', 'viewCount': '312'}}, True), '录音', 'duration', '1:01:02'),
    'zhihu': ('https://www.zhihu.com/question/12/answer/34', script('js-initialData', {'initialState': {'entities': {
        'questions': {'12': {'title': '为什么有潮汐？'}}, 'answers': {'33': {'content': '无关内容'},
        '34': {'id': 34, 'content': '<p>引力造成潮汐。</p>', 'author': {'name': '答主'}}}}}}),
              '为什么有潮汐？', 'description', '引力造成潮汐。'),
}


def run(key, url, payload, *, landing=None):
    seen = []

    async def read(target):
        seen.append(target)
        h = urlsplit(target).hostname
        assert any(h == d or h.endswith('.' + d) for d in public.HOSTS[key])
        if isinstance(payload, dict):
            return FetchResult(landing or target, 'application/json', json.dumps(payload))
        return FetchResult(landing or target, 'text/html', payload)

    return asyncio.run(public.extract(key, url, read)), seen


@pytest.mark.parametrize('key', CASES)
def test_public_detail(key):
    url, payload, title, attr, value = CASES[key]
    card, seen = run(key, url, payload)
    assert seen and card and card.title == title
    assert getattr(card, attr) == value


@pytest.mark.parametrize('key', CASES)
def test_login_title_only_and_noncontent_landing(key):
    url, payload, *_ = CASES[key]
    assert public.matches(key, url)
    for shell in ('<title>正常标题</title><meta property="og:image" content="https://img.example/logo.jpg">',
                  '<title>登录</title>' + (payload if isinstance(payload, str) else '')):
        assert run(key, url, shell)[0] is None
    assert run(key, url, payload, landing=f'https://{urlsplit(url).hostname}/accounts/login')[0] is None
    assert run(key, url, payload, landing=url.replace('://', '://bad.invalid/', 1))[0] is None
    for bad in (url.replace('://', '://evil.invalid/', 1), url.replace('://', '://someone@', 1),
                url.replace('https:', 'ftp:'), f'https://{urlsplit(url).hostname}.evil.test{urlsplit(url).path}'):
        assert run(key, bad, payload) == (None, [])
    assert not public.matches(key, f'https://{urlsplit(url).hostname}/')


@pytest.mark.parametrize(('key', 'url'), [
    ('netease_music', 'https://music.163.com/search?id=12'), ('nga', 'https://bbs.nga.cn/thread.php?tid=123'),
    ('x', 'https://publish.twitter.com/oembed'), ('iwara', 'https://api.iwara.tv/video/123'),
    ('youtube', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=00000000000'),
    ('zhihu', 'https://www.zhihu.com/people/123'), ('tiktok', 'https://www.tiktok.com/@name'),
    ('instagram', 'https://www.instagram.com/accounts/login/'), ('shipinhao', 'https://weixin.qq.com/finder-preview/pages/sph?id=x'),
    ('xiaoheihe', 'https://www.xiaoheihe.cn/app/topic/game/pc/'), ('kuaishou', 'https://www.kuaishou.com/profile/123'),
    ('acfun', 'https://www.acfun.cn/u/123'), ('weibo', 'https://weibo.com/u/123'),
])
def test_platform_non_content_route(key, url):
    assert not public.matches(key, url)


def test_short_link_uses_final_content_no_other_reads():
    key = 'kuaishou'
    url, body, *_ = CASES[key]
    short = 'https://v.kuaishou.com/2abc'
    card, seen = run(key, short, body, landing=url)
    assert seen == [short] and card.author == '海螺'
    assert run(key, short, body) == (None, [short])


def test_same_host_redirect_cannot_switch_items():
    url, body, *_ = CASES['youtube']
    assert run('youtube', url, body, landing=url.replace('dQw4w9WgXcQ', '00000000000'))[0] is None
    url, data, *_ = CASES['netease_music']
    assert run('netease_music', url, data, landing='https://music.163.com/api/song/detail/?id=70&ids=[70]')[0] is None


def test_instagram_relay_selects_the_requested_shortcode():
    body = script('relay', {'data': {'items': [{'code': 'other', 'caption': {'text': '不是这条'}},
        {'code': 'ABC_12', 'caption': {'text': '清晨的海星'}, 'user': {'username': '摄影师'}}]}})
    card, _ = run('instagram', CASES['instagram'][0], body)
    assert (card.title, card.author) == ('清晨的海星', '摄影师')


def test_kuaishou_desktop_photo_joins_own_author():
    body = script('__APOLLO_STATE__', {'defaultClient': {
        'VisionVideoPhoto:abcd': {'id': 'abcd', 'caption': '黄昏'}, 'User:22': {'name': '作者'},
        'VideoDetail:abcd': {'photo': {'__ref': 'VisionVideoPhoto:abcd'}, 'author': {'__ref': 'User:22'}}}}, True)
    card, _ = run('kuaishou', 'https://www.kuaishou.com/short-video/abcd', body)
    assert (card.title, card.author) == ('黄昏', '作者')


@pytest.mark.parametrize('direct_author', [True, False], ids=['photo', 'linked-detail'])
def test_kuaishou_short_link_detail_photo(direct_author):
    ident = '3xrxw85zqdjizmy'
    photo_ref = f'VisionVideoDetailPhoto:{ident}'
    author_ref = 'VisionVideoDetailAuthor:3xu9jn7pnvxwpug'
    caption = '什么样的女生城府深？ #原创动画 #情感 #恋爱 #爱情'
    cover = 'https://p23.a.yximgs.com/upic/2026/11/24/17/cover.jpg'
    photo = {'id': ident, 'caption': caption, 'duration': 227720, 'likeCount': 2453,
             'viewCount': '3.5万', 'coverUrl': cover, 'timestamp': 1795514845361}
    cache = {'推荐详情': {'photo': {'__ref': 'VisionVideoDetailPhoto:other'}, 'author': {'__ref': 'User:other'}},
             'User:other': {'name': '推荐作者'}, photo_ref: photo, author_ref: {'name': '动画作者'}}
    if direct_author:
        photo['author'] = {'__ref': author_ref}
    else:
        cache['作品详情'] = {'photo': {'__ref': photo_ref}, 'author': {'__ref': author_ref}}
    short = 'https://v.kuaishou.com/JV8w9VwT'
    card, seen = run('kuaishou', short, script('__APOLLO_STATE__', {'defaultClient': cache}, True),
                     landing=f'https://www.kuaishou.com/short-video/{ident}')
    assert seen == [short] and card is not None
    assert (card.title, card.author, card.duration, card.cover) == (caption, '动画作者', '3:47', cover)
    assert card.stats == {'点赞': 2453, '播放': '3.5万'}


@pytest.mark.parametrize('entity_type', ['VisionVideoDetailPhoto', 'VisionVideoPhoto'])
def test_kuaishou_apollo_rejects_mismatched_id(entity_type):
    ref = f'{entity_type}:abcd'
    cache = {ref: {'id': 'other', 'caption': '不是这条', 'likeCount': 1},
             '推荐详情': {'photo': {'__ref': ref}, 'author': {'__ref': 'User:22'}}, 'User:22': {'name': '作者'}}
    body = script('__APOLLO_STATE__', {'defaultClient': cache}, True)
    assert run('kuaishou', 'https://www.kuaishou.com/short-video/abcd', body)[0] is None


def test_kuaishou_mobile_rejects_missing_photo_id():
    body = script('INIT_STATE', {'推荐详情': {'photo': {'caption': '不是这条', 'userName': '推荐作者'}}}, True)
    assert run('kuaishou', CASES['kuaishou'][0], body)[0] is None


def test_challenge_form_rejects_old_metadata():
    url, body, *_ = CASES['instagram']
    assert run('instagram', url, body + '<form id="challenge-form"></form>')[0] is None


def test_structured_login_message_and_unrated_work_are_rejected():
    url, _, *_ = CASES['nga']
    assert run('nga', url, '<h3 id="postsubject0">公告</h3><p id="postcontent0">请先登录后查看此帖</p>')[0] is None
    url, data, *_ = CASES['iwara']
    assert run('iwara', url, {**data, 'rating': None})[0] is None


def test_short_song_resolves_before_reading_detail_api():
    short = 'https://163cn.tv/abc'
    seen = []

    async def read(url):
        seen.append(url)
        if url == short:
            return FetchResult('https://y.music.163.com/m/song?id=42', 'text/html', '')
        return FetchResult(url, 'application/json', json.dumps(CASES['netease_music'][1]))

    card = asyncio.run(public.extract('netease_music', short, read))
    assert card.title == '浪花' and seen == [short, 'https://music.163.com/api/song/detail/?id=42&ids=%5B42%5D']


def test_private_or_different_item_is_not_returned():
    for key, fieldname, value in [('iwara', 'rating', 'ecchi'), ('x', 'url', 'https://twitter.com/other/status/999')]:
        url, data, *_ = CASES[key]
        assert run(key, url, {**data, fieldname: value})[0] is None
    url, body, *_ = CASES['youtube']
    for changed in (body.replace('dQw4w9WgXcQ', '00000000000'), body.replace('"OK"', '"LOGIN_REQUIRED"')):
        assert run('youtube', url, changed)[0] is None
