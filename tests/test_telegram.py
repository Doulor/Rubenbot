"""Telegram 适配器：getUpdates 解析、发送拼装、offset 推进、异常重试。"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from rubenbot.adapters.base import CapabilityError, Segment
from rubenbot.adapters.telegram import (
    TelegramAdapter,
    _entity_text,
)
from rubenbot.config import Config, TelegramConfig

TOKEN = "123456:TEST-TOKEN"
BOT_ID = 999000
BOT_USERNAME = "ruben_test_bot"


# ----------------------------------------------------------------------
# 假传输层
# ----------------------------------------------------------------------


class FakeTelegram:
    """记录请求并按方法名返回预设响应的假 Bot API。"""

    def __init__(self, *, bot_id: int = BOT_ID, username: str = BOT_USERNAME):
        self.bot_id = bot_id
        self.username = username
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[httpx.Request] = []
        self.updates_queue: list[list[dict]] = []
        self.update_error: Exception | None = None
        self.get_file_result: dict | None = {"file_path": "photos/file_1.jpg"}
        self.file_bytes = b"\xff\xd8\xff\xe0fake-jpeg"
        self.hold_poll: asyncio.Event | None = None
        self.poll_started = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        # 文件下载：/file/bot<token>/<file_path>
        if path.startswith(f"/file/bot{TOKEN}/"):
            return httpx.Response(200, content=self.file_bytes)
        method = path.rsplit("/", 1)[-1]
        payload = _request_payload(request)
        self.calls.append((method, payload))
        if method == "getMe":
            return _ok(
                {
                    "id": self.bot_id,
                    "is_bot": True,
                    "username": self.username,
                    "first_name": "Ruben",
                }
            )
        if method == "getUpdates":
            if self.update_error is not None:
                raise self.update_error
            self.poll_started.set()
        if method == "getFile":
            return _ok(self.get_file_result)
        if method in ("sendMessage", "sendPhoto"):
            return _ok({"message_id": 100 + len(self.calls)})
        return _ok(True)

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def payload_for(self, method: str) -> dict:
        for name, payload in self.calls:
            if name == method:
                return payload
        raise AssertionError(f"没有调用 {method}：{self.methods()}")


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _request_payload(request: httpx.Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return json.loads(request.content or b"{}")
    if content_type.startswith("multipart/form-data"):
        return _parse_multipart(request)
    return dict(httpx.QueryParams(request.content.decode("utf-8", "replace")))


def _parse_multipart(request: httpx.Request) -> dict:
    """极简 multipart 解析：只取文本字段，文件记为 ``<filename>:<bytes>``。"""
    boundary = request.headers["content-type"].split("boundary=", 1)[1].strip('"')
    body = request.content
    fields: dict[str, object] = {}
    for part in body.split(b"--" + boundary.encode()):
        if b"\r\n\r\n" not in part:
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        if value.endswith(b"\r\n"):
            value = value[:-2]
        headers = head.decode("utf-8", "replace")
        if 'name="' not in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        if "filename=" in headers:
            filename = headers.split('filename="', 1)[1].split('"', 1)[0]
            fields[name] = (filename, value)
        else:
            fields[name] = value.decode("utf-8")
    return fields


def test_send_file_uses_document_bytes_and_current_chat():
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def send():
        try:
            await adapter.send_file('123', 'report.txt', b'report', chat_type='private')
        finally:
            await close(adapter)

    asyncio.run(send())
    assert fake.payload_for('sendDocument') == {'chat_id': '123', 'document': ('report.txt', b'report')}


def make_adapter(fake: FakeTelegram, **kwargs) -> TelegramAdapter:
    client = httpx.AsyncClient(
        base_url="https://api.telegram.org", transport=httpx.MockTransport(fake.handler)
    )
    kwargs.setdefault("bot_token", TOKEN)
    kwargs.setdefault("offset_file", None)
    kwargs.setdefault("poll_timeout", 1.0)
    return TelegramAdapter(http_client=client, **kwargs)


async def close(adapter: TelegramAdapter) -> None:
    if adapter._client is not None:
        await adapter._client.aclose()


# ----------------------------------------------------------------------
# 事件构造
# ----------------------------------------------------------------------


def make_chat(chat_type="private", chat_id=111, **overrides):
    chat = {"id": chat_id, "type": chat_type}
    chat.update(overrides.pop("chat", {}))
    return chat


def make_message(*, text=None, chat_type="private", chat_id=111, sender_id=555, **overrides):
    message = {
        "message_id": 42,
        "date": 1700000000,
        "chat": make_chat(chat_type, chat_id),
        "from": {"id": sender_id, "is_bot": False, "first_name": "阿花", "username": "ahua"},
    }
    if text is not None:
        message["text"] = text
    message.update(overrides)
    return message


def make_update(message, update_id=10):
    return {"update_id": update_id, "message": message}


# ----------------------------------------------------------------------
# 入站解析
# ----------------------------------------------------------------------


def test_parse_private_text():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        adapter.bot_username = BOT_USERNAME
        event = await adapter.parse_update(make_update(make_message(text="在吗")))
        await close(adapter)
        return event

    event = asyncio.run(scenario())
    assert event.platform == "telegram"
    assert event.chat_type == "private"
    assert event.chat_id == "111"
    assert event.sender_id == "555"
    assert event.sender_name == "阿花"
    assert event.text == "在吗"
    assert event.mentions_bot is False
    assert event.reply_to_id is None
    assert event.segments == [{"type": "text", "data": {"text": "在吗"}}]


def test_parse_group_and_supergroup_are_group():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        group = await adapter.parse_update(
            make_update(make_message(text="大家好", chat_type="group", chat_id=-100))
        )
        supergroup = await adapter.parse_update(
            make_update(make_message(text="大家好", chat_type="supergroup", chat_id=-100123))
        )
        await close(adapter)
        return group, supergroup

    group, supergroup = asyncio.run(scenario())
    assert group.chat_type == "group" and group.chat_id == "-100"
    assert supergroup.chat_type == "group" and supergroup.chat_id == "-100123"
    assert supergroup.is_group


def test_parse_native_sticker_as_unified_segment():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        result = await adapter.parse_update(
            make_update(make_message(sticker={"file_id": "STICKER_1", "emoji": "😀"}))
        )
        await close(adapter)
        return result

    event = asyncio.run(scenario())
    assert event is not None
    assert event.segments == [
        {
            "type": "sticker",
            "data": {"native_ref": {"kind": "sticker", "file_id": "STICKER_1"}, "emoji": "😀"},
        }
    ]


def test_parse_sticker_does_not_download_or_transform_native_ref():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        result = await adapter.parse_update(
            make_update(make_message(sticker={"file_id": "STICKER_2", "width": 64, "height": 64}))
        )
        await close(adapter)
        return result

    event = asyncio.run(scenario())
    assert event is not None
    assert event.text == "[表情]"
    assert event.segments[0].data["native_ref"] == {
        "kind": "sticker",
        "file_id": "STICKER_2",
    }


@pytest.mark.parametrize("limit", [4, 3])
def test_resolve_static_sticker_streams_with_limit_and_preserves_native_send(monkeypatch, caplog, limit):
    monkeypatch.setattr("rubenbot.adapters.telegram.MAX_PHOTO_BYTES", limit)
    caplog.set_level("INFO")
    seen = []
    calls = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"ab", b"cd"):
                seen.append(chunk)
                yield chunk
            seen.append("end")

        async def aclose(self):
            seen.append("closed")

    def handler(request):
        if request.method == "GET":
            assert request.url.path == f"/file/bot{TOKEN}/stickers/img.webp"
            return httpx.Response(200, stream=Chunks())
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, _request_payload(request)))
        if method == "getFile":
            return _ok({"file_path": "stickers/img.webp"})
        return _ok({"message_id": 7})

    async def scenario():
        async with httpx.AsyncClient(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)) as client:
            adapter = TelegramAdapter(TOKEN, http_client=client)
            event = await adapter.parse_update(make_update(make_message(sticker={"file_id": "STATIC_1"})))
            assert not calls
            native = event.segments[0].data["native_ref"]
            source = await adapter.resolve_image_source(event.segments[0])
            await adapter.call_capability("send_sticker", chat_id="123", sticker={"native_refs": {"telegram": native}})
            return source

    source = asyncio.run(scenario())
    assert calls == [("getFile", {"file_id": "STATIC_1"}), ("sendSticker", {"chat_id": 123, "sticker": "STATIC_1"})]
    if limit == 4:
        assert source == {"base64": "YWJjZA==", "mime_type": "image/webp"}
        assert seen == [b"ab", b"cd", "end", "closed"]
    else:
        assert source is None
        assert seen == [b"ab", b"cd", "closed"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("sticker", [
    {"file_id": "ANIM_1", "is_animated": True}, {"file_id": "VID_1", "is_video": True},
    {"file_id": "https://untrusted.example/x"}, {"file_id": "../secret"},
])
def test_resolve_only_static_platform_sticker_ids(sticker):
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def scenario():
        event = await adapter.parse_update(make_update(make_message(sticker=sticker)))
        assert await adapter.resolve_image_source(event.segments[0]) is None
        await close(adapter)

    asyncio.run(scenario())
    assert not fake.requests


def test_parse_channel_and_unhandled_kinds_are_ignored():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        channel = await adapter.parse_update(
            make_update(make_message(text="频道消息", chat_type="channel", chat_id=-200))
        )
        sticker = await adapter.parse_update(
            make_update(make_message(sticker={"file_id": "x", "emoji": "😀"}))
        )
        edited = await adapter.parse_update(
            {"update_id": 3, "edited_message": make_message(text="改过了")}
        )
        from_bot = await adapter.parse_update(
            make_update(
                make_message(
                    text="我是机器人",
                    sender_id=BOT_ID,
                    **{"from": {"id": BOT_ID, "is_bot": True, "first_name": "Ruben"}},
                )
            )
        )
        await close(adapter)
        return channel, sticker, edited, from_bot

    channel, sticker, edited, from_bot = asyncio.run(scenario())
    assert channel is None and edited is None and from_bot is None
    assert sticker is not None and sticker.segments[0].type == "sticker"


def test_parse_mentions_via_entity():
    """entities 里的 @bot 用户名即视为叫到 bot；按 UTF-16 偏移切实体文本。"""
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        adapter.bot_username = BOT_USERNAME
        text = f"😀 @{BOT_USERNAME} 在吗"
        offset = 3  # emoji 占 2 个 UTF-16 码元，加一个空格
        mentioned = await adapter.parse_update(
            make_update(
                make_message(
                    text=text,
                    entities=[{"type": "mention", "offset": offset, "length": len(BOT_USERNAME) + 1}],
                    chat_type="group",
                )
            )
        )
        other = await adapter.parse_update(
            make_update(
                make_message(
                    text="@someone_else hi",
                    entities=[{"type": "mention", "offset": 0, "length": 13}],
                    chat_type="group",
                )
            )
        )
        await close(adapter)
        return mentioned, other

    mentioned, other = asyncio.run(scenario())
    assert mentioned.mentions_bot is True
    assert mentioned.text == f"😀 @{BOT_USERNAME} 在吗"
    assert other.mentions_bot is False
    assert other.mentions == {"@someone_else": "@someone_else"}
    assert set(mentioned.mentions) == {"__bot__"}


def test_parse_text_mention_entity_points_to_bot():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        adapter.bot_username = None
        event = await adapter.parse_update(
            make_update(
                make_message(
                    text="Ruben 你好",
                    entities=[
                        {
                            "type": "text_mention",
                            "offset": 0,
                            "length": 5,
                            "user": {"id": BOT_ID},
                        }
                    ],
                    chat_type="group",
                )
            )
        )
        await close(adapter)
        return event

    event = asyncio.run(scenario())
    assert event.mentions_bot is True


def test_parse_reply_to_bot_sets_mentions_and_reply_id():
    adapter = make_adapter(FakeTelegram())
    bot_message = {
        "message_id": 77,
        "from": {"id": BOT_ID, "is_bot": True, "first_name": "Ruben"},
        "chat": make_chat("private", 111),
        "date": 1700000000,
        "text": "我先说",
    }

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        adapter.bot_username = BOT_USERNAME
        event = await adapter.parse_update(
            make_update(make_message(text="刚才说什么", reply_to_message=bot_message))
        )
        await close(adapter)
        return event

    event = asyncio.run(scenario())
    assert event.reply_to_id == "77"
    assert event.mentions_bot is True
    assert event.raw["reply_to_bot"] is True
    assert event.reply_sender_id == "__bot__"
    assert event.mentions == {}  # 引用不是 @
    assert event.platform_msg_id is not None


def test_parse_photo_downloads_largest_size():
    """photo 取最大尺寸，getFile + 下载后转成带 base64 的 image 段。"""
    fake = FakeTelegram()
    adapter = make_adapter(fake)
    photo = [
        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90, "file_size": 1000},
        {"file_id": "big", "file_unique_id": "b", "width": 1280, "height": 720, "file_size": 9000},
        {"file_id": "mid", "file_unique_id": "m", "width": 320, "height": 180, "file_size": 3000},
    ]

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        adapter.bot_username = BOT_USERNAME
        event = await adapter.parse_update(
            make_update(make_message(photo=photo, caption="看这个", chat_type="group"))
        )
        await close(adapter)
        return event

    event = asyncio.run(scenario())
    image = event.segments[0]
    assert image.type == "image"
    assert image.data["file_id"] == "big"
    assert image.data["base64"] == base64.b64encode(fake.file_bytes).decode()
    assert image.data["mime_type"] == "image/jpeg"
    # caption 作为文本段，纯文本投影与 SnowLuma 一致
    assert event.segments[1] == {"type": "text", "data": {"text": "看这个"}}
    assert event.text == "[图片]看这个"
    assert fake.payload_for("getFile") == {"file_id": "big"}
    assert any("/file/bot" in str(req.url) for req in fake.requests)


def test_photo_download_failure_keeps_placeholder_segment():
    fake = FakeTelegram()
    fake.get_file_result = None
    adapter = make_adapter(fake)

    async def scenario():
        adapter.bot_id = str(BOT_ID)
        event = await adapter.parse_update(
            make_update(
                make_message(
                    photo=[{"file_id": "only", "width": 640, "height": 480}],
                    chat_type="group",
                )
            )
        )
        await close(adapter)
        return event

    event = asyncio.run(scenario())
    assert event.text == "[图片]"
    assert event.segments[0].data == {"file_id": "only", "width": 640, "height": 480}


def test_entity_text_uses_utf16_offsets():
    text = "😀@bot"
    # emoji 占 2 个 UTF-16 码元，@bot 从 offset=2 开始
    assert _entity_text(text, 2, 4) == "@bot"
    assert _entity_text(text, 0, 2) == "😀"
    assert _entity_text(text, 10, 2) == ""
    assert _entity_text(text, None, 2) == ""


# ----------------------------------------------------------------------
# 出站发送
# ----------------------------------------------------------------------


def test_send_text_and_reply():
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def scenario():
        await adapter.send(
            "111",
            [Segment.reply("77"), Segment.text("收到")],
            chat_type="private",
        )
        await close(adapter)

    asyncio.run(scenario())
    payload = fake.payload_for("sendMessage")
    assert payload == {"chat_id": 111, "text": "收到", "reply_to_message_id": 77}


def test_send_image_base64_uploads_multipart():
    fake = FakeTelegram()
    adapter = make_adapter(fake)
    raw = b"fake-png-bytes"

    async def scenario():
        await adapter.send(
            "-100123",
            [
                Segment.text("发张图"),
                Segment("image", {"base64": base64.b64encode(raw).decode(), "mime_type": "image/png"}),
            ],
            chat_type="group",
        )
        await close(adapter)

    asyncio.run(scenario())
    # 图片前的文本并入图片说明，只发一条 sendPhoto
    assert "sendMessage" not in fake.methods()
    photo_call = [p for m, p in fake.calls if m == "sendPhoto"][0]
    assert photo_call["chat_id"] == "-100123"
    assert photo_call["caption"] == "发张图"
    filename, content = photo_call["photo"]
    assert filename.endswith(".png")
    assert content == raw


def test_send_image_url_and_caption():
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def scenario():
        await adapter.send(
            "111",
            [
                Segment.reply("9"),
                Segment.text("看这个"),
                Segment.image("https://example.com/a.jpg"),
            ],
            chat_type="private",
        )
        await close(adapter)

    asyncio.run(scenario())
    payload = fake.payload_for("sendPhoto")
    assert payload["photo"] == "https://example.com/a.jpg"
    assert payload["caption"] == "看这个"
    assert payload["chat_id"] == 111
    assert payload["reply_to_message_id"] == 9
    # 图片说明承载了文本，不再重复发 sendMessage
    assert "sendMessage" not in fake.methods()


def test_send_image_data_uri_base64():
    fake = FakeTelegram()
    adapter = make_adapter(fake)
    raw = b"data-uri-bytes"

    async def scenario():
        await adapter.send(
            "111",
            [Segment("image", {"base64": "data:image/jpeg;base64," + base64.b64encode(raw).decode()})],
        )
        await close(adapter)

    asyncio.run(scenario())
    photo_call = [p for m, p in fake.calls if m == "sendPhoto"][0]
    filename, content = photo_call["photo"]
    assert filename.endswith(".jpg")
    assert content == raw


def test_send_unsupported_segments_degrade_to_placeholder():
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def scenario():
        await adapter.send("111", [Segment.face("1"), Segment.text("好")])
        await close(adapter)

    asyncio.run(scenario())
    assert fake.payload_for("sendMessage")["text"] == "[表情]好"


def test_send_image_without_source_degrades_to_text():
    fake = FakeTelegram()
    adapter = make_adapter(fake)

    async def scenario():
        await adapter.send("111", [Segment("image", {})])
        await close(adapter)

    asyncio.run(scenario())
    assert fake.payload_for("sendMessage")["text"] == "[图片]"
    assert "sendPhoto" not in fake.methods()


def test_send_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"})

    adapter = TelegramAdapter(
        bot_token=TOKEN,
        offset_file=None,
        http_client=httpx.AsyncClient(
            base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)
        ),
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="Forbidden"):
            await adapter.send("111", [Segment.text("x")])
        await close(adapter)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# offset 推进与持久化
# ----------------------------------------------------------------------


def test_handle_updates_advances_offset_and_saves(tmp_path):
    fake = FakeTelegram()
    offset_file = tmp_path / "offset.json"
    adapter = make_adapter(fake, offset_file=offset_file)
    received = []

    async def on_event(event):
        received.append(event.text)

    async def scenario():
        adapter._on_event = on_event
        adapter.bot_id = str(BOT_ID)
        await adapter._handle_updates(
            [
                make_update(make_message(text="一"), update_id=100),
                make_update(make_message(text="二"), update_id=101),
            ]
        )
        await close(adapter)

    asyncio.run(scenario())
    assert received == ["一", "二"]
    assert adapter._offset == 102
    assert json.loads(offset_file.read_text()) == {"offset": 102}


def test_offset_restored_from_file(tmp_path):
    offset_file = tmp_path / "offset.json"
    offset_file.write_text(json.dumps({"offset": 500}), encoding="utf-8")
    adapter = make_adapter(FakeTelegram(), offset_file=offset_file)
    adapter._load_offset()
    assert adapter._offset == 500


def test_offset_not_rewound_by_old_updates():
    adapter = make_adapter(FakeTelegram())
    adapter.bot_id = str(BOT_ID)
    adapter._offset = 100

    async def scenario():
        await adapter._handle_updates([make_update(make_message(text="旧"), update_id=50)])
        await close(adapter)

    asyncio.run(scenario())
    assert adapter._offset == 100


def test_bad_update_does_not_kill_batch(tmp_path):
    """单条 update 解析/回调异常只跳过这一条，后面的照常处理，offset 照常推进。"""
    fake = FakeTelegram()
    offset_file = tmp_path / "offset.json"
    adapter = make_adapter(fake, offset_file=offset_file)
    received = []

    async def on_event(event):
        received.append(event.text)
        if event.text == "会炸":
            raise RuntimeError("回调炸了")

    async def scenario():
        adapter._on_event = on_event
        adapter.bot_id = str(BOT_ID)
        await adapter._handle_updates(
            [
                make_update(make_message(text="会炸"), update_id=1),
                "这不是 dict",
                make_update(make_message(text="正常"), update_id=2),
            ]
        )
        await close(adapter)

    asyncio.run(scenario())
    assert received == ["会炸", "正常"]
    assert adapter._offset == 3
    assert json.loads(offset_file.read_text())["offset"] == 3


def test_allowed_chat_ids_filters():
    fake = FakeTelegram()
    adapter = make_adapter(fake, allowed_chat_ids=[111, -100123])
    received = []
    async def on_event(event):
        received.append(event.chat_id)

    async def scenario():
        adapter._on_event = on_event
        adapter.bot_id = str(BOT_ID)
        await adapter._handle_updates(
            [
                make_update(make_message(text="私聊", chat_id=111), update_id=1),
                make_update(make_message(text="别的群", chat_id=-100999, chat_type="group"), update_id=2),
                make_update(make_message(text="本群", chat_id=-100123, chat_type="supergroup"), update_id=3),
            ]
        )
        await close(adapter)

    asyncio.run(scenario())
    assert received == ["111", "-100123"]


# ----------------------------------------------------------------------
# 轮询循环：启动、重试退避、干净退出
# ----------------------------------------------------------------------


def test_start_polls_and_dispatches(monkeypatch):
    import rubenbot.adapters.telegram as module

    monkeypatch.setattr(module, "POLL_BACKOFF_MIN", 0.01)
    fake = FakeTelegram()
    fake.updates_queue = [[make_update(make_message(text="来了"), update_id=7)]]
    received = []

    async def handler_with_queue(request):
        payload = _request_payload(request)
        fake.calls.append((request.url.path.rsplit("/", 1)[-1], payload))
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return _ok({"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME, "first_name": "Ruben"})
        if method == "getUpdates" and fake.updates_queue:
            updates = fake.updates_queue.pop(0)
            received.append(payload)
            return _ok(updates)
        # 队列空了就挂住，等待 stop
        await asyncio.Event().wait()

    adapter = TelegramAdapter(
        bot_token=TOKEN,
        offset_file=None,
        poll_timeout=1.0,
        http_client=httpx.AsyncClient(
            base_url="https://api.telegram.org",
            transport=httpx.MockTransport(lambda request: handler_with_queue(request)),
        ),
    )

    async def on_event(event):
        received.append(event.text)

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        for _ in range(100):
            if len(received) >= 2 or task.done():
                break
            await asyncio.sleep(0.01)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert "来了" in received
    assert adapter.bot_id == str(BOT_ID)
    assert adapter.bot_username == BOT_USERNAME
    # 第一次 getUpdates 不带 offset（从待处理消息开始）
    assert "offset" not in received[0]


def test_start_retries_with_backoff(monkeypatch):
    """getUpdates 连续异常时退避重试，恢复后继续，stop 能干净退出。"""
    import rubenbot.adapters.telegram as module

    monkeypatch.setattr(module, "POLL_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(module, "POLL_BACKOFF_MAX", 0.02)
    attempts = []
    received = []

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return _ok({"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME, "first_name": "Ruben"})
        attempts.append(1)
        if len(attempts) <= 2:
            raise httpx.ConnectError("连不上", request=request)
        if len(attempts) == 3:
            return _ok([make_update(make_message(text="恢复了"), update_id=1)])
        await asyncio.Event().wait()

    adapter = TelegramAdapter(
        bot_token=TOKEN,
        offset_file=None,
        poll_timeout=1.0,
        http_client=httpx.AsyncClient(
            base_url="https://api.telegram.org",
            transport=httpx.MockTransport(lambda request: handler(request)),
        ),
    )

    async def on_event(event):
        received.append(event.text)

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        for _ in range(200):
            if received or task.done():
                break
            await asyncio.sleep(0.01)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert received == ["恢复了"]
    assert adapter.poll_error_count == 2
    # 恢复后 offset 已推进，下一次 getUpdates 会带上
    assert adapter._offset == 2


def test_stop_cancels_hanging_poll():
    """stop 时取消挂起的长轮询，start 立即返回。"""
    fake = FakeTelegram()

    async def hanging_handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return _ok({"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME, "first_name": "Ruben"})
        fake.poll_started.set()
        await asyncio.Event().wait()

    adapter = TelegramAdapter(
        bot_token=TOKEN,
        offset_file=None,
        poll_timeout=30.0,
        http_client=httpx.AsyncClient(
            base_url="https://api.telegram.org",
            transport=httpx.MockTransport(lambda request: hanging_handler(request)),
        ),
    )

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.wait_for(fake.poll_started.wait(), timeout=1)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=1)
        return task

    task = asyncio.run(scenario())
    assert task.done() and not task.cancelled()


def test_start_without_token_returns_cleanly(caplog):
    """缺 bot_token 属于配置错误：不抛异常（避免主循环无限重启），记日志后返回。"""
    adapter = TelegramAdapter(bot_token="")

    async def scenario():
        await adapter.start(lambda event: None)

    asyncio.run(scenario())
    assert any("bot_token" in record.message for record in caplog.records)


def test_capabilities_voice_and_unsupported_raise():
    """Telegram 声明语音回复与发表情；其他能力仍按「不支持」优雅失败。"""
    assert TelegramAdapter.capabilities == frozenset({"message.voice", "send_sticker"})

    async def scenario():
        adapter = make_adapter(FakeTelegram())
        result = await adapter.call_capability("message.emoji_like", message_id=1)
        await close(adapter)
        return result

    with pytest.raises(Exception, match="不支持能力"):
        asyncio.run(scenario())


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_telegram_config_from_options():
    cfg = Config.from_dict(
        {
            "adapters": {
                "telegram": {
                    "bot_token": "abc",
                    "poll_timeout": "15",
                    "allowed_chat_ids": [111, "-100123"],
                }
            }
        }
    )
    tg = cfg.telegram_config()
    assert tg == TelegramConfig(
        bot_token="abc",
        poll_timeout=15.0,
        allowed_chat_ids=["111", "-100123"],
        offset_file="data/telegram_offset.json",
    )
    assert cfg.enabled_adapters == ["telegram"]


def test_telegram_config_defaults_and_bad_values():
    assert Config().telegram_config() == TelegramConfig()
    cfg = Config.from_dict(
        {"adapters": {"telegram": {"poll_timeout": "不是数字", "allowed_chat_ids": -100}}}
    )
    tg = cfg.telegram_config()
    assert tg.poll_timeout == 30.0
    assert tg.allowed_chat_ids == ["-100"]


def test_telegram_token_expands_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUBENBOT_TELEGRAM_TOKEN", "123:env-token")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
adapters:
  telegram:
    bot_token: "${RUBENBOT_TELEGRAM_TOKEN}"
    poll_timeout: 20
""",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.telegram_config().bot_token == "123:env-token"
    assert cfg.telegram_config().poll_timeout == 20.0

# ----------------------------------------------------------------------
# 能力：发表情（send_sticker）
# ----------------------------------------------------------------------


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.telegram.org", transport=httpx.MockTransport(handler)
    )


def test_send_sticker_uses_native_file_id():
    """有 file_id 时直接走原生 sendSticker，不读本地文件。"""
    fake = FakeTelegram()

    async def scenario():
        adapter = make_adapter(fake)
        result = await adapter.call_capability(
            "send_sticker",
            chat_id=111,
            sticker={"id": "st_1", "native_refs": {"telegram": {"file_id": "FILE_1"}}},
        )
        await close(adapter)
        return result

    result = asyncio.run(scenario())
    assert fake.methods() == ["sendSticker"]
    assert fake.payload_for("sendSticker") == {"chat_id": 111, "sticker": "FILE_1"}
    assert result["file_id"] == "FILE_1"


def test_send_sticker_prefers_platform_returned_file_id():
    """平台返回了新 file_id 就回传它，能力层据此回填表情库。"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payloads.append(_request_payload(request))
        assert method == "sendSticker"
        return _ok({"message_id": 9, "sticker": {"file_id": "NEW_ID"}})

    adapter = TelegramAdapter(bot_token=TOKEN, offset_file=None, http_client=_client_with(handler))

    async def scenario():
        result = await adapter.call_capability(
            "send_sticker",
            chat_id=-100123,
            sticker={"native_refs": {"telegram": {"file_id": "OLD_ID"}}},
        )
        await close(adapter)
        return result

    result = asyncio.run(scenario())
    assert payloads[0] == {"chat_id": -100123, "sticker": "OLD_ID"}
    assert result == {"message_id": 9, "file_id": "NEW_ID"}


def test_send_sticker_uploads_local_file(tmp_path):
    """没有原生引用时把表情库里的图片文件上传成表情。"""
    sticker_file = tmp_path / "cat.png"
    sticker_file.write_bytes(b"\x89PNG-fake-sticker")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_request_payload(request))
        return _ok({"message_id": 7, "sticker": {"file_id": "UP_ID"}})

    adapter = TelegramAdapter(bot_token=TOKEN, offset_file=None, http_client=_client_with(handler))

    async def scenario():
        result = await adapter.call_capability(
            "send_sticker", chat_id="111", sticker={"file": str(sticker_file)}
        )
        await close(adapter)
        return result

    result = asyncio.run(scenario())
    # multipart：文件按 sticker 字段上传，扩展名沿用本地文件
    assert payloads[0]["chat_id"] == "111"
    assert payloads[0]["sticker"] == ("sticker.png", b"\x89PNG-fake-sticker")
    assert result["file_id"] == "UP_ID"


def test_send_sticker_uploads_base64():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_request_payload(request))
        return _ok({"message_id": 8, "sticker": {"file_id": "B64_ID"}})

    adapter = TelegramAdapter(bot_token=TOKEN, offset_file=None, http_client=_client_with(handler))

    async def scenario():
        result = await adapter.call_capability(
            "send_sticker",
            chat_id=1,
            sticker={
                "base64": base64.b64encode(b"webp-bytes").decode("ascii"),
                "mime_type": "image/webp",
            },
        )
        await close(adapter)
        return result

    result = asyncio.run(scenario())
    assert payloads[0]["sticker"] == ("sticker.webp", b"webp-bytes")
    assert result["file_id"] == "B64_ID"


def test_send_sticker_without_source_raises():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        with pytest.raises(CapabilityError, match="缺少可用的表情引用或图片文件"):
            await adapter.call_capability("send_sticker", chat_id=1, sticker={"id": "st_x"})
        await close(adapter)

    asyncio.run(scenario())


def test_send_sticker_requires_chat_id():
    adapter = make_adapter(FakeTelegram())

    async def scenario():
        with pytest.raises(CapabilityError, match="需要 chat_id"):
            await adapter.call_capability(
                "send_sticker", sticker={"native_refs": {"telegram": {"file_id": "x"}}}
            )
        await close(adapter)

    asyncio.run(scenario())


def test_send_sticker_api_error_is_capability_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request: wrong file identifier"}
        )

    adapter = TelegramAdapter(bot_token=TOKEN, offset_file=None, http_client=_client_with(handler))

    async def scenario():
        with pytest.raises(CapabilityError, match="send_sticker 失败"):
            await adapter.call_capability(
                "send_sticker",
                chat_id=1,
                sticker={"native_refs": {"telegram": {"file_id": "bad"}}},
            )
        await close(adapter)

    asyncio.run(scenario())
