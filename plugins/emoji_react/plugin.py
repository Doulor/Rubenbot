"""贴表情反应插件：什么时候贴、贴哪个由插件决定，动作走核心能力入口。

两条触发路径：

1. **跟随回复**（``on_after_reply``）：bot 回复后，按 ``after_reply.probability``
   给刚被回复的那条用户消息贴一个表情，表情从 ``after_reply.emojis`` 里随机选。
2. **关键词触发**（``on_message``）：消息命中 ``keywords.groups`` 里的关键词时，
   按 ``keywords.probability`` 给这条消息贴该组对应的表情。

节制：同一会话（平台 + chat_id）在 ``cooldown_seconds`` 内最多贴一次；表情池为空、
能力未声明、配置开关关闭、拿不到消息 id 时都只记 debug 日志并跳过。
所有异常都在插件内部吞掉，绝不影响主回复流程。

能力调用通道：核心给钩子传入的 ``runtime``（BotContext）的 ``extras["bot"]``
就是 :class:`rubenbot.core.RubenBot`，这里只通过它的 ``call_capability`` 调
``message.emoji_like``，不直接拼任何 OneBot 报文。

配置在本目录的 ``config.yaml``（没有就读 ``config.example.yaml``，两者都没有
也能跑，缺省值在代码里）；表情 ID 含义以公开的 OneBot/CQ ``face`` 资料为准，
见配置文件里的注释。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml

logger = logging.getLogger(__name__)

# 适配器声明的能力名（SnowLuma 的 set_msg_emoji_like 扩展动作）
CAPABILITY = "message.emoji_like"

# 默认表情 ID 取自公开的 CQ/OneBot ``face`` 经典表（0-104），含义如下：
#   0 惊讶   5 流泪   13 呲牙   14 微笑   15 难过   20 偷笑   21 可爱
#  26 惊恐  27 流汗  32 疑问  40 擦汗  42 鼓掌  52 亲亲  54 可怜
#  63 玫瑰  65 示爱  66 爱心  78 拥抱  79 强    89 OK
# 105 之后的新表情在不同客户端版本里编号可能不一致，默认不采用；需要时在
# config.yaml 里自行补充，确认不了含义的就不填（空池 = 不贴）。
DEFAULT_AFTER_REPLY_EMOJIS: tuple[int, ...] = (13, 14, 20, 21, 79)
DEFAULT_KEYWORD_GROUPS: tuple[tuple[tuple[str, ...], tuple[int, ...]], ...] = (
    # 开心/笑
    (("哈哈", "笑死", "嘿嘿", "233", "hhh", "lol", "笑不活了"), (13, 20, 21)),
    # 喜欢/亲近
    (("爱你", "喜欢你", "么么", "亲亲", "抱抱", "可爱"), (66, 65, 78, 52)),
    # 佩服/赞同
    (("厉害", "牛啊", "666", "好强", "膜拜", "大佬", "绝了"), (79, 42, 89)),
    # 惊讶
    (("卧槽", "居然", "真的假的", "天哪", "震惊", "啊这"), (0, 26, 32)),
    # 无语
    (("无语", "服了", "离谱", "流汗", "汗"), (27, 40)),
    # 安慰
    (("难过", "想哭", "emo", "累了", "郁闷", "焦虑"), (78, 5, 15, 54)),
)

EmojiId = int | str


@dataclass(frozen=True)
class KeywordGroup:
    """一组关键词及其对应表情池。"""

    keywords: tuple[str, ...]
    emojis: tuple[EmojiId, ...]


@dataclass(frozen=True)
class ReactConfig:
    """归一化后的插件配置。"""

    enabled: bool = True
    cooldown_seconds: float = 120.0
    after_reply_probability: float = 0.25
    after_reply_emojis: tuple[EmojiId, ...] = DEFAULT_AFTER_REPLY_EMOJIS
    keyword_probability: float = 0.6
    keyword_groups: tuple[KeywordGroup, ...] = field(
        default_factory=lambda: tuple(
            KeywordGroup(tuple(keywords), tuple(emojis))
            for keywords, emojis in DEFAULT_KEYWORD_GROUPS
        )
    )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ReactConfig":
        """从 YAML 映射构造；类型不对的项退回默认值，不让配置错误炸掉插件。"""
        raw = data if isinstance(data, Mapping) else {}
        after = raw.get("after_reply")
        after = after if isinstance(after, Mapping) else {}
        keywords = raw.get("keywords")
        keywords = keywords if isinstance(keywords, Mapping) else {}

        groups = _as_keyword_groups(keywords.get("groups"))
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            cooldown_seconds=_as_non_negative(raw.get("cooldown_seconds"), 120.0),
            after_reply_probability=_as_probability(
                after.get("probability"), 0.25
            ),
            # 显式写了空列表就表示不贴；没写才用默认池
            after_reply_emojis=_emojis_or_default(
                after.get("emojis"), DEFAULT_AFTER_REPLY_EMOJIS
            ),
            keyword_probability=_as_probability(keywords.get("probability"), 0.6),
            keyword_groups=_groups_or_default(groups),
        )


def load_config(path: str | Path | None = None) -> ReactConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；都没有或损坏时用默认值。"""
    if path is not None:
        candidates = [Path(path)]
    else:
        here = Path(__file__).parent
        candidates = [here / "config.yaml", here / "config.example.yaml"]
    file = next((item for item in candidates if item.exists()), None)
    if file is None:
        return ReactConfig.from_mapping(None)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能启动
        logger.warning("emoji_react 读取配置 %s 失败，使用默认配置：%s", file, exc)
        return ReactConfig.from_mapping(None)
    return ReactConfig.from_mapping(data)


class EmojiReactor:
    """贴表情的决策与执行；时间与随机源可注入，便于测试。"""

    def __init__(
        self,
        config: ReactConfig | Mapping[str, Any] | None = None,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if isinstance(config, ReactConfig):
            self.config = config
        else:
            self.config = ReactConfig.from_mapping(config)
        self.rng = rng or random.Random()
        self.clock = clock or time.monotonic
        # 会话 -> 上次尝试贴表情的时间
        self._last_react_at: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    async def on_message(self, runtime, event) -> None:
        """关键词触发。任何异常都只记 debug，不影响主流程。"""
        try:
            if not self.config.enabled:
                return
            group = self._match_keyword(getattr(event, "text", "") or "")
            if group is None:
                return
            await self._react(
                runtime,
                event,
                group.emojis,
                self.config.keyword_probability,
                "关键词",
            )
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.debug("emoji_react 关键词贴表情异常：%s", exc)

    async def on_after_reply(self, runtime, event, reply: str) -> None:
        """跟随回复触发。任何异常都只记 debug，不影响主流程。"""
        try:
            if not self.config.enabled:
                return
            await self._react(
                runtime,
                event,
                self.config.after_reply_emojis,
                self.config.after_reply_probability,
                "跟随回复",
            )
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.debug("emoji_react 跟随回复贴表情异常：%s", exc)

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    def _match_keyword(self, text: str) -> KeywordGroup | None:
        if not text:
            return None
        lowered = text.lower()
        for group in self.config.keyword_groups:
            for keyword in group.keywords:
                if keyword and keyword.lower() in lowered:
                    return group
        return None

    def _in_cooldown(self, event, now: float) -> bool:
        key = (str(getattr(event, "platform", "")), str(getattr(event, "chat_id", "")))
        last = self._last_react_at.get(key)
        return last is not None and now - last < self.config.cooldown_seconds

    async def _react(
        self,
        runtime,
        event,
        emojis: Iterable[EmojiId],
        probability: float,
        source: str,
    ) -> None:
        """按概率、冷却与能力可用性决定是否贴，并走核心能力入口执行。"""
        pool = tuple(emojis)
        if not pool:
            logger.debug("emoji_react：表情池为空，跳过（%s）", source)
            return

        bot = _resolve_bot(runtime)
        if bot is None:
            logger.debug("emoji_react：拿不到 bot 能力入口，跳过（%s）", source)
            return

        platform = str(getattr(event, "platform", "") or "")
        adapter_name = _select_adapter(bot, platform, CAPABILITY)
        if adapter_name is None:
            # 能力未声明或配置里关掉了：静默跳过，不调用核心以免刷 warning
            logger.debug("emoji_react：%s 没有可用的 %s 能力，跳过（%s）", platform, CAPABILITY, source)
            return

        if self.rng.random() >= probability:
            return

        now = self.clock()
        if self._in_cooldown(event, now):
            logger.debug(
                "emoji_react：会话 %s 仍在冷却（%.0fs 内最多一次），跳过（%s）",
                getattr(event, "chat_id", "?"),
                self.config.cooldown_seconds,
                source,
            )
            return

        message_id = _message_id(event)
        if message_id is None:
            logger.debug("emoji_react：事件里没有消息 id，跳过（%s）", source)
            return

        emoji_id = self.rng.choice(pool)
        # 先记冷却再调用：即使平台调用失败，也不在冷却期内反复重试
        self._last_react_at[
            (str(getattr(event, "platform", "")), str(getattr(event, "chat_id", "")))
        ] = now
        ok, reason = await bot.call_capability(
            adapter_name, CAPABILITY, message_id=message_id, emoji_id=emoji_id
        )
        if ok:
            logger.debug(
                "emoji_react：已给 %s 的消息 %s 贴表情 %s（%s）",
                getattr(event, "chat_id", "?"),
                message_id,
                emoji_id,
                source,
            )
        else:
            logger.debug("emoji_react：贴表情失败（%s），忽略：%s", source, reason)


# ----------------------------------------------------------------------
# 运行时/事件辅助
# ----------------------------------------------------------------------


def _resolve_bot(runtime):
    """从钩子运行时上下文里取核心 bot（能力入口）。"""
    extras = getattr(runtime, "extras", None)
    if isinstance(extras, dict):
        bot = extras.get("bot")
        if bot is not None and hasattr(bot, "call_capability"):
            return bot
    # 兼容直接传 bot 作为 runtime 的用法
    if hasattr(runtime, "call_capability"):
        return runtime
    return None


def _select_adapter(bot, platform: str, capability: str) -> str | None:
    """返回当前可用的适配器名；None 表示能力不可用。

    先看事件来源平台（回复从哪个平台来就贴到哪），没有注册该平台适配器时
    再按注册顺序自动挑。这里提前判断声明与配置开关，避免核心在能力关闭时
    每次打 warning。
    """
    adapters = getattr(bot, "adapters", None)
    if not isinstance(adapters, dict) or not adapters:
        return None
    if platform in adapters:
        candidates = [adapters[platform]]
    else:
        candidates = list(adapters.values())

    config = getattr(bot, "config", None)
    for adapter in candidates:
        declared = getattr(adapter, "capabilities", frozenset()) or ()
        if capability not in declared:
            continue
        config_key = (getattr(adapter, "capability_config_keys", None) or {}).get(capability)
        if config_key and config is not None:
            default = (getattr(adapter, "capability_defaults", None) or {}).get(
                capability, True
            )
            try:
                if not config.adapter_capability_enabled(
                    adapter.name, config_key, default=default
                ):
                    continue
            except Exception:  # noqa: BLE001 - 配置读取失败按不可用处理
                continue
        return adapter.name
    return None


def _message_id(event) -> Any | None:
    """取用户这条消息在平台上的 id（贴表情的对象）。

    OneBot 事件顶层的 ``message_id`` 就是它；SnowLuma 适配器把同一个值额外
    存成 ``raw["_reply_message_id"]``，两种键都认。
    """
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict):
        for key in ("message_id", "_reply_message_id"):
            value = raw.get(key)
            if value not in (None, ""):
                return value
    return None


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_probability(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(1.0, max(0.0, float(value)))


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def _as_emojis(value: Any) -> tuple[EmojiId, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[EmojiId] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            result.append(item)
        elif isinstance(item, str) and item.strip():
            result.append(item.strip())
    return tuple(result)


def _as_keyword_groups(value: Any) -> tuple[KeywordGroup, ...] | None:
    """解析关键词组；字段缺失返回 None，显式空列表返回空元组。"""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return ()
    groups: list[KeywordGroup] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        keywords = tuple(
            str(keyword).strip()
            for keyword in (item.get("keywords") or [])
            if str(keyword).strip()
        )
        emojis = _as_emojis(item.get("emojis"))
        if keywords and emojis:
            groups.append(KeywordGroup(keywords, emojis))
    return tuple(groups)


def _emojis_or_default(value: Any, default: tuple[EmojiId, ...]) -> tuple[EmojiId, ...]:
    """未配置用默认池；显式写成列表（哪怕是空的）则尊重配置。"""
    if value is None:
        return default
    return _as_emojis(value)


def _groups_or_default(groups: tuple[KeywordGroup, ...] | None) -> tuple[KeywordGroup, ...]:
    """未配置关键词表时用默认表；显式配置了但全部无效则视为关闭关键词触发。"""
    if groups is None:
        return tuple(
            KeywordGroup(tuple(keywords), tuple(emojis))
            for keywords, emojis in DEFAULT_KEYWORD_GROUPS
        )
    return groups


def register(ctx) -> None:
    """插件入口：注册关键词与跟随回复两个钩子。"""
    reactor = EmojiReactor(load_config())
    ctx.on_message(reactor.on_message)
    ctx.on_after_reply(reactor.on_after_reply)
    logger.debug(
        "emoji_react 已注册（enabled=%s，冷却 %.0fs）",
        reactor.config.enabled,
        reactor.config.cooldown_seconds,
    )
