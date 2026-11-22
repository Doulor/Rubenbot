"""显式保密与原话来源的封存登记。"""

from __future__ import annotations

from collections import defaultdict
import re
import unicodedata
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    from ..adapters.base import MessageEvent
    from ..memory.extract import ExtractedEvent, ExtractedFact
    from ..memory.store import MemoryStore, RawMessage
    from ..memory.timeline import TimelineEntry


_SECRECY = re.compile(
    r"保密|保守秘密|秘密|不要外[传傳]|别外[传傳]|[勿莫不]外[传傳]|只[跟和对告]你|"
    r"(?:别|別|不要|不能|不许|不许再|勿|禁止).{0,16}(?:说|說|提|讲|講|透露|泄露|告诉|告訴|转发|傳|转述|公開|公开|分享)|"
    r"\b(?:secret|confidential|off the record|between us|keep (?:this|it) private|"
    r"(?:don['’]?t|do not|never) (?:tell|share|repeat|disclose|mention))\b", re.I,
)


def requests_secrecy(text: str) -> bool:
    """按整条原话判断是否明说保密，不能先截断再查。"""
    return bool(_SECRECY.search(unicodedata.normalize('NFKC', text)))


def is_followup_secrecy(text: str) -> bool:
    """独立封口指令针对上一句；带具体新内容的「请保密，我……」不追封前文。"""
    value = unicodedata.normalize('NFKC', text).strip(' \t\r\n。.!！~～')
    return bool(requests_secrecy(value) and re.fullmatch(
        r'(?:请|请你|记得|麻烦你)?(?:刚才|刚刚|之前|前面|前边|上面|我刚说的)?'
        r'(?:说的)?(?:上|前|那|这)?(?:一)?(?:句(?:话)?|条(?:消息)?|件事|个事)?[,， ]*'
        r'(?:也|要|请|请你|务必|帮我|替我|给我|记得)*(?:保密|(?:别|不要|不许)(?:再)?'
        r'(?:外传|转述|转发|(?:跟|和|对)?(?:别人|其他人|任何人)?(?:说|讲|提)|告诉(?:别人|其他人|任何人)))'
        r'(?:哦|噢|哈|啊|呀|吧)?', value))


def source_spans(origin: str, ref: str) -> list[tuple[int, int]]:
    """引用同一来源的一段或多段流水；老合并的记忆 id 列表不是流水证据。"""
    prefix, sep, value = ref.rpartition(':')
    if not sep or prefix != origin or not re.fullmatch(r'\d+-\d+(?:,\d+-\d+)*', value):
        return []
    spans = []
    for part in value.split(','):
        first, last = map(int, part.split('-'))
        spans.append((min(first, last), max(first, last)))
    return spans


def merged_source_ref(origin: str, refs: Iterable[str]) -> str:
    """合并只并来源区间，不把间隔里的其它发言算作证据。"""
    spans = []
    for ref in refs:
        parts = source_spans(origin, ref)
        if not parts:
            return ''  # 证据不全，不能给出看似完整的区间
        spans.extend(parts)
    return _format_spans(origin, spans)


def batch_source_ref(origin: str, ids: Iterable[int]) -> str:
    return _format_spans(origin, [(rid, rid) for rid in ids])


def _format_spans(origin: str, spans: list[tuple[int, int]]) -> str:
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return origin + ':' + ','.join(f'{lo}-{hi}' for lo, hi in merged) if merged else ''


def register_message(store: MemoryStore, event: MessageEvent | RawMessage, raw_id: int) -> None:
    """在原话落库时立即登记，抽取还未开始也不会漏出封存内容。"""
    if requests_secrecy(event.text):
        store.seal_statement(event.platform, event.chat_id, event.chat_type, event.sender_id, raw_id,
                             followup=is_followup_secrecy(event.text))


def register_extraction(store: MemoryStore, messages: Sequence[RawMessage],
                        items: Iterable[ExtractedFact | ExtractedEvent]) -> set[int]:
    """收拢封存的原话证据，兼容旧库/直接导入的流水与模型漏标。"""
    ids = {m.id for m in messages}
    for msg in messages:
        register_message(store, msg, msg.id)
    sealed = ids - store.available_raw_ids(ids, unblocked=True)
    blocked = set()
    for item in items:
        if item.sensitivity in ('high', 'sealed'):
            evidence = {item.source_msg_id} if item.source_msg_id in ids else ids
            blocked.update(evidence)
            if item.sensitivity == 'sealed':
                sealed.update(evidence)
    if blocked:
        store.block_raw_recall(blocked)
    return sealed


def restricted_entries(store: MemoryStore, entries: Sequence[TimelineEntry]) -> set[int]:
    """按证据主键检查旧库封存与单独发出的追封指令，不依赖它们也命中本次检索。"""
    by_person = defaultdict(list)
    for e in entries:
        if e.kind == 'raw':
            by_person[e.person_id].append(e)
    blocked = set()
    for person, rows in by_person.items():
        sources = store.restricted_raw_sources(person, since=0)
        for chat, ref, created, site in sources:
            origin = chat or ref.rpartition(':')[0]
            spans = source_spans(origin, ref)
            for row in rows:
                if (origin and origin != f'{row.platform}:{row.chat_id}') or (site and site != row.chat_type):
                    continue
                matches = any(lo <= row.id <= hi for lo, hi in spans) if spans else row.ts <= created
                if matches:
                    blocked.add(row.id)
    ids = [e.id for rows in by_person.values() for e in rows if e.id not in blocked]
    blocked.update(rid for rid, text in store.following_sender_texts(ids) if text and is_followup_secrecy(text))
    return blocked
