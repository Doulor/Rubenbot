"""用离线响应验证作品绑定、只读公开数据及失败口径。"""

import asyncio
import json
from urllib.parse import quote

import pytest

from plugins.link_detail.primary import extract, matches
from rubenbot.linkparse.fetch import FetchError, FetchResult


BV = 'BV1xx411c7mD'
VIDEO = f'https://www.bilibili.com/video/{BV}'
BAPI = 'https://api.bilibili.com/x/'
VIEW = BAPI + 'web-interface/view?bvid=' + BV
DY = 'https://www.douyin.com/video/7721'
SHARE = 'https://www.iesdouyin.com/share/video/7721/'
XHS = 'https://www.xiaohongshu.com/explore/65abc?xsec_token=example'
PIX = 'https://www.pixiv.net/en/artworks/8100'
PAPI = 'https://www.pixiv.net/ajax/illust/8100'


def page_state(name, data):
    return f'<script>window.{name} = {json.dumps(data, ensure_ascii=False)};</script>'


def run(key, url, responses):
    calls = []

    async def read(u):
        calls.append(u)
        response = responses[u]  # 未约定的读取直接让测试失败（包括取媒体等子请求）。
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FetchResult):
            return response
        if isinstance(response, dict):
            return FetchResult(u, 'application/json', json.dumps(response))
        return FetchResult(u, 'text/html', response)

    return asyncio.run(extract(key, url, read)), calls


@pytest.mark.parametrize(('key', 'url'), [
    ('bilibili', VIDEO), ('bilibili', 'https://bilibili.com/av123'),
    ('bilibili', 'https://live.bilibili.com/123'), ('bilibili', 'https://t.bilibili.com/123'),
    ('bilibili', 'https://m.bilibili.com/opus/123'), ('bilibili', 'https://bili2233.cn/F4m'),
    ('douyin', 'https://v.douyin.com/K8yA/'), ('douyin', 'https://www.iesdouyin.com/share/slides/123/'),
    ('xiaohongshu', XHS), ('xiaohongshu', 'https://xhslink.cn/o/E8Ay'), ('pixiv', PIX),
])
def test_matches_work_routes(key, url):
    assert matches(key, url)


@pytest.mark.parametrize(('key', 'url'), [
    ('bilibili', VIDEO + '/fake'), ('bilibili', 'https://api.bilibili.com/video/' + BV),
    ('bilibili', 'https://space.bilibili.com/123/favlist?fid=456'),
    ('douyin', 'https://www.douyin.com/user/7721'), ('douyin', 'https://douyin.com.example/video/7721'),
    ('xiaohongshu', 'https://www.xiaohongshu.com/search_result?keyword=note'),
    ('pixiv', PAPI), ('pixiv', 'https://www.pixiv.net/users/8100'), ('pixiv', 'https://example.net/artworks/8100'),
])
def test_does_not_claim_nonwork_routes(key, url):
    assert not matches(key, url)
    assert run(key, url, {}) == (None, [])


def test_video_prefers_api_no_second_fetch_and_keeps_part():
    data = dict(bvid=BV, title='航线', owner={'name': '绒鸟'}, pic='//i.example/cover.jpg',
                desc='候鸟调查', duration=601, pubdate=1700000000, stat={'view': 902, 'like': 0},
                pages=[{'page': 2, 'part': '河口', 'duration': 82}])
    out, calls = run('bilibili', VIDEO + '?p=2', {VIEW: {'code': 0, 'data': data}})
    assert calls == [VIEW] and out.title == '航线 · P2 河口'
    assert (out.author, out.cover, out.duration, out.description) == ('绒鸟', 'https://i.example/cover.jpg', '1:22', '候鸟调查')
    assert out.stats == {'播放': 902, '点赞': 0} and out.published == '2023-11-14'


def test_short_video_reuses_resolved_page_when_api_unavailable():
    short = 'https://b23.tv/A9Bc'
    data = {'video': {'viewInfo': {'bvid': BV, 'title': '恒星', 'owner': {'name': '星友'}}}}
    page = FetchResult(VIDEO, 'text/html', page_state('__INITIAL_STATE__', data))
    out, calls = run('bilibili', short, {short: page, VIEW: FetchError('站点返回 HTTP 412')})
    assert calls == [short, VIEW] and (out.title, out.author) == ('恒星', '星友')


@pytest.mark.parametrize(('tag', 'ident'), [('bvid', 'BV2xx411c7mD'), ('aid', '123')])
def test_unrelated_video_and_page_recommendations_are_not_details(tag, ident):
    data = {tag: ident, 'title': '推荐页的视频', 'owner': {'name': '作者'}}
    page = page_state('__INITIAL_STATE__', {'videoData': data, 'recommendations': [{'bvid': BV, 'title': '也不要'}]})
    assert run('bilibili', VIDEO, {VIEW: {'code': 0, 'data': data}, VIDEO: page})[0] is None


def test_dynamic_and_opus_bind_object_and_reject_hidden():
    endpoint = BAPI + 'polymer/web-dynamic/v1/detail?id=128'
    mods = {'module_author': {'name': '小宋'}, 'module_dynamic': {'major': {'opus': {
        'summary': {'text': '海水结冰的记录'}, 'pics': [{'url': 'https://i.example/sea.jpg'}]}}},
            'module_stat': {'like': {'count': 0}}}
    for path in ('https://t.bilibili.com/128', 'https://www.bilibili.com/opus/128', 'https://m.bilibili.com/dynamic/128'):
        item = {'id_str': '128', 'modules': mods}
        out, _ = run('bilibili', path, {endpoint: {'code': 0, 'data': {'item': item}}})
        assert out.title == out.description == '海水结冰的记录' and out.author == '小宋' and out.stats == {'点赞': 0}
        for update in ({'id_str': '127'}, {'visible': False}):
            assert run('bilibili', path, {endpoint: {'code': 0, 'data': {'item': item | update}}})[0] is None


def test_article_and_live_use_their_own_fields():
    url = 'https://www.bilibili.com/read/cv42'
    endpoint = BAPI + 'article/viewinfo?id=42'
    article = {'id': 42, 'title': '模型展', 'author_name': '木子', 'stats': {'view': 180}, 'image_urls': ['https://i.example/cv']}
    out, _ = run('bilibili', url, {endpoint: {'code': 0, 'data': article}})
    assert (out.title, out.cover, out.stats) == ('模型展', 'https://i.example/cv', {'阅读': 180})
    assert run('bilibili', url, {endpoint: {'code': 0, 'data': article | {'id': 43}}})[0] is None
    url = 'https://live.bilibili.com/12'
    endpoint = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id=12'
    room = {'room_id': 128008, 'short_id': 12, 'title': '直播画水彩', 'area_name': '绘画', 'online': 960}
    data = {'room_info': room, 'anchor_info': {'base_info': {'uname': '阿奇'}}}
    out, _ = run('bilibili', url, {endpoint: {'code': 0, 'data': data}})
    assert (out.title, out.author, out.description, out.stats) == ('直播画水彩', '阿奇', '绘画', {'人气': 960})
    room['short_id'] = 13
    assert run('bilibili', url, {endpoint: {'code': 0, 'data': data}})[0] is None


def dy_page(*items):
    data = {'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': list(items)}}}}
    return page_state('_ROUTER_DATA', data)


def test_douyin_follow_public_share_and_match_id_not_first_item():
    item = {'aweme_id': '7721', 'desc': '烟花', 'author': {'nickname': '舞灯'}, 'statistics': {'digg_count': 12},
            'video': {'duration': 45000, 'cover': {'url_list': ['https://i.example/cover']}}}
    out, calls = run('douyin', DY, {DY: '<title>抖音</title>', SHARE: dy_page(item | {'aweme_id': '5', 'desc': '推荐'}, item)})
    assert calls == [DY, SHARE]
    assert (out.title, out.author, out.duration, out.stats) == ('烟花', '舞灯', '0:45', {'点赞': 12})
    assert run('douyin', SHARE, {SHARE: dy_page(item | {'aweme_id': '7720'})})[0] is None


def test_douyin_http_failure_can_try_public_share_and_report_play_count():
    item = {'aweme_id': '7721', 'desc': '日出', 'statistics': {'play_count': 680}}
    out, calls = run('douyin', DY, {DY: FetchError('站点返回 HTTP 403'), SHARE: dy_page(item)})
    assert calls == [DY, SHARE] and out.stats == {'播放': 680}


@pytest.mark.parametrize('error', ['站点策略拒绝目标链接', '目标不是公网单播地址', '正文过大', '链接抓取超时'])
def test_no_fallback_after_transport_or_security_failure(error):
    for key, url, first_url in [('douyin', DY, DY), ('bilibili', VIDEO, VIEW)]:
        assert run(key, url, {first_url: FetchError(error)}) == (None, [first_url])


def test_douyin_accepts_encoded_desktop_state_and_refuses_login_redirect():
    item = {'awemeId': '7721', 'desc': '雨景', 'author': {'nickname': '雨燕'}}
    state = quote(json.dumps({'17': {'aweme': {'detail': item}}}))
    page = f'<script id="RENDER_DATA" type="application/json">{state}</script>'
    assert run('douyin', DY, {DY: page})[0].title == '雨景'
    for bad in (FetchResult('https://www.douyin.com/login', 'text/html', page), '<title>登录 - 抖音</title>' + page):
        assert run('douyin', DY, {DY: bad, SHARE: bad})[0] is None


def test_xhs_uses_target_map_key_and_preserves_undefined_in_strings():
    item = {'title': 'undefined 的意思', 'desc': '前端笔记', 'user': {'nickname': '阿澜'},
            'imageList': [{'urlDefault': 'https://i.example/x.jpg'}], 'interactInfo': {'likedCount': '1.2万'}}
    def page(key, note):
        state = {'ad': None, 'note': {'noteDetailMap': {key: {'note': note}}}}
        return page_state('__INITIAL_STATE__', state).replace('"ad": null', '"ad":undefined')

    out, calls = run('xiaohongshu', XHS, {XHS: page('65abc', item)})
    assert calls == [XHS] and out.title == 'undefined 的意思' and out.stats == {'点赞': '1.2万'}
    for html in (page('65abb', item), page('65abc', item | {'noteId': '65abb'}), page('65abc', {'title': '只有标题'})):
        assert run('xiaohongshu', XHS, {XHS: html})[0] is None


def test_xhs_short_link_and_discovery_mobile_schema():
    url = 'https://xhslink.com/o/Axx'
    final = 'https://www.xiaohongshu.com/discovery/item/65abc?xsec_token=example'
    item = {'id': '65abc', 'title': '露台', 'user': {'nickName': '阿远'}, 'imageList': [{'url': 'https://i.example/x.jpg'}]}
    html = page_state('__INITIAL_STATE__', {'noteData': {'data': {'noteData': item}}})
    out, calls = run('xiaohongshu', url, {url: FetchResult(final, 'text/html', html)})
    assert calls == [url] and (out.author, out.cover) == ('阿远', 'https://i.example/x.jpg')
    for other in ('https://www.douyin.com/video/65', 'https://www.xiaohongshu.com/login'):
        assert run('xiaohongshu', url, {url: FetchResult(other, 'text/html', html)})[0] is None


PIX_ITEM = {'id': '8100', 'title': '荧火', 'userName': '青空', 'description': '<p>山间 &amp; 湖面</p>',
            'urls': {'small': 'https://i.example/art.jpg'}, 'xRestrict': 0, 'restrict': 0, 'bookmarkCount': 8}


def test_pixiv_requires_explicit_public_all_ages_artwork_and_never_fetches_media():
    out, calls = run('pixiv', PIX, {PAPI: {'error': False, 'body': PIX_ITEM}})
    assert calls == [PAPI] and (out.title, out.author, out.description, out.stats) == ('荧火', '青空', '山间 & 湖面', {'收藏': 8})
    for key in ('restrict', 'xRestrict', 'id'):
        assert run('pixiv', PIX, {PAPI: {'error': False, 'body': {k: v for k, v in PIX_ITEM.items() if k != key}}})[0] is None


@pytest.mark.parametrize('update', [{'id': '8101'}, {'illustId': '9100'}, {'xRestrict': False}, {'xRestrict': '0'},
                                    {'xRestrict': 1}, {'xRestrict': 2}, {'restrict': 1}, {'isMasked': True},
                                    {'isUnlisted': True}, {'isLoginOnly': True}, {'tags': {'tags': [{'tag': 'R-18G'}]}}])
def test_pixiv_fails_closed_on_unknown_restricted_or_unrelated(update):
    assert run('pixiv', PIX, {PAPI: {'error': False, 'body': PIX_ITEM | update}})[0] is None


@pytest.mark.parametrize(('key', 'url', 'endpoint'), [('xiaohongshu', XHS, XHS), ('douyin', SHARE, SHARE), ('pixiv', PIX, PAPI)])
def test_site_shell_is_not_content_even_with_og(key, url, endpoint):
    html = '<title>登录</title><meta property="og:title" content="站点名称"><meta property="og:image" content="https://i.example/logo">'
    assert run(key, url, {endpoint: html})[0] is None
