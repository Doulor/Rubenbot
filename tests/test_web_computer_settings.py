"""操作电脑设置的授权默认值、表单保存及只读审计。"""

from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.web.auth import SessionSigner
from rubenbot.web.state import WebState


def test_default_owner_matches_local_console():
    event = ConsoleAdapter().parse_line("测试")
    cfg = Config().computer
    assert not cfg.enabled
    assert cfg.whitelist == [f"{event.platform}:{event.sender_id}"] == ["console:console:me"]
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    assert Config.load(example).computer == cfg
    assert Config.from_raw({"computer": {"whitelist": []}}).computer.whitelist == []


@pytest.fixture
def web(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rubenbot.web.app import create_app

    store = MemoryStore(":memory:")
    path = tmp_path / "config.yaml"
    path.write_text("computer:\n  whitelist: ['telegram:123']\n", encoding="utf-8")
    state = WebState(Config.load(path), store, path)
    try:
        with TestClient(create_app(state)) as client:
            yield client, state
    finally:
        store.close()


def test_form_round_trip_preserves_omissions_and_clears_list(web):
    client, state = web
    view = client.get("/api/config").json()
    assert view["structured"]["computer"] == asdict(state.config.computer)
    assert view["structured"]["computer"]["whitelist"] == ["telegram:123"]
    response = client.put("/api/config", json={"config": {"computer": {
        "enabled": True, "max_task_rounds": "5", "command_timeout_seconds": "20", "sandbox_quota_mb": "25", "output_limit_bytes": "4096",
    }}})
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == ["computer"]
    expected = {"enabled": True, "whitelist": ["telegram:123"], "command_timeout_seconds": 20,
                "sandbox_quota_mb": 25, "output_limit_bytes": 4096, "max_task_rounds": 5}
    assert asdict(Config.load(state.config_path).computer) == expected
    assert response.json()["structured"]["computer"] == expected
    assert client.put("/api/config", json={"config": {"computer": {"whitelist": []}}}).status_code == 200
    assert client.get("/api/config").json()["structured"]["computer"]["whitelist"] == []
    assert Config.load(state.config_path).computer.whitelist == []


@pytest.mark.parametrize(("field", "value"), [
    ("enabled", "true"), ("whitelist", None), ("whitelist", ["telegram:*"]),
    ("whitelist", ["telegram:${ACCOUNT}"]), ("whitelist", ["123"]),
    ("max_task_rounds", 0), ("max_task_rounds", 17), ("output_limit_bytes", 31), ("command_timeout_seconds", True), ("sandbox_quota_mb", 10241),
])
def test_invalid_authorization_and_limits_never_written(web, monkeypatch, field, value):
    client, state = web
    monkeypatch.setenv("ACCOUNT", "sensitive-account-value")
    old = state.config_path.read_bytes()
    data = {"computer": {field: value}}
    with pytest.raises(ValueError, match=rf"computer\.{field}"):
        Config.from_raw(data)
    for body in ({"config": data}, {"text": yaml.safe_dump(data)}):
        response = client.put("/api/config", json=body)
        assert response.status_code == 422
        assert f"computer.{field}" in response.text
        assert "sensitive-account-value" not in response.text
        assert state.config_path.read_bytes() == old


def test_readonly_audit_stays_available_when_disabled(web):
    client, state = web
    url = "/api/computer/audit"
    assert client.get(url).json()["items"] == []
    summary = {"arg_count": 1, "input_bytes": 12}
    state.store.log_computer_action("console:console:me", "host", "list_dir", summary, True, 7)
    state.store.log_computer_action("telegram:123", "sandbox", "read_file", summary, False, 11)
    items = client.get(url, params={"limit": 1}).json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item.pop("created_at") > 0
    assert item == {"who": "telegram:123", "context": "sandbox", "tool": "read_file", "args_summary": summary,
                    "success": False, "duration_ms": 11}
    assert client.get(url, params={"limit": "invalid"}).status_code == 422
    assert client.post(url).status_code == 405
    state.signer = SessionSigner("test-password")
    assert client.get(url).status_code == 401
