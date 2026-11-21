"""插件系统：加载、钩子分发、命令、禁用与异常隔离。"""

import asyncio
from pathlib import Path

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.plugins.loader import PluginManager

from conftest import RecordingAdapter, ScriptedLLM, write_manifest

PLUGIN_SOURCE = '''
def register(ctx):
    @ctx.command("ping")
    async def ping(runtime, event, args):
        return f"pong {args}".strip()

    @ctx.command("boom")
    def boom(runtime, event, args):
        raise RuntimeError("插件炸了")

    @ctx.on_message
    def note(runtime, event):
        runtime.extras.setdefault("seen", []).append(event.text)
        if event.text == "屏蔽我":
            return False
        return True

    @ctx.on_before_reply
    async def add_hint(runtime, event, messages):
        return messages + [{"role": "system", "content": "插件提示"}]

    @ctx.on_after_reply
    def after(runtime, event, reply):
        runtime.extras["last_reply"] = reply
'''


@pytest.fixture()
def plugin_dir(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    demo = root / "demo"
    demo.mkdir(parents=True)
    (demo / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_manifest(demo, "demo")
    bad = root / "broken"
    bad.mkdir()
    (bad / "plugin.py").write_text("raise RuntimeError('import 就炸')", encoding="utf-8")
    write_manifest(bad, "broken")
    return root


def make_manager(plugin_dir: Path, enabled=None) -> PluginManager:
    config = Config()
    config.plugins.dir = str(plugin_dir)
    config.plugins.enabled = list(enabled or [])
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    return PluginManager(config, MemoryStore(":memory:"))


def test_load_all_skips_broken(plugin_dir):
    manager = make_manager(plugin_dir, enabled=["demo", "broken"])
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["demo"]
    assert "broken" not in manager.plugins  # 加载失败的被移除
    info = {item["name"]: item for item in manager.list_plugins()}
    assert info["demo"]["commands"] == ["boom", "ping"]
    assert info["demo"]["hooks"]["on_message"] == 1


def test_no_enabled_config_loads_no_plugins(plugin_dir):
    """未配置 enabled（插件默认全关）：发现插件但不加载、不注册任何钩子。"""
    manager = make_manager(plugin_dir)
    assert manager.config.plugins.enabled == []
    assert manager.load_all() == []
    demo = manager.plugins["demo"]
    assert demo.enabled is False and demo.loaded is False
    assert demo.message_hooks == [] and demo.commands == {}
    assert manager.enabled_plugins == []


def test_only_listed_plugins_load(plugin_dir):
    """enabled 列表是唯一依据：只加载列出的插件，未列出的保持禁用。"""
    manager = make_manager(plugin_dir, enabled=["demo"])
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["demo"]
    assert manager.plugins["demo"].enabled is True
    assert manager.plugins["broken"].enabled is False
    # broken 被显式列出但 import 就炸：只跳过它，不拖垮已启用的插件
    manager2 = make_manager(plugin_dir, enabled=["broken"])
    assert manager2.load_all() == []
    assert "broken" not in manager2.plugins


def test_enabled_name_missing_from_dir_is_ignored(plugin_dir, caplog):
    manager = make_manager(plugin_dir, enabled=["demo", "ghost"])
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["demo"]
    assert "ghost" not in manager.plugins


def test_default_plugins_config_enabled_is_empty():
    """配置默认值：不带 plugins 段时一个插件都不启用（旧语义是全部启用）。"""
    from rubenbot.config import PluginsConfig

    assert Config().plugins.enabled == []
    assert PluginsConfig().enabled == []
    # 旧配置里写 null 也按「全部关闭」处理，不能意外变成全部启用
    assert PluginsConfig(enabled=None).enabled == []
    assert PluginsConfig(enabled="hello").enabled == ["hello"]


def test_disabled_plugin_not_registered(plugin_dir):
    manager = make_manager(plugin_dir, enabled=["other"])
    manager.load_all()
    demo = manager.plugins["demo"]
    assert demo.enabled is False
    assert demo.message_hooks == [] and demo.commands == {}


def test_apply_enabled_hot_load_and_unload(plugin_dir):
    """WebUI 保存启用列表：热加载新启用的、停用被移除的，并同步配置。"""
    manager = make_manager(plugin_dir)
    manager.load_all()
    assert manager.enabled_plugins == []

    assert manager.apply_enabled(["demo"]) == []
    assert manager.plugins["demo"].enabled is True and manager.plugins["demo"].loaded is True
    assert manager.config.plugins.enabled == ["demo"]

    assert manager.apply_enabled([]) == []
    assert manager.plugins["demo"].enabled is False
    assert manager.enabled_plugins == []
    assert manager.config.plugins.enabled == []


def test_apply_enabled_report_unloadable(plugin_dir):
    """启用列表里的插件加载失败时如实返回失败名，不拖垮其它插件。"""
    manager = make_manager(plugin_dir)
    manager.load_all()
    assert manager.apply_enabled(["broken", "demo"]) == ["broken"]
    assert manager.plugins["demo"].enabled is True
    assert "broken" not in manager.plugins

    # 再次 load_all：已加载的插件不重复 register，仍按配置列表保持启用
    assert [p.name for p in manager.load_all()] == ["demo"]
    assert len(manager.plugins["demo"].message_hooks) == 1
    assert manager.plugins["demo"].enabled is True


def test_load_missing_dir(tmp_path):
    manager = make_manager(tmp_path / "nope")
    assert manager.load_all() == []


def make_bot(plugin_dir: Path, replies=None) -> tuple[RubenBot, RecordingAdapter, PluginManager, ScriptedLLM]:
    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(plugin_dir)
    config.plugins.enabled = ["demo"]
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, store)
    manager.load_all()
    llm = ScriptedLLM({"chat": replies or []})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, manager, llm


def private(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def test_command_handled_without_llm(plugin_dir):
    bot, adapter, _, llm = make_bot(plugin_dir)
    asyncio.run(bot.handle_event(private("/ping 你好")))
    assert adapter.texts == ["pong 你好"]
    assert llm.calls == []


def test_command_exception_contained(plugin_dir):
    bot, adapter, _, _ = make_bot(plugin_dir)
    asyncio.run(bot.handle_event(private("/boom")))
    assert adapter.texts == ["命令执行出错了"]


def test_unknown_command_falls_through(plugin_dir):
    bot, adapter, _, llm = make_bot(plugin_dir, replies=["嗯"])
    asyncio.run(bot.handle_event(private("/nonexistent")))
    assert adapter.texts == ["嗯"]


def test_on_message_can_block(plugin_dir):
    bot, adapter, _, llm = make_bot(plugin_dir, replies=["不该出现"])
    asyncio.run(bot.handle_event(private("屏蔽我")))
    assert adapter.sent == []
    assert llm.calls == []


def test_hooks_before_and_after(plugin_dir):
    bot, adapter, manager, llm = make_bot(plugin_dir, replies=["好的呀"])
    asyncio.run(bot.handle_event(private("你好")))
    messages = llm.chat_messages[0]
    assert messages[-2] == {"role": "system", "content": "插件提示"}
    assert bot.context.extras["last_reply"] == "好的呀"
    assert bot.context.extras["seen"] == ["你好"]


def test_hook_exception_does_not_crash(plugin_dir):
    manager = make_manager(plugin_dir, enabled=["demo"])
    manager.load_all()
    plugin = manager.plugins["demo"]

    def bad_hook(ctx, event):
        raise RuntimeError("钩子爆炸")

    plugin.message_hooks.insert(0, bad_hook)
    store = manager.store
    config = manager.config
    llm = ScriptedLLM({"chat": ["还在"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(private("hi")))
    assert adapter.texts == ["还在"]


def test_set_enabled_runtime_and_lazy_load(plugin_dir):
    """运行时启停：禁用后钩子不再分发；被跳过的插件启用时现场 register。"""
    manager = make_manager(plugin_dir, enabled=[])
    manager.load_all()
    demo = manager.plugins["demo"]
    assert demo.enabled is False and demo.loaded is False

    assert manager.set_enabled("demo", True) is True
    assert demo.enabled is True and demo.loaded is True
    assert "ping" in demo.commands

    assert manager.set_enabled("demo", False) is True
    assert demo.enabled is False
    assert manager.enabled_plugins == []

    assert manager.set_enabled("missing", True) is False
    # 加载失败的插件也能在启用时报错而不是拖垮宿主
    assert manager.set_enabled("broken", True) is False


def test_set_enabled_broken_plugin_returns_false(plugin_dir):
    manager = make_manager(plugin_dir, enabled=[])
    manager.load_all()
    # broken 插件 import 就炸，load_all 会把它移除；手工放回去模拟
    from rubenbot.plugins.loader import Plugin

    manager.plugins["broken"] = Plugin(name="broken", path=plugin_dir / "broken", enabled=False)
    assert manager.set_enabled("broken", True) is False
    assert manager.plugins["broken"].enabled is False


# ----------------------------------------------------------------------
# 能力通道与生命周期
# ----------------------------------------------------------------------

LIFECYCLE_SOURCE = '''
def register(ctx):
    runtime = ctx.extras.setdefault("life", {"events": [], "published": []})
    runtime["events"].append("register")

    @ctx.on_start
    async def started():
        runtime["events"].append("start")

    @ctx.on_stop
    async def stopped():
        runtime["events"].append("stop")

    @ctx.command("publish")
    async def publish(runtime_ctx, event, args):
        ok, detail = await ctx.call_capability("qzone.publish", content=args)
        return f"{ok}:{detail}"
'''


@pytest.fixture()
def lifecycle_dir(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    demo = root / "life"
    demo.mkdir(parents=True)
    (demo / "plugin.py").write_text(LIFECYCLE_SOURCE, encoding="utf-8")
    write_manifest(demo, "life", hooks=["command", "on_start", "on_stop"])
    return root


def test_plugin_can_call_capability(lifecycle_dir):
    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(lifecycle_dir)
    config.plugins.enabled = ["life"]
    config.reply.segment_delay_enabled = False
    manager = PluginManager(config, store)
    manager.load_all()

    calls = []

    class FakeBot:
        async def call_capability(self, adapter_name, name, **kwargs):
            calls.append((adapter_name, name, kwargs))
            return True, "ok: 1"

    manager.extras["bot"] = FakeBot()
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(private("/publish 今天不错")))
    assert calls == [(None, "qzone.publish", {"content": "今天不错"})]
    assert adapter.texts == ["True:ok: 1"]


def test_plugin_capability_without_bot_fails_gracefully(lifecycle_dir):
    manager = make_manager(lifecycle_dir, enabled=["life"])
    manager.load_all()

    async def scenario():
        # 直接取插件注册时闭包里的 ctx 不便，改为从 manager 重建一个上下文
        from rubenbot.plugins.loader import PluginContext

        context = PluginContext(manager.plugins["life"], manager)
        return await context.call_capability("qzone.publish", content="x")

    ok, reason = asyncio.run(scenario())
    assert ok is False and "未就绪" in reason


def test_lifecycle_start_stop_on_toggle(lifecycle_dir):
    manager = make_manager(lifecycle_dir, enabled=[])
    manager.load_all()
    plugin = manager.plugins["life"]
    assert plugin.enabled is False
    # 启用时 register + on_start 都会跑（此时没有事件循环，协程回调被丢弃）
    assert manager.set_enabled("life", True) is True
    assert plugin.enabled is True
    assert manager.extras["life"]["events"] == ["register"]

    async def scenario():
        # 有事件循环时重新禁用/启用，生命周期回调会被调度执行
        manager.set_enabled("life", False)
        await asyncio.sleep(0)
        manager.set_enabled("life", True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    events = manager.extras["life"]["events"]
    assert "stop" in events and "start" in events


def test_reregister_does_not_duplicate_hooks(lifecycle_dir):
    manager = make_manager(lifecycle_dir, enabled=[])
    manager.load_all()
    plugin = manager.plugins["life"]
    manager.set_enabled("life", True)
    first = len(plugin.message_hooks), len(plugin.start_hooks)
    manager.set_enabled("life", False)
    manager.set_enabled("life", True)
    assert (len(plugin.message_hooks), len(plugin.start_hooks)) == first
