"""即时检索机主在别处说过的原话；只返回摘录，不调用模型、不修改记忆。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from ..adapters.base import MessageEvent
from ..computer import is_host
from ..config import ComputerConfig
from .gate import OWNER_AUDIENCE, PrivacyGate, ReviewItem, has_high_sensitivity_hint
from .owner_statement import is_followup_secrecy, is_owner_self_statement
from .retrieve import audience_for_chat, normalize, tokenize
from .store import MemoryStore, RawMessage, make_person_id

MAX_SNIPPETS = 3
MAX_SNIPPET_CHARS = 160
_WINDOW = 24 * 3600
_CANDIDATES = 256
_AT = re.compile(r'@[\w:.-]+\s+')
_FILLER = re.compile(r'你(?:还)?(?:知道|记得)(?:吗|么)?|你能告诉我|请问')


def _text_key(text: str) -> str:
    return ''.join(c for c in _AT.sub('', normalize(text)) if c.isalnum())


def _keywords(text: str) -> set[str]:
    return set(tokenize(_FILLER.sub(' ', _AT.sub('', normalize(text)))))


def _restricted_ids(rows: list[RawMessage], sources: list[tuple[str, str, float]]) -> set[int]:
    """新数据在抽取时逐条屏蔽；旧库按范围补筛，无原话索引则不泄露入库前的同源候选。"""
    blocked = set()
    for chat, ref, created in sources:
        match = re.fullmatch(r'(.+):(\d+)-(\d+)', ref)
        if match and (not chat or match[1] == chat):
            first, last = int(match[2]), int(match[3])
            blocked.update(r.id for r in rows if first <= r.id <= last and match[1] == f'{r.platform}:{r.chat_id}')
        else:
            blocked.update(r.id for r in rows if r.ts <= created and (not chat or chat == f'{r.platform}:{r.chat_id}'))
    return blocked


def owner_recent_snippets(
    store: MemoryStore, event: MessageEvent, *, computer: ComputerConfig,
    query: str | None = None, now: float | None = None, exclude_texts: Iterable[str] = (),
) -> list[str]:
    """按本批真实机主事件查其他场合的相关原话；空列表表示无可用依据。

    ``query`` 可合并同一机主的多条提问，``exclude_texts`` 传本批原句以免回显。
    只在最近一天的最新 256 条跨会话流水中排名；同分优先较新原话。
    """
    if not is_host(computer, event) or event.chat_type not in ('private', 'group') or not event.chat_id:
        return []
    query = event.text if query is None else query
    words = _keywords(query)
    if not words:
        return []
    excluded = {_text_key(t) for t in (event.text, *query.splitlines(), *exclude_texts)}
    now = time.time() if now is None else now
    person_id = make_person_id(event.platform, event.sender_id)
    rows = store.recent_sender_messages_elsewhere(
        event.platform, event.sender_id, chat_type=event.chat_type, chat_id=event.chat_id,
        since=now - _WINDOW, until=now, limit=_CANDIDATES,
    )
    if not rows:
        return []
    # 原话没有可信类目。只按「这个人 × 当前受众 × 敏感度」审，不对每行重复查同一裁定。
    audience = audience_for_chat(event.chat_type, event.chat_id)
    gate = PrivacyGate(store, None)
    allowed: dict[str, bool] = {}
    blocked = _restricted_ids(rows, store.restricted_raw_sources(person_id, since=now - _WINDOW))
    # 旧库没有持久化的追封标记。用真实流水 id 定位对象，不能把前一条仍可见的原话当成对象。
    blocked.update(store.preceding_sender_raw_ids(
        [r.id for r in rows if r.chat_type == 'private' and is_followup_secrecy(r.text)]))
    ranked = []
    for row in rows:
        if row.id in blocked or not is_owner_self_statement(row.text) or _text_key(row.text) in excluded:
            continue
        text = ' '.join(row.text.split())
        snippet = text if len(text) <= MAX_SNIPPET_CHARS else text[:MAX_SNIPPET_CHARS - 1] + '…'
        score = len(words & _keywords(snippet))
        if not score:
            continue
        sensitivity = 'high' if has_high_sensitivity_hint(row.text) else 'normal'
        if sensitivity not in allowed:
            item = ReviewItem(person_id=person_id, audience=OWNER_AUDIENCE, kind='raw', subject='raw',
                              sensitivity=sensitivity, askable=False)
            allowed[sensitivity] = gate.owner_verdict(item, audience, {person_id}, now=now)
        if allowed[sensitivity]:
            ranked.append((score, snippet))
    hits: list[str] = []
    for _, snippet in sorted(ranked, key=lambda item: -item[0]):
        key = _text_key(snippet)
        if key in excluded:
            continue
        hits.append(snippet)
        excluded.add(key)
        if len(hits) == MAX_SNIPPETS:
            break
    return hits
