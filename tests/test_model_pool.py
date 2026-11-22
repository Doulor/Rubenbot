"""模型池与故障转移（Q14）：配置解析、三种策略、用量记账与 WebUI 校验。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rubenbot.config import MODEL_STRATEGIES, Config, LLMConfig
from rubenbot.llm import LLMClient, LLMError, LLMResponseError, ToolUnsupported, make_usage_recorder
from rubenbot.memory.store import MemoryStore
from rubenbot.web.config_schema import ConfigValidationError, structured_config, validate_structured


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _quiet_config_logs(caplog):
    caplog.set_level("CRITICAL", logger="rubenbot.config")


def ok(content="ok", model=None):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "http://test/chat/completions"),
    )


def client_for(handler, store=None, **config) -> LLMClient:
    cfg = LLMConfig(api_key="k", chat_model="m-chat", **config)
    return LLMClient(
        cfg,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store) if store is not None else None,
        retry_delays=(0.0, 0.0),
    )


def run(client, **kwargs):
    async def scenario():
        try:
            return await client.complete([{"role": "user", "content": "hi"}], **kwargs)
        finally:
            await client.close()

    return asyncio.run(scenario())


def usage_rows(store):
    with store._lock:
        rows = store._conn.execute(
            "SELECT task, model, ok FROM llm_usage ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------------
# 配置解析：旧写法不变，列表解析成模型池
# ----------------------------------------------------------------------


def test_models_value_accepts_list_and_single_string():
    cfg = LLMConfig(
        chat_model="m-chat",
        models={"chat": ["m-a", "m-b"], "fast": "m-fast"},
    )
    assert cfg.models_for("chat") == ["m-a", "m-b"]
    assert cfg.model_for("chat") == "m-a"  # 首选不变
    assert cfg.models_for("fast") == ["m-fast"]
    assert cfg.model_for("fast") == "m-fast"


def test_legacy_single_string_config_behaves_exactly_as_before():
    """旧配置（不写 models、单字符串）每个角色都只有一个模型，策略仍是默认。"""
    legacy = LLMConfig(chat_model="m-chat", extract_model="m-extract", consolidate_model="m-cons")
    assert legacy.models == {}
    assert legacy.model_for("chat") == "m-chat"
    assert legacy.model_for("extract") == "m-extract"
    assert legacy.model_for("consolidate") == "m-cons"
    assert legacy.models_for("extract") == ["m-extract"]
    assert legacy.models_for("fast") == ["m-chat"]
    assert legacy.strategy_for("chat") == "failover"

    loaded = Config.from_dict({"llm": {"chat_model": "m-chat", "models": {"fast": " m-f "}}})
    assert loaded.llm.models == {"fast": "m-f"}
    assert loaded.llm.models_for("fast") == ["m-f"]
    assert loaded.llm.models_for("vision") == ["m-chat"]


def test_models_list_normalized_deduped_and_empty_ignored():
    cfg = LLMConfig(
        models={
            "chat": [" m-a ", "m-a", "", None, "m-b"],
            "fast": [],
            "summary": "   ",
        }
    )
    assert cfg.models == {"chat": ["m-a", "m-b"]}
    assert cfg.models_for("chat") == ["m-a", "m-b"]
    # 空列表/空白模型名等于没配，照旧走回退链（这里是 chat 的池）
    assert "fast" not in cfg.models
    assert cfg.models_for("fast") == ["m-a", "m-b"]


def test_strategy_resolution_global_and_per_role():
    cfg = LLMConfig(
        chat_model="m",
        models={"chat": ["a", "b"], "fast": ["x", "y"]},
        model_strategy="round_robin",
        model_strategies={"fast": "random"},
    )
    assert cfg.strategy_for("chat") == "round_robin"
    assert cfg.strategy_for("fast") == "random"
    assert cfg.strategy_for("summary") == "round_robin"


def test_unknown_strategy_falls_back_without_crashing():
    cfg = LLMConfig(chat_model="m", model_strategy="carousel", model_strategies={"chat": "weird"})
    assert cfg.model_strategy == "failover"
    assert cfg.model_strategies == {}
    assert cfg.strategy_for("chat") == "failover"


def test_from_dict_loads_pool_and_strategies():
    cfg = Config.from_dict(
        {
            "llm": {
                "chat_model": "m-chat",
                "models": {"chat": ["m-a", "m-b"]},
                "model_strategy": "random",
                "model_strategies": {"chat": "round_robin"},
            }
        }
    )
    assert cfg.llm.models_for("chat") == ["m-a", "m-b"]
    assert cfg.llm.strategy_for("chat") == "round_robin"
    assert cfg.llm.strategy_for("fast") == "random"


# ----------------------------------------------------------------------
# 调用与故障转移
# ----------------------------------------------------------------------


def test_single_model_normal_path_calls_once():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return ok("你好")

    client = client_for(handler, models={"chat": "only"})
    assert run(client) == "你好"
    assert calls == ["only"]  # 正常路径不增加任何调用次数


def test_failover_records_pool_failure_then_success_model(store):
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "m-a":
            raise httpx.ReadTimeout("timeout", request=request)
        return ok("备选答的")

    client = client_for(handler, store, models={"chat": ["m-a", "m-b"]})
    assert run(client) == "备选答的"
    rows = usage_rows(store)
    # 每次尝试都记账，最终成功那条记实际出结果的模型
    assert [(row["model"], row["ok"]) for row in rows] == [("m-a", 0), ("m-b", 1)]


def test_failover_5xx_and_429_switch_to_next():
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "m-a":
            return httpx.Response(503, text="busy", request=request)
        if model == "m-b":
            return httpx.Response(429, text="slow down", request=request)
        return ok("第三个")

    client = client_for(handler, store, models={"chat": ["m-a", "m-b", "m-c"]})
    assert run(client) == "第三个"
    assert calls == ["m-a", "m-b", "m-c"]


def test_failover_does_not_retry_same_model():
    """多模型时最多按列表走一遍，不在同一个模型上退避重试。"""
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(500, text="boom", request=request)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]}, max_retries=2)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())
    assert calls == ["m-a", "m-b"]  # 不重复、不超出列表


def test_all_models_fail_raises_last_error(store):
    errors_by_model = {"m-a": 500, "m-b": 502}

    def handler(request):
        model = json.loads(request.content)["model"]
        return httpx.Response(errors_by_model[model], text=f"{model} failed", request=request)

    client = client_for(handler, store, models={"chat": ["m-a", "m-b"]})

    async def scenario():
        with pytest.raises(LLMError) as info:
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()
        return info.value

    error = asyncio.run(scenario())
    # 抛出的是最后一次的错误，不是第一个模型的 500
    last = error.__cause__
    assert isinstance(last, LLMResponseError)
    assert last.status == 502
    assert "m-b" in str(last)
    rows = usage_rows(store)
    assert [(row["model"], row["ok"]) for row in rows] == [("m-a", 0), ("m-b", 0)]


def test_all_models_network_fail_wraps_last_error():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]})

    async def scenario():
        with pytest.raises(LLMError) as info:
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()
        return info.value

    error = asyncio.run(scenario())
    assert "down" in str(error)
    assert isinstance(error.__cause__, httpx.ConnectError)


def test_failover_4xx_config_error_not_switched():
    """400/401/403/404 换模型也救不了：直接报，不消耗后续模型。"""
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(401, text="unauthorized", request=request)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]})

    async def scenario():
        with pytest.raises(LLMResponseError) as info:
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()
        return info.value

    error = asyncio.run(scenario())
    assert error.status == 401
    assert calls == ["m-a"]


def test_single_model_keeps_legacy_retry_behavior():
    """单模型列表仍按 max_retries 退避重试，与旧行为完全一致。"""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="busy", request=request)
        return ok("终于")

    client = client_for(handler, models={"chat": "only"}, max_retries=2)
    assert run(client) == "终于"
    assert len(calls) == 3


def test_round_robin_cycles_in_order():
    picks = []

    def handler(request):
        model = json.loads(request.content)["model"]
        picks.append(model)
        return ok(model)

    client = client_for(
        handler, models={"chat": ["m-a", "m-b", "m-c"]}, model_strategy="round_robin"
    )

    async def scenario():
        results = []
        for _ in range(4):
            results.append(await client.complete([{"role": "user", "content": "hi"}]))
        await client.close()
        return results

    results = asyncio.run(scenario())
    assert picks == ["m-a", "m-b", "m-c", "m-a"]
    assert results == picks  # 结果确实来自各自的模型


def test_round_robin_counter_is_in_memory_only():
    client = client_for(lambda request: ok(), models={"chat": ["a", "b"]}, model_strategy="round_robin")
    assert client._round_robin == {}
    # 计数只在内存里，配置对象里没有任何落盘字段
    assert not hasattr(client.config, "round_robin")


def test_random_only_picks_from_configured_list(monkeypatch):
    models = ["m-a", "m-b", "m-c"]
    starts = []

    class FakeRandom:
        def randrange(self, n):
            starts.append(n)
            return 2

    monkeypatch.setattr("rubenbot.llm.random", FakeRandom())
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return httpx.Response(500, text="boom", request=request)

    client = client_for(handler, models={"chat": models}, model_strategy="random")

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())
    # 随机只决定起点，后续按列表顺序全部试到；永远不越出列表
    assert starts == [3]
    assert calls == ["m-c", "m-a", "m-b"]


def test_random_success_uses_one_of_the_models(monkeypatch):
    monkeypatch.setattr("rubenbot.llm.random", type("R", (), {"randrange": lambda self, n: 1})())
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return ok(model)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]}, model_strategy="random")
    assert run(client) == "m-b"
    assert calls == ["m-b"]


def test_pool_falls_back_through_role_chain():
    """没配模型池的角色沿用回退链，池也一起继承。"""
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return ok(model)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]}, max_retries=0)
    assert run(client, role="extract") == "m-a"
    assert calls == ["m-a"]


def test_tool_calls_also_failover_and_keep_tool_unsupported_semantics():
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "m-a":
            return httpx.Response(503, text="busy", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "recall", "arguments": "{}"}}
                    ]}}
                ]
            },
            request=request,
        )

    client = client_for(handler, models={"chat": ["m-a", "m-b"]})

    async def scenario():
        message = await client.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "recall"}}],
        )
        await client.close()
        return message

    message = asyncio.run(scenario())
    assert message["tool_calls"][0]["function"]["name"] == "recall"
    assert calls == ["m-a", "m-b"]


def test_tool_unsupported_is_not_failover_signal():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text='{"error": "tools are not supported"}', request=request)

    client = client_for(handler, models={"chat": ["m-a", "m-b"]})

    async def scenario():
        with pytest.raises(ToolUnsupported):
            await client.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        await client.close()

    asyncio.run(scenario())
    assert len(calls) == 1  # 不是换模型的理由，直接退化


# ----------------------------------------------------------------------
# WebUI 结构化配置：校验、增删改顺序
# ----------------------------------------------------------------------


def test_validate_models_accepts_pool_and_single_string():
    update = validate_structured(
        {
            "llm": {
                "models": {"chat": [" m-a ", "m-b", "m-a"], "fast": "m-f", "summary": ""},
                "model_strategy": "round_robin",
                "model_strategies": {"chat": " random ", "gate": ""},
            }
        }
    )
    assert update["llm"]["models"] == {"chat": ["m-a", "m-b"], "fast": "m-f"}
    assert update["llm"]["model_strategy"] == "round_robin"
    assert update["llm"]["model_strategies"] == {"chat": "random"}


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"llm": {"models": {"chat": []}}}, "llm.models.chat"),
        ({"llm": {"models": {"chat": [""]}}}, "llm.models.chat.0"),
        ({"llm": {"models": {"chat": ["m-a", "  "]}}}, "llm.models.chat.1"),
        ({"llm": {"model_strategy": "carousel"}}, "llm.model_strategy"),
        ({"llm": {"model_strategies": {"chat": "carousel"}}}, "llm.model_strategies.chat"),
        ({"llm": {"models": ["chat"]}}, "llm.models"),
    ],
)
def test_validate_models_rejects_bad_values_with_clear_field(payload, field):
    with pytest.raises(ConfigValidationError) as info:
        validate_structured(payload)
    assert any(item["field"] == field for item in info.value.fields), info.value.fields
    message = next(item["message"] for item in info.value.fields if item["field"] == field)
    assert message  # 每条都有能读的原因

    empty_pool_messages = [item["message"] for item in info.value.fields if item["field"] == field]
    if field == "llm.models.chat":
        assert "空" in empty_pool_messages[0]
    if "strategy" in field:
        assert "failover" in empty_pool_messages[0]


def test_validate_models_schema_exposed_to_frontend():
    cfg = Config.from_dict(
        {
            "llm": {
                "models": {"chat": ["m-a", "m-b"]},
                "model_strategy": "random",
                "model_strategies": {"chat": "round_robin"},
            }
        }
    )
    view = structured_config(cfg)
    assert view["llm"]["models"] == {"chat": ["m-a", "m-b"]}
    assert view["llm"]["model_strategy"] == "random"
    assert view["llm"]["model_strategies"] == {"chat": "round_robin"}
    # 原样回传不应报错（表单「打开 → 原样保存」没改动）
    assert validate_structured({"llm": view["llm"]})["llm"]["models"] == {"chat": ["m-a", "m-b"]}


def test_default_strategy_is_listed_and_valid():
    assert "failover" in MODEL_STRATEGIES
    assert set(MODEL_STRATEGIES) == {"failover", "round_robin", "random"}
    assert Config().llm.model_strategy == "failover"


# ----------------------------------------------------------------------
# WebUI 管理接口：GET/PUT /api/models/pool（增删、调顺序、改策略）
# ----------------------------------------------------------------------


from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.state import WebState  # noqa: E402


@pytest.fixture()
def pool_state(store, tmp_path: Path):
    cfg = Config()
    cfg.llm.api_key = "sk-original"
    cfg.llm.chat_model = "m-old"
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  api_key: ${MY_KEY}\n  chat_model: m-old\n"
        "unknown_section:\n  keep_me: true\n",
        encoding="utf-8",
    )
    state = WebState(config=cfg, store=store, config_path=path)
    return state, path


@pytest.fixture()
def pool_client(pool_state):
    state, path = pool_state
    return TestClient(create_app(state)), state, path


def read_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pool_snapshot_lists_roles_and_effective_models(pool_client):
    client, state, _ = pool_client
    body = client.get("/api/models/pool").json()
    assert body["ok"] is True
    assert body["strategy"] == "failover"
    roles = {row["role"]: row for row in body["roles"]}
    # 预置角色都在，未配置的角色显示回退链结果
    assert roles["chat"]["models"] == ["m-old"] and roles["chat"]["configured"] is False
    assert roles["fast"]["models"] == ["m-old"]
    assert roles["chat"]["strategy"] == "failover"
    assert roles["chat"]["has_override"] is False


def test_put_pool_add_remove_reorder_and_strategy(pool_client):
    client, state, path = pool_client
    first = client.put(
        "/api/models/pool",
        json={
            "models": {"chat": ["m-a", "m-b"], "fast": "m-fast"},
            "strategy": "round_robin",
            "strategies": {"fast": "random"},
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["applied"] == ["llm"]
    assert "llm.models" in body["changed"]
    assert body["requires_restart"] == []
    # 热生效：进程内配置与 LLM 池立即更新
    assert state.config.llm.models_for("chat") == ["m-a", "m-b"]
    assert state.config.llm.strategy_for("chat") == "round_robin"
    assert state.config.llm.strategy_for("fast") == "random"
    roles = {row["role"]: row for row in body["roles"]}
    assert roles["chat"]["models"] == ["m-a", "m-b"]
    assert roles["chat"]["strategy"] == "round_robin"
    assert roles["fast"]["has_override"] is True

    # 调顺序 + 删角色：整表替换，删掉的角色真的从文件消失
    reordered = client.put(
        "/api/models/pool",
        json={"models": {"chat": ["m-b", "m-a"]}, "strategies": {}},
    )
    assert reordered.status_code == 200, reordered.text
    written = read_yaml(path)
    assert written["llm"]["models"] == {"chat": ["m-b", "m-a"]}
    assert written["llm"]["model_strategies"] == {}
    # 其它键（环境变量引用、未知段）原样保留
    assert written["llm"]["api_key"] == "${MY_KEY}"
    assert written["llm"]["chat_model"] == "m-old"
    assert written["unknown_section"] == {"keep_me": True}
    assert state.config.llm.models_for("chat") == ["m-b", "m-a"]
    assert "fast" not in state.config.llm.models


def test_put_pool_rejects_invalid_without_writing(pool_client):
    client, state, path = pool_client
    original = path.read_bytes()
    for payload, field in [
        ({"models": {"chat": []}}, "llm.models.chat"),
        ({"models": {"chat": ["m-a", ""]}}, "llm.models.chat.1"),
        ({"strategy": "carousel"}, "llm.model_strategy"),
        ({"strategies": {"chat": "carousel"}}, "llm.model_strategies.chat"),
    ]:
        response = client.put("/api/models/pool", json=payload)
        assert response.status_code == 422
        fields = {item["field"] for item in response.json()["fields"]}
        assert field in fields, response.json()
        assert path.read_bytes() == original  # 校验失败不写盘、不热应用
    assert state.config.llm.models == {}
    # 什么都不提交：明确 400
    assert client.put("/api/models/pool", json={}).status_code == 400


def test_pool_requires_session(store, tmp_path):
    from rubenbot.web.auth import SessionSigner

    cfg = Config()
    cfg.webui.password = "pw"
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  chat_model: m-old\n", encoding="utf-8")
    state = WebState(config=cfg, store=store, config_path=path, signer=SessionSigner("pw"))
    client = TestClient(create_app(state))
    assert client.get("/api/models/pool").status_code == 401
    assert client.put("/api/models/pool", json={"models": {}}).status_code == 401
