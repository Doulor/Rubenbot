"""两段式生成：指导注入、失败回退与用量打标。"""

import asyncio
import json

import httpx
import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.llm import LLMClient, make_usage_recorder
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


def make_bot(config, store, llm) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def private_event(text="在吗，今天好累") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


class SlowLLM(ScriptedLLM):
    """第一段（role=fast）卡住不返回，正文正常。"""

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        if role == "fast":
            self.calls.append({"role": role, "task": task, "messages": messages})
            await asyncio.sleep(10)
            return "永远不会到这里"
        return await super().complete(
            messages, role=role, task=task, temperature=temperature, json_mode=json_mode
        )


class Stage1BoomLLM(ScriptedLLM):
    """第一段（role=fast）抛异常，正文正常。"""

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        if role == "fast":
            self.calls.append({"role": role, "task": task, "messages": messages})
            raise RuntimeError("指导模型挂了")
        return await super().complete(
            messages, role=role, task=task, temperature=temperature, json_mode=json_mode
        )


# ----------------------------------------------------------------------
# 开口裁定始终先于正文
# ----------------------------------------------------------------------


def test_group_with_missing_basis_does_not_generate(config, store):
    llm = ScriptedLLM({"fast": ['{"intent":"提问","increment":"问地点"}']})
    bot, adapter = make_bot(config, store, llm)
    event = MessageEvent("recording", "group", "g", "u", "群友", "去哪里玩")
    asyncio.run(bot.handle_event(event))
    assert not adapter.sent and not llm.chat_messages
    row, = store.list_decision_logs()
    assert row["factors"]["planner"]["basis"] == "guess"


def test_default_flow_still_plans_private_reply(config, store):
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["在的"]
    assert [c["task"] for c in llm.calls] == ["gen_stage1", "gen_stage2"]
    assert [c["role"] for c in llm.calls] == ["fast", "chat"]


def test_invalid_plan_in_direct_reply_carries_uncertainty(config, store):
    llm = ScriptedLLM({"fast": ["不是裁定书"], "chat": ["这个我不确定"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["这个我不确定"]
    assert "不确定" in llm.chat_messages[0][-1]["content"]
    row, = store.list_decision_logs()
    assert row["factors"]["expression_decision"]["source"] == "fallback"


# ----------------------------------------------------------------------
# 策划指导生成并注入第二段
# ----------------------------------------------------------------------


def test_guidance_generated_and_injected(config, store):
    llm = ScriptedLLM(
        {
            "fast": [json.dumps({"intent": "提问", "increment": "问问是哪件事累着了"})],
            "chat": ["辛苦啦，早点歇着"],
        }
    )
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))

    assert adapter.texts == ["辛苦啦，早点歇着"]
    # 第一段走 fast 角色、独立的 task
    stage1 = llm.calls[0]
    assert stage1["role"] == "fast"
    assert stage1["task"] == "gen_stage1"
    # 第一段拿到的是与正文相同的上下文（人设 + 历史 + 本轮消息）
    assert "你是 Ruben" in stage1["messages"][0]["content"]
    assert any("今天好累" in m["content"] for m in stage1["messages"])
    # 第二段 prompt 在历史之后多了一节内部指导
    stage2 = llm.calls[1]
    assert stage2["role"] == "chat"
    assert stage2["task"] == "gen_stage2"
    last = stage2["messages"][-1]
    assert last["role"] == "system"
    assert "内部回复指导" in last["content"]
    assert "问问是哪件事累着了" in last["content"]
    assert "只有聊天正文" in last["content"]
    assert "一个完整的意思" in last["content"] and "自然停顿处换行" in last["content"]
    assert "延续、补充、递进或转折" in last["content"]
    # 指导在历史之后：前面仍是原来的上下文
    assert stage2["messages"][: len(stage1["messages"]) - 1] == stage1["messages"][:-1]


def test_guidance_after_history_before_instruction(config, store):
    """指导块位于历史之后、生成指令之前。"""
    llm = ScriptedLLM({"fast": ["- 语气：轻松"], "chat": ["好"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    messages = llm.calls[1]["messages"]
    assert messages[-1]["role"] == "system"
    # 最后一条历史是本轮用户消息，指导块排在它后面
    assert messages[-2]["role"] == "user"
    assert "今天好累" in messages[-2]["content"]


def test_fast_missing_falls_back_to_chat_model(config, store):
    """没配 fast 时，第一段仍用 fast 角色，由客户端回退到 chat 模型。"""
    assert config.llm.model_for("fast") == config.llm.model_for("chat")
    llm = ScriptedLLM({"fast": ["- 语气：随口"], "chat": ["嗯"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert llm.calls[0]["role"] == "fast"


def test_guidance_truncated_to_limit(config, store):
    long_guidance = "长" * 1000
    llm = ScriptedLLM({"fast": [long_guidance], "chat": ["好"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    injected = llm.calls[1]["messages"][-1]["content"]
    assert len(injected) < 600  # 指导被截断，不会整段塞进 prompt


# ----------------------------------------------------------------------
# 容错：第一段失败/超时/空 → 硬触发仍按保守指导生成
# ----------------------------------------------------------------------


def test_stage1_failure_uses_conservative_direct_reply(config, store):
    llm = Stage1BoomLLM({"chat": ["还好吗"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常
    assert adapter.texts == ["还好吗"]
    # 正文按保守的碎片指导生成
    assert llm.calls[-1]["role"] == "chat"
    assert "短附和；长度档：碎片" in llm.calls[-1]["messages"][-1]["content"]


def test_stage1_empty_guidance_falls_back(config, store):
    llm = ScriptedLLM({"fast": ["   "], "chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["在的"]
    assert "短附和；长度档：碎片" in llm.calls[-1]["messages"][-1]["content"]


def test_stage1_veto_marker_is_not_guidance(config, store):
    """第一段顺着「不回复」出口只回了标记：不能把它当指导塞进正文。"""
    marker = config.decision.veto_marker
    llm = ScriptedLLM({"fast": [marker], "chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(MessageEvent("recording", "group", "g", "u", "群友", "今天好累")))
    assert not adapter.sent and not llm.chat_messages
    row, = store.list_decision_logs()
    assert row["factors"]["planner"]["reason"] == ""
    assert row["factors"]["planner"]["result_code"] == "veto_marker"


def test_stage1_timeout_falls_back(config, store):
    config.llm.timeout = 0.05
    llm = SlowLLM()
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常，也不卡住
    # 超时后仍用默认回复正常发出
    assert adapter.texts == [llm.default]


def test_plugin_can_edit_context_before_stage1(config, store):
    """插件改写过的上下文同样喂给第一段，保证两段看到的一致。"""
    from rubenbot.plugins.loader import Plugin, PluginContext, PluginManager
    from pathlib import Path

    plugins = PluginManager(config, store)

    async def edit(runtime, event, messages):
        messages[0]["content"] += "\n插件补充：今天别开玩笑。"
        return messages

    plugin = Plugin(name="hint", path=Path("."), enabled=True)
    PluginContext(plugin, plugins).on_before_reply(edit)
    plugins.plugins["hint"] = plugin

    llm = ScriptedLLM({"fast": ["- 避开：玩笑"], "chat": ["好"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=plugins)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(private_event()))
    assert "插件补充" in llm.calls[0]["messages"][0]["content"]


# ----------------------------------------------------------------------
# 用量：两段调用各记一条（真实客户端 + MockTransport）
# ----------------------------------------------------------------------


def usage_rows(store):
    with store._lock:
        rows = store._conn.execute(
            "SELECT task, model, ok FROM llm_usage ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def test_usage_records_both_stages(config, store):
    def handler(request):
        body = json.loads(request.content)
        content = json.dumps({"intent": "提问", "increment": "问具体累在哪里"}) if body.get("response_format") else "好呀"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    config.llm.models = {"fast": "m-fast"}
    client = LLMClient(
        config.llm,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
        retry_delays=(0.0,),
    )
    bot, adapter = make_bot(config, store, client)
    asyncio.run(bot.handle_event(private_event()))
    asyncio.run(client.close())

    assert adapter.texts == ["好呀"]
    rows = usage_rows(store)
    assert [row["task"] for row in rows] == ["gen_stage1", "gen_stage2"]
    assert rows[0]["model"] == "m-fast"
    assert rows[1]["model"] == config.llm.model_for("chat")
    assert all(row["ok"] == 1 for row in rows)


def test_usage_includes_default_planner(config, store):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好呀"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = LLMClient(
        config.llm,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
        retry_delays=(0.0,),
    )
    bot, _ = make_bot(config, store, client)
    asyncio.run(bot.handle_event(private_event()))
    asyncio.run(client.close())
    assert [row["task"] for row in usage_rows(store)] == ["gen_stage1", "gen_stage2"]


def test_stage1_failed_call_still_counted_as_failure(config, store):
    """第一段失败时用量表里仍有一条 ok=0 的 gen_stage1。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        body = json.loads(request.content)
        if body["model"] == "m-fast":
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"choices": [{"message": {"content": "好呀"}}]})

    config.llm.models = {"fast": "m-fast"}
    config.llm.max_retries = 0
    client = LLMClient(
        config.llm,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
        retry_delays=(0.0,),
    )
    bot, adapter = make_bot(config, store, client)
    asyncio.run(bot.handle_event(private_event()))
    asyncio.run(client.close())

    assert adapter.texts == ["好呀"]
    rows = usage_rows(store)
    assert [(row["task"], row["ok"]) for row in rows] == [("gen_stage1", 0), ("gen_stage2", 1)]


# ----------------------------------------------------------------------
# WebUI 结构化设置
# ----------------------------------------------------------------------


def test_structured_view_omits_retired_switch():
    from rubenbot.web.config_schema import structured_config

    config = Config.from_dict({"generation": {"two_stage": {"enabled": True}}})
    assert "two_stage" not in structured_config(config).get("generation", {})


@pytest.mark.parametrize("old_switch", [{"enabled": True}, {"enabled": "on"}, True])
def test_validate_structured_ignores_retired_switch(old_switch):
    from rubenbot.web.config_schema import validate_structured

    update = validate_structured({"generation": {"two_stage": old_switch}, "reply": {"max_segments": 2}})
    assert update == {"reply": {"max_segments": 2}}


def test_stale_form_cannot_save_or_apply_retired_switch(tmp_path, store):
    """旧表单和现役回复设置一起提交时，只保存并热应用现役部分。"""
    pytest.importorskip("fastapi")
    import yaml
    from fastapi.testclient import TestClient

    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    config_path = tmp_path / "config.yaml"
    config_path.write_text("reply:\n  max_segments: 3\n", encoding="utf-8")
    state = WebState(config=Config.load(config_path), store=store, config_path=config_path)
    client = TestClient(create_app(state))
    draft = {"generation": {"two_stage": {"enabled": True}}, "reply": {"max_segments": 2}}
    response = client.put("/api/config", json={"config": draft})
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == ["reply"]
    assert response.json()["resolution"] == {"reply.max_segments": "hot"}
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {"reply": {"max_segments": 2}}
    assert state.config.reply.max_segments == 2
    view = client.get("/api/config").json()["structured"]
    assert "two_stage" not in view.get("generation", {})
    assert view["reply"]["max_segments"] == 2
    again = client.put("/api/config", json={"config": view})
    assert again.status_code == 200, again.text
    assert again.json()["applied"] == []
