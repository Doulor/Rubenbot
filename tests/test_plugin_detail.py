"""插件详情页与 schema 化配置：详情接口、规范化、表单往返与旧插件兼容。

全部用临时目录里的插件，不依赖真实网络与仓库里的官方插件状态。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.plugins.config_schema import (
    ConfigValueError,
    coerce_value,
    normalize_schema,
    validate_values,
)
from rubenbot.plugins.manifest import load_manifest
from rubenbot.web.market import MarketService
from rubenbot.web.plugin_detail import PluginDetailError, plugin_detail
from rubenbot.web.state import WebState

from conftest import write_manifest

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "plugins"

PLUGIN_SOURCE = """
def register(ctx):
    pass
"""


def make_state(tmp_path: Path, plugins_dir: Path | None = None, *, manager: bool = True) -> WebState:
    config = Config()
    root = Path(plugins_dir) if plugins_dir is not None else tmp_path / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    config.plugins.dir = str(root)
    state = WebState(config=config, store=MemoryStore(":memory:"), config_path=tmp_path / "config.yaml")
    if manager:
        from rubenbot.plugins.loader import PluginManager

        state.plugins = PluginManager(state.config, state.store)
    return state


def make_plugin(root: Path, name: str, *, readme: str | None = None, plugin_id: str | None = None, **manifest_overrides) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(directory, plugin_id or name, **manifest_overrides)
    if readme is not None:
        (directory / "README.md").write_text(readme, encoding="utf-8")
    return directory


# ----------------------------------------------------------------------
# schema 规范化
# ----------------------------------------------------------------------


def test_normalize_schema_maps_aliases_and_skips_garbage():
    schema = [
        {"key": "enabled", "type": "boolean", "default": True, "description": "开关"},
        {"key": "count", "type": "integer", "default": 3},
        {"key": "ratio", "type": "float", "default": 0.5},
        {"key": "tags", "type": "string_list", "default": ["a"]},
        {"key": "mode", "type": "string", "default": "x", "options": ["x", "y"]},
        {"key": "", "type": "string"},                       # 没有 key
        {"key": "weird", "type": "object"},                  # 不支持的类型
        {"key": "enabled", "type": "bool"},                  # 重复 key
        "not-a-dict",
    ]
    fields = normalize_schema(schema)
    assert [field.key for field in fields] == ["enabled", "count", "ratio", "tags", "mode"]
    assert [field.type for field in fields] == ["bool", "int", "number", "list", "string"]
    assert fields[0].default is True
    assert fields[4].options[0] == {"value": "x", "label": "x"}


def test_normalize_schema_repairs_bad_default_and_range():
    fields = normalize_schema(
        [
            {"key": "count", "type": "int", "default": "很多", "min": 1, "max": 5},
            {"key": "ratio", "type": "number", "default": 0.1, "min": 5, "max": 1},
        ]
    )
    by_key = {field.key: field for field in fields}
    # 默认值类型不对：退回类型默认值，不让插件因为 schema 写错而无法读取
    assert by_key["count"].default == 0
    # min > max：范围整体忽略
    assert by_key["ratio"].min is None and by_key["ratio"].max is None


def test_coerce_value_checks_type_range_and_options():
    fields = normalize_schema(
        [
            {"key": "count", "type": "int", "default": 1, "min": 1, "max": 10},
            {"key": "mode", "type": "string", "default": "a", "options": ["a", "b"]},
            {"key": "sites", "type": "list", "default": [], "options": ["x", "y"]},
        ]
    )
    by_key = {field.key: field for field in fields}
    assert coerce_value(by_key["count"], 5) == 5
    assert coerce_value(by_key["count"], 5.0) == 5
    for bad in (0, 11, 1.5, True, "3"):
        with pytest.raises(ConfigValueError):
            coerce_value(by_key["count"], bad)
    assert coerce_value(by_key["mode"], "b") == "b"
    with pytest.raises(ConfigValueError):
        coerce_value(by_key["mode"], "c")
    assert coerce_value(by_key["sites"], ["x", " x ", ""]) == ["x"]
    with pytest.raises(ConfigValueError):
        coerce_value(by_key["sites"], ["x", "z"])


def test_validate_values_skips_empty_secret_and_reports_fields():
    fields = normalize_schema(
        [
            {"key": "api_key", "type": "string", "default": ""},
            {"key": "count", "type": "int", "default": 1, "min": 0},
            {"key": "name", "type": "string", "default": ""},
        ]
    )
    parsed, errors = validate_values(fields, {"api_key": "", "count": -1, "name": "ok"})
    assert parsed == {"name": "ok"}          # 空密钥留给调用方「保留原值」
    assert errors == [{"field": "count", "message": "不能小于 0"}]


def test_official_music_schema_is_fully_expressive():
    """schema 要能表达枚举、范围与列表这类配置面。"""
    manifest = load_manifest(OFFICIAL / "music")
    fields = {field.key: field for field in normalize_schema(manifest.config_schema)}
    assert fields["select_mode"].options
    assert fields["song_limit"].min == pytest.approx(1)
    assert fields["song_limit"].max == pytest.approx(20)


def test_official_plugins_declare_all_runtime_keys():
    """官方插件的全部可配项都要进 schema，否则 UI 改不到。"""
    from plugins.music.plugin import MusicConfig

    music = {field.key for field in normalize_schema(load_manifest(OFFICIAL / "music").config_schema)}
    assert music == set(MusicConfig.__dataclass_fields__)


# ----------------------------------------------------------------------
# 详情接口
# ----------------------------------------------------------------------


def test_detail_covers_every_section_for_installed_plugin(tmp_path):
    root = tmp_path / "plugins"
    make_plugin(
        root,
        "demo",
        readme="# 演示插件\n\n这是介绍。\n",
        display_name="演示",
        config_schema=[
            {"key": "mode", "type": "string", "default": "a", "description": "模式", "options": ["a", "b"]},
            {"key": "count", "type": "int", "default": 2, "description": "数量", "min": 0, "max": 9},
        ],
        hooks=["on_message"],
        triggers=["说「演示」"],
    )
    state = make_state(tmp_path, root)
    state.plugins.discover()
    detail = plugin_detail(state, MarketService(state), "demo")

    assert detail["name"] == "演示"
    assert detail["version"] == "1.0.0"
    assert detail["author"] == "test"
    assert detail["status_text"] == "已安装 · 未启用"
    assert detail["readme"]["text"].startswith("# 演示插件")
    assert [item["id"] for item in detail["permissions"]["items"]]  # 声明齐全
    # conftest 的测试 manifest 默认声明全部权限并视为已授权（一次性迁移口径）；
    # 详情页如实照抄授权层的结果，不在这里另算
    assert all(item["granted"] is True for item in detail["permissions"]["items"])
    assert detail["triggers"] == ["说「演示」"]
    assert [doc["key"] for doc in detail["schema_docs"]] == ["mode", "count"]
    assert detail["schema_docs"][0]["options"][1]["label"] == "b"
    assert detail["schema_docs"][1]["range"] == "0 – 9"
    assert detail["can_configure"] is True
    assert detail["config"]["values"] == {"mode": "a", "count": 2}


def test_detail_without_schema_or_readme_does_not_crash(tmp_path):
    root = tmp_path / "plugins"
    make_plugin(root, "plain", display_name="朴素插件", config_schema=[])
    state = make_state(tmp_path, root)
    state.plugins.discover()
    detail = plugin_detail(state, MarketService(state), "plain")

    assert detail["schema"] == []
    assert detail["schema_docs"] == []
    assert detail["can_configure"] is False
    assert detail["readme"] is None
    assert detail["description"] == "测试插件 plain"


def test_detail_for_invalid_manifest_reports_reason(tmp_path):
    root = tmp_path / "plugins"
    make_plugin(root, "broken", permissions=["root"])
    state = make_state(tmp_path, root)
    state.plugins.discover()
    detail = plugin_detail(state, MarketService(state), "broken")
    assert detail["valid"] is False
    assert "root" in detail["invalid_reason"]
    assert "不合规" in detail["status_text"]


def test_detail_unknown_plugin_is_404(tmp_path):
    state = make_state(tmp_path)
    with pytest.raises(PluginDetailError) as excinfo:
        plugin_detail(state, MarketService(state), "nope")
    assert excinfo.value.status == 404


def test_detail_without_manager_reads_plugin_dir(tmp_path):
    """独立 WebUI（没有插件管理器）：详情仍能从目录与 manifest 拼出来。"""
    root = tmp_path / "plugins"
    make_plugin(
        root,
        "offline",
        readme="离线说明",
        config_schema=[{"key": "x", "type": "string", "default": "1", "description": "参数"}],
    )
    state = make_state(tmp_path, root, manager=False)
    detail = plugin_detail(state, MarketService(state), "offline")
    assert detail["installed"] is True
    assert detail["enabled"] is False
    assert detail["readme"]["text"] == "离线说明"
    assert detail["can_configure"] is True
    assert detail["config"]["values"] == {"x": "1"}


def test_detail_ignores_readme_outside_plugin_dir(tmp_path):
    root = tmp_path / "plugins"
    directory = make_plugin(root, "linked", config_schema=[])
    outside = tmp_path / "outside.md"
    outside.write_text("不该被读到", encoding="utf-8")
    (directory / "README.md").symlink_to(outside)
    state = make_state(tmp_path, root)
    detail = plugin_detail(state, MarketService(state), "linked")
    assert detail["readme"] is None


def test_detail_route_returns_payload_and_404(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rubenbot.web.app import create_app

    root = tmp_path / "plugins"
    make_plugin(root, "demo", config_schema=[{"key": "x", "type": "int", "default": 1}])
    state = make_state(tmp_path, root)
    client = TestClient(create_app(state))
    ok = client.get("/api/plugins/demo/detail")
    assert ok.status_code == 200
    plugin = ok.json()["plugin"]
    assert plugin["id"] == "demo"
    assert plugin["schema"][0]["key"] == "x"
    assert client.get("/api/plugins/nope/detail").status_code == 404


def test_detail_for_uninstalled_plugin_uses_index_declaration(tmp_path):
    """索引声明了 schema / triggers 时，未安装插件的详情页也能预览配置说明。"""
    root = tmp_path / "plugins"
    make_plugin(root, "local", config_schema=[])
    state = make_state(tmp_path, root)
    state.plugins.discover()
    state.config.market.index_url = "https://example.test/plugins.json"
    schema = [{"key": "mode", "type": "string", "default": "a", "description": "模式"}]
    entry = {
        "id": "remote",
        "name": "远程插件",
        "version": "2.0.0",
        "description": "索引里的插件",
        "author": "someone",
        "permissions": ["read_messages"],
        "path": "remote",
        "download_url": "https://example.test/remote.zip",
        "repository": "https://example.test/remote",
        "triggers": ["说「远程」"],
        "config_schema": schema,
    }
    doc = json.dumps({"plugins": [entry]}, ensure_ascii=False).encode("utf-8")
    state.extras["market_fetcher"] = lambda url, *, timeout: doc

    detail = plugin_detail(state, MarketService(state), "remote")
    assert detail["installed"] is False
    assert detail["status_text"] == "未安装"
    assert detail["repository"] == "https://example.test/remote"
    assert detail["triggers"] == ["说「远程」"]
    assert [doc_row["key"] for doc_row in detail["schema_docs"]] == ["mode"]
    # 未安装插件没有本地值可读，也没有可写的配置
    assert detail["can_configure"] is False
    assert detail["config"] == {"values": {}, "secrets": {}}
    assert detail["readme"] is None


# ----------------------------------------------------------------------
# 保存回读往返
# ----------------------------------------------------------------------


def test_music_config_roundtrip_through_schema(tmp_path):
    root = tmp_path / "plugins"
    shutil.copytree(OFFICIAL / "music", root / "music")
    state = make_state(tmp_path, root)
    state.plugins.discover()
    market = MarketService(state)

    form = market.get_plugin_config("music")
    assert {spec["key"] for spec in form["schema"]} == {
        "song_limit", "select_mode", "timeout", "send_modes", "enable_comments",
        "enable_lyrics", "timeout_recall", "cooldown_seconds", "default_player",
    }
    wanted = {
        "song_limit": 3,
        "select_mode": "single",
        "timeout": 60,
        "send_modes": ["record", "text"],
        "enable_comments": False,
        "enable_lyrics": True,
        "timeout_recall": False,
        "cooldown_seconds": 5,
        "default_player": "QQ点歌",
    }
    saved = market.save_plugin_config("music", wanted)
    assert saved["values"] == wanted
    assert market.get_plugin_config("music")["values"] == wanted
    on_disk = yaml.safe_load((root / "music" / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk == wanted
    # 详情页读的也是同一份值
    detail = plugin_detail(state, market, "music")
    assert detail["config"]["values"] == wanted


def test_save_config_keeps_unknown_yaml_keys_and_drops_empty_secret(tmp_path):
    root = tmp_path / "plugins"
    directory = make_plugin(
        root,
        "keyed",
        config_schema=[
            {"key": "api_key", "type": "string", "default": "", "description": "密钥"},
            {"key": "label", "type": "string", "default": "a", "description": "标签", "max_length": 3},
        ],
    )
    (directory / "config.yaml").write_text("api_key: secret\nlegacy: keep\nlabel: x\n", encoding="utf-8")
    state = make_state(tmp_path, root)
    state.plugins.discover()
    market = MarketService(state)

    form = market.get_plugin_config("keyed")
    assert form["values"]["api_key"] == ""       # 不回显密钥
    assert form["secrets"] == {"api_key": True}
    saved = market.save_plugin_config("keyed", {"api_key": "", "label": "yy"})
    data = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    assert data == {"api_key": "secret", "legacy": "keep", "label": "yy"}
    assert saved["values"]["label"] == "yy"


def test_save_config_reports_field_errors_over_http(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rubenbot.web.app import create_app

    root = tmp_path / "plugins"
    make_plugin(
        root,
        "demo",
        config_schema=[{"key": "count", "type": "int", "default": 1, "description": "数量", "max": 3}],
    )
    state = make_state(tmp_path, root)
    client = TestClient(create_app(state))
    response = client.put("/api/plugins/demo/config", json={"values": {"count": 9}})
    assert response.status_code == 422
    body = response.json()
    assert body["fields"] == [{"field": "count", "message": "不能大于 3"}]
    ok = client.put("/api/plugins/demo/config", json={"values": {"count": 2}})
    assert ok.status_code == 200
    assert ok.json()["values"]["count"] == 2


def test_detail_route_uses_manifest_id(tmp_path):
    """市场条目按 manifest id 合并，详情路由也接受 id 而不是目录名。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rubenbot.web.app import create_app

    root = tmp_path / "plugins"
    make_plugin(root, "dir_name", plugin_id="manifest-id", config_schema=[])
    state = make_state(tmp_path, root)
    client = TestClient(create_app(state))
    for key in ("manifest-id", "dir_name"):
        response = client.get(f"/api/plugins/{key}/detail")
        assert response.status_code == 200, key
        assert response.json()["plugin"]["id"] == "manifest-id"


def test_registry_json_is_valid_after_schema_upgrade():
    """官方 manifest 仍能被市场解析（schema 只是多了可选字段）。"""
    for name in ("music", "image_gen"):
        path = OFFICIAL / name / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = load_manifest(OFFICIAL / name)
        assert manifest.id == name
        assert data["config_schema"] == list(manifest.config_schema)


def test_detail_only_manifest_fields_are_backward_compatible(tmp_path):
    """triggers / repository 写坏了也只按「没声明」处理，不能把插件判成不合规。"""
    from rubenbot.plugins.manifest import ManifestError, parse_manifest

    base = {
        "id": "demo",
        "name": "演示",
        "version": "1.0.0",
        "description": "说明",
        "author": "test",
        "min_core_version": "0.0.0",
        "permissions": ["read_messages"],
    }
    plain = parse_manifest(dict(base))
    assert plain.triggers == () and plain.repository == ""
    broken = parse_manifest({**base, "triggers": "「演示」"})
    assert broken.triggers == ()
    ok = parse_manifest({**base, "triggers": ["「演示」"], "repository": "https://example.test"})
    assert ok.triggers == ("「演示」",)
    # 必填字段仍然严格：漏了 permissions 照样拒绝
    without_permissions = {k: v for k, v in base.items() if k != "permissions"}
    with pytest.raises(ManifestError):
        parse_manifest(without_permissions)
