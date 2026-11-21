"""WebUI 浏览器图标：跳到 bot 当前登录 QQ 的头像（qlogo 外部接口）。

多萝 2026-11-21 要求：把原来字母 R 的内置图标换成 bot 的 QQ 头像，
通过外部 API 现取。解析顺序：配置 self_id → 适配器 self_id →
协议端 get_login_info；都拿不到时回退内置 favicon.svg。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.web.app import create_app
from rubenbot.web.state import WebState

QLOGO = "https://q1.qlogo.cn/g?b=qq&nk={}&s=640"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def _client(tmp_path: Path, store, *, adapters=None, bot=None) -> TestClient:
    cfg = Config()
    cfg.webui.host = "127.0.0.1"
    cfg.webui.port = 8765
    cfg.webui.password = ""
    cfg.persona_path = str(tmp_path / "SOUL.md")
    if adapters is not None:
        cfg.adapters = adapters
    state = WebState(
        config=cfg, store=store, config_path=tmp_path / "config.yaml", bot=bot
    )
    return TestClient(create_app(state))


def _location(client: TestClient) -> str:
    response = client.get("/bot-avatar", follow_redirects=False)
    assert response.status_code == 302
    return response.headers["location"]


def test_config_self_id_wins(tmp_path, store):
    client = _client(tmp_path, store, adapters={"snowluma": {"self_id": "3657338908"}})
    assert _location(client) == QLOGO.format("3657338908")


def test_adapter_self_id_used_when_config_empty(tmp_path, store):
    adapter = SimpleNamespace(self_id="123456789")
    bot = SimpleNamespace(adapters={"snowluma": adapter})
    client = _client(tmp_path, store, bot=bot)
    assert _location(client) == QLOGO.format("123456789")


def test_login_info_fallback(tmp_path, store):
    class FakeAdapter:
        self_id = None

        async def call_api(self, action, params=None):
            assert action == "get_login_info"
            return {"data": {"user_id": 987654321, "nickname": "Ruben"}}

    bot = SimpleNamespace(adapters={"snowluma": FakeAdapter()})
    client = _client(tmp_path, store, bot=bot)
    assert _location(client) == QLOGO.format("987654321")


def test_no_account_falls_back_to_bundled_icon(tmp_path, store):
    client = _client(tmp_path, store)
    assert _location(client) == "/static/favicon.svg"


def test_login_info_failure_still_falls_back(tmp_path, store):
    class BrokenAdapter:
        self_id = None

        async def call_api(self, action, params=None):
            raise RuntimeError("协议端没起")

    bot = SimpleNamespace(adapters={"snowluma": BrokenAdapter()})
    client = _client(tmp_path, store, bot=bot)
    assert _location(client) == "/static/favicon.svg"


def test_index_references_bot_avatar(tmp_path, store):
    client = _client(tmp_path, store)
    html = client.get("/").text
    assert 'href="/bot-avatar"' in html
    assert "favicon.svg" not in html
