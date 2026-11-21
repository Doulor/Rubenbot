"""DSML 文本工具调用：解析成结构化调用 + 出站兜底剥除。

事故（2026-11-21 17:01/17:02，线上群 1125257581）：上游把 adapter_call
写成 DSML 文本混在 content 里，框架没解析、清洗没拦，原始标记被拆条
发进群两次共 8 条。这里用事故原文钉死行为。
"""

import asyncio
import json

import pytest

from conftest import RecordingAdapter
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot, clean_reply_patterns
from rubenbot.llm import normalize_dsml_message, parse_dsml_calls, strip_dsml
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

# 事故原文（两份，只差 count）
INCIDENT_5 = (
    "<｜｜DSML｜｜ calls>"
    '<｜｜DSML｜｜ invoke name="adapter_call">'
    '<｜｜DSML｜｜ parameter name="action" string="true">get_group_msg_history</｜｜DSML｜｜ parameter>'
    '<｜｜DSML｜｜ parameter name="args" string="false">{"count": 5}</｜｜DSML｜｜ parameter>'
    "</｜｜DSML｜｜ invoke>"
    "</｜｜DSML｜｜ calls>"
)
INCIDENT_3 = INCIDENT_5.replace('"count": 5', '"count": 3')


def _args(call):
    return json.loads(call["function"]["arguments"])


def test_incident_payload_parses_to_structured_call():
    for payload, count in ((INCIDENT_5, 5), (INCIDENT_3, 3)):
        calls, rest = parse_dsml_calls(payload)
        assert len(calls) == 1
        assert calls[0]["type"] == "function"
        assert calls[0]["function"]["name"] == "adapter_call"
        args = _args(calls[0])
        assert args["action"] == "get_group_msg_history"  # string="true" 保持字符串
        assert args["args"] == {"count": count}  # string="false" 按 JSON 解析成 dict
        assert rest.strip() == ""


def test_text_outside_block_is_kept():
    calls, rest = parse_dsml_calls("我先看一眼刚才的聊天。" + INCIDENT_5)
    assert len(calls) == 1
    assert rest == "我先看一眼刚才的聊天。"


def test_multiple_invokes_in_one_block():
    payload = (
        "<｜｜DSML｜｜ calls>"
        '<｜｜DSML｜｜ invoke name="recall">'
        '<｜｜DSML｜｜ parameter name="query" string="true">天气</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
        '<｜｜DSML｜｜ invoke name="recall">'
        '<｜｜DSML｜｜ parameter name="query" string="true">作业</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
        "</｜｜DSML｜｜ calls>"
    )
    calls, _ = parse_dsml_calls(payload)
    assert [c["function"]["name"] for c in calls] == ["recall", "recall"]
    assert _args(calls[0])["query"] == "天气"
    assert _args(calls[1])["query"] == "作业"
    assert calls[0]["id"] != calls[1]["id"]


def test_bad_json_param_falls_back_to_string_without_raising():
    payload = (
        "<｜｜DSML｜｜ calls>"
        '<｜｜DSML｜｜ invoke name="adapter_call">'
        '<｜｜DSML｜｜ parameter name="args" string="false">{坏的</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
        "</｜｜DSML｜｜ calls>"
    )
    calls, _ = parse_dsml_calls(payload)
    assert len(calls) == 1
    assert _args(calls[0])["args"] == "{坏的"


def test_unclosed_block_never_leaks_markup():
    broken = "<｜｜DSML｜｜ calls>" + '<｜｜DSML｜｜ invoke name="adapter_call">'
    assert "DSML" not in strip_dsml(broken)
    assert strip_dsml(broken).strip() == ""


def test_split_fragments_are_stripped():
    # 拆条把一整个块切成多条时，单条碎片也不能漏出去
    for fragment in (
        "<｜｜DSML｜｜ calls>",
        '<｜｜DSML｜｜ invoke name="adapter_call">',
        '<｜｜DSML｜｜ parameter name="action" string="true">get_group_msg_history</｜｜DSML｜｜ parameter>',
    ):
        assert "DSML" not in strip_dsml(fragment)


def test_normal_text_untouched():
    text = "今晚降温，记得加件外套"
    assert strip_dsml(text) == text
    calls, rest = parse_dsml_calls(text)
    assert calls == [] and rest == text


def test_normalize_merges_into_tool_calls_and_cleans_content():
    message = {"role": "assistant", "content": "好的，我查一下" + INCIDENT_5}
    updated = normalize_dsml_message(message)
    assert updated["content"] == "好的，我查一下"
    assert len(updated["tool_calls"]) == 1
    assert updated["tool_calls"][0]["function"]["name"] == "adapter_call"
    # 原消息不被就地修改
    assert "tool_calls" not in message


def test_clean_reply_patterns_strips_dsml():
    cleaned = clean_reply_patterns(INCIDENT_5)
    assert "DSML" not in cleaned


class _DsmlLLM:
    """chat_with_tools 返回带 DSML 文本调用的假 LLM。"""

    def __init__(self, message):
        self._message = message

    async def complete(self, messages, **kwargs):  # pragma: no cover - 本测试不走这条路
        return "嗯嗯"

    async def chat_with_tools(self, messages, tools, **kwargs):
        return dict(self._message)


@pytest.fixture
def bot(tmp_path, config):
    config.database_path = str(tmp_path / "data" / "bot.db")
    store = MemoryStore(config.database_path)
    dsml = (
        "<｜｜DSML｜｜ calls>"
        '<｜｜DSML｜｜ invoke name="recall">'
        '<｜｜DSML｜｜ parameter name="query" string="true">天气</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
        "</｜｜DSML｜｜ calls>"
    )
    instance = RubenBot(
        config, store, _DsmlLLM({"role": "assistant", "content": dsml}),
        Persona("Ruben", "说人话"),
    )
    instance.register_adapter(RecordingAdapter())
    yield instance
    store.close()


def test_core_converts_dsml_and_dispatches(bot):
    message = asyncio.run(bot._call_with_tools([], [], role="chat", task="chat"))
    assert message["content"] == ""
    calls = message["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "recall"
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "天气"}

    event = MessageEvent(
        platform="recording", chat_id="c1", sender_name="多萝",
        sender_id="owner", chat_type="private", text="天气怎么样", raw={},
    )
    result = asyncio.run(bot._tool_result_message(calls[0], event))
    assert result["role"] == "tool"
    assert result["tool_call_id"] == calls[0]["id"]
    assert "DSML" not in result["content"]
