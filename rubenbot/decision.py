"""回复决策：群聊里决定「要不要开口」。

这个模块只做判断，不发消息、不碰适配器：

- :class:`MessageMerger`：同一群聊短时间连发的消息先攒着，合并成一轮再决策。
  纯逻辑、时钟外部传入，测试可以完全确定。
- :class:`DecisionEngine`：硬触发直通（私聊、@、回复 bot、叫名字），普通群消息
  经过节奏护栏后交给策划。冷场主动开口另行评分。
- :func:`parse_generation_output`：解析模型输出里的「不回复」标记。

设计上刻意保持轻量：所有状态都在内存里，进程重启后从零开始；没有额外的表。
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .adapters.base import MessageEvent
from .config import DecisionConfig, ProactiveConfig

logger = logging.getLogger(__name__)

# bot 自己发出的消息在流水里的 sender_id（与 core 保持一致）
BOT_SENDER_ID = "__bot__"

# 硬触发的短窗口默认值在这里只做兜底，真正取值来自 DecisionConfig
_DEFAULT_ALIASES = ("Ruben",)

# 除了配置里的 veto_marker，也接受几种常见写法，避免模型自由发挥时漏判
_VETO_ALIASES = {"不回复", "（不回复）", "(不回复)", "【不回复】", "[[不回复]]"}

# 相似度只比较归一化后的短文本：数字、时刻的小变化不是新模板。
_TEMPLATE_VARIABLE = re.compile(r"\d+(?:[.:/-]\d+)*")


def _template(text: str) -> str:
    text = "".join(text[:512].lower().split())
    text = _TEMPLATE_VARIABLE.sub("#", text)
    # 纯数字、表情占位和「好」「哈哈」等短接话不能单独证明存在模板。
    if sum(ch.isalpha() for ch in text) < 4 or text in {"[图片]", "[表情]", "[sticker]"}:
        return ""
    return text


def _similar_template(a: str, b: str) -> bool:
    if a == b:
        return True
    # 字三元组重合度，线性开销；短文本有一处实质差别就宁可放行。
    left = {a[i:i+3] for i in range(len(a) - 2)}
    right = {b[i:i+3] for i in range(len(b) - 2)}
    return len(left & right) >= 0.85 * len(left | right)


_QUIET_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")


# ----------------------------------------------------------------------
# 合并窗口
# ----------------------------------------------------------------------


@dataclass
class MergedRound:
    """攒在一起、等待决策的一轮消息（保留每条的真实发送者）。"""

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
    # 批次里出现过 @/回复 bot 的消息；一旦为真，截止时间不再延后
    urgent: bool = False
    # 有真机操作资格的群消息不与其他发送者共用工具轮。
    isolated_sender: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.chat_type, self.chat_id)

    @property
    def text(self) -> str:
        return "\n".join(e.text for e in self.events if e.text and e.text.strip())

    @property
    def primary_sender(self) -> str:
        """按条数选主要发言人；并列时取最后发言的人。"""
        counts = Counter(e.sender_id for e in self.events)
        return max(reversed(self.events), key=lambda e: counts[e.sender_id]).sender_id

    def to_event(self) -> MessageEvent:
        """当前说话人的投影；不同人的正文、引用和身份绝不合成一人的请求。"""
        sender = self.primary_sender
        own = [e for e in self.events if e.sender_id == sender]
        last = own[-1]
        reply = next((e for e in reversed(own) if e.reply_to_id), None)
        raw = dict(last.raw or {})
        raw["merged_count"] = len(self.events)
        segments = [segment for event in own for segment in (event.segments or [])]
        has_voice = any(e.raw.get("has_voice") for e in own) or any(
            segment.type == "record" for segment in segments
        )
        if has_voice:
            raw["has_voice"] = True
        return MessageEvent(
            platform=last.platform,
            chat_type=last.chat_type,
            chat_id=last.chat_id,
            sender_id=last.sender_id,
            sender_name=last.sender_name,
            text="\n".join(e.text for e in own if e.text.strip()),
            reply_to_id=reply.reply_to_id if reply else None,
            reply_sender_id=reply.reply_sender_id if reply else "",
            mentions={uid: name for e in own for uid, name in e.mentions.items()},
            mentions_bot=any(e.mentions_bot or is_reply_to_bot(e) for e in own),
            raw=raw,
            segments=segments,
            familiarity=tuple({tag.entity: tag for e in self.events for tag in e.familiarity}.values()),
        )


class MessageMerger:
    """消息合并窗口。

    规则：

    - 群聊按会话收集，谁发言都刷新静默窗口；私聊立即处理；
    - 硬触发消息使用短窗口（``hard_window``），设为 0 表示立即处理；
    - **整批上限**：批次从首条消息起最多等 ``batch_max`` 秒，到点必放出
      （0 表示不设上限）；
    - **@ 上限**：批次里出现 @/回复 bot 的消息时，截止时间提前到
      「首条 + ``urgent``」秒（0 表示不启用）；
    - **到期不续命**：批次一旦到期封口，新的同人消息不再延长它，而是让
      旧批次先放出、自己进新批次；
    - 窗口设为 0 时所有消息都立即处理。

    所有时间都由调用方传入，因此单测可以用假时钟精确控制。
    """

    def __init__(
        self,
        window: float = 2.5,
        hard_window: float = 1.0,
        *,
        batch_max: float = 10.0,
        urgent: float = 2.0,
    ):
        self.window = max(0.0, float(window))
        self.hard_window = max(0.0, float(hard_window))
        self.batch_max = max(0.0, float(batch_max))
        self.urgent = max(0.0, float(urgent))
        self._pending: dict[tuple[str, str, str], MergedRound] = {}

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def pending(self) -> list[MergedRound]:
        return sorted(self._pending.values(), key=lambda r: (r.first_at, r.deadline))

    def _window_for(self, hard: bool) -> float:
        return self.hard_window if hard else self.window

    def _deadline_for(self, round_: MergedRound, now: float, hard: bool, urgent: bool) -> float:
        """统一截止时间计算：窗口 / 整批上限 / @ 上限取最早者。"""
        deadline = now + self._window_for(hard)
        if self.batch_max > 0:
            deadline = min(deadline, round_.first_at + self.batch_max)
        if urgent and self.urgent > 0:
            deadline = min(deadline, round_.first_at + self.urgent)
        return deadline

    def add(
        self,
        event: MessageEvent,
        now: float,
        *,
        hard: bool = False,
        urgent: bool = False,
        isolate_sender: bool = False,
    ) -> list[MergedRound]:
        """加入一条消息，返回此刻应当处理的轮次（按时间先后）。

        ``urgent`` 表示这条消息 @/回复了 bot，会把本批截止时间提前。
        """
        ready: list[MergedRound] = []
        key = (event.platform, event.chat_type, event.chat_id)

        round_ = self._pending.get(key)
        if round_ is not None and (round_.deadline <= now or (
            (round_.isolated_sender and event.sender_id != round_.sender_id)
            or (isolate_sender and any(item.sender_id != event.sender_id for item in round_.events))
        )):
            # 到期或跨越授权边界就封口；不能把访客的任务拼给机主。
            ready.append(self._pending.pop(key))
            round_ = None
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
        round_.isolated_sender |= isolate_sender
        round_.events.append(event)
        round_.last_at = now
        round_.hard = round_.hard or hard
        round_.urgent = round_.urgent or urgent
        round_.deadline = now if event.is_private else self._deadline_for(round_, now, round_.hard, round_.urgent)

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
class OpenerFactors:
    """主动冷场开口的独立评分明细。"""

    base: float
    relevance: float
    noise: float = 0.0

    def describe(self) -> str:
        return f"基础{self.base:.2f} 相关+{self.relevance:.2f} 随机{self.noise:+.2f}"


@dataclass
class ReplyDecision:
    """是否回复的结论。``reason`` 记录硬触发类型或主要否决因子。"""

    should_reply: bool
    reason: str = ""
    score: float = 0.0
    threshold: float = 0.0
    hard_trigger: str | None = None
    factors: OpenerFactors | None = None
    effective_talk_value: float | None = None
    # 裁定当时该发送者的画面快照；不读回稍后才完成的视觉结果冒充当时所见。
    visual_note: str = ""

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
    if event.reply_sender_id:
        return event.reply_sender_id == "__bot__"
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
# 会话护栏与主动冷场评分
# ----------------------------------------------------------------------


class DecisionEngine:
    """识别硬触发，按会话维护真实发言的间隔与主动发言窗口。"""

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        aliases: Iterable[str] = _DEFAULT_ALIASES,
        rng: random.Random | None = None,
    ):
        self.config = config or DecisionConfig()
        self.rng = rng or random.Random()
        self.set_aliases(aliases)
        self._last_reply_at: dict[str, float] = {}
        self._active_replies: dict[str, list[float]] = {}
        # 每笔为一次实际发出的跟评；合批中的同人多条原文仍只算一笔。
        self._source_replies: dict[tuple[str, str, str], list[tuple[float, set[str]]]] = {}
        self._source_cooldowns: dict[tuple[str, str, str], float] = {}
        self._next_source_sweep = 0.0

    def _fatigue_enabled(self) -> bool:
        cfg = self.config
        return cfg.fatigue_window_seconds > 0 and cfg.fatigue_reply_limit > 0 and cfg.fatigue_cooldown_seconds > 0

    def note_source_reply(self, event: MessageEvent, events: list[MessageEvent], now: float) -> None:
        """只在本轮实际发言后调用；不把次要发言人的话误记为被跟评对象。"""
        if not event.is_group or not self._fatigue_enabled():
            return
        templates = {t for e in events if e.sender_id == event.sender_id and (t := _template(e.text))}
        if not templates:
            return
        key = (event.platform, event.chat_id, event.sender_id)
        cutoff = now - self.config.fatigue_window_seconds
        if now >= self._next_source_sweep:
            # 人员流动时也淘汰过期正文，不等待同一账号再次出现。
            self._source_replies = {k: rows for k, rows in self._source_replies.items() if rows[-1][0] > cutoff}
            self._next_source_sweep = now + 60
        recent = self._source_replies.get(key, [])
        self._source_replies[key] = [(ts, texts) for ts, texts in recent if ts > cutoff] + [(now, templates)]

    def source_guard(self, event: MessageEvent, now: float) -> str | None:
        """逐个真实来源检查；解除也返回一次状态，以便调用方审计后继续裁定。"""
        if event.sender_id in self.config.muted_senders:
            return "muted_sender"
        if not event.is_group:
            return None
        key = (event.platform, event.chat_id, event.sender_id)
        enabled = self._fatigue_enabled()
        until = self._source_cooldowns.get(key)
        if until is not None:
            if enabled and now < until:
                return "fatigue_cooldown"
            del self._source_cooldowns[key]
            return "fatigue_released"
        if not enabled or key not in self._source_replies:
            return None
        template = _template(event.text)
        if template:
            cutoff = now - self.config.fatigue_window_seconds
            count = sum(1 for ts, texts in self._source_replies[key] if ts > cutoff
                        and any(_similar_template(template, t) for t in texts))
            if count >= self.config.fatigue_reply_limit:
                self._source_cooldowns[key] = now + self.config.fatigue_cooldown_seconds
                del self._source_replies[key]
                return "fatigue_triggered"
        return None

    def set_aliases(self, aliases: Iterable[str]) -> None:
        """替换硬触发的名字/别名（配置热更新用）。"""
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
        """返回硬触发类型；``None`` 表示普通群消息。"""
        if event.is_private:
            return "private"
        if event.mentions_bot:
            return "mentioned"
        if is_reply_to_bot(event):
            return "reply_to_bot"
        # 有明确收件人时，正文里的名字也可能只是话题；把这种情况留给策划判断。
        if any(uid != "all" for uid in event.mentions) or event.reply_sender_id:
            return None
        alias = self.match_alias(event.text)
        return f"name:{alias}" if alias else None

    # ---- 状态 ----

    def min_reply_interval(self) -> float:
        """最小发言间隔（秒）；0 表示关闭。"""
        return max(0.0, float(getattr(self.config, "min_reply_interval_seconds", 0.0) or 0.0))

    @staticmethod
    def chat_key(event: MessageEvent) -> str:
        return f"{event.platform}:{event.chat_id}"

    def note_bot_reply(self, chat_key: str, now: float, *, proactive: bool = False) -> None:
        """成功发出才刷新冷却；只有未被叫到的主动发言占用窗口名额。"""
        self._last_reply_at[chat_key] = now
        if proactive:
            self._active_replies.setdefault(chat_key, []).append(now)

    def active_reply_count(self, chat_key: str, now: float) -> int:
        """返回滚动窗口内的主动发言次数，并顺手淘汰过期时间戳。"""
        window = max(0.0, float(self.config.proactive_window_seconds))
        values = self._active_replies.get(chat_key, [])
        if window <= 0:
            return 0
        values = [value for value in values if now - value < window]
        self._active_replies[chat_key] = values
        return len(values)

    def last_reply_at(self, chat_key: str) -> float | None:
        return self._last_reply_at.get(chat_key)

    def must_reply(self, event: MessageEvent) -> str | None:
        """必回规则：被 @/点名/回复、私聊消息一律必回；返回原因或 None。

        必回覆盖 stage1 的 ``speak=false``：模型可以说「不想接」，但被叫到
        头上还装死就是事故。``private`` 覆盖现在「私聊必回」的语义（本项目
        没有单独的主人账号配置；私聊主人也就是唯一能私聊到 bot 的人）。
        """
        return self.is_hard_trigger(event)

    def guard(
        self,
        event: MessageEvent,
        *,
        now: float,
        proactive: bool = False,
    ) -> ReplyDecision | None:
        """开口前的廉价护栏；返回 None 表示放行。

        - **必回**：@/点名/私聊直接放行，不管间隔与不应期；
        - **不应期**：``refractory_seconds``，仅拦普通消息（``proactive``）；
        - **最小发言间隔**：这个会话刚说过话，间隔内普通消息不开口
          （``min_reply_interval_seconds``，0 表示关闭；放在不应期之后，
          两者同时命中时以更贴切的不应期为准）；
        - **主动发言上限**：滚动窗口内主动发言到顶后拦下（防刷屏）。

        这些都在 stage1 之前做，省下模型调用；不改变已记录的发言状态。
        """
        hard = self.must_reply(event)
        if hard:
            return None
        chat_key = self.chat_key(event)
        cfg = self.config
        if cfg.refractory_enabled and cfg.refractory_seconds > 0 and proactive:
            refractory_at = self._last_reply_at.get(chat_key)
            if refractory_at is not None and now - refractory_at < cfg.refractory_seconds:
                return ReplyDecision(False, "refractory", 0.0, 0.0)
        interval = self.min_reply_interval()
        last = self._last_reply_at.get(chat_key)
        if interval > 0 and last is not None and now - last < interval:
            return ReplyDecision(False, "min_interval", 0.0, 0.0)
        if proactive and cfg.proactive_window_enabled and cfg.proactive_window_limit >= 0:
            if self.active_reply_count(chat_key, now) >= cfg.proactive_window_limit:
                return ReplyDecision(False, "proactive_window", 0.0, 0.0)
        return None

    # ---- 评分 ----

    def evaluate_opener(
        self,
        config: ProactiveConfig,
        *,
        relevance: float = 0.0,
    ) -> ReplyDecision:
        """只给主动冷场开口评分：基础分加话题相关性，阈值附近允许扰动。"""
        relevance_bonus = config.relevance_bonus * min(
            1.0, max(0.0, relevance) / max(1e-6, config.relevance_scale)
        )
        factors = OpenerFactors(base=config.base, relevance=relevance_bonus)
        score = factors.base + factors.relevance
        if config.threshold_noise > 0 and abs(score - config.threshold) <= config.threshold_noise_band:
            factors.noise = self.rng.uniform(-config.threshold_noise, config.threshold_noise)
            score += factors.noise
        should = score >= config.threshold
        return ReplyDecision(
            should,
            "proactive_ok" if should else "proactive_below",
            score,
            config.threshold,
            None,
            factors,
        )
