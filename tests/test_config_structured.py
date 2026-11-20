"""设置页结构化配置：校验、往返、密钥保留、热生效与重启接口。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.web.auth import SessionSigner
from rubenbot.web.state import WebState, restart_fields

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.config_schema import (  # noqa: E402
    SECRET_PATHS,
    ConfigValidationError,
    merge_config,
    structured_config,
    validate_structured,
)

from conftest import ScriptedLLM  # noqa: E402


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def persona_path(tmp_path: Path) -> Path:
    path = tmp_path / "SOUL.md"
    path.write_text("# Ruben\n\n你是 Ruben，随便聊聊。\n", encoding="utf-8")
    return path


@pytest.fixture()
def config(tmp_path: Path, persona_path: Path) -> Config:
    cfg = Config()
    cfg.persona_path = str(persona_path)
    cfg.database_path = str(tmp_path / "rubenbot.db")
    cfg.llm.api_key = "sk-original-key"
    cfg.llm.chat_model = "model-old"
    cfg.decision.threshold = 0.85
    cfg.memory.inject_limit = 20
    cfg.adapters = {"console": {}}
    return cfg


@pytest.fixture()
def config_path(tmp_path: Path, config: Config) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  api_key: sk-original-key\n  chat_model: model-old\n"
        f"persona:\n  path: {config.persona_path}\n"
        f"database_path: {config.database_path}\n"
        "decision:\n  threshold: 0.85\n"
        "adapters:\n  console: {}\n"
        "webui:\n  host: 127.0.0.1\n  port: 8765\n"
        "unknown_section:\n  keep_me: true\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def state(config: Config, store: MemoryStore, config_path: Path) -> WebState:
    return WebState(config=config, store=store, config_path=config_path)


@pytest.fixture()
def client(state: WebState) -> TestClient:
    return TestClient(create_app(state))


@pytest.fixture()
def bot_state(config: Config, store: MemoryStore, config_path: Path, persona_path: Path) -> WebState:
    bot = RubenBot(config, store, ScriptedLLM(), Persona(name="Ruben", text="你是 Ruben。"))
    return WebState.from_bot(bot, config_path)


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 结构化视图与校验
# ----------------------------------------------------------------------


def test_structured_config_covers_every_field(config):
    view = structured_config(config)
    for section, keys in {
        "llm": [
            "base_url",
            "chat_model",
            "extract_model",
            "consolidate_model",
            "models",
            "temperature",
            "timeout",
            "max_retries",
        ],
        "memory": [
            "extract_every_n",
            "extract_silence_seconds",
            "extract_min_pending",
            "extract_window",
            "inject_limit",
            "dedupe_threshold",
            "consolidate_new_threshold",
            "consolidate_interval_hours",
            "consolidate_max_memories",
            "consolidate_remove_ratio",
            "consolidate_remove_min",
            "raw_keep_per_chat",
        ],
        "chat": [
            "bot_name",
            "history_size",
            "split_max_parts",
            "split_part_chars",
            "type_speed",
            "type_delay_max_seconds",
            "strip_action_parens",
            "history_ttl_hours",
        ],
        "decision": [
            "merge_window_seconds",
            "hard_merge_window_seconds",
            "batch_max_seconds",
            "urgent_seconds",
            "aliases",
            "base",
            "threshold",
            "threshold_noise",
            "threshold_noise_band",
            "momentum_bonus",
            "momentum_half_life_seconds",
            "relevance_bonus",
            "relevance_scale",
            "ratio_window",
            "ratio_threshold",
            "ratio_silence",
            "ratio_penalty",
            "cooldown_seconds",
            "cooldown_penalty",
            "veto_marker",
            "groups",
        ],
        "plugins": ["dir", "enabled", "command_prefix"],
        "webui": ["enabled", "host", "port", "session_hours"],
    }.items():
        for key in keys:
            assert key in view[section], (section, key)
    assert view["persona"]["path"] == config.persona_path
    assert view["database_path"] == config.database_path
    assert view["background_check_minutes"] == config.background_check_minutes
    assert view["log_level"] == config.log_level
    assert set(view["adapters"]) == {"console", "snowluma"}
    # 密钥一律留空，只用标记说明是否已设置
    assert view["llm"]["api_key"] == ""
    assert view["webui"]["password"] == ""
    assert view["adapters"]["snowluma"]["access_token"] == ""
    assert view["secrets"]["llm.api_key"] is True


def test_validate_structured_models_map():
    update = validate_structured(
        {"llm": {"models": {"fast": " m-fast ", "summary": "", "vision": None, "": "x"}}}
    )
    assert update["llm"]["models"] == {"fast": "m-fast"}
    with pytest.raises(ConfigValidationError):
        validate_structured({"llm": {"models": ["fast"]}})
    with pytest.raises(ConfigValidationError):
        validate_structured({"llm": {"models": {"fast": 3}}})


def test_put_structured_models_map_written_and_resent_unchanged(client, config_path):
    response = client.put(
        "/api/config",
        json={"config": {"llm": {"models": {"fast": "m-fast", "vision": "m-vision"}}}},
    )
    assert response.status_code == 200, response.text
    assert read_yaml(config_path)["llm"]["models"] == {"fast": "m-fast", "vision": "m-vision"}

    # 原样再存一次不应报变化
    view = client.get("/api/config").json()["structured"]
    view.pop("secrets")
    again = client.put("/api/config", json={"config": view})
    assert again.status_code == 200, again.text
    assert again.json()["applied"] == []


def test_validate_structured_accepts_string_numbers():
    update = validate_structured(
        {
            "llm": {"temperature": "0.5", "max_retries": "3"},
            "memory": {"inject_limit": "12"},
            "decision": {"threshold": "0.7", "ratio_window": "40"},
            "webui": {"port": "9000", "enabled": True},
            "log_level": "debug",
        }
    )
    assert update["llm"]["temperature"] == 0.5
    assert update["llm"]["max_retries"] == 3
    assert update["memory"]["inject_limit"] == 12
    assert update["decision"]["threshold"] == 0.7
    assert update["webui"]["port"] == 9000
    assert update["log_level"] == "DEBUG"


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"llm": {"temperature": 3}}, "llm.temperature"),
        ({"decision": {"threshold": 1.5}}, "decision.threshold"),
        ({"decision": {"ratio_threshold": -0.1}}, "decision.ratio_threshold"),
        ({"memory": {"inject_limit": -1}}, "memory.inject_limit"),
        ({"webui": {"port": 70000}}, "webui.port"),
        ({"log_level": "LOUD"}, "log_level"),
        ({"chat": {"split_max_parts": 0}}, "chat.split_max_parts"),
    ],
)
def test_validate_structured_rejects_bad_values(payload, field):
    with pytest.raises(ConfigValidationError) as info:
        validate_structured(payload)
    assert any(item["field"] == field for item in info.value.fields)


def test_quiet_hours_validation():
    update = validate_structured(
        {"decision": {"groups": {"g1": {"quiet_hours": ["23:00-07:30"]}}}}
    )
    assert update["decision"]["groups"]["g1"]["quiet_hours"] == ["23:00-07:30"]
    with pytest.raises(ConfigValidationError):
        validate_structured({"decision": {"groups": {"g1": {"quiet_hours": ["25:00-30:00"]}}}})


def test_merge_config_keeps_unknown_keys():
    merged = merge_config(
        {"chat": {"bot_name": "Ruben", "extra": 1}, "custom": {"a": 2}},
        {"chat": {"bot_name": "小R"}},
    )
    assert merged["chat"] == {"bot_name": "小R", "extra": 1}
    assert merged["custom"] == {"a": 2}


# ----------------------------------------------------------------------
# 结构化 PUT
# ----------------------------------------------------------------------


def test_put_structured_roundtrip_and_hot_fields(client, config, config_path, state):
    response = client.put(
        "/api/config",
        json={
            "config": {
                "llm": {"chat_model": "model-new"},
                "decision": {"threshold": 0.42},
                "memory": {"inject_limit": 7},
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "structured"
    assert body["restart_required"] is False
    assert set(body["applied"]) == {"llm", "decision", "memory"}
    assert body["resolution"]["llm.chat_model"] == "hot"
    assert body["resolution"]["decision.threshold"] == "hot"

    # 文件写回、未知键保留
    data = read_yaml(config_path)
    assert data["llm"]["chat_model"] == "model-new"
    assert data["llm"]["api_key"] == "sk-original-key"
    assert data["decision"]["threshold"] == 0.42
    assert data["memory"]["inject_limit"] == 7
    assert data["unknown_section"] == {"keep_me": True}

    # 重新加载配置一致
    reloaded = Config.load(config_path)
    assert reloaded.llm.chat_model == "model-new"
    assert reloaded.decision.threshold == 0.42
    assert reloaded.memory.inject_limit == 7

    # 进程内配置也已同步（无 bot 时至少 state.config 更新）
    assert state.config.llm.chat_model == "model-new"
    assert state.config.decision.threshold == 0.42


def test_put_structured_secrets_blank_keeps_original(client, config_path):
    response = client.put(
        "/api/config",
        json={
            "config": {
                "llm": {"api_key": "", "chat_model": "m2"},
                "webui": {"password": ""},
                "adapters": {"snowluma": {"access_token": "", "enabled": False}},
            }
        },
    )
    assert response.status_code == 200, response.text
    data = read_yaml(config_path)
    assert data["llm"]["api_key"] == "sk-original-key"
    # 没提交过 password 就不写进文件
    assert "password" not in (data.get("webui") or {})


def test_search_section_structured_view_and_secret(config):
    """搜索分组出现在表单视图里；两个渠道的 key 按密码型处理（留空 + secrets 标记）。"""
    config.search.tavily.api_key = "tvly-search"
    config.search.deepseek.api_key = "sk-search"
    view = structured_config(config)
    assert view["search"]["order"] == ["tavily", "deepseek"]
    assert view["search"]["timeout_seconds"] == 10.0
    assert view["search"]["cooldown_seconds"] == 30.0
    assert view["search"]["tavily"]["api_key"] == ""
    assert view["search"]["deepseek"]["api_key"] == ""
    assert view["secrets"]["search.tavily.api_key"] is True
    assert view["secrets"]["search.deepseek.api_key"] is True
    assert "search.tavily.api_key" in SECRET_PATHS
    assert "search.deepseek.api_key" in SECRET_PATHS


def test_put_structured_search_section(client, config_path, state):
    response = client.put(
        "/api/config",
        json={
            "config": {
                "search": {
                    "order": ["deepseek", "tavily"],
                    "tavily": {"api_key": "tvly-key"},
                    "deepseek": {"api_key": "sk-ds-key"},
                    "timeout_seconds": "5",
                    "cooldown_seconds": "0",
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    data = read_yaml(config_path)
    assert data["search"] == {
        "order": ["deepseek", "tavily"],
        "tavily": {"api_key": "tvly-key"},
        "deepseek": {"api_key": "sk-ds-key"},
        "timeout_seconds": 5.0,
        "cooldown_seconds": 0.0,
    }
    assert state.config.search.order == ["deepseek", "tavily"]
    assert state.config.search.tavily.api_key == "tvly-key"
    assert state.config.search.deepseek.api_key == "sk-ds-key"

    # 空 key 表示保留原值，不会把文件里的 key 清掉
    again = client.put(
        "/api/config",
        json={
            "config": {
                "search": {
                    "order": "tavily, deepseek",
                    "tavily": {"api_key": ""},
                    "deepseek": {"api_key": ""},
                }
            }
        },
    )
    assert again.status_code == 200, again.text
    data = read_yaml(config_path)
    assert data["search"]["order"] == ["tavily", "deepseek"]
    assert data["search"]["tavily"]["api_key"] == "tvly-key"
    assert data["search"]["deepseek"]["api_key"] == "sk-ds-key"


def test_validate_search_rejects_unknown_channel():
    with pytest.raises(ConfigValidationError) as info:
        validate_structured({"search": {"order": ["tavily", "bing"]}})
    assert any(item["field"] == "search.order" for item in info.value.fields)
    with pytest.raises(ConfigValidationError) as info:
        validate_structured({"search": {"order": []}})
    assert any(item["field"] == "search.order" for item in info.value.fields)
    with pytest.raises(ConfigValidationError) as info:
        validate_structured({"search": {"cooldown_seconds": -1}})
    assert any(item["field"] == "search.cooldown_seconds" for item in info.value.fields)


def test_put_structured_masked_placeholder_not_written(client, config_path, config, state):
    config.webui.password = "hunter2"
    data = read_yaml(config_path)
    data.setdefault("webui", {})["password"] = "hunter2"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    masked = mask_for_test(config.webui.password)
    response = client.put(
        "/api/config",
        json={"config": {"webui": {"password": masked, "session_hours": 12}}},
    )
    assert response.status_code == 200, response.text
    written = read_yaml(config_path)
    assert written["webui"]["password"] == "hunter2"
    assert written["webui"]["session_hours"] == 12
    assert state.config.webui.password == "hunter2"


def mask_for_test(value: str) -> str:
    from rubenbot.web.auth import mask_secret

    return mask_secret(value)


def test_put_structured_invalid_returns_422_with_fields_and_no_write(client, config_path):
    original = config_path.read_text(encoding="utf-8")
    response = client.put(
        "/api/config",
        json={"config": {"llm": {"temperature": 9}, "decision": {"threshold": 2}}},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    fields = {item["field"] for item in body["fields"]}
    assert {"llm.temperature", "decision.threshold"} <= fields
    assert config_path.read_text(encoding="utf-8") == original


def test_put_structured_refuses_non_loopback_without_password(client, config_path):
    original = config_path.read_text(encoding="utf-8")
    response = client.put("/api/config", json={"config": {"webui": {"host": "0.0.0.0"}}})
    assert response.status_code == 422
    assert config_path.read_text(encoding="utf-8") == original


def test_put_structured_requires_restart_fields(client, config_path, state):
    response = client.put(
        "/api/config",
        json={
            "config": {
                "database_path": "/tmp/another.db",
                "persona": {"path": "/tmp/other-persona.md"},
                "adapters": {"console": None},
                "webui": {"port": 9999},
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restart_required"] is True
    assert {"database_path", "persona.path", "adapters.console", "webui.port"} <= set(
        body["requires_restart"]
    )
    assert "database_path" not in body["applied"]
    # 保存后页面会持续提示这些字段等待重启
    pending = client.get("/api/config").json()["restart_fields"]
    assert {"database_path", "persona.path", "adapters.console", "webui.port"} <= set(pending)


def test_resaving_form_untouched_reports_no_changes(client, config_path):
    """打开设置原样保存：不应误报「已生效」或把值写歪。"""
    view = client.get("/api/config").json()["structured"]
    view.pop("secrets")
    response = client.put("/api/config", json={"config": view})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == []
    assert body["requires_restart"] == []
    assert body["relogin"] is False
    # 密钥仍然保留在文件里
    assert read_yaml(config_path)["llm"]["api_key"] == "sk-original-key"

    # 再原样存一次同样无变化（第一次保存后表单视图应与提交的一致）
    again = body["structured"]
    again.pop("secrets")
    second = client.put("/api/config", json={"config": again})
    assert second.status_code == 200
    assert second.json()["applied"] == []
    assert second.json()["message"] == "配置已保存（与当前一致，无需变更）"


def test_group_override_change_is_reported_applied(client, config_path):
    initial = client.get("/api/config").json()["structured"]["decision"]["groups"]
    assert initial == {}
    response = client.put(
        "/api/config",
        json={"config": {"decision": {"groups": {"g9": {"activity_multiplier": 2}}}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution"]["decision.groups"] == "hot"
    assert "decision" in response.json()["applied"]
    written = read_yaml(config_path)
    assert written["decision"]["groups"]["g9"]["activity_multiplier"] == 2.0

    # 删除群覆盖也算改动，且文件里真的删掉了
    response = client.put("/api/config", json={"config": {"decision": {"groups": {}}}})
    assert response.status_code == 200
    assert "decision" in response.json()["applied"]
    assert "g9" not in read_yaml(config_path)["decision"]["groups"]


def test_legacy_text_mode_still_works(client, config_path):
    response = client.put("/api/config", json={"text": "chat:\n  bot_name: 小R\n"})
    assert response.status_code == 200
    assert response.json()["mode"] == "text"
    assert read_yaml(config_path) == {"chat": {"bot_name": "小R"}}


# ----------------------------------------------------------------------
# 热生效
# ----------------------------------------------------------------------


def test_hot_apply_updates_decision_engine_and_model(bot_state):
    config = bot_state.config
    bot = bot_state.bot
    assert bot.decision_engine.config.threshold == 0.85

    client = TestClient(create_app(bot_state))
    response = client.put(
        "/api/config",
        json={
            "config": {
                "llm": {"chat_model": "model-hot"},
                "decision": {"threshold": 0.0, "base": 0.0, "threshold_noise": 0.0},
                "memory": {"inject_limit": 3},
                "chat": {"bot_name": "小R"},
            }
        },
    )
    assert response.status_code == 200, response.text

    # 意愿引擎参数已更新，且下一次普通群消息就能通过（阈值 0）
    assert bot.decision_engine.config.threshold == 0.0
    assert bot.extractor.config.inject_limit == 3
    assert bot.config.memory.inject_limit == 3
    assert bot.config.chat.bot_name == "小R"

    event = make_group_event()
    decision = bot.decide(event, now=time.time())
    assert decision.should_reply is True

    # 换了 llm 配置后客户端重建，模型名更新
    from rubenbot.llm import LLMClient

    assert isinstance(bot.llm, LLMClient)
    assert bot.llm.config.chat_model == "model-hot"


def test_hot_apply_keeps_fake_llm_when_llm_unchanged(bot_state, store):
    bot = bot_state.bot
    fake = bot.llm
    client = TestClient(create_app(bot_state))
    response = client.put("/api/config", json={"config": {"decision": {"threshold": 0.3}}})
    assert response.status_code == 200
    assert bot.llm is fake
    assert bot.decision_engine.config.threshold == 0.3


def make_group_event():
    from rubenbot.adapters.base import MessageEvent

    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text="今天天气不错",
    )


# ----------------------------------------------------------------------
# 重启
# ----------------------------------------------------------------------


def test_restart_requires_session(config, store, config_path):
    config.webui.password = "pw"
    state = WebState(
        config=config,
        store=store,
        config_path=config_path,
        signer=SessionSigner("pw"),
    )
    client = TestClient(create_app(state))
    response = client.post("/api/restart")
    assert response.status_code == 401


def test_restart_without_password_uses_hook(client, state):
    calls = []
    state.extras["restart_hook"] = calls.append
    response = client.post("/api/restart")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert calls and calls[0] == body["command"]
    # 命令里带着原启动参数（或回退成 -m rubenbot）
    assert body["command"]


def test_restart_command_preserves_arguments(tmp_path, config, store):
    state = WebState(
        config=config,
        store=store,
        config_path=tmp_path / "config.yaml",
        argv=["/usr/bin/rubenbot", "-c", "my.yaml", "--webui-only"],
        executable="/usr/bin/rubenbot",
    )
    assert state.restart_command() == [
        "/usr/bin/rubenbot",
        "-c",
        "my.yaml",
        "--webui-only",
    ]


def test_restart_command_for_module_launch(tmp_path, config, store):
    state = WebState(
        config=config,
        store=store,
        config_path=tmp_path / "config.yaml",
        argv=["/opt/rubenbot/rubenbot/__main__.py", "-c", "x.yaml"],
        executable="/usr/bin/python3",
    )
    assert state.restart_command() == ["/usr/bin/python3", "-m", "rubenbot", "-c", "x.yaml"]


def test_restart_command_resolves_launcher_script(tmp_path, config, store, monkeypatch):
    """venv 的 rubenbot 启动器是 Python 脚本，必须用解释器执行。"""
    launcher = tmp_path / "bin" / "rubenbot"
    launcher.parent.mkdir()
    launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    state = WebState(
        config=config,
        store=store,
        config_path=tmp_path / "config.yaml",
        argv=[str(launcher), "-c", "x.yaml"],
        executable="/usr/bin/python3",
    )
    assert state.restart_command() == [
        "/usr/bin/python3",
        str(launcher),
        "-c",
        "x.yaml",
    ]


def test_restart_fields_detects_bind_changes():
    old = Config.from_dict({"adapters": {"console": {}}})
    new = Config.from_dict({"adapters": {"console": {}, "snowluma": {"ws_url": "ws://x"}}})
    fields = restart_fields(old, new)
    assert "adapters.snowluma" in fields
    assert "adapters.console" not in fields

    new.webui.port = 9000
    assert "webui.port" in restart_fields(old, new)

    # 写了默认值不算改动（旧配置里本来就启用了 snowluma）
    base = Config.from_dict(
        {
            "adapters": {
                "console": {},
                "snowluma": {"ws_url": "ws://127.0.0.1:3001", "http_url": "http://127.0.0.1:3000"},
            }
        }
    )
    same = Config.from_dict(
        {
            "adapters": {
                "console": {},
                "snowluma": {
                    "ws_url": "ws://127.0.0.1:3001",
                    "http_url": "http://127.0.0.1:3000",
                    "access_token": "",
                },
            }
        }
    )
    assert "adapters.snowluma" not in restart_fields(base, same)


def test_get_config_reports_pending_restart(client, config_path, state):
    # 磁盘上写入了端口，但进程内仍是旧值
    data = read_yaml(config_path)
    data["webui"] = {"port": 9999, "host": "127.0.0.1"}
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    body = client.get("/api/config").json()
    assert "webui.port" in body["restart_fields"]
    assert body["structured"]["llm"]["chat_model"] == "model-old"
