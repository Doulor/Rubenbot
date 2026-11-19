"""回复决策（意愿系统）：群聊里决定「要不要开口」。

这个模块只做判断，不发消息、不碰适配器：

- :class:`MessageMerger`：同一发送者短时间连发的消息先攒着，合并成一轮再决策。
  纯逻辑、时钟外部传入，测试可以完全确定。
- :class:`DecisionEngine`：硬触发直通（私聊、@、回复 bot、叫名字），普通群消息
  按意愿评分（基础值 + 动量 - 发言占比 - 冷却，阈值附近加随机扰动）。
- :func:`parse_generation_output`：解析模型输出里的「不回复」标记。

设计上刻意保持轻量：所有状态都在内存里，进程重启后从零开始；没有额外的表。
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .adapters.base import MessageEvent
from .config import DecisionConfig, GroupDecisionConfig

logger = logging.getLogger(__name__)

# bot 自己发出的消息在流水里的 sender_id（与 core 保持一致）
BOT_SENDER_ID = "__bot__"

# 硬触发的短窗口默认值在这里只做兜底，真正取值来自 DecisionConfig
_DEFAULT_ALIASES = ("Ruben",)

# 除了配置里的 veto_marker，也接受几种常见写法，避免模型自由发挥时漏判
_VETO_ALIASES = {"不回复", "（不回复）", "(不回复)", "【不回复】", "[[不回复]]"}

_QUIET_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")


# ----------------------------------------------------------------------
# 合并窗口
# ----------------------------------------------------------------------


@dataclass
class MergedRound:
    """攒在一起、等待决策的一轮消息（来自同一发送者）。"""

    platform: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    events: list[MessageEvent] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    deadline: float = 0.0
    hard: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.chat_id, self.sender_id)

    @property
    def text(self) -> str:
        return "\n".join(e.text for e in self.events if e.text and e.text.strip())

    def to_event(self) -> MessageEvent:
        """合并后的合成事件，供决策、上下文与插件使用。"""
        last = self.events[-1]
        reply_to = next((e.reply_to_id for e in reversed(self.events) if e.reply_to_id), None)
        raw = dict(last.raw or {})
        raw["merged_count"] = len(self.events)
        return MessageEvent(
            platform=last.platform,
            chat_type=last.chat_type,
            chat_id=last.chat_id,
            sender_id=last.sender_id,
            sender_name=last.sender_name,
            text=self.text,
            reply_to_id=reply_to,
            mentions_bot=any(e.mentions_bot for e in self.events),
            raw=raw,
        )


class MessageMerger:
    """消息合并窗口。

    规则：

    - 同一会话里，新消息到达时把**其他发送者**尚未到期的轮次立刻放出来，
      因为对方插话通常意味着前一个人这一段说完了；
    - 同一发送者在窗口内继续发，延长截止时间并合并；
    - 硬触发消息使用短窗口（``hard_window``），设为 0 表示立即处理；
    - 窗口设为 0 时所有消息都立即处理。

    所有时间都由调用方传入，因此单测可以用假时钟精确控制。
    """

    def __init__(self, window: float = 4.0, hard_window: float = 1.0):
        self.window = max(0.0, float(window))
        self.hard_window = max(0.0, float(hard_window))
        self._pending: dict[tuple[str, str, str], MergedRound] = {}

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def pending(self) -> list[MergedRound]:
        return sorted(self._pending.values(), key=lambda r: (r.first_at, r.deadline))

    def _window_for(self, hard: bool) -> float:
        return self.hard_window if hard else self.window

    def add(self, event: MessageEvent, now: float, *, hard: bool = False) -> list[MergedRound]:
        """加入一条消息，返回此刻应当处理的轮次（按时间先后）。"""
        ready: list[MergedRound] = []
        key = (event.platform, event.chat_id, event.sender_id)

        # 同一会话里其他人说话了：先把他/她攒的那轮放出来
        for other_key, other in list(self._pending.items()):
            if other_key[0] == key[0] and other_key[1] == key[1] and other_key[2] != key[2]:
                ready.append(self._pending.pop(other_key))

        round_ = self._pending.get(key)
        if round_ is None:
            round_ = MergedRound(
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                sender_id=event.sender_id,
                sender_name=event.sender_name,
                first_at=now,
            )
            self._pending[key] = round_
        round_.events.append(event)
        round_.last_at = now
        round_.sender_name = event.sender_name or round_.sender_name
        round_.hard = round_.hard or hard
        round_.deadline = now + self._window_for(round_.hard)

        # 窗口为 0（或硬窗口为 0 的硬触发）时立即放行
        if round_.deadline <= now:
            ready.append(self._pending.pop(key))

        ready.sort(key=lambda r: (r.first_at, r.deadline))
        return ready

    def pop_ready(self, now: float) -> list[MergedRound]:
        """取出所有已到期的轮次（不改动未到期的）。"""
        ready = [r for r in self._pending.values() if r.deadline <= now]
        for round_ in ready:
            self._pending.pop(round_.key, None)
        ready.sort(key=lambda r: (r.deadline, r.first_at))
        return ready

    def flush_all(self) -> list[MergedRound]:
        """强制放出全部待处理轮次（停机、测试用）。"""
        ready = sorted(self._pending.values(), key=lambda r: (r.first_at, r.deadline))
        self._pending.clear()
        return ready

    def next_deadline(self) -> float | None:
        if not self._pending:
            return None
        return min(r.deadline for r in self._pending.values())


# ----------------------------------------------------------------------
# 决策结果
# ----------------------------------------------------------------------


@dataclass
class DecisionFactors:
    """一次意愿评分的各因子，便于日志与调参。"""

    base: float = 0.0
    momentum: float = 0.0
    relevance: float = 0.0
    ratio_penalty: float = 0.0
    cooldown_penalty: float = 0.0
    multiplier: float = 1.0
    noise: float = 0.0
    score: float = 0.0
    threshold: float = 0.0
    share: float = 0.0

    def describe(self) -> str:
        return (
            f"基础{self.base:.2f} 动量+{self.momentum:.2f} "
            f"相关+{self.relevance:.2f} 占比-{self.ratio_penalty:.2f} "
            f"冷却-{self.cooldown_penalty:.2f} 倍率x{self.multiplier:.2f}"
            + (f" 随机{self.noise:+.2f}" if self.noise else "")
        )


@dataclass
class ReplyDecision:
    """是否回复的结论。``reason`` 记录硬触发类型或主要否决因子。"""

    should_reply: bool
    reason: str = ""
    score: float = 0.0
    threshold: float = 0.0
    hard_trigger: str | None = None
    factors: DecisionFactors | None = None

    def log_text(self) -> str:
        verdict = "回" if self.should_reply else "不回"
        if self.factors is None:
            return f"{verdict}（{self.reason}）"
        return f"{verdict}（{self.reason}）分数 {self.score:.2f}/{self.threshold:.2f} [{self.factors.describe()}]"


@dataclass
class GenerationOutput:
    """模型输出解析结果。"""

    text: str
    vetoed: bool = False
    reason: str = ""


# ----------------------------------------------------------------------
# 硬触发与生成后否决
# ----------------------------------------------------------------------


def is_reply_to_bot(event: MessageEvent) -> bool:
    """被回复到 bot：适配器通常已置位 mentions_bot，这里看 raw 兜底。"""
    if not event.reply_to_id:
        return False
    raw = event.raw if isinstance(event.raw, dict) else {}
    bot_ids = raw.get("bot_message_ids")
    if bot_ids:
        return any(str(event.reply_to_id) == str(item) for item in bot_ids)
    return bool(raw.get("reply_to_bot"))


def _compile_alias(alias: str) -> re.Pattern[str]:
    """英文/数字别名按词边界匹配，中文/混合别名按子串匹配。"""
    pattern = re.escape(alias.strip())
    if alias.strip().isascii() and re.fullmatch(r"[\w.-]+", alias.strip()):
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
    return re.compile(pattern, re.IGNORECASE)


def parse_generation_output(reply: str | None, marker: str = "[不回复]") -> GenerationOutput:
    """解析生成结果：整条输出就是「不回复」标记时静默丢弃。

    只认「整条只有标记」的情况（允许外面包引号/括号、句末标点），避免模型
    在正常句子里提到「不回复」三个字就被误杀。
    """
    text = (reply or "").strip()
    if not text:
        return GenerationOutput("", vetoed=True, reason="empty")
    compact = text.strip(" \t\r\n　。.!！~～「」『』\"'")
    marker_compact = (marker or "").strip()
    if compact == marker_compact or compact in _VETO_ALIASES:
        return GenerationOutput("", vetoed=True, reason="veto_marker")
    # 标记外面再包一层方括号/引号，例如「【[不回复]】」
    if marker_compact and compact.strip("[]【】（）()") == marker_compact.strip("[]【】（）()"):
        return GenerationOutput("", vetoed=True, reason="veto_marker")
    return GenerationOutput(text)


# ----------------------------------------------------------------------
# 静默时段
# ----------------------------------------------------------------------


def parse_quiet_hours(values: Iterable[str] | None) -> list[tuple[int, int]]:
    """把 ``["23:00-07:30"]`` 解析成分钟区间；格式错误的项忽略。"""
    ranges: list[tuple[int, int]] = []
    for item in values or []:
        if not isinstance(item, str):
            continue
        match = _QUIET_RE.fullmatch(item)
        if not match:
            logger.warning("忽略格式错误的静默时段：%r（应为 HH:MM-HH:MM）", item)
            continue
        start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
        if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= end_h <= 23 and 0 <= end_m <= 59):
            logger.warning("忽略非法的静默时段：%r", item)
            continue
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        if start == end:
            continue
        ranges.append((start, end))
    return ranges


def in_quiet_hours(now: float, ranges: Iterable[tuple[int, int]]) -> bool:
    """按本地时间判断是否处于静默时段；支持跨午夜区间。"""
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    for start, end in ranges:
        if start < end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # 跨午夜
            return True
    return False


# ----------------------------------------------------------------------
# 意愿评分
# ----------------------------------------------------------------------


class DecisionEngine:
    """意愿评分引擎。状态只有「最近一次发言时间」，按会话记录。"""

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        aliases: Iterable[str] = _DEFAULT_ALIASES,
        rng: random.Random | None = None,
    ):
        self.config = config or DecisionConfig()
        self.rng = rng or random.Random()
        cleaned = []
        for alias in aliases:
            if not alias or not alias.strip():
                continue
            if alias.strip() not in cleaned:
                cleaned.append(alias.strip())
        self.aliases = cleaned
        self._alias_patterns = [(_compile_alias(a), a) for a in cleaned]
        self._last_reply_at: dict[str, float] = {}

    def set_aliases(self, aliases: Iterable[str]) -> None:
        """替换硬触发的名字/别名（配置热更新用，不改评分逻辑）。"""
        cleaned: list[str] = []
        for alias in aliases:
            if not alias or not alias.strip():
                continue
            if alias.strip() not in cleaned:
                cleaned.append(alias.strip())
        self.aliases = cleaned
        self._alias_patterns = [(_compile_alias(a), a) for a in cleaned]

    # ---- 硬触发 ----

    def match_alias(self, text: str) -> str | None:
        """消息里叫了 bot 的名字/别名则返回命中的别名。"""
        for pattern, alias in self._alias_patterns:
            if pattern.search(text or ""):
                return alias
        return None

    def is_hard_trigger(self, event: MessageEvent) -> str | None:
        """返回硬触发类型；``None`` 表示需要走意愿评分。"""
        if event.is_private:
            return "private"
        if event.mentions_bot:
            return "mentioned"
        if is_reply_to_bot(event):
            return "reply_to_bot"
        alias = self.match_alias(event.text)
        if alias:
            return f"name:{alias}"
        return None

    # ---- 状态 ----

    @staticmethod
    def chat_key(event: MessageEvent) -> str:
        return f"{event.platform}:{event.chat_id}"

    def note_reply(self, chat_key: str, now: float) -> None:
        """记录 bot 在某个会话发言的时间（动量与冷却都用它）。"""
        self._last_reply_at[chat_key] = now

    def last_reply_at(self, chat_key: str) -> float | None:
        return self._last_reply_at.get(chat_key)

    def group_settings(self, chat_id: str) -> GroupDecisionConfig:
        return self.config.groups.get(str(chat_id)) or GroupDecisionConfig()

    def _quiet_ranges(self, settings: GroupDecisionConfig) -> list[tuple[int, int]]:
        # 每次现算（量很小），配置改了也能立即生效
        return parse_quiet_hours(settings.quiet_hours)

    # ---- 评分 ----

    def evaluate(
        self,
        event: MessageEvent,
        *,
        now: float | None = None,
        recent_messages: Sequence[Any] = (),
        relevance: float = 0.0,
        hard_trigger: str | None = None,
    ) -> ReplyDecision:
        """对一条（或合并后的一轮）群消息评分。

        ``recent_messages`` 只需有 ``sender_id`` 属性；``relevance`` 是记忆检索的
        最高分（未命中为 0）。硬触发（含私聊）直接通过，不受冷却/静默影响。
        """
        now = time.time() if now is None else now
        hard = hard_trigger if hard_trigger is not None else self.is_hard_trigger(event)
        if hard:
            # 硬触发不看分数，factors 留空，日志里只记触发原因
            return ReplyDecision(
                True,
                reason=hard,
                score=1.0,
                threshold=self.config.threshold,
                hard_trigger=hard,
            )

        cfg = self.config
        settings = self.group_settings(event.chat_id)
        chat_key = self.chat_key(event)
        factors = DecisionFactors(
            base=cfg.base if settings.base is None else settings.base,
            multiplier=settings.activity_multiplier,
            threshold=cfg.threshold,
        )

        # 静默时段：普通消息直接闭嘴，硬触发在上面已经放行
        if in_quiet_hours(now, self._quiet_ranges(settings)):
            factors.score = 0.0
            return ReplyDecision(
                False, "quiet_hours", 0.0, cfg.threshold, None, factors
            )

        # 对话动量：刚参与过这个会话，对方还在继续说，加分随时间指数衰减
        last_reply = self._last_reply_at.get(chat_key)
        elapsed = None if last_reply is None else max(0.0, now - last_reply)
        if elapsed is not None and cfg.momentum_bonus > 0:
            half_life = max(1.0, cfg.momentum_half_life_seconds)
            factors.momentum = cfg.momentum_bonus * (0.5 ** (elapsed / half_life))

        # 话题相关性：记忆检索最高分归一化后加分
        if cfg.relevance_bonus > 0 and cfg.relevance_scale > 0 and relevance > 0:
            factors.relevance = cfg.relevance_bonus * min(1.0, relevance / cfg.relevance_scale)

        # 发言占比抑制：最近 N 条里 bot 占比超过阈值，大幅减分；过高则直接静默
        total = len(recent_messages)
        if total > 0:
            bot_count = sum(1 for msg in recent_messages if getattr(msg, "sender_id", "") == BOT_SENDER_ID)
            factors.share = bot_count / total
            if factors.share > cfg.ratio_threshold:
                span = max(1e-6, 1.0 - cfg.ratio_threshold)
                factors.ratio_penalty = cfg.ratio_penalty * min(1.0, (factors.share - cfg.ratio_threshold) / span)
                if factors.share >= cfg.ratio_silence:
                    factors.score = 0.0
                    return ReplyDecision(
                        False, "ratio_silence", 0.0, cfg.threshold, None, factors
                    )

        # 发言后冷却：刚发过言的短时间内压低意愿（硬触发已在上面放行）
        if elapsed is not None and cfg.cooldown_seconds > 0 and elapsed < cfg.cooldown_seconds:
            remain = 1.0 - elapsed / cfg.cooldown_seconds
            factors.cooldown_penalty = cfg.cooldown_penalty * max(0.0, min(1.0, remain))

        positive = factors.base + factors.momentum + factors.relevance
        score = positive * max(0.0, factors.multiplier) - factors.ratio_penalty - factors.cooldown_penalty

        # 阈值附近加随机扰动，避免每次都在同一个位置机械地卡线
        if cfg.threshold_noise > 0 and abs(score - cfg.threshold) <= cfg.threshold_noise_band:
            factors.noise = self.rng.uniform(-cfg.threshold_noise, cfg.threshold_noise)
            score += factors.noise

        factors.score = score
        should = score >= cfg.threshold
        reason = "willing" if should else "below_threshold"
        return ReplyDecision(should, reason, score, cfg.threshold, None, factors)
