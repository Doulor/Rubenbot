"""冷场开口：群里长时间没人说话时，偶尔主动说一句。

默认关闭（``proactive.enabled``），核心理念是**宁可漏，不可扰**：

- 只做群聊，私聊不主动开口；
- 距上一条消息超过 ``idle_hours``、且不在安静时段、且当日未达每群上限，
  才进入意愿评估；
- 意愿评估复用 ``DecisionEngine`` 的评分口径，但用「主动开口」场景的低
  基础分：大多数时候评估不通过，就不开口；
- 评估通过后让模型结合本群近期消息/摘要现写一句，写不出或用罐头句就放弃。

所有判断都是纯函数式的，时间由调用方传入，便于测试逐项覆盖条件矩阵。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .config import ProactiveConfig
from .decision import (
    DecisionEngine,
    ReplyDecision,
    in_quiet_hours,
    parse_generation_output,
    parse_quiet_hours,
)
from .memory.store import BOT_SENDER_ID, MemoryStore, RawMessage

# 主动开口的否决标记：与普通回复的 veto_marker 区分，避免配置改动互相影响
PROACTIVE_VETO = "[不开口]"

# 罐头句黑名单：出现这些就当作「没写出合适的」，放弃本次
_CANNED_MARKERS = (
    "大家都在吗",
    "有人吗",
    "怎么都不说话",
    "冒个泡",
    "有人在吗",
)


@dataclass
class OpenerDecision:
    """一次评估的结果。``decision`` 为 None 表示条件矩阵没过、没进入评分。"""

    decision: ReplyDecision | None
    reason: str

    @property
    def should_open(self) -> bool:
        return bool(self.decision and self.decision.should_reply)


def _day_start(now: float) -> float:
    """本地时区的今天 0 点（epoch 秒）。"""
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


class ProactiveOpener:
    """条件判断 + 意愿评分 + 生成提示；发送由 core 完成（走正常发送通道）。"""

    def __init__(
        self,
        config: ProactiveConfig,
        rng: random.Random | None = None,
        *,
        engine: "DecisionEngine | None" = None,
    ):
        self.config = config
        self.rng = rng or random.Random()
        # 评分复用 DecisionEngine 的「主动开口」场景（低基础分 + 同一套扰动）
        self.engine = engine or DecisionEngine(config=None, rng=self.rng)
        # 每个群的评估冷却与当日开口计数都放内存：进程重启后从零开始，
        # 重启本身已经足够打断「连续开口」的观感，没必要落库。
        self._last_eval: dict[str, float] = {}
        self._opened: dict[str, list[float]] = {}

    # ---- 条件矩阵 ----

    def last_message_at(self, store: MemoryStore, platform: str, chat_id: str) -> float | None:
        """该群最近一条**真实**消息的时间（bot 自己说的话也算，毕竟群里不冷）。"""
        recent = store.recent_messages(chat_id, limit=1, platform=platform, real_only=True)
        return recent[-1].ts if recent else None

    def opened_today(self, platform: str, chat_id: str, now: float) -> int:
        key = f"{platform}:{chat_id}"
        start = _day_start(now)
        stamps = [ts for ts in self._opened.get(key, []) if ts >= start]
        self._opened[key] = stamps
        return len(stamps)

    def blocked_reason(
        self,
        store: MemoryStore,
        platform: str,
        chat_id: str,
        now: float,
        *,
        chat_type: str = "group",
    ) -> str | None:
        """不满足的条件直接返回原因（日志用）；全部满足返回 None。"""
        cfg = self.config
        if not cfg.enabled:
            return "disabled"
        if str(chat_type).lower() != "group":
            return "not_group"
        if in_quiet_hours(now, parse_quiet_hours(cfg.quiet_hours)):
            return "quiet_hours"
        if self.opened_today(platform, chat_id, now) >= cfg.daily_limit:
            return "daily_limit"
        last = self.last_message_at(store, platform, chat_id)
        if last is None or now - last < cfg.idle_hours * 3600:
            return "not_idle"
        return None

    def evaluate(
        self,
        store: MemoryStore,
        platform: str,
        chat_id: str,
        *,
        now: float,
        relevance: float = 0.0,
        relevance_fn: "Callable[[], float] | None" = None,
        chat_type: str = "group",
    ) -> OpenerDecision:
        """完整判断：先过条件矩阵，再做低基础分的意愿评估。

        ``relevance`` 由 core 用本群最近消息检索记忆得出（0 表示没积累）；
        条件矩阵没过时不会调用 ``relevance_fn``，避免对一堆安静不下来的群
        白算检索。
        """
        reason = self.blocked_reason(store, platform, chat_id, now, chat_type=chat_type)
        if reason is not None:
            return OpenerDecision(None, reason)
        key = f"{platform}:{chat_id}"
        last = self._last_eval.get(key)
        if last is not None and now - last < self.config.cooldown_minutes * 60.0:
            return OpenerDecision(None, "cooldown")

        self._last_eval[key] = now
        if relevance_fn is not None:
            relevance = relevance_fn()
        decision = self.score(chat_id, now=now, relevance=relevance)
        return OpenerDecision(decision, decision.reason)

    def score(self, chat_id: str, *, now: float, relevance: float = 0.0) -> ReplyDecision:
        """按「主动开口」场景评分：低基础分 + 话题相关性，不带动量/占比/冷却。

        ``chat_id`` 只作日志/对账口径，评分本身与群无关（每个群都是冷启动）。
        """
        return self.engine.evaluate_opener(self.config, relevance=relevance)

    def note_opened(self, platform: str, chat_id: str, now: float) -> None:
        self._opened.setdefault(f"{platform}:{chat_id}", []).append(now)

    @staticmethod
    def build_prompt(
        *,
        persona: str,
        bot_name: str,
        recent: list[RawMessage],
        summary_block: str,
    ) -> list[dict[str, str]]:
        """让模型结合近期话题现写一句，写不出就输出否决标记。"""
        lines = [persona]
        if summary_block.strip():
            lines.append(summary_block.strip())
        rendered = []
        for msg in recent[-12:]:
            name = bot_name if msg.sender_id == BOT_SENDER_ID else msg.sender_name
            rendered.append(f"[{name}] {msg.text}")
        if rendered:
            lines.append("群里最近的对话：\n" + "\n".join(rendered))
        lines.append(
            f"群里已经安静了挺久。如果你想主动说一句，就结合上面的话题随口聊一句，"
            f"像{bot_name}本人在群里冒个泡：一两句话，不要罐头问候，不要总结陈词。"
            f"想不出合适的话、或者话题已经聊完，就只输出 {PROACTIVE_VETO}，"
            "不要输出任何其他内容。"
        )
        return [{"role": "system", "content": "\n\n".join(lines)}]


def is_acceptable_opener(text: str) -> bool:
    """检查生成的一句是否可用：非空、不是否决标记、不像罐头句。"""
    body = (text or "").strip()
    if not body:
        return False
    if parse_generation_output(body, PROACTIVE_VETO).vetoed:
        return False
    if any(marker in body for marker in _CANNED_MARKERS):
        return False
    return True
