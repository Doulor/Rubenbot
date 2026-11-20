"""插件市场：manifest 校验、索引回退与合并、安装 / 更新 / 卸载。

全部用本地 fixture（临时目录里的索引 JSON 与 zip 包），不依赖真实网络；
远程索引通过注入的 fetcher 模拟。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.plugins.loader import PluginManager
from rubenbot.plugins.manifest import (
    ManifestError,
    load_manifest,
    parse_manifest,
)
from rubenbot.web.market import (
    MarketError,
    MarketService,
    builtin_index,
    parse_index,
)
from rubenbot.web.state import WebState

from conftest import write_manifest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402

OFFICIAL_DIR = Path(__file__).resolve().parents[1] / "plugins"

PLUGIN_SOURCE = '''
def register(ctx):
    @ctx.command("ping")
    async def ping(runtime, event, args):
        return "pong"
'''


def manifest_data(plugin_id: str = "alpha", **overrides) -> dict:
    data = {
        "id": plugin_id,
        "name": "Alpha 插件",
        "version": "1.0.0",
        "description": "测试用的插件",
        "author": "tester",
        "min_core_version": "0.0.0",
        "permissions": ["read_messages"],
        "hooks": ["command"],
    }
    data.update(overrides)
    return data


def make_zip(tmp_path: Path, files: dict[str, str], name: str = "pkg.zip") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, text in files.items():
            zf.writestr(member, text)
    return path


def plugin_zip(tmp_path: Path, plugin_id: str = "alpha", *, zip_name: str | None = None, **overrides) -> Path:
    """一个目录被打包的插件 zip（带单层顶层目录，模拟 GitHub 打包结果）。"""
    data = manifest_data(plugin_id, **overrides)
    return make_zip(
        tmp_path,
        {
            f"{plugin_id}/manifest.json": json.dumps(data, ensure_ascii=False),
            f"{plugin_id}/plugin.py": PLUGIN_SOURCE,
        },
        name=zip_name or f"{plugin_id}.zip",
    )


def index_doc(entries: list[dict]) -> bytes:
    return json.dumps({"plugins": entries}, ensure_ascii=False).encode("utf-8")


def index_entry(plugin_id: str = "alpha", *, download_url: str | None = None, **overrides) -> dict:
    entry = {
        "id": plugin_id,
        "name": "Alpha 插件",
        "version": "1.0.0",
        "description": "测试用的插件",
        "author": "tester",
        "permissions": ["read_messages"],
        "path": plugin_id,
    }
    if download_url is not None:
        entry["download_url"] = download_url
    entry.update(overrides)
    return entry


def make_state(
    tmp_path: Path,
    *,
    index_url: str = "",
    fetcher=None,
    manager=True,
    plugins_dir: Path | None = None,
) -> WebState:
    config = Config()
    root = Path(plugins_dir) if plugins_dir is not None else tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    config.plugins.dir = str(root)
    state = WebState(
        config=config,
        store=MemoryStore(":memory:"),
        config_path=tmp_path / "config.yaml",
    )
    state.config_path.write_text("chat:\n  bot_name: Ruben\n", encoding="utf-8")
    if manager:
        manager_obj = PluginManager(config, state.store)
        manager_obj.load_all()
        state.plugins = manager_obj
    state.config.market.index_url = index_url
    if fetcher is not None:
        state.extras["market_fetcher"] = fetcher
    return state


# ----------------------------------------------------------------------
# manifest 校验
# ----------------------------------------------------------------------


def test_parse_manifest_accepts_valid_data():
    manifest = parse_manifest(manifest_data("alpha", icon="🅰", category="互动"))
    assert manifest.id == "alpha"
    assert manifest.permissions == ("read_messages",)
    assert manifest.category == "互动"
    assert manifest.as_dict()["version"] == "1.0.0"


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(tmp_path)
    assert excinfo.value.missing is True
    assert "缺少 manifest.json" in excinfo.value.reason


def test_parse_manifest_missing_field():
    data = manifest_data()
    data.pop("author")
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(data)
    assert "author" in excinfo.value.reason


def test_parse_manifest_rejects_unknown_permission():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(manifest_data(permissions=["read_messages", "root"]))
    assert "root" in excinfo.value.reason
    assert excinfo.value.core_too_old is False


def test_parse_manifest_core_too_old():
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(manifest_data(min_core_version="99.0.0"))
    assert excinfo.value.core_too_old is True
    assert "99.0.0" in excinfo.value.reason


def test_loader_rejects_invalid_manifest(tmp_path):
    """loader 看到缺 manifest / 权限越界 / 核心版本过低都拒绝加载并记原因。"""
    root = tmp_path / "plugins"
    missing = root / "missing_manifest"
    missing.mkdir(parents=True)
    (missing / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")

    bad_perm = root / "bad_perm"
    bad_perm.mkdir()
    (bad_perm / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(bad_perm, "bad_perm", permissions=["read_messages", "root"])

    too_old = root / "too_old"
    too_old.mkdir()
    (too_old / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(too_old, "too_old", min_core_version="99.0.0")

    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["missing_manifest", "bad_perm", "too_old"]
    manager = PluginManager(config, MemoryStore(":memory:"))
    assert manager.load_all() == []

    info = {item["name"]: item for item in manager.list_plugins()}
    assert info["missing_manifest"]["valid"] is False
    assert "缺少 manifest.json" in info["missing_manifest"]["invalid_reason"]
    assert info["bad_perm"]["valid"] is False
    assert "root" in info["bad_perm"]["invalid_reason"]
    assert info["too_old"]["core_too_old"] is True
    # 不合规的插件不能被启用
    assert manager.set_enabled("bad_perm", True) is False


def test_official_plugins_all_have_valid_manifest():
    for name in ("hello", "privacy", "emoji_react", "qzone_post"):
        manifest = load_manifest(OFFICIAL_DIR / name)
        assert manifest.id == name
        assert manifest.permissions
        assert set(manifest.permissions) <= {
            "read_messages", "send_messages", "capabilities", "qzone_write",
            "scheduler", "llm", "network",
        }


# ----------------------------------------------------------------------
# 索引
# ----------------------------------------------------------------------


def test_builtin_index_lists_official_plugins():
    entries = builtin_index(OFFICIAL_DIR)
    by_id = {entry.id: entry for entry in entries}
    assert {"hello", "privacy", "emoji_react", "qzone_post"} <= set(by_id)
    assert set(by_id["qzone_post"].permissions) == {
        "read_messages", "send_messages", "qzone_write", "scheduler", "llm"
    }
    # 内置索引里的官方插件没有下载地址（随框架发布，天然已安装）
    assert all(entry.download_url is None for entry in entries)


def test_parse_index_skips_bad_entries():
    entries = parse_index(
        {
            "plugins": [
                index_entry("alpha"),
                index_entry("beta", permissions=["not_a_permission"]),
                {"id": "gamma"},  # 缺字段
            ]
        }
    )
    assert [entry.id for entry in entries] == ["alpha"]


def test_index_unavailable_falls_back_to_builtin(tmp_path):
    state = make_state(tmp_path, index_url="https://example.invalid/plugins.json")

    def failing(url, *, timeout):
        raise TimeoutError("timed out")

    market = MarketService(state, fetcher=failing)
    snapshot = market.snapshot()
    assert snapshot["index"]["source"] == "builtin"
    assert snapshot["index"]["available"] is False
    assert "超时" in snapshot["index"]["error"]
    # 回退后页面数据不能空
    assert {item["id"] for item in snapshot["items"]} >= {
        "hello", "privacy", "emoji_react", "qzone_post"
    }


def test_index_remote_source_used_when_available(tmp_path):
    state = make_state(
        tmp_path,
        index_url="https://example.test/plugins.json",
        fetcher=lambda url, *, timeout: index_doc([index_entry("alpha", download_url="x")]),
    )
    market = MarketService(state)
    snapshot = market.snapshot()
    assert snapshot["index"]["source"] == "remote"
    assert snapshot["index"]["available"] is True
    alpha = next(item for item in snapshot["items"] if item["id"] == "alpha")
    assert alpha["state"] == "not_installed"
    assert alpha["download_available"] is True


# ----------------------------------------------------------------------
# 合并与状态
# ----------------------------------------------------------------------


def test_snapshot_merges_local_and_index(tmp_path):
    """本地插件与索引按 id 合并成一行；状态按启用/版本计算。"""
    root = tmp_path / "plugins"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(alpha, "alpha", version="1.0.0")

    state = make_state(tmp_path)
    state.config.plugins.enabled = []
    state.plugins.config.plugins.enabled = []
    state.plugins.discover()

    # 索引给出同 id 的更高版本 + 一个未安装的新插件
    state.config.market.index_url = "https://example.test/plugins.json"
    fetcher = lambda url, *, timeout: index_doc(  # noqa: E731
        [
            index_entry("alpha", version="1.2.0", download_url="x"),
            index_entry("beta", download_url="x"),
        ]
    )
    state.extras["market_fetcher"] = fetcher
    market = MarketService(state)
    snapshot = market.snapshot()
    by_id = {item["id"]: item for item in snapshot["items"]}

    assert by_id["alpha"]["installed"] is True
    assert by_id["alpha"]["state"] == "installed"
    assert by_id["alpha"]["updatable"] is True
    assert by_id["alpha"]["latest_version"] == "1.2.0"
    assert by_id["beta"]["state"] == "not_installed"
    assert by_id["beta"]["installed"] is False
    assert len(snapshot["items"]) == 2  # 合并后 alpha 只有一行

    # 启用后状态变成 enabled
    assert state.set_plugin_enabled("alpha", True) is True
    snapshot = market.snapshot()
    by_id = {item["id"]: item for item in snapshot["items"]}
    assert by_id["alpha"]["state"] == "enabled"


def test_local_invalid_plugin_shows_reason(tmp_path):
    root = tmp_path / "plugins"
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(broken, "broken", permissions=["root"])

    state = make_state(tmp_path)
    state.plugins.discover()
    snapshot = MarketService(state).snapshot()
    item = next(item for item in snapshot["items"] if item["id"] == "broken")
    assert item["invalid"] is True
    assert "root" in item["invalid_reason"]
    assert item["state"] == "installed"


def test_local_plugin_missing_manifest_uses_index_info(tmp_path):
    """目录里没有 manifest 的插件也要出现在列表里，展示信息退回索引并标不合规。"""
    root = tmp_path / "plugins"
    legacy = root / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")

    state = make_state(tmp_path)
    state.plugins.discover()
    archive = plugin_zip(tmp_path, "legacy", zip_name="legacy.zip")
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("legacy", download_url=str(archive))]
    )
    market = MarketService(state)
    item = next(i for i in market.snapshot()["items"] if i["id"] == "legacy")
    assert item["state"] == "installed"
    assert item["invalid"] is True
    assert "缺少 manifest.json" in item["invalid_reason"]
    # 展示信息来自索引（名字、说明、权限），不是一片空白
    assert item["name"] == "Alpha 插件"
    assert item["permissions"] == ["read_messages"]
    # 索引有下载地址：可以一键更新把 manifest 补回来
    assert item["updatable"] is True
    market.update("legacy")
    assert load_manifest(legacy).id == "legacy"
    assert next(i for i in market.snapshot()["items"] if i["id"] == "legacy")["invalid"] is False


def test_official_plugins_states_in_snapshot(tmp_path):
    """内置 4 插件的状态跟着 enabled 配置走，权限徽标来自各自 manifest。"""
    state = make_state(tmp_path, plugins_dir=OFFICIAL_DIR)
    state.config.plugins.enabled = ["hello"]
    state.plugins.config.plugins.enabled = ["hello"]
    state.plugins.discover()
    state.plugins.set_enabled("hello", True)

    snapshot = MarketService(state).snapshot()
    by_id = {item["id"]: item for item in snapshot["items"]}
    assert by_id["hello"]["state"] == "enabled"
    assert by_id["privacy"]["state"] == "installed"
    assert by_id["qzone_post"]["permissions"] == [
        "read_messages", "send_messages", "qzone_write", "scheduler", "llm"
    ]
    labels = {item["id"]: item["label"] for item in by_id["qzone_post"]["permission_meta"]}
    assert labels["qzone_write"] == "空间写入"
    assert any(meta["high_risk"] for meta in by_id["qzone_post"]["permission_meta"])


# ----------------------------------------------------------------------
# 安装 / 更新 / 卸载
# ----------------------------------------------------------------------


def test_install_from_fixture_zip(tmp_path):
    archive = plugin_zip(tmp_path, "alpha")
    state = make_state(tmp_path)
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", download_url=str(archive))]
    )
    market = MarketService(state)

    assert market.snapshot()["items"][0]["state"] == "not_installed"
    result = market.install("alpha")
    target = Path(state.config.plugins.dir) / "alpha"
    assert target.is_dir()
    assert (target / "plugin.py").is_file()
    assert (target / "manifest.json").is_file()
    assert result["version"] == "1.0.0"

    snapshot = market.snapshot()
    item = next(item for item in snapshot["items"] if item["id"] == "alpha")
    assert item["state"] == "installed"  # 默认不启用
    assert item["enabled"] is False
    assert state.plugins.plugins["alpha"].enabled is False


def test_install_rejects_path_traversal(tmp_path):
    archive = make_zip(
        tmp_path,
        {
            "alpha/manifest.json": json.dumps(manifest_data("alpha")),
            "alpha/plugin.py": PLUGIN_SOURCE,
            "alpha/../../escaped.txt": "pwned",
        },
        name="evil.zip",
    )
    state = make_state(tmp_path)
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", download_url=str(archive))]
    )
    market = MarketService(state)
    with pytest.raises(MarketError) as excinfo:
        market.install("alpha")
    assert "越界" in excinfo.value.message or "路径" in excinfo.value.message
    assert not (Path(state.config.plugins.dir) / "alpha").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "plugins" / "escaped.txt").exists()


def test_install_rejects_absolute_path_member(tmp_path):
    archive = make_zip(
        tmp_path,
        {
            "alpha/manifest.json": json.dumps(manifest_data("alpha")),
            "alpha/plugin.py": PLUGIN_SOURCE,
            "/tmp/evil.txt": "pwned",
        },
        name="abs.zip",
    )
    state = make_state(tmp_path)
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", download_url=str(archive))]
    )
    with pytest.raises(MarketError):
        MarketService(state).install("alpha")


def test_install_rejects_id_mismatch_and_core_too_old(tmp_path):
    mismatch = plugin_zip(tmp_path, "beta", zip_name="beta.zip")  # 包内 id 是 beta
    state = make_state(tmp_path)
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", download_url=str(mismatch))]
    )
    with pytest.raises(MarketError) as excinfo:
        MarketService(state).install("alpha")
    assert "不一致" in excinfo.value.message

    too_new = plugin_zip(tmp_path, "gamma", min_core_version="99.0.0", zip_name="gamma.zip")
    state2 = make_state(tmp_path / "second")
    state2.config.market.index_url = "https://example.test/plugins.json"
    state2.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("gamma", download_url=str(too_new))]
    )
    with pytest.raises(MarketError) as excinfo2:
        MarketService(state2).install("gamma")
    assert "版本" in excinfo2.value.message


def test_install_without_download_url_is_display_only(tmp_path):
    state = make_state(tmp_path)
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc([index_entry("alpha")])
    market = MarketService(state)
    item = market.snapshot()["items"][0]
    assert item["download_available"] is False
    with pytest.raises(MarketError) as excinfo:
        market.install("alpha")
    assert "没有下载地址" in excinfo.value.message


def test_uninstall_removes_dir_and_keeps_config(tmp_path):
    root = tmp_path / "plugins"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (alpha / "config.yaml").write_text("option: 1\n", encoding="utf-8")
    write_manifest(alpha, "alpha")

    state = make_state(tmp_path)
    state.plugins.discover()
    state.plugins.set_enabled("alpha", True)
    state.config.plugins.enabled = ["alpha"]

    market = MarketService(state)
    market.uninstall("alpha")
    assert not alpha.exists()
    assert "alpha" not in state.plugins.plugins
    # config.yaml 的其它段不受影响
    import yaml

    data = yaml.safe_load(state.config_path.read_text(encoding="utf-8"))
    assert data["chat"]["bot_name"] == "Ruben"
    assert data.get("plugins", {}).get("enabled") == []


def test_update_keeps_plugin_config_and_enabled_state(tmp_path):
    root = tmp_path / "plugins"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    (alpha / "config.yaml").write_text("option: keep-me\n", encoding="utf-8")
    write_manifest(alpha, "alpha", version="1.0.0")

    state = make_state(tmp_path)
    state.plugins.discover()
    state.plugins.set_enabled("alpha", True)
    state.config.plugins.enabled = ["alpha"]

    archive = plugin_zip(tmp_path, "alpha", version="1.1.0", zip_name="alpha-1.1.zip")
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", version="1.1.0", download_url=str(archive))]
    )
    market = MarketService(state)
    assert market.snapshot()["items"][0]["updatable"] is True

    result = market.update("alpha")
    assert result["version"] == "1.1.0"
    # 代码与 manifest 换新，插件自己的配置保留
    assert load_manifest(alpha).version == "1.1.0"
    assert (alpha / "config.yaml").read_text(encoding="utf-8") == "option: keep-me\n"
    # 启用状态保持
    assert state.plugins.plugins["alpha"].enabled is True
    assert result["enabled"] is True
    import yaml

    data = yaml.safe_load(state.config_path.read_text(encoding="utf-8"))
    assert data["plugins"]["enabled"] == ["alpha"]


def test_update_rejected_when_not_newer(tmp_path):
    root = tmp_path / "plugins"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(alpha, "alpha", version="1.0.0")
    state = make_state(tmp_path)
    state.plugins.discover()

    archive = plugin_zip(tmp_path, "alpha", version="1.0.0", zip_name="same.zip")
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", version="1.0.0", download_url=str(archive))]
    )
    with pytest.raises(MarketError) as excinfo:
        MarketService(state).update("alpha")
    assert "最新版本" in excinfo.value.message


# ----------------------------------------------------------------------
# WebUI 接口
# ----------------------------------------------------------------------


@pytest.fixture()
def market_client(tmp_path: Path):
    state = make_state(tmp_path)
    client = TestClient(create_app(state))
    return client, state, tmp_path


def test_api_market_snapshot_and_actions(market_client):
    client, state, tmp_path = market_client
    archive = plugin_zip(tmp_path, "alpha")
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", download_url=str(archive))]
    )

    snapshot = client.get("/api/market").json()
    assert snapshot["ok"] is True
    assert snapshot["index"]["source"] == "remote"
    assert snapshot["categories"] == ["互动", "空间", "工具", "系统"]
    alpha = next(item for item in snapshot["items"] if item["id"] == "alpha")
    assert alpha["state"] == "not_installed"

    installed = client.post("/api/market/plugins/alpha/install").json()
    assert installed["ok"] is True
    after = {item["id"]: item for item in installed["market"]["items"]}
    assert after["alpha"]["state"] == "installed"

    enabled = client.post("/api/plugins/alpha", json={"enabled": True}).json()
    assert enabled["ok"] is True
    assert enabled["items"][0]["enabled"] is True

    uninstalled = client.post("/api/market/plugins/alpha/uninstall").json()
    assert uninstalled["ok"] is True
    assert not (Path(state.config.plugins.dir) / "alpha").exists()


def test_api_market_full_lifecycle(tmp_path: Path):
    """未安装 → 安装 → 启用 → 更新 → 卸载，全程用 fixture 索引与本地 zip。"""
    state = make_state(tmp_path)
    v1 = plugin_zip(tmp_path, "alpha", version="1.0.0", zip_name="alpha-1.0.zip")
    v2 = plugin_zip(tmp_path, "alpha", version="1.1.0", zip_name="alpha-1.1.zip")
    state.config.market.index_url = "https://example.test/plugins.json"

    def fetcher(url, *, timeout):
        return index_doc([index_entry("alpha", version="1.0.0", download_url=str(v1))])

    state.extras["market_fetcher"] = fetcher
    client = TestClient(create_app(state))

    def card():
        items = client.get("/api/market").json()["items"]
        return next(item for item in items if item["id"] == "alpha")

    assert card()["state"] == "not_installed"

    # 安装：落盘到 plugins/alpha，校验通过，状态变已安装（不自动启用）
    assert client.post("/api/market/plugins/alpha/install").json()["ok"] is True
    assert card()["state"] == "installed"
    assert (Path(state.config.plugins.dir) / "alpha" / "plugin.py").is_file()

    # 启用：写回 plugins.enabled
    enabled = client.post("/api/plugins/alpha", json={"enabled": True}).json()
    assert enabled["items"][0]["enabled"] is True
    assert card()["state"] == "enabled"
    import yaml

    disk = yaml.safe_load(state.config_path.read_text(encoding="utf-8"))
    assert disk["plugins"]["enabled"] == ["alpha"]

    # 更新：索引给出新版本，换代码但保持启用与插件自己的配置
    (Path(state.config.plugins.dir) / "alpha" / "config.yaml").write_text(
        "keep: me\n", encoding="utf-8"
    )
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc(
        [index_entry("alpha", version="1.1.0", download_url=str(v2))]
    )
    assert card()["updatable"] is True
    updated = client.post("/api/market/plugins/alpha/update").json()
    assert updated["ok"] is True
    assert card()["version"] == "1.1.0"
    assert card()["state"] == "enabled"
    assert (Path(state.config.plugins.dir) / "alpha" / "config.yaml").read_text(encoding="utf-8") == "keep: me\n"

    # 卸载：先停用再删目录，config.yaml 的其它段保留
    assert client.post("/api/market/plugins/alpha/uninstall").json()["ok"] is True
    assert not (Path(state.config.plugins.dir) / "alpha").exists()
    assert card()["state"] == "not_installed"
    disk = yaml.safe_load(state.config_path.read_text(encoding="utf-8"))
    assert disk["chat"]["bot_name"] == "Ruben"


def test_api_market_errors_are_json(market_client):
    client, state, _tmp = market_client
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: index_doc([])
    response = client.post("/api/market/plugins/nope/install")
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_api_market_falls_back_to_builtin(market_client):
    client, state, _tmp = market_client
    state.config.market.index_url = "https://example.test/plugins.json"
    state.extras["market_fetcher"] = lambda url, *, timeout: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    data = client.get("/api/market").json()
    assert data["index"]["source"] == "builtin"
    assert data["index"]["available"] is False
    assert data["items"]
