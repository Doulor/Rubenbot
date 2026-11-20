"""SnowLuma（OneBot v11）适配器：事件解析、发送、重连。"""

import asyncio
import json

import httpx
import pytest

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.adapters.snowluma import (
    SnowLumaAdapter,
    onebot_to_segments,
    parse_onebot_event,
    segment_to_onebot,
)


def group_payload(**overrides):
    payload = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 10000,
        "group_id": 55555,
        "user_id": 12345,
        "message_id": 9001,
        "sender": {"user_id": 12345, "nickname": "阿花", "card": "花姐"},
        "message": [{"type": "text", "data": {"text": "大家晚上好"}}],
        "raw_message": "大家晚上好",
    }
    payload.update(overrides)
    return payload


def test_parse_group_message():
    event = parse_onebot_event(group_payload())
    assert isinstance(event, MessageEvent)
    assert event.platform == "snowluma"
    assert event.chat_type == "group"
    assert event.chat_id == "55555"
    assert event.sender_id == "12345"
    assert event.sender_name == "花姐"  # 群名片优先
    assert event.text == "大家晚上好"
    assert event.mentions_bot is False


def test_parse_private_message():
    event = parse_onebot_event(
        group_payload(message_type="private", group_id=None, sender={"nickname": "阿花"})
    )
    assert event.chat_type == "private"
    assert event.chat_id == "12345"
    assert event.sender_name == "阿花"


def test_parse_ignores_non_message_and_self():
    assert parse_onebot_event({"post_type": "notice"}) is None
    assert parse_onebot_event({"post_type": "message", "message_type": "discuss"}) is None
    assert parse_onebot_event(group_payload(user_id=10000)) is None  # bot 自己


def test_parse_mentions_and_reply():
    message = [
        {"type": "at", "data": {"qq": "10000"}},
        {"type": "text", "data": {"text": " 在吗"}},
    ]
    event = parse_onebot_event(group_payload(message=message, raw_message="[CQ:at,qq=10000] 在吗"))
    assert event.mentions_bot is True
    assert "在吗" in event.text

    reply_message = [
        {"type": "reply", "data": {"id": "777"}},
        {"type": "text", "data": {"text": "回你"}},
    ]
    event = parse_onebot_event(group_payload(message=reply_message))
    assert event.reply_to_id == "777"
    # 事件自身的消息 id 与被引用的目标 id 分开带出去
    assert event.platform_msg_id == "9001"
    assert event.raw["_reply_message_id"] == "9001"


def test_parse_string_message_with_cq_codes():
    event = parse_onebot_event(
        group_payload(
            message="[CQ:at,qq=10000] 你好[CQ:face,id=1]",
            raw_message="[CQ:at,qq=10000] 你好[CQ:face,id=1]",
        )
    )
    assert event.mentions_bot is True
    assert event.text == "@10000 你好"


def test_parse_string_message_with_cq_reply():
    """字符串形式（CQ 码）里的 reply 段也要解析出被引用 id。"""
    event = parse_onebot_event(
        group_payload(message="[CQ:reply,id=777]回你", raw_message="[CQ:reply,id=777]回你")
    )
    assert event.reply_to_id == "777"
    assert event.platform_msg_id == "9001"
    assert "777" not in event.text  # reply 码不进入正文


def test_onebot_to_segments_variants():
    assert onebot_to_segments("纯文本") == "纯文本"
    assert onebot_to_segments(None) == ""
    assert onebot_to_segments([{"type": "image", "data": {"file": "a.jpg"}}]) == "[图片]"


def test_segment_to_onebot():
    assert segment_to_onebot(Segment.text("hi")) == {"type": "text", "data": {"text": "hi"}}
    assert segment_to_onebot(Segment.image("http://x/a.png")) == {
        "type": "image",
        "data": {"file": "http://x/a.png"},
    }


def make_adapter(handler) -> SnowLumaAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="http://127.0.0.1:3000", transport=transport)
    return SnowLumaAdapter(http_client=client)


def test_send_group_and_private():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 42}})

    adapter = make_adapter(handler)

    async def scenario():
        group_id = await adapter.send("55555", [Segment.text("你好")], chat_type="group")
        private_id = await adapter.send("12345", [Segment.text("私聊")], chat_type="private")
        await adapter._client.aclose()
        return group_id, private_id

    group_id, private_id = asyncio.run(scenario())
    assert group_id == "42"  # 出站回执：OneBot 响应里的 message_id
    assert private_id == "42"
    assert calls[0][0] == "/send_group_msg"
    assert calls[0][1]["group_id"] == 55555  # 数字 id 转 int
    assert calls[0][1]["message"] == [{"type": "text", "data": {"text": "你好"}}]
    assert calls[1][0] == "/send_private_msg"
    assert calls[1][1]["user_id"] == 12345
    # 记录自己发的消息 id，用于识别“回复 bot”
    assert "42" in adapter._bot_message_ids


def test_send_without_message_id_returns_none():
    def handler(request):
        return httpx.Response(200, json={"status": "ok", "data": {}})

    adapter = make_adapter(handler)

    async def scenario():
        result = await adapter.send("1", [Segment.text("x")])
        await adapter._client.aclose()
        return result

    assert asyncio.run(scenario()) is None


def test_send_failure_raises():
    def handler(request):
        return httpx.Response(200, json={"status": "failed", "retcode": 100})

    adapter = make_adapter(handler)

    async def scenario():
        with pytest.raises(RuntimeError):
            await adapter.send("1", [Segment.text("x")])
        await adapter._client.aclose()

    asyncio.run(scenario())


def test_handle_raw_marks_reply_to_bot():
    """回复 bot 自己发出的消息时，自动置位 mentions_bot。"""
    adapter = SnowLumaAdapter()
    adapter._bot_message_ids.add("424242")
    received = []

    async def on_event(event):
        received.append(event)

    async def scenario():
        adapter._on_event = on_event
        message = [
            {"type": "reply", "data": {"id": "424242"}},
            {"type": "text", "data": {"text": "刚才说什么"}},
        ]
        await adapter._handle_raw(json.dumps(group_payload(message=message)))

    asyncio.run(scenario())
    assert received and received[0].mentions_bot is True


def test_handle_raw_ignores_self_message_but_tracks_id():
    adapter = SnowLumaAdapter()
    received = []

    async def on_event(event):
        received.append(event)

    async def scenario():
        adapter._on_event = on_event
        await adapter._handle_raw(json.dumps(group_payload(user_id=10000, message_id=123)))

    asyncio.run(scenario())
    assert received == []
    assert "123" in adapter._bot_message_ids


def test_start_reconnects_after_disconnect(monkeypatch):
    """ws 连接失败时按退避重连，stop 后退出。"""
    import rubenbot.adapters.snowluma as module

    attempts = []

    class FakeConnect:
        def __init__(self, *a, **k):
            attempts.append(1)

        async def __aenter__(self):
            raise OSError("连接被拒绝")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(module, "ws_connect", FakeConnect)
    monkeypatch.setattr(module, "_RECONNECT_MIN", 0.01)

    adapter = SnowLumaAdapter(ws_url="ws://fake")

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.sleep(0.08)
        await adapter.stop()
        try:
            await asyncio.wait_for(task, timeout=1)
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert len(attempts) >= 2
    assert adapter.reconnect_count >= 1


# ----------------------------------------------------------------------
# 能力：发表情（send_sticker）
# ----------------------------------------------------------------------


def test_sticker_segment_prefers_native_mface():
    """有本平台原生 mface 引用时直接用，不读图片文件。"""
    from rubenbot.adapters.snowluma import sticker_segment_for_onebot

    sticker = {
        "file": "/tmp/never-read.png",
        "native_refs": {
            "snowluma": {"kind": "mface", "data": {"emoji_id": "7", "summary": "[表情]"}}
        },
    }
    assert sticker_segment_for_onebot(sticker) == {
        "type": "mface",
        "data": {"emoji_id": "7", "summary": "[表情]"},
    }


def test_sticker_segment_falls_back_to_image_file(tmp_path):
    from rubenbot.adapters.snowluma import sticker_segment_for_onebot

    path = tmp_path / "cat.png"
    path.write_bytes(b"png")
    assert sticker_segment_for_onebot({"file": str(path)}) == {
        "type": "image",
        "data": {"file": str(path)},
    }


def test_sticker_segment_without_source_is_none():
    from rubenbot.adapters.snowluma import sticker_segment_for_onebot

    assert sticker_segment_for_onebot({"id": "st_x"}) is None
    assert sticker_segment_for_onebot("不是引用") is None


def test_sticker_segment_accepts_bare_native_data():
    """原生引用也接受直接给段 data 的写法（没有 kind，按 mface 处理）。"""
    from rubenbot.adapters.snowluma import sticker_segment_for_onebot

    assert sticker_segment_for_onebot(
        {"native_refs": {"snowluma": {"emoji_id": "3"}}}
    ) == {"type": "mface", "data": {"emoji_id": "3"}}


def test_call_capability_send_sticker_uses_mface():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 5}})

    adapter = make_adapter(handler)

    async def scenario():
        result = await adapter.call_capability(
            "send_sticker",
            chat_id="55555",
            sticker={
                "native_refs": {"snowluma": {"kind": "mface", "data": {"emoji_id": "7"}}}
            },
        )
        await adapter._client.aclose()
        return result

    result = asyncio.run(scenario())
    assert calls == [
        (
            "/send_group_msg",
            {
                "group_id": 55555,
                "message": [{"type": "mface", "data": {"emoji_id": "7"}}],
            },
        )
    ]
    assert result == {"message_id": 5}


def test_call_capability_send_sticker_private_and_image_fallback(tmp_path):
    path = tmp_path / "cat.png"
    path.write_bytes(b"png")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "data": {}})

    adapter = make_adapter(handler)

    async def scenario():
        await adapter.call_capability(
            "send_sticker",
            chat_id="12345",
            chat_type="private",
            sticker={"file": str(path)},
        )
        await adapter._client.aclose()

    asyncio.run(scenario())
    assert calls == [
        (
            "/send_private_msg",
            {"user_id": 12345, "message": [{"type": "image", "data": {"file": str(path)}}]},
        )
    ]


def test_call_capability_send_sticker_without_source_raises():
    from rubenbot.adapters.base import CapabilityError

    adapter = make_adapter(lambda request: httpx.Response(200, json={"status": "ok"}))

    async def scenario():
        with pytest.raises(CapabilityError, match="缺少可用的表情引用或图片文件"):
            await adapter.call_capability("send_sticker", chat_id="1", sticker={"id": "st_x"})
        with pytest.raises(CapabilityError, match="需要 chat_id"):
            await adapter.call_capability("send_sticker", sticker={"file": "x"})
        await adapter._client.aclose()

    asyncio.run(scenario())


def test_call_capability_send_sticker_failure_wrapped():
    from rubenbot.adapters.base import CapabilityError

    adapter = make_adapter(
        lambda request: httpx.Response(200, json={"status": "failed", "retcode": 100})
    )

    async def scenario():
        with pytest.raises(CapabilityError, match="send_sticker 失败"):
            await adapter.call_capability(
                "send_sticker",
                chat_id="1",
                sticker={"native_refs": {"snowluma": {"kind": "mface", "data": {"emoji_id": "1"}}}},
            )
        await adapter._client.aclose()

    asyncio.run(scenario())


def test_unsupported_capability_raises():
    from rubenbot.adapters.base import CapabilityNotSupported

    adapter = make_adapter(lambda request: httpx.Response(200, json={"status": "ok"}))

    async def scenario():
        with pytest.raises(CapabilityNotSupported):
            await adapter.call_capability("message.teleport", target="月球")
        await adapter._client.aclose()

    asyncio.run(scenario())
