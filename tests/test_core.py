"""主循环：触发判断、上下文拼装、拆条发送、异常隔离。"""

import asyncio
import json
import random

import pytest

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.core import BOT_SENDER_ID, RubenBot
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


def make_bot(config, store, llm, *, plugins=None, rng=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"), plugins=plugins, rng=rng)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def group_event(text="你好呀", sender="u1", name="阿花", mentions=False) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender,
        sender_name=name,
        text=text,
        mentions_bot=mentions,
    )


def private_event(text="在吗", sender="u1", name="阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender,
        sender_id=sender,
        sender_name=name,
        text=text,
    )


class FixedRng(random.Random):
    def __init__(self, value: float):
        super().__init__()
        self.value = value

    def random(self):
        return self.value


# ----------------------------------------------------------------------
# 触发判断
# ----------------------------------------------------------------------


def test_private_always_replies(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.99))
    assert bot.should_reply(private_event()).should_reply


def test_mention_always_replies(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.99))
    assert bot.should_reply(group_event(mentions=True)).should_reply


def test_group_probability(config, store):
    config.chat.group_talkativeness = 0.08
    low, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.5))
    assert not low.should_reply(group_event()).should_reply
    high, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.01))
    assert high.should_reply(group_event()).should_reply


def test_conversation_boost(config, store):
    config.chat.group_talkativeness = 0.05
    config.chat.group_talkativeness_boost = 0.3
    config.chat.boost_window_seconds = 60
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.2))
    assert not bot.should_reply(group_event(), now=1000.0).should_reply
    bot._last_reply_at["recording:g1"] = 990.0
    assert bot.should_reply(group_event(), now=1000.0).should_reply


# ----------------------------------------------------------------------
# 事件处理全流程
# ----------------------------------------------------------------------


def test_handle_event_records_and_replies(config, store):
    llm = ScriptedLLM({"chat": ["在呢在呢"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("在吗")))

    assert adapter.texts == ["在呢在呢"]
    # 人物与流水都落了
    assert store.get_person("recording:u1").display_name == "阿花"
    msgs = store.recent_messages("u1", platform="recording")
    assert msgs[0].text == "在吗"
    assert msgs[1].sender_id == BOT_SENDER_ID
    assert msgs[1].text == "在呢在呢"
    # bot 的消息不参与抽取
    assert store.pending_senders()[0].sender_id == "u1"


def test_group_silent_does_not_call_llm(config, store):
    config.chat.group_talkativeness = 0.0
    llm = ScriptedLLM()
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(group_event()))
    assert adapter.sent == []
    assert llm.chat_messages == []
    # 但消息仍然进流水
    assert store.recent_messages("g1")[0].text == "你好呀"


def test_context_contains_persona_memories_and_history(config, store):
    store.upsert_person("recording", "u1", "阿花")
    store.add_memory("recording:u1", "TA 最喜欢的歌手是坂本龙一")
    config.memory.inject_limit = 5
    llm = ScriptedLLM({"chat": ["坂本龙一啊，我也喜欢"]})
    bot, _ = make_bot(config, store, llm)
    # 先放一条历史消息
    store.add_raw_message(platform="recording", chat_id="u1", sender_id="u2", sender_name="阿草", text="今天听歌吗")
    asyncio.run(bot.handle_event(private_event("最近在听坂本龙一")))

    messages = llm.chat_messages[0]
    system = messages[0]
    assert system["role"] == "system"
    assert "你是 Ruben" in system["content"]
    assert "坂本龙一" in system["content"]  # 注入的记忆
    assert "阿花" in system["content"]  # 说话人信息
    # 历史消息以 说话人：内容 的形式出现
    assert any(m["content"].startswith("阿草：今天听歌吗") for m in messages)
    assert messages[-1]["content"].startswith("阿花：最近在听坂本龙一")


def test_llm_failure_does_not_crash(config, store):
    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("模型崩了")

    bot, adapter = make_bot(config, store, BoomLLM())
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常
    assert adapter.sent == []


def test_adapter_send_failure_does_not_crash(config, store):
    class BrokenAdapter(RecordingAdapter):
        async def send(self, chat_id, segments, *, chat_type="group"):
            raise RuntimeError("发送失败")

    bot = RubenBot(config, store, ScriptedLLM({"chat": ["a"]}), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(BrokenAdapter())
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常


# ----------------------------------------------------------------------
# 拆条
# ----------------------------------------------------------------------


def test_split_short_reply_kept(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM())
    assert bot.split_reply("好呀") == ["好呀"]


def test_split_long_reply_into_parts(config, store):
    config.chat.split_part_chars = 10
    config.chat.split_max_parts = 3
    bot, _ = make_bot(config, store, ScriptedLLM())
    parts = bot.split_reply("第一句话。第二句话。第三句话。第四句话。第五句话。")
    assert 2 <= len(parts) <= 3
    assert all(parts)
    assert "".join(parts) == "第一句话。第二句话。第三句话。第四句话。第五句话。"


def test_split_respects_max_parts(config, store):
    config.chat.split_part_chars = 5
    config.chat.split_max_parts = 2
    bot, _ = make_bot(config, store, ScriptedLLM())
    parts = bot.split_reply("一。二。三。四。五。六。")
    assert len(parts) == 2


def test_send_reply_sends_parts(config, store):
    config.chat.split_part_chars = 10
    config.chat.split_max_parts = 3
    bot, adapter = make_bot(config, store, ScriptedLLM())
    asyncio.run(bot.send_reply(private_event(), "第一句话。第二句话。第三句话。"))
    assert len(adapter.sent) >= 2
    assert all(chat_type == "private" for _, chat_type, _ in adapter.sent)


# ----------------------------------------------------------------------
# 后台维护
# ----------------------------------------------------------------------


def test_maintenance_extracts_then_uses_memory(config, store):
    """端到端：发消息 → 落流水 → 抽取 → 再发消息时能引用记忆。"""
    extract_payload = json.dumps(
        [{"content": "TA 最喜欢的歌手是坂本龙一", "category": "preference", "scope": "global"}],
        ensure_ascii=False,
    )
    llm = ScriptedLLM({"chat": ["记得，你最喜欢坂本龙一"], "extract": [extract_payload]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("我最喜欢坂本龙一了")))
    assert store.count_memories("recording:u1") == 0

    asyncio.run(bot.run_maintenance(now=1e9, force_extract=True))
    assert store.count_memories("recording:u1") == 1

    asyncio.run(bot.handle_event(private_event("你记得我喜欢谁吗")))
    system = llm.chat_messages[-1][0]["content"]
    assert "坂本龙一" in system


def test_maintenance_error_contained(config, store):
    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("挂")

    bot, _ = make_bot(config, store, BoomLLM())
    store.add_raw_message(platform="recording", chat_id="u1", sender_id="u1", text="x")
    asyncio.run(bot.run_maintenance(force_extract=True))  # 不抛异常


# ----------------------------------------------------------------------
# 适配器运行容错
# ----------------------------------------------------------------------


def test_adapter_crash_does_not_kill_run(config, store, monkeypatch):
    import rubenbot.core as core_module

    monkeypatch.setattr(core_module, "ADAPTER_RESTART_MIN", 0.02)

    class CrashAdapter(RecordingAdapter):
        def __init__(self):
            super().__init__()
            self.starts = 0

        async def start(self, on_event):
            self.starts += 1
            raise RuntimeError("连接失败")

    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    adapter = CrashAdapter()
    bot.register_adapter(adapter)
    config.background_check_minutes = 0.01

    async def scenario():
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.1)
        bot.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert adapter.starts >= 2  # 至少重启过一次


def test_console_adapter_parse_and_send():
    import io

    out = io.StringIO()
    adapter = ConsoleAdapter(stdout=out)
    private = adapter.parse_line("你好")
    assert private.chat_type == "private"
    group = adapter.parse_line("[摸鱼群] 大家好")
    assert group.chat_type == "group"
    assert group.chat_id == "摸鱼群"
    assert group.text == "大家好"
    named = adapter.parse_line("[摸鱼群|阿花] 我来啦")
    assert named.sender_name == "阿花"
    asyncio.run(adapter.send("摸鱼群", [Segment.text("收到")]))
    assert "Ruben: 收到" in out.getvalue()
