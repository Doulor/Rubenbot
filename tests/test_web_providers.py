"""提供商、模型目录和角色池在 WebUI 中的读写、热应用与密钥边界。"""

from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient

from rubenbot.config import Config
from rubenbot.web.app import create_app
from rubenbot.web.state import WebState


def ref(provider, model):
    return {"provider": provider, "model": model}


@pytest.fixture
def web(tmp_path, store, monkeypatch):
    monkeypatch.setenv("OLD_KEY", "secret-first")
    monkeypatch.setenv("OLD_URL", "https://first.test/v1")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "llm": {"base_url": "${OLD_URL}", "api_key": "${OLD_KEY}", "models": {"chat": ["a", "v"]}},
        "custom": {"keep": 42},
    }))
    state = WebState(Config.load(path), store, path)
    with TestClient(create_app(state)) as client:
        yield client, state


def save(client, llm):
    return client.put("/api/config", json={"config": {"llm": llm}})


def test_web_migrate_edit_delete_reload_and_hide_keys(web):
    client, state = web
    original = state.config_path.read_bytes()
    body = client.get("/api/config").json()
    assert body["raw_hidden"] and not body["text"]
    draft = body["structured"]["llm"]
    assert draft["providers"] == [{"name": "default", "base_url": "https://first.test/v1", "models": ["a", "v"], "has_api_key": True}]
    assert draft["role_pools"] == {"chat": [ref("default", "a"), ref("default", "v")]}
    assert state.config_path.read_bytes() == original
    assert save(client, {"temperature": 0.4}).is_success
    assert "providers" not in yaml.safe_load(state.config_path.read_text())["llm"]

    # 旧端点和新增提供商都有两个模型；同名模型不能混淆。模型页保存热重建客户端。
    draft["providers"].append({"name": "second", "base_url": "https://second.test/v1", "api_key": "secret-second", "models": ["a", "w"]})
    draft["role_pools"] = {"chat": [ref("default", "a"), ref("second", "a")],
                         "vision": [ref("second", "w"), ref("default", "v")]}
    draft["model_strategies"] = {"vision": "round_robin"}
    state.bot = Mock()
    response = save(client, draft)
    assert response.status_code == 200, response.text
    state.bot.apply_config.assert_called_once_with(state.config, rebuild_llm=True)
    state.bot = None
    assert response.json()["applied"] == ["llm"]
    assert response.json()["requires_restart"] == []
    raw = yaml.safe_load(state.config_path.read_text())
    assert raw["custom"] == {"keep": 42}
    assert not {"models", "base_url", "api_key", "chat_model"} & raw["llm"].keys()
    assert raw["llm"]["providers"][0]["api_key"] == "${OLD_KEY}"
    assert raw["llm"]["providers"][0]["base_url"] == "${OLD_URL}"
    assert raw["llm"]["role_pools"] == draft["role_pools"]
    assert Config.load(state.config_path).llm == state.config.llm
    assert [(t.provider, t.api_key) for t in state.config.llm.targets_for("vision")] == [("second", "secret-second"), ("default", "secret-first")]

    for snapshot in (response, *(client.get(url) for url in ("/api/config", "/api/models/pool", "/api/about"))):
        assert "secret-first" not in snapshot.text and "secret-second" not in snapshot.text
    view = client.get("/api/config").json()
    assert view["raw_hidden"] and not view["text"]
    for section in (view["effective"]["llm"], view["structured"]["llm"]):
        assert "api_key" not in section and "api_key" not in section["providers"][0]
    current = view["structured"]["llm"]
    assert all(row["has_api_key"] for row in current["providers"])
    assert save(client, current).json()["applied"] == []
    # 仅删角色也算热更新，不因其余池未变而谎报「无需变更」。
    assert save(client, {"role_pools": {"chat": current["role_pools"]["chat"]}}).json()["applied"] == ["llm"]
    # 删掉提供商、角色覆盖、同一提供商的一个模型，再调默认和策略。
    current["providers"] = [{**current["providers"][1], "models": ["w"]}]
    current["role_pools"] = {"chat": [ref("second", "w")]}
    current["model_strategies"] = {}
    assert save(client, current).is_success
    readback = Config.load(state.config_path).llm
    assert readback.providers == [{"name": "second", "base_url": "https://second.test/v1", "api_key": "secret-second", "models": ["w"]}]
    assert readback.role_pools == current["role_pools"] and not readback.model_strategies
    assert readback.targets_for("vision")[0].model == "w"


@pytest.mark.parametrize(("name", "url"), [("default", "https://changed.test"), ("renamed", "https://first.test/v1")])
def test_omitted_key_never_moves_to_changed_identity(web, name, url):
    client, state = web
    row = {"name": name, "base_url": url, "models": ["a"]}
    response = save(client, {"providers": [row], "role_pools": {"chat": [ref(name, "a")]}})
    assert response.status_code == 200, response.text
    assert Config.load(state.config_path).llm.targets_for()[0].api_key == ""
    assert "OLD_KEY" not in state.config_path.read_text()


def test_catalog_with_normalized_identity_retains_raw_key(web, monkeypatch):
    client, state = web
    monkeypatch.setenv("PROVIDER_NAME", "first")
    raw = {"llm": {"providers": [{"name": " ${PROVIDER_NAME} ", "base_url": " ${OLD_URL} ",
                                 "api_key": "${OLD_KEY}", "models": ["a"]}],
                   "role_pools": {"chat": [ref("${PROVIDER_NAME}", "a")]}}}
    state.config_path.write_text(yaml.safe_dump(raw))
    state.config = Config.load(state.config_path)
    view = client.get("/api/config").json()["structured"]["llm"]
    assert view["providers"][0]["name"] == "${PROVIDER_NAME}"
    assert save(client, view).is_success
    row = yaml.safe_load(state.config_path.read_text())["llm"]["providers"][0]
    assert row["api_key"] == "${OLD_KEY}" and row["base_url"] == " ${OLD_URL} "


def test_pool_api_refuses_ambiguous_or_dangling_edits_without_writing(web):
    client, state = web
    catalog = client.get("/api/models/pool").json()
    assert client.put("/api/models/pool", json={k: catalog[k] for k in ("providers", "role_pools")}).is_success
    original = state.config_path.read_bytes()
    old_config = state.config
    for invalid in ({"models": {"chat": "not-qualified"}}, {"role_pools": {}},
                    {"role_pools": {"chat": [ref("default", "undefined")]}}, {"providers": []}):
        response = client.put("/api/models/pool", json=invalid)
        assert response.status_code == 422, response.text
        assert state.config_path.read_bytes() == original and state.config is old_config
    invalid = yaml.safe_load(original)
    invalid["llm"]["providers"] = []
    assert client.put("/api/config", json={"text": yaml.safe_dump(invalid)}).status_code == 422
    assert state.config_path.read_bytes() == original
    assert client.put("/api/models/pool", json={"strategies": {"chat": "random"}}).is_success
    assert state.config.llm.strategy_for("chat") == "random"
