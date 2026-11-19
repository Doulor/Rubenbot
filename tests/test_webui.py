"""WebUI 后端 API 测试。

用 FastAPI 的 TestClient 直接打 app，不真正监听端口；只在启动安全校验
相关的用例里碰 uvicorn 配置。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.web.auth import (
    BindSecurityError,
    SessionSigner,
    check_bind_security,
    mask_secret,
)
from rubenbot.web.logbuffer import clear as clear_logs
from rubenbot.web.state import WebState, save_config_text

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.chat.bot_name = "Ruben"
    cfg.webui.host = "127.0.0.1"
    cfg.webui.port = 8765
    cfg.webui.password = ""
    return cfg


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "chat:\n  bot_name: Ruben\nwebui:\n  host: 127.0.0.1\n  port: 8765\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def state(config: Config, store: MemoryStore, config_path: Path) -> WebState:
    return WebState(config=config, store=store, config_path=config_path)


@pytest.fixture()
def client(state: WebState) -> TestClient:
    clear_logs()
    return TestClient(create_app(state))


@pytest.fixture()
def locked_client(config: Config, store: MemoryStore, config_path: Path) -> TestClient:
    config.webui.password = "s3cret-pass"
    state = WebState(
        config=config,
        store=store,
        config_path=config_path,
        signer=SessionSigner(config.webui.password),
    )
    return TestClient(create_app(state))


def seed(store: MemoryStore) -> None:
    now = time.time()
    store.upsert_person("snowluma", "1001", "阿花", now=now - 100)
    store.upsert_person("snowluma", "1002", "老周", now=now - 50)
    store.add_memory("snowluma:1001", "TA 在杭州做后端", category="identity", now=now - 40)
    store.add_memory("snowluma:1001", "TA 喜欢爬山", category="preference", now=now - 30)
    store.add_memory("snowluma:1002", "TA 养了一只橘猫", category="fact", now=now - 20)
    store.add_raw_message(
        platform="snowluma", chat_id="g1", sender_id="1001", sender_name="阿花", text="今天好累", ts=now - 10
    )
    store.add_raw_message(
        platform="snowluma",
        chat_id="g1",
        sender_id="__bot__",
        sender_name="Ruben",
        text="那就早点睡",
        ts=now - 9,
        processed=True,
    )
    store.add_decision_log(
        platform="snowluma",
        chat_id="g1",
        sender_name="阿花",
        sender_id="1001",
        text="今天好累",
        should_reply=False,
        reason="below_threshold",
        score=0.4,
        threshold=0.85,
        factors={"base": 0.1, "momentum": 0.3, "relevance": 0.0, "ratio_penalty": 0.0, "cooldown_penalty": 0.0},
        now=now - 5,
    )


# ----------------------------------------------------------------------
# 健康检查与登录
# ----------------------------------------------------------------------


def test_health_open_without_auth(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["auth_required"] is False
    assert data["loopback_only"] is True


def test_api_requires_session_when_password_set(locked_client):
    response = locked_client.get("/api/memories")
    assert response.status_code == 401
    assert response.json()["auth_required"] is True


def test_login_wrong_password_then_success(locked_client):
    wrong = locked_client.post("/api/login", json={"password": "nope"})
    assert wrong.status_code == 401
    assert locked_client.get("/api/memories").status_code == 401

    ok = locked_client.post("/api/login", json={"password": "s3cret-pass"})
    assert ok.status_code == 200
    assert "rubenbot_session" in locked_client.cookies
    assert locked_client.get("/api/memories").status_code == 200

    # 退出后再访问需要重新登录
    locked_client.post("/api/logout")
    assert locked_client.get("/api/memories").status_code == 401


def test_me_reports_state(locked_client, client):
    assert locked_client.get("/api/me").json()["authenticated"] is False
    assert client.get("/api/me").json()["authenticated"] is True


def test_non_loopback_bind_requires_password():
    cfg = Config()
    cfg.webui.host = "0.0.0.0"
    cfg.webui.password = ""
    with pytest.raises(BindSecurityError):
        check_bind_security(cfg.webui)

    cfg.webui.password = "x"
    check_bind_security(cfg.webui)  # 不抛异常

    cfg.webui.host = "localhost"
    cfg.webui.password = ""
    check_bind_security(cfg.webui)  # 本地名不需要口令

    cfg.webui.host = "::1"
    check_bind_security(cfg.webui)  # IPv6 回环


def test_create_app_refuses_non_loopback_without_password(store, config, tmp_path):
    config.webui.host = "192.168.1.10"
    config.webui.password = ""
    state = WebState(config=config, store=store, config_path=tmp_path / "config.yaml")
    with pytest.raises(BindSecurityError):
        create_app(state)


def test_session_signer_roundtrip_and_tamper():
    signer = SessionSigner("pw", ttl_hours=1)
    token = signer.issue(now=1000.0)
    session = signer.parse(token, now=1001.0)
    assert session is not None and session.user == "admin"
    assert signer.parse(token, now=1000.0 + 3600 * 2) is None  # 过期

    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert signer.parse(tampered, now=1001.0) is None

    other = SessionSigner("pw", ttl_hours=1)  # 独立密钥
    assert other.parse(token, now=1001.0) is None

    assert signer.verify_password("pw") is True
    assert signer.verify_password("PW") is False


# ----------------------------------------------------------------------
# 概览
# ----------------------------------------------------------------------


def test_overview_counts(client, store):
    seed(store)
    data = client.get("/api/overview").json()
    assert data["ok"] is True
    counts = data["counts"]
    assert counts["memories"] == 3
    assert counts["messages"] == 2
    assert counts["replies"] == 1
    assert counts["chats"] == 1
    assert counts["people"] == 2
    assert counts["decisions"] == 1
    assert len(data["recent_decisions"]) == 1
    assert data["recent_decisions"][0]["reason"] == "below_threshold"
    assert data["webui"]["host"] == "127.0.0.1"
    assert data["bot"]["running"] is False  # 没有 bot 实例


def test_overview_with_bot_instance(client, store, config):
    class FakeBot:
        def __init__(self):
            self.started_at = time.time() - 120
            self.last_message_at = time.time() - 5
            self.adapters = {}
            self.config = config

    state = client.app.state.web
    state.bot = FakeBot()
    data = client.get("/api/overview").json()
    assert data["bot"]["running"] is True
    assert data["bot"]["uptime_seconds"] >= 100


# ----------------------------------------------------------------------
# 记忆
# ----------------------------------------------------------------------


def test_memory_list_search_and_filter(client, store):
    seed(store)
    data = client.get("/api/memories").json()
    assert data["total"] == 3
    assert len(data["items"]) == 3

    search = client.get("/api/memories", params={"q": "杭州"}).json()
    assert search["total"] == 1
    assert "杭州" in search["items"][0]["content"]

    by_person = client.get("/api/memories", params={"person_id": "snowluma:1001"}).json()
    assert by_person["total"] == 2

    paged = client.get("/api/memories", params={"limit": 1, "offset": 1}).json()
    assert len(paged["items"]) == 1
    assert paged["total"] == 3


def test_memory_search_escapes_like_wildcards(client, store):
    store.add_memory("p:1", "100% 确定的事")
    store.add_memory("p:1", "普通记忆")
    data = client.get("/api/memories", params={"q": "%"}).json()
    assert data["total"] == 1


def test_memory_create_update_delete_with_log(client, store):
    seed(store)
    created = client.post(
        "/api/memories",
        json={"person_id": "snowluma:1002", "content": "TA 最近在学摄影", "category": "preference"},
    )
    assert created.status_code == 200
    memory = created.json()["item"]
    assert memory["source"] == "user"
    assert memory["scope_type"] == "global"

    updated = client.put(f"/api/memories/{memory['id']}", json={"content": "TA 在学胶片摄影"}).json()
    assert updated["item"]["content"] == "TA 在学胶片摄影"

    deleted = client.delete(f"/api/memories/{memory['id']}")
    assert deleted.status_code == 200
    log_id = deleted.json()["log_id"]
    assert store.get_memory(memory["id"]) is None

    log = client.get("/api/memory-log").json()["items"]
    assert log[0]["id"] == log_id
    assert log[0]["action"] == "drop"
    assert log[0]["before"][0]["content"] == "TA 在学胶片摄影"

    restored = client.post(f"/api/memory-log/{log_id}/restore")
    assert restored.status_code == 200
    assert store.get_memory(memory["id"]) is not None
    # 重复撤销应报冲突
    assert client.post(f"/api/memory-log/{log_id}/restore").status_code == 409


def test_memory_validation(client, store):
    seed(store)
    assert client.post("/api/memories", json={"person_id": "p", "content": ""}).status_code == 422
    assert client.post("/api/memories", json={"person_id": "", "content": "x"}).status_code == 422
    assert (
        client.post("/api/memories", json={"person_id": "p", "content": "x", "category": "bogus"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/memories", json={"person_id": "p", "content": "x", "scope_type": "group"}
        ).status_code
        == 422
    )
    assert client.put("/api/memories/99999", json={"content": "x"}).status_code == 404
    assert client.delete("/api/memories/99999").status_code == 404
    assert client.post("/api/memory-log/99999/restore").status_code == 404


def test_memory_group_scope(client, store):
    seed(store)
    created = client.post(
        "/api/memories",
        json={"person_id": "snowluma:1001", "content": "这个群里 TA 负责组织活动", "scope_type": "group", "scope_id": "g1"},
    ).json()["item"]
    assert created["scope_id"] == "g1"
    filtered = client.get("/api/memories", params={"scope_type": "group"}).json()
    assert filtered["total"] == 1


# ----------------------------------------------------------------------
# 人物
# ----------------------------------------------------------------------


def test_people_list_and_detail(client, store):
    seed(store)
    people = client.get("/api/people").json()["items"]
    assert len(people) == 2
    by_id = {p["person_id"]: p for p in people}
    assert by_id["snowluma:1001"]["memory_count"] == 2
    assert by_id["snowluma:1002"]["display_name"] == "老周"

    detail = client.get("/api/people/snowluma:1001").json()
    assert detail["person"]["display_name"] == "阿花"
    assert len(detail["memories"]) == 2

    assert client.get("/api/people/snowluma:404").status_code == 404


def test_people_search(client, store):
    seed(store)
    data = client.get("/api/people", params={"q": "老周"}).json()
    assert len(data["items"]) == 1


# ----------------------------------------------------------------------
# 群组与意愿
# ----------------------------------------------------------------------


def test_chats_include_decision_summary(client, store):
    seed(store)
    data = client.get("/api/chats").json()
    assert len(data["items"]) == 1
    chat = data["items"][0]
    assert chat["chat_id"] == "g1"
    assert chat["message_count"] == 2
    assert len(chat["recent_decisions"]) == 1


def test_get_and_put_group_decision(client, store, config, config_path):
    seed(store)
    initial = client.get("/api/chats/g1/decision").json()
    assert initial["configured"] is False
    assert initial["effective"]["base"] == config.decision.base

    saved = client.put(
        "/api/chats/g1/decision",
        json={"activity_multiplier": 1.5, "base": 0.2, "quiet_hours": ["23:00-07:30"]},
    )
    assert saved.status_code == 200
    assert saved.json()["restart_required"] is False

    # 写进了配置文件，而且保留了其它段
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["decision"]["groups"]["g1"]["activity_multiplier"] == 1.5
    assert data["decision"]["groups"]["g1"]["base"] == 0.2
    assert data["decision"]["groups"]["g1"]["quiet_hours"] == ["23:00-07:30"]
    assert data["chat"]["bot_name"] == "Ruben"

    # 内存配置同步生效
    assert config.decision.groups["g1"].activity_multiplier == 1.5

    current = client.get("/api/chats/g1/decision").json()
    assert current["configured"] is True
    assert current["effective"]["base"] == 0.2

    # 恢复默认
    removed = client.put("/api/chats/g1/decision", json={"remove": True}).json()
    assert removed["configured"] is False
    assert "g1" not in config.decision.groups


def test_group_decision_validation(client):
    for payload in (
        {"activity_multiplier": "abc"},
        {"activity_multiplier": 99},
        {"base": 2},
        {"quiet_hours": ["25:00-30:00"]},
        {"quiet_hours": "23:00-07:00"},  # 单字符串也接受
    ):
        response = client.put("/api/chats/g1/decision", json=payload)
        if payload == {"quiet_hours": "23:00-07:00"}:
            assert response.status_code == 200
        else:
            assert response.status_code == 422, payload


def test_decisions_list_and_filter(client, store):
    seed(store)
    store.add_decision_log(platform="snowluma", chat_id="g2", text="另一个群", should_reply=True, score=0.9, threshold=0.85)
    all_items = client.get("/api/decisions").json()["items"]
    assert len(all_items) == 2
    only_g1 = client.get("/api/decisions", params={"chat_id": "g1"}).json()["items"]
    assert len(only_g1) == 1
    assert only_g1[0]["factors"]["base"] == 0.1


# ----------------------------------------------------------------------
# 插件
# ----------------------------------------------------------------------


def test_plugins_toggle_runtime(client, store, config):
    from rubenbot.plugins.loader import PluginManager

    plugins = PluginManager(config, store)
    state = client.app.state.web
    state.plugins = plugins
    # 手工塞一个已加载的插件，不碰文件系统
    from rubenbot.plugins.loader import Plugin

    plugins.plugins["demo"] = Plugin(name="demo", path=Path("/tmp/demo"), enabled=True)

    listing = client.get("/api/plugins").json()["items"]
    assert listing[0]["name"] == "demo"
    assert listing[0]["enabled"] is True

    plugins.plugins["demo"].loaded = True

    disabled = client.post("/api/plugins/demo", json={"enabled": False}).json()
    assert disabled["items"][0]["enabled"] is False

    enabled = client.post("/api/plugins/demo", json={"enabled": True}).json()
    assert enabled["items"][0]["enabled"] is True

    assert client.post("/api/plugins/nope", json={"enabled": True}).status_code == 404


def test_plugins_without_manager(client):
    state = client.app.state.web
    state.plugins = None
    assert client.get("/api/plugins").json()["items"] == []
    assert client.post("/api/plugins/demo", json={"enabled": True}).status_code == 503


# ----------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------


def test_logs_endpoint(client):
    import logging

    from rubenbot.web.logbuffer import install

    install()
    logging.getLogger("rubenbot.test").warning("一条测试日志")
    data = client.get("/api/logs", params={"q": "测试日志"}).json()
    assert any("一条测试日志" in item["message"] for item in data["items"])

    warnings_only = client.get("/api/logs", params={"level": "WARNING", "q": "测试日志"}).json()
    assert warnings_only["items"]
    infos = client.get("/api/logs", params={"level": "ERROR", "q": "测试日志"}).json()
    assert infos["items"] == []


# ----------------------------------------------------------------------
# 设置
# ----------------------------------------------------------------------


def test_get_and_put_config(client, config_path, state):
    data = client.get("/api/config").json()
    assert data["path"] == str(config_path)
    assert "bot_name" in data["text"]
    assert data["effective"]["chat"]["bot_name"] == "Ruben"

    new_text = "chat:\n  bot_name: 小R\n"
    saved = client.put("/api/config", json={"text": new_text})
    assert saved.status_code == 200
    assert saved.json()["restart_required"] is True
    assert config_path.read_text(encoding="utf-8") == new_text
    # 留了备份
    backup = config_path.with_name(config_path.name + ".bak")
    assert backup.exists()
    assert "bot_name: Ruben" in backup.read_text(encoding="utf-8")


def test_put_config_rejects_invalid_yaml(client, config_path):
    original = config_path.read_text(encoding="utf-8")
    bad = client.put("/api/config", json={"text": "a: [unclosed"})
    assert bad.status_code == 422
    assert config_path.read_text(encoding="utf-8") == original

    not_mapping = client.put("/api/config", json={"text": "- a\n- b\n"})
    assert not_mapping.status_code == 422


def test_save_config_text_creates_missing_file(tmp_path):
    target = tmp_path / "nested" / "config.yaml"
    backup = save_config_text(target, "chat: {}\n")
    assert backup is None
    assert target.read_text(encoding="utf-8") == "chat: {}\n"


def test_config_masks_secrets(client, config_path, store, state):
    state.config.llm.api_key = "sk-abcdefghijklmn"
    state.config.webui.password = "hunter2"
    data = client.get("/api/config").json()
    assert data["effective"]["llm"]["api_key"] != "sk-abcdefghijklmn"
    assert data["effective"]["llm"]["api_key"].startswith("sk")
    assert data["effective"]["webui"]["password"] == ""
    assert "hunter2" not in str(data["effective"])


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("ab") == "**"
    assert mask_secret("abcdefgh") == "ab****gh"


# ----------------------------------------------------------------------
# 静态页
# ----------------------------------------------------------------------


def test_index_serves_spa(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "RubenBot" in response.text
    assert "/static/app.js" in response.text


def test_static_assets_served(client):
    for asset in ("/static/app.js", "/static/styles.css", "/static/fonts.css", "/static/favicon.svg"):
        response = client.get(asset)
        assert response.status_code == 200, asset


def test_unauthorized_api_call_does_not_leak_html(locked_client):
    response = locked_client.get("/api/overview")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
