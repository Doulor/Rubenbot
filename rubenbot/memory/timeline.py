"""跨会话时间线：统一读取流水与事件，生成最近背景，检索相关原话。

压缩作业另按远近保留原样、周梗概或高显著度事件。

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
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import datetime

from ..adapters.base import MessageEvent
from ..privacy.gate import OWNER_AUDIENCE
from .store import Event, MemoryStore

logger = logging.getLogger(__name__)

MAX_SNIPPETS = 3
MAX_SNIPPET_CHARS = 160
_PAGE = 128
_FILLER = re.compile(r'你(?:还)?(?:知道|记得)(?:吗|么)?|你能告诉我|请问')


def _clip(text: str, length: int) -> str:
    return text if len(text) <= length else text[:max(0, length - 1)] + '…'


def _one_line(text: str) -> str:
    return ' '.join(text.split())


@dataclass(frozen=True)
class TimelineEntry:
    """一次在场见闻；受众和封存是元数据，不在时间线存取层判定。"""

    kind: str
    id: int
    platform: str
    chat_type: str
    chat_id: str
    chat_name: str
    person_id: str
    speaker: str
    text: str
    ts: float
    audience: str
    sensitivity: str
    source_raw_id: int | None
    entity_blocked: bool

    def excerpt(self, max_chars: int = MAX_SNIPPET_CHARS) -> str:
        return _clip(self.text, max_chars)

    def label(self) -> str:
        try:
            when = datetime.fromtimestamp(self.ts).strftime('%Y-%m-%d %H:%M')
        except (ValueError, OverflowError, OSError):
            when = '时间不详'
        kind = {'group': '群聊', 'private': '私聊'}.get(self.chat_type, '场合')
        ref = f'{self.platform}:{self.chat_id}' if self.platform and self.chat_id else self.audience or '出处不详'
        place = f'{self.chat_name}（{ref}）' if self.chat_name else ref
        return f'[{when}｜{kind} {_clip(_one_line(place), 80)}｜{_clip(_one_line(self.speaker), 40)}]'

    def line(self, max_chars: int = MAX_SNIPPET_CHARS) -> str:
        return f'{self.label()} {self.excerpt(max_chars)}'


AllowBatch = Callable[[list[TimelineEntry]], Awaitable[list[TimelineEntry]]]
_ENTRY_FIELDS = tuple(f.name for f in fields(TimelineEntry))


def read_timeline(store: MemoryStore, *, since: float | None = None, now: float | None = None,
                  person_id: str | None = None, exclude_chat: MessageEvent | None = None,
                  raw_only: bool = False, limit: int = 256, offset: int = 0,
                  prefer_events: bool = False, latest_raw: bool = False,
                  terms: tuple[tuple[str, int], ...] = (), mentioned: Iterable[str] = ()) -> list[TimelineEntry]:
    """读取统一时间线的一页；默认最新在先，有检索词时先比相关性再比时间。"""
    if limit <= 0 or (exclude_chat and (exclude_chat.chat_type not in ('private', 'group') or not exclude_chat.chat_id)):
        return []
    place = (exclude_chat.platform, exclude_chat.chat_type, str(exclude_chat.chat_id)) if exclude_chat else None
    rows = store.timeline_rows(since=since, until=time.time() if now is None else now,
                               person_id=person_id, exclude_chat=place, raw_only=raw_only,
                               prefer_events=prefer_events, latest_raw=latest_raw, terms=terms, mentioned=tuple(mentioned),
                               limit=limit, offset=offset)
    return [TimelineEntry(**{key: row[key] for key in _ENTRY_FIELDS}) for row in rows]


async def recent_timeline(store: MemoryStore, event: MessageEvent, *, hours: float = 24,
                          char_limit: int = 1800, allow: AllowBatch | None = None,
                          now: float | None = None) -> list[str]:
    """本地历史通道之外的近况。已有蒸馏事件的场合不用流水；其余每个场合取一行。"""
    if hours <= 0 or char_limit <= 0:
        return []
    now = time.time() if now is None else now
    lines: list[str] = []
    raw_places: set[tuple[str, str, str]] = set()
    offset = 0
    while True:
        page = read_timeline(store, since=now - hours * 3600, now=now, exclude_chat=event,
                             prefer_events=True, latest_raw=allow is None, limit=_PAGE, offset=offset)
        if not page:
            return lines
        candidates = [r for r in page if r.kind != 'raw' or (r.platform, r.chat_type, r.chat_id) not in raw_places]
        for row in (await allow(candidates) if allow and candidates else candidates):
            place = row.platform, row.chat_type, row.chat_id
            if row.kind == 'raw' and place in raw_places:
                continue
            raw_places.add(place)
            prefix = row.label() + ' '
            line = prefix + _clip(_one_line(row.text), 100 if row.kind == 'raw' else MAX_SNIPPET_CHARS)
            remaining = char_limit - bool(lines)
            if len(line) > remaining:
                # 即便预算很小也不孤零零地截出半个来源标签。
                if remaining > len(prefix) + 1:
                    lines.append(line[:remaining - 1] + '…')
                return lines
            lines.append(line)
            char_limit = remaining - len(line)
        if len(page) < _PAGE or not char_limit:
            return lines
        offset += len(page)


def _query_terms(query: str) -> tuple[tuple[str, int], ...]:
    from .retrieve import normalize, tokenize

    query = _FILLER.sub(' ', normalize(query)).strip()
    # bigram 给较高权重；「吃啥」对「吃的」等说法仍可通过实义单字找到。
    terms = dict.fromkeys(tokenize(query), 4)
    for c in query:
        if '一' <= c <= '鿿' and c not in '的了是我你他她它们这那有在和也都就还吗呢吧啊呀哦嗯哈么什怎得说今啥':
            terms.setdefault(c, 1)
    if len(query) > 1 and terms:
        terms.pop(query, None)
        terms = {query: 8, **terms}
    # 很长的粘贴文本不能展开成上千个 SQL 运算符或绑定参数。
    return tuple(terms.items())[:32]


def _text_key(text: str) -> str:
    from .retrieve import normalize

    return ''.join(c for c in normalize(text) if c.isalnum())


async def search_timeline(store: MemoryStore, query: str, *, exclude_chat: MessageEvent | None = None,
                          exclude_texts: Iterable[str] = (), exclude_raw_ids: Iterable[int | None] = (),
                          raw_only: bool = False, mentioned: Iterable[str] = (),
                          person_id: str | None = None, since: float | None = None,
                          limit: int = 8, allow: AllowBatch | None = None,
                          now: float | None = None) -> list[TimelineEntry]:
    """跨全时间线查人名和话题。不给话题的主动 recall 按时间列出，不截为近 24h。"""
    terms = _query_terms(query)
    mentioned = tuple(mentioned)
    if limit <= 0 or (not terms and not mentioned and (raw_only or query.strip())):
        return []
    now = time.time() if now is None else now
    omitted = {_text_key(text) for text in exclude_texts}
    visible_ids = set(exclude_raw_ids)
    if exclude_chat:
        omitted.update(_text_key(t) for t in (exclude_chat.text, *query.splitlines()))
    result: list[TimelineEntry] = []
    seen = set()
    offset = 0
    while True:
        page = read_timeline(store, since=since, now=now, person_id=person_id, exclude_chat=exclude_chat,
                             raw_only=raw_only, terms=terms, mentioned=mentioned, limit=_PAGE, offset=offset)
        if not page:
            return result
        candidates = [e for e in page if not (e.kind == 'raw' and e.id in visible_ids)
                      and _text_key(e.text) not in omitted]
        for e in (await allow(candidates) if allow and candidates else candidates):
            # 同一句被不同人说过，不能把甲的证词归给乙。
            key = (e.kind, e.person_id, _text_key(e.excerpt()))
            if key not in seen:
                result.append(e)
                seen.add(key)
            if len(result) == limit:
                return result
        if len(page) < _PAGE:
            return result
        offset += len(page)


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
        groups: dict[tuple[str, tuple[int, int], str, str, str], list[Event]] = {}
        for event in candidates:
            # 不同来源不能压成空受众的梗概，否则会把私聊或机主专属事实洗白。
            key = (event.person_id, week_key(event.ts), event.audience, event.source_chat, event.source_chat_type)
            groups.setdefault(key, []).append(event)

        digests = 0
        children = 0
        digest_ids: list[int] = []
        for (person_id, _week, _audience, _source, _type), events in sorted(groups.items(), key=lambda item: (item[0][0], min(e.ts for e in item[1]))):
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
            source_chat_type=events[0].source_chat_type,
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
