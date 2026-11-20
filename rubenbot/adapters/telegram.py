"""Telegram Bot API 适配器。

设计取向（与 SnowLuma 适配器对齐）：

- 用 **长轮询** ``getUpdates`` 收消息，部署时不需要公网回调地址
- ``offset`` 落盘保存（默认 ``data/telegram_offset.json``），重启后不会重复处理旧消息
- 入站统一转成 :class:`MessageEvent`：私聊 / 群组 / 超级群，``text``、``photo``
  （取最大尺寸，``getFile`` 下载后转成带 base64 的 image 段）、``voice``
  （下载后转成带 base64 的 record 段，供转写）、``caption``
- 出站 ``sendMessage`` / ``sendPhoto`` / ``sendVoice``，图片段支持 ``base64``
  与 ``url`` 两种来源，语音段只支持 base64 上传
- 声明 ``message.voice`` 能力：语音回复经它发 ``sendVoice``

``${ENV_VAR}`` 的展开由 :mod:`rubenbot.config` 统一处理，这里只接收展开后的 token。
配置段 ``adapters.telegram`` 由 :meth:`rubenbot.config.Config.telegram_config`
解析，接线方式::

    tg = config.telegram_config()
    TelegramAdapter(
        tg.bot_token,
        poll_timeout=tg.poll_timeout,
        allowed_chat_ids=tg.allowed_chat_ids,
        offset_file=tg.offset_file or None,
    )
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .base import (
    SEND_STICKER,
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    EventCallback,
    MessageEvent,
    Segment,
    as_segment,
    render_segments,
    sticker_native_ref,
)

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_OFFSET_FILE = "data/telegram_offset.json"

# getFile 下载的单条语音上限（与 STT 的单条上限对齐；超大文件不下）
MAX_VOICE_BYTES = 20 * 1024 * 1024

# 轮询失败后的重试退避（秒）；测试会 monkeypatch 调小
POLL_BACKOFF_MIN = 1.0
POLL_BACKOFF_MAX = 60.0

# Telegram 单条文本 / 图片说明的字数上限
MAX_TEXT_CHARS = 4096
MAX_CAPTION_CHARS = 1024

# getFile 单张图片的下载上限，避免把异常大文件整个塞进内存
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# 只有这些会话类型有“对话”语义；频道（channel）不参与
_CHAT_TYPES = {"private": "private", "group": "group", "supergroup": "group"}


class TelegramAdapter(BaseAdapter):
    """Telegram Bot API 长轮询适配器。"""

    name = "telegram"

    # 语音回复：sendVoice；表情：sendSticker（file_id 直达，没有就上传图片）
    capabilities: frozenset[str] = frozenset({"message.voice", SEND_STICKER})
    capability_config_keys = {"message.voice": "voice", SEND_STICKER: "sticker"}

    def __init__(
        self,
        bot_token: str = "",
        *,
        poll_timeout: float = 30.0,
        allowed_chat_ids: Iterable[str | int] | None = None,
        api_base: str = DEFAULT_API_BASE,
        offset_file: str | Path | None = DEFAULT_OFFSET_FILE,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.bot_token = str(bot_token or "").strip()
        self.poll_timeout = max(0.0, float(poll_timeout))
        # 空集合表示不限制；统一转字符串，便于和事件里的 chat_id 比较
        if isinstance(allowed_chat_ids, (str, int)):
            allowed_chat_ids = [allowed_chat_ids]
        self.allowed_chat_ids = {str(item) for item in (allowed_chat_ids or []) if str(item) != ""}
        self.api_base = api_base.rstrip("/")
        self.offset_file = Path(offset_file) if offset_file else None
        self._client = http_client
        self._owns_client = http_client is None
        self._stop = asyncio.Event()
        self._on_event: EventCallback | None = None
        # 下一条要确认的 update_id；None 表示还没有（首次启动从服务端待处理消息开始）
        self._offset: int | None = None
        self.bot_id: str | None = None
        self.bot_username: str | None = None
        self._me_ready = False
        # 统计信息，便于排查
        self.poll_error_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, on_event: EventCallback) -> None:
        """长轮询直到 stop；轮询异常内部退避重试，不把异常抛给主循环。"""
        self._on_event = on_event
        self._stop.clear()
        if not self.bot_token:
            # 配置错误重试再多次也没用：记日志后正常返回，主循环不再重启本适配器
            logger.error("Telegram 适配器未启动：缺少 bot_token（adapters.telegram.bot_token）")
            return
        self._load_offset()
        delay = POLL_BACKOFF_MIN
        while not self._stop.is_set():
            try:
                if not self._me_ready:
                    await self._fetch_me()
                updates = await self._poll_updates()
                if updates is None:  # 收到停止信号
                    break
                delay = POLL_BACKOFF_MIN
                if updates:
                    await self._handle_updates(updates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/平台错误按退避重试
                if self._stop.is_set():
                    break
                self.poll_error_count += 1
                logger.warning("Telegram 轮询异常：%s（%.0fs 后重试）", exc, delay)
                await self._sleep_or_stop(delay)
                delay = min(delay * 2, POLL_BACKOFF_MAX)

    async def stop(self) -> None:
        """请求退出并释放自建的 httpx 客户端；可重复调用。"""
        self._stop.set()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        self._me_ready = False

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # Bot API 调用
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = httpx.Timeout(
                connect=10.0,
                read=self.poll_timeout + 10.0,
                write=30.0,
                pool=10.0,
            )
            self._client = httpx.AsyncClient(base_url=self.api_base, timeout=timeout)
            self._owns_client = True
        return self._client

    def _api_path(self, method: str) -> str:
        return f"/bot{self.bot_token}/{method}"

    async def _call(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        """调用一个 Bot API 方法，返回 ``result`` 字段；失败抛 RuntimeError。"""
        client = await self._get_client()
        if files is not None:
            # multipart 表单的值必须是字符串/字节，数字 id 先转字符串
            form = {key: str(value) for key, value in (payload or {}).items()}
            resp = await client.post(self._api_path(method), data=form, files=files)
        else:
            resp = await client.post(self._api_path(method), json=dict(payload or {}))
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"Telegram API {method} 返回了非 JSON 响应")
        if isinstance(data, dict) and data.get("ok"):
            return data.get("result")
        if isinstance(data, dict) and data.get("description"):
            raise RuntimeError(
                f"Telegram API {method} 失败：{data.get('description')}"
                f"（code {data.get('error_code')}）"
            )
        resp.raise_for_status()
        raise RuntimeError(f"Telegram API {method} 失败：{data!r}")

    async def _fetch_me(self) -> None:
        """getMe：拿 bot 自己的 id / username，用于识别 @ 与“回复 bot”。"""
        result = await self._call("getMe")
        if not isinstance(result, dict) or result.get("id") is None:
            raise RuntimeError("Telegram getMe 返回异常")
        self.bot_id = str(result["id"])
        self.bot_username = str(result.get("username") or "") or None
        self._me_ready = True
        logger.info("Telegram bot 已就绪：@%s（id=%s）", self.bot_username or "?", self.bot_id)

    async def _poll_updates(self) -> list[dict[str, Any]] | None:
        """发一次 getUpdates；收到停止信号时取消挂起请求并返回 None。"""
        params: dict[str, Any] = {
            # Bot API 的 timeout 是整数秒，长轮询上限 50
            "timeout": int(min(self.poll_timeout, 50)),
            "allowed_updates": ["message"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        call = asyncio.create_task(self._call("getUpdates", params))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            done, _ = await asyncio.wait({call, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                result = call.result()
                return result if isinstance(result, list) else []
            # 停止信号：丢弃还在等待的长轮询
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            return None
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)

    # ------------------------------------------------------------------
    # offset 持久化
    # ------------------------------------------------------------------

    def _load_offset(self) -> None:
        if self.offset_file is None:
            return
        try:
            data = json.loads(self.offset_file.read_text(encoding="utf-8"))
            value = int(data.get("offset")) if isinstance(data, dict) else None
            if value is not None and value >= 0:
                self._offset = value
                logger.info("Telegram offset 已恢复：%s", value)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - 坏文件不影响轮询
            logger.warning("读取 Telegram offset 失败（忽略）：%s", exc)

    def _save_offset(self) -> None:
        if self.offset_file is None or self._offset is None:
            return
        try:
            self.offset_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.offset_file.with_name(self.offset_file.name + ".tmp")
            tmp.write_text(json.dumps({"offset": self._offset}), encoding="utf-8")
            os.replace(tmp, self.offset_file)
        except Exception as exc:  # noqa: BLE001 - 落盘失败只记录，不影响主循环
            logger.warning("保存 Telegram offset 失败：%s", exc)

    # ------------------------------------------------------------------
    # 入站：update -> MessageEvent
    # ------------------------------------------------------------------

    async def _handle_updates(self, updates: list[Any]) -> None:
        """逐条处理一批 update；单条失败只记录日志，不影响后续。

        offset 在整批处理完后落盘：即使某条解析/回调失败，也推进 offset，
        避免同一条坏消息反复卡住主循环。
        """
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
                self._offset = next_offset if self._offset is None else max(self._offset, next_offset)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            if self.allowed_chat_ids:
                chat = message.get("chat")
                chat = chat if isinstance(chat, Mapping) else {}
                if str(chat.get("id")) not in self.allowed_chat_ids:
                    continue
            try:
                event = await self.parse_update(update)
            except Exception as exc:  # noqa: BLE001 - 坏 update 不能打死循环
                logger.warning("解析 Telegram update 失败：%s", exc)
                continue
            if event is None or self._on_event is None:
                continue
            try:
                await self._on_event(event)
            except Exception as exc:  # noqa: BLE001 - 事件回调失败也不能打死循环
                logger.warning("处理 Telegram 事件失败：%s", exc)
        self._save_offset()

    async def parse_update(self, update: Mapping[str, Any]) -> MessageEvent | None:
        """单条 getUpdates update -> 统一事件；非消息 / 不支持的会话返回 None。"""
        message = update.get("message") if isinstance(update, Mapping) else None
        if not isinstance(message, Mapping):
            return None
        return await self._parse_message(message)

    async def _parse_message(self, message: Mapping[str, Any]) -> MessageEvent | None:
        chat = message.get("chat")
        chat = chat if isinstance(chat, Mapping) else {}
        chat_id = chat.get("id")
        chat_type = _CHAT_TYPES.get(str(chat.get("type") or ""))
        if chat_id is None or chat_type is None:
            return None
        sender = message.get("from")
        sender = sender if isinstance(sender, Mapping) else {}
        sender_id = sender.get("id")
        if sender_id is None:
            return None
        # bot 自己的消息（含其他 bot）一律不回显，避免自问自答
        if sender.get("is_bot"):
            return None
        if self.bot_id is not None and str(sender_id) == self.bot_id:
            return None

        segments: list[Segment] = []
        sticker = message.get("sticker")
        if isinstance(sticker, Mapping):
            segment = await self._sticker_segment(sticker)
            if segment is not None:
                segments.append(segment)
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            image = await self._photo_segment(_largest_photo(photo))
            if image is not None:
                segments.append(image)
        voice = message.get("voice")
        if isinstance(voice, Mapping):
            segment = await self._voice_segment(voice)
            if segment is not None:
                segments.append(segment)
        text = message.get("text")
        caption = message.get("caption")
        if isinstance(text, str) and text:
            segments.append(Segment.text(text))
        elif isinstance(caption, str) and caption:
            segments.append(Segment.text(caption))
        if not segments:
            # 文件等本轮不支持的形态：跳过，避免空消息进上下文
            return None

        reply_to = message.get("reply_to_message")
        reply_to = reply_to if isinstance(reply_to, Mapping) else {}
        reply_to_id = reply_to.get("message_id")
        reply_from = reply_to.get("from")
        reply_to_bot = bool(self.bot_id) and isinstance(reply_from, Mapping) and (
            str(reply_from.get("id")) == self.bot_id
        )

        return MessageEvent(
            platform=self.name,
            chat_type=chat_type,
            chat_id=str(chat_id),
            sender_id=str(sender_id),
            sender_name=_sender_name(sender),
            chat_name=str(chat.get("title") or ""),
            text=render_segments(segments),
            reply_to_id=str(reply_to_id) if reply_to_id is not None else None,
            mentions_bot=self._mentions_bot(message) or reply_to_bot,
            raw={
                **dict(message),
                "reply_to_bot": reply_to_bot,
                "telegram_chat_type": str(chat.get("type") or ""),
            },
            segments=segments,
        )

    def _mentions_bot(self, message: Mapping[str, Any]) -> bool:
        """文本 / 图片说明的 entities 里是否 @ 到 bot，或 text_mention 指向 bot。"""
        text = message.get("text")
        entities = message.get("entities")
        if not isinstance(text, str):
            text = message.get("caption")
            entities = message.get("caption_entities")
        if not isinstance(text, str) or not isinstance(entities, list):
            return False
        for entity in entities:
            if not isinstance(entity, Mapping):
                continue
            kind = entity.get("type")
            if kind == "text_mention":
                user = entity.get("user")
                if self.bot_id and isinstance(user, Mapping) and str(user.get("id")) == self.bot_id:
                    return True
            elif kind == "mention" and self.bot_username:
                fragment = _entity_text(text, entity.get("offset"), entity.get("length"))
                if fragment and fragment.lstrip("@").lower() == self.bot_username.lower():
                    return True
        return False

    async def _sticker_segment(self, sticker: Mapping[str, Any]) -> Segment | None:
        """Telegram 原生 sticker -> 统一 sticker 段，并保留平台引用。"""
        file_id = sticker.get("file_id")
        if not file_id:
            return None
        data: dict[str, Any] = {
            "native_ref": {"kind": "sticker", "file_id": str(file_id)},
        }
        for key in ("emoji", "set_name", "width", "height", "file_size"):
            if sticker.get(key) is not None:
                data[key] = sticker[key]
        return Segment("sticker", data)

    async def _photo_segment(self, photo: Mapping[str, Any]) -> Segment | None:
        """最大尺寸的 PhotoSize -> image 段（下载后以 base64 保存）。"""
        file_id = photo.get("file_id")
        data: dict[str, Any] = {}
        if file_id:
            data["file_id"] = str(file_id)
        for key in ("width", "height", "file_size"):
            if photo.get(key) is not None:
                data[key] = photo[key]
        if file_id:
            try:
                result = await self._call("getFile", {"file_id": str(file_id)})
                file_path = result.get("file_path") if isinstance(result, Mapping) else None
                if file_path:
                    raw = await self._download_file(str(file_path))
                    data["base64"] = base64.b64encode(raw).decode("ascii")
                    data["mime_type"] = mimetypes.guess_type(str(file_path))[0] or "image/jpeg"
            except Exception as exc:  # noqa: BLE001 - 下载失败仍保留图片段占位
                logger.warning("下载 Telegram 图片失败（file_id=%s）：%s", file_id, exc)
        if not data:
            return None
        return Segment("image", data)

    async def _download_file(self, file_path: str) -> bytes:
        client = await self._get_client()
        url = f"{self.api_base}/file/bot{self.bot_token}/{file_path.lstrip('/')}"
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content
        if len(content) > MAX_PHOTO_BYTES:
            raise ValueError(f"图片过大（{len(content)} 字节）")
        return content

    async def _voice_segment(self, voice: Mapping[str, Any]) -> Segment | None:
        """语音条 -> record 段；下载失败时只留 file_id/duration 占位。

        下载后以 base64 放进段里：转写层不再需要第二次请求，也不会因为
        Telegram 的临时文件地址过期而取不到音频。
        """
        file_id = voice.get("file_id")
        data: dict[str, Any] = {}
        if file_id:
            data["file_id"] = str(file_id)
        for key in ("duration", "mime_type", "file_size"):
            if voice.get(key) is not None:
                data[key] = voice[key]
        if file_id:
            try:
                result = await self._call("getFile", {"file_id": str(file_id)})
                file_path = result.get("file_path") if isinstance(result, Mapping) else None
                if file_path:
                    raw = await self._download_voice(str(file_path))
                    data["base64"] = base64.b64encode(raw).decode("ascii")
                    data["filename"] = str(file_path).rsplit("/", 1)[-1]
                    data["mime_type"] = (
                        str(data.get("mime_type") or "")
                        or mimetypes.guess_type(str(file_path))[0]
                        or "audio/ogg"
                    )
            except Exception as exc:  # noqa: BLE001 - 下载失败仍保留语音段占位
                logger.warning("下载 Telegram 语音失败（file_id=%s）：%s", file_id, exc)
        if not data:
            return None
        return Segment("record", data)

    async def _download_voice(self, file_path: str) -> bytes:
        client = await self._get_client()
        url = f"{self.api_base}/file/bot{self.bot_token}/{file_path.lstrip('/')}"
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content
        if len(content) > MAX_VOICE_BYTES:
            raise ValueError(f"语音过大（{len(content)} 字节）")
        return content

    # ------------------------------------------------------------------
    # 出站：统一 segments -> sendMessage / sendPhoto
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> None:
        """按顺序发送消息段：连续文本合成一条，图片用 sendPhoto（可带说明）。"""
        reply_id: str | None = None
        actions: list[tuple[str, Any, str]] = []  # (kind, payload, caption)
        buffer = ""
        for raw_segment in segments:
            segment = as_segment(raw_segment)
            if segment.type == "reply":
                value = segment.data.get("id")
                reply_id = str(value) if value not in (None, "") else None
            elif segment.type == "image":
                actions.append(("image", segment, buffer))
                buffer = ""
            elif segment.type == "record":
                # 语音单独成一条（sendVoice 只带 caption），前面的文本并入说明
                actions.append(("record", segment, buffer))
                buffer = ""
            else:
                # Telegram 没有对应形态的段（表情等）退化成占位文本
                buffer += segment.render()
        if buffer:
            actions.append(("text", buffer, ""))

        first = True
        for kind, payload, caption in actions:
            if kind == "text":
                text = str(payload)
                if not text.strip():
                    continue
                await self._send_message(
                    chat_id,
                    _clip(text, MAX_TEXT_CHARS),
                    reply_to=reply_id if first else None,
                )
                first = False
                continue
            if kind == "record":
                # 语音段：有音频就 sendVoice，没有可用来源时退化成占位文本
                source = _voice_source(payload.data)
                reply_to = reply_id if first else None
                first = False
                if source is None:
                    text = caption + payload.render()
                    await self._send_message(chat_id, _clip(text, MAX_TEXT_CHARS), reply_to=reply_to)
                    continue
                raw, mime = source
                if caption and len(caption) > MAX_CAPTION_CHARS:
                    # 说明太长：先发语音，再单独补一条文本
                    await self._send_voice(chat_id, raw, mime, "", reply_to=reply_to)
                    await self._send_message(
                        chat_id, _clip(caption, MAX_TEXT_CHARS), reply_to=None
                    )
                else:
                    await self._send_voice(chat_id, raw, mime, caption, reply_to=reply_to)
                continue
            # 图片段：没有可用来源时退化成文本，保证内容不丢
            source = _image_source(payload.data)
            if source is None:
                text = caption + payload.render()
                await self._send_message(
                    chat_id,
                    _clip(text, MAX_TEXT_CHARS),
                    reply_to=reply_id if first else None,
                )
                first = False
                continue
            reply_to = reply_id if first else None
            first = False
            if caption and len(caption) > MAX_CAPTION_CHARS:
                # 说明太长：先发图，再单独补一条文本
                await self._send_photo(chat_id, source, "", reply_to=reply_to)
                await self._send_message(
                    chat_id,
                    _clip(caption, MAX_TEXT_CHARS),
                    reply_to=None,
                )
            else:
                await self._send_photo(chat_id, source, caption, reply_to=reply_to)

    async def _send_message(self, chat_id: str, text: str, *, reply_to: str | None) -> None:
        payload: dict[str, Any] = {"chat_id": _to_id(chat_id), "text": text}
        _attach_reply(payload, reply_to)
        await self._call("sendMessage", payload)

    async def _send_photo(
        self,
        chat_id: str,
        source: tuple[str, Any],
        caption: str,
        *,
        reply_to: str | None,
    ) -> None:
        """发送图片；``source`` 为 ("url", url) 或 ("upload", (bytes, mime))。"""
        kind, value = source
        payload: dict[str, Any] = {"chat_id": _to_id(chat_id)}
        if caption:
            payload["caption"] = caption
        _attach_reply(payload, reply_to)
        if kind == "upload":
            raw, mime = value
            filename = "image" + (mimetypes.guess_extension(mime or "") or ".jpg")
            await self._call("sendPhoto", payload, files={"photo": (filename, raw, mime)})
        else:
            payload["photo"] = str(value)
            await self._call("sendPhoto", payload)

    async def send_file(self, chat_id: str, filename: str, content: bytes, *, chat_type: str) -> None:
        await self._call("sendDocument", {"chat_id": _to_id(chat_id)},
                         files={"document": (filename, content, "application/octet-stream")})

    async def _send_voice(
        self,
        chat_id: str,
        raw: bytes,
        mime: str,
        caption: str,
        *,
        reply_to: str | None,
    ) -> Any:
        """发送语音：按平台规则 OGG/OPUS 用 sendVoice，其余用 sendAudio。

        返回 Bot API 的 ``result``（里面带 message_id），便于调用方落流水。
        """
        payload: dict[str, Any] = {"chat_id": _to_id(chat_id)}
        if caption:
            payload["caption"] = caption
        _attach_reply(payload, reply_to)
        suffix = mimetypes.guess_extension(mime or "") or ".ogg"
        is_voice = (mime or "").startswith("audio/ogg")
        method = "sendVoice" if is_voice else "sendAudio"
        # 两个方法的文件字段名不同：sendVoice 用 voice，sendAudio 用 audio
        files = {("voice" if is_voice else "audio"): ("voice" + suffix, raw, mime or "audio/ogg")}
        return await self._call(method, payload, files=files)

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        """平台能力：message.voice 发语音、send_sticker 发表情；其余不支持。"""
        if name == SEND_STICKER:
            return await self._send_sticker(kwargs)
        if name != "message.voice":
            raise CapabilityNotSupported(self.name, name)
        data = kwargs.get("audio") or kwargs.get("data") or b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            raise CapabilityError("message.voice 需要非空音频数据")
        chat_id = kwargs.get("chat_id")
        if chat_id in (None, ""):
            raise CapabilityError("message.voice 需要 chat_id")
        mime = str(kwargs.get("mime") or kwargs.get("mime_type") or "audio/mpeg")
        caption = str(kwargs.get("text") or kwargs.get("caption") or "")
        try:
            result = await self._send_voice(
                str(chat_id),
                data,
                mime,
                caption[:MAX_CAPTION_CHARS],
                reply_to=None,
            )
        except Exception as exc:  # noqa: BLE001 - 能力失败收敛成统一异常
            raise CapabilityError(f"message.voice 失败：{exc}") from exc
        return result

    async def _send_sticker(self, kwargs: dict[str, Any]) -> Any:
        """send_sticker：有 file_id 走原生 sendSticker，没有就上传图片当表情发。

        返回值统一成 ``{"message_id": ..., "file_id": ...}``（能力层据此回填
        表情库）；``file_id`` 优先取平台返回的，平台没给时用请求里那个。
        图片上传只接受本地文件 / base64 / URL——都没有就报错，由上层降级。
        """
        chat_id = kwargs.get("chat_id")
        if chat_id in (None, ""):
            raise CapabilityError("send_sticker 需要 chat_id")
        sticker = kwargs.get("sticker")
        native = sticker_native_ref(sticker, self.name)
        file_id = _native_file_id(native)
        payload: dict[str, Any] = {"chat_id": _to_id(chat_id)}
        try:
            if file_id:
                payload["sticker"] = file_id
                result = await self._call("sendSticker", payload)
                return _sticker_result(result, _result_file_id(result) or file_id)
            source = _sticker_upload(sticker)
            if source is None:
                url = _sticker_url(sticker)
                if not url:
                    raise CapabilityError("send_sticker 缺少可用的表情引用或图片文件")
                # URL 也交给 Bot API 自己拉取；返回的 file_id 同样能回填
                payload["sticker"] = url
                result = await self._call("sendSticker", payload)
                return _sticker_result(result, _result_file_id(result) or "")
            raw, mime = source
            filename = "sticker" + (mimetypes.guess_extension(mime or "") or ".webp")
            result = await self._call(
                "sendSticker", payload, files={"sticker": (filename, raw, mime or "image/webp")}
            )
            return _sticker_result(result, _result_file_id(result) or "")
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - 能力失败收敛成统一异常
            raise CapabilityError(f"send_sticker 失败：{exc}") from exc


# ----------------------------------------------------------------------
# 纯函数辅助
# ----------------------------------------------------------------------


def _sender_name(sender: Mapping[str, Any]) -> str:
    """群昵称概念在 Telegram 不存在：first_name + last_name，退而用 username。"""
    name = " ".join(
        str(part) for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()
    if name:
        return name
    if sender.get("username"):
        return str(sender["username"])
    return str(sender.get("id") or "")


def _largest_photo(photos: list[Any]) -> Mapping[str, Any]:
    candidates = [item for item in photos if isinstance(item, Mapping)]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            int(item.get("width") or 0) * int(item.get("height") or 0),
            int(item.get("file_size") or 0),
        ),
    )


def _entity_text(text: str, offset: Any, length: Any) -> str:
    """按 Telegram 的 UTF-16 码元偏移切出实体文本（中文/emoji 前也准确）。"""
    if not isinstance(offset, int) or not isinstance(length, int):
        return ""
    if offset < 0 or length <= 0:
        return ""
    raw = text.encode("utf-16-le")
    start, end = offset * 2, (offset + length) * 2
    if start >= len(raw):
        return ""
    return raw[start:end].decode("utf-16-le", errors="ignore")


def _image_source(data: Mapping[str, Any]) -> tuple[str, Any] | None:
    """图片段来源归一：base64 优先（上传），其次 url（交给 Telegram 下载）。

    返回 ``("upload", (bytes, mime))`` 或 ``("url", url)``；没有可用来源返回 None。
    """
    raw = data.get("base64")
    if raw:
        value = str(raw)
        mime = str(data.get("mime_type") or "image/jpeg")
        if value.startswith("data:"):
            header, _, encoded = value.partition(",")
            mime = header[5:].split(";")[0] or mime
        else:
            encoded = value
        try:
            return ("upload", (base64.b64decode(encoded, validate=False), mime))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"图片段 base64 无效：{exc}") from exc
    url = data.get("url")
    if url:
        return ("url", str(url))
    return None


def _voice_source(data: Mapping[str, Any]) -> tuple[bytes, str] | None:
    """语音段的可用来源：base64 音频（``(bytes, mime)``）；没有就返回 None。

    语音只支持上传：``sendVoice`` 不接受随意 URL，TTS 的产物也总是字节。
    """
    raw = data.get("base64")
    if raw:
        value = str(raw)
        if value.startswith("data:"):
            header, _, encoded = value.partition(",")
            mime = header[5:].split(";")[0]
        else:
            encoded = value
            mime = str(data.get("mime_type") or "audio/ogg")
        try:
            return base64.b64decode(encoded, validate=False), mime or "audio/ogg"
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"语音段 base64 无效：{exc}") from exc
    return None


def _to_id(value: str | int) -> int | str:
    """能转 int 的 id 转 int（Bot API 更偏好数字），频道用户名等保持字符串。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _attach_reply(payload: dict[str, Any], reply_to: str | None) -> None:
    if reply_to is not None:
        payload["reply_to_message_id"] = _to_id(reply_to)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    logger.warning("Telegram 消息超过 %s 字，已截断（原长 %s）", limit, len(text))
    return text[: limit - 3] + "..."


# ----------------------------------------------------------------------
# 表情（send_sticker）
# ----------------------------------------------------------------------


def _native_file_id(native: Any) -> str:
    """平台原生引用 -> file_id；接受裸字符串或含 ``file_id`` 的结构。"""
    if isinstance(native, str):
        return native.strip()
    if isinstance(native, Mapping):
        value = native.get("file_id")
        if not value and isinstance(native.get("data"), Mapping):
            value = native["data"].get("file_id")
        if value:
            return str(value)
    return ""


def _sticker_payload(sticker: Any) -> Mapping[str, Any]:
    """表情引用 -> 可直接读的映射（段 / 普通 dict 都行）。"""
    if isinstance(sticker, Mapping):
        return sticker
    data = getattr(sticker, "data", None)
    return data if isinstance(data, Mapping) else {}


def _sticker_upload(sticker: Any) -> tuple[bytes, str] | None:
    """表情引用的图片本体：本地文件或 base64 → ``(bytes, mime)``；没有返回 None。"""
    data = _sticker_payload(sticker)
    raw = data.get("base64")
    if raw:
        value = str(raw)
        mime = str(data.get("mime_type") or "image/webp")
        if value.startswith("data:"):
            header, _, encoded = value.partition(",")
            mime = header[5:].split(";")[0] or mime
        else:
            encoded = value
        try:
            return base64.b64decode(encoded, validate=False), mime
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"表情 base64 无效：{exc}") from exc
    path = data.get("file")
    if path:
        file = Path(str(path))
        try:
            if file.is_file():
                mime = str(data.get("mime_type") or mimetypes.guess_type(file.name)[0] or "image/webp")
                return file.read_bytes(), mime
        except OSError:  # pragma: no cover - 读不到按没有处理
            return None
    return None


def _sticker_url(sticker: Any) -> str:
    """表情引用里的 URL（Telegram 会自己下载）；没有返回空串。"""
    value = _sticker_payload(sticker).get("url")
    return str(value) if value else ""


def _result_file_id(result: Any) -> str:
    """Bot API 返回的 Message 里取表情的 file_id（可能没有）。"""
    if not isinstance(result, Mapping):
        return ""
    sticker = result.get("sticker")
    if isinstance(sticker, Mapping) and sticker.get("file_id"):
        return str(sticker["file_id"])
    return ""


def _sticker_result(result: Any, file_id: str) -> dict[str, Any]:
    """统一能力返回值：``{"message_id": ..., "file_id": ...}``（缺项就不放）。"""
    payload: dict[str, Any] = {}
    if isinstance(result, Mapping) and result.get("message_id") is not None:
        payload["message_id"] = result["message_id"]
    if file_id:
        payload["file_id"] = file_id
    return payload
