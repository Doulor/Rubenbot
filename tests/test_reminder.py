"""提醒工具：注册条件、上限、到点发送走正常通道、迟到说明。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot, chat_tools
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.reminder import describe_fire_at, resolve_fire_at

from conftest import RecordingAdapter, ScriptedLLM


class ToolLLM:
    """按脚本返回工具 message 的假 LLM；``tool_script`` 用完后退化为普通生成。"""

    def __init__(self, tool_script=None, *, chat_reply="好，记下了", tools_supported=True):
        self.tool_script = list(tool_script or [])
        self.chat_reply = chat_reply
        self.supports_tools = tools_supported
        self.tool_messages: list[list] = []
        self.tools_seen: list[list] = []
        self.chat_calls = 0

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        if role == "fast":
            return json.dumps({"intent": "补充新信息", "increment": "根据工具结果回复"})
        self.chat_calls += 1
        return self.chat_reply

    async def chat_with_tools(self, messages, tools, *, role="chat", temperature=None, tool_choice="auto"):
        self.tool_messages.append(messages)
        self.tools_seen.append(tools)
        if not self.tool_script:
            return {"content": self.chat_reply}
        return self.tool_script.pop(0)


def reminder_message(arguments, call_id="r1"):
    """包成 LLM 返回的 message 形状：content 为空、带一个 tool_calls。"""
    return {"content": "", "tool_calls": [reminder_call(arguments, call_id=call_id)]}


def reminder_call(arguments, call_id="r1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "set_reminder",
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def make_bot(store, llm, *, reminder_enabled=True) -> tuple[RubenBot, RecordingAdapter]:
    config = Config()
    config.chat.type_speed = 0
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    config.memory.recall_tool_enabled = False
    config.reminder.enabled = reminder_enabled
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def private(text="十分钟后提醒我吃药", sender="u1", name="阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender,
        sender_id=sender,
        sender_name=name,
        text=text,
    )


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("recording", "u1", "阿花")
    yield s
    s.close()


# ----------------------------------------------------------------------
# 注册条件
# ----------------------------------------------------------------------


def test_tool_registered_when_enabled():
    config = Config()
    config.memory.recall_tool_enabled = False
    names = [tool["function"]["name"] for tool in chat_tools(config)]
    assert names == ["set_reminder"]


def test_tool_absent_when_disabled():
    config = Config()
    config.memory.recall_tool_enabled = False
    config.reminder.enabled = False
    assert chat_tools(config) == []


def test_tool_offered_to_model(store):
    llm = ToolLLM([reminder_message({"text": "吃药", "delay_minutes": 10}), {"content": "好，十分钟后喊你"}])
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private()))
    offered = [tool["function"]["name"] for tool in llm.tools_seen[0]]
    assert "set_reminder" in offered
    jobs = store.list_scheduled_jobs(status="pending")
    assert len(jobs) == 1
    assert jobs[0].payload["text"] == "吃药"


def test_disabled_tool_not_offered(store):
    llm = ToolLLM([{"content": "好的"}])
    bot, _ = make_bot(store, llm, reminder_enabled=False)
    asyncio.run(bot.handle_event(private()))
    # 没有工具可给，直接走普通生成
    assert llm.tools_seen == []
    assert llm.chat_calls == 1


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------


def test_resolve_delay_minutes():
    fire_at, error = resolve_fire_at({"delay_minutes": 30}, 1000.0, max_delay_minutes=600)
    assert error is None and fire_at == 1000.0 + 30 * 60


def test_resolve_at_iso_local():
    target = datetime.fromtimestamp(1000.0 + 3600).replace(second=0, microsecond=0)
    fire_at, error = resolve_fire_at({"at_iso": target.isoformat()}, 1000.0, max_delay_minutes=600)
    assert error is None
    assert fire_at == pytest.approx(target.timestamp())


def test_resolve_rejects_past_and_bad_values():
    past = datetime.fromtimestamp(1000.0 - 3600).isoformat()
    _, error = resolve_fire_at({"at_iso": past}, 1000.0, max_delay_minutes=600)
    assert "过去" in error
    _, error = resolve_fire_at({"delay_minutes": 0}, 1000.0, max_delay_minutes=600)
    assert "大于 0" in error
    _, error = resolve_fire_at({"delay_minutes": "soon"}, 1000.0, max_delay_minutes=600)
    assert "数字" in error
    _, error = resolve_fire_at({}, 1000.0, max_delay_minutes=600)
    assert "delay_minutes" in error
    _, error = resolve_fire_at({"delay_minutes": 601}, 1000.0, max_delay_minutes=600)
    assert "最远" in error


def test_delay_takes_priority_over_at_iso():
    future = (datetime.fromtimestamp(1000.0) + timedelta(days=1)).isoformat()
    fire_at, error = resolve_fire_at(
        {"delay_minutes": 5, "at_iso": future}, 1000.0, max_delay_minutes=600
    )
    assert error is None and fire_at == 1000.0 + 300


def test_describe_fire_at():
    now = 1_795_000_000.0
    assert "分钟" in describe_fire_at(now + 240, now)
    same_day = now + 6 * 3600
    assert describe_fire_at(same_day, now).count(":") == 1


# ----------------------------------------------------------------------
# 上限与拒绝
# ----------------------------------------------------------------------


def test_pending_limit_per_chat(store):
    bot, _ = make_bot(store, ScriptedLLM())
    event = private()
    for _ in range(bot.config.reminder.max_pending_per_chat):
        answer = asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=event))
        assert "已设好提醒" in answer
    over = asyncio.run(bot.set_reminder({"text": "再一个", "delay_minutes": 10}, event=event))
    assert "上限" in over
    assert store.count_pending_jobs(kind="reminder") == bot.config.reminder.max_pending_per_chat


def test_limit_counts_per_conversation(store):
    bot, _ = make_bot(store, ScriptedLLM())
    for _ in range(bot.config.reminder.max_pending_per_chat):
        asyncio.run(bot.set_reminder({"text": "A", "delay_minutes": 10}, event=private(sender="u1")))
    other = asyncio.run(
        bot.set_reminder({"text": "B", "delay_minutes": 10}, event=private(sender="u2", name="阿草"))
    )
    assert "已设好提醒" in other


def test_empty_text_rejected(store):
    bot, _ = make_bot(store, ScriptedLLM())
    answer = asyncio.run(bot.set_reminder({"delay_minutes": 10, "text": "  "}, event=private()))
    assert "不能为空" in answer
    assert store.count_pending_jobs(kind="reminder") == 0


def test_text_truncated(store):
    bot, _ = make_bot(store, ScriptedLLM())
    bot.config.reminder.max_text_chars = 20
    asyncio.run(bot.set_reminder({"text": "长" * 100, "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    assert len(job.payload["text"]) == 20


# ----------------------------------------------------------------------
# 到点发送
# ----------------------------------------------------------------------


def test_fire_sends_through_normal_channel(store):
    """到点的提醒让模型现写一句，经正常发送通道发出，并落进流水。"""
    llm = ScriptedLLM({"chat": ["该吃药啦，别忘了"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]

    asyncio.run(bot.handle_scheduled_job(job))
    assert adapter.texts == ["该吃药啦，别忘了"]
    # 提示词里带提醒正文，且要求自然表达、不走模板
    system = llm.calls[0]["messages"][0]["content"]
    assert "吃药" in system and "模板" in system
    # 落进流水（sender 是 bot），后续上下文能看到自己说过
    recent = store.recent_messages("u1", limit=5, platform="recording")
    assert recent[-1].text == "该吃药啦，别忘了"
    assert recent[-1].sender_id == "__bot__"


def test_fire_late_explains_delay(store):
    llm = ScriptedLLM({"chat": ["来晚了——该吃药了"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    asyncio.run(bot.handle_scheduled_job(job, late=True))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "来晚了" in prompt
    assert adapter.texts == ["来晚了——该吃药了"]


def test_fire_falls_back_to_reminder_text_when_model_silent(store):
    llm = ScriptedLLM({"chat": [""]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "开会", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    asyncio.run(bot.handle_scheduled_job(job))
    assert adapter.texts == ["开会"]


def test_fire_without_adapter_is_logged_not_raised(store):
    bot, _ = make_bot(store, ScriptedLLM())
    asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    bot.adapters.clear()
    asyncio.run(bot.handle_scheduled_job(job))  # 不抛异常


def test_scheduler_wired_to_bot(store):
    llm = ScriptedLLM({"chat": ["到点了"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    asyncio.run(bot.scheduler.scan(now=job.fire_at + 1))
    assert adapter.texts == ["到点了"]
    assert store.get_scheduled_job(job.id).status == "done"
