"""装后授权：存储、迁移、执行期拦截、注入收口与 WebUI 开关。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.plugins.loader import PluginManager
from rubenbot.plugins.permissions import (
    PluginPermissionError,
    PluginPermissionStore,
    capability_permission,
)

from conftest import RecordingAdapter, ScriptedLLM, write_manifest



PLUGIN_SOURCE = '''
def register(ctx):
    state = ctx.extras.setdefault("perm", {"events": []})
    state["events"].append("register")

    @ctx.on_start
    def started():
        state["events"].append("start")

    @ctx.on_stop
    def stopped():
        state["events"].append("stop")

    @ctx.command("publish")
    async def publish(runtime, event, args):
        ok, detail = await ctx.call_capability("qzone.publish", content=args)
        return f"{ok}:{detail}"

    @ctx.command("read")
    def read(runtime, event, args):
        return str(runtime.store.count_memories())

    @ctx.command("send")
    async def send(runtime, event, args):
        adapter = runtime.adapters.get(event.platform)
        await adapter.send(event.chat_id, [], chat_type=event.chat_type)
        return "sent"

    @ctx.command("note")
    def note(runtime, event, args):
        return "note"

    def describe(event, adapter):
        from rubenbot.expression import CapabilityNote
        return CapabilityNote("命令回执", "我能回复 note。", "收到 /note 时用。")

    ctx.capability_note(describe, entry=note)
'''


# 测试插件默认声明除 network 外的全部权限：命令与钩子不会被 network 准入拦掉，
# 网络相关的用例自己显式声明 network。
_DEFAULT_PERMISSIONS = [
    "read_messages", "send_messages", "capabilities", "qzone_write", "scheduler", "llm",
]


def _make_plugin(root: Path, name: str = "demo", **manifest) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    manifest.setdefault("permissions", _DEFAULT_PERMISSIONS)
    write_manifest(directory, name, hooks=["command"], **manifest)
    return directory


def _manager(
    root: Path,
    *,
    path: Path | None = None,
    enabled=("demo",),
    permissions: PluginPermissionStore | None = None,
) -> PluginManager:
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = list(enabled)
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    store = MemoryStore(path.with_name("bot.sqlite") if path else ":memory:")
    if permissions is not None:
        store.set_meta("plugins:enable_trust:v1", "1")
    if permissions is None and path is not None:
        permissions = PluginPermissionStore(path)
    manager = PluginManager(config, store, permissions=permissions)
    manager.load_all()
    return manager


def _migrated_store(path: Path) -> PluginPermissionStore:
    """模拟「授权层早已落地」：迁移标记已写，之后的插件一律从空授权开始。"""
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


def _run_command(
    manager: PluginManager, text: str, *, bot_override=None
) -> tuple[list[str], RecordingAdapter]:
    adapter = RecordingAdapter()
    if bot_override is not None:
        # 插件经 extras["bot"] 调用；给一个只实现能力入口的假宿主
        manager.extras["bot"] = bot_override
        manager.current_runtime = _runtime(manager, adapter)
        asyncio.run(manager.handle_commands(manager.current_runtime, _private(text)))
        return adapter.texts, adapter
    bot = RubenBot(
        manager.config,
        manager.store,
        ScriptedLLM(),
        Persona("Ruben", "你是 Ruben"),
        plugins=manager,
    )
    bot.register_adapter(adapter)
    manager.extras["bot"] = bot
    asyncio.run(bot.handle_event(_private(text)))
    return adapter.texts, adapter


def _runtime(manager: PluginManager, adapter: RecordingAdapter):
    from types import SimpleNamespace

    return SimpleNamespace(
        store=manager.store,
        adapters={adapter.name: adapter},
        llm=None,
        extras={"bot": manager.extras["bot"]},
    )


# ----------------------------------------------------------------------
# 存储与迁移
# ----------------------------------------------------------------------


def test_new_install_defaults_to_no_grants(tmp_path):
    """首次升级之后才装进来的插件从空授权开始（默认拒绝）。"""
    path = tmp_path / "plugin_permissions.json"
    root = tmp_path / "plugins"
    _make_plugin(root, "existing")
    manager = _manager(root, path=path, enabled=["existing"])
    # 首次升级只补启用名单
    assert manager.permissions.grants("existing")

    # 授权文件已经有了，再放一个新插件：一项都不授
    _make_plugin(root, "later")
    manager.discover()
    assert manager.permissions.grants("later") == frozenset()


@pytest.mark.parametrize("existing", [[], ["read_messages"]])
def test_upgrade_fills_only_enabled_before_first_hook(tmp_path, caplog, existing):
    root = tmp_path / "plugins"
    declared = ["read_messages", "network", "scheduler"]
    _make_plugin(root, "demo", permissions=declared)
    _make_plugin(root, "off", permissions=declared)
    _make_plugin(root, "untouched", permissions=declared)
    path = tmp_path / "plugin_permissions.json"
    # 线上旧版即使 migrated=true，也可能有空记录或只授了一部分。
    path.write_text(json.dumps({"version": 1, "migrated": True, "plugins": {
        "demo": existing, "off": ["read_messages"],
    }}))
    with caplog.at_level("INFO", logger="rubenbot.plugins.permissions"):
        manager = _manager(root, path=path)
    assert manager.extras["perm"]["events"] == ["register", "start"]
    assert manager.permissions.grants("demo") == set(declared)
    assert json.loads(path.read_text())["plugins"] == {"demo": sorted(declared), "off": ["read_messages"]}
    assert sum("插件授权变更：授予 demo" in r.message for r in caplog.records) == len(declared) - len(existing)

    manager.permissions.revoke("demo", "network")
    manager.reload_plugin("demo")
    assert not manager.permissions.allows("demo", "network")
    again = _manager(root, path=path)
    assert not again.permissions.allows("demo", "network")
    assert again.extras["perm"]["events"] == ["register"]


def test_forget_clears_grants_for_reinstall(tmp_path):
    store = PluginPermissionStore(tmp_path / "plugin_permissions.json")
    store.set_permissions("demo", {"read_messages"})
    store.forget("demo")
    assert store.grants("demo") == frozenset()


def test_capability_permission_mapping():
    assert capability_permission("qzone.publish") == "qzone_write"
    assert capability_permission("send_sticker") == "capabilities"


# ----------------------------------------------------------------------
# 执行期拦截
# ----------------------------------------------------------------------


def test_unauthorized_capability_call_is_rejected_and_audited(tmp_path, caplog):
    _make_plugin(tmp_path / "plugins", "demo")
    path = tmp_path / "plugin_permissions.json"
    manager = _manager(tmp_path / "plugins", permissions=_migrated_store(path))
    # 只授发消息：调用能被拦下，命令回复还能发出来（否则什么都看不到）
    manager.set_permissions("demo", ["send_messages"])
    calls = []

    class FakeBot:
        async def call_capability(self, adapter_name, name, **kwargs):
            calls.append((adapter_name, name, kwargs))
            return True, "ok"

    manager.extras["bot"] = FakeBot()
    with caplog.at_level("WARNING", logger="rubenbot.plugins.permissions"):
        texts, _ = _run_command(manager, "/publish 今天不错")
    assert calls == []
    assert texts and "未获授权" in texts[0] and texts[0].startswith("False:")
    assert any(
        "demo" in record.message and "qzone_write" in record.message and "已拦截" in record.message
        for record in caplog.records
    )


def test_granted_capability_call_passes(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo")
    path = tmp_path / "plugin_permissions.json"
    manager = _manager(tmp_path / "plugins", permissions=_migrated_store(path))
    manager.set_permissions("demo", ["qzone_write", "send_messages"])
    calls = []

    class FakeBot:
        async def call_capability(self, adapter_name, name, **kwargs):
            calls.append((adapter_name, name, kwargs))
            return True, "ok: 1"

    texts, _ = _run_command(manager, "/publish 今天不错", bot_override=FakeBot())
    assert calls == [(None, "qzone.publish", {"content": "今天不错"})]
    assert texts == ["True:ok: 1"]


def test_revoke_takes_effect_immediately_for_existing_proxy(tmp_path):
    """权限撤回后，插件手里已经拿到的代理也立即失效。"""
    _make_plugin(tmp_path / "plugins", "demo")
    path = tmp_path / "plugin_permissions.json"
    manager = _manager(tmp_path / "plugins", permissions=_migrated_store(path))
    manager.set_permissions("demo", ["read_messages"])
    plugin = manager.plugins["demo"]
    from rubenbot.plugins.loader import PluginContext

    context = PluginContext(plugin, manager)
    store = context.store  # 先拿到代理
    assert store.count_memories() == 0
    manager.set_permissions("demo", [])
    with pytest.raises(PluginPermissionError):
        store.count_memories()


def test_store_read_requires_read_messages_and_writes_are_denied(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo")
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", [])
    from rubenbot.plugins.loader import PluginContext

    context = PluginContext(manager.plugins["demo"], manager)
    with pytest.raises(PluginPermissionError):
        context.store.recent_messages("g1")
    # 写入口没有对应授权项：永远拒绝，默认不放行
    with pytest.raises(PluginPermissionError):
        context.store.add_raw_message(platform="x", chat_id="g1", sender_id="u", sender_name="u", text="x")
    with pytest.raises(PluginPermissionError):
        context.store.set_meta("k", "v")


def test_network_and_llm_gates(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo")
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", [])

    class FakeLLM:
        async def complete(self, *args, **kwargs):
            return "ok"

    class FakeBot:
        llm = FakeLLM()

    manager.extras["bot"] = FakeBot()
    from rubenbot.plugins.loader import PluginContext

    context = PluginContext(manager.plugins["demo"], manager)

    async def no_network():
        with pytest.raises(PluginPermissionError):
            await context.fetch("https://example.test/")

    asyncio.run(no_network())
    # 未授权时连客户端都拿不到（默认拒绝）
    with pytest.raises(PluginPermissionError):
        context.llm

    manager.set_permissions("demo", ["llm"])
    assert context.llm is not None

    async def allowed():
        assert await context.llm.complete([]) == "ok"

    asyncio.run(allowed())


def test_network_unauthorized_plugin_hooks_are_skipped(tmp_path):
    """声明了 network 却没授权：钩子和命令都不分发。"""
    _make_plugin(tmp_path / "plugins", "demo", permissions=["read_messages", "send_messages", "network"])
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", ["read_messages", "send_messages"])
    assert manager._hooks_allowed(manager.plugins["demo"]) is False
    # 钩子与命令都不分发；消息落在普通回复通道
    manager.extras.setdefault("bot", None)

    class _NoBot:
        pass

    manager.extras["bot"] = _NoBot()
    runtime = _runtime(manager, RecordingAdapter())
    messages = asyncio.run(manager.run_before_reply(runtime, _private("点歌"), []))
    assert messages == []
    texts, _ = _run_command(manager, "/note")
    assert "note" not in texts


def test_scheduler_gate_controls_on_start(tmp_path):
    """声明了 scheduler 却没授权：插件加载了，但后台任务不启动。"""
    _make_plugin(tmp_path / "plugins", "demo", permissions=["scheduler"])
    path = tmp_path / "plugin_permissions.json"
    manager = _manager(tmp_path / "plugins", permissions=_migrated_store(path))
    manager.set_permissions("demo", [])
    events = manager.extras["perm"]["events"]
    assert events == ["register"]
    manager.set_permissions("demo", ["scheduler"])
    assert "start" in events
    manager.set_permissions("demo", [])
    assert events[-1] == "stop"


def test_command_reply_requires_send_messages(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo")
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", [])
    texts, _ = _run_command(manager, "/note")
    assert texts == []  # 没有 send_messages：宿主不代发
    manager.set_permissions("demo", ["send_messages"])
    texts, _ = _run_command(manager, "/note")
    assert texts == ["note"]


# ----------------------------------------------------------------------
# 注入收口
# ----------------------------------------------------------------------


def test_capability_notes_only_include_granted_permissions(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo", permissions=["read_messages", "send_messages"])
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    adapter = RecordingAdapter()
    event = _private("/note")
    manager.set_permissions("demo", ["read_messages"])
    assert manager.capability_notes(event, adapter) == []
    manager.set_permissions("demo", ["read_messages", "send_messages"])
    notes = manager.capability_notes(event, adapter)
    note, = notes
    assert note.title == "命令回执"
    assert note.evidence == ("plugin:demo:loaded", "entry:command:note", "permissions:granted")


def test_list_plugins_exposes_declared_vs_granted(tmp_path):
    _make_plugin(tmp_path / "plugins", "demo", permissions=["read_messages", "scheduler"])
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", ["read_messages"])
    item = manager.list_plugins()[0]
    summary = item["permissions"]
    assert summary["declared"] == ["read_messages", "scheduler"]
    assert summary["granted"] == ["read_messages"]
    assert summary["all_granted"] is False
    by_id = {meta["id"]: meta for meta in summary["permission_meta"]}
    assert by_id["read_messages"]["granted"] is True
    assert by_id["scheduler"]["granted"] is False
    assert by_id["scheduler"]["high_risk"] is True


# ----------------------------------------------------------------------
# WebUI 开关
# ----------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from rubenbot.web.app import create_app  # noqa: E402
from rubenbot.web.state import WebState  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    config = Config()
    config.webui.password = ""
    config.persona_path = str(tmp_path / "SOUL.md")
    config.plugins.dir = str(tmp_path / "plugins")
    store = MemoryStore(":memory:")
    state = WebState(config=config, store=store, config_path=tmp_path / "config.yaml")
    return TestClient(create_app(state)), state


@pytest.mark.parametrize("hot", [True, False])
def test_webui_enable_trusts_only_local_declarations_before_load(client, tmp_path, hot, caplog):
    client_obj, state = client
    root = Path(state.config.plugins.dir)
    declared = ["read_messages", "send_messages", "network"]
    directory = _make_plugin(root, "link_detail", permissions=declared)
    state.store.path = str(tmp_path / "bot.sqlite")
    # 钩子内实际使用按权限收口的入口；不接触真实网络。
    (directory / "plugin.py").write_text(
        'def register(ctx):\n'
        '    ctx.store.count_memories()\n'
        '    @ctx.on_load\n'
        '    def check(context, manifest):\n'
        '        context.extras["loaded"] = True\n'
    )
    if hot:
        state.plugins = PluginManager(state.config, state.store)
        state.plugins.load_all()
    assert client_obj.get("/api/plugins").json()["items"][0]["permissions"]["granted"] == []
    with caplog.at_level("INFO", logger="rubenbot.plugins.permissions"):
        response = client_obj.post("/api/plugins/link_detail", json={"enabled": True, "permissions": ["llm"]})
    assert response.status_code == 200
    assert response.json()["restart_required"] is not hot
    path = tmp_path / "plugin_permissions.json"
    assert json.loads(path.read_text())["plugins"]["link_detail"] == sorted(declared)
    assert any("授予 link_detail" in r.message and "network" in r.message for r in caplog.records)
    assert not any("已拦截" in r.message for r in caplog.records)
    if hot:
        assert state.plugins.extras["loaded"] is True
    detail = client_obj.get("/api/plugins/link_detail/detail").json()["plugin"]["permissions"]
    assert set(detail["declared"]) == set(detail["granted"]) == set(declared)
    assert all(row["granted"] for row in detail["items"])
    assert client_obj.put("/api/plugins/link_detail/permissions", json={"permissions": []}).status_code in (404, 405)


def test_unrelated_webui_save_is_not_an_enable_gesture(client):
    web, state = client
    root = Path(state.config.plugins.dir)
    _make_plugin(root, "demo", permissions=["network"])
    state.plugins = PluginManager(state.config, state.store)
    state.plugins.load_all()
    # 磁盘名单由别处更改。保存不相关设置可能同步运行状态，但不是显式授权。
    state.config_path.write_text(f'plugins:\n  dir: {root}\n  enabled: [demo]\n')
    response = web.put("/api/config", json={"config": {"chat": {"bot_name": "测试"}}})
    assert response.status_code == 200
    assert state.plugins.plugins["demo"].enabled
    assert not state.plugins.permissions.allows("demo", "network")


def test_enable_real_link_detail_allows_checked_fetch(client, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from rubenbot.plugins.loader import PluginContext

    web, state = client
    state.config.plugins.dir = str(Path(__file__).resolve().parents[1] / "plugins")
    state.store.path = str(tmp_path / "bot.sqlite")
    manager = state.plugins = PluginManager(state.config, state.store)
    manager.load_all()
    result = web.post("/api/plugins/link_detail", json={"enabled": True})
    assert result.status_code == 200
    assert manager.permissions.grants("link_detail") == {"read_messages", "send_messages", "network"}
    fetch = AsyncMock()
    monkeypatch.setattr("rubenbot.plugins.loader.public_web.fetch", fetch)
    ctx = PluginContext(manager.plugins["link_detail"], manager)
    asyncio.run(ctx.fetch_public_page("https://example.test/", allow_url=lambda url: True))
    fetch.assert_awaited_once()


def test_corrupt_grants_file_fails_closed(tmp_path):
    """授权文件损坏时按默认拒绝处理，不再当成首次升级自动全授。"""
    path = tmp_path / "plugin_permissions.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    store = PluginPermissionStore(path)
    assert store.migrated is True
    assert store.migrate({"demo": ["read_messages"]}) == []
    assert store.grants("demo") == frozenset()


def test_missing_grants_file_is_the_only_migration_trigger(tmp_path):
    store = PluginPermissionStore(tmp_path / "plugin_permissions.json")
    assert store.migrated is False
    assert store.migrate({"demo": ["read_messages"]}) == ["demo"]
    assert store.grants("demo") == {"read_messages"}


def test_network_gate_skipping_command_is_audited(tmp_path, caplog):
    _make_plugin(tmp_path / "plugins", "demo", permissions=["read_messages", "network"])
    manager = _manager(
        tmp_path / "plugins", permissions=_migrated_store(tmp_path / "plugin_permissions.json")
    )
    manager.set_permissions("demo", ["read_messages"])
    with caplog.at_level("WARNING", logger="rubenbot.plugins.permissions"):
        texts, _ = _run_command(manager, "/note")
    assert "note" not in texts
    assert any(
        "demo" in record.message and "network" in record.message and "执行命令 note" in record.message
        for record in caplog.records
    )
