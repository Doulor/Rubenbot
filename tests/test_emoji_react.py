"""贴表情插件：概率、关键词、冷却、能力不可用时静默。"""

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.plugins.loader import PluginManager

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugins" / "emoji_react" / "plugin.py"
LOGGER_NAME = "emoji_react_plugin"


def load_plugin_module():
    """按 loader 的方式从文件加载 plugin.py，拿到独立的模块对象。"""
    spec = importlib.util.spec_from_file_location(LOGGER_NAME, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要能从 sys.modules 里按模块名找到自己
    sys.modules[LOGGER_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin_module()


class FakeAdapter:
    name = "snowluma"
    capabilities = frozenset({"message.emoji_like"})
    capability_config_keys = {"message.emoji_like": "emoji_like"}
    capability_defaults = {}


class NoCapabilityAdapter:
    name = "console"
    capabilities = frozenset()
    capability_config_keys = {}
    capability_defaults = {}


class FakeBot:
    """假能力入口：记录调用，可控成功/失败。"""

    def __init__(self, adapters=None, *, config=None, result=(True, "ok"), raises=None):
        self.adapters = {adapter.name: adapter for adapter in (adapters or [FakeAdapter()])}
        self.config = config or Config()
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, str, dict]] = []

    async def call_capability(self, adapter_name, name, **kwargs):
        self.calls.append((adapter_name, name, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


class FixedRng:
    def __init__(self, value: float = 0.0):
        self.value = value

    def random(self) -> float:
        return self.value

    def choice(self, seq):
        return seq[0]


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def event(text="你好", *, chat_id="g1", message_id=9001) -> MessageEvent:
    raw = {"message_id": message_id} if message_id is not None else {}
    return MessageEvent(
        platform="snowluma",
        chat_type="group",
        chat_id=chat_id,
        sender_id="u1",
        sender_name="阿花",
        text=text,
        raw=raw,
    )


def runtime(bot: FakeBot) -> SimpleNamespace:
    return SimpleNamespace(extras={"bot": bot})


def make_reactor(bot, **overrides):
    config = {
        "cooldown_seconds": 120,
        "after_reply": {"probability": 0.25, "emojis": [13, 20]},
        "keywords": {
            "probability": 0.6,
            "groups": [
                {"keywords": ["哈哈", "笑死"], "emojis": [13, 20]},
                {"keywords": ["难过", "想哭"], "emojis": [78, 5]},
            ],
        },
    }
    config.update(overrides)
    return plugin.EmojiReactor(config, rng=FixedRng(0.0), clock=FakeClock())


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------


def test_register_registers_both_hooks():
    hooks = {"message": [], "after": []}

    class Ctx:
        def on_message(self, handler):
            hooks["message"].append(handler)
            return handler

        def on_after_reply(self, handler):
            hooks["after"].append(handler)
            return handler

    plugin.register(Ctx())
    assert len(hooks["message"]) == 1
    assert len(hooks["after"]) == 1


def test_default_config_and_shipped_yaml():
    cfg = plugin.load_config()
    assert cfg.enabled is True
    assert cfg.cooldown_seconds == 120
    # 默认概率：跟随回复 0.25，关键词 0.6
    assert cfg.after_reply_probability == 0.25
    assert cfg.keyword_probability == 0.6
    # 默认池与关键词表都非空，且每个表情 ID 都是整数
    assert cfg.after_reply_emojis
    assert cfg.keyword_groups
    assert all(isinstance(emoji, int) for emoji in cfg.after_reply_emojis)


def test_broken_config_falls_back_to_defaults(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("enabled: [unclosed", encoding="utf-8")
    cfg = plugin.load_config(bad)
    assert cfg.after_reply_probability == 0.25


# ----------------------------------------------------------------------
# 概率贴：跟随回复
# ----------------------------------------------------------------------


def test_follow_reply_sticks_emoji():
    bot = FakeBot()
    reactor = make_reactor(bot, after_reply={"probability": 1.0, "emojis": [13, 20]})
    ev = event("随便说点什么")

    asyncio.run(reactor.on_after_reply(runtime(bot), ev, "好的呀"))

    assert len(bot.calls) == 1
    adapter_name, capability, kwargs = bot.calls[0]
    assert adapter_name == "snowluma"
    assert capability == "message.emoji_like"
    # 贴的是被回复的那条用户消息，表情来自池子
    assert kwargs == {"message_id": 9001, "emoji_id": 13}


def test_follow_reply_probability_miss_skips():
    bot = FakeBot()
    reactor = make_reactor(bot, after_reply={"probability": 0.25, "emojis": [13]})
    reactor.rng = FixedRng(0.9)

    asyncio.run(reactor.on_after_reply(runtime(bot), event(), "嗯"))

    assert bot.calls == []


def test_empty_pool_skips():
    bot = FakeBot()
    reactor = make_reactor(bot, after_reply={"probability": 1.0, "emojis": []})

    asyncio.run(reactor.on_after_reply(runtime(bot), event(), "嗯"))

    assert bot.calls == []


# ----------------------------------------------------------------------
# 关键词贴
# ----------------------------------------------------------------------


def test_keyword_matches_and_sticks_group_emoji():
    bot = FakeBot()
    reactor = make_reactor(bot, keywords={"probability": 1.0, "groups": [
        {"keywords": ["哈哈", "笑死"], "emojis": [13, 20]},
    ]})

    asyncio.run(reactor.on_message(runtime(bot), event("笑死我了哈哈哈")))

    assert len(bot.calls) == 1
    _, capability, kwargs = bot.calls[0]
    assert capability == "message.emoji_like"
    assert kwargs["emoji_id"] in (13, 20)
    assert kwargs["message_id"] == 9001


def test_keyword_group_picks_matching_emojis():
    bot = FakeBot()
    reactor = make_reactor(bot, keywords={"probability": 1.0, "groups": [
        {"keywords": ["哈哈"], "emojis": [13, 20]},
        {"keywords": ["难过"], "emojis": [78, 5]},
    ]})

    asyncio.run(reactor.on_message(runtime(bot), event("有点难过")))

    assert bot.calls[0][2]["emoji_id"] in (78, 5)


def test_keyword_miss_no_call():
    bot = FakeBot()
    reactor = make_reactor(bot, keywords={"probability": 1.0, "groups": [
        {"keywords": ["哈哈"], "emojis": [13]},
    ]})

    asyncio.run(reactor.on_message(runtime(bot), event("今天天气不错")))

    assert bot.calls == []


def test_keyword_probability_miss_skips():
    bot = FakeBot()
    reactor = make_reactor(bot, keywords={"probability": 0.6, "groups": [
        {"keywords": ["哈哈"], "emojis": [13]},
    ]})
    reactor.rng = FixedRng(0.99)

    asyncio.run(reactor.on_message(runtime(bot), event("哈哈")))

    assert bot.calls == []


# ----------------------------------------------------------------------
# 冷却
# ----------------------------------------------------------------------


def test_cooldown_blocks_second_stick_in_same_chat():
    bot = FakeBot()
    clock = FakeClock(1000.0)
    reactor = plugin.EmojiReactor(
        {
            "cooldown_seconds": 120,
            "keywords": {"probability": 1.0, "groups": [
                {"keywords": ["哈哈"], "emojis": [13]},
            ]},
        },
        rng=FixedRng(0.0),
        clock=clock,
    )
    rt = runtime(bot)

    asyncio.run(reactor.on_message(rt, event("哈哈")))
    clock.now += 60  # 冷却期内
    asyncio.run(reactor.on_message(rt, event("哈哈")))
    assert len(bot.calls) == 1

    clock.now += 61  # 超过 120 秒，冷却结束
    asyncio.run(reactor.on_message(rt, event("哈哈")))
    assert len(bot.calls) == 2


def test_cooldown_is_per_chat():
    bot = FakeBot()
    reactor = make_reactor(bot, keywords={"probability": 1.0, "groups": [
        {"keywords": ["哈哈"], "emojis": [13]},
    ]})
    rt = runtime(bot)

    asyncio.run(reactor.on_message(rt, event("哈哈", chat_id="g1")))
    asyncio.run(reactor.on_message(rt, event("哈哈", chat_id="g2")))

    assert len(bot.calls) == 2


def test_cooldown_shared_between_paths():
    """同一条消息先触发关键词、回复后想再贴，会被冷却拦住。"""
    bot = FakeBot()
    reactor = make_reactor(
        bot,
        after_reply={"probability": 1.0, "emojis": [14]},
        keywords={"probability": 1.0, "groups": [{"keywords": ["哈哈"], "emojis": [13]}]},
    )
    rt = runtime(bot)
    ev = event("哈哈")

    asyncio.run(reactor.on_message(rt, ev))
    asyncio.run(reactor.on_after_reply(rt, ev, "笑什么"))

    assert len(bot.calls) == 1
    assert bot.calls[0][2]["emoji_id"] == 13


# ----------------------------------------------------------------------
# 能力不可用：静默
# ----------------------------------------------------------------------


def _plugin_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]


def test_capability_not_declared_is_silent(caplog):
    bot = FakeBot([NoCapabilityAdapter()])
    reactor = make_reactor(bot)
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    asyncio.run(reactor.on_message(runtime(bot), event("哈哈")))
    asyncio.run(reactor.on_after_reply(runtime(bot), event("哈哈"), "嗯"))

    assert bot.calls == []
    assert _plugin_warnings(caplog) == []
    # 只留下 debug 说明，便于排查
    assert any("跳过" in record.message for record in caplog.records)


def test_capability_disabled_in_config_is_silent(caplog):
    config = Config.from_dict(
        {"adapters": {"snowluma": {"capabilities": {"emoji_like": False}}}}
    )
    bot = FakeBot(config=config)
    reactor = make_reactor(bot)
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    asyncio.run(reactor.on_message(runtime(bot), event("哈哈")))

    assert bot.calls == []
    assert _plugin_warnings(caplog) == []


def test_plugin_disabled_in_config_skips():
    bot = FakeBot()
    reactor = make_reactor(bot, enabled=False)

    asyncio.run(reactor.on_message(runtime(bot), event("哈哈")))
    asyncio.run(reactor.on_after_reply(runtime(bot), event("哈哈"), "嗯"))

    assert bot.calls == []


def test_missing_message_id_skips():
    bot = FakeBot()
    reactor = make_reactor(bot)

    asyncio.run(reactor.on_message(runtime(bot), event("哈哈", message_id=None)))

    assert bot.calls == []


def test_missing_bot_channel_skips():
    reactor = make_reactor(FakeBot())
    # 老版本运行时没有 extras["bot"]，也不能报错
    asyncio.run(reactor.on_message(SimpleNamespace(), event("哈哈")))


def test_capability_exception_does_not_propagate(caplog):
    bot = FakeBot(raises=RuntimeError("能力炸了"))
    reactor = make_reactor(bot)
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    # 不抛异常就算通过
    asyncio.run(reactor.on_message(runtime(bot), event("哈哈")))

    assert len(bot.calls) == 1
    assert _plugin_warnings(caplog) == []


def test_failed_call_still_marks_cooldown():
    """调用失败也算贴过，冷却期内不反复打平台。"""
    bot = FakeBot(result=(False, "失败"))
    reactor = make_reactor(bot, keywords={"probability": 1.0, "groups": [
        {"keywords": ["哈哈"], "emojis": [13]},
    ]})
    rt = runtime(bot)

    asyncio.run(reactor.on_message(rt, event("哈哈")))
    asyncio.run(reactor.on_message(rt, event("哈哈")))

    assert len(bot.calls) == 1


# ----------------------------------------------------------------------
# 与真实 loader 的接线
# ----------------------------------------------------------------------


def test_loaded_by_plugin_manager():
    config = Config()
    config.plugins.dir = str(PLUGIN_FILE.parent.parent)
    config.plugins.enabled = ["emoji_react"]
    manager = PluginManager(config, store=None)
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["emoji_react"]

    fake_bot = FakeBot()
    # 用确定性随机源替换插件内部的 rng，便于断言一定触发
    hook = manager.plugins["emoji_react"].message_hooks[0]
    hook.__self__.rng = FixedRng(0.0)

    asyncio.run(manager.run_message_hooks(runtime(fake_bot), event("哈哈")))

    assert len(fake_bot.calls) == 1
    assert fake_bot.calls[0][2]["message_id"] == 9001


def test_end_to_end_through_core_with_real_bot():
    """真实 RubenBot 的钩子运行时里，插件能通过 extras["bot"] 调到能力入口。"""
    from rubenbot.core import RubenBot
    from rubenbot.memory.store import MemoryStore
    from rubenbot.persona import Persona

    class CapableAdapter(FakeAdapter):
        def __init__(self):
            self.calls: list[dict] = []

        async def start(self, on_event):
            pass

        async def send(self, chat_id, segments, *, chat_type="group"):
            pass

        async def stop(self):
            pass

        async def call_capability(self, name, **kwargs):
            self.calls.append({"name": name, **kwargs})
            return {"message_id": kwargs.get("message_id")}

    class SilentLLM:
        async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
            return "嗯嗯" if role == "chat" else "[]"

    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(PLUGIN_FILE.parent.parent)
    config.plugins.enabled = ["emoji_react"]
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, store)
    manager.load_all()
    hook = manager.plugins["emoji_react"].message_hooks[0]
    hook.__self__.rng = FixedRng(0.0)

    adapter = CapableAdapter()
    bot = RubenBot(config, store, SilentLLM(), Persona("Ruben", "你是 Ruben"), plugins=manager)
    bot.register_adapter(adapter)

    asyncio.run(bot.handle_event(event("笑死我了")))
    store.close()

    assert [call["name"] for call in adapter.calls] == ["message.emoji_like"]
    assert adapter.calls[0]["message_id"] == 9001
