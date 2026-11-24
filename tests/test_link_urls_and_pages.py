"""链接理解（core 感知层）：抽取、规范化、平台识别、正文提取与投递。

全部使用本地假页面 / 假响应，不访问外网。
"""

from __future__ import annotations

import asyncio

import pytest

from rubenbot.linkparse.extract import extract
from rubenbot.linkparse.fetch import FetchError, FetchResult
from rubenbot.linkparse.urls import GENERIC, extract as extract_urls, is_short, normalize, platform_for, strip_urls


# ----------------------------------------------------------------------
# 链接抽取与规范化
# ----------------------------------------------------------------------


def test_extract_keeps_query_strips_fragment_and_chat_punctuation():
    text = "看看（https://page.example/a?chapter=2#part），还有 https://example.org/One_(film)。"
    assert extract_urls(text) == [
        "https://page.example/a?chapter=2",
        "https://example.org/One_(film)",
    ]


def test_extract_dedupes_and_limits():
    text = " ".join(["https://a.example/x"] * 5 + ["https://b.example/y", "https://c.example/z",
                                                   "https://d.example/w"])
    assert extract_urls(text) == ["https://a.example/x", "https://b.example/y", "https://c.example/z"]


def test_normalize_drops_tracking_and_default_port():
    url = "HTTPS://Page.Example:443/a?utm_source=x&ok=1&fbclid=2#top"
    assert normalize(url) == "https://page.example/a?ok=1"


def test_normalize_keeps_credentials_out():
    assert normalize("https://u:p@page.example/x") == "https://u:p@page.example/x"


@pytest.mark.parametrize(("url", "key"), [
    ("https://www.bilibili.com/video/BV1xx411c7mD/", "bilibili"),
    ("https://b23.tv/abc", "bilibili"),
    ("https://m.weibo.cn/status/123", "weibo"),
    ("https://x.com/someone/status/1", "x"),
    ("https://twitter.com/someone/status/1", "x"),
    ("https://www.zhihu.com/question/1", "zhihu"),
    ("https://mp.weixin.qq.com/s/abc", "wechat"),
    ("https://bilibili.com.example.org/video/av1", "generic"),
    ("https://example.org/news/1", "generic"),
])
def test_platform_for(url, key):
    platform = platform_for(url)
    assert platform.key == key
    if key == "generic":
        assert platform is GENERIC


def test_short_hosts_and_strip_urls():
    assert is_short("https://b23.tv/abc") and is_short("https://t.co/x")
    assert not is_short("https://b23.tv.example.org/abc")
    assert "https" not in strip_urls("这个讲了什么？ https://a.example/x")
    assert "https" not in strip_urls("看 https://a.example/x 这个")


# ----------------------------------------------------------------------
# 正文提取
# ----------------------------------------------------------------------


def test_generic_prefers_og_article_and_ignores_scripts():
    html = """
    <title>旧标题</title>
    <meta property="og:title" content="标题 &amp; 正文">
    <meta property="og:site_name" content="新闻站">
    <meta name="description" content="摘要">
    <body><p>推广</p><article><nav>导航</nav><p>来看海。</p><p>第二行。</p>
    <script>不可见</script><style>不可见</style><aside>广告</aside></article></body>
    """
    fields = extract("https://example.org/news/1", html, platform=GENERIC).fields
    assert fields == {"标题": "标题 & 正文", "摘要": "摘要", "正文": "来看海。 第二行。", "站点": "新闻站"}


def test_json_ld_fills_missing_metadata():
    html = """
    <meta property="og:site_name" content="站">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"NewsArticle","headline":"结构化标题",
     "description":"结构化摘要","author":{"@type":"Person","name":"作者甲"},
     "datePublished":"2026-11-20T08:00:00+08:00"}
    </script>
    <p>这是一段足够长的正文内容，用来补足页面没有 article 标签时的提取。</p>
    """
    fields = extract("https://example.org/news/2", html, platform=GENERIC).fields
    assert fields["标题"] == "结构化标题"
    assert fields["摘要"] == "结构化摘要"
    assert fields["作者"] == "作者甲"
    assert fields["发布时间"].startswith("2026-11-20")
    assert fields["正文"].startswith("这是一段足够长的正文")


def test_bilibili_initial_state_wins_over_metadata():
    html = """
    <meta property="og:title" content="分享视频">
    <script>window.__INITIAL_STATE__ = {"videoData":{"title":"访谈 {一}",
    "owner":{"name":"山雨"},"desc":"公开访谈","duration":3672}};
    (function(){})();</script>
    """
    fields = extract("https://www.bilibili.com/video/BV1xx411c7mD/", html,
                     platform=platform_for("https://www.bilibili.com/video/BV1xx411c7mD/")).fields
    assert fields == {"标题": "访谈 {一}", "作者": "山雨", "简介": "公开访谈", "时长": "1:01:12"}


def test_bilibili_malformed_state_falls_back_to_metadata():
    html = """<script>window.__INITIAL_STATE__ = invalid;</script>
    <title>普通标题</title><meta property="og:title" content="视频标题">
    <meta name="author" content="老张"><meta name="description" content="视频简介">
    <meta property="og:video:duration" content="65">"""
    fields = extract("https://www.bilibili.com/video/BV1xx411c7mD/", html,
                     platform=platform_for("https://www.bilibili.com/video/BV1xx411c7mD/")).fields
    assert fields["标题"] == "视频标题"
    assert fields["作者"] == "老张"
    assert fields["简介"] == "视频简介"
    assert fields["时长"] == "01:05"


@pytest.mark.parametrize("html", ["<script>隐藏文字</script>", "<title>登录</title>", ""])
def test_pages_without_readable_content_are_rejected(html):
    assert not extract("https://x.example/", html, platform=GENERIC).readable


def test_extract_does_not_execute_resources():
    html = '<img src="https://tracker.example/pixel"><p>' + "正文" * 20 + "</p>"
    fields = extract("https://x.example/", html, platform=GENERIC).fields
    assert "tracker.example" not in str(fields)


# ----------------------------------------------------------------------
# LinkService：短链、平台、缓存、限额、失败
# ----------------------------------------------------------------------


def make_service(html="<article><p>公开正文段落，足够长到被当作正文。</p></article>", *,
                 mime="text/html", final=None):
    calls: list[str] = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return FetchResult(final or url, mime, html)

    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    return LinkService(LinkConfig(enabled=True), fetch_fn=fetch), calls


def test_service_reads_any_host_and_reports_final_url():
    from rubenbot.linkparse.service import LinkService

    async def fetch(url):
        return FetchResult("https://x.com/someone/status/1", "text/html",
                           '<html><head><title>一条推文</title>'
                           '<meta property="og:description" content="推文正文"></head><body></body></html>')

    from rubenbot.linkparse.settings import LinkConfig

    service = LinkService(LinkConfig(), fetch_fn=fetch)
    result = asyncio.run(service.read("https://t.co/abc"))
    assert result.ok and result.platform == "x"
    assert result.payload() == {
        "链接": "https://x.com/someone/status/1",
        "请求链接": "https://t.co/abc",
        "平台": "X",
        "标题": "一条推文",
        "摘要": "推文正文",
    }


def test_service_does_not_pass_off_site_title_as_x_content():
    """只剩站点名标题的 X / 小红书页面按抓不到处理，不让营销标题冒充内容。"""
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    async def fetch(url):
        return FetchResult("https://x.com/someone/status/1", "text/html",
                           "<html><head><title>X</title></head><body></body></html>")

    service = LinkService(LinkConfig(), fetch_fn=fetch)
    result = asyncio.run(service.read("https://t.co/abc"))
    assert not result.ok and "标题" not in result.payload()


def test_service_marks_login_shell_as_unreadable():
    service, _ = make_service(html="<title>登录</title><meta name='description' content='请登录后查看'>")
    result = asyncio.run(service.read("https://x.com/someone/status/1"))
    assert not result.ok
    assert result.payload()["状态"] == "抓不到"
    assert "原因" in result.payload() and "标题" not in result.payload()


def test_service_failure_degrades_without_raising():
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    async def fetch(_url):
        raise FetchError("正文过大")

    service = LinkService(LinkConfig(), fetch_fn=fetch)
    result = asyncio.run(service.read("https://page.example/big"))
    assert not result.ok and result.error == "正文过大"
    assert result.payload()["状态"] == "抓不到"


def test_service_handles_non_html_response():
    service, _ = make_service(mime="application/pdf")
    result = asyncio.run(service.read("https://page.example/file"))
    assert not result.ok and result.error == "不是网页"


def test_service_cache_and_duplicate_in_flight():
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    calls: list[str] = []
    now = [0.0]

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0)
        return FetchResult(url, "text/html", "<title>页面标题</title>")

    service = LinkService(LinkConfig(cache_ttl_seconds=10), clock=lambda: now[0], fetch_fn=fetch)

    async def run():
        one, two = await asyncio.gather(service.read("https://a.example/1"),
                                        service.read("https://a.example/1"))
        assert one is two and calls == ["https://a.example/1"]
        now[0] = 9
        await service.read("https://a.example/1")
        assert len(calls) == 1
        now[0] = 10
        await service.read("https://a.example/1")
        assert len(calls) == 2

    asyncio.run(run())


def test_service_short_link_expands_and_caches_final_page():
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        return FetchResult("https://www.bilibili.com/video/BV1xx411c7mD/", "text/html",
                           "<title>分享视频</title>")

    service = LinkService(LinkConfig(), fetch_fn=fetch)

    async def run():
        short = await service.read("https://b23.tv/abc")
        final = await service.read("https://www.bilibili.com/video/BV1xx411c7mD/")
        assert short is final

    asyncio.run(run())
    assert calls == ["https://b23.tv/abc"]


def test_service_auto_quota_and_cooldown():
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    calls: list[str] = []
    now = [0.0]
    day = [1]

    async def fetch(url):
        calls.append(url)
        if url.endswith("/fail"):
            raise FetchError("链接抓取超时")
        return FetchResult(url, "text/html", "<title>页面</title>")

    service = LinkService(LinkConfig(mode="auto", cooldown_seconds=5, daily_limit=2),
                          clock=lambda: now[0], today=lambda: day[0], fetch_fn=fetch)
    chat = ("p", "group", "g")

    async def run():
        assert await service.read("https://a.example/1", auto_chat=chat)
        await service.prefetch(_event("https://a.example/1"))  # 缓存命中不占额度
        assert await service.read("https://a.example/2", auto_chat=chat) is None  # 冷却
        now[0] = 5
        failed = await service.read("https://a.example/fail", auto_chat=chat)
        assert failed is not None and not failed.ok  # 失败也计入当日额度
        assert await service.read("https://a.example/3", auto_chat=chat) is None  # 当日额度用尽
        # 跨本地自然日额度清零；冷却仍是时间窗，推进时钟后再读。
        day[0], now[0] = 2, 10
        assert await service.read("https://a.example/3", auto_chat=chat)

    asyncio.run(run())
    assert calls == ["https://a.example/1", "https://a.example/fail", "https://a.example/3"]


def test_service_ssrf_rejects_loopback_and_private_without_connecting():
    """真实 fetch 在建立连接前就拒绝非公网地址；这条路径不会触网。"""
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    service = LinkService(LinkConfig(timeout_seconds=0.2))

    async def run():
        for url in ("http://127.0.0.1/", "http://192.168.1.1/", "http://[::1]/",
                    "http://localhost/"):
            result = await service.read(url)
            assert not result.ok and result.error

    asyncio.run(run())


def test_service_fallback_only_when_page_unreadable_and_never_fakes_success():
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    calls: list[tuple[str, str]] = []

    async def fetch(url):
        return FetchResult(url, "text/html", "<title>登录</title>")

    async def fallback(name, url):
        calls.append((name, url))
        return "工具错误：服务器离线或工具不存在"

    service = LinkService(LinkConfig(fallback_tool="mcp__fetch__read"), fetch_fn=fetch,
                          fallback=fallback)
    result = asyncio.run(service.read("https://x.com/a/status/1"))
    assert calls == [("mcp__fetch__read", "https://x.com/a/status/1")]
    assert not result.ok and result.payload()["状态"] == "抓不到"


def _event(text):
    from rubenbot.adapters.base import MessageEvent

    return MessageEvent(platform="recording", chat_type="group", chat_id="g",
                        sender_id="u", sender_name="u", text=text)


# ----------------------------------------------------------------------
# 平台专用请求提示与登录壳
# ----------------------------------------------------------------------


def test_platform_request_hint_points_bilibili_video_at_mobile_site():
    from rubenbot.linkparse import sites

    target, headers = sites.prepare("https://www.bilibili.com/video/BV1xx411c7mD/?p=2")
    assert target == "https://m.bilibili.com/video/BV1xx411c7mD/?p=2"
    assert headers["User-Agent"] == sites.MOBILE_UA
    # 非视频页、非 B 站都不改地址与请求头
    assert sites.prepare("https://www.bilibili.com/read/cv123") == (
        "https://www.bilibili.com/read/cv123", {})
    assert sites.prepare("https://news.example/a") == ("https://news.example/a", {})


def test_mobile_bilibili_state_is_read():
    """移动站没有 videoData，视频信息在 __INITIAL_STATE__.video.viewInfo 里。"""
    html = """
    <title>字幕君交流场所_哔哩哔哩_bilibili</title>
    <script>window.__INITIAL_STATE__ = {"video":{"viewInfo":{"title":"字幕君交流场所",
    "desc":"简介原文","duration":2055,"owner":{"name":"碧诗"}}}, "route":{}};</script>
    """
    url = "https://m.bilibili.com/video/BV1xx411c7mD"
    fields = extract(url, html, platform=platform_for(url)).fields
    assert fields["标题"] == "字幕君交流场所"
    assert fields["作者"] == "碧诗"
    assert fields["简介"] == "简介原文"
    assert fields["时长"] == "34:15"
    # 视频页不把统计串当摘要、不把页脚法务链接当正文
    assert "摘要" not in fields and "正文" not in fields


@pytest.mark.parametrize(("title", "description"), [
    ("Sina Visitor System", ""),
    ("访客系统", "请登录后查看"),
    ("X", "enable javascript to continue"),
])
def test_login_shells_are_unreadable(title, description):
    html = f'<title>{title}</title><meta name="description" content="{description}">'
    assert not extract("https://weibo.example/a", html, platform=GENERIC).readable


def test_bad_port_does_not_break_normalization():
    """端口不是数字或越界时原样保留，交给抓取层按格式无效处理。"""
    for url in ("https://a.example:abc/x", "https://a.example:99999/x"):
        assert normalize(url) == url
    assert extract_urls("看 https://a.example:abc/x 和 https://ok.example/y") == [
        "https://a.example:abc/x", "https://ok.example/y",
    ]


def test_prefetch_reads_every_link_in_one_message_under_one_window():
    """一条消息里同时有几个链接：都预取，各计一次额度，共用一个冷却窗口。"""
    from rubenbot.linkparse.service import LinkService
    from rubenbot.linkparse.settings import LinkConfig

    calls: list[str] = []
    now = [0.0]

    async def fetch(url):
        calls.append(url)
        return FetchResult(url, "text/html", "<title>页面</title>")

    service = LinkService(LinkConfig(mode="auto", cooldown_seconds=60, daily_limit=2),
                          clock=lambda: now[0], fetch_fn=fetch)
    text = "https://a.example/1 https://b.example/2 https://c.example/3"
    asyncio.run(service.prefetch(_event(text)))
    # 额度 2：只抓前两个；第三个留给下一条消息
    assert calls == ["https://a.example/1", "https://b.example/2"]
    # 同一会话冷却中：再发也不抓
    asyncio.run(service.prefetch(_event("https://d.example/4")))
    assert calls == ["https://a.example/1", "https://b.example/2"]
    # 冷却过后额度已用尽，也不抓
    now[0] = 60
    asyncio.run(service.prefetch(_event("https://d.example/4")))
    assert calls == ["https://a.example/1", "https://b.example/2"]


def test_shell_platform_keeps_real_metadata():
    """X 这类平台只要拿到正文/摘要就照常输出，不只认标题。"""
    html = """
    <title>X 上的 jack：“just setting up my twttr” / X</title>
    <meta property="og:title" content="X 上的 jack (@jack)">
    <meta property="og:description" content="just setting up my twttr">
    <meta name="twitter:creator" content="@jack">
    """
    url = "https://x.com/jack/status/20"
    fields = extract(url, html, platform=platform_for(url)).fields
    assert fields["摘要"] == "just setting up my twttr"
    assert fields["作者"] == "@jack"
