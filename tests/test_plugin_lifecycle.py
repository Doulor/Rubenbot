"""插件生命周期：on_load / on_unload / on_config_change、热重载与失败回滚。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore
from rubenbot.plugins.loader import PluginManager, PluginReloadError
from rubenbot.plugins.permissions import PluginPermissionStore

from conftest import write_manifest


LIFECYCLE_SOURCE = '''
def register(ctx):
    events = ctx.extras.setdefault("events", [])
    events.append("register")

    @ctx.on_load
    def loaded(context, manifest):
        events.append("load")
        # 入参就是注册上下文与校验后的 manifest
        events.append(("manifest", manifest.id))

    @ctx.on_unload
    def unloaded(context):
        events.append("unload")

    @ctx.on_config_change
    def config_changed(context, old, new):
        events.append(("config", old.get("count"), new.get("count")))

    @ctx.on_message
    def note(runtime, event):
        events.append(("message", event.text))
        return True
'''


def _make_plugin(root: Path, name: str = "demo", source: str = LIFECYCLE_SOURCE, **manifest) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(source, encoding="utf-8")
    manifest.setdefault("permissions", ["read_messages", "send_messages"])
    write_manifest(directory, name, **manifest)
    return directory


def _manager(root: Path, *, enabled=("demo",), permissions=None, source: str = LIFECYCLE_SOURCE, name: str = "demo", **manifest) -> PluginManager:
    if not (root / name / "plugin.py").exists():
        _make_plugin(root, name, source=source, **manifest)
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = list(enabled)
    store = MemoryStore(":memory:")
    manager = PluginManager(
        config,
        store,
        permissions=permissions or PluginPermissionStore(None),
    )
    manager.load_all()
    return manager


def _migrated(path: Path) -> PluginPermissionStore:
    store = PluginPermissionStore(path)
    store.migrate({})
    return store


def _private(text: str) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


# ----------------------------------------------------------------------
# 三个钩子本身
# ----------------------------------------------------------------------


def test_load_hook_runs_once_with_context_and_manifest(tmp_path):
    manager = _manager(tmp_path / "plugins")
    events = manager.extras["events"]
    assert events[:2] == ["register", "load"]
    assert ("manifest", "demo") in events
    plugin = manager.plugins["demo"]
    assert plugin.lifecycle_active is True
    info = manager.list_plugins()[0]
    assert info["lifecycle"] is True
    assert info["hooks"]["on_load"] == 1
    assert info["hooks"]["on_unload"] == 1
    assert info["hot_config"] is True


def test_unload_hook_runs_on_disable_and_load_again_on_enable(tmp_path):
    manager = _manager(tmp_path / "plugins")
    events = manager.extras["events"]
    assert manager.set_enabled("demo", False) is True
    assert events[-1] == "unload"
    assert manager.plugins["demo"].lifecycle_active is False
    # 再次启用补一次 on_load（register 不重复跑，旧钩子仍在）
    assert manager.set_enabled("demo", True) is True
    assert events[-2:] == ["load", ("manifest", "demo")]
    assert events.count("register") == 1


def test_config_change_hook_receives_old_and_new_values(tmp_path):
    manager = _manager(tmp_path / "plugins")
    events = manager.extras["events"]
    hot, message = manager.apply_config_change("demo", {"count": 1}, {"count": 3})
    assert hot is True
    assert ("config", 1, 3) in events
    assert "热生效" in message
    # 值没变就不重复调用
    count = events.count(("config", 1, 3))
    hot, _ = manager.apply_config_change("demo", {"count": 3}, {"count": 3})
    assert hot is True
    assert events.count(("config", 1, 3)) == count


def test_plugin_without_config_hook_says_restart(tmp_path):
    source = LIFECYCLE_SOURCE.replace(
        '    @ctx.on_config_change\n'
        '    def config_changed(context, old, new):\n'
        '        events.append(("config", old.get("count"), new.get("count")))\n',
        "",
    )
    manager = _manager(tmp_path / "plugins")
    # 用不带钩子的源码重建一个管理器，确保没有残留的 config_change_hooks
    root = tmp_path / "other"
    _make_plugin(root, "plain", source=source)
    manager = _manager(root, enabled=("plain",))
    assert manager.plugins["plain"].config_change_hooks == []
    hot, message = manager.apply_config_change("plain", {"count": 1}, {"count": 2})
    assert hot is False
    assert "重启" in message


def test_hook_exceptions_are_isolated(tmp_path):
    """三个钩子各抛一次异常：加载、停用、配置变更都照常完成。"""
    source = '''
def register(ctx):
    events = ctx.extras.setdefault("events", [])

    @ctx.on_load
    def loaded(context, manifest):
        events.append("load")
        raise RuntimeError("on_load 炸了")

    @ctx.on_unload
    def unloaded(context):
        events.append("unload")
        raise RuntimeError("on_unload 炸了")

    @ctx.on_config_change
    def config_changed(context, old, new):
        events.append("config")
        raise RuntimeError("on_config_change 炸了")
'''
    root = tmp_path / "plugins"
    _make_plugin(root, "demo", source=source)
    manager = _manager(root)
    events = manager.extras["events"]
    assert events == ["load"]
    assert manager.plugins["demo"].loaded is True

    assert manager.set_enabled("demo", False) is True
    assert events == ["load", "unload"]
    assert manager.set_enabled("demo", True) is True

    hot, _message = manager.apply_config_change("demo", {"a": 1}, {"a": 2})
    assert hot is True
    assert events[-1] == "config"


def test_async_lifecycle_hooks_are_scheduled(tmp_path):
    source = '''
def register(ctx):
    events = ctx.extras.setdefault("events", [])

    @ctx.on_load
    async def loaded(context, manifest):
        events.append("load")

    @ctx.on_unload
    async def unloaded(context):
        events.append("unload")

    @ctx.on_config_change
    async def config_changed(context, old, new):
        events.append("config")
'''
    root = tmp_path / "plugins"
    _make_plugin(root, "demo", source=source)
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["demo"]
    manager = PluginManager(config, MemoryStore(":memory:"), permissions=PluginPermissionStore(None))

    async def scenario():
        manager.load_all()
        await asyncio.sleep(0)
        manager.apply_config_change("demo", {"a": 1}, {"a": 2})
        manager.set_enabled("demo", False)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert manager.extras["events"] == ["load", "config", "unload"]


# ----------------------------------------------------------------------
# 与授权联动
# ----------------------------------------------------------------------


def test_unauthorized_network_plugin_gets_no_lifecycle_hooks(tmp_path, caplog):
    root = tmp_path / "plugins"
    _make_plugin(root, "demo", permissions=["read_messages", "network"])
    manager = _manager(
        root, permissions=_migrated(tmp_path / "plugin_permissions.json")
    )
    events = manager.extras["events"]
    # register 总会执行（插件代码可信），但钩子按 network 授权收口
    assert events == ["register"]
    assert manager.plugins["demo"].lifecycle_active is False
    with caplog.at_level("WARNING", logger="rubenbot.plugins.permissions"):
        # 停用（此时没激活生命周期）与配置变更都不该把钩子发给未授权插件
        assert manager.set_enabled("demo", False) is True
        hot, message = manager.apply_config_change("demo", {"a": 1}, {"a": 2})
        manager.set_enabled("demo", True)
    assert events == ["register"]
    assert hot is False and "授权" in message
    assert any("on_load" in record.message for record in caplog.records)
    assert any("on_config_change" in record.message for record in caplog.records)

    # 插件在启用状态：授权恢复后就补发加载回调
    manager.set_permissions("demo", ["read_messages", "network"])
    assert events[-2:] == ["load", ("manifest", "demo")]
    # 撤回授权：未授权插件连收尾回调都不发，只记审计并解除生命周期标记
    with caplog.at_level("WARNING", logger="rubenbot.plugins.permissions"):
        manager.set_permissions("demo", ["read_messages"])
    assert events[-1] == ("manifest", "demo")
    assert any("on_unload" in record.message for record in caplog.records)
    assert manager.plugins["demo"].lifecycle_active is False
    # 再次恢复授权时补一次 on_load，状态保持一致
    manager.set_permissions("demo", ["read_messages", "network"])
    assert events[-2:] == ["load", ("manifest", "demo")]


def test_reload_keeps_permission_grants(tmp_path):
    manager = _manager(tmp_path / "plugins", permissions=PluginPermissionStore(None))
    manager.permissions.set_permissions("demo", ["send_messages"])
    before = manager.permissions.grants("demo")
    manager.reload_plugin("demo")
    assert manager.permissions.grants("demo") == before == {"send_messages"}
    assert manager.permissions.allows("demo", "network") is False


def test_reload_resets_registrations_but_not_lifecycle_of_disabled_plugin(tmp_path):
    """停用中的插件重载：重建注册，但不激活生命周期、不跑 on_load。"""
    root = tmp_path / "plugins"
    _make_plugin(root, "demo")
    manager = _manager(root)
    events = manager.extras["events"]
    manager.set_enabled("demo", False)
    assert events[-1] == "unload"

    result = manager.reload_plugin("demo")
    assert result == {"name": "demo", "enabled": False, "loaded": True}
    assert "register" in events
    assert manager.plugins["demo"].lifecycle_active is False
    assert events.count("load") == 1  # 只有最初那一次，重载没有补 on_load
    # 重新启用时补一次 on_load
    assert manager.set_enabled("demo", True) is True
    assert events[-2:] == ["load", ("manifest", "demo")]


# ----------------------------------------------------------------------
# 热重载
# ----------------------------------------------------------------------


V2_SOURCE = LIFECYCLE_SOURCE.replace(
    '    @ctx.on_message\n'
    '    def note(runtime, event):\n'
    '        events.append(("message", event.text))\n'
    '        return True\n',
    '    @ctx.on_message\n'
    '    def note(runtime, event):\n'
    '        events.append(("message-v2", event.text))\n'
    '        return True\n'
    '    @ctx.command("v2")\n'
    '    def v2(runtime, event, args):\n'
    '        return "v2"\n',
)


def test_reload_success_runs_unload_then_load_with_new_code(tmp_path):
    root = tmp_path / "plugins"
    directory = _make_plugin(root, "demo", hooks=["command"])
    manager = _manager(root)
    events = manager.extras["events"]
    assert events[:2] == ["register", "load"]

    (directory / "plugin.py").write_text(V2_SOURCE, encoding="utf-8")
    result = manager.reload_plugin("demo")
    assert result == {"name": "demo", "enabled": True, "loaded": True}
    # 旧实例先 on_unload，新代码重新 register 后再 on_load；钩子不会翻倍
    assert events[-4:] == ["unload", "register", "load", ("manifest", "demo")]
    assert events.count("register") == 2
    plugin = manager.plugins["demo"]
    assert "v2" in plugin.commands
    # 新实例的钩子生效：旧回调没有被保留下来翻倍
    asyncio.run(manager.run_message_hooks(None, _private("hi")))
    assert events[-1] == ("message-v2", "hi")
    assert len(plugin.message_hooks) == 1


def test_reload_failure_rolls_back_and_old_instance_keeps_running(tmp_path):
    root = tmp_path / "plugins"
    directory = _make_plugin(root, "demo", hooks=["command"])
    manager = _manager(root)
    manager.permissions.set_permissions("demo", ["send_messages"])
    old_hooks = list(manager.plugins["demo"].message_hooks)
    events = manager.extras["events"]

    (directory / "plugin.py").write_text("raise RuntimeError('新代码 import 就炸')\n", encoding="utf-8")
    with pytest.raises(PluginReloadError) as excinfo:
        manager.reload_plugin("demo")
    assert "回滚" in str(excinfo.value)
    assert "新代码 import 就炸" in str(excinfo.value)

    plugin = manager.plugins["demo"]
    assert plugin.enabled is True and plugin.loaded is True
    assert plugin.message_hooks == old_hooks  # 旧实例继续跑
    # 回滚后旧实例的加载回调补跑过一次（它在重载前已被卸载）
    assert events[-2:] == ["load", ("manifest", "demo")]
    asyncio.run(manager.run_message_hooks(None, _private("still")))
    assert events[-1] == ("message", "still")
    # 授权记录不因失败回滚而丢失
    assert manager.permissions.grants("demo") == {"send_messages"}


def test_reload_failure_on_invalid_manifest_restores_previous_state(tmp_path):
    root = tmp_path / "plugins"
    directory = _make_plugin(root, "demo")
    manager = _manager(root)
    (directory / "manifest.json").write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(PluginReloadError):
        manager.reload_plugin("demo")
    plugin = manager.plugins["demo"]
    assert plugin.valid is True  # manifest 快照随回滚恢复
    assert plugin.manifest is not None
    assert plugin.loaded is True


def test_reload_unknown_plugin_raises_keyerror(tmp_path):
    manager = _manager(tmp_path / "plugins")
    with pytest.raises(KeyError):
        manager.reload_plugin("ghost")


def test_reload_by_manifest_id_and_submodules(tmp_path):
    """重载按 manifest id 也能定位；插件自己 import 的子模块同样换成新代码。"""
    root = tmp_path / "plugins"
    source = (
        "from . import helper\n\n"
        "def register(ctx):\n"
        "    events = ctx.extras.setdefault('events', [])\n"
        "    events.append(('helper', helper.VALUE))\n"
    )
    directory = _make_plugin(root, "demo", source=source, id="demo-id")
    (directory / "helper.py").write_text("VALUE = 'v1'\n", encoding="utf-8")
    manager = _manager(root)
    events = manager.extras["events"]
    assert ("helper", "v1") in events

    (directory / "helper.py").write_text("VALUE = 'v2'\n", encoding="utf-8")
    result = manager.reload_plugin("demo-id")
    assert result["name"] == "demo"
    # 子模块被清掉后重新 import，拿到磁盘上的新值
    assert ("helper", "v2") in events


def test_reload_unloaded_plugin_only_validates(tmp_path):
    """未启用的插件重载只刷新 manifest，不执行插件代码、不跑钩子。"""
    root = tmp_path / "plugins"
    _make_plugin(root, "demo")
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = []
    manager = PluginManager(config, MemoryStore(":memory:"), permissions=PluginPermissionStore(None))
    manager.load_all()
    plugin = manager.plugins["demo"]
    assert plugin.loaded is False
    result = manager.reload_plugin("demo")
    assert result == {"name": "demo", "enabled": False, "loaded": False}
    assert manager.extras.get("events") is None
    assert plugin.valid is True


def test_shutdown_runs_unload_once(tmp_path):
    manager = _manager(tmp_path / "plugins")
    events = manager.extras["events"]
    manager.shutdown()
    assert events[-1] == "unload"
    # 已经卸载过的插件不重复收到 on_unload
    manager.shutdown()
    assert events.count("unload") == 1


def test_market_uninstall_runs_unload_before_removing_directory(tmp_path):
    """市场卸载删目录前先 on_unload：插件有机会关掉自己的定时任务与临时资源。"""
    from rubenbot.web.market import MarketService
    from rubenbot.web.state import WebState

    root = tmp_path / "plugins"
    _make_plugin(root, "demo")
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["demo"]
    state = WebState(config=config, store=MemoryStore(":memory:"), config_path=tmp_path / "config.yaml")
    manager = PluginManager(config, state.store, permissions=PluginPermissionStore(None))
    state.plugins = manager
    manager.load_all()
    events = manager.extras["events"]
    assert events[:2] == ["register", "load"]

    MarketService(state).uninstall("demo")
    assert events[-1] == "unload"
    assert "demo" not in manager.plugins
    assert not (root / "demo").exists()


# ----------------------------------------------------------------------
# WebUI：重载与配置热生效
# ----------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.state import WebState  # noqa: E402


@pytest.fixture()
def web(tmp_path):
    config = Config()
    config.webui.password = ""
    config.persona_path = str(tmp_path / "SOUL.md")
    config.plugins.dir = str(tmp_path / "plugins")
    state = WebState(config=config, store=MemoryStore(":memory:"), config_path=tmp_path / "config.yaml")
    manager = PluginManager(state.config, state.store, permissions=PluginPermissionStore(None))
    state.plugins = manager
    return TestClient(create_app(state)), state, manager


def test_webui_reload_route_keeps_grants_and_reports_errors(web, tmp_path):
    client, state, manager = web
    directory = _make_plugin(Path(state.config.plugins.dir), "demo")
    manager.config.plugins.enabled = ["demo"]
    manager.load_all()
    manager.set_permissions("demo", ["read_messages", "send_messages"])

    response = client.post("/api/plugins/demo/reload")
    assert response.status_code == 200
    payload = response.json()
    assert payload["restart_required"] is False
    assert "热重载" in payload["message"]
    assert manager.permissions.grants("demo") == {"read_messages", "send_messages"}

    # 新代码坏掉：409 带原因，插件仍在列表里且旧实例继续工作
    (directory / "plugin.py").write_text("raise RuntimeError('坏代码')\n", encoding="utf-8")
    failed = client.post("/api/plugins/demo/reload")
    assert failed.status_code == 409
    assert "回滚" in failed.json()["error"]
    names = {item["name"] for item in client.get("/api/plugins").json()["items"]}
    assert "demo" in names
    assert manager.plugins["demo"].message_hooks


def test_webui_reload_requires_running_bot(web):
    client, state, manager = web
    state.plugins = None
    assert client.post("/api/plugins/demo/reload").status_code == 409


def test_webui_save_config_hot_vs_restart(web, tmp_path):
    client, state, manager = web
    _make_plugin(
        Path(state.config.plugins.dir),
        "demo",
        config_schema=[{"key": "count", "type": "integer", "default": 1, "description": "数量"}],
    )
    manager.config.plugins.enabled = ["demo"]
    manager.load_all()
    path = "/api/plugins/demo/config"
    hot = client.put(path, json={"values": {"count": 2}})
    assert hot.status_code == 200
    payload = hot.json()
    assert payload["hot"] is True
    assert payload["restart_required"] is False
    assert "热生效" in payload["message"]
    assert ("config", 1, 2) in manager.extras["events"]

    # 插件不声明 on_config_change：如实提示需要重启（新插件也要有 schema 才能进配置页）
    root = Path(state.config.plugins.dir)
    source = LIFECYCLE_SOURCE.replace(
        '    @ctx.on_config_change\n'
        '    def config_changed(context, old, new):\n'
        '        events.append(("config", old.get("count"), new.get("count")))\n',
        "",
    )
    _make_plugin(
        root,
        "plain",
        source=source,
        config_schema=[{"key": "count", "type": "integer", "default": 1, "description": "数量"}],
    )
    manager.discover()
    manager.set_enabled("plain", True)
    plain = client.put(
        "/api/plugins/plain/config", json={"values": {"count": 3}}
    )
    assert plain.status_code == 200
    data = plain.json()
    assert data["hot"] is False
    assert data["restart_required"] is True
    assert "重启" in data["message"]
