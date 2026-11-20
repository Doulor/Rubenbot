"""core 表情包行为测试：不依赖插件目录或插件加载器。"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.sticker_behavior import StickerBehavior
from rubenbot.stickers import StickerStore


class Adapter:
    def __init__(self, name="snowluma", capabilities=("send_sticker",)):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.capability_config_keys = {"send_sticker": "sticker"}
        self.capability_defaults = {}


class Bot:
    def __init__(self, store, adapters=None, *, result=(True, "ok"), raises=None, config=None):
        self.stickers = store
        self.adapters = {item.name: item for item in (adapters or [Adapter()])}
        self.config = config or Config()
        self.result = result
        self.raises = raises
        self.calls = []

    async def call_capability(self, adapter, name, **kwargs):
        self.calls.append((adapter, name, kwargs))
        if self.raises:
            raise self.raises
        return self.result


class FixedRng:
    def __init__(self, value=0.0):
        self.value = value

    def random(self):
        return self.value

    def choice(self, values):
        return values[0]


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def event(text="你好", *, chat_id="g1", chat_type="group", segments=None):
    return MessageEvent(
        platform="snowluma", chat_type=chat_type, chat_id=chat_id,
        sender_id="u1", sender_name="阿花", text=text, segments=list(segments or []),
    )


def image(raw=b"png-bytes"):
    return Segment("image", {"base64": base64.b64encode(raw).decode(), "filename": "cat.png", "mime_type": "image/png"})


def make_behavior(store, **values):
    cfg = SimpleNamespace(enabled=True, collect=True, send_probability=1.0, cooldown_seconds=300.0)
    for key, value in values.items():
        setattr(cfg, key, value)
    return StickerBehavior(cfg, store_getter=lambda: store, rng=FixedRng(), clock=Clock())


def runtime(bot):
    return SimpleNamespace(extras={"bot": bot})


def test_disabled_is_zero_action(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store, enabled=False)
    bot = Bot(store)
    asyncio.run(behavior.on_message(runtime(bot), event(segments=[image()])))
    asyncio.run(behavior.on_after_reply(runtime(bot), event(), "嗯"))
    assert store.find_by_tags(None) == [] and bot.calls == []


def test_collect_image_and_topic_tags(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store)
    asyncio.run(behavior.on_message(runtime(Bot(store)), event("今天猫猫好可爱", segments=[image()])))
    item = store.find_by_tags(None)[0]
    assert item.file and ("猫猫" in item.tags or "可爱" in item.tags)
    assert item.native_refs["snowluma"]["kind"] == "image"


def test_collect_deduplicates_and_merges_tags(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store)
    bot = Bot(store)
    asyncio.run(behavior.on_message(runtime(bot), event("猫猫", segments=[image()])))
    asyncio.run(behavior.on_message(runtime(bot), event("狗狗", segments=[image()])))
    assert len(store.find_by_tags(None)) == 1
    assert "狗狗" in store.find_by_tags(None)[0].tags or "狗" in store.find_by_tags(None)[0].tags


def test_collect_reference_only_segment(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store)
    segment = Segment("image", {"file": "a.jpg", "url": "https://cdn.example/a.jpg"})
    asyncio.run(behavior.on_message(runtime(Bot(store)), event("看图", segments=[segment])))
    item = store.find_by_tags(None)[0]
    assert item.file == "" and item.native_refs["snowluma"]["data"]["url"].startswith("https://")


def test_collect_ignores_private_and_other_segments(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store)
    bot = Bot(store)
    asyncio.run(behavior.on_message(runtime(bot), event(chat_type="private", segments=[image()])))
    asyncio.run(behavior.on_message(runtime(bot), event(segments=[Segment.text("哈")])))
    assert store.find_by_tags(None) == []


def test_collect_false_does_nothing(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store, collect=False)
    asyncio.run(behavior.on_message(runtime(Bot(store)), event(segments=[image()])))
    assert store.find_by_tags(None) == []


def test_collect_store_error_isolated(tmp_path):
    class Broken:
        def add(self, *args, **kwargs):
            raise RuntimeError("库炸了")
    behavior = make_behavior(Broken())
    asyncio.run(behavior.on_message(runtime(Bot(Broken())), event(segments=[image()])))


def test_send_probability_and_cooldown(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one", tags=["猫"])
    bot = Bot(store)
    clock = Clock()
    behavior = StickerBehavior(SimpleNamespace(enabled=True, collect=False, send_probability=1.0, cooldown_seconds=300), store_getter=lambda: store, rng=FixedRng(), clock=clock)
    rt = runtime(bot)
    asyncio.run(behavior.on_after_reply(rt, event("猫猫"), "可爱"))
    clock.value += 120
    asyncio.run(behavior.on_after_reply(rt, event("猫猫"), "可爱"))
    clock.value += 181
    asyncio.run(behavior.on_after_reply(rt, event("猫猫"), "可爱"))
    assert len(bot.calls) == 2 and bot.calls[0][2]["chat_id"] == "g1"


def test_send_probability_miss(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    behavior = make_behavior(store, send_probability=0.15)
    behavior.rng = FixedRng(0.9)
    bot = Bot(store)
    asyncio.run(behavior.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_cooldown_is_per_chat(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    bot = Bot(store)
    behavior = make_behavior(store)
    asyncio.run(behavior.on_after_reply(runtime(bot), event(chat_id="g1"), "嗯"))
    asyncio.run(behavior.on_after_reply(runtime(bot), event(chat_id="g2"), "嗯"))
    assert len(bot.calls) == 2


def test_topic_match_is_preferred(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"cat", tags=["猫猫"])
    store.add(b"dog", tags=["狗狗"])
    bot = Bot(store)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event("猫猫在干嘛"), "在看猫"))
    assert bot.calls[0][2]["sticker"]["tags"] == ["猫猫"]


def test_send_falls_back_to_any(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    added = store.add(b"cat", tags=["猫猫"])
    bot = Bot(store)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event("天气不错"), "是啊"))
    assert bot.calls[0][2]["sticker"]["id"] == added.id


def test_empty_store_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    bot = Bot(store)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_without_capability_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    bot = Bot(store, [Adapter(capabilities=())])
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_capability_config_disabled_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    config = Config.from_dict({"adapters": {"snowluma": {"capabilities": {"sticker": False}}}})
    bot = Bot(store, config=config)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_failure_still_marks_cooldown(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    bot = Bot(store, result=(False, "平台失败"))
    behavior = make_behavior(store)
    rt = runtime(bot)
    asyncio.run(behavior.on_after_reply(rt, event(), "嗯"))
    asyncio.run(behavior.on_after_reply(rt, event(), "嗯"))
    assert len(bot.calls) == 1


def test_send_exception_isolated(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    bot = Bot(store, raises=RuntimeError("能力炸了"))
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert len(bot.calls) == 1


def test_without_bot_channel_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(b"one")
    asyncio.run(make_behavior(store).on_after_reply(SimpleNamespace(), event(), "嗯"))


def test_probability_config_is_clamped():
    cfg = Config.from_dict({"stickers": {"enabled": True, "collect": True, "send_probability": 4, "cooldown_seconds": -1}})
    assert cfg.stickers.enabled and cfg.stickers.collect
    assert cfg.stickers.send_probability == 1.0 and cfg.stickers.cooldown_seconds == 0.0
