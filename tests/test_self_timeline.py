"""自我时间线：bot 发出的每条消息在发送成功后记一条 self 事件。"""

import asyncio

from rubenbot.adapters.base import MessageEvent

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.core import RubenBot
from rubenbot.persona import Persona


def make_bot(config, store, llm, adapter=None):
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"))
    bot.register_adapter(adapter or RecordingAdapter())
    return bot


def event(text="在吗", *, chat_id="u1", chat_type="private") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def test_self_event_written_after_send(config, store):
    bot = make_bot(config, store, ScriptedLLM({"chat": ["在的呀"]}))
    asyncio.run(bot.handle_event(event()))

    events = store.list_events(person_id="self")
    assert len(events) == 1
    assert events[0].summary == "在的呀"
    assert events[0].source_chat == "recording:u1"
    assert events[0].audience == "private"
    assert events[0].kind == "event"


def test_group_self_event_audience(config, store):
    bot = make_bot(config, store, ScriptedLLM({"chat": ["大家好"]}))
    asyncio.run(bot.handle_event(event("Ruben 在吗", chat_id="g1", chat_type="group")))

    events = store.list_events(person_id="self")
    assert len(events) == 1
    assert events[0].audience == "group:g1"
    assert events[0].source_chat == "recording:g1"


def test_self_event_summary_is_truncated(config, store):
    config.reply.max_segments = 1
    config.memory.self_event_max_chars = 20
    long_reply = "这是一句非常长的话" * 10
    bot = make_bot(config, store, ScriptedLLM({"chat": [long_reply]}))
    asyncio.run(bot.handle_event(event()))

    events = store.list_events(person_id="self")
    assert len(events) == 1
    assert len(events[0].summary) == 20


def test_failed_send_writes_no_self_event(config, store):
    from rubenbot.adapters.base import BaseAdapter

    class BoomAdapter(BaseAdapter):
        name = "recording"

        async def start(self, on_event):
            self.on_event = on_event

        async def send(self, chat_id, segments, *, chat_type="group"):
            raise RuntimeError("网络断了")

        async def stop(self):
            pass

    bot = make_bot(config, store, ScriptedLLM({"chat": ["在的"]}), adapter=BoomAdapter())
    asyncio.run(bot.handle_event(event()))
    assert store.list_events(person_id="self") == []


def test_self_event_failure_does_not_break_send(config, store):
    bot = make_bot(config, store, ScriptedLLM({"chat": ["在的"]}))

    def boom(*args, **kwargs):
        raise RuntimeError("自我时间线写坏了")

    store.add_self_event = boom  # type: ignore[method-assign]
    asyncio.run(bot.handle_event(event()))
    # 回复照常落流水
    bot_messages = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id == "__bot__"]
    assert [m.text for m in bot_messages] == ["在的"]
