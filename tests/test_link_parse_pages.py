"""只用内存页面验证解析、短链展开后的分派和站点开关。"""

import asyncio
from types import SimpleNamespace

import pytest

from plugins.link_parse.parse import resolve


VIDEO = "https://www.bilibili.com/video/BV1xx411c7mD/"
WEB = "https://example.org/news/1"
BOTH = frozenset({"generic", "bilibili"})


def parse(html, *, url=WEB, final=None, sites=BOTH, mime="text/html; charset=UTF-8"):
    calls = []

    async def fetch(requested):
        calls.append(requested)
        assert requested == url
        return SimpleNamespace(text=html, final_url=final or url, content_type=mime)

    return asyncio.run(resolve(url, sites=sites, fetch=fetch)), calls


def test_short_link_uses_final_video_and_prefers_initial_state():
    short = "https://b23.tv/abc"
    result, calls = parse('''
        <meta property="og:title" content="分享视频">
        <script>window.__INITIAL_STATE__ = {"videoData":{"title":"访谈 {一}",
        "owner":{"name":"山雨"},"desc":"公开访谈","duration":3672}};
        (function(){})();</script>
    ''', url=short, final=VIDEO, sites=frozenset({"bilibili"}))
    assert calls == [short]
    assert result == {"标题": "访谈 {一}", "UP主": "山雨", "简介": "公开访谈",
                      "时长": "1:01:12", "链接": VIDEO}


def test_video_malformed_script_falls_back_to_page_metadata():
    result, _ = parse('''<script>window.__INITIAL_STATE__ = invalid;</script>
        <title>普通标题</title><meta property="og:title" content="视频标题">
        <meta name="author" content="老张"><meta name="description" content="视频简介">
        <meta property="og:video:duration" content="65">''', url=VIDEO)
    assert result == {"标题": "视频标题", "UP主": "老张", "简介": "视频简介",
                      "时长": "01:05", "链接": VIDEO}


def test_generic_prefers_article_and_ignores_navigation_and_scripts():
    result, _ = parse('''<title>旧标题</title><meta content="标题 &amp; 正文" property="og:title">
        <meta property="og:site_name" content="新闻站"><meta name="og:description" content="摘要">
        <body><p>推广</p><article><nav>导航</nav><p>来<b>看</b>海。</p><p>第二行。</p>
        <script>不可见</script><style>不可见</style><aside>广告</aside></article></body>''')
    assert result == {"标题": "标题 & 正文", "摘要": "摘要", "正文": "来看海。 第二行。",
                      "站点": "新闻站", "链接": WEB}


@pytest.mark.parametrize(("html", "expected"), [
    ("<title>只有标题</title>", {"标题": "只有标题"}),
    ('<meta property="og:description" content="只有介绍">', {"摘要": "只有介绍"}),
    ("<p>第一段</p><p>第二段</p>", {"正文": "第一段 第二段"}),
])
def test_generic_fallbacks(html, expected):
    assert parse(html)[0] == {**expected, "链接": WEB}


@pytest.mark.parametrize(("url", "sites"), [
    (VIDEO, {"generic"}), ("https://b23.tv/abc", {"generic"}),
    ("https://space.bilibili.com/123", {"generic"}),
    (WEB, {"bilibili"}), (WEB, set()),
])
def test_disabled_site_is_not_fetched(url, sites):
    assert parse("<title>忽略</title>", url=url, sites=frozenset(sites)) == (None, [])


@pytest.mark.parametrize(("url", "final", "sites"), [
    (WEB, VIDEO, {"generic"}), ("https://b23.tv/abc", WEB, {"bilibili"}),
    ("https://b23.tv/abc", "https://b23.tv/abc", BOTH),
])
def test_redirect_does_not_bypass_site_selection(url, final, sites):
    assert parse("<title>忽略</title>", url=url, final=final, sites=frozenset(sites)) == (None, [url])


def test_bilibili_lookalike_is_a_generic_site():
    url = "https://bilibili.com.example.org/video/av1"
    assert parse("<p>通用正文</p>", url=url, sites=frozenset({"generic"}))[0] == {
        "正文": "通用正文", "链接": url,
    }


def test_fetch_failure_uses_resolver_error_contract():
    async def fetch(url):
        raise TimeoutError("超时")

    with pytest.raises(ValueError, match="抓取失败"):
        asyncio.run(resolve(WEB, sites=BOTH, fetch=fetch))


def test_non_html_and_empty_page_are_not_previews():
    assert parse("<title>不是网页</title>", mime="image/svg+xml")[0] is None
    assert parse("<script>隐藏文字</script>")[0] is None
