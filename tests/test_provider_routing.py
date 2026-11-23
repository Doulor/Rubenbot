"""三层模型配置：同时核对上游路径、凭据和模型名，不把同名模型当成同一目标。"""

import asyncio
import json

import httpx
import pytest
import yaml

from rubenbot.config import Config
from rubenbot.llm import LLMClient, LLMResponseError, ToolUnsupported


def catalog():
    return {
        "providers": [
            {"name": "甲", "base_url": "https://relay.test/a/v1", "api_key": "a-key",
             "models": ["shared", "vision-a"]},
            {"name": "乙", "base_url": "https://relay.test/b/v2/", "api_key": "",
             "models": ["shared", "vision-b"]},
        ],
        "role_pools": {
            "chat": [{"provider": p, "model": "shared"} for p in ("甲", "乙")],
            "vision": [{"provider": "甲", "model": "vision-a"}, {"provider": "乙", "model": "vision-b"}],
        },
    }


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_two_providers_two_roles_failover_and_credential_isolation():
    calls = []

    def handle(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body["model"], req.headers.get("authorization"), bool(body.get("tools"))))
        if req.url.path.startswith("/a/"):
            return httpx.Response(503, headers={"set-cookie": "private=a; Path=/"})
        assert "cookie" not in req.headers  # 即便同域的两个中转也不能混用 Cookie
        return reply("乙")

    async def run():
        client = LLMClient(Config.from_raw({"llm": catalog()}).llm, transport=httpx.MockTransport(handle))
        try:
            assert await client.complete([]) == "乙"
            assert (await client.chat_with_tools([], [{"type": "function"}], role="vision"))["content"] == "乙"
        finally:
            await client.close()

    asyncio.run(run())
    assert calls == [
        ("/a/v1/chat/completions", "shared", "Bearer a-key", False),
        ("/b/v2/chat/completions", "shared", None, False),
        ("/a/v1/chat/completions", "vision-a", "Bearer a-key", True),
        ("/b/v2/chat/completions", "vision-b", None, True),
    ]


def test_role_pools_rotate_independently_and_random_can_use_secondary(monkeypatch):
    raw = catalog()
    raw["model_strategy"] = "round_robin"
    raw["model_strategies"] = {"vision": "random"}
    monkeypatch.setattr("rubenbot.llm.random.randrange", lambda n: n - 1)

    async def run():
        client = LLMClient(Config.from_raw({"llm": raw}).llm, transport=httpx.MockTransport(
            lambda req: reply(req.url.path.split("/")[1])))
        try:
            return [await client.complete([], role=r) for r in ("chat", "custom", "vision", "chat", "chat")]
        finally:
            await client.close()

    assert asyncio.run(run()) == ["a", "a", "b", "b", "a"]


def test_auth_error_stops_tool_capability_cache_scoped_to_actual_target():
    calls = []

    def handle(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body["model"]))
        if len(calls) == 1:
            return httpx.Response(401)
        if len(calls) == 2:
            return httpx.Response(400, text="unsupported tools")
        return reply("ok")

    async def run():
        client = LLMClient(Config.from_raw({"llm": catalog()}).llm, transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(LLMResponseError):
                await client.complete([])
            with pytest.raises(ToolUnsupported):
                await client.chat_with_tools([], [])
            assert client.supports_tools
            await client.chat_with_tools([], [])
            await client.chat_with_tools([], [], role="vision")
        finally:
            await client.close()

    asyncio.run(run())
    assert calls == [("/a/v1/chat/completions", "shared"), ("/a/v1/chat/completions", "shared"),
                     ("/b/v2/chat/completions", "shared"), ("/a/v1/chat/completions", "vision-a")]


def test_legacy_yaml_and_explicit_catalog_have_identical_routes(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    before = """llm:
  base_url: https://old.test/v1
  api_key: ${LEGACY_TOKEN}
  chat_model: ignored
  gate_model: gate-m
  consolidate_model: cons-m
  models: {chat: [primary, backup], extract: extract-m, custom: single}
  model_strategy: round_robin
  model_strategies: {extract: failover}
"""
    path.write_text(before)
    monkeypatch.setenv("LEGACY_TOKEN", "resolved")
    legacy = Config.load(path).llm
    assert path.read_text() == before
    providers, pools = legacy.catalog_view()
    raw = {"providers": providers, "role_pools": pools, "model_strategy": legacy.model_strategy,
           "model_strategies": legacy.model_strategies}
    path.write_text(yaml.safe_dump({"llm": raw}))
    new = Config.load(path).llm
    for role in ("chat", "extract", "consolidate", "distill", "vision", "gate", "summary", "custom", "other"):
        assert legacy.targets_for(role) == new.targets_for(role)
        assert legacy.strategy_for(role) == new.strategy_for(role)
    assert pools.keys() == {"chat", "extract", "consolidate", "gate", "custom"}
    assert new.targets_for("chat")[0].api_key == "resolved"
    assert new.models_for("vision") == ["primary", "backup"]
    assert new.model_for("distill") == "cons-m"


def test_provider_credentials_expand_but_public_names_remain_literal(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "private-value")
    raw = catalog()
    raw["providers"][0].update(name="${RELAY_TOKEN}", api_key="${RELAY_TOKEN}")
    for refs in raw["role_pools"].values():
        refs[0]["provider"] = "${RELAY_TOKEN}"
    llm = Config.from_raw({"llm": raw}).llm
    target = llm.targets_for()[0]
    assert target.api_key == "private-value" and target.provider == "${RELAY_TOKEN}"
    assert "private-value" not in repr(llm) and "private-value" not in repr(target)
    providers, pools = llm.catalog_view()
    providers.clear()
    pools["chat"].clear()
    assert llm.targets_for()[0] == target


@pytest.mark.parametrize("alter", [
    lambda c: c.update(providers=None, role_pools=None),
    lambda c: c["providers"].append(dict(c["providers"][0])),
    lambda c: c["providers"][0]["models"].append("shared"),
    lambda c: c["providers"][0].update(name=" "),
    lambda c: c["providers"][0].update(base_url="file:///tmp/x"),
    lambda c: c["providers"][0].update(base_url="https://name:password@relay.test/"),
    lambda c: c["role_pools"].pop("chat"),
    lambda c: c["role_pools"].update(chat=[]),
    lambda c: c["role_pools"]["chat"].append({"provider": "乙", "model": "vision-a"}),
    lambda c: c["role_pools"]["chat"].append({"provider": "丙", "model": "shared"}),
    lambda c: c["role_pools"]["chat"].append({"provider": "乙", "model": "shared"}),
    lambda c: c["role_pools"].update(vision="vision-a"),
])
def test_invalid_catalog_is_rejected_before_any_request(alter):
    raw = catalog()
    alter(raw)
    with pytest.raises(ValueError, match="llm\\."):
        Config.from_raw({"llm": raw})
