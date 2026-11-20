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
import mimetypes
import os
import re
import tempfile
from pathlib import Path
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
from .platform import KIND_NOTICE, KIND_REQUEST, PlatformEvent
from .qzone import QzoneClient

logger = logging.getLogger(__name__)

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0
# 字符串形式消息里的 CQ @ 码（用于 mentions_bot 判断）
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")


class SnowLumaAdapter(BaseAdapter):
    """OneBot v11 正向 WS + HTTP API。"""

    name = "snowluma"

    # SnowLuma 在 OneBot v11 之外的扩展动作，以及经 get_cookies 直调的空间接口
    capabilities = frozenset(
        {
            "message.emoji_like",
            "message.voice",
            "qzone.publish",
            "qzone.feeds",
            "qzone.like",
            "qzone.comment",
            "qzone.my_comments",
            "qzone.reply",
        }
    )
    capability_config_keys = {
        "message.emoji_like": "emoji_like",
        "message.voice": "voice",
        "qzone.publish": "qzone",
        # 空间只读能力共用一个开关；写操作各自独立，默认全关
        "qzone.feeds": "feeds",
        "qzone.my_comments": "feeds",
        "qzone.like": "like",
        "qzone.comment": "comment",
        "qzone.reply": "reply",
    }
    # 配置未写开关时的默认值：贴表情与空间只读默认开，其余（对外写操作）默认关
    capability_defaults = {
        "qzone.publish": False,
        "qzone.like": False,
        "qzone.comment": False,
        "qzone.reply": False,
    }

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:3001",
        http_url: str = "http://127.0.0.1:3000",
        access_token: str = "",
        self_id: str | int | None = None,
        reconnect: bool = True,
        http_client: httpx.AsyncClient | None = None,
        qzone_client: QzoneClient | None = None,
    ):
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.self_id = str(self_id) if self_id not in (None, "") else None
        self.reconnect = reconnect
        self._client = http_client
        self._owns_client = http_client is None
        # 空间接口客户端：缺省惰性创建，登录态 Cookie 由 get_cookies 取
        self._qzone = qzone_client
        self._owns_qzone = qzone_client is None
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
        if self._owns_qzone and self._qzone is not None:
            await self._qzone.aclose()
            self._qzone = None

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
            # 非消息事件：notice / request 归一成统一平台事件；心跳等直接忽略
            await self._dispatch_platform_event(data)
            return
        if event.reply_to_id and event.reply_to_id in self._bot_message_ids:
            event.mentions_bot = True
        if self._on_event is not None:
            await self._on_event(event)

    async def _dispatch_platform_event(self, data: dict[str, Any]) -> None:
        """把 notice / request 交给核心入口；自身引发的事件在入口过滤。

        入口过滤放在这里也放在核心（双保险）：这里能拿到协议端的 self_id，
        核心层则不依赖适配器是否传对。meta_event 的心跳没有语义，直接丢弃。
        """
        callback = self.platform_event_callback
        if callback is None:
            return
        event = parse_onebot_platform_event(data, self_id=self.self_id)
        if event is None:
            return
        await callback(event)

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

    async def call_action(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """通用动作通道：动作名原样作为 OneBot 接口名，走现有 HTTP 通道。

        风险分级、冷却与审计都由核心层（``rubenbot.gateway``）负责，这里
        只做协议层调用；失败抛 :class:`CapabilityError`（带上游信息），
        由网关收敛成给模型看的错误摘要。
        """
        action = str(name or "").strip()
        if not action:
            raise CapabilityError("动作名不能为空")
        try:
            payload = await self.call_api(action, dict(params or {}))
        except Exception as exc:  # noqa: BLE001 - 网关负责把失败原因回给模型
            raise CapabilityError(f"{action} 失败：{exc}") from exc
        return payload.get("data", payload) if isinstance(payload, dict) else payload


    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> str | None:
        action = "send_private_msg" if chat_type == "private" else "send_group_msg"
        key = "user_id" if chat_type == "private" else "group_id"
        payload = await self.call_api(
            action,
            {key: _to_id(chat_id), "message": [segment_to_onebot(s) for s in segments]},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_id = data.get("message_id")
        if message_id is None:
            return None
        message_id = str(message_id)
        self._bot_message_ids.add(message_id)
        # 简单裁剪，避免无限增长
        if len(self._bot_message_ids) > 500:
            self._bot_message_ids = set(list(self._bot_message_ids)[-250:])
        return message_id

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
        if name == "message.voice":
            return await self._send_voice(kwargs)
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
        if name.startswith("qzone."):
            return await self._qzone_capability(name, **kwargs)
        raise CapabilityNotSupported(self.name, name)

    async def _qzone_capability(self, capability: str, **kwargs: Any) -> Any:
        """空间互动能力：经 get_cookies 取登录态后直调空间 Web 接口。"""
        client = self._get_qzone_client()
        try:
            if capability == "qzone.feeds":
                return await client.feeds(
                    kwargs.get("uin"),
                    num=kwargs.get("num", 10),
                    pos=kwargs.get("pos", 0),
                )
            if capability == "qzone.my_comments":
                return await client.my_comments(
                    num=kwargs.get("num", 10),
                    pos=kwargs.get("pos", 0),
                )
            if capability == "qzone.like":
                return await client.like(_required(kwargs, "uin"), _required(kwargs, "tid"))
            if capability == "qzone.comment":
                return await client.comment(
                    _required(kwargs, "uin"),
                    _required(kwargs, "tid"),
                    _required_str(kwargs, "content"),
                )
            if capability == "qzone.reply":
                comment_id = kwargs.get("comment_id", kwargs.get("commentid"))
                if comment_id in (None, ""):
                    raise CapabilityError("qzone.reply 需要 comment_id")
                return await client.reply(
                    _required(kwargs, "uin"),
                    _required(kwargs, "tid"),
                    comment_id,
                    _required_str(kwargs, "content"),
                    comment_uin=kwargs.get("comment_uin"),
                    reply_uin=kwargs.get("reply_uin"),
                    comment_nickname=kwargs.get("comment_nickname"),
                )
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - 能力失败不能打断调用方
            raise CapabilityError(f"{capability} 失败：{exc}") from exc
        raise CapabilityNotSupported(self.name, capability)

    def _get_qzone_client(self) -> QzoneClient:
        if self._qzone is None:
            self._qzone = QzoneClient(self._fetch_qzone_cookie)
            self._owns_qzone = True
        return self._qzone

    async def _fetch_qzone_cookie(self, domain: str) -> str:
        """经 OneBot ``get_cookies`` 取协议端登录态 Cookie。"""
        try:
            payload = await self.call_api("get_cookies", {"domain": domain})
        except Exception as exc:  # noqa: BLE001 - 由 QzoneClient 汇总成失败原因
            raise RuntimeError(f"{domain}: {exc}") from exc
        data = payload.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, list):
            # 部分实现（get_cookies 的 format=array）返回 [{name, value}, ...]
            pairs = [
                f"{item['name']}={item.get('value', '')}"
                for item in data
                if isinstance(item, dict) and item.get("name")
            ]
            return "; ".join(pairs)
        if not isinstance(data, dict):
            data = payload
        for key in ("cookies", "cookie"):
            value = data.get(key)
            if value:
                return str(value)
        return ""

    async def _extension_api(self, capability: str, action: str, params: dict[str, Any]) -> Any:
        """扩展动作调用：把网络/平台错误收敛为能力失败原因。"""
        try:
            payload = await self.call_api(action, params)
        except Exception as exc:  # noqa: BLE001 - 能力失败不能打断调用方
            raise CapabilityError(f"{capability} 失败（{action}）：{exc}") from exc
        return payload.get("data", payload)

    async def _send_voice(self, kwargs: dict[str, Any]) -> Any:
        """message.voice：把音频字节落成临时文件，用 record 段发出去。

        OneBot 的 record 段 ``file`` 接受本地路径；临时文件在发送完成后删除
        （协议端可能异步读取，所以放到 finally 里删）。没有音频字节直接报错，
        由上层降级为发文字。
        """
        data = kwargs.get("audio") or kwargs.get("data") or b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            raise CapabilityError("message.voice 需要非空音频数据")
        chat_id = kwargs.get("chat_id")
        if chat_id in (None, ""):
            raise CapabilityError("message.voice 需要 chat_id")
        chat_type = str(kwargs.get("chat_type") or "group")
        # 扩展名按 MIME 推断（TTS 默认是 mp3）；协议端按扩展名识别音频格式
        mime = str(kwargs.get("mime") or kwargs.get("mime_type") or "")
        suffix = str(kwargs.get("suffix") or kwargs.get("format") or "").lstrip(".")
        if not suffix:
            suffix = (mimetypes.guess_extension(mime.split(";")[0].strip()) or ".amr").lstrip(".")
        fd, path = tempfile.mkstemp(prefix="rubenbot-voice-", suffix=f".{suffix}")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            action = "send_private_msg" if chat_type == "private" else "send_group_msg"
            key = "user_id" if chat_type == "private" else "group_id"
            payload = await self.call_api(
                action,
                {key: _to_id(str(chat_id)), "message": [{"type": "record", "data": {"file": path}}]},
            )
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - 能力失败不能打断调用方
            raise CapabilityError(f"message.voice 失败：{exc}") from exc
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 删不掉不致命
                pass
        return payload.get("data", payload) if isinstance(payload, dict) else payload


def _to_id(value: str | int) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _required(kwargs: dict[str, Any], key: str) -> Any:
    value = kwargs.get(key)
    if value in (None, ""):
        raise CapabilityError(f"缺少参数 {key}")
    return value


def _required_str(kwargs: dict[str, Any], key: str) -> str:
    value = str(_required(kwargs, key)).strip()
    if not value:
        raise CapabilityError(f"缺少参数 {key}")
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
    """被引用消息的 id：消息段数组里的 reply 段，或字符串形式里的 CQ:reply。"""
    if isinstance(message, list):
        for item in message:
            if isinstance(item, dict) and item.get("type") == "reply":
                rid = (item.get("data") or {}).get("id")
                if rid is not None:
                    return str(rid)
    elif isinstance(message, str):
        match = _CQ_CODE_RE.search(message)
        while match is not None:
            if match.group("type").lower() == "reply":
                params = {key: value for key, value in _CQ_PARAM_RE.findall(match.group("params") or "")}
                if params.get("id"):
                    return str(params["id"])
            match = _CQ_CODE_RE.search(message, match.end())
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


def parse_onebot_platform_event(
    data: dict[str, Any], *, self_id: str | None = None
) -> PlatformEvent | None:
    """OneBot v11 的 notice / request 事件 -> 统一 :class:`PlatformEvent`。

    只看 ``post_type`` 为 notice / request 的推送；meta_event（心跳）与
    message（已由 :func:`parse_onebot_event` 处理）一律返回 None。
    ``self_id`` 只用于判定「这件事是不是冲着 bot 来的」，自身引发的事件
    在核心入口统一过滤，这里不丢，便于审计。
    """
    post_type = str(data.get("post_type") or "")
    if post_type not in ("notice", "request"):
        return None
    effective_self = self_id or (str(data["self_id"]) if data.get("self_id") is not None else None)
    notice_type = str(data.get("notice_type") or "")
    request_type = str(data.get("request_type") or "")
    sub_type = str(data.get("sub_type") or "")
    group_id = data.get("group_id")
    user_id = data.get("user_id")
    target_id = data.get("target_id")
    operator_id = data.get("operator_id")

    kind = KIND_REQUEST if post_type == "request" else KIND_NOTICE
    event_type = request_type if kind == KIND_REQUEST else notice_type
    if not event_type:
        return None

    conversation_type = "group" if group_id not in (None, "") else "private"
    conversation_id = str(group_id) if conversation_type == "group" else str(user_id or "")
    actor_id = str(user_id) if user_id not in (None, "") else str(operator_id or "")
    actor_name = _onebot_name(data)

    # 戳一戳：target_id 是被戳的人；戳到 bot 才算「必判」
    targets_bot = False
    if kind == KIND_NOTICE and notice_type == "notify" and sub_type == "poke":
        targets_bot = effective_self is not None and str(target_id) == effective_self
    elif kind == KIND_NOTICE and notice_type == "poke":
        targets_bot = effective_self is not None and str(target_id or user_id) == effective_self

    description = _describe_platform_event(
        kind=kind,
        event_type=event_type,
        sub_type=sub_type,
        actor_name=actor_name,
        actor_id=actor_id,
        target_id=str(target_id) if target_id not in (None, "") else "",
        group_id=str(group_id) if group_id not in (None, "") else "",
        data=data,
        targets_bot=targets_bot,
    )

    return PlatformEvent(
        platform="snowluma",
        kind=kind,
        type=event_type,
        sub_type=sub_type,
        actor_id=actor_id,
        actor_name=actor_name,
        target_id=str(target_id) if target_id not in (None, "") else "",
        target_name="",
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        description=description,
        raw={
            key: data[key]
            for key in (
                "post_type",
                "notice_type",
                "request_type",
                "sub_type",
                "group_id",
                "user_id",
                "target_id",
                "operator_id",
                "message_id",
                "duration",
                "comment",
                "flag",
            )
            if data.get(key) is not None
        },
        ts=float(data["time"]) if isinstance(data.get("time"), (int, float)) else 0.0,
        platform_event_id=str(data.get("flag") or data.get("message_id") or ""),
        pending=kind == KIND_REQUEST,
        targets_bot=targets_bot,
    )


def _onebot_name(data: dict[str, Any]) -> str:
    """事件里尽量取一个可读的名字（不同实现字段不一）。"""
    for key in ("sender", "user"):
        value = data.get(key)
        if isinstance(value, dict):
            name = value.get("card") or value.get("nickname") or value.get("name")
            if name:
                return str(name)
    for key in ("nickname", "target_name"):
        if data.get(key):
            return str(data[key])
    return ""


# 事件类型的可读名（只影响描述文案，不认识的原样用类型名）
_NOTICE_LABELS = {
    "group_increase": "进群",
    "group_decrease": "退群/被踢",
    "group_ban": "禁言变动",
    "group_admin": "管理员变动",
    "group_recall": "撤回群消息",
    "friend_recall": "撤回私聊消息",
    "friend_add": "添加好友",
    "group_upload": "上传群文件",
    "group_card": "群名片变更",
    "notify": "通知",
}
_REQUEST_LABELS = {
    "friend": "好友申请",
    "group": "入群申请/邀请",
}


def _describe_platform_event(
    *,
    kind: str,
    event_type: str,
    sub_type: str,
    actor_name: str,
    actor_id: str,
    target_id: str,
    group_id: str,
    data: dict[str, Any],
    targets_bot: bool = False,
) -> str:
    """一句话描述：给模型看的事件描述，不含平台内部术语。"""
    who = actor_name or actor_id or "某人"
    if kind == KIND_REQUEST:
        label = _REQUEST_LABELS.get(event_type, f"{event_type} 申请")
        where = f"群 {group_id}" if group_id else "私聊"
        comment = str(data.get("comment") or "").strip()
        tail = f"：{comment}" if comment else ""
        return f"{who} 发来{label}（{where}）{tail}"
    if event_type == "notify" and sub_type == "poke":
        return f"{who} 戳了戳你" if targets_bot else f"{who} 戳了戳某人"
    label = _NOTICE_LABELS.get(event_type, event_type)
    if event_type == "group_increase":
        return f"{who} 加入了群 {group_id}"
    if event_type == "group_decrease":
        reason = "被踢出" if sub_type == "kick" else "主动退出"
        return f"{who} {reason}群 {group_id}"
    if event_type == "group_ban":
        duration = data.get("duration")
        if sub_type == "ban" and duration:
            return f"{who} 在群 {group_id} 被禁言 {int(duration)} 秒"
        return f"{who} 在群 {group_id} 被解除禁言"
    if event_type in ("group_recall", "friend_recall"):
        return f"{who} 撤回了一条消息（消息 id {data.get('message_id', '?')}）"
    if event_type == "group_admin":
        action = "成为管理员" if sub_type == "set" else "被取消管理员"
        return f"{who} 在群 {group_id} {action}"
    where = f"群 {group_id}" if group_id else "私聊"
    return f"{who} 触发了{label}（{where}）"


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

    # 注意：``reply_id`` 是这条消息自身的 id（变量名沿用旧实现）；被引用的
    # 目标 id 在 ``reply_to`` 里，两个都要带出去。
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
        platform_msg_id=reply_id,
    )
