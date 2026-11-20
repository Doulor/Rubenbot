"""sticker 插件：收集群里的表情入库，在对的时机回一张。

两块功能，互不耦合：

1. **收集**（``on_message``）：群聊里出现表情 / 图片段时，把图（有字节就用
   字节，没有就用平台侧引用）连同当前话题标签并进表情库；同一张图重复出现
   由表情库按内容哈希去重，不会重复入库。收集受 ``collect`` 开关控制，
   **默认关**：表情是别人的东西，主动收要先显式打开。
2. **发送**（``on_after_reply``）：bot 完成一轮回复后，按 ``send_probability``
   掷骰、按 ``cooldown_seconds`` 做会话冷却，决定要不要补一张表情。选图优先
   与当前话题标签沾边的，没有就随机；只经能力层 ``send_sticker`` 发送。

插件不感知任何平台差异：收进来时把消息段原样存成「平台原生引用」，发送时
交给能力层去翻译；平台回的原生引用（如 Telegram file_id）由核心回填表情库。
所有异常都在插件内部吞掉，绝不影响主回复流程。

配置在插件目录的 ``config.yaml``（没有就读 ``config.example.yaml``，都没有
用代码里的默认值）。
"""

from __future__ import annotations

import base64
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

logger = logging.getLogger(__name__)

# 能力名：发表情（与 rubenbot.adapters.base.SEND_STICKER 一致）
CAPABILITY = "send_sticker"

# 收集时认可的消息段类型：贴纸 / 图片 / OneBot 的商城表情
COLLECTABLE_SEGMENTS = frozenset({"sticker", "image", "mface"})

# 一条消息最多并进来的标签数，避免把整段话都塞成标签
MAX_TAGS = 8


@dataclass(frozen=True)
class StickerPluginConfig:
    """归一化后的插件配置。"""

    collect: bool = False
    send_probability: float = 0.15
    cooldown_seconds: float = 300.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "StickerPluginConfig":
        raw = data if isinstance(data, Mapping) else {}
        return cls(
            # 收集默认关：写错类型也按默认（关）处理
            collect=raw.get("collect") is True,
            send_probability=_as_probability(raw.get("send_probability"), 0.15),
            cooldown_seconds=_as_non_negative(raw.get("cooldown_seconds"), 300.0),
        )


def load_config(path: str | Path | None = None) -> StickerPluginConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；没有或损坏用默认值。"""
    if path is not None:
        candidates = [Path(path)]
    else:
        here = Path(__file__).parent
        candidates = [here / "config.yaml", here / "config.example.yaml"]
    file = next((item for item in candidates if item.exists()), None)
    if file is None:
        return StickerPluginConfig.from_mapping(None)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能启动
        logger.warning("sticker 读取配置 %s 失败，使用默认配置：%s", file, exc)
        return StickerPluginConfig.from_mapping(None)
    return StickerPluginConfig.from_mapping(data)


class StickerPlugin:
    """收集与发送的决策/执行；随机源、时钟与表情库都可注入，便于测试。"""

    def __init__(
        self,
        config: StickerPluginConfig | Mapping[str, Any] | None = None,
        *,
        store: Any = None,
        store_getter: Callable[[], Any] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if isinstance(config, StickerPluginConfig):
            self.config = config
        else:
            self.config = StickerPluginConfig.from_mapping(config)
        self._store = store
        self._store_getter = store_getter
        self.rng = rng or random.Random()
        self.clock = clock or time.monotonic
        # 会话 -> 上次发表情的时间
        self._last_sent_at: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # 表情库
    # ------------------------------------------------------------------

    @property
    def store(self) -> Any:
        if self._store is not None:
            return self._store
        if self._store_getter is not None:
            self._store = self._store_getter()
            if self._store is not None:
                return self._store
        return None

    # ------------------------------------------------------------------
    # 钩子：收集
    # ------------------------------------------------------------------

    async def on_message(self, runtime, event) -> None:
        """收集群聊里的表情/图片段。任何异常都只记 debug，不影响主流程。"""
        try:
            if not self.config.collect or not getattr(event, "is_group", False):
                return
            store = self.store
            if store is None:
                return
            platform = str(getattr(event, "platform", "") or "")
            tags = topic_tags(getattr(event, "text", "") or "")
            for segment in getattr(event, "segments", None) or []:
                self._collect_segment(store, platform, segment, tags)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.debug("sticker 收集异常：%s", exc)

    def _collect_segment(self, store, platform: str, segment: Any, tags: list[str]) -> None:
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
        # 有 base64 就落成图片本体（能去重、也能跨平台复用）；否则只存平台引用
        raw = _decode_base64(payload.get("base64"))
        refs = {platform: {"kind": kind, "data": payload}} if platform else None
        if raw is None and not refs:
            return
        store.add(
            raw,
            tags=tags,
            native_refs=refs,
            file_name=payload.get("filename") or payload.get("name") or None,
            mime_type=payload.get("mime_type") or payload.get("content_type") or None,
        )

    # ------------------------------------------------------------------
    # 钩子：发送
    # ------------------------------------------------------------------

    async def on_after_reply(self, runtime, event, reply: str) -> None:
        """回复后按概率/冷却补一张表情。任何异常都只记 debug。"""
        try:
            if self.rng.random() >= self.config.send_probability:
                return
            now = self.clock()
            if self._in_cooldown(event, now):
                logger.debug(
                    "sticker：会话 %s 仍在冷却（%.0fs 内最多一张）",
                    getattr(event, "chat_id", "?"),
                    self.config.cooldown_seconds,
                )
                return
            store = self.store
            if store is None:
                return
            sticker = self._pick(store, getattr(event, "text", "") or "")
            if sticker is None:
                logger.debug("sticker：表情库是空的，跳过")
                return
            bot = _resolve_bot(runtime)
            if bot is None:
                return
            platform = str(getattr(event, "platform", "") or "")
            adapter_name = _select_adapter(bot, platform, CAPABILITY)
            if adapter_name is None:
                logger.debug("sticker：%s 没有可用的 %s 能力，跳过", platform, CAPABILITY)
                return
            # 先记冷却再调用：失败也不在冷却期内反复重试
            self._last_sent_at[_chat_key(event)] = now
            ok, reason = await bot.call_capability(
                adapter_name,
                CAPABILITY,
                sticker=store.reference(sticker),
                chat_id=getattr(event, "chat_id", ""),
                chat_type=getattr(event, "chat_type", "group"),
            )
            if ok:
                logger.debug(
                    "sticker：已给 %s 补一张表情 %s",
                    getattr(event, "chat_id", "?"),
                    sticker.id,
                )
            else:
                logger.debug("sticker：发表情失败（忽略）：%s", reason)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.debug("sticker 发送异常：%s", exc)

    def _in_cooldown(self, event, now: float) -> bool:
        last = self._last_sent_at.get(_chat_key(event))
        return last is not None and now - last < self.config.cooldown_seconds

    def _pick(self, store, text: str):
        """选一张表情：优先话题标签沾边的，没有就全库随机。"""
        tags = topic_tags(text)
        candidates = store.find_by_tags(tags) if tags else []
        if not candidates:
            candidates = store.find_by_tags(None)
        if not candidates:
            return None
        return self.rng.choice(candidates)


# ----------------------------------------------------------------------
# 运行时/事件辅助（与平台无关）
# ----------------------------------------------------------------------


def topic_tags(text: str) -> list[str]:
    """从消息文本里抽话题标签：2 字以上的词 + 中文相邻二字。"""
    try:
        from rubenbot.memory.retrieve import tokenize

        tokens = tokenize(text or "")
    except Exception:  # noqa: BLE001 - 分词失败就不打标签
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
    """段里的 base64（``base64://`` / ``data:`` 前缀都认）-> 字节；没有返回 None。"""
    if not value:
        return None
    text = str(value)
    if text.startswith("base64://"):
        text = text[len("base64://") :]
    elif text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        data = base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001 - 坏 base64 按没有处理
        return None
    return data or None


def _resolve_bot(runtime):
    extras = getattr(runtime, "extras", None)
    if isinstance(extras, dict):
        bot = extras.get("bot")
        if bot is not None and hasattr(bot, "call_capability"):
            return bot
    if hasattr(runtime, "call_capability"):
        return runtime
    return None


def _select_adapter(bot, platform: str, capability: str) -> str | None:
    """返回当前可用的适配器名；None 表示能力不可用。

    只看能力声明与配置开关，不做任何平台特有判断：先试事件来源平台，
    没有就按注册顺序找第一个支持的。
    """
    adapters = getattr(bot, "adapters", None)
    if not isinstance(adapters, dict) or not adapters:
        return None
    candidates = [adapters[platform]] if platform in adapters else list(adapters.values())
    config = getattr(bot, "config", None)
    for adapter in candidates:
        if capability not in (getattr(adapter, "capabilities", frozenset()) or ()):
            continue
        config_key = (getattr(adapter, "capability_config_keys", None) or {}).get(capability)
        if config_key and config is not None:
            default = (getattr(adapter, "capability_defaults", None) or {}).get(capability, True)
            try:
                if not config.adapter_capability_enabled(adapter.name, config_key, default=default):
                    continue
            except Exception:  # noqa: BLE001 - 配置读取失败按不可用处理
                continue
        return adapter.name
    return None


def _chat_key(event) -> tuple[str, str]:
    return (str(getattr(event, "platform", "")), str(getattr(event, "chat_id", "")))


def _as_probability(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(1.0, max(0.0, float(value)))


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def register(ctx) -> None:
    """插件入口：注册收集与回复后发送两个钩子。"""
    plugin = StickerPlugin(
        load_config(),
        store_getter=lambda: getattr(ctx.bot, "stickers", None),
    )
    ctx.extras["sticker"] = plugin
    ctx.on_message(plugin.on_message)
    ctx.on_after_reply(plugin.on_after_reply)
    logger.debug(
        "sticker 已注册（collect=%s，概率 %.2f，冷却 %.0fs）",
        plugin.config.collect,
        plugin.config.send_probability,
        plugin.config.cooldown_seconds,
    )
