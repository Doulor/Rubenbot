"""两段式生成：开关、指导注入、失败回退与用量打标。"""

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
# 开关关：行为与单段直出一致
# ----------------------------------------------------------------------


def test_disabled_by_default():
    assert Config().generation.two_stage.enabled is False


def test_default_flow_still_plans_private_reply(config, store):
    config.generation.two_stage.enabled = False
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["在的"]
    # 旧开关关闭也不绕过表达决策。
    assert [c["task"] for c in llm.calls] == ["gen_stage1", "gen_stage2"]
    assert [c["role"] for c in llm.calls] == ["fast", "chat"]


def test_config_parses_switch_and_defaults_to_off():
    assert Config.from_dict({"generation": {"two_stage": {"enabled": True}}}).generation.two_stage.enabled
    assert not Config.from_dict({}).generation.two_stage.enabled
    # 写错类型一律按关闭处理
    assert not Config.from_dict({"generation": {"two_stage": {"enabled": "yes"}}}).generation.two_stage.enabled
    assert not Config.from_dict({"generation": {"two_stage": "on"}}).generation.two_stage.enabled


# ----------------------------------------------------------------------
# 开关开：指导生成并注入第二段
# ----------------------------------------------------------------------


def test_guidance_generated_and_injected(config, store):
    config.generation.two_stage.enabled = True
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
    assert "不要复述" in last["content"]
    # 指导在历史之后：前面仍是原来的上下文
    assert stage2["messages"][: len(stage1["messages"]) - 1] == stage1["messages"][:-1]


def test_guidance_after_history_before_instruction(config, store):
    """指导块位于历史之后、生成指令之前。"""
    config.generation.two_stage.enabled = True
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
    config.generation.two_stage.enabled = True
    assert config.llm.model_for("fast") == config.llm.model_for("chat")
    llm = ScriptedLLM({"fast": ["- 语气：随口"], "chat": ["嗯"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert llm.calls[0]["role"] == "fast"


def test_guidance_truncated_to_limit(config, store):
    config.generation.two_stage.enabled = True
    long_guidance = "长" * 1000
    llm = ScriptedLLM({"fast": [long_guidance], "chat": ["好"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    injected = llm.calls[1]["messages"][-1]["content"]
    assert len(injected) < 600  # 指导被截断，不会整段塞进 prompt


# ----------------------------------------------------------------------
# 容错：第一段失败/超时/空 → 回退单段直出
# ----------------------------------------------------------------------


def test_stage1_failure_falls_back_to_single_pass(config, store):
    config.generation.two_stage.enabled = True
    llm = Stage1BoomLLM({"chat": ["还好吗"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常
    assert adapter.texts == ["还好吗"]
    # 正文照常生成；第二段 prompt 里没有指导块
    assert llm.calls[-1]["role"] == "chat"
    assert "短附和；长度档：碎片" in llm.calls[-1]["messages"][-1]["content"]


def test_stage1_empty_guidance_falls_back(config, store):
    config.generation.two_stage.enabled = True
    llm = ScriptedLLM({"fast": ["   "], "chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["在的"]
    assert "短附和；长度档：碎片" in llm.calls[-1]["messages"][-1]["content"]


def test_stage1_veto_marker_is_not_guidance(config, store):
    """第一段顺着「不回复」出口只回了标记：不能把它当指导塞进正文。"""
    config.generation.two_stage.enabled = True
    marker = config.decision.veto_marker
    llm = ScriptedLLM({"fast": [marker], "chat": ["在的"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    assert not adapter.sent
    assert not llm.chat_messages


def test_stage1_timeout_falls_back(config, store):
    config.generation.two_stage.enabled = True
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

    config.generation.two_stage.enabled = True
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
        text = " ".join(m["content"] for m in body["messages"])
        content = json.dumps({"intent": "提问", "increment": "问具体累在哪里"}) if body.get("response_format") else "好呀"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    config.generation.two_stage.enabled = True
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

    config.generation.two_stage.enabled = True
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


def test_structured_view_exposes_switch(config):
    from rubenbot.web.config_schema import structured_config

    view = structured_config(config)
    assert view["generation"]["two_stage"]["enabled"] is False


def test_validate_structured_switch():
    from rubenbot.web.config_schema import ConfigValidationError, validate_structured

    update = validate_structured({"generation": {"two_stage": {"enabled": True}}})
    assert update["generation"]["two_stage"]["enabled"] is True
    with pytest.raises(ConfigValidationError):
        validate_structured({"generation": {"two_stage": {"enabled": "on"}}})
    with pytest.raises(ConfigValidationError):
        validate_structured({"generation": {"two_stage": True}})


def test_put_structured_switch_written(tmp_path):
    """保存后写进 YAML，重新加载仍是开启状态。"""
    pytest.importorskip("fastapi")
    import yaml
    from fastapi.testclient import TestClient

    from rubenbot.memory.store import MemoryStore
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    persona = tmp_path / "SOUL.md"
    persona.write_text("# Ruben\n\n你是 Ruben。\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"llm:\n  api_key: k\npersona:\n  path: {persona}\n", encoding="utf-8"
    )
    store = MemoryStore(":memory:")
    try:
        state = WebState(config=Config.load(config_path), store=store, config_path=config_path)
        client = TestClient(create_app(state))
        response = client.put(
            "/api/config", json={"config": {"generation": {"two_stage": {"enabled": True}}}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["resolution"]["generation.two_stage.enabled"] == "hot"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert data["generation"]["two_stage"]["enabled"] is True
        assert "generation" in response.json()["applied"]
        # 热生效：运行中的配置对象立即可见
        assert state.config.generation.two_stage.enabled is True
        # 原样再存一次不算改动
        view = client.get("/api/config").json()["structured"]
        view.pop("secrets")
        again = client.put("/api/config", json={"config": view})
        assert again.status_code == 200, again.text
        assert again.json()["applied"] == []
    finally:
        store.close()
