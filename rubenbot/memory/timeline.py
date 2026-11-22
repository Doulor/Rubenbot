"""时间线压缩：近的保原样，老的压成周梗概，再老只留高显著度。

规则（见 SPEC「时间线层」）：
- ``compress_after_days``（默认 7 天）前、还没压缩过的事件，按人按自然周
  合并成一条 digest 事件；digest 的 salience 取子事件最大值，子事件标
  ``compressed=1``（只改标志，不删数据，需要时仍可回查原文）。
- ``prune_after_days``（默认 90 天）前只保留 salience ≥ 阈值的普通事件与
  digest，其余标记 compressed。

digest 的梗概由代码拼接子事件摘要得到——不让模型二次概括，避免编造，
也保证任务不依赖 LLM（失败面最小）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..memory.gate import OWNER_AUDIENCE
from ..memory.store import Event, MemoryStore

logger = logging.getLogger(__name__)

# 敏感度比较：sealed 最高
_SENSITIVITY_RANK = {"normal": 0, "high": 1, "sealed": 2}


@dataclass
class CompressResult:
    digests: int = 0
    compressed_children: int = 0
    pruned: int = 0
    digest_ids: list[int] = field(default_factory=list)
    error: str | None = None


class TimelineCompressor:
    """时间线压缩作业。``run`` 异常只记录，绝不抛出。"""

    def __init__(self, store: MemoryStore, config):
        self.store = store
        self.config = config

    def run(self, now: float | None = None) -> CompressResult:
        now = time.time() if now is None else now
        result = CompressResult()
        try:
            result.digests, result.compressed_children, result.digest_ids = self._compress_weeks(now)
        except Exception as exc:  # noqa: BLE001 - 压缩失败不影响其他工作
            logger.warning("时间线周压缩失败：%s", exc)
            result.error = str(exc)
        try:
            cutoff = now - float(getattr(self.config, "timeline_prune_after_days", 90.0)) * 86400
            result.pruned = self.store.prune_events(
                cutoff, keep_salience=float(getattr(self.config, "timeline_prune_keep_salience", 0.7))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("时间线老事件裁剪失败：%s", exc)
            result.error = result.error or str(exc)
        return result

    # ------------------------------------------------------------------

    def _compress_weeks(self, now: float) -> tuple[int, int, list[int]]:
        cutoff = now - float(getattr(self.config, "timeline_compress_after_days", 7.0)) * 86400
        candidates = [
            event
            for event in self.store.list_events(until=cutoff, include_compressed=False, newest_first=False)
            # 机主事件保留原 id 和逐条来源，不能用 digest 的新键绕过 deny 或事后封存。
            if event.kind == "event" and event.audience != OWNER_AUDIENCE
        ]
        groups: dict[tuple[str, tuple[int, int], str, str], list[Event]] = {}
        for event in candidates:
            # 不同来源不能压成空受众的梗概，否则会把私聊或机主专属事实洗白。
            key = (event.person_id, week_key(event.ts), event.audience, event.source_chat)
            groups.setdefault(key, []).append(event)

        digests = 0
        children = 0
        digest_ids: list[int] = []
        for (person_id, _week, _audience, _source), events in sorted(groups.items(), key=lambda item: (item[0][0], min(e.ts for e in item[1]))):
            if not events:
                continue
            child_ids = [int(event.id) for event in events if event.id is not None]
            digest = self._make_digest(person_id, events, now)
            if digest.id is not None:
                digest_ids.append(digest.id)
                digests += 1
            children += self.store.mark_events_compressed(child_ids)
        return digests, children, digest_ids

    def _make_digest(self, person_id: str, events: list[Event], now: float) -> Event:
        first, last = min(e.ts for e in events), max(e.ts for e in events)
        period = f"{day_label(first)}~{day_label(last)}" if day_label(first) != day_label(last) else day_label(first)
        body = "；".join(event.summary for event in events)
        summary = f"{period}：{body}"
        if len(summary) > 500:
            summary = summary[:499] + "…"
        chats = {event.source_chat for event in events}
        audiences = {event.audience for event in events}
        sensitivity = max(
            (event.sensitivity for event in events),
            key=lambda value: _SENSITIVITY_RANK.get(value, 0),
        )
        return self.store.add_event(
            person_id,
            summary,
            ts=last,
            source_chat=chats.pop() if len(chats) == 1 else "",
            audience=audiences.pop() if len(audiences) == 1 else "",
            sensitivity=sensitivity,
            salience=max(event.salience for event in events),
            kind="digest",
            now=now,
        )


def week_key(ts: float) -> tuple[int, int]:
    """自然周键（ISO 年, ISO 周）；失败时退化为按 7 天分桶。"""
    try:
        iso = datetime.fromtimestamp(ts).isocalendar()
        return int(iso[0]), int(iso[1])
    except (OverflowError, OSError, ValueError):
        return 0, int(ts // (7 * 86400))


def day_label(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"
