"""鉴权、缓存和配置引用边界；上游仅使用本地模拟传输。"""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from rubenbot.config import Config
from rubenbot.llm import LLMClient, make_usage_recorder
from rubenbot.web import app as webapp
from rubenbot.web.auth import SessionSigner
from rubenbot.web.state import WebState


@pytest.fixture
def state(tmp_path, store):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"llm": {
        "providers": [{"name": "relay", "base_url": "https://relay.test/v1", "api_key": "secret-ABC",
                       "models": ["listed", "missing"]}],
        "role_pools": {"chat": [{"provider": "relay", "model": "listed"}]},
        "budget": {"tasks": {"model_probe": {"daily_calls": 1}}},
    }}))
    return WebState(Config.load(path), store, path)


def test_discover_probe_and_pool_are_separate(state, caplog):
    calls = []

    def upstream(req):
        calls.append(req.url.path)
        assert req.headers["authorization"] == "Bearer secret-ABC"
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "listed"}, {"id": "listed"}]})
        assert json.loads(req.content)["model"] == "listed"
        return httpx.Response(401, text="upstream-private: secret-ABC")

    llm = LLMClient(state.config.llm, transport=httpx.MockTransport(upstream),
                    usage_recorder=make_usage_recorder(state.store))
    state.bot = SimpleNamespace(llm=llm)
    original = state.config_path.read_bytes()
    with TestClient(webapp.create_app(state)) as web:
        def pool():
            r = web.get("/api/models/pool")
            assert "secret-ABC" not in r.text
            return r.json()

        assert pool()["observations"]["relay"] == {"discovery": None, "probes": {}}
        assert not calls
        first = web.post("/api/models/discover", json={"provider": "relay"}).json()
        assert first["status"] == "available" and first["models"] == ["listed", "z"]
        assert first["cached_at"] is None and datetime.fromisoformat(first["checked_at"]).tzinfo
        assert pool()["providers"][0]["models"] == ["listed", "missing"]
        payload = {"provider": "relay", "model": "listed"}
        probe = web.post("/api/models/probe", json=payload).json()
        assert probe["error"] == "http_401" and probe["status"] == "error"
        assert datetime.fromisoformat(probe["checked_at"]).tzinfo
        obs = pool()["observations"]["relay"]
        assert obs == {"discovery": first, "probes": {"listed": probe}}
        blocked = web.post("/api/models/probe", json=payload).json()
        assert blocked["error"] == "budget_exceeded"
        assert llm.budget.snapshot()["tasks"]["model_probe"]["calls"] == 1
        assert calls == ["/v1/models", "/v1/chat/completions"]
        assert state.config_path.read_bytes() == original
        for text in (str(obs), str(blocked), caplog.text):
            assert "secret-ABC" not in text and "upstream-private" not in text


def test_cache_ttl_refresh_and_provider_identity(state, monkeypatch):
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(webapp, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def discover(self, target):
        calls.append(target)
        return [f"m-{len(calls)}"]

    monkeypatch.setattr(LLMClient, "discover_models", discover)
    with TestClient(webapp.create_app(state)) as web:
        def fetch(**extra):
            return web.post("/api/models/discover", json={"provider": "relay", **extra}).json()

        first = fetch()
        clock[0] += 599.99
        hit = fetch()
        assert hit == {**first, "cached_at": first["checked_at"]}
        clock[0] += .01
        assert fetch()["models"] == ["m-2"]
        assert fetch(refresh=True)["models"] == ["m-3"]
        # 改密钥后不能沿用旧目录，哪怕提供商名称与 URL 没动。
        row = web.get("/api/models/pool").json()["providers"][0]
        saved = web.put("/api/models/pool", json={"providers": [{**row, "api_key": "rotated"}]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["observations"]["relay"]["discovery"] is None
        assert fetch()["models"] == ["m-4"] and calls[-1].api_key == "rotated"


def test_failed_fetch_is_cached_and_does_not_mean_missing(state, monkeypatch):
    from rubenbot.llm import LLMResponseError

    calls = []

    async def discover(self, target):
        calls.append(target)
        raise LLMResponseError("secret-ABC", status=429)

    monkeypatch.setattr(LLMClient, "discover_models", discover)
    with TestClient(webapp.create_app(state)) as web:
        for cached in (False, True):
            result = web.post("/api/models/discover", json={"provider": "relay"})
            assert "secret-ABC" not in result.text
            data = result.json()
            assert data["status"] == "error" and data["error"] == "http_429"
            assert bool(data["cached_at"]) == cached
        assert len(calls) == 1


def test_rejects_dangling_delete_and_untrusted_targets(state):
    with TestClient(webapp.create_app(state)) as web:
        original = state.config_path.read_bytes()
        row = web.get("/api/models/pool").json()["providers"][0]
        assert web.put("/api/models/pool", json={"providers": [{**row, "models": ["missing"]}]}).status_code == 422
        for route, body in [
            ("discover", {"provider": "draft"}), ("discover", {"provider": "relay", "refresh": "true"}),
            ("discover", {"provider": "relay", "base_url": "https://other.test"}),
            ("probe", {"provider": "relay", "model": ["listed", "missing"]}),
            ("probe", {"provider": "relay", "model": "not-saved"}),
        ]:
            assert web.post(f"/api/models/{route}", json=body).status_code == 422
        assert state.config_path.read_bytes() == original


@pytest.mark.parametrize("route", ["discover", "probe"])
def test_model_diagnostics_require_session(state, route):
    state.signer = SessionSigner("locked")
    with TestClient(webapp.create_app(state)) as web:
        assert web.post(f"/api/models/{route}", json={"provider": "relay"}).status_code == 401
