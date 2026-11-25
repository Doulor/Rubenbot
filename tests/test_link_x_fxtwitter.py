"""X（Twitter）推文：fxtwitter 路 —— core 解析与 link_detail 插件共用。

全部用构造的 fxtwitter 响应离线运行，不访问外网。正文按实测那条法语长帖的
形态（长段落、法语标点）构造，不是逐字抄录的线上原文；要验的是取值路径：
作者、正文、时间能从镜像响应里读出来，且不拿别的推文顶替。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from plugins.link_detail import public
from rubenbot.linkparse import xapi
from rubenbot.linkparse.fetch import FetchError, FetchResult
from rubenbot.linkparse.service import LinkService
from rubenbot.linkparse.settings import LinkConfig
from rubenbot.linkparse.urls import x_json_api, x_status

STATUS = "2125520927920214526"
URL = f"https://x.com/Frenchie_/status/{STATUS}"
MIRROR = f"https://api.fxtwitter.com/Frenchie_/status/{STATUS}"

BODY = (
    "Je vois beaucoup de gens dire que l'IA va tout changer. "
    "Pour avoir passé dix ans dans ce métier, je pense que le vrai bouleversement "
    "n'est pas la technologie elle-même, mais ce qu'on décide d'en faire. "
    "Le reste, c'est de la communication."
)

TWEET = {
    "code": 200,
    "message": "OK",
    "tweet": {
        "id": STATUS,
        "url": URL,
        "text": BODY,
        "created_at": "Fri Nov 21 09:14:00 +0000 2026",
        "created_timestamp": 1795252440,
        "likes": 1284,
        "reposts": 96,
        "replies": 45,
        "views": 98000,
        "author": {"id": "42", "name": "Frenchie", "screen_name": "Frenchie_"},
        "media": {"photos": [{"type": "photo", "url": "https://pbs.twimg.com/media/fx.jpg"}]},
    },
}


def mirror(payload=None):
    return FetchResult(MIRROR, "application/json", json.dumps(payload or TWEET, ensure_ascii=False))


# ----------------------------------------------------------------------
# 纯字符串：推文识别与镜像地址
# ----------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    f"https://x.com/Frenchie_/status/{STATUS}",
    f"https://twitter.com/Frenchie_/status/{STATUS}",
    f"https://mobile.twitter.com/Frenchie_/status/{STATUS}",
    f"https://x.com/Frenchie_/status/{STATUS}/photo/1",
    f"https://x.com/Frenchie_/status/{STATUS}/",
])
def test_x_status_and_mirror_url(url):
    assert x_status(url) == ("Frenchie_", STATUS)
    assert x_json_api(url) == MIRROR


@pytest.mark.parametrize("url", [
    "https://x.com/Frenchie_", "https://x.com/i/web/status/123",
    "https://x.com/Frenchie_/status/not-a-number", "https://news.example/status/123",
    "https://t.co/abc",
])
def test_non_status_links_have_no_mirror(url):
    assert x_json_api(url) == ""


# ----------------------------------------------------------------------
# fxtwitter 字段取值
# ----------------------------------------------------------------------


def test_fxtwitter_fields_keep_author_body_time_and_media():
    data = xapi.parse(json.dumps(TWEET, ensure_ascii=False))
    fields = xapi.fields(data, STATUS)
    assert fields["作者"] == "Frenchie (@Frenchie_)"
    assert fields["正文"].startswith("Je vois beaucoup de gens dire")
    assert fields["发布时间"] == "2026-11-21 09:14 UTC"
    assert fields["媒体"] == "图片 1 张"
    assert fields["封面"] == "https://pbs.twimg.com/media/fx.jpg"
    assert fields["点赞"] == 1284 and fields["转发"] == 96 and fields["查看"] == 98000


@pytest.mark.parametrize("payload", [
    {"code": 404, "message": "NOT_FOUND"},
    {"code": 200, "tweet": {**TWEET["tweet"], "id": "999"}},
    {"code": 200, "tweet": "not-an-object"},
    {"code": 200},
    [],
])
def test_fxtwitter_rejects_wrong_or_missing_tweet(payload):
    assert xapi.fields(xapi.parse(json.dumps(payload)), STATUS) == {}


def test_fxtwitter_unparsable_body_is_empty():
    for raw in ("", "not json", "<html>error</html>", "{", None):
        assert xapi.parse(raw) == {}


def test_fxtwitter_media_only_tweet_uses_media_as_body():
    tweet = {"id": "7", "author": {"screen_name": "pic"}, "media": {"photos": [{"url": "https://p.example/1.jpg"}],
             "videos": [{"thumbnail_url": "https://p.example/v.jpg"}]}}
    fields = xapi.fields({"code": 200, "tweet": tweet}, "7")
    assert fields["正文"] == "图片 1 张 · 视频 1 个"
    assert fields["作者"] == "@pic" and fields["封面"] == "https://p.example/1.jpg"


# ----------------------------------------------------------------------
# core：oEmbed / 网页读不到时走 fxtwitter
# ----------------------------------------------------------------------


def service_with(pages):
    """按 URL 返回假响应；未列出的 URL 抛 FetchError（模拟 x.com 连不上）。"""
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        page = pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise FetchError("链接格式、连接或响应无效")
        return page

    return LinkService(LinkConfig(), fetch_fn=fetch), calls


def test_core_reads_x_via_fxtwitter_first():
    service, calls = service_with({MIRROR: mirror()})
    result = asyncio.run(service.read(URL))
    assert result.ok and result.source == "mirror"
    assert calls == [MIRROR]
    payload = result.payload()
    assert payload["链接"] == URL and payload["平台"] == "X"
    assert payload["作者"] == "Frenchie (@Frenchie_)"
    assert payload["正文"].startswith("Je vois beaucoup de gens dire")
    assert payload["发布时间"] == "2026-11-21 09:14 UTC"
    # 封面地址只留给需要发图的调用方，不交给模型
    assert "封面" not in payload
    # 策划步只拿索引时仍能看到这条讲什么
    assert result.index()["摘要"].startswith("Je vois beaucoup de gens dire")
    assert "正文" not in result.index()


def test_core_falls_back_to_x_page_when_mirror_fails():
    """镜像不可用时仍保留 x.com 直连；页面里有正文就照常用。"""
    html = ('<html><head><meta property="og:url" content="' + URL + '">'
            '<meta property="og:description" content="推文正文"><title>X 上的 Frenchie</title>'
            "</head><body></body></html>")
    service, calls = service_with({MIRROR: FetchError("站点返回 HTTP 500"),
                                   URL: FetchResult(URL, "text/html", html)})
    result = asyncio.run(service.read(URL))
    assert result.ok and result.source == "page" and result.fields["摘要"] == "推文正文"
    assert calls == [MIRROR, URL]


def test_core_reports_unreadable_when_all_x_routes_fail():
    service, calls = service_with({URL: FetchResult(URL, "text/html", "<title>X</title>")})
    result = asyncio.run(service.read(URL))
    assert not result.ok and "抓不到" not in result.payload().get("正文", "")
    assert result.payload()["状态"] == "抓不到"
    assert calls == [MIRROR, URL]


def test_core_short_link_expands_to_tweet_and_uses_mirror():
    """t.co 展开成推文地址后同样先走 fxtwitter，镜像之外的页面不用再读。"""
    short = "https://t.co/abc"
    service, calls = service_with({
        short: FetchResult(URL, "text/html", "<title>X</title>"),
        MIRROR: mirror(),
    })
    result = asyncio.run(service.read(short))
    assert result.ok and result.source == "mirror" and result.fields["作者"] == "Frenchie (@Frenchie_)"
    assert result.payload()["请求链接"] == short and result.payload()["链接"] == URL
    # 短链先展开（镜像地址没法从 t.co 直接得出），落在推文地址后再补一次镜像。
    assert calls == [short, MIRROR]


def test_core_x_page_landing_on_other_tweet_is_rejected():
    """页面跳到另一条推文时不顶替：没有镜像时如实报抓不到。"""
    other = URL.replace(STATUS, "999")
    html = (f'<meta property="og:url" content="{other}">'
            '<meta property="og:description" content="别人的推文"><title>X 上的某人</title>')
    service, calls = service_with({URL: FetchResult(other, "text/html", html)})
    result = asyncio.run(service.read(URL))
    assert not result.ok and "另一条推文" in result.error
    assert calls == [MIRROR, URL]


def test_core_mirror_timeout_budget_is_wider_than_default(monkeypatch):
    """镜像单独给足总耗时；其余平台仍走默认预算。"""
    seen = []

    async def fake_fetch(url, max_bytes, timeout_seconds, **kwargs):
        seen.append((url, timeout_seconds))
        return mirror() if url == MIRROR else FetchResult(url, "text/html", "")

    monkeypatch.setattr("rubenbot.linkparse.service.fetch", fake_fetch)
    service = LinkService(LinkConfig())
    asyncio.run(service.read(URL))
    asyncio.run(service.read("https://news.example/a"))
    assert seen[0][0] == MIRROR and seen[0][1] == xapi.TIMEOUT_SECONDS
    assert seen[1][1] == LinkConfig().timeout_seconds


@pytest.mark.parametrize("platform_url", [
    "https://www.bilibili.com/video/BV1xx411c7mD/",
    "https://weibo.com/123/abCD",
    "https://www.zhihu.com/question/12/answer/34",
    "https://news.example/story",
])
def test_other_platforms_never_touch_fxtwitter(platform_url):
    service, calls = service_with({
        platform_url: FetchResult(platform_url, "text/html",
                                  "<title>标题</title><article><p>一篇足够长的正文段落，用来看是否被读取。</p></article>"),
    })
    result = asyncio.run(service.read(platform_url))
    assert result.ok and calls == [platform_url]
    assert all("fxtwitter" not in url for url in calls)


# ----------------------------------------------------------------------
# link_detail 插件：同一套优先级
# ----------------------------------------------------------------------


def run_plugin(url, pages):
    calls = []

    async def read(target, **kwargs):
        calls.append(target)
        page = pages.get(target)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise FetchError("链接格式、连接或响应无效")
        return page

    return asyncio.run(public.extract("x", url, read)), calls


def test_plugin_prefers_fxtwitter():
    card, calls = run_plugin(URL, {MIRROR: mirror()})
    assert calls == [MIRROR]
    assert card is not None
    assert card.author == "Frenchie (@Frenchie_)"
    assert card.published == "2026-11-21 09:14 UTC"
    # 标题接描述正好是原文，不丢字也不重复
    assert card.title + card.description == BODY
    assert card.title.startswith("Je vois beaucoup de gens dire")
    assert card.cover == "https://pbs.twimg.com/media/fx.jpg"
    assert card.stats["点赞"] == 1284


def test_plugin_uses_oembed_when_mirror_fails():
    oembed = {
        "type": "rich", "url": URL, "author_name": "Frenchie",
        "html": '<blockquote class="twitter-tweet"><p>原帖正文</p> — 别抄署名</blockquote>',
    }
    oembed_url = f"https://publish.twitter.com/oembed?url=https%3A%2F%2Ftwitter.com%2Fi%2Fweb%2Fstatus%2F{STATUS}&omit_script=true"
    card, calls = run_plugin(URL, {
        MIRROR: FetchError("超时"),
        oembed_url: FetchResult(oembed_url, "application/json", json.dumps(oembed)),
    })
    assert calls == [MIRROR, oembed_url]
    assert card is not None and (card.title, card.author) == ("原帖正文", "Frenchie")


def test_plugin_empty_oembed_still_consults_mirror_and_page():
    """oEmbed 返回空不再直接判失败：镜像照常先行，空 oEmbed 之后继续读网页。"""
    oembed_url = f"https://publish.twitter.com/oembed?url=https%3A%2F%2Ftwitter.com%2Fi%2Fweb%2Fstatus%2F{STATUS}&omit_script=true"
    html = ('<meta property="og:url" content="' + URL + '">'
            '<meta property="og:description" content="Je vois beaucoup de gens dire">'
            '<meta name="twitter:creator" content="@Frenchie_">')
    card, calls = run_plugin(URL, {
        MIRROR: FetchError("超时"),
        oembed_url: FetchResult(oembed_url, "application/json", ""),
        URL: FetchResult(URL, "text/html", html),
    })
    assert calls == [MIRROR, oembed_url, URL]
    assert card is not None and card.title == "Je vois beaucoup de gens dire"


def test_plugin_all_x_routes_empty_is_not_fabricated():
    """镜像、oEmbed、网页都拿不到时如实返回抓不到，不编造。"""
    oembed_url = f"https://publish.twitter.com/oembed?url=https%3A%2F%2Ftwitter.com%2Fi%2Fweb%2Fstatus%2F{STATUS}&omit_script=true"
    card, calls = run_plugin(URL, {
        MIRROR: FetchError("超时"),
        oembed_url: FetchResult(oembed_url, "application/json", json.dumps({"error": "not found"})),
        URL: FetchResult(URL, "text/html", "<title>X</title>"),
    })
    assert card is None
    assert calls == [MIRROR, oembed_url, URL]


def test_plugin_x_page_fallback_requires_same_status():
    html = ('<meta property="og:url" content="https://x.com/other/status/999">'
            '<meta property="og:description" content="别人的推文">')
    card, calls = run_plugin(URL, {URL: FetchResult(URL, "text/html", html)})
    assert card is None and len(calls) == 3 and calls[-1] == URL


def test_plugin_mirror_id_mismatch_is_not_used():
    other = json.loads(json.dumps(TWEET))
    other["tweet"]["id"] = "999"
    card, calls = run_plugin(URL, {MIRROR: mirror(other)})
    assert card is None
    assert calls[0] == MIRROR


def test_plugin_ignores_i_web_links_without_username():
    """``i/web`` 形态没有用户名，不猜镜像地址；走 oEmbed / 网页。"""
    url = f"https://twitter.com/i/web/status/{STATUS}"
    oembed = {"type": "rich", "url": url, "author_name": "Frenchie",
              "html": '<blockquote class="twitter-tweet"><p>原帖</p></blockquote>'}
    oembed_url = f"https://publish.twitter.com/oembed?url=https%3A%2F%2Ftwitter.com%2Fi%2Fweb%2Fstatus%2F{STATUS}&omit_script=true"
    card, calls = run_plugin(url, {oembed_url: FetchResult(oembed_url, "application/json", json.dumps(oembed))})
    assert calls == [oembed_url]
    assert card is not None and card.title == "原帖"


def test_fxtwitter_mirror_is_allowed_for_reads_but_not_claimed():
    """镜像域名只为经宿主 read 取 JSON 放行；它自己的链接不算作品地址。"""
    from plugins.link_detail import routing

    assert routing.allowed("x", MIRROR)
    assert routing.identify(MIRROR, ["x"]) is None
    assert routing.identify(URL, ["x"]) == "x"
