"""核心表情包行为：收集群聊表情，按话题选图用于独立回复或文字后补发。"""

from __future__ import annotations

import base64
import logging
import random
import time
from collections.abc import Mapping
from typing import Any, Callable

from .adapters.base import SEND_STICKER
from .memory.retrieve import tokenize

logger = logging.getLogger(__name__)

COLLECTABLE_SEGMENTS = frozenset({"sticker", "image"})
MAX_TAGS = 8


class StickerBehavior:
    """表情包收集与发送决策；随机源、时钟和库均可注入测试。"""

    def __init__(
        self,
        config: Any,
        *,
        store_getter: Callable[[], Any] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self._store_getter = store_getter
        self._store = None
        self.rng = rng or random.Random()
        self.clock = clock or time.monotonic
        self._last_sent_at: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return getattr(self.config, "enabled", False) is True

    @property
    def store(self) -> Any:
        if self._store is None and self._store_getter is not None:
            self._store = self._store_getter()
        return self._store

    async def on_message(self, runtime: Any, event: Any) -> None:
        """在 core 的消息预处理时机收集群聊里的表情/图片段。"""
        try:
            if not self.enabled or not getattr(self.config, "collect", False) or not getattr(event, "is_group", False):
                return
            store = self.store
            if store is None:
                return
            platform = str(getattr(event, "platform", "") or "")
            tags = topic_tags(getattr(event, "text", "") or "")
            for segment in getattr(event, "segments", None) or []:
                self._collect_segment(store, platform, segment, tags)
        except Exception as exc:  # noqa: BLE001
            logger.debug("表情包收集异常：%s", exc)

    def _collect_segment(self, store: Any, platform: str, segment: Any, tags: list[str]) -> None:
        kind = str(getattr(segment, "type", "") or (segment.get("type") if isinstance(segment, Mapping) else ""))
        if kind not in COLLECTABLE_SEGMENTS:
            return
        data = getattr(segment, "data", None)
        if not isinstance(data, Mapping):
            data = segment.get("data") if isinstance(segment, Mapping) else None
        if not isinstance(data, Mapping) or not data:
            return
        payload = {str(key): value for key, value in data.items() if value not in (None, "")}
        if not payload:
            return
        raw = _decode_base64(payload.get("base64"))
        if kind == "sticker" and isinstance(payload.get("native_ref"), Mapping):
            # 适配器已经把平台原生段放进 native_ref；存储层直接保留这份
            # 约定结构，发送侧才能按平台还原原生段。
            native_ref = payload["native_ref"]
        else:
            native_ref = {"kind": kind, "data": payload}
        refs = {platform: native_ref} if platform else None
        if raw is None and not refs:
            return
        store.add(raw, tags=tags, native_refs=refs, file_name=payload.get("filename") or payload.get("name") or None, mime_type=payload.get("mime_type") or payload.get("content_type") or None)

    async def on_after_reply(self, runtime: Any, event: Any, reply: str) -> None:
        """在 core 发送完回复后按概率/冷却补一张表情。"""
        await self.try_send(runtime, event, probability=self.config.send_probability)

    async def try_send(self, runtime: Any, event: Any, *, probability: float) -> bool:
        """独立回复与文字后补发共用选图、冷却和发送；只有能力调用成功才算已发。"""
        try:
            if not self.enabled or self.rng.random() >= probability:
                return False
            now = self.clock()
            if self._in_cooldown(event, now):
                return False
            bot = _resolve_bot(runtime)
            platform = event.platform
            if bot is None or not _can_send(bot, platform):
                return False
            store = self.store
            if store is None:
                return False
            sticker = self._pick(store, event.text or "")
            if sticker is None:
                return False
            # 冷却按尝试计，避免平台故障时每轮重试；它不是发言额度。
            self._last_sent_at[_chat_key(event)] = now
            ok, reason = await bot.call_capability(
                platform, SEND_STICKER, sticker=store.reference(sticker),
                chat_id=event.chat_id, chat_type=event.chat_type,
            )
            if not ok:
                logger.debug("发表情失败（忽略）：%s", reason)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.debug("表情包发送异常：%s", exc)
            return False

    def _in_cooldown(self, event: Any, now: float) -> bool:
        last = self._last_sent_at.get(_chat_key(event))
        return last is not None and now - last < getattr(self.config, "cooldown_seconds", 300.0)

    def _pick(self, store: Any, text: str) -> Any:
        tags = topic_tags(text)
        candidates = store.find_by_tags(tags) if tags else []
        if not candidates:
            candidates = store.find_by_tags(None)
        return self.rng.choice(candidates) if candidates else None


def topic_tags(text: str) -> list[str]:
    """从正文抽取最多八个话题标签。"""
    try:
        tokens = tokenize(text or "")
    except Exception:  # noqa: BLE001
        return []
    tags: list[str] = []
    for token in tokens:
        token = token.strip().lower()
        if len(token) < 2 or token in tags:
            continue
        tags.append(token)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _decode_base64(value: Any) -> bytes | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("base64://"):
        text = text[len("base64://") :]
    elif text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        data = base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return data or None


def _resolve_bot(runtime: Any) -> Any:
    extras = getattr(runtime, "extras", None)
    if isinstance(extras, dict):
        bot = extras.get("bot")
        if bot is not None and hasattr(bot, "call_capability"):
            return bot
    return runtime if hasattr(runtime, "call_capability") else None


def _can_send(bot: Any, platform: str) -> bool:
    # chat_id 只在来源平台上有意义，不能让能力层改走别的在线适配器。
    adapter = bot.adapters.get(platform)
    if adapter is None or SEND_STICKER not in adapter.capabilities:
        return False
    key = adapter.capability_config_keys.get(SEND_STICKER)
    if not key:
        return True
    default = adapter.capability_defaults.get(SEND_STICKER, True)
    return bot.config.adapter_capability_enabled(platform, key, default=default)


def _chat_key(event: Any) -> tuple[str, str]:
    return str(getattr(event, "platform", "")), str(getattr(event, "chat_id", ""))


__all__ = ["StickerBehavior", "topic_tags"]
