"""QQ 空间互动能力：读说说/点赞/评论/回复。

HTTP 层全部用 ``httpx.MockTransport`` 假掉，断言能力拼出的请求（URL、g_tk、
参数）与返回解析；并覆盖 get_cookies 失败、空间返回未登录、开关关闭等路径。
"""

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

from rubenbot.adapters.base import CapabilityError
from rubenbot.adapters.qzone import (
    QzoneClient,
    compute_gtk,
    parse_cookie_string,
)
from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.core import RubenBot
from rubenbot.persona import Persona

from conftest import ScriptedLLM

# 样本 Cookie：p_skey=xyz789 -> g_tk=681278680（hash33）
COOKIE = "uin=o10000; skey=abc123; p_skey=xyz789; qqmusic_key=mk"

MSGLIST_SAMPLE = {
    "code": 0,
    "msglist": [
        {
            "tid": "tid-1",
            "uin": 20001,
            "name": "阿花",
            "content": "今天去爬山了 <br>风景不错 [em]e100[/em]",
            "created_time": 1700000000,
            "isliked": 1,
            "pic": [{"url2": "http://img/1.jpg"}, {"url2": "http://img/2.jpg"}],
            "commentnum": 2,
            "commentlist": [
                {
                    "commentid": "c-1",
                    "uin": 30003,
                    "name": "小明",
                    "content": "羡慕",
                    "create_time": 1700000100,
                    "list_3": [
                        {
                            "commentid": "c-1-1",
                            "uin": 10000,
                            "name": "我",
                            "content": "下次一起",
                            "create_time": 1700000200,
                        }
                    ],
                },
                {
                    "commentid": "c-2",
                    "uin": 40004,
                    "name": "小红",
                    "content": "照片呢",
                    "create_time": 1700000300,
                },
            ],
        },
        {
            "tid": "tid-2",
            "uin": 10000,
            "name": "我",
            "content": "晚安",
            "created_time": 1699990000,
            "pic": [],
            "commentlist": [],
        },
    ],
}


class QzoneMock:
    """假协议端 + 假 HTTP 层。

    - ``cookies``：get_cookies 返回的 Cookie（None 表示 get_cookies 失败/空）
    - ``responses``：按 URL 路径前缀匹配的 (状态码, body) 队列
    """

    def __init__(self, *, cookies: str | None = COOKIE):
        self.cookies = cookies
        self.onebot_calls: list[tuple[str, dict]] = []
        self.http_calls: list[httpx.Request] = []
        self.responses: list[tuple[str, int, str]] = []

    # OneBot HTTP 层（get_cookies）
    def onebot_handler(self, request: httpx.Request) -> httpx.Response:
        self.onebot_calls.append((request.url.path, json.loads(request.content or b"{}")))
        if self.cookies is None:
            raise httpx.ConnectError("协议端不可用")
        return httpx.Response(200, json={"status": "ok", "data": {"cookies": self.cookies}})

    # 空间 Web 层
    def qzone_handler(self, request: httpx.Request) -> httpx.Response:
        self.http_calls.append(request)
        for prefix, status, body in self.responses:
            if request.url.path.endswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(200, text="{}")

    def reply(self, suffix: str, body, *, status: int = 200):
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        self.responses.append((suffix, status, text))


def make_adapter_with(mock: QzoneMock) -> SnowLumaAdapter:
    """构造适配器：OneBot 与空间 HTTP 都走 mock。

    - OneBot HTTP（get_cookies）走 mock.onebot_handler
    - 空间 HTTP 层整体替换成 mock.qzone_handler，但 Cookie 仍走适配器自己的
      ``_fetch_qzone_cookie``（get_cookies）链路。
    """
    adapter = SnowLumaAdapter(
        http_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:3000",
            transport=httpx.MockTransport(mock.onebot_handler),
        ),
    )
    adapter._qzone = QzoneClient(
        adapter._fetch_qzone_cookie,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock.qzone_handler)),
    )
    adapter._owns_qzone = True
    return adapter


def make_bot(config, store, adapter, capabilities=None) -> RubenBot:
    if capabilities is not None:
        config.adapters["snowluma"] = {"capabilities": capabilities}
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(adapter)
    return bot


def run(coro_factory, adapter: SnowLumaAdapter):
    async def scenario():
        try:
            return await coro_factory()
        finally:
            await adapter._client.aclose()
            if adapter._qzone is not None:
                await adapter._qzone.aclose()

    return asyncio.run(scenario())


# ----------------------------------------------------------------------
# 底层：g_tk / Cookie 解析
# ----------------------------------------------------------------------


def test_compute_gtk_matches_hash33():
    assert compute_gtk("xyz789") == 681278680
    assert compute_gtk("@abc123") == 733459617
    assert compute_gtk("") == 5381


def test_parse_cookie_string():
    cookies = parse_cookie_string("uin=o10000; p_skey=xyz789; a=1")
    assert cookies == {"uin": "o10000", "p_skey": "xyz789", "a": "1"}
    assert parse_cookie_string("") == {}


# ----------------------------------------------------------------------
# qzone.feeds
# ----------------------------------------------------------------------


def test_feeds_request_and_parse(config, store):
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=5),
        adapter,
    )
    assert ok is True, reason

    # get_cookies 先按 qzone.qq.com 要
    assert mock.onebot_calls == [("/get_cookies", {"domain": "qzone.qq.com"})]

    request = mock.http_calls[0]
    url = urlparse(str(request.url))
    assert url.hostname == "user.qzone.qq.com"
    assert url.path.endswith("/cgi-bin/emotion_cgi_msglist_v6")
    query = parse_qs(url.query)
    assert query["g_tk"] == ["681278680"]
    assert query["uin"] == ["20001"]
    assert query["hostUin"] == ["20001"]
    assert query["num"] == ["5"]
    assert query["pos"] == ["0"]
    assert query["format"] == ["json"]
    # Cookie 透传登录态
    assert "p_skey=xyz789" in request.headers["Cookie"]


def test_feeds_returns_structured_items():
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            items = await adapter._qzone.feeds(20001, num=5)
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()
        return items

    items = asyncio.run(scenario())
    assert items[0] == {
        "tid": "tid-1",
        "uin": "20001",
        "nickname": "阿花",
        "content": "今天去爬山了 风景不错",  # HTML/表情已清理，连续空格压成一个
        "created_time": 1700000000,
        "image_count": 2,
        "comment_count": 2,
        "is_liked": True,
    }
    # 未标 isliked 的说说：None 表示“不知道”，而不是“未赞”
    assert items[1]["is_liked"] is None


def test_feeds_jsonp_response_is_parsed():
    """msglist 接口常态是 _preloadCallback({...}); 的 JSONP。"""
    body = "_preloadCallback(" + json.dumps(MSGLIST_SAMPLE, ensure_ascii=False) + ");"
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", body)
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.feeds(None, num=1)
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    items = asyncio.run(scenario())
    assert [item["tid"] for item in items] == ["tid-1", "tid-2"]
    # 不传 uin 时读自己
    query = parse_qs(urlparse(str(mock.http_calls[0].url)).query)
    assert query["uin"] == ["10000"]


def test_feeds_unwraps_data_envelope():
    """部分 CGI 形如 {"code":0, "data": {"msglist": [...]}}。"""
    mock = QzoneMock()
    mock.reply(
        "emotion_cgi_msglist_v6",
        {"code": 0, "data": {"msglist": MSGLIST_SAMPLE["msglist"]}},
    )
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.feeds(20001, num=1)
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    items = asyncio.run(scenario())
    assert [item["tid"] for item in items] == ["tid-1", "tid-2"]


def test_feeds_login_expired_inside_data_envelope():
    mock = QzoneMock()
    mock.reply(
        "emotion_cgi_msglist_v6",
        {"code": 0, "data": {"code": -3000, "message": "请先登录"}},
    )
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.feeds(20001, num=1)
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    try:
        asyncio.run(scenario())
    except CapabilityError as exc:
        assert "登录态" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当失败")


# ----------------------------------------------------------------------
# qzone.like / qzone.comment
# ----------------------------------------------------------------------


def test_like_request_params(config, store):
    mock = QzoneMock()
    mock.reply("internal_dolike_app", {"code": 0})
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"like": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.like", uin=20001, tid="tid-1"),
        adapter,
    )
    assert ok is True, reason
    request = mock.http_calls[0]
    url = urlparse(str(request.url))
    assert "internal_dolike_app" in url.path
    assert parse_qs(url.query)["g_tk"] == ["681278680"]
    form = parse_qs(request.content.decode())
    assert form["opuin"] == ["10000"]
    assert form["fid"] == ["tid-1"]
    assert form["appid"] == ["311"]
    # unikey/curkey 必须 http:// 前缀（https 会“假成功”）
    assert form["unikey"] == ["http://user.qzone.qq.com/20001/mood/tid-1"]
    assert form["curkey"] == ["http://user.qzone.qq.com/20001/mood/tid-1"]
    assert form["qzreferrer"] == ["https://user.qzone.qq.com/10000"]


def test_comment_request_params(config, store):
    mock = QzoneMock()
    mock.reply("emotion_cgi_re_feeds", {"code": 0, "commentid": "c-new"})
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"comment": True})

    ok, reason = run(
        lambda: bot.call_capability(
            "snowluma", "qzone.comment", uin=20001, tid="tid-1", content="拍得真好"
        ),
        adapter,
    )
    assert ok is True, reason
    request = mock.http_calls[0]
    assert request.url.path.endswith("/cgi-bin/emotion_cgi_re_feeds")
    assert parse_qs(urlparse(str(request.url)).query)["g_tk"] == ["681278680"]
    form = parse_qs(request.content.decode())
    assert form["topicId"] == ["20001_tid-1__1"]
    assert form["uin"] == ["10000"]
    assert form["hostUin"] == ["20001"]
    assert form["content"] == ["拍得真好"]
    assert form["format"] == ["fs"]


def test_comment_requires_content(config, store):
    mock = QzoneMock()
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"comment": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.comment", uin=20001, tid="tid-1", content="  "),
        adapter,
    )
    assert ok is False
    assert "content" in reason
    assert mock.http_calls == []
    assert mock.onebot_calls == []


# ----------------------------------------------------------------------
# qzone.my_comments / qzone.reply
# ----------------------------------------------------------------------


def test_my_comments_filters_self():
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.my_comments(num=10)
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    comments = asyncio.run(scenario())
    ids = [item["comment_id"] for item in comments]
    # 自己发的回复 c-1-1 不算“收到的评论”
    assert ids == ["c-1", "c-2"]
    assert comments[0] == {
        "tid": "tid-1",
        "feed_uin": "20001",
        "comment_id": "c-1",
        "comment_uin": "30003",
        "nickname": "小明",
        "content": "羡慕",
        "created_time": 1700000100,
        "parent_id": None,
    }
    # 读自己的说说列表
    query = parse_qs(urlparse(str(mock.http_calls[0].url)).query)
    assert query["uin"] == ["10000"]


def test_reply_uses_pc_proxy_form(config, store):
    mock = QzoneMock()
    mock.reply("emotion_cgi_re_feeds", {"code": 0})
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"reply": True})

    ok, reason = run(
        lambda: bot.call_capability(
            "snowluma",
            "qzone.reply",
            uin=10000,
            tid="tid-3",
            comment_id="c-9",
            comment_uin=50005,
            comment_nickname="老王",
            content="我也这么觉得",
        ),
        adapter,
    )
    assert ok is True, reason
    request = mock.http_calls[0]
    form = parse_qs(request.content.decode())
    # PC 代理：topicId 不带 __1、format=json
    assert form["topicId"] == ["10000_tid-3"]
    assert form["format"] == ["json"]
    assert form["commentId"] == ["c-9"]
    assert form["commentUin"] == ["50005"]
    assert form["replyUin"] == ["50005"]
    # 正文前加 @ 提及标记，与网页版一致
    assert form["content"] == ["@{uin:50005,nick:老王,auto:1} 我也这么觉得"]


def test_reply_looks_up_comment_author_when_missing():
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    mock.reply("emotion_cgi_re_feeds", {"code": 0})
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.reply(20001, "tid-1", "c-2", "哈哈")
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    result = asyncio.run(scenario())
    assert result["reply_to"] == "40004"
    assert [urlparse(str(r.url)).path.rsplit("/", 1)[-1] for r in mock.http_calls] == [
        "emotion_cgi_msglist_v6",
        "emotion_cgi_re_feeds",
    ]
    form = parse_qs(mock.http_calls[1].content.decode())
    assert form["commentUin"] == ["40004"]
    assert form["content"] == ["@{uin:40004,nick:小红,auto:1} 哈哈"]


def test_reply_unknown_comment_fails_without_write():
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    adapter = make_adapter_with(mock)

    async def scenario():
        try:
            return await adapter._qzone.reply(20001, "tid-1", "c-missing", "hi")
        finally:
            await adapter._client.aclose()
            await adapter._qzone.aclose()

    try:
        asyncio.run(scenario())
    except CapabilityError as exc:
        assert "comment_uin" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当失败")
    # 只有读请求，没有写请求
    assert len(mock.http_calls) == 1


# ----------------------------------------------------------------------
# 登录态失败路径
# ----------------------------------------------------------------------


def test_get_cookies_unavailable_returns_false(config, store):
    """get_cookies 本身抛错（协议端不可用）：返回 (False, 原因)，不发任何空间请求。"""
    mock = QzoneMock()
    adapter = SnowLumaAdapter(
        http_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:3000",
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("协议端连不上"))
            ),
        ),
    )
    adapter._qzone = QzoneClient(
        adapter._fetch_qzone_cookie,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock.qzone_handler)),
    )
    adapter._owns_qzone = True
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "get_cookies" in reason
    assert mock.http_calls == []


def test_get_cookies_returns_empty_cookie(config, store):
    mock = QzoneMock(cookies="")
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "get_cookies" in reason or "登录态" in reason
    # 三个域都试过
    assert [call[1]["domain"] for call in mock.onebot_calls] == [
        "qzone.qq.com",
        "user.qzone.qq.com",
        "qq.com",
    ]
    assert mock.http_calls == []


def test_qzone_login_expired_code_returns_false(config, store):
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", {"code": -3000, "message": "请先登录"})
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "登录态" in reason
    # 失效后登录态缓存被清掉
    assert adapter._qzone._session is None


def test_qzone_http_403_is_login_failure(config, store):
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", "", status=403)
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "登录态" in reason or "403" in reason


def test_qzone_redirect_to_login_detected(config, store):
    mock = QzoneMock()
    mock.responses.append(("emotion_cgi_msglist_v6", 302, ""))
    adapter = make_adapter_with(mock)
    adapter._qzone  # 惰性创建前替换 handler
    mock_redirect = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "https://ptlogin2.qq.com/login?u=1"}
        )
    )
    adapter._qzone = QzoneClient(
        adapter._fetch_qzone_cookie, http_client=httpx.AsyncClient(transport=mock_redirect)
    )
    bot = make_bot(config, store, adapter, capabilities={"feeds": True})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "登录" in reason


# ----------------------------------------------------------------------
# 开关
# ----------------------------------------------------------------------


def test_capabilities_declared():
    assert {
        "qzone.feeds",
        "qzone.like",
        "qzone.comment",
        "qzone.my_comments",
        "qzone.reply",
    } <= SnowLumaAdapter.capabilities


def test_interaction_capabilities_disabled_by_default(config, store):
    """只开 emoji_like：所有空间互动能力都应被开关挡住，且不发任何请求。"""
    mock = QzoneMock()
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"emoji_like": True})

    async def scenario():
        results = {}
        for name, kwargs in [
            ("qzone.like", {"uin": 1, "tid": "t"}),
            ("qzone.comment", {"uin": 1, "tid": "t", "content": "x"}),
            ("qzone.reply", {"uin": 1, "tid": "t", "comment_id": "c", "content": "x"}),
        ]:
            results[name] = await bot.call_capability("snowluma", name, **kwargs)
        await adapter._client.aclose()
        await adapter._qzone.aclose()
        return results

    results = asyncio.run(scenario())
    for name, (ok, reason) in results.items():
        assert ok is False, name
        assert "关闭" in reason, (name, reason)
    assert mock.onebot_calls == []
    assert mock.http_calls == []


def test_feeds_enabled_by_default(config, store):
    """feeds/my_comments 只读，未写开关时默认开启。"""
    mock = QzoneMock()
    mock.reply("emotion_cgi_msglist_v6", MSGLIST_SAMPLE)
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"emoji_like": True})

    ok, _ = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is True
    assert len(mock.http_calls) == 1


def test_feeds_can_be_disabled(config, store):
    mock = QzoneMock()
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"feeds": False})

    ok, reason = run(
        lambda: bot.call_capability("snowluma", "qzone.feeds", uin=20001, num=1),
        adapter,
    )
    assert ok is False
    assert "关闭" in reason
    assert mock.onebot_calls == [] and mock.http_calls == []


def test_switches_are_independent(config, store):
    """开了 like 不等于允许 comment/reply。"""
    mock = QzoneMock()
    mock.reply("internal_dolike_app", {"code": 0})
    adapter = make_adapter_with(mock)
    bot = make_bot(config, store, adapter, capabilities={"like": True})

    async def scenario():
        liked = await bot.call_capability("snowluma", "qzone.like", uin=20001, tid="t1")
        commented = await bot.call_capability(
            "snowluma", "qzone.comment", uin=20001, tid="t1", content="x"
        )
        await adapter._client.aclose()
        await adapter._qzone.aclose()
        return liked, commented

    liked, commented = asyncio.run(scenario())
    assert liked[0] is True
    assert commented[0] is False and "关闭" in commented[1]
    assert len(mock.http_calls) == 1
