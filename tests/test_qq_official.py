"""QQ 官方机器人适配器：token 刷新、事件解析、被动回复、网关重连。

全部走假 HTTP（httpx.MockTransport）与假 WebSocket，不碰网络。
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import rubenbot.adapters.qq_official as module
from rubenbot.adapters.base import Segment
from rubenbot.adapters.qq_official import (
    QQAPIError,
    QQOfficialAdapter,
    QQOfficialError,
    QQTokenError,
    media_upload_payload,
    parse_qq_official_event,
    split_outgoing_segments,
)
from rubenbot.config import Config

# ----------------------------------------------------------------------
# 假 HTTP / 假 WS
# ----------------------------------------------------------------------


class FakeHTTP:
    """按路径分发的假 QQ 官方 REST 层，记录所有请求。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_responses: list[httpx.Response] = []
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app/getAppAccessToken":
            self.token_calls += 1
            if self.token_responses:
                return self.token_responses.pop(0)
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 7200})
        response = self.routes.get((request.method, path))
        if response is None:
            return httpx.Response(404, json={"code": 404, "message": "not found"})
        return response

    def client(self, base_url: str = "https://api.sgroup.qq.com") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(self.handler))

    def json_body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def make_adapter(fake: FakeHTTP, **kwargs) -> QQOfficialAdapter:
    adapter = QQOfficialAdapter(
        app_id="APPID",
        client_secret="SECRET",
        http_client=fake.client(kwargs.pop("base_url", "https://api.sgroup.qq.com")),
        **kwargs,
    )
    return adapter


def run(coro):
    return asyncio.run(coro)


def c2c_payload(**overrides):
    payload = {
        "id": "MSG_C2C_1",
        "content": "你好呀",
        "timestamp": "2026-11-20T10:00:00+08:00",
        "author": {"id": "U1", "user_openid": "OPENID_A", "username": "阿花"},
        "attachments": [],
    }
    payload.update(overrides)
    return payload


def group_payload(**overrides):
    payload = {
        "id": "MSG_GROUP_1",
        "content": "在吗",
        "timestamp": "2026-11-20T10:00:00+08:00",
        "group_openid": "GROUP_A",
        "author": {
            "id": "M1",
            "member_openid": "MEMBER_1",
            "member_nickname": "花姐",
            "username": "ahua",
        },
        "attachments": [],
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# 事件解析
# ----------------------------------------------------------------------


def test_parse_c2c_message():
    event = parse_qq_official_event("C2C_MESSAGE_CREATE", c2c_payload())
    assert event is not None
    assert event.platform == "qq_official"
    assert event.chat_type == "private"
    assert event.is_private
    assert event.chat_id == "OPENID_A"
    assert event.sender_id == "OPENID_A"
    assert event.sender_name == "阿花"
    assert event.text == "你好呀"
    # 私聊全部收下
    assert event.mentions_bot is True
    assert event.raw["_reply_message_id"] == "MSG_C2C_1"
    assert event.raw["_event_type"] == "C2C_MESSAGE_CREATE"
    assert [s.type for s in event.segments] == ["text"]


def test_parse_group_at_message():
    event = parse_qq_official_event("GROUP_AT_MESSAGE_CREATE", group_payload())
    assert event is not None
    assert event.chat_type == "group"
    assert event.chat_id == "GROUP_A"
    assert event.sender_id == "MEMBER_1"
    assert event.sender_name == "花姐"  # 群昵称优先
    assert event.text == "在吗"
    # 群聊 AT 事件本身即 @ 机器人
    assert event.mentions_bot is True


def test_parse_group_falls_back_to_username():
    payload = group_payload(author={"member_openid": "MEMBER_1", "username": "ahua"})
    event = parse_qq_official_event("GROUP_AT_MESSAGE_CREATE", payload)
    assert event.sender_name == "ahua"


def test_parse_image_attachments():
    payload = c2c_payload(
        attachments=[
            {
                "content_type": "image/png",
                "filename": "a.png",
                "url": "https://cdn.example/a.png",
                "width": 100,
                "height": 100,
            },
            {"content_type": "video/mp4", "filename": "b.mp4", "url": "https://cdn.example/b.mp4"},
            {"content_type": "image/jpeg", "url": "https://cdn.example/c.jpg"},
        ]
    )
    event = parse_qq_official_event("C2C_MESSAGE_CREATE", payload)
    assert event is not None
    assert [s.type for s in event.segments] == ["text", "image", "image"]
    image = event.segments[1]
    assert image["data"]["url"] == "https://cdn.example/a.png"
    assert image["data"]["filename"] == "a.png"
    # text 是纯文本投影：图片变占位符
    assert event.text == "你好呀[图片][图片]"


def test_parse_ignores_bot_author():
    payload = c2c_payload(author={"user_openid": "BOT_OPENID", "bot": True})
    assert parse_qq_official_event("C2C_MESSAGE_CREATE", payload) is None


def test_parse_ignores_non_message_events_and_bad_payloads():
    assert parse_qq_official_event("READY", {"session_id": "x"}) is None
    assert parse_qq_official_event("GROUP_MESSAGE_CREATE", group_payload()) is None
    assert parse_qq_official_event("C2C_MESSAGE_CREATE", {"id": "1", "content": "x"}) is None
    assert parse_qq_official_event("GROUP_AT_MESSAGE_CREATE", {"group_openid": "G"}) is None


# ----------------------------------------------------------------------
# 出站消息段拼装
# ----------------------------------------------------------------------


def test_split_outgoing_segments():
    parts = split_outgoing_segments(
        [
            Segment.text("看这个"),
            Segment.image("https://cdn.example/a.png"),
            Segment.text("好看吗"),
            Segment.at("123"),
            Segment("face", {"id": "1"}),
        ]
    )
    assert [kind for kind, _ in parts] == ["text", "image", "text"]
    assert parts[0][1] == "看这个"
    assert parts[2][1] == "好看吗[表情]"  # 非文本段投影成占位符


def test_split_outgoing_skips_empty_and_at_reply():
    assert split_outgoing_segments([Segment.text("   "), Segment.reply("9"), Segment.at("1")]) == []


def test_media_upload_payload_variants(tmp_path):
    assert media_upload_payload({"url": "https://x/a.png"}) == {
        "file_type": 1,
        "srv_send_msg": False,
        "url": "https://x/a.png",
    }
    assert media_upload_payload({"base64": "base64://QUJD", "filename": "a.png"}) == {
        "file_type": 1,
        "srv_send_msg": False,
        "file_data": "QUJD",
        "file_name": "a.png",
    }
    data_uri = media_upload_payload({"base64": "data:image/png;base64,QUJD"})
    assert data_uri["file_data"] == "QUJD"

    image = tmp_path / "a.png"
    image.write_bytes(b"PNG")
    from_file = media_upload_payload({"file": str(image)})
    assert from_file["file_name"] == "a.png"
    assert from_file["file_data"] == base64.b64encode(b"PNG").decode()

    assert media_upload_payload({"file": str(tmp_path / "missing.png")}) is None
    assert media_upload_payload({}) is None


# ----------------------------------------------------------------------
# token
# ----------------------------------------------------------------------


def test_token_fetch_and_cache():
    fake = FakeHTTP()
    adapter = make_adapter(fake)

    async def scenario():
        first = await adapter._ensure_token()
        second = await adapter._ensure_token()
        await adapter._client.aclose()
        return first, second

    first, second = run(scenario())
    assert first == second == "TOKEN"
    assert fake.token_calls == 1
    token_request = fake.requests[0]
    assert token_request.url.path == "/app/getAppAccessToken"
    assert json.loads(token_request.content) == {"appId": "APPID", "clientSecret": "SECRET"}


def test_token_refreshes_before_expiry():
    fake = FakeHTTP()
    fake.token_responses = [
        httpx.Response(200, json={"access_token": "OLD", "expires_in": "7200"}),
        httpx.Response(200, json={"access_token": "NEW", "expires_in": "7200"}),
    ]
    adapter = make_adapter(fake)

    async def scenario():
        assert await adapter._ensure_token() == "OLD"
        # 逼近过期（余量 60 秒以内）应刷新
        adapter._token_expires_at = 0.0
        refreshed = await adapter._ensure_token()
        await adapter._client.aclose()
        return refreshed

    assert run(scenario()) == "NEW"
    assert fake.token_calls == 2


def test_token_refresh_failure_backs_off():
    fake = FakeHTTP()
    fake.token_responses = [
        httpx.Response(200, json={"code": 100007, "message": "appid invalid"}),
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"access_token": "OK", "expires_in": 7200}),
    ]
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(QQTokenError):
            await adapter._ensure_token()
        with pytest.raises(QQTokenError) as backoff:
            # 退避期内不再发请求，直接失败并说明原因
            await adapter._ensure_token()
        assert "退避" in str(backoff.value)
        await adapter._client.aclose()

    run(scenario())
    assert fake.token_calls == 1
    assert adapter._token_retry_at > 0


def test_token_refresh_failure_keeps_unexpired_old_token():
    fake = FakeHTTP()
    fake.token_responses = [
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(500, json={"message": "boom"}),
    ]
    adapter = make_adapter(fake)

    async def scenario():
        import time as _time

        adapter._token = "OLD"
        # 进入刷新余量（60 秒内）但还没真正过期
        adapter._token_expires_at = _time.monotonic() + 30
        first = await adapter._ensure_token()
        # 退避期内也不再发请求，继续用旧 token
        second = await adapter._ensure_token()
        await adapter._client.aclose()
        return first, second

    # 旧 token 尚未过期：刷新失败也继续用
    assert run(scenario()) == ("OLD", "OLD")
    assert fake.token_calls == 1
    assert adapter._token_retry_at > 0


def test_401_invalidates_token():
    fake = FakeHTTP()
    fake.routes[("GET", "/gateway")] = httpx.Response(
        401, json={"code": 11244, "message": "AccessToken无效或过期"}
    )
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(QQAPIError) as exc:
            await adapter._api("GET", "/gateway")
        await adapter._client.aclose()
        return exc.value

    error = run(scenario())
    assert error.code == 11244
    assert adapter._token is None  # 下次调用强制刷新


# ----------------------------------------------------------------------
# 发送（被动回复）
# ----------------------------------------------------------------------


def _remember(adapter: QQOfficialAdapter, chat_type: str, chat_id: str, msg_id: str) -> None:
    from rubenbot.adapters.qq_official import _PassiveReply
    import time as _time

    adapter._reply_state[(chat_type, chat_id)] = _PassiveReply(
        msg_id=msg_id, received_at=_time.monotonic()
    )


def test_send_group_text_with_msg_id_and_seq():
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/groups/GROUP_A/messages")] = httpx.Response(
        200, json={"id": "OUT_1", "timestamp": "2026-11-20T10:00:01+08:00"}
    )
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")

    async def scenario():
        await adapter.send("GROUP_A", [Segment.text("收到")], chat_type="group")
        await adapter.send("GROUP_A", [Segment.text("再来一条")], chat_type="group")
        await adapter._client.aclose()

    run(scenario())
    posts = [r for r in fake.requests if r.url.path == "/v2/groups/GROUP_A/messages"]
    assert len(posts) == 2
    first, second = (json.loads(r.content) for r in posts)
    assert first == {"msg_type": 0, "content": "收到", "msg_id": "MSG_GROUP_1", "msg_seq": 1}
    # 同一 msg_id 多次回复必须递增 msg_seq，否则平台按重复消息拒绝
    assert second["msg_seq"] == 2
    assert posts[0].headers["Authorization"] == "QQBot TOKEN"


def test_send_private_text_path():
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/users/OPENID_A/messages")] = httpx.Response(200, json={"id": "OUT"})
    adapter = make_adapter(fake)
    _remember(adapter, "private", "OPENID_A", "MSG_C2C_1")

    async def scenario():
        await adapter.send("OPENID_A", [Segment.text("嗨")], chat_type="private")
        await adapter._client.aclose()

    run(scenario())
    body = fake.json_body()
    assert body["msg_id"] == "MSG_C2C_1"
    assert fake.requests[-1].url.path == "/v2/users/OPENID_A/messages"


def test_send_text_and_image_uses_upload_then_media_message():
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/groups/GROUP_A/files")] = httpx.Response(
        200, json={"file_uuid": "uuid-1", "file_info": "FILE_INFO", "ttl": 3600}
    )
    fake.routes[("POST", "/v2/groups/GROUP_A/messages")] = httpx.Response(200, json={"id": "OUT"})
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")

    async def scenario():
        await adapter.send(
            "GROUP_A",
            [Segment.text("看"), Segment.image("https://cdn.example/a.png")],
            chat_type="group",
        )
        await adapter._client.aclose()

    run(scenario())
    upload = next(r for r in fake.requests if r.url.path == "/v2/groups/GROUP_A/files")
    assert json.loads(upload.content) == {
        "file_type": 1,
        "srv_send_msg": False,
        "url": "https://cdn.example/a.png",
    }
    messages = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/messages")]
    assert messages[0]["msg_type"] == 0
    assert messages[0]["msg_seq"] == 1
    assert messages[1] == {
        "msg_type": 7,
        "media": {"file_info": "FILE_INFO"},
        "msg_id": "MSG_GROUP_1",
        "msg_seq": 2,
    }


def test_send_rejects_when_no_inbound_message():
    fake = FakeHTTP()
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(QQOfficialError) as exc:
            await adapter.send("GROUP_X", [Segment.text("hi")])
        await adapter._client.aclose()
        return exc.value

    assert "还没收到过消息" in str(run(scenario()))


def test_send_rejects_after_passive_window(monkeypatch):
    fake = FakeHTTP()
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")
    # 把时钟往后拨过窗口
    import time as _time

    state = adapter._reply_state[("group", "GROUP_A")]
    state.received_at = _time.monotonic() - 301

    async def scenario():
        with pytest.raises(QQOfficialError) as exc:
            await adapter.send("GROUP_A", [Segment.text("hi")])
        await adapter._client.aclose()
        return exc.value

    message = str(run(scenario()))
    assert "被动回复已超时" in message
    assert "300" in message


def test_send_rejects_after_reply_limit():
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/groups/GROUP_A/messages")] = httpx.Response(200, json={"id": "OUT"})
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")

    async def scenario():
        for _ in range(5):
            await adapter.send("GROUP_A", [Segment.text("x")])
        with pytest.raises(QQOfficialError) as exc:
            await adapter.send("GROUP_A", [Segment.text("第六条")])
        await adapter._client.aclose()
        return exc.value

    message = str(run(scenario()))
    assert "最多回复 5 次" in message


def test_send_surfaces_platform_error_code():
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/groups/GROUP_A/messages")] = httpx.Response(
        400, json={"code": 40034128, "message": "被动回复时间或次数超限"}
    )
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")

    async def scenario():
        with pytest.raises(QQAPIError) as exc:
            await adapter.send("GROUP_A", [Segment.text("x")])
        await adapter._client.aclose()
        return exc.value

    error = run(scenario())
    assert error.code == 40034128
    assert "被动回复时间或次数超限" in str(error)


# ----------------------------------------------------------------------
# 假 WebSocket 与网关
# ----------------------------------------------------------------------


class FakeWS:
    """可脚本化的假网关连接。"""

    def __init__(self, messages: list, *, close_code: int | None = None):
        self._messages = list(messages)
        self.sent: list[dict] = []
        self.close_code = close_code
        self.closed = False
        self._closed_event = asyncio.Event()

    def __aiter__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        self._closed_event.set()
        return False

    async def __anext__(self):
        if not self._messages:
            # 消息放完就等着被关，模拟长连接
            await self._closed_event.wait()
            raise StopAsyncIteration
        item = self._messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item) if not isinstance(item, str) else item

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        self._closed_event.set()


class FakeConnect:
    """替换 module.ws_connect：记录连接次数，按脚本返回 FakeWS 或抛错。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        factory = self.script.pop(0) if self.script else (lambda: FakeWS([]))
        return factory()


def _hello(interval: int = 30000) -> dict:
    return {"op": 10, "d": {"heartbeat_interval": interval}}


def _ready(session_id: str = "SESSION_1", seq: int = 1) -> dict:
    return {
        "op": 0,
        "s": seq,
        "t": "READY",
        "d": {"version": 1, "session_id": session_id, "shard": [0, 1], "user": {"id": "BOT"}},
    }


@pytest.fixture()
def gateway_url(monkeypatch):
    """假 /gateway 返回固定地址。"""
    fake = FakeHTTP()
    fake.routes[("GET", "/gateway")] = httpx.Response(200, json={"url": "wss://fake/websocket"})

    def make(**kwargs):
        adapter = QQOfficialAdapter(
            app_id="APPID",
            client_secret="SECRET",
            http_client=fake.client(),
            **kwargs,
        )
        return adapter

    return make, fake


def test_gateway_identify_heartbeat_and_events(monkeypatch, gateway_url):
    """Hello → Identify → READY → 心跳；事件交给回调。"""
    make, _fake = gateway_url
    received = []

    async def on_event(event):
        received.append(event)

    class RecordingWS(FakeWS):
        pass

    ws = RecordingWS(
        [
            _hello(interval=50),
            _ready(),
            {"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "d": c2c_payload()},
            {"op": 11},
            {"op": 0, "s": 3, "t": "GROUP_AT_MESSAGE_CREATE", "d": group_payload()},
            {"op": 0, "s": 4, "t": "SOMETHING_ELSE", "d": {}},
        ]
    )
    connect = FakeConnect([lambda: ws])
    monkeypatch.setattr(module, "ws_connect", connect)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        await asyncio.sleep(0.25)  # 留出至少一次心跳
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())

    assert [e.chat_type for e in received] == ["private", "group"]
    identify = next(p for p in ws.sent if p["op"] == module.OP_IDENTIFY)
    assert identify["d"]["token"] == "QQBot TOKEN"
    assert identify["d"]["intents"] == module.INTENT_GROUP_AND_C2C
    assert identify["d"]["shard"] == [0, 1]
    heartbeats = [p for p in ws.sent if p["op"] == module.OP_HEARTBEAT]
    assert heartbeats
    # 心跳 d 是最近收到的 seq；收到 seq=4 之前可能已经发过一两次心跳
    assert {p["d"] for p in heartbeats} <= {None, 2, 3, 4}
    assert heartbeats[-1]["d"] == 4
    assert adapter.event_count == 2
    assert ws.closed
    # stop 会清掉会话，避免下次启动拿旧 seq 去 Resume
    assert adapter._last_seq is None


def test_gateway_resumes_with_session_id(monkeypatch, gateway_url):
    """已有会话时重连发 Resume（op 6），而不是重新 Identify。"""
    make, _fake = gateway_url
    ws = FakeWS([_hello(interval=30000)])
    connect = FakeConnect([lambda: ws])
    monkeypatch.setattr(module, "ws_connect", connect)

    adapter = make()
    adapter._session_id = "SESSION_OLD"
    adapter._last_seq = 42

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.sleep(0.05)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    assert ws.sent and ws.sent[0]["op"] == module.OP_RESUME
    assert ws.sent[0]["d"] == {
        "token": "QQBot TOKEN",
        "session_id": "SESSION_OLD",
        "seq": 42,
    }
    # 主动 stop 后清掉会话，下次启动走完整 Identify
    assert adapter._session_id is None


def test_gateway_invalid_session_resets_and_reconnects(monkeypatch, gateway_url):
    make, _fake = gateway_url
    first = FakeWS([_hello(), _ready("SESSION_BAD"), {"op": 9}])
    second = FakeWS([_hello(interval=30000)])
    connect = FakeConnect([lambda: first, lambda: second])
    monkeypatch.setattr(module, "ws_connect", connect)
    monkeypatch.setattr(module, "_RECONNECT_MIN", 0.01)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.sleep(0.15)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    assert adapter.reconnect_count >= 1
    assert adapter._session_id is None
    # 第二次连接重新 Identify（被 Invalid Session 丢掉了旧会话）
    assert second.sent and second.sent[0]["op"] == module.OP_IDENTIFY


def test_gateway_reconnect_op_resumes_session(monkeypatch, gateway_url):
    make, _fake = gateway_url
    first = FakeWS([_hello(interval=30000), _ready("SESSION_KEEP"), {"op": 7}])
    second = FakeWS([_hello(interval=30000)])
    connect = FakeConnect([lambda: first, lambda: second])
    monkeypatch.setattr(module, "ws_connect", connect)
    monkeypatch.setattr(module, "_RECONNECT_MIN", 0.01)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.sleep(0.15)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    # op 7 只要求重连；会话仍有效，第二次连接走 Resume 并带上最后 seq
    assert len(connect.calls) == 2
    assert first.sent[-1]["op"] == module.OP_HEARTBEAT or first.sent
    resume = second.sent[0]
    assert resume["op"] == module.OP_RESUME
    assert resume["d"]["session_id"] == "SESSION_KEEP"
    assert resume["d"]["seq"] == 1


def test_start_reconnects_after_connect_failure(monkeypatch, gateway_url):
    make, _fake = gateway_url

    class FailingConnect:
        def __init__(self, error):
            self.error = error

        async def __aenter__(self):
            raise self.error

        async def __aexit__(self, *exc):
            return False

    attempts = []

    def factory(url, **kwargs):
        attempts.append(url)
        return FailingConnect(OSError("连接被拒绝"))

    monkeypatch.setattr(module, "ws_connect", factory)
    monkeypatch.setattr(module, "_RECONNECT_MIN", 0.01)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: None))
        await asyncio.sleep(0.08)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    assert len(attempts) >= 2
    assert adapter.reconnect_count >= 1


def test_single_event_exception_does_not_kill_loop(monkeypatch, gateway_url):
    make, _fake = gateway_url
    ws = FakeWS(
        [
            _hello(interval=30000),
            "{not json",
            {"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "d": c2c_payload()},
        ]
    )
    monkeypatch.setattr(module, "ws_connect", FakeConnect([lambda: ws]))
    received = []

    async def on_event(event):
        received.append(event)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        await asyncio.sleep(0.08)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    # 坏消息被记日志跳过，后续事件照常处理
    assert len(received) == 1
    assert received[0].chat_id == "OPENID_A"


def test_inbound_image_downloaded_to_base64(monkeypatch, gateway_url):
    make, fake = gateway_url
    fake.routes[("GET", "/cdn/a.png")] = httpx.Response(200, content=b"PNGDATA")
    ws = FakeWS(
        [
            _hello(interval=30000),
            {
                "op": 0,
                "s": 1,
                "t": "C2C_MESSAGE_CREATE",
                "d": c2c_payload(
                    attachments=[{"content_type": "image/png", "url": "https://api.sgroup.qq.com/cdn/a.png"}]
                ),
            },
        ]
    )
    monkeypatch.setattr(module, "ws_connect", FakeConnect([lambda: ws]))
    received = []

    async def on_event(event):
        received.append(event)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        await asyncio.sleep(0.08)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    image = received[0].segments[1]
    assert image["data"]["base64"] == base64.b64encode(b"PNGDATA").decode()


def test_inbound_image_keeps_url_when_download_fails(monkeypatch, gateway_url):
    make, fake = gateway_url
    fake.routes[("GET", "/cdn/broken.png")] = httpx.Response(500, text="nope")
    ws = FakeWS(
        [
            _hello(interval=30000),
            {
                "op": 0,
                "s": 1,
                "t": "GROUP_AT_MESSAGE_CREATE",
                "d": group_payload(
                    attachments=[
                        {"content_type": "image/png", "url": "https://api.sgroup.qq.com/cdn/broken.png"}
                    ]
                ),
            },
        ]
    )
    monkeypatch.setattr(module, "ws_connect", FakeConnect([lambda: ws]))
    received = []

    async def on_event(event):
        received.append(event)

    adapter = make()

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        await asyncio.sleep(0.08)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    image = received[0].segments[1]
    assert "base64" not in image["data"]
    assert image["data"]["url"].endswith("/cdn/broken.png")


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_qq_official_config_env_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("QQBOT_ID", "102030")
    monkeypatch.setenv("QQBOT_SECRET", "s3cret")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
adapters:
  qq_official:
    app_id: "${QQBOT_ID}"
    client_secret: "${QQBOT_SECRET}"
    sandbox: true
""",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.enabled_adapters == ["qq_official"]
    options = cfg.qq_official_options()
    assert options.app_id == "102030"
    assert options.client_secret == "s3cret"
    assert options.sandbox is True
    assert options.configured


def test_qq_official_config_defaults_and_sandbox(monkeypatch):
    from rubenbot.config import QQOfficialConfig

    assert QQOfficialConfig.from_options({}).configured is False
    assert QQOfficialConfig.from_options({"app_id": "1", "client_secret": "x"}).sandbox is False
    # 只认真布尔，字符串 "true" 不算
    assert QQOfficialConfig.from_options({"sandbox": "true"}).sandbox is False
    assert QQOfficialConfig.from_options({"download_images": False}).download_images is False

    adapter = QQOfficialAdapter(app_id="1", client_secret="x", sandbox=True)
    assert adapter.api_base == "https://sandbox.api.sgroup.qq.com"


def test_from_config_builds_adapter():
    from rubenbot.config import Config

    cfg = Config.from_dict(
        {
            "adapters": {
                "qq_official": {
                    "app_id": "102030",
                    "client_secret": "s3cret",
                    "sandbox": True,
                    "download_images": False,
                }
            }
        }
    )
    adapter = QQOfficialAdapter.from_config(cfg)
    assert adapter.app_id == "102030"
    assert adapter.client_secret == "s3cret"
    assert adapter.sandbox is True
    assert adapter.api_base == "https://sandbox.api.sgroup.qq.com"
    assert adapter.download_images is False


def test_sandbox_base_url_is_used_for_rest(monkeypatch):
    fake = FakeHTTP()
    fake.routes[("GET", "/gateway")] = httpx.Response(200, json={"url": "wss://sandbox/ws"})
    adapter = QQOfficialAdapter(
        app_id="APPID",
        client_secret="SECRET",
        sandbox=True,
        http_client=fake.client(base_url="https://sandbox.api.sgroup.qq.com"),
    )

    async def scenario():
        url = await adapter._gateway_url()
        await adapter._client.aclose()
        return url

    assert run(scenario()) == "wss://sandbox/ws"
    gateway = next(r for r in fake.requests if r.url.path == "/gateway")
    assert gateway.url.host == "sandbox.api.sgroup.qq.com"
    assert gateway.headers["Authorization"] == "QQBot TOKEN"


# ----------------------------------------------------------------------
# 能力：发表情（send_sticker，图片兜底）
# ----------------------------------------------------------------------


def _sticker_adapter(fake: FakeHTTP) -> QQOfficialAdapter:
    fake.routes[("POST", "/v2/groups/GROUP_A/files")] = httpx.Response(
        200, json={"file_uuid": "uuid-1", "file_info": "FILE_INFO", "ttl": 3600}
    )
    fake.routes[("POST", "/v2/groups/GROUP_A/messages")] = httpx.Response(200, json={"id": "OUT"})
    adapter = make_adapter(fake)
    _remember(adapter, "group", "GROUP_A", "MSG_GROUP_1")
    return adapter


def test_send_sticker_uploads_image_and_replies_passively():
    fake = FakeHTTP()
    adapter = _sticker_adapter(fake)
    sticker = {"id": "st_1", "url": "https://cdn.example/sticker.webp"}

    async def scenario():
        result = await adapter.call_capability(
            "send_sticker", chat_id="GROUP_A", sticker=sticker
        )
        await adapter._client.aclose()
        return result

    result = run(scenario())
    upload = next(r for r in fake.requests if r.url.path == "/v2/groups/GROUP_A/files")
    assert json.loads(upload.content) == {
        "file_type": 1,
        "srv_send_msg": False,
        "url": "https://cdn.example/sticker.webp",
    }
    post = next(r for r in fake.requests if r.url.path == "/v2/groups/GROUP_A/messages")
    assert json.loads(post.content) == {
        "msg_type": 7,
        "media": {"file_info": "FILE_INFO"},
        "msg_id": "MSG_GROUP_1",
        "msg_seq": 1,
    }
    # QQ 官方没有原生表情引用可回填
    assert result == {"sent": True}


def test_send_sticker_reads_local_file_and_sends_base64(tmp_path):
    fake = FakeHTTP()
    adapter = _sticker_adapter(fake)
    path = tmp_path / "cat.png"
    path.write_bytes(b"png-bytes")

    async def scenario():
        await adapter.call_capability(
            "send_sticker", chat_id="GROUP_A", sticker={"file": str(path)}
        )
        await adapter._client.aclose()

    run(scenario())
    upload = next(r for r in fake.requests if r.url.path == "/v2/groups/GROUP_A/files")
    body = json.loads(upload.content)
    assert body["file_name"] == "cat.png"
    assert base64.b64decode(body["file_data"]) == b"png-bytes"


def test_send_sticker_without_source_raises():
    fake = FakeHTTP()
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(module.CapabilityError, match="缺少可用的图片文件或 URL"):
            await adapter.call_capability(
                "send_sticker", chat_id="GROUP_A", sticker={"id": "st_x"}
            )
        await adapter._client.aclose()

    run(scenario())


def test_send_sticker_requires_chat_id():
    fake = FakeHTTP()
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(module.CapabilityError, match="需要 chat_id"):
            await adapter.call_capability(
                "send_sticker", sticker={"url": "https://cdn.example/x.webp"}
            )
        await adapter._client.aclose()

    run(scenario())


def test_send_sticker_without_inbound_message_is_capability_error():
    """QQ 官方只支持被动回复：没收到过消息时能力失败要收敛成 CapabilityError。"""
    fake = FakeHTTP()
    fake.routes[("POST", "/v2/groups/GROUP_B/files")] = httpx.Response(
        200, json={"file_uuid": "u", "file_info": "FILE_INFO"}
    )
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(module.CapabilityError, match="send_sticker 失败"):
            await adapter.call_capability(
                "send_sticker",
                chat_id="GROUP_B",
                sticker={"url": "https://cdn.example/x.webp"},
            )
        await adapter._client.aclose()

    run(scenario())


def test_unsupported_capability_raises():
    fake = FakeHTTP()
    adapter = make_adapter(fake)

    async def scenario():
        with pytest.raises(module.CapabilityNotSupported):
            await adapter.call_capability("message.emoji_like", message_id=1, emoji_id=2)
        await adapter._client.aclose()

    run(scenario())
