"""T2 recall 工具循环：模型可用工具时最多两轮；不支持/不用/出错都退化为单次生成。"""

import asyncio
import json

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import RecordingAdapter


class ToolLLM:
    """假 LLM：``tool_script`` 是每次工具请求要返回的 message 列表。"""

    def __init__(self, tool_script=None, *, tools_supported=True, tool_error=None, chat_reply="嗯嗯"):
        self.tool_script = list(tool_script or [])
        self.supports_tools = tools_supported
        self.tool_error = tool_error
        self.chat_reply = chat_reply
        self.tool_calls = 0
        self.chat_calls = 0
        self.last_tool_messages = None

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        if role == "fast":
            return json.dumps({"intent": "补充新信息", "increment": "根据工具结果回复"})
        self.chat_calls += 1
        return self.chat_reply

    async def chat_with_tools(self, messages, tools, *, role="chat", temperature=None, tool_choice="auto"):
        self.tool_calls += 1
        if self.tool_error is not None:
            raise self.tool_error
        self.last_tool_messages = messages
        if not self.tool_script:
            return {"content": ""}
        return self.tool_script.pop(0)


def tool_call(call_id="c1", name="recall", arguments=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"person_query": "阿花"}, ensure_ascii=False),
        },
    }


def make_bot(store, llm, *, recall_enabled=True, reminder_enabled=False) -> tuple[RubenBot, RecordingAdapter]:
    config = Config()
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    config.memory.recall_tool_enabled = recall_enabled
    # 提醒工具默认开着；这组用例只测 recall，关掉它保证「没有工具」的退化路径
    config.reminder.enabled = reminder_enabled
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def private(text="在吗", sender="u1", name="阿花") -> MessageEvent:
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


def test_tool_loop_calls_recall_and_answers(store):
    llm = ToolLLM(
        [
            {"content": "", "tool_calls": [tool_call(arguments={"person_query": "阿花"})]},
            {"content": "记得，你最近在准备考试"},
        ]
    )
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("你还记得我最近在干嘛吗")))
    assert adapter.texts == ["记得，你最近在准备考试"]
    assert llm.tool_calls == 2
    assert llm.chat_calls == 0  # 工具路径成功，不再走普通生成
    # 第二轮请求里应带上工具结果
    roles = [message["role"] for message in llm.last_tool_messages]
    assert "tool" in roles
    tool_message = next(m for m in llm.last_tool_messages if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "c1"


def test_tool_result_respects_gate(store):
    """工具查到的私聊来源记忆，在群里发起时要被审查口扣下。"""
    store.add_memory("recording:u1", "TA 去看心理医生了", audience="private", now=1_795_000_000.0)
    llm = ToolLLM(
        [
            {"content": "", "tool_calls": [tool_call(arguments={"person_query": "阿花", "topic": "心理"})]},
            {"content": "嗯"},
        ]
    )
    # 直接调 recall，模拟群聊场景（事件给群聊）
    bot, _ = make_bot(store, llm)
    event = MessageEvent(
        platform="recording", chat_type="group", chat_id="g9",
        sender_id="u2", sender_name="阿草", text="阿花最近怎么样",
    )
    content = asyncio.run(bot.recall("阿花", "心理", event=event))
    assert "心理医生" not in content


def test_tool_unsupported_falls_back(store):
    llm = ToolLLM(tools_supported=False)
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["嗯嗯"]
    assert llm.tool_calls == 0
    assert llm.chat_calls == 1


def test_tool_error_falls_back(store):
    llm = ToolLLM(tool_error=RuntimeError("中转不支持"))
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["嗯嗯"]
    assert llm.chat_calls == 1


def test_tool_not_used_uses_first_reply(store):
    """模型没要工具但直接给了内容：直接用，不再发第二次请求。"""
    llm = ToolLLM([{"content": "在的呀"}])
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["在的呀"]
    assert llm.tool_calls == 1
    assert llm.chat_calls == 0


def test_empty_tool_reply_falls_back(store):
    llm = ToolLLM([{"content": ""}])
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["嗯嗯"]
    assert llm.chat_calls == 1


def test_unknown_tool_name_reports_back(store):
    llm = ToolLLM(
        [
            {"content": "", "tool_calls": [tool_call(name="search")]},
            {"content": "好"},
        ]
    )
    bot, _ = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    tool_message = next(m for m in llm.last_tool_messages if m["role"] == "tool")
    assert "没有名为 search 的工具" in tool_message["content"]


def test_two_rounds_then_forced_finish(store):
    """模型连着两轮都要工具：第三轮强制不给工具，只收尾一次。"""
    llm = ToolLLM(
        [
            {"content": "", "tool_calls": [tool_call(call_id="c1")]},
            {"content": "", "tool_calls": [tool_call(call_id="c2")]},
            {"content": "好了"},
        ]
    )
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["好了"]
    assert llm.tool_calls == 3


def test_recall_tool_disabled(store):
    llm = ToolLLM([{"content": "不该用到"}])
    bot, adapter = make_bot(store, llm, recall_enabled=False)
    asyncio.run(bot.handle_event(private("在吗")))
    assert adapter.texts == ["嗯嗯"]
    assert llm.tool_calls == 0
