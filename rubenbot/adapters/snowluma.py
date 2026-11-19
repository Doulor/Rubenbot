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

from .base import BaseAdapter, EventCallback, MessageEvent, Segment

logger = logging.getLogger(__name__)

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0
# 字符串形式消息里的 CQ 码
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")
_CQ_ANY_RE = re.compile(r"\[CQ:[^\]]+\]")


class SnowLumaAdapter(BaseAdapter):
    """OneBot v11 正向 WS + HTTP API。"""

    name = "snowluma"

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


def segment_to_onebot(segment: Segment) -> dict[str, Any]:
    """Segment -> OneBot v11 消息段。"""
    if segment.type == "text":
        return {"type": "text", "data": {"text": str(segment.data.get("text", ""))}}
    if segment.type == "image":
        url = segment.data.get("url") or segment.data.get("file") or ""
        return {"type": "image", "data": {"file": url}}
    if segment.type == "face":
        return {"type": "face", "data": {"id": str(segment.data.get("id", ""))}}
    return {"type": "text", "data": {"text": segment.render()}}


def onebot_to_segments(message: Any) -> str:
    """把 OneBot 的 message 字段（字符串或消息段数组）渲染为纯文本。"""
    if isinstance(message, str):
        # 字符串形式可能带 CQ 码：at 渲染成 @qq，其余去掉
        text = _CQ_AT_RE.sub(lambda m: "@" + m.group(1) + " ", message)
        return re.sub(r"\s+", " ", _CQ_ANY_RE.sub("", text)).strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for item in message:
        if not isinstance(item, dict):
            continue
        seg_type = item.get("type")
        data = item.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            parts.append(f"@{qq} " if qq else " ")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "reply":
            continue  # reply 段单独处理
    return "".join(parts).strip()


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
    text = onebot_to_segments(message).strip()
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
    )
