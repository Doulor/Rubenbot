"""关于页只读接口：提交来源、许可证、实际运行状态与更新检查。"""

from importlib import metadata
from pathlib import Path
import subprocess
import tomllib
from types import SimpleNamespace

import httpx
import pytest
from websockets.protocol import State

from conftest import write_manifest
from rubenbot import __version__
from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.config import Config, LLMConfig
from rubenbot.plugins.loader import PluginManager
from rubenbot.web import about
from rubenbot.web.auth import SessionSigner
from rubenbot.web.state import WebState

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from rubenbot.web.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOCAL = "ab" * 20
LATEST = "cd" * 20


@pytest.fixture
def state(config, store, tmp_path):
    config.plugins.dir = str(tmp_path / "plugins")
    return WebState(config, store, config_path=tmp_path / "config.yaml")


@pytest.fixture
def client(state, monkeypatch):
    # 本机代理的 bypass 列表不应影响使用模拟 GitHub 响应的离线测试。
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    with TestClient(create_app(state)) as client:
        yield client


def test_about_commit_sources_and_version(client, tmp_path, monkeypatch):
    monkeypatch.setattr(about, "ROOT", tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost",
                    "commit", "--allow-empty", "-qm", "test"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    stamp = tmp_path / ".deploy-commit"
    stamp.write_text(LOCAL + "\n")
    project = client.get("/api/about").json()["project"]
    assert project == {"name": "RubenBot", "version": __version__, "repository_url": about.REPOSITORY,
                       "commit": LOCAL, "commit_url": f"{about.REPOSITORY}/commit/{LOCAL}",
                       "commit_source": "deploy", "commit_reason": None}
    assert __version__ == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for invalid in (None, "bad stamp"):
        if invalid:
            stamp.write_text(invalid)
        else:
            stamp.unlink()
        project = client.get("/api/about").json()["project"]
        assert (project["commit"], project["commit_source"]) == (head, "git")
    # 安装在别的项目中，也不能把外层仓库当作自己的版本。
    nested = tmp_path / "site-packages"
    nested.mkdir()
    monkeypatch.setattr(about, "ROOT", nested)
    project = client.get("/api/about").json()["project"]
    assert project["commit"] is project["commit_url"] is None
    assert project["commit_source"] == "unknown" and project["commit_reason"]


def test_about_source_license_and_no_network(client, monkeypatch):
    def no_request(*args, **kwargs):
        pytest.fail("关于页首屏不应发网络请求")

    monkeypatch.setattr(httpx.AsyncClient, "get", no_request)
    assert client.get("/api/about").json()["license"] == {
        "name": "MIT", "text": (ROOT / "LICENSE").read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("location", ["licenses/LICENSE", "LICENSE", None])
def test_about_installed_license(client, tmp_path, monkeypatch, location):
    monkeypatch.setattr(about, "ROOT", tmp_path)
    (tmp_path / "LICENSE").write_text("别的项目的许可证")
    info = tmp_path / "rubenbot-0.1.0.dist-info"
    info.mkdir()
    expected = (ROOT / "LICENSE").read_text() if location else None
    if location:
        file = info / location
        file.parent.mkdir(exist_ok=True)
        file.write_text(expected)
    monkeypatch.setattr(metadata, "distribution", lambda name: metadata.PathDistribution(info))
    assert client.get("/api/about").json()["license"] == {"name": "MIT", "text": expected}


def test_about_runtime_models_adapters_plugins(client, state, tmp_path):
    config = Config()
    config.llm = LLMConfig(chat_model="旧主模型", extract_model="提取", gate_model="门控",
                           models={"chat": ["主", "备"], "custom": "备"}, api_key="机密甲",
                           base_url="机密乙", model_strategy="random", model_strategies={"chat": "round_robin"})
    config.plugins.dir = state.config.plugins.dir
    config.plugins.enabled = ["on", "bad", "ghost"]
    for name in ("on", "off", "bad"):
        path = Path(config.plugins.dir) / name
        path.mkdir(parents=True)
        (path / "plugin.py").write_text("def register(ctx): pass")
        if name != "bad":
            write_manifest(path)
    state.plugins = PluginManager(config, state.store)
    state.plugins.load_all()
    # 已加载的钩子仍在内存中，手工移走目录不等于停用。
    (Path(config.plugins.dir) / "on").rename(tmp_path / "moved")
    snow = SnowLumaAdapter(ws_url="机密丙", http_url="机密丁", access_token="机密戊")
    state.bot = SimpleNamespace(config=config, adapters={"console": object(), "snowluma": snow})
    snow._ws = SimpleNamespace(state=State.OPEN)
    state.config_path.write_text("llm: {chat_model: 磁盘值}")
    response = client.get("/api/about")
    runtime = response.json()["runtime"]
    assert "机密" not in response.text and "磁盘值" not in response.text
    assert runtime["models"] == {"chat_model": "旧主模型", "extract_model": "提取",
                                "consolidate_model": None, "gate_model": "门控"}
    assert runtime["model_strategy"] == "random" and runtime["model_count"] == 4
    assert runtime["roles"]["chat"] == {"model": "主", "strategy": "round_robin", "count": 2}
    assert runtime["roles"]["consolidate"] == {"model": "主", "strategy": "random", "count": 2}
    assert runtime["roles"]["extract"]["model"] == "提取"
    assert runtime["roles"]["gate"]["model"] == "门控"
    assert runtime["roles"]["custom"]["model"] == "备"
    assert runtime["plugins"] == {"total": 3, "enabled": 1}
    assert runtime["adapters"] == ["console", "snowluma"] and runtime["snowluma_status"] == "connected"
    snow._ws.state = State.CLOSED
    assert client.get("/api/about").json()["runtime"]["snowluma_status"] == "disconnected"
    del state.bot.adapters["snowluma"]
    assert client.get("/api/about").json()["runtime"]["snowluma_status"] == "disabled"
    state.bot = state.plugins = None
    state.config.plugins.enabled = ["on"]
    runtime = client.get("/api/about").json()["runtime"]
    assert runtime["adapters"] == [] and runtime["snowluma_status"] == "not_running"
    assert runtime["plugins"] == {"total": 2, "enabled": 0}
    assert state.config_path.read_text() == "llm: {chat_model: 磁盘值}"


@pytest.mark.parametrize(("local", "expected", "message"), [
    (LOCAL, "available", f"有更新：{LATEST[:7]}"), (LATEST, "current", "已是最新"),
    (None, "unknown", "本地提交未知，无法判断是否有更新"),
])
def test_update_check(client, monkeypatch, tmp_path, local, expected, message):
    monkeypatch.setattr(about, "ROOT", tmp_path)
    if local:
        (tmp_path / ".deploy-commit").write_text(local)

    async def fetch(self, url):
        assert url == "https://api.github.com/repos/Doulor/Rubenbot/commits/main"
        assert self.timeout == httpx.Timeout(10)
        return httpx.Response(200, json={"sha": LATEST, "html_url": "https://invalid.test"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fetch)
    assert client.get("/api/about/update").json() == {
        "status": expected, "message": message, "latest_commit": LATEST,
        "commit_url": f"{about.REPOSITORY}/commit/{LATEST}",
    }


@pytest.mark.parametrize("result", [httpx.ConnectError("机密"), httpx.ReadTimeout("机密"),
                                    TimeoutError(), 403, b"not JSON", b'{"sha":"bad"}', b'[]'])
def test_update_check_failure(client, monkeypatch, result):
    async def fetch(self, url):
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result if isinstance(result, int) else 200,
                              content=result if isinstance(result, bytes) else b"",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fetch)
    response = client.get("/api/about/update")
    assert response.status_code == 200
    assert response.json() == {"status": "error", "message": "检查失败，请稍后再试",
                               "latest_commit": None, "commit_url": None}
    assert client.get("/api/about").status_code == 200


def test_about_auth_and_read_only(state, client):
    state.signer = SessionSigner("口令")
    for path in ("/api/about", "/api/about/update"):
        assert client.get(path).status_code == 401
    assert client.post("/api/login", json={"password": "口令"}).status_code == 200
    assert client.get("/api/about").status_code == 200
    for path in ("/api/about", "/api/about/update"):
        assert client.post(path).status_code == 405
