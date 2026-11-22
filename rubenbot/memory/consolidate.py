"""定期归纳：模型只许提议，代码负责裁决。

- 按人分组，一人一次，输入该人最近 ≤150 条记忆
- 模型只能输出 ``merge`` / ``drop`` 两种操作
- 代码逐条校验，任何一条不过就跳过该提议：
  1. 合并出的新句子不许出现原文没有的词（中文逐字核对，英文逐词核对，允许连接词白名单）
  2. ``source = user`` 的记忆不许 drop
  3. 一轮最多移除 20%（且至少允许 4 条）
  4. 按人分组天然保证不跨人合并
- 每次变更写 ``memory_log``，可用 ``store.restore`` 撤销
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from ..llm import LLMProtocol, parse_json_payload
from .gate import OWNER_AUDIENCE
from ..memory.store import Memory, MemoryStore

logger = logging.getLogger(__name__)

CONSOLIDATE_SYSTEM_PROMPT = """你是一个记忆整理员。你会看到关于某个人的一组记忆条目，请提出整理建议。

你可以提出两种操作：
1. merge：把多条同义或相关的记忆合并成一条更完整的记忆（至少 2 条）
2. drop：删除不是耐久事实的记忆（比如一次性的、临时的、没有信息量的）

**极其重要**：合并后的新句子只能使用原记忆里已经出现过的字词，允许少量连接词。
不得添加任何原记忆中没有的新词、新数字、新地名。宁可少合并，也不要编造。

矛盾时保留新事实并注明变化（例如两条记忆分别说“住上海”和“住北京”，
只能写“住北京（原住上海）”，不能用原记忆没有的字）。

严格输出 JSON 数组，元素形如：
{"op": "merge", "ids": [3, 7], "content": "合并后的完整句子", "category": "fact", "reason": "理由"}
{"op": "drop", "ids": [9], "reason": "理由"}
没有任何建议就输出 []。
"""

# 允许在合并句里新增的连接词（单字）
CONNECTOR_CHARS = set("的了着和与及并而或也就都还又再才更但所因此所以然后并且以及之其于是有为把被让给对")
CONNECTOR_WORDS = {
    "and", "or", "but", "also", "then", "which", "who", "while", "with",
}

_CJK_CHAR_RE = re.compile(r"[㐀-䶿一-鿿]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass
class Proposal:
    op: str
    ids: list[int]
    content: str = ""
    category: str = "fact"
    reason: str = ""


@dataclass
class ConsolidateResult:
    person_id: str
    merged: int = 0
    dropped: int = 0
    rejected: list[str] = field(default_factory=list)
    error: str | None = None
    applied_log_ids: list[int] = field(default_factory=list)


# ----------------------------------------------------------------------
# 校验工具
# ----------------------------------------------------------------------


def vocab_of(memories: Iterable[Memory]) -> tuple[set[str], set[str]]:
    """返回 (中文字集合, 英文/数字词集合)。"""
    chars: set[str] = set()
    words: set[str] = set()
    for mem in memories:
        chars.update(_CJK_CHAR_RE.findall(mem.content))
        words.update(w.lower() for w in _WORD_RE.findall(mem.content))
    return chars, words


def find_new_tokens(content: str, memories: Iterable[Memory]) -> list[str]:
    """找出合并句里原记忆没有的字/词（连接词白名单除外）。"""
    chars, words = vocab_of(memories)
    new_tokens: list[str] = []
    for ch in _CJK_CHAR_RE.findall(content):
        if ch in chars or ch in CONNECTOR_CHARS:
            continue
        new_tokens.append(ch)
    for word in _WORD_RE.findall(content):
        word = word.lower()
        if word in words or word in CONNECTOR_WORDS:
            continue
        # 纯数字必须在原文出现过，防止模型编造年份等
        new_tokens.append(word)
    # 同一 token 只报一次，保持出现顺序
    return list(dict.fromkeys(new_tokens))


def removal_budget(total: int, ratio: float = 0.2, minimum: int = 4) -> int:
    """一轮允许移除的条数：最多 20%，且至少允许 4 条。"""
    return max(minimum, int(total * ratio))


def parse_proposals(raw: str) -> list[Proposal]:
    """解析模型输出，过滤结构不合法的项。"""
    data = parse_json_payload(raw)
    if isinstance(data, dict):
        data = data.get("operations") or data.get("proposals") or []
    if not isinstance(data, list):
        return []
    proposals: list[Proposal] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or item.get("action") or "").strip().lower()
        if op not in {"merge", "drop"}:
            continue
        raw_ids = item.get("ids", item.get("id"))
        if isinstance(raw_ids, (int, str)):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list):
            continue
        ids: list[int] = []
        for value in raw_ids:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not ids:
            continue
        proposals.append(
            Proposal(
                op=op,
                ids=ids,
                content=str(item.get("content") or "").strip(),
                category=str(item.get("category") or "fact").strip().lower(),
                reason=str(item.get("reason") or "").strip(),
            )
        )
    return proposals


# ----------------------------------------------------------------------
# 归纳器
# ----------------------------------------------------------------------


class Consolidator:
    def __init__(self, llm: LLMProtocol, store: MemoryStore, config):
        self.llm = llm
        self.store = store
        self.config = config

    # ---- 触发 ----

    def due_people(self, now: float | None = None) -> list[str]:
        """返回需要归纳的 person_id 列表。"""
        now = time.time() if now is None else now
        cfg = self.config
        last_at = self.store.get_meta_float("consolidate:last_at", 0.0)
        interval_due = (now - last_at) >= cfg.consolidate_interval_hours * 3600
        people: list[str] = []
        for person in self.store.list_people(limit=10000):
            count = self.store.count_memories(person_id=person.person_id, since=last_at)
            if count <= 0:
                continue
            if count >= cfg.consolidate_new_threshold or interval_due:
                people.append(person.person_id)
        return people

    async def maybe_consolidate(self, now: float | None = None, force: bool = False) -> list[ConsolidateResult]:
        """后台任务入口；异常只记录不抛出。"""
        now = time.time() if now is None else now
        if force:
            people = [
                p.person_id for p in self.store.list_people(limit=10000)
                if self.store.count_memories(person_id=p.person_id) > 0
            ]
        else:
            people = self.due_people(now)
        results: list[ConsolidateResult] = []
        for person_id in people:
            try:
                results.append(await self.consolidate_person(person_id, now=now))
            except Exception as exc:  # noqa: BLE001 - 单人失败不影响其他人
                logger.warning("归纳失败（%s）：%s", person_id, exc, extra={"person_id": person_id})
                results.append(ConsolidateResult(person_id=person_id, error=str(exc)))
        # 只有全部成功才推进时间游标；有失败则下一轮还会重试
        if results and all(r.error is None for r in results):
            self.store.set_meta("consolidate:last_at", str(now))
        return results

    # ---- 单人归纳 ----

    async def consolidate_person(self, person_id: str, *, now: float | None = None) -> ConsolidateResult:
        now = time.time() if now is None else now
        memories = self.store.list_memories(
            person_id=person_id, limit=self.config.consolidate_max_memories
        )
        result = ConsolidateResult(person_id=person_id)
        if not memories:
            return result

        raw = await self.llm.complete(
            [
                {"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT},
                {"role": "user", "content": self._render_memories(memories)},
            ],
            role="consolidate",
            task="consolidate",
            temperature=0.2,
            json_mode=True,
        )
        proposals = parse_proposals(raw)
        by_id = {m.id: m for m in memories}
        consumed: set[int] = set()
        removed_so_far = 0
        budget = removal_budget(len(memories), self.config.consolidate_remove_ratio, self.config.consolidate_remove_min)

        for proposal in proposals:
            problem = self._validate(proposal, by_id, consumed)
            if problem:
                result.rejected.append(f"{proposal.op} {proposal.ids}: {problem}")
                continue
            sources = [by_id[i] for i in dict.fromkeys(proposal.ids)]
            # 本轮实际“减少”的记忆条数：drop 全部算，merge 是 N 条并成 1 条
            net_removed = len(sources) if proposal.op == "drop" else len(sources) - 1
            if removed_so_far + net_removed > budget:
                # 超预算的留到下一轮
                result.rejected.append(f"{proposal.op} {proposal.ids}: 超过本轮移除上限 {budget}")
                continue
            if proposal.op == "merge":
                log = self.store.apply_changes(
                    action="merge",
                    delete_ids=[m.id for m in sources],
                    add_specs=[
                        {
                            "person_id": person_id,
                            "scope_type": sources[0].scope_type,
                            "scope_id": sources[0].scope_id,
                            "content": proposal.content,
                            # 带来源的记忆按类目复用分寸裁定，模型不能替它换一个裁定键。
                            "category": sources[0].category if sources[0].audience else proposal.category or sources[0].category,
                            "origin_chat": sources[0].origin_chat,
                            "audience": sources[0].audience,
                            "sensitivity": sources[0].sensitivity,
                            "stability": sources[0].stability,
                            "expires_at": sources[0].expires_at,
                            "source": "extracted",
                            "source_ref": ",".join(str(m.id) for m in sources),
                        }
                    ],
                    reason=proposal.reason,
                    person_id=person_id,
                    now=now,
                )
                result.merged += 1
            else:
                log = self.store.apply_changes(
                    action="drop",
                    delete_ids=[m.id for m in sources],
                    reason=proposal.reason,
                    person_id=person_id,
                    now=now,
                )
                result.dropped += len(sources)
            consumed.update(m.id for m in sources)
            removed_so_far += net_removed
            result.applied_log_ids.append(log.id)
        return result

    def _render_memories(self, memories: list[Memory]) -> str:
        lines = [f"- id={m.id} [{m.category}] {m.content}" for m in memories]
        return "关于这个人的记忆：\n" + "\n".join(lines)

    def _validate(
        self,
        proposal: Proposal,
        by_id: dict[int, Memory],
        consumed: set[int],
    ) -> str | None:
        """返回拒绝原因；None 表示通过。"""
        unique_ids = list(dict.fromkeys(proposal.ids))
        if any(i not in by_id for i in unique_ids):
            return "引用了不存在的记忆 id"
        if any(i in consumed for i in unique_ids):
            return "与前一条提议重复引用"
        sources = [by_id[i] for i in unique_ids]
        if proposal.op == "drop":
            if any(m.source == "user" for m in sources):
                return "user 来源的记忆不可删除"
            return None
        if proposal.op == "merge":
            if len(unique_ids) < 2:
                return "合并至少需要 2 条"
            # 合并会删除源记忆，因此 user 来源的记忆也不参与合并（保守处理）
            if any(m.source == "user" for m in sources):
                return "user 来源的记忆不可合并删除"
            if any(m.audience == OWNER_AUDIENCE for m in sources):
                return "机主自述须保留逐条原话以便追封，不可合并"
            # 合并不能洗掉来源、封存或有效期；边界不一致时宁可保留原记忆。
            boundaries = {(m.audience, m.origin_chat, m.sensitivity, m.scope_type, m.scope_id,
                           m.category if m.audience else None, m.stability, m.expires_at) for m in sources}
            if len(boundaries) != 1:
                return "来源、分寸裁定或有效期不同，不可合并"
            if not proposal.content.strip():
                return "合并结果为空"
            new_tokens = find_new_tokens(proposal.content, sources)
            if new_tokens:
                return f"合并句出现原文没有的词：{''.join(new_tokens)}"
            return None
        return "未知操作"


def format_result(result: ConsolidateResult) -> str:
    return json.dumps(
        {
            "person_id": result.person_id,
            "merged": result.merged,
            "dropped": result.dropped,
            "rejected": result.rejected,
        },
        ensure_ascii=False,
    )
