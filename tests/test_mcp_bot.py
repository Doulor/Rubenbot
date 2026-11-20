"""走真实 bot 生命周期和模型工具循环，不连外部服务。"""

import asyncio
import sys
from pathlib import Path

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.config import MCPServerConfig
from rubenbot.core import RubenBot
from rubenbot.persona import Persona


ECHO = "mcp__local__echo"
FIXTURE = Path(__file__).with_name("fixtures") / "mcp_echo.py"


def private():
    return MessageEvent(platform="recording", chat_type="private", chat_id="u1",
                        sender_id="u1", sender_name="阿花", text="帮我复述一下")


class EchoLLM(ScriptedLLM):
    """第一轮请求外部工具，第二轮拿到 tool 消息再回答。"""

    def __init__(self):
        super().__init__({"fast": ['{"intent":"补充新信息","increment":"复述测试词"}']})
        self.offered = []
        self.reply = None

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.offered.append({item["function"]["name"]: item["function"] for item in tools})
        if len(self.offered) == 1:
            return {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": ECHO, "arguments": '{"text":"回声测试"}',
            }}]}
        self.reply = messages[-1]
        return {"content": "听到回声了"}


def test_registered_mcp_tool_reaches_model_and_returns_result(config, store):
    config.mcp.servers = [MCPServerConfig(name="local", command=sys.executable, args=[str(FIXTURE)])]
    llm = EchoLLM()
    bot = RubenBot(config, store, llm, Persona("Ruben", "说人话"))
    adapter = RecordingAdapter()

    async def chat(on_event):
        assert bot.mcp.status()[0]["state"] == "connected"
        await on_event(private())

    adapter.start = chat
    bot.register_adapter(adapter)
    asyncio.run(asyncio.wait_for(bot.run(), timeout=15))
    assert ECHO in llm.offered[0] and "recall" in llm.offered[0]
    assert llm.offered[0][ECHO]["parameters"]["required"] == ["text"]
    assert llm.reply["role"] == "tool" and llm.reply["tool_call_id"] == "c1"
    assert "回声测试" in llm.reply["content"]
    assert adapter.texts == ["听到回声了"]
    assert ECHO not in [item["function"]["name"] for item in bot.tool_definitions()]


def test_missing_server_does_not_interrupt_chat(config, store, tmp_path):
    config.mcp.servers = [MCPServerConfig(name="absent", command=str(tmp_path / "absent"))]
    llm = ScriptedLLM({"chat": ["在呢"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "说人话"))
    adapter = RecordingAdapter()

    async def chat(on_event):
        assert bot.mcp.status()[0]["last_error"]
        await on_event(private())

    adapter.start = chat
    bot.register_adapter(adapter)
    asyncio.run(asyncio.wait_for(bot.run(), timeout=15))
    assert adapter.texts == ["在呢"]
    assert bot.mcp.tools() == []


def test_loss_of_last_tool_keeps_its_result_for_final_reply(config, store, monkeypatch):
    """外部工具是唯一工具时，断线后的收尾仍能看到前一轮工具错误。"""
    config.memory.recall_tool_enabled = config.reminder.enabled = False
    llm = EchoLLM()
    bot = RubenBot(config, store, llm, Persona("Ruben", "说人话"))
    available = [{"type": "function", "function": {"name": ECHO, "parameters": {"type": "object"}}}]

    async def disconnect(name, args):
        available.clear()
        return "工具错误：断线"

    monkeypatch.setattr(bot.mcp, "tools", available.copy)
    monkeypatch.setattr(bot.mcp, "call", disconnect)
    asyncio.run(bot.generate([{"role": "user", "content": "测试"}], event=private()))
    assert len(llm.offered) == 1
    assert llm.chat_messages[0][-1] == {"role": "tool", "tool_call_id": "c1", "content": "工具错误：断线"}


@pytest.mark.parametrize("raw", ["{", "[]", "null", None])
def test_invalid_arguments_are_not_dispatched_as_an_empty_object(config, store, monkeypatch, raw):
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "说人话"))
    seen = []

    async def call(name, args):
        seen.append((name, args))
        return "工具错误"

    monkeypatch.setattr(bot.mcp, "call", call)
    result = asyncio.run(bot._tool_result_message(
        {"id": "bad", "function": {"name": ECHO, "arguments": raw}}, private(),
    ))
    assert seen == [(ECHO, None)]
    assert result == {"role": "tool", "tool_call_id": "bad", "content": "工具错误"}
