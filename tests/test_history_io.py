"""结构化历史与 IO 基础：消息 id 落库、渲染、窗口计数、入站去重、出站回执。

灵感来源：MaiBot 研读 #1（结构化历史）与 #17（IO 层），是本项目里的最小闭环。
"""

import asyncio
import re
import sqlite3
import time

from rubenbot.adapters.base import BaseAdapter, MessageEvent, Segment
from rubenbot.core import BOT_SENDER_ID, RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import ScriptedLLM

CLOCK_RE = r"\[\d{2}:\d{2}\]"


def make_bot(config, store, llm, adapter=None, rng=None) -> tuple[RubenBot, BaseAdapter]:
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"), rng=rng)
    adapter = adapter or EchoIdAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


class EchoIdAdapter(BaseAdapter):
    """回执带 id 的假适配器：发出的第 n 条返回 ``sent-n``。"""

    name = "recording"

    def __init__(self):
        self.sent: list[tuple[str, str, list[Segment]]] = []

    async def start(self, on_event):
        self.on_event = on_event

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.sent.append((chat_id, chat_type, segments))
        return f"sent-{len(self.sent)}"

    async def stop(self):
        pass

    @property
    def texts(self) -> list[str]:
        return ["".join(s.render() for s in segs) for _, _, segs in self.sent]


class NullIdAdapter(EchoIdAdapter):
    """平台拿不到消息 id：send 返回 None。"""

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.sent.append((chat_id, chat_type, segments))
        return None


def event(
    text="你好",
    *,
    chat_id="g1",
    sender="u1",
    name="阿花",
    msg_id=None,
    reply_to=None,
    chat_type="group",
    mentions=False,
) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id=sender,
        sender_name=name,
        text=text,
        reply_to_id=reply_to,
        mentions_bot=mentions,
        platform_msg_id=msg_id,
    )


# ----------------------------------------------------------------------
# 旧库迁移
# ----------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE raw_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_type   TEXT NOT NULL DEFAULT 'group',
    sender_id   TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    ts          REAL NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_raw_chat ON raw_messages(chat_id, ts);
"""


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO raw_messages(platform, chat_id, chat_type, sender_id, sender_name, text, ts, processed)"
        " VALUES('console', 'g1', 'group', 'u1', '阿花', '老消息', 10.0, 0)"
    )
    conn.commit()
    conn.close()

    store = MemoryStore(path)
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(raw_messages)")}
        assert {"platform_msg_id", "reply_to_id"} <= columns

        # 旧数据可读，新列回落空串
        old = store.recent_messages("g1")[0]
        assert old.text == "老消息"
        assert old.platform_msg_id == ""
        assert old.reply_to_id == old.reply_sender_id == ""
        assert old.mentions == {}

        # 迁移后仍能正常写入新列、按 id 去重查询
        store.add_raw_message(
            platform="console",
            chat_id="g1",
            sender_id="u1",
            text="新消息",
            platform_msg_id="m1",
            reply_to_id="m0",
        )
        assert store.platform_message_exists("console", "m1")
        found = store.message_by_platform_id("console", "m1")
        assert found is not None and found.reply_to_id == "m0"
        # 迁移建好的索引存在（旧库也补上了）
        indexes = {row[1] for row in store._conn.execute("PRAGMA index_list(raw_messages)")}
        assert "idx_raw_platform_msg" in indexes
    finally:
        store.close()

    # 再开一次是幂等的
    MemoryStore(path).close()


def test_fresh_database_has_new_columns(store):
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(raw_messages)")}
    assert {"platform_msg_id", "reply_to_id"} <= columns


# ----------------------------------------------------------------------
# 结构化历史渲染
# ----------------------------------------------------------------------


def _add(store, text, *, sender="u1", name="阿花", msg_id=None, reply_to="", ts=100.0, bot=False,
         chat_id="g1", chat_type="group"):
    return store.add_raw_message(
        platform="recording",
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id=BOT_SENDER_ID if bot else sender,
        sender_name="Ruben" if bot else name,
        text=text,
        ts=ts,
        processed=bot,
        platform_msg_id=msg_id or "",
        reply_to_id=reply_to or "",
    )


def test_render_history_bot_content_is_plain(config, store):
    """bot 自己的历史只有干净正文，不带转录前缀（否则模型会模仿前缀）。"""
    _add(store, "今天下雨了", sender="u1", name="阿花", msg_id="m1", ts=100.0)
    _add(store, "是啊", bot=True, msg_id="m2", ts=160.0)
    bot, _ = make_bot(config, store, ScriptedLLM())
    messages = store.recent_messages("g1", platform="recording")
    rendered = bot.render_history(messages)

    assert rendered[0]["role"] == "user"
    assert re.fullmatch(CLOCK_RE + r" \[阿花\] 今天下雨了", rendered[0]["content"])
    assert rendered[1]["role"] == "assistant"
    assert rendered[1]["content"] == "是啊"


def test_render_history_bot_keeps_quote_suffix(config, store):
    """bot 历史不带前缀，但引用附注仍照旧追加在正文后。"""
    _add(store, "今天下雨了", sender="u1", name="阿花", msg_id="m1", ts=100.0)
    _add(store, "是啊", bot=True, msg_id="m2", reply_to="m1", ts=160.0)
    bot, _ = make_bot(config, store, ScriptedLLM())
    rendered = bot.render_history(store.recent_messages("g1", platform="recording"))

    assert rendered[1]["content"] == "是啊（引用 [阿花]：今天下雨了）"


def test_render_history_quote_resolved_in_batch(config, store):
    _add(store, "这家店排队好久", sender="u2", name="阿草", msg_id="m1", ts=100.0)
    _add(store, "你说的是哪家", sender="u1", name="阿花", msg_id="m2", reply_to="m1", ts=160.0)
    bot, _ = make_bot(config, store, ScriptedLLM())
    rendered = bot.render_history(store.recent_messages("g1", platform="recording"))

    assert rendered[1]["content"].endswith("（引用 [阿草]：这家店排队好久）")


def test_render_history_quote_truncated_to_50(config, store):
    long_text = "长" * 80
    _add(store, long_text, sender="u2", name="阿草", msg_id="m1", ts=100.0)
    _add(store, "回你", sender="u1", name="阿花", msg_id="m2", reply_to="m1", ts=160.0)
    bot, _ = make_bot(config, store, ScriptedLLM())
    rendered = bot.render_history(store.recent_messages("g1", platform="recording"))

    suffix = rendered[1]["content"].split("（引用 [阿草]：", 1)[1]
    snippet = suffix.rstrip("）")
    assert snippet.endswith("…")
    assert len(snippet) == 51  # 50 字 + 省略号


def test_render_history_quote_fallback_when_target_missing(config, store):
    _add(store, "回一条不在记录里的消息", sender="u1", name="阿花", msg_id="m2", reply_to="nope", ts=160.0)
    bot, _ = make_bot(config, store, ScriptedLLM())
    rendered = bot.render_history(store.recent_messages("g1", platform="recording"))
    assert rendered[0]["content"].endswith("（引用了一条消息）")


def test_build_context_reply_target_in_requirements(config, store):
    """回复目标的正文截断约 300 字进上下文要求区，不整段重复。"""
    long_text = "起" + "某" * 400
    _add(store, long_text, sender="u2", name="阿草", msg_id="m1", ts=100.0, chat_id="u1", chat_type="private")
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))
    asyncio.run(bot.handle_event(event("你刚说的这个", msg_id="m2", reply_to="m1", chat_type="private", chat_id="u1")))

    system = bot.llm.chat_messages[0][0]["content"]
    assert "上下文要求" in system
    assert "对方引用的那条消息（阿草）" in system
    snippet = system.split("对方引用的那条消息（阿草）：", 1)[1].split("\n", 1)[0]
    assert snippet.endswith("…")
    assert len(snippet) == 301  # 300 字 + 省略号
    assert "某" * 400 not in system


def test_build_context_without_reply_has_no_quote(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))
    asyncio.run(bot.handle_event(event("在吗", msg_id="m1", chat_type="private", chat_id="u1")))
    system = bot.llm.chat_messages[0][0]["content"]
    assert "上下文要求" not in system
    assert "引用" not in system


# ----------------------------------------------------------------------
# 窗口按真实消息计
# ----------------------------------------------------------------------


def test_history_window_excludes_tool_and_system_entries(config, store):
    _add(store, "第一条", sender="u1", name="阿花", msg_id="m1", ts=100.0)
    _add(store, "系统提示", sender="__system__", name="系统", ts=101.0)
    _add(store, "工具结果", sender="__tool__", name="工具", ts=102.0)
    _add(store, "第二条", sender="u2", name="阿草", msg_id="m2", ts=103.0)
    _add(store, "bot 的话", bot=True, msg_id="m3", ts=104.0)

    real = store.recent_messages("g1", platform="recording", limit=2, real_only=True)
    assert [m.text for m in real] == ["第二条", "bot 的话"]
    # bot 自己的消息算真实消息，不受影响
    assert real[-1].sender_id == BOT_SENDER_ID

    everything = store.recent_messages("g1", platform="recording", limit=10)
    assert len(everything) == 5


def test_build_context_uses_real_message_window(config, store):
    """窗口条数按真实消息计：系统/工具条目不会挤掉真实历史。"""
    config.chat.history_size = 3
    now = time.time()
    _add(store, "旧的真实消息", sender="u2", name="阿草", msg_id="m0", ts=now - 30)
    _add(store, "系统一", sender="__system__", name="系统", ts=now - 20)
    _add(store, "系统二", sender="__system__", name="系统", ts=now - 10)
    _add(store, "最近的真实消息", sender="u3", name="阿草", msg_id="m1", ts=now - 5)

    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))
    asyncio.run(bot.handle_event(event("在吗", msg_id="m2", chat_type="group", chat_id="g1", mentions=True)))

    messages = bot.llm.chat_messages[0]
    contents = [m["content"] for m in messages]
    assert any("旧的真实消息" in c for c in contents)
    assert any("最近的真实消息" in c for c in contents)
    assert not any("系统一" in c or "系统二" in c for c in contents)


# ----------------------------------------------------------------------
# 入站去重
# ----------------------------------------------------------------------


def test_duplicate_event_is_skipped(config, store):
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    first = event("在吗", msg_id="dup-1", chat_type="private", chat_id="u1")
    asyncio.run(bot.handle_event(first))
    asyncio.run(bot.handle_event(first))

    assert adapter.texts == ["在的"]
    assert len(llm.chat_messages) == 1
    # 流水里只有一条入站消息（加上 bot 的回复）
    inbound = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id != BOT_SENDER_ID]
    assert [m.text for m in inbound] == ["在吗"]


def test_duplicate_checks_ttl(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM())
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="u1",
        text="原始",
        ts=100.0,
        platform_msg_id="dup-ttl",
    )
    target = event("重复", msg_id="dup-ttl")
    ttl = config.chat.dedupe_ttl_seconds
    assert bot._is_duplicate(target, now=100.0 + ttl - 1)
    assert not bot._is_duplicate(target, now=100.0 + ttl + 1)


def test_no_platform_id_never_deduplicated(config, store):
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["嗯", "嗯"]}))
    asyncio.run(bot.handle_event(event("在吗", msg_id=None, chat_type="private", chat_id="u1")))
    asyncio.run(bot.handle_event(event("在吗", msg_id=None, chat_type="private", chat_id="u1")))
    assert len(adapter.texts) == 2
    inbound = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id != BOT_SENDER_ID]
    assert len(inbound) == 2


def test_dedupe_can_be_disabled(config, store):
    config.chat.dedupe_ttl_seconds = 0
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["嗯", "嗯"]}))
    same = event("在吗", msg_id="dup-off", chat_type="private", chat_id="u1")
    asyncio.run(bot.handle_event(same))
    asyncio.run(bot.handle_event(same))
    assert len(adapter.texts) == 2


# ----------------------------------------------------------------------
# 出站回执
# ----------------------------------------------------------------------


def test_bot_message_id_is_stored(config, store):
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["在的"]}))
    asyncio.run(bot.handle_event(event("在吗", msg_id="in-1", chat_type="private", chat_id="u1")))

    bot_messages = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id == BOT_SENDER_ID]
    assert len(bot_messages) == 1
    assert bot_messages[0].text == "在的"
    assert bot_messages[0].platform_msg_id == "sent-1"


def test_send_returning_none_still_records_text(config, store):
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["在的"]}), adapter=NullIdAdapter())
    asyncio.run(bot.handle_event(event("在吗", msg_id="in-1", chat_type="private", chat_id="u1")))

    bot_messages = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id == BOT_SENDER_ID]
    assert len(bot_messages) == 1
    assert bot_messages[0].platform_msg_id == ""


def test_bot_reply_can_be_quoted_later(config, store):
    """bot 回执 id 落库后，后续消息引用它时能在历史里解析出来。"""
    _add(store, "在吗", sender="u1", name="阿花", msg_id="in-1", ts=time.time() - 10)
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["在的"]}))
    asyncio.run(bot.handle_event(event("在吗", msg_id="in-2", chat_type="private", chat_id="u1")))
    bot_msg = next(m for m in store.recent_messages("u1", platform="recording") if m.sender_id == BOT_SENDER_ID)
    assert bot_msg.platform_msg_id

    # 用户引用 bot 的回复
    _add(
        store,
        "刚才那是什么意思",
        sender="u1",
        name="阿花",
        msg_id="in-3",
        reply_to=bot_msg.platform_msg_id,
        ts=time.time() + 1,
        chat_id="u1",
    )
    history = store.recent_messages("u1", platform="recording")
    rendered = bot.render_history(history)
    last = rendered[-1]["content"]
    assert last.endswith("（引用 [我]：在的）")
