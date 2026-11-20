"""sticker 插件：收集入库、概率与冷却决策、只经能力层发送。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.plugins.loader import PluginManager
from rubenbot.plugins.manifest import load_manifest
from rubenbot.stickers import StickerStore

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "sticker"
PLUGIN_FILE = PLUGIN_DIR / "plugin.py"
LOGGER_NAME = "sticker_plugin"


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
    capabilities = frozenset({"send_sticker"})
    capability_config_keys = {"send_sticker": "sticker"}
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
    """确定性随机源：random() 固定，choice() 取首个。"""

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


def event(text="你好", *, chat_id="g1", chat_type="group", segments=None) -> MessageEvent:
    return MessageEvent(
        platform="snowluma",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id="u1",
        sender_name="阿花",
        text=text,
        segments=list(segments or []),
    )


def image_segment(raw: bytes = b"png-bytes", mime: str = "image/png") -> Segment:
    return Segment(
        "image",
        {
            "base64": base64.b64encode(raw).decode("ascii"),
            "mime_type": mime,
            "filename": "cat.png",
        },
    )


def make_store(tmp_path) -> StickerStore:
    return StickerStore(tmp_path / "stickers")


def runtime(bot) -> SimpleNamespace:
    return SimpleNamespace(extras={"bot": bot})


def make_plugin(store, bot=None, **overrides):
    config = {
        "collect": True,
        "send_probability": 1.0,
        "cooldown_seconds": 300,
    }
    config.update(overrides)
    return plugin.StickerPlugin(config, store=store, rng=FixedRng(0.0), clock=FakeClock())


# ----------------------------------------------------------------------
# manifest 与注册
# ----------------------------------------------------------------------


def test_manifest_is_compliant_market_sample():
    manifest = load_manifest(PLUGIN_DIR)
    assert manifest.id == "sticker"
    assert set(manifest.permissions) == {"read_messages", "send_messages", "capabilities"}
    keys = {item["key"]: item for item in manifest.config_schema}
    assert set(keys) >= {"collect", "send_probability", "cooldown_seconds"}
    assert keys["collect"]["default"] is False
    assert keys["send_probability"]["default"] == 0.15
    assert keys["cooldown_seconds"]["default"] == 300
    assert set(manifest.hooks) == {"on_message", "on_after_reply"}


def test_register_registers_both_hooks_and_defaults():
    hooks = {"message": [], "after": []}

    class Ctx:
        extras: dict = {}
        bot = None

        def on_message(self, handler):
            hooks["message"].append(handler)
            return handler

        def on_after_reply(self, handler):
            hooks["after"].append(handler)
            return handler

    plugin.register(Ctx())
    assert len(hooks["message"]) == 1
    assert len(hooks["after"]) == 1
    assert Ctx.extras["sticker"].config.collect is False


def test_shipped_config_is_conservative():
    cfg = plugin.load_config()
    assert cfg.collect is False
    assert cfg.send_probability == 0.15
    assert cfg.cooldown_seconds == 300


def test_broken_config_falls_back_to_defaults(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("collect: [unclosed", encoding="utf-8")
    cfg = plugin.load_config(bad)
    assert cfg.collect is False
    assert cfg.send_probability == 0.15


def test_loaded_by_plugin_manager_is_disabled_by_default():
    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    manager = PluginManager(config, store=None)
    manager.load_all()
    # 插件目录里能看到，但默认不启用：不注册任何钩子
    assert "sticker" in manager.plugins
    assert manager.plugins["sticker"].enabled is False
    assert manager.plugins["sticker"].message_hooks == []


# ----------------------------------------------------------------------
# 收集
# ----------------------------------------------------------------------


def test_collect_off_by_default_does_nothing(tmp_path):
    store = make_store(tmp_path)
    sticker = plugin.StickerPlugin({}, store=store)
    asyncio.run(sticker.on_message(runtime(FakeBot()), event(segments=[image_segment()])))
    assert store.find_by_tags(None) == []


def test_collect_image_bytes_and_tags(tmp_path):
    store = make_store(tmp_path)
    sticker = make_plugin(store)
    asyncio.run(
        sticker.on_message(
            runtime(FakeBot()), event("今天猫猫好可爱", segments=[image_segment()])
        )
    )
    items = store.find_by_tags(None)
    assert len(items) == 1
    assert items[0].file
    # 正文话题被抽成标签（中文 bigram）
    assert "猫猫" in items[0].tags or "可爱" in items[0].tags
    # 平台原生引用也一起记下，发送时交给适配器翻译
    assert items[0].native_refs["snowluma"]["kind"] == "image"


def test_collect_dedupes_same_image(tmp_path):
    store = make_store(tmp_path)
    sticker = make_plugin(store)
    rt = runtime(FakeBot())
    asyncio.run(sticker.on_message(rt, event("猫猫", segments=[image_segment()])))
    asyncio.run(sticker.on_message(rt, event("狗狗", segments=[image_segment()])))
    items = store.find_by_tags(None)
    assert len(items) == 1
    # 重复收集把新标签并进来
    assert "狗狗" in items[0].tags or "狗" in items[0].tags


def test_collect_reference_only_segment(tmp_path):
    """没有 base64 的图片段只存平台引用，同样能入库。"""
    store = make_store(tmp_path)
    sticker = make_plugin(store)
    segment = Segment("image", {"file": "a.jpg", "url": "https://cdn.example/a.jpg"})
    asyncio.run(sticker.on_message(runtime(FakeBot()), event("看图", segments=[segment])))
    items = store.find_by_tags(None)
    assert len(items) == 1
    assert items[0].file == ""
    assert items[0].native_refs["snowluma"]["data"]["url"] == "https://cdn.example/a.jpg"


def test_collect_ignores_private_and_other_segments(tmp_path):
    store = make_store(tmp_path)
    sticker = make_plugin(store)
    rt = runtime(FakeBot())
    asyncio.run(
        sticker.on_message(
            rt, event("私聊", chat_type="private", segments=[image_segment()])
        )
    )
    asyncio.run(sticker.on_message(rt, event("文字", segments=[Segment.text("哈")])))
    assert store.find_by_tags(None) == []


def test_collect_swallows_store_errors(tmp_path):
    class BrokenStore:
        def add(self, *args, **kwargs):
            raise RuntimeError("库炸了")

    sticker = make_plugin(BrokenStore())
    # 不抛异常就算通过
    asyncio.run(sticker.on_message(runtime(FakeBot()), event(segments=[image_segment()])))


# ----------------------------------------------------------------------
# 发送：概率、冷却、选图
# ----------------------------------------------------------------------


def test_send_by_probability_and_cooldown(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one", tags=["猫"])
    bot = FakeBot()
    clock = FakeClock(1000.0)
    sticker = plugin.StickerPlugin(
        {"send_probability": 1.0, "cooldown_seconds": 300},
        store=store,
        rng=FixedRng(0.0),
        clock=clock,
    )
    rt = runtime(bot)

    asyncio.run(sticker.on_after_reply(rt, event("猫猫"), "可爱"))
    assert len(bot.calls) == 1
    adapter_name, capability, kwargs = bot.calls[0]
    assert adapter_name == "snowluma"
    assert capability == "send_sticker"
    assert kwargs["chat_id"] == "g1"
    assert kwargs["sticker"]["id"] == store.find_by_tags(None)[0].id

    clock.now += 120  # 冷却期内
    asyncio.run(sticker.on_after_reply(rt, event("猫猫"), "可爱"))
    assert len(bot.calls) == 1

    clock.now += 181  # 超过 300 秒
    asyncio.run(sticker.on_after_reply(rt, event("猫猫"), "可爱"))
    assert len(bot.calls) == 2


def test_send_probability_miss_skips(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    bot = FakeBot()
    sticker = make_plugin(store, send_probability=0.15)
    sticker.rng = FixedRng(0.9)

    asyncio.run(sticker.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_cooldown_is_per_chat(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    bot = FakeBot()
    sticker = make_plugin(store)
    rt = runtime(bot)

    asyncio.run(sticker.on_after_reply(rt, event(chat_id="g1"), "嗯"))
    asyncio.run(sticker.on_after_reply(rt, event(chat_id="g2"), "嗯"))
    assert len(bot.calls) == 2


def test_send_prefers_topic_matched_sticker(tmp_path):
    store = make_store(tmp_path)
    store.add(b"cat", tags=["猫猫"])
    store.add(b"dog", tags=["狗狗"])
    bot = FakeBot()
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(bot), event("猫猫在干嘛"), "在看猫"))
    assert bot.calls[0][2]["sticker"]["tags"] == ["猫猫"]


def test_send_falls_back_to_any_sticker(tmp_path):
    store = make_store(tmp_path)
    added = store.add(b"cat", tags=["猫猫"])
    bot = FakeBot()
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(bot), event("今天天气不错"), "是啊"))
    assert bot.calls[0][2]["sticker"]["id"] == added.id


def test_send_empty_store_skips(tmp_path):
    store = make_store(tmp_path)
    bot = FakeBot()
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_without_capable_adapter_skips(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    bot = FakeBot([NoCapabilityAdapter()])
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_capability_disabled_in_config_skips(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    config = Config.from_dict(
        {"adapters": {"snowluma": {"capabilities": {"sticker": False}}}}
    )
    bot = FakeBot(config=config)
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_failure_still_marks_cooldown(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    bot = FakeBot(result=(False, "平台失败"))
    clock = FakeClock(1000.0)
    sticker = plugin.StickerPlugin(
        {"send_probability": 1.0, "cooldown_seconds": 300},
        store=store,
        rng=FixedRng(0.0),
        clock=clock,
    )
    rt = runtime(bot)

    asyncio.run(sticker.on_after_reply(rt, event(), "嗯"))
    asyncio.run(sticker.on_after_reply(rt, event(), "嗯"))
    # 失败也算过了冷却，不反复打平台
    assert len(bot.calls) == 1


def test_send_exception_does_not_propagate(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    bot = FakeBot(raises=RuntimeError("能力炸了"))
    sticker = make_plugin(store)

    # 不抛异常就算通过
    asyncio.run(sticker.on_after_reply(runtime(bot), event(), "嗯"))
    assert len(bot.calls) == 1


def test_send_without_bot_channel_skips(tmp_path):
    store = make_store(tmp_path)
    store.add(b"one")
    sticker = make_plugin(store)

    # 老版本运行时没有 extras["bot"]，也不能报错
    asyncio.run(sticker.on_after_reply(SimpleNamespace(), event(), "嗯"))


def test_send_logs_debug_not_warning(tmp_path, caplog):
    store = make_store(tmp_path)
    store.add(b"one")
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    sticker = make_plugin(store)

    asyncio.run(sticker.on_after_reply(runtime(FakeBot([NoCapabilityAdapter()])), event(), "嗯"))
    assert not [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]
    assert any("跳过" in record.message for record in caplog.records)


# ----------------------------------------------------------------------
# 与真实 loader 的接线
# ----------------------------------------------------------------------


def test_loaded_by_plugin_manager_when_enabled(tmp_path):
    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    config.plugins.enabled = ["sticker"]
    manager = PluginManager(config, store=None)
    loaded = manager.load_all()
    assert [p.name for p in loaded] == ["sticker"]
    assert manager.plugins["sticker"].message_hooks
