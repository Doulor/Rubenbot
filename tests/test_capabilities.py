"""通用消息段与适配器能力：解析、出站映射、能力门控。"""

import asyncio
import json

import httpx
import pytest

from rubenbot.adapters.base import (
    CapabilityNotSupported,
    MessageEvent,
    Segment,
    render_segments,
    text_segments,
)
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.adapters.snowluma import (
    SnowLumaAdapter,
    onebot_message_segments,
    onebot_to_segments,
    parse_onebot_event,
    segment_to_onebot,
)
from rubenbot.core import RubenBot
from rubenbot.persona import Persona

from conftest import ScriptedLLM


# ----------------------------------------------------------------------
# 入站：OneBot 消息数组 -> segments + text 投影
# ----------------------------------------------------------------------


def group_payload(**overrides):
    payload = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10000,
        "group_id": 55555,
        "user_id": 12345,
        "message_id": 9001,
        "sender": {"user_id": 12345, "nickname": "阿花", "card": "花姐"},
        "message": [],
        "raw_message": "",
    }
    payload.update(overrides)
    return payload


MIXED_MESSAGE = [
    {"type": "reply", "data": {"id": "777"}},
    {"type": "at", "data": {"qq": "10000"}},
    {"type": "text", "data": {"text": "看我拍的"}},
    {
        "type": "image",
        "data": {"file": "a.jpg", "url": "http://cdn.example/a.jpg", "summary": "[图片]"},
    },
    {"type": "face", "data": {"id": 178}},
    {"type": "text", "data": {"text": "好看吗"}},
]


def test_inbound_mixed_message_to_segments_and_text():
    event = parse_onebot_event(group_payload(message=MIXED_MESSAGE))
    assert event is not None
    assert [segment["type"] for segment in event.segments] == [
        "reply",
        "at",
        "text",
        "image",
        "face",
        "text",
    ]
    # 图片保留 file/url，便于后续下载或转发
    image = event.segments[3]
    assert image["data"]["file"] == "a.jpg"
    assert image["data"]["url"] == "http://cdn.example/a.jpg"
    # text 是纯文本投影：@ 保留、图片/表情变占位符、reply 不进入正文
    assert event.text == "@10000 看我拍的[图片][表情]好看吗"
    assert event.reply_to_id == "777"
    assert event.mentions_bot is True
    # 段本身可当普通 dict 用（JSON 序列化、下标访问）
    assert json.loads(json.dumps(event.segments[0])) == {"type": "reply", "data": {"id": "777"}}


def test_inbound_media_placeholders():
    message = [
        {"type": "text", "data": {"text": "音频"}},
        {"type": "record", "data": {"file": "a.amr"}},
        {"type": "video", "data": {"file": "b.mp4"}},
        {"type": "file", "data": {"file": "c.pdf", "name": "说明.pdf"}},
    ]
    event = parse_onebot_event(group_payload(message=message))
    assert [segment["type"] for segment in event.segments] == ["text", "record", "video", "file"]
    assert event.text == "音频[语音][视频][文件]"
    assert event.segments[3]["data"]["name"] == "说明.pdf"


def test_inbound_string_cq_codes():
    message = "[CQ:at,qq=10000] 你好[CQ:face,id=1]"
    event = parse_onebot_event(
        group_payload(message=message, raw_message=message)
    )
    assert event.text == "@10000 你好"
    assert [segment["type"] for segment in event.segments] == ["at", "text"]


def test_inbound_mface_is_normalized_to_sticker():
    message = [{"type": "mface", "data": {"summary": "[动画表情]"}}]
    segments = onebot_message_segments(message)
    assert segments == [
        Segment(
            "sticker",
            {"native_ref": {"kind": "mface", "data": {"summary": "[动画表情]"}}},
        )
    ]
    assert onebot_to_segments(message) == "[表情]"


def test_inbound_without_message_field():
    event = parse_onebot_event(group_payload(message=None, raw_message=""))
    assert event.segments == []
    assert event.text == ""


# ----------------------------------------------------------------------
# 出站：segments -> OneBot 段
# ----------------------------------------------------------------------


def test_segment_to_onebot_all_types():
    assert segment_to_onebot(Segment.text("hi")) == {"type": "text", "data": {"text": "hi"}}
    assert segment_to_onebot(Segment.at(10000)) == {"type": "at", "data": {"qq": "10000"}}
    assert segment_to_onebot(Segment.reply(42)) == {"type": "reply", "data": {"id": "42"}}
    assert segment_to_onebot(Segment.face(178)) == {"type": "face", "data": {"id": "178"}}
    # 普通 dict 也能直接用
    assert segment_to_onebot({"type": "text", "data": {"text": "dict"}}) == {
        "type": "text",
        "data": {"text": "dict"},
    }


def test_segment_to_onebot_image_inputs():
    assert segment_to_onebot(Segment.image("http://x/a.png")) == {
        "type": "image",
        "data": {"file": "http://x/a.png"},
    }
    # file 优先
    assert segment_to_onebot(Segment("image", {"file": "/tmp/a.png", "url": "http://x/b"})) == {
        "type": "image",
        "data": {"file": "/tmp/a.png"},
    }
    # url 次之
    assert segment_to_onebot(Segment("image", {"url": "http://x/b.png"})) == {
        "type": "image",
        "data": {"file": "http://x/b.png"},
    }
    # base64 补 base64:// 前缀
    assert segment_to_onebot(Segment("image", {"base64": "QUJD"})) == {
        "type": "image",
        "data": {"file": "base64://QUJD"},
    }
    assert segment_to_onebot(Segment("image", {"base64": "base64://QUJD"})) == {
        "type": "image",
        "data": {"file": "base64://QUJD"},
    }


def test_segment_to_onebot_media_and_unknown():
    assert segment_to_onebot(Segment("record", {"file": "a.amr"})) == {
        "type": "record",
        "data": {"file": "a.amr"},
    }
    assert segment_to_onebot(Segment("file", {"url": "http://x/c.pdf"})) == {
        "type": "file",
        "data": {"file": "http://x/c.pdf"},
    }
    # 表情段（sticker）直接交给 OneBot 的 mface（QQ 原生表情）
    assert segment_to_onebot(Segment("sticker", {"native_ref": {"emoji_id": "1"}})) == {
        "type": "mface",
        "data": {"emoji_id": "1"},
    }
    # 未知类型退化为文本占位，不丢消息
    assert segment_to_onebot(Segment("mface", {"summary": "x"})) == {
        "type": "text",
        "data": {"text": "[mface]"},
    }


def make_adapter(handler) -> SnowLumaAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="http://127.0.0.1:3000", transport=transport)
    return SnowLumaAdapter(http_client=client)


def test_send_segments_request_body():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 42}})

    adapter = make_adapter(handler)
    segments = [
        Segment.reply(9001),
        Segment.at("12345"),
        Segment.text(" 看这张"),
        Segment.image("http://x/a.png"),
        Segment.face("178"),
        Segment("record", {"file": "a.amr"}),
    ]

    async def scenario():
        await adapter.send("55555", segments, chat_type="group")
        await adapter._client.aclose()

    asyncio.run(scenario())
    path, body = calls[0]
    assert path == "/send_group_msg"
    assert body["group_id"] == 55555
    assert body["message"] == [
        {"type": "reply", "data": {"id": "9001"}},
        {"type": "at", "data": {"qq": "12345"}},
        {"type": "text", "data": {"text": " 看这张"}},
        {"type": "image", "data": {"file": "http://x/a.png"}},
        {"type": "face", "data": {"id": "178"}},
        {"type": "record", "data": {"file": "a.amr"}},
    ]


# ----------------------------------------------------------------------
# Console 与非文本段的文本渲染
# ----------------------------------------------------------------------


def test_console_inbound_has_text_segment():
    adapter = ConsoleAdapter()
    event = adapter.parse_line("[摸鱼群] 大家好啊")
    assert event.segments == [Segment.text("大家好啊")]
    assert event.text == "大家好啊"


def test_console_send_renders_placeholders():
    import io

    out = io.StringIO()
    adapter = ConsoleAdapter(stdout=out)
    segments = [
        Segment.text("看"),
        Segment.image("http://x/a.png"),
        Segment("record", {"file": "a.amr"}),
        Segment.face("1"),
    ]
    asyncio.run(adapter.send("摸鱼群", segments))
    assert "Ruben: 看[图片][语音][表情]" in out.getvalue()


def test_render_segments_and_unknown():
    assert render_segments([Segment.text("a"), Segment.reply(1), Segment.at("2")]) == "a@2 "
    assert render_segments(None) == ""
    assert render_segments([{"type": "text", "data": {"text": "dict"}}]) == "dict"
    assert Segment("weird", {}).render() == "[weird]"


def test_text_segments_helper():
    assert text_segments("hi") == [Segment.text("hi")]


# ----------------------------------------------------------------------
# 能力门控
# ----------------------------------------------------------------------


def make_bot(config, store, *, adapters, capabilities=None) -> RubenBot:
    if capabilities is not None:
        config.adapters["snowluma"] = {"capabilities": capabilities}
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    for adapter in adapters:
        bot.register_adapter(adapter)
    return bot


class Recorder:
    """假 HTTP 层：记录请求；可指定失败响应。"""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content)))
        if self.fail:
            return httpx.Response(200, json={"status": "failed", "retcode": 100})
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 1}})


def test_capabilities_declared():
    assert {"message.emoji_like", "qzone.publish", "send_sticker"} <= SnowLumaAdapter.capabilities
    # Console 只多一个「发表情」的文本占位能力，别的平台能力仍然没有
    assert ConsoleAdapter.capabilities == frozenset({"send_sticker"})


def test_capability_disabled_returns_false_without_request(config, store):
    recorder = Recorder()
    adapter = make_adapter(recorder.handler)
    bot = make_bot(
        config,
        store,
        adapters=[adapter],
        capabilities={"emoji_like": False, "qzone": False},
    )

    async def scenario():
        result = await bot.call_capability(
            "snowluma", "message.emoji_like", message_id=9001, emoji_id=66
        )
        await adapter._client.aclose()
        return result

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "关闭" in reason
    assert recorder.calls == []


def test_capability_enabled_calls_extension_action(config, store):
    recorder = Recorder()
    adapter = make_adapter(recorder.handler)
    bot = make_bot(config, store, adapters=[adapter], capabilities={"emoji_like": True})

    async def scenario():
        result = await bot.call_capability(
            "snowluma", "message.emoji_like", message_id=9001, emoji_id=66
        )
        await adapter._client.aclose()
        return result

    ok, reason = asyncio.run(scenario())
    assert ok is True
    assert reason.startswith("ok")
    assert recorder.calls == [
        ("/set_msg_emoji_like", {"message_id": 9001, "emoji_id": "66"})
    ]


def test_qzone_disabled_by_default(config, store):
    """配置里只开 emoji_like 时，qzone 仍保持默认关闭。"""
    recorder = Recorder()
    adapter = make_adapter(recorder.handler)
    bot = make_bot(config, store, adapters=[adapter], capabilities={"emoji_like": True})

    async def scenario():
        result = await bot.call_capability("snowluma", "qzone.publish", content="你好")
        await adapter._client.aclose()
        return result

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "qzone" in reason
    assert recorder.calls == []


def test_qzone_enabled_sends_content(config, store):
    recorder = Recorder()
    adapter = make_adapter(recorder.handler)
    bot = make_bot(config, store, adapters=[adapter], capabilities={"qzone": True})

    async def scenario():
        result = await bot.call_capability("snowluma", "qzone.publish", content="今天天气不错")
        await adapter._client.aclose()
        return result

    ok, _ = asyncio.run(scenario())
    assert ok is True
    assert recorder.calls == [("/send_qzone_msg", {"content": "今天天气不错"})]


def test_capability_failure_is_contained(config, store):
    recorder = Recorder(fail=True)
    adapter = make_adapter(recorder.handler)
    bot = make_bot(config, store, adapters=[adapter], capabilities={"emoji_like": True})

    async def scenario():
        result = await bot.call_capability(
            "snowluma", "message.emoji_like", message_id=1, emoji_id=2
        )
        await adapter._client.aclose()
        return result

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "失败" in reason


def test_capability_missing_params(config, store):
    adapter = make_adapter(lambda request: httpx.Response(200, json={"status": "ok"}))
    bot = make_bot(config, store, adapters=[adapter], capabilities={"emoji_like": True})

    async def scenario():
        result = await bot.call_capability("snowluma", "message.emoji_like", message_id=1)
        await adapter._client.aclose()
        return result

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "emoji_id" in reason


def test_console_capability_fails_gracefully(config, store):
    adapter = ConsoleAdapter()
    bot = make_bot(config, store, adapters=[adapter])

    async def scenario():
        return await bot.call_capability("console", "message.emoji_like", message_id=1, emoji_id=2)

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "不支持" in reason


def test_unknown_or_offline_adapter_fails(config, store):
    bot = make_bot(config, store, adapters=[ConsoleAdapter()])

    async def scenario():
        missing = await bot.call_capability("snowluma", "message.emoji_like", message_id=1)
        none_declared = await bot.call_capability("console", "qzone.publish", content="x")
        return missing, none_declared

    (ok1, reason1), (ok2, reason2) = asyncio.run(scenario())
    assert ok1 is False and "不在线" in reason1
    assert ok2 is False and "不支持" in reason2


def test_capability_auto_picks_supporting_adapter(config, store):
    recorder = Recorder()
    snowluma = SnowLumaAdapter(http_client=httpx.AsyncClient(
        base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(recorder.handler)
    ))
    bot = make_bot(
        config,
        store,
        adapters=[ConsoleAdapter(), snowluma],
        capabilities={"emoji_like": True},
    )

    async def scenario():
        result = await bot.call_capability(None, "message.emoji_like", message_id=7, emoji_id=8)
        await snowluma._client.aclose()
        return result

    ok, _ = asyncio.run(scenario())
    assert ok is True
    assert recorder.calls[0][0] == "/set_msg_emoji_like"


def test_adapter_direct_capability_not_supported():
    adapter = ConsoleAdapter()

    async def scenario():
        try:
            await adapter.call_capability("whatever")
        except CapabilityNotSupported as exc:
            return exc
        return None

    exc = asyncio.run(scenario())
    assert exc is not None
    assert exc.capability == "whatever"


# ----------------------------------------------------------------------
# 配置：能力开关
# ----------------------------------------------------------------------


def test_config_adapter_capabilities_parsing():
    from rubenbot.config import Config

    cfg = Config.from_dict(
        {
            "adapters": {
                "snowluma": {
                    "ws_url": "ws://x",
                    "capabilities": {"emoji_like": True, "qzone": False},
                }
            }
        }
    )
    assert cfg.adapter_capabilities("snowluma") == {"emoji_like": True, "qzone": False}
    assert cfg.adapter_capability_enabled("snowluma", "emoji_like") is True
    assert cfg.adapter_capability_enabled("snowluma", "qzone", default=True) is False


def test_config_capability_defaults_and_non_bool():
    from rubenbot.config import Config

    cfg = Config.from_dict({"adapters": {"snowluma": {"capabilities": {"qzone": "false"}}}})
    # 未写的开关用调用方默认值
    assert cfg.adapter_capability_enabled("snowluma", "emoji_like", default=True) is True
    # 非布尔值一律视为关闭，避免 "false" 被当成真
    assert cfg.adapter_capability_enabled("snowluma", "qzone", default=True) is False
    # 没有 capabilities 段时只看默认值
    assert cfg.adapter_capability_enabled("console", "x", default=False) is False
    assert cfg.adapter_capabilities("console") == {}


# ----------------------------------------------------------------------
# 核心：带消息段发送
# ----------------------------------------------------------------------


class SegRecordingAdapter(ConsoleAdapter):
    name = "segrec"

    def __init__(self):
        import io

        super().__init__(stdout=io.StringIO())
        self.batches: list[list[Segment]] = []

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.batches.append(list(segments))
        self.sent.append((chat_id, chat_type, list(segments)))


def test_send_reply_with_segments(config, store):
    adapter = SegRecordingAdapter()
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(adapter)
    event = MessageEvent(
        platform="segrec",
        chat_type="group",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text="看",
    )
    segments = [Segment.text("看"), Segment.image("http://x/a.png")]

    asyncio.run(bot.send_reply(event, segments))
    # 段列表整体只发一次，不按句拆分
    assert len(adapter.batches) == 1
    assert adapter.batches[0] == segments
    # 流水里存文本投影
    stored = store.recent_messages("g1", platform="segrec")
    assert stored[-1].text == "看[图片]"


def test_text_reply_still_split(config, store):
    config.chat.split_part_chars = 10
    config.chat.split_max_parts = 3
    adapter = SegRecordingAdapter()
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(adapter)
    event = MessageEvent(
        platform="segrec",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text="在吗",
    )
    asyncio.run(bot.send_reply(event, "第一句话。第二句话。第三句话。"))
    assert len(adapter.batches) >= 2
    assert all(len(batch) == 1 and batch[0]["type"] == "text" for batch in adapter.batches)


# ----------------------------------------------------------------------
# 能力：发表情（send_sticker）
# ----------------------------------------------------------------------


class StickerRecorder:
    """假适配器：声明 send_sticker，可指定返回值 / 抛错。"""

    def __init__(self, *, result=None, raises=None):
        self.name = "sticker_rec"
        self.capabilities = frozenset({"send_sticker"})
        self.capability_config_keys = {"send_sticker": "sticker"}
        self.capability_defaults = {}
        self.result = result if result is not None else {"sent": True}
        self.raises = raises
        self.calls: list[dict] = []

    async def call_capability(self, name, **kwargs):
        self.calls.append({"name": name, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result

    def set_platform_event_callback(self, callback) -> None:
        """网关会往适配器注入事件回调；这个假适配器不解析平台事件，忽略。"""
        pass


class NoStickerAdapter:
    name = "plain"
    capabilities = frozenset()
    capability_config_keys = {}
    capability_defaults = {}

    async def call_capability(self, name, **kwargs):  # pragma: no cover - 不会被调用
        raise AssertionError("不支持的能力不应被调用")

    def set_platform_event_callback(self, callback) -> None:  # pragma: no cover - 无事件
        pass


def test_console_send_sticker_writes_placeholder():
    import io

    out = io.StringIO()
    adapter = ConsoleAdapter(stdout=out)

    async def scenario():
        return await adapter.call_capability(
            "send_sticker", chat_id="摸鱼群", sticker={"id": "st_1"}
        )

    result = asyncio.run(scenario())
    assert result == {"sent": True}
    assert "Ruben: [表情]" in out.getvalue()
    assert adapter.sent[-1][0] == "摸鱼群"
    assert adapter.sent[-1][2][0]["type"] == "sticker"


def test_console_unrelated_capability_still_unsupported():
    adapter = ConsoleAdapter()

    async def scenario():
        with pytest.raises(CapabilityNotSupported):
            await adapter.call_capability("message.emoji_like", message_id=1, emoji_id=2)

    asyncio.run(scenario())


def test_send_sticker_through_core_reaches_adapter(config, store):
    recorder = StickerRecorder()
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(recorder)

    async def scenario():
        return await bot.call_capability(
            "sticker_rec",
            "send_sticker",
            sticker={"id": "st_1", "native_refs": {}},
            chat_id="g1",
        )

    ok, reason = asyncio.run(scenario())
    assert ok is True
    assert recorder.calls[0]["sticker"]["id"] == "st_1"
    assert reason == "ok"


def test_send_sticker_unsupported_platform_degrades(config, store):
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(NoStickerAdapter())

    async def scenario():
        return await bot.call_capability(
            "plain", "send_sticker", sticker={"id": "st_1"}, chat_id="g1"
        )

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "不支持" in reason


def test_send_sticker_capability_disabled(config, store):
    recorder = StickerRecorder()
    config.adapters["sticker_rec"] = {"capabilities": {"sticker": False}}
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(recorder)

    async def scenario():
        return await bot.call_capability(
            "sticker_rec", "send_sticker", sticker={"id": "st_1"}, chat_id="g1"
        )

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "关闭" in reason
    assert recorder.calls == []


def test_send_sticker_backfills_native_ref(config, store, tmp_path):
    """Telegram 返回 file_id 时，核心回填表情库，下次发送可直达。"""
    from rubenbot.stickers import StickerStore

    stickers = StickerStore(tmp_path / "stickers")
    registered = stickers.add(b"\x89PNG\r\n\x1a\nsticker-bytes", tags=["猫"])
    recorder = StickerRecorder(result={"message_id": 1, "file_id": "FILE_9"})
    bot = RubenBot(
        config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"), stickers=stickers
    )
    bot.register_adapter(recorder)

    async def scenario():
        return await bot.call_capability(
            recorder.name,
            "send_sticker",
            sticker=stickers.reference(registered),
            chat_id="g1",
        )

    ok, _ = asyncio.run(scenario())
    assert ok is True
    refilled = stickers.get(registered.id)
    assert refilled is not None
    assert refilled.native_refs["sticker_rec"] == {"file_id": "FILE_9"}


def test_send_sticker_without_file_id_does_not_backfill(config, store, tmp_path):
    from rubenbot.stickers import StickerStore

    stickers = StickerStore(tmp_path / "stickers")
    registered = stickers.add(b"\x89PNG\r\n\x1a\nsticker-bytes")
    recorder = StickerRecorder(result={"sent": True})
    bot = RubenBot(
        config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"), stickers=stickers
    )
    bot.register_adapter(recorder)

    asyncio.run(
        bot.call_capability(
            recorder.name, "send_sticker", sticker=stickers.reference(registered), chat_id="g1"
        )
    )
    assert stickers.get(registered.id).native_refs == {}


def test_send_sticker_exception_is_contained(config, store):
    recorder = StickerRecorder(raises=RuntimeError("平台炸了"))
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(recorder)

    async def scenario():
        return await bot.call_capability(
            recorder.name, "send_sticker", sticker={"id": "st_1"}, chat_id="g1"
        )

    ok, reason = asyncio.run(scenario())
    assert ok is False
    assert "平台炸了" in reason
