"""SnowLuma（OneBot v11）适配器。

- 正向 WebSocket 连接 OneBot v11 的 WS 服务（可配置 access_token）
- 通过 HTTP API 发送消息（send_group_msg / send_private_msg）
- 自动重连（指数退避）；WS 层 ping/pong 由 websockets 库负责，另可选 HTTP 心跳探测

OneBot v11 事件结构参考：post_type=message，message_type=group/private，
sender 里有 user_id / nickname，message 可能是字符串或消息段数组。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

try:  # websockets 13+ 的新版客户端 API
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 兼容旧版
    from websockets.client import connect as ws_connect  # type: ignore

from .base import (
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    EventCallback,
    MessageEvent,
    Segment,
    as_segment,
    render_segments,
)

logger = logging.getLogger(__name__)

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0
# 字符串形式消息里的 CQ @ 码（用于 mentions_bot 判断）
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")


class SnowLumaAdapter(BaseAdapter):
    """OneBot v11 正向 WS + HTTP API。"""

    name = "snowluma"

    # SnowLuma 在 OneBot v11 之外的扩展动作
    capabilities = frozenset({"message.emoji_like", "qzone.publish"})
    capability_config_keys = {
        "message.emoji_like": "emoji_like",
        "qzone.publish": "qzone",
    }
    # 配置未写开关时的默认值：贴表情默认开，发说说默认关
    capability_defaults = {"qzone.publish": False}

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:3001",
        http_url: str = "http://127.0.0.1:3000",
        access_token: str = "",
        self_id: str | int | None = None,
        reconnect: bool = True,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.self_id = str(self_id) if self_id not in (None, "") else None
        self.reconnect = reconnect
        self._client = http_client
        self._owns_client = http_client is None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._on_event: EventCallback | None = None
        self._ws: Any = None
        # 记录 bot 自己发出的消息 id，用于识别“回复 bot”
        self._bot_message_ids: set[str] = set()
        # 统计信息，便于调试
        self.reconnect_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, on_event: EventCallback) -> None:
        """连接并持续收事件；返回意味着已 stop。"""
        self._on_event = on_event
        self._stop.clear()
        delay = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = _RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接错误需要重试
                if self._stop.is_set():
                    break
                logger.warning("SnowLuma WS 断开：%s（%.0fs 后重连）", exc, delay)
            if not self.reconnect or self._stop.is_set():
                break
            self.reconnect_count += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, _RECONNECT_MAX)

    async def _run_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else None
        async with ws_connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        ) as ws:
            self._ws = ws
            logger.info("SnowLuma WS 已连接：%s", self.ws_url)
            async for message in ws:
                if self._stop.is_set():
                    break
                try:
                    await self._handle_raw(message)
                except Exception as exc:  # noqa: BLE001 - 单条事件解析失败不能断连接
                    logger.warning("处理 OneBot 事件失败：%s", exc)
        self._ws = None

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 事件解析
    # ------------------------------------------------------------------

    async def _handle_raw(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            data = json.loads(raw)
        else:
            data = raw
        if not isinstance(data, dict):
            return
        # 先记录 bot 自己的消息 id（OneBot 若回显自身消息）
        if data.get("post_type") == "message" and data.get("user_id") is not None:
            effective_self = self.self_id or (str(data["self_id"]) if data.get("self_id") is not None else None)
            if effective_self is not None and str(data["user_id"]) == effective_self:
                if data.get("message_id") is not None:
                    self._bot_message_ids.add(str(data["message_id"]))
        event = parse_onebot_event(data, self_id=self.self_id)
        if event is None:
            return
        if event.reply_to_id and event.reply_to_id in self._bot_message_ids:
            event.mentions_bot = True
        if self._on_event is not None:
            await self._on_event(event)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
            self._client = httpx.AsyncClient(base_url=self.http_url, timeout=20.0, headers=headers)
            self._owns_client = True
        return self._client

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用 OneBot v11 HTTP API。"""
        client = await self._get_client()
        resp = await client.post(f"/{action}", json=params or {})
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("status") == "failed":
            raise RuntimeError(f"OneBot API {action} 失败：{payload}")
        return payload if isinstance(payload, dict) else {"data": payload}

    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> None:
        action = "send_private_msg" if chat_type == "private" else "send_group_msg"
        key = "user_id" if chat_type == "private" else "group_id"
        payload = await self.call_api(
            action,
            {key: _to_id(chat_id), "message": [segment_to_onebot(s) for s in segments]},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_id = data.get("message_id")
        if message_id is not None:
            self._bot_message_ids.add(str(message_id))
            # 简单裁剪，避免无限增长
            if len(self._bot_message_ids) > 500:
                self._bot_message_ids = set(list(self._bot_message_ids)[-250:])

    async def heartbeat(self) -> bool:
        """可选 HTTP 心跳探测：get_status 是否可用。"""
        try:
            data = await self.call_api("get_status", {})
            return bool(data)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 平台能力
    # ------------------------------------------------------------------

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        """调用 SnowLuma 扩展动作；失败统一抛 :class:`CapabilityError`。"""
        if name == "message.emoji_like":
            message_id = kwargs.get("message_id")
            emoji_id = kwargs.get("emoji_id", kwargs.get("emoji"))
            if message_id in (None, "") or emoji_id in (None, ""):
                raise CapabilityError("message.emoji_like 需要 message_id 和 emoji_id")
            return await self._extension_api(
                name,
                "set_msg_emoji_like",
                {"message_id": _to_id(message_id), "emoji_id": str(emoji_id)},
            )
        if name == "qzone.publish":
            content = kwargs.get("content")
            if content is None or not str(content).strip():
                raise CapabilityError("qzone.publish 需要非空 content")
            return await self._extension_api(name, "send_qzone_msg", {"content": str(content)})
        raise CapabilityNotSupported(self.name, name)

    async def _extension_api(self, capability: str, action: str, params: dict[str, Any]) -> Any:
        """扩展动作调用：把网络/平台错误收敛为能力失败原因。"""
        try:
            payload = await self.call_api(action, params)
        except Exception as exc:  # noqa: BLE001 - 能力失败不能打断调用方
            raise CapabilityError(f"{capability} 失败（{action}）：{exc}") from exc
        return payload.get("data", payload)


def _to_id(value: str | int) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def segment_to_onebot(segment: Segment | dict[str, Any]) -> dict[str, Any]:
    """Segment -> OneBot v11 消息段。

    图片支持 ``file`` / ``url`` / ``base64`` 任一输入：OneBot 的 ``file``
    字段本身接受本地路径、URL 或 ``base64://`` 数据。
    """
    segment = as_segment(segment)
    data = segment.data
    if segment.type == "text":
        return {"type": "text", "data": {"text": str(data.get("text", ""))}}
    if segment.type == "image":
        return {"type": "image", "data": {"file": _image_source(data)}}
    if segment.type == "at":
        return {"type": "at", "data": {"qq": str(data.get("qq", ""))}}
    if segment.type == "reply":
        return {"type": "reply", "data": {"id": str(data.get("id", ""))}}
    if segment.type == "face":
        return {"type": "face", "data": {"id": str(data.get("id", ""))}}
    if segment.type in ("record", "video", "file"):
        source = data.get("file") or data.get("url") or ""
        return {"type": segment.type, "data": {"file": source}}
    # 未知类型退化为文本，保证消息不会凭空丢失
    return {"type": "text", "data": {"text": segment.render()}}


def _image_source(data: dict[str, Any]) -> str:
    """图片段的输入归一：file 优先，其次 url，最后 base64。"""
    file = data.get("file")
    if file:
        return str(file)
    url = data.get("url")
    if url:
        return str(url)
    raw = data.get("base64")
    if raw:
        value = str(raw)
        if value.startswith(("base64://", "data:")):
            return value
        return "base64://" + value
    return ""


# OneBot 段类型 -> 保留的 data 字段
_SEGMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "image": ("file", "url", "summary"),
    "at": ("qq", "name"),
    "reply": ("id",),
    "face": ("id",),
    "record": ("file", "url"),
    "video": ("file", "url"),
    "file": ("file", "url", "name"),
}

# 字符串形式消息里的 CQ 码：拆出 type 与 key=value 参数
_CQ_CODE_RE = re.compile(r"\[CQ:(?P<type>[A-Za-z0-9_.-]+)(?P<params>(?:,[^,\]]*=[^,\]]*)*)\]")
_CQ_PARAM_RE = re.compile(r",([^,=\]]+)=([^,\]]*)")


def onebot_message_segments(message: Any) -> list[Segment]:
    """OneBot ``message`` 字段（字符串或消息段数组）-> 统一消息段列表。"""
    if isinstance(message, str):
        return _cq_string_to_segments(message)
    if not isinstance(message, list):
        return []
    segments: list[Segment] = []
    for item in message:
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type") or "")
        if not seg_type:
            continue
        raw_data = item.get("data") or {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        fields = _SEGMENT_FIELDS.get(seg_type)
        if fields is None:
            # 未知类型原样保留，便于插件/后续扩展按名处理
            segments.append(Segment(seg_type, raw_data))
            continue
        data = {key: raw_data[key] for key in fields if raw_data.get(key) is not None}
        if seg_type in ("at", "reply", "face"):
            for key in data:
                data[key] = str(data[key])
        segments.append(Segment(seg_type, data))
    return segments


def _cq_string_to_segments(message: str) -> list[Segment]:
    """字符串形式（CQ 码）消息 -> 消息段列表。

    字符串形式只在没有消息段数组时出现（部分实现）；这里仅识别 ``at``，
    其余 CQ 码（图片、表情、红包等）按历史行为剔除，避免 ``[CQ:...]``
    原文进入模型上下文。
    """
    segments: list[Segment] = []
    pos = 0
    for match in _CQ_CODE_RE.finditer(message):
        if match.start() > pos:
            segments.append(Segment.text(message[pos : match.start()]))
        params = {key: value for key, value in _CQ_PARAM_RE.findall(match.group("params") or "")}
        if match.group("type").lower() == "at" and params.get("qq"):
            segments.append(Segment.at(params["qq"]))
        pos = match.end()
    if pos < len(message):
        segments.append(Segment.text(message[pos:]))
    return segments


def onebot_to_segments(message: Any) -> str:
    """把 OneBot 的 message 字段渲染为纯文本（``MessageEvent.text`` 的投影）。"""
    text = render_segments(onebot_message_segments(message))
    if isinstance(message, str):
        # 字符串形式按旧行为归一空白
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_reply_id(message: Any) -> str | None:
    if isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("type") == "reply":
                rid = (item.get("data") or {}).get("id")
                if rid is not None:
                    return str(rid)
    return None


def _mentions_self(message: Any, self_id: str | None) -> bool:
    if self_id is None:
        return False
    if isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("type") == "at":
                if str((item.get("data") or {}).get("qq")) == self_id:
                    return True
    elif isinstance(message, str):
        return self_id in _CQ_AT_RE.findall(message)
    return False


def parse_onebot_event(data: dict[str, Any], *, self_id: str | None = None) -> MessageEvent | None:
    """OneBot v11 消息事件 -> 统一事件；非消息事件返回 None。"""
    if data.get("post_type") != "message":
        return None
    message_type = data.get("message_type")
    if message_type not in ("group", "private"):
        return None
    if self_id is None and data.get("self_id") is not None:
        self_id = str(data["self_id"])
    sender = data.get("sender") or {}
    user_id = data.get("user_id")
    if user_id is None:
        return None
    if self_id is not None and str(user_id) == self_id:
        return None  # 自己的消息不回显

    message = data.get("message", data.get("raw_message", ""))
    segments = onebot_message_segments(message)
    text = onebot_to_segments(message)
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id)

    if message_type == "private":
        chat_id = str(user_id)
        chat_type = "private"
    else:
        chat_id = str(data.get("group_id"))
        chat_type = "group"

    reply_id = str(data.get("message_id")) if data.get("message_id") is not None else None
    reply_to = _extract_reply_id(message)
    mentions_bot = _mentions_self(message, self_id) or (
        self_id is not None and _mentions_self(data.get("raw_message"), self_id)
    )

    return MessageEvent(
        platform="snowluma",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id=str(user_id),
        sender_name=sender_name,
        text=text,
        reply_to_id=reply_to,
        mentions_bot=mentions_bot,
        raw={**data, "_reply_message_id": reply_id},
        segments=segments,
    )
