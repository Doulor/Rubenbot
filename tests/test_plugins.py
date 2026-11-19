"""插件系统：加载、钩子分发、命令、禁用与异常隔离。"""

import asyncio
from pathlib import Path

import pytest

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.plugins.loader import PluginManager

from conftest import RecordingAdapter, ScriptedLLM

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
    bad = root / "broken"
    bad.mkdir()
    (bad / "plugin.py").write_text("raise RuntimeError('import 就炸')", encoding="utf-8")
    return root


def make_manager(plugin_dir: Path, enabled=None) -> PluginManager:
    config = Config()
    config.plugins.dir = str(plugin_dir)
    config.plugins.enabled = enabled
    return PluginManager(config, MemoryStore(":memory:"))


def test_load_all_skips_broken(plugin_dir):
    manager = make_manager(plugin_dir)
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["demo"]
    assert "broken" not in manager.plugins  # 加载失败的被移除
    info = {item["name"]: item for item in manager.list_plugins()}
    assert info["demo"]["commands"] == ["boom", "ping"]
    assert info["demo"]["hooks"]["on_message"] == 1


def test_disabled_plugin_not_registered(plugin_dir):
    manager = make_manager(plugin_dir, enabled=["other"])
    manager.load_all()
    demo = manager.plugins["demo"]
    assert demo.enabled is False
    assert demo.message_hooks == [] and demo.commands == {}


def test_load_missing_dir(tmp_path):
    manager = make_manager(tmp_path / "nope")
    assert manager.load_all() == []


def make_bot(plugin_dir: Path, replies=None) -> tuple[RubenBot, RecordingAdapter, PluginManager, ScriptedLLM]:
    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(plugin_dir)
    config.chat.split_delay_seconds = 0
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
    assert messages[-1] == {"role": "system", "content": "插件提示"}
    assert bot.context.extras["last_reply"] == "好的呀"
    assert bot.context.extras["seen"] == ["你好"]


def test_hook_exception_does_not_crash(plugin_dir):
    manager = make_manager(plugin_dir)
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
