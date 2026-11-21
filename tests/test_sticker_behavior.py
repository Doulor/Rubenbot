"""core 表情包行为测试：不依赖插件目录或插件加载器。"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import SEND_STICKER, MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.expression import ExpressionDecision
from rubenbot.persona import Persona
from rubenbot.sticker_behavior import StickerBehavior
from rubenbot.stickers import StickerStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png(payload: bytes = b"x") -> bytes:
    """造一份文件头合法的假 PNG；存储层如今按文件头嗅探格式。"""
    return PNG_MAGIC + payload


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


def image(raw=png(b"png-bytes")):
    """群聊里的真表情图（摘要为「[动画表情]」，收集时按 sticker 入库）。"""
    return Segment("image", {
        "base64": base64.b64encode(raw).decode(), "filename": "cat.png",
        "mime_type": "image/png", "summary": "[动画表情]",
    })


def make_behavior(store, **values):
    downloader = values.pop("downloader", _no_download)
    cfg = SimpleNamespace(enabled=True, collect=True, send_probability=1.0, cooldown_seconds=300.0)
    for key, value in values.items():
        setattr(cfg, key, value)
    # 默认不碰网络：真正的下载行为由专门用例注入假下载器
    return StickerBehavior(
        cfg, store_getter=lambda: store, rng=FixedRng(), clock=Clock(), downloader=downloader,
    )


async def _no_download(url, limit):
    return None


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


def test_collect_snowluma_mface_after_adapter_normalization(tmp_path):
    from rubenbot.adapters.snowluma import parse_onebot_event

    store = StickerStore(tmp_path / "stickers")
    behavior = make_behavior(store)
    inbound = parse_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": 10000,
            "group_id": 55555,
            "user_id": 12345,
            "message_id": 9001,
            "sender": {"nickname": "阿花"},
            "message": [{"type": "mface", "data": {"emoji_id": "商城-1"}}],
        }
    )
    assert inbound is not None
    asyncio.run(behavior.on_message(runtime(Bot(store)), inbound))
    item = store.find_by_tags(None)[0]
    assert item.file == ""
    assert item.native_refs["snowluma"] == {
        "kind": "mface",
        "data": {"emoji_id": "商城-1"},
    }


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
    segment = Segment("image", {"file": "a.jpg", "url": "https://cdn.example/a.jpg", "summary": "[动画表情]"})
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
    store.add(png(b"one"), tags=["猫"])
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
    store.add(png(b"one"))
    behavior = make_behavior(store, send_probability=0.15)
    behavior.rng = FixedRng(0.9)
    bot = Bot(store)
    asyncio.run(behavior.on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_cooldown_is_per_chat(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    bot = Bot(store)
    behavior = make_behavior(store)
    asyncio.run(behavior.on_after_reply(runtime(bot), event(chat_id="g1"), "嗯"))
    asyncio.run(behavior.on_after_reply(runtime(bot), event(chat_id="g2"), "嗯"))
    assert len(bot.calls) == 2


def test_topic_match_is_preferred(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"cat"), tags=["猫猫"])
    store.add(png(b"dog"), tags=["狗狗"])
    bot = Bot(store)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event("猫猫在干嘛"), "在看猫"))
    assert bot.calls[0][2]["sticker"]["tags"] == ["猫猫"]


def test_send_falls_back_to_any(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    added = store.add(png(b"cat"), tags=["猫猫"])
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
    store.add(png(b"one"))
    bot = Bot(store, [Adapter(capabilities=())])
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_capability_config_disabled_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    config = Config.from_dict({"adapters": {"snowluma": {"capabilities": {"sticker": False}}}})
    bot = Bot(store, config=config)
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert bot.calls == []


def test_send_failure_still_marks_cooldown(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    bot = Bot(store, result=(False, "平台失败"))
    behavior = make_behavior(store)
    rt = runtime(bot)
    asyncio.run(behavior.on_after_reply(rt, event(), "嗯"))
    asyncio.run(behavior.on_after_reply(rt, event(), "嗯"))
    assert len(bot.calls) == 1


def test_send_exception_isolated(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    bot = Bot(store, raises=RuntimeError("能力炸了"))
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert len(bot.calls) == 1


def test_without_bot_channel_skips(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    asyncio.run(make_behavior(store).on_after_reply(SimpleNamespace(), event(), "嗯"))


def test_probability_config_is_clamped():
    cfg = Config.from_dict({"stickers": {"enabled": True, "collect": True, "send_probability": 4, "cooldown_seconds": -1}})
    assert cfg.stickers.enabled and cfg.stickers.collect
    assert cfg.stickers.send_probability == 1.0 and cfg.stickers.cooldown_seconds == 0.0


@pytest.mark.parametrize("value, expected", [(-1, 0.0), (4, 1.0), (0.25, 0.25), (True, 0.30)])
def test_nonverbal_probability_config(value, expected):
    assert Config().stickers.nonverbal_probability == 0.30
    assert Config.from_dict({"stickers": {"nonverbal_probability": value}}).stickers.nonverbal_probability == expected


def test_unknown_platform_never_uses_another_adapter(tmp_path):
    store = StickerStore(tmp_path / "stickers")
    store.add(png(b"one"))
    bot = Bot(store, [Adapter(name="another")])
    asyncio.run(make_behavior(store).on_after_reply(runtime(bot), event(), "嗯"))
    assert not bot.calls


class StickerAdapter(RecordingAdapter):
    name = "snowluma"
    capabilities = frozenset({SEND_STICKER})


def sticker_bot(config, store, tmp_path, *, intent="接梗"):
    config.stickers.enabled = True
    config.stickers.dir = str(tmp_path / "stickers")
    config.stickers.nonverbal_probability = 1.0
    config.stickers.send_probability = 0.0
    config.generation.two_stage.enabled = True
    llm = ScriptedLLM({"fast": ['{"intent":"%s","increment":"接住猫猫梗"}' % intent], "chat": ["路过一只"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben。"))
    adapter = StickerAdapter()
    adapter.call_capability = AsyncMock(return_value={})
    bot.register_adapter(adapter)
    return bot, adapter


def bot_messages(store):
    return [row for row in store.recent_messages("g1", platform="snowluma") if row.sender_id == BOT_SENDER_ID]


@pytest.mark.parametrize("intent,proactive", [("接梗", True), ("短附和", False)])
def test_sticker_reply_records_success_once(config, store, tmp_path, intent, proactive):
    bot, adapter = sticker_bot(config, store, tmp_path)
    bot.stickers.add(png(b"dog"), tags=["狗狗"])
    tagged = bot.stickers.add(png(b"cat"), tags=["猫猫"])
    bot.plugins = SimpleNamespace(run_after_reply=AsyncMock(side_effect=RuntimeError("插件故障")))
    incoming = event("猫猫")
    decision = ExpressionDecision(intent, "碎片", "接梗", ())
    assert asyncio.run(bot.try_sticker_reply(incoming, decision, proactive=proactive, now=100.0))
    # 即使文字补发概率不为零，冷却也必须和纯表情回复共用。
    config.stickers.send_probability = 1.0
    asyncio.run(bot.sticker_behavior.on_after_reply(bot.context, incoming, "嗯"))
    adapter.call_capability.assert_awaited_once()
    args, kwargs = adapter.call_capability.await_args
    assert args == (SEND_STICKER,) and kwargs["sticker"]["id"] == tagged.id
    assert (kwargs["chat_id"], kwargs["chat_type"]) == ("g1", "group")
    assert [msg.text for msg in bot_messages(store)] == ["[表情]"]
    assert not store.pending_senders()
    assert bot.decision_engine.last_reply_at("snowluma:g1") == 100.0
    assert bot.decision_engine.active_reply_count("snowluma:g1", 100.0) == int(proactive)
    assert bot.decide(incoming, now=101.0).reason == "refractory"
    bot.plugins.run_after_reply.assert_awaited_once_with(bot.context, incoming, "[表情]")
    assert not adapter.sent


@pytest.mark.parametrize("scenario", ["disabled", "empty", "miss", "ineligible", "absent", "unsupported", "failed"])
def test_sticker_reply_fallback_does_not_count_as_speech(config, store, tmp_path, scenario):
    bot, adapter = sticker_bot(config, store, tmp_path)
    if scenario not in ("disabled", "empty"):
        bot.stickers.add(png(b"cat"))
    if scenario == "disabled":
        config.stickers.enabled = False
    elif scenario == "miss":
        config.stickers.nonverbal_probability = 0.0
    elif scenario == "unsupported":
        adapter.capabilities = frozenset()
    elif scenario == "failed":
        adapter.call_capability.side_effect = RuntimeError("未发出")
    intent = "提问" if scenario == "ineligible" else "短附和"
    decision = None if scenario == "absent" else ExpressionDecision(intent, "短句", "接住话题", ())
    bot.plugins = SimpleNamespace(run_after_reply=AsyncMock())
    assert not asyncio.run(bot.try_sticker_reply(event(), decision, proactive=True, now=100.0))
    assert adapter.call_capability.await_count == int(scenario == "failed")
    assert not bot_messages(store)
    assert bot.decision_engine.last_reply_at("snowluma:g1") is None
    assert bot.decision_engine.active_reply_count("snowluma:g1", 100.0) == 0
    bot.plugins.run_after_reply.assert_not_awaited()
    if scenario == "disabled":
        assert not (tmp_path / "stickers").exists()


@pytest.mark.parametrize("scenario", ["success", "empty", "disabled", "failed", "other_intent"])
def test_nonverbal_reply_through_handle_event(config, store, tmp_path, scenario):
    bot, adapter = sticker_bot(config, store, tmp_path, intent="提问" if scenario == "other_intent" else "接梗")
    if scenario != "empty":
        bot.stickers.add(png(b"cat"), tags=["猫猫"])
    if scenario == "disabled":
        config.stickers.enabled = False
    if scenario == "failed":
        adapter.call_capability.side_effect = RuntimeError("未发出")
    # 普通群消息确定性地通过意愿评分，验证接线也传递了主动插话标记。
    config.decision.base = 1.0
    config.decision.threshold = 0.1
    asyncio.run(bot.handle_event(event("猫猫")))
    key = "snowluma:g1"
    assert bot.decision_engine.active_reply_count(key, bot.decision_engine.last_reply_at(key)) == 1
    assert adapter.call_capability.await_count == int(scenario in ("success", "failed"))
    expected_text = [] if scenario == "success" else ["路过一只"]
    assert adapter.texts == expected_text
    assert len(bot.llm.chat_messages) == len(expected_text)
    assert [msg.text for msg in bot_messages(store)] == (expected_text or ["[表情]"])
