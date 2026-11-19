"""记忆检索：不依赖 embedding 的 IDF 稀有词加权打分。

- 分词：英文/数字按整词，中文按字符 bigram（单字句按 unigram）
- 打分：``sum(idf(token))``，其中 ``idf = log(N/df)``；稀有词权重高
- 整句原文命中额外加 bonus
- 预留 embedding 融合位：传入 ``embed_fn``（query 字符串 -> 向量）和每条记忆的向量即可启用
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .store import Memory

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]+")
# 常见虚词/语气词，作为中文 unigram 时不提供信息量
_STOP_CHARS = set("的了是我你他她它们这那有在和也都就还吗呢吧啊呀哦嗯哈么什怎")
# 英文停用词
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "and", "or", "it", "this", "that", "i", "you", "he", "she", "we", "they",
}


def normalize(text: str) -> str:
    """全角转半角、转小写、压空白。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    """字母/数字整词 + 中文 bigram（单字串退化为 unigram）。"""
    text = normalize(text)
    tokens: list[str] = []
    for word in _WORD_RE.findall(text):
        if word not in _STOP_WORDS:
            tokens.append(word)
    for segment in _CJK_RE.findall(text):
        if len(segment) == 1:
            if segment not in _STOP_CHARS:
                tokens.append(segment)
            continue
        for i in range(len(segment) - 1):
            bigram = segment[i : i + 2]
            if bigram[0] in _STOP_CHARS and bigram[1] in _STOP_CHARS:
                continue
            tokens.append(bigram)
    return tokens


def jaccard(a: str, b: str) -> float:
    """两个句子的 token 集合 Jaccard 相似度。"""
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class ScoredMemory:
    memory: Memory
    score: float


class Retriever:
    """基于 IDF 的记忆检索器。语料即当前所有记忆。"""

    def __init__(self, store):
        self.store = store
        # embedding 融合位：v1 不实现，接口预留给后续阶段
        self.embed_fn = None
        self.embedding_weight = 0.0

    def _corpus(self) -> list[Memory]:
        return self.store.list_memories(newest_first=False)

    def score(
        self,
        query: str,
        memories: list[Memory] | None = None,
        *,
        idf_cache: dict[str, float] | None = None,
    ) -> list[ScoredMemory]:
        """对给定记忆（默认全部）按 query 打分，降序返回正分项。"""
        memories = self._corpus() if memories is None else memories
        if not memories:
            return []
        query = normalize(query)
        q_tokens = tokenize(query)
        if not query:
            return []
        docs = [Counter(tokenize(m.content)) for m in memories]
        total = len(memories)
        if idf_cache is None:
            df: Counter[str] = Counter()
            for doc in docs:
                df.update(set(doc))
            idf_cache = {tok: math.log(1 + total / (1 + n)) for tok, n in df.items()}

        scored: list[ScoredMemory] = []
        for mem, doc in zip(memories, docs):
            score = 0.0
            matched = 0
            for token in set(q_tokens):
                if token in doc:
                    score += idf_cache.get(token, 0.0) * (1 + math.log(1 + doc[token]))
                    matched += 1
            # 覆盖率：防止长句里偶尔命中一个词就被抬高
            coverage = matched / max(1, len(set(q_tokens)))
            score *= 0.5 + 0.5 * coverage
            # 整句原文命中 bonus
            if len(query) >= 2 and query in normalize(mem.content):
                score += 5.0
            if score > 0:
                scored.append(ScoredMemory(memory=mem, score=score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored

    def retrieve(
        self,
        query: str,
        *,
        person_id: str | None = None,
        group_id: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """拼装注入上下文所用的记忆。

        优先级：当前说话人的相关记忆 → 本群 scope 的相关记忆 → 最近记忆兜底。
        结果去重，总量不超过 limit。
        """
        if limit <= 0:
            return []
        pool = self._corpus()
        if not pool:
            return []
        idf_cache = self._idf_cache(pool)
        chosen: list[Memory] = []
        seen: set[int] = set()

        def add(items: list[Memory]) -> None:
            for mem in items:
                if len(chosen) >= limit:
                    return
                if mem.id in seen:
                    continue
                seen.add(mem.id)  # type: ignore[arg-type]
                chosen.append(mem)

        if person_id:
            mine = [m for m in pool if m.person_id == person_id]
            add([s.memory for s in self.score(query, mine, idf_cache=idf_cache)][:limit])

        if len(chosen) < limit:
            group_pool = [
                m for m in pool
                if m.scope_type == "group" and group_id is not None and m.scope_id == str(group_id)
            ]
            add([s.memory for s in self.score(query, group_pool, idf_cache=idf_cache)][:limit])

        if len(chosen) < limit:
            # 最近记忆兜底：按时间顺序补足（越新越靠后，注入时读起来自然）
            add(sorted(pool, key=lambda m: m.created_at)[-limit:])

        return chosen[:limit]

    def _idf_cache(self, pool: list[Memory]) -> dict[str, float]:
        df: Counter[str] = Counter()
        for mem in pool:
            df.update(set(tokenize(mem.content)))
        total = len(pool)
        return {tok: math.log(1 + total / (1 + n)) for tok, n in df.items()}
