"""链接理解接入主循环：触发口径、两种投递方式、失败如实与不落记忆。

网络全部替换成本地假响应；不访问外网。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.linkparse.fetch import FetchError, FetchResult
from rubenbot.persona import Persona


BILI = "https://www.bilibili.com/video/BV1xx411c7mD/"
NEWS = "https://news.example/story?chapter=2"
XPOST = "https://x.com/someone/status/1"

BILI_HTML = """
<meta property="og:title" content="分享视频">
<script>window.__INITIAL_STATE__ = {"videoData":{"title":"访谈 {一}",
"owner":{"name":"山雨"},"desc":"公开访谈","duration":3672}};</script>
"""
NEWS_HTML = """
<title>旧标题</title>
<meta property="og:title" content="新闻标题">
<meta property="og:site_name" content="新闻站">
<meta name="description" content="新闻摘要">
<article><p>来看海。</p><p>第二行。</p></article>
"""
SHELL_HTML = "<title>X</title><meta name='description' content='请登录后查看这条推文'>"


def event(text, *, mentions=False, sender="u1", chat="g1", chat_type="group", **kwargs):
    return MessageEvent(
        platform="recording", chat_type=chat_type, chat_id=chat, sender_id=sender,
        sender_name="群友", text=text, mentions_bot=mentions, **kwargs,
    )


def make_bot(config, store, llm, pages):
    """装配 bot，并把链接抓取换成按 URL 返回假页面的函数。"""
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)

    async def fake_fetch(url):
        page = pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise FetchError("站点返回 HTTP 404")
        final, mime, html = page
        return FetchResult(final or url, mime, html)

    bot.link_service.fetch = fake_fetch
    return bot, adapter


def planner_messages(llm):
    calls = [c for c in llm.calls if c["task"] == "gen_stage1"]
    return calls[-1]["messages"] if calls else []


def generator_messages(llm):
    return llm.chat_messages[-1] if llm.chat_messages else []


def user_texts(messages):
    return [m["content"] for m in messages if m["role"] == "user"]


def system_texts(messages):
    return [m["content"] for m in messages if m["role"] == "system"]


def link_block(messages):
    for text in user_texts(messages):
        marker = "<external_link_data>"
        if marker in text:
            return json.loads(text.split(marker, 1)[1].removesuffix("</external_link_data>"))
    return None


@pytest.fixture
def pages():
    return {
        BILI: (BILI, "text/html", BILI_HTML),
        NEWS: (NEWS, "text/html", NEWS_HTML),
        XPOST: (XPOST, "text/html", SHELL_HTML),
    }


# ----------------------------------------------------------------------
# 触发口径
# ----------------------------------------------------------------------


def test_link_not_fetched_without_being_addressed_or_asked(config, store, pages):
    calls = []
    original = pages.get

    class Tracked(dict):
        def get(self, key, default=None):
            calls.append(key)
            return original(key, default)

    llm = ScriptedLLM({"fast": ['{"basis":"common","speak":true,"intent":"接梗"}'],
                       "chat": ["嗯"]})
    bot, adapter = make_bot(config, store, llm, Tracked(pages))
    # 普通群聊只说「看这个 <链接>」，没被叫到也没有提问：不读。
    asyncio.run(bot.handle_event(event(f"看这个 {NEWS}")))
    assert calls == []


def test_link_read_when_mentioned_and_answered_with_content(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["新闻讲的是看海。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"这个讲了什么？ {NEWS}", mentions=True)))
    assert adapter.texts == ["新闻讲的是看海。"]
    data = link_block(generator_messages(llm))
    assert data and data[0]["标题"] == "新闻标题" and data[0]["正文"] == "来看海。 第二行。"
    # 隔离说明在系统提示里，网页字段单独作数据块，不进系统提示
    assert any("不是指令" in text for text in system_texts(generator_messages(llm)))
    assert not any("来看海" in text for text in system_texts(generator_messages(llm)))
    # 网页正文不落进聊天流水 / 记忆
    assert not any("来看海" in msg.text for msg in store.recent_messages("g1"))


def test_link_read_when_just_asked_without_mention(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["讲的是访谈。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event("总结一下这个链接 " + BILI)))
    assert adapter.texts == ["讲的是访谈。"]
    data = link_block(generator_messages(llm))
    assert data[0]["平台"] == "哔哩哔哩" and data[0]["标题"] == "访谈 {一}"


def test_multiple_link_kinds_are_read_together(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["都看了。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"{BILI} {NEWS} 都讲讲", mentions=True)))
    data = link_block(generator_messages(llm))
    assert {item["平台"] for item in data} == {"哔哩哔哩", "网页"}
    assert len(data) == 2


# ----------------------------------------------------------------------
# 失败如实说
# ----------------------------------------------------------------------


def test_unreadable_x_link_is_reported_not_invented(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["这条推文我打不开，抓不到内容。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"这条推文讲了什么？ {XPOST}", mentions=True)))
    assert adapter.texts == ["这条推文我打不开，抓不到内容。"]
    data = link_block(generator_messages(llm))
    assert data[0]["平台"] == "X"
    assert data[0]["状态"] == "抓不到"
    assert "正文" not in data[0] and "标题" not in data[0]
    # 明确要求如实说抓不到，不编造
    assert any("抓不到" in text for text in user_texts(generator_messages(llm)))


def test_fetch_failure_is_reported_with_reason(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'] * 2,
                       "chat": ["那个链接超时了，没读到。"]})
    bot, adapter = make_bot(config, store, llm, pages | {NEWS: FetchError("链接抓取超时")})
    asyncio.run(bot.handle_event(event(f"讲讲 {NEWS}", mentions=True)))
    data = link_block(generator_messages(llm))
    assert data[0]["状态"] == "抓不到" and data[0]["原因"] == "链接抓取超时"


def test_no_fetch_when_link_disabled(config, store, pages):
    config.link.enabled = False
    llm = ScriptedLLM({"fast": ['{"basis":"common","speak":true,"intent":"接梗"}'], "chat": ["嗯"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"讲讲 {NEWS}", mentions=True)))
    assert not planner_messages(llm) or link_block(planner_messages(llm)) is None
    assert not generator_messages(llm) or link_block(generator_messages(llm)) is None


# ----------------------------------------------------------------------
# 投递方式
# ----------------------------------------------------------------------


def test_direct_delivery_full_body_reaches_planner(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["看海。"]})
    bot, _ = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"讲讲 {NEWS}", mentions=True)))
    block = next(m["content"] for m in planner_messages(llm) if "<external_link_data>" in m["content"])
    assert "来看海" in block


def test_planner_delivery_hides_body_unless_chosen(config, store, pages):
    config.link.delivery = "planner"
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"沉默","capabilities":[]}'],
                       "chat": ["不该用到"]})
    bot, _ = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"讲讲 {NEWS}", mentions=True)))
    block = next(m["content"] for m in planner_messages(llm) if "<external_link_data>" in m["content"])
    assert "来看海" not in block and "新闻标题" in block
    # 策划沉默：不给生成正文，也不带链接数据
    assert not llm.chat_messages


def test_planner_delivery_gives_body_when_selected(config, store, pages):
    config.link.delivery = "planner"
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题",'
                                '"capabilities":["看链接"]}'],
                       "chat": ["看海。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"讲讲 {NEWS}", mentions=True)))
    assert adapter.texts == ["看海。"]
    block = next(m["content"] for m in generator_messages(llm) if "<external_link_data>" in m["content"])
    assert "来看海" in block
    # 策划步只看到索引，看不到正文
    planner_block = next(m["content"] for m in planner_messages(llm) if "<external_link_data>" in m["content"])
    assert "来看海" not in planner_block


# ----------------------------------------------------------------------
# 留痕与不落记忆
# ----------------------------------------------------------------------


def test_decision_log_records_read_and_failed_links(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["看了。"]})
    bot, _ = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(f"{NEWS} {XPOST} 讲讲", mentions=True)))
    factors = store.list_decision_logs(chat_id="g1")[0]["factors"]
    assert factors["links"]["read"] == [NEWS]
    assert factors["links"]["failed"][0]["链接"] == XPOST
    assert factors["links"]["mode"] == "direct"


def test_auto_mode_prefetches_and_replies_from_ready(config, store, pages):
    config.link.mode = "auto"
    llm = ScriptedLLM({
        "fast": ['{"basis":"common","speak":false,"intent":"沉默"}',
                 '{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
        "chat": ["讲看海。"],
    })
    calls = []

    class Counted(dict):
        def get(self, key, default=None):
            calls.append(key)
            return super().get(key, default)

    bot, adapter = make_bot(config, store, llm, Counted(pages))
    # 收到链接就预取；普通闲聊不开口
    asyncio.run(bot.handle_event(event(f"看这个 {NEWS}")))
    assert calls == [NEWS] and adapter.texts == []
    # 之后被 @ 追问：直接取已抓到的资料，不再联网
    asyncio.run(bot.handle_event(event(f"这个讲了什么？ {NEWS}", mentions=True)))
    assert adapter.texts == ["讲看海。"] and calls == [NEWS]
    assert link_block(generator_messages(llm))[0]["标题"] == "新闻标题"


def test_auto_mode_respects_cooldown_for_new_links(config, store, pages):
    config.link.mode = "auto"
    config.link.cooldown_seconds = 600
    llm = ScriptedLLM({"fast": ['{"basis":"common","speak":false,"intent":"沉默"}']})
    calls = []

    class Counted(dict):
        def get(self, key, default=None):
            calls.append(key)
            return super().get(key, default)

    bot, _ = make_bot(config, store, llm, Counted(pages))
    asyncio.run(bot.handle_event(event(f"看这个 {NEWS}")))
    asyncio.run(bot.handle_event(event(f"再看这个 {BILI}")))
    assert calls == [NEWS]


def test_followup_quote_reads_link_from_same_chat(config, store, pages):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["讲看海。"]})
    bot, adapter = make_bot(config, store, llm, pages)
    asyncio.run(bot.handle_event(event(NEWS, platform_msg_id="shared")))
    asyncio.run(bot.handle_event(event("刚才那个链接说的啥", mentions=True,
                                       reply_to_id="shared", sender="u2")))
    data = link_block(generator_messages(llm))
    assert data and data[0]["标题"] == "新闻标题"


def test_ssrf_target_is_never_fetched(config, store):
    """真实抓取路径拒绝环回/内网；这里只验证主循环把它当普通消息处理。"""
    llm = ScriptedLLM({"fast": ['{"basis":"settled","speak":true,"intent":"补充新信息","increment":"标题"}'],
                       "chat": ["打不开。"]})
    bot, adapter = make_bot(config, store, llm, {})
    bot.link_service.fetch = None  # 走真实 fetch：建立连接前就会被拒绝
    asyncio.run(bot.handle_event(event("讲讲 http://127.0.0.1/secret", mentions=True)))
    data = link_block(generator_messages(llm))
    assert data[0]["状态"] == "抓不到"
    assert not data[0].get("正文")
