"""记忆检索与读取侧。

两层内容：

1. **事实层打分**（原有）：不依赖 embedding 的 IDF 稀有词加权。
   分词：英文/数字按整词，中文按字符 bigram（单字句按 unigram）；
   打分：``sum(idf(token))``，稀有词权重高；整句原文命中额外加 bonus。
2. **记忆 v2 读取与注入**：群成员索引（T0）、预拉取与注入（T1）、
   跨会话摘要、recall（T2）。隐私判断全部交给 :mod:`rubenbot.privacy`，
   本模块只负责「按场合筛出候选、按优先级拼装、按预算裁剪」。
"""

from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from functools import partial
from collections import Counter
from dataclasses import dataclass, field
from typing import Collection, Iterable, Sequence

from ..privacy.gate import OWNER_AUDIENCE, PrivacyGate, ReviewItem
from .timeline import search_timeline
from .store import (
    SELF_PERSON_ID,
    Event,
    Memory,
    MemoryStore,
    Profile,
    make_person_id,
)

logger = logging.getLogger(__name__)

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
        """拼装注入上下文所用的记忆（旧接口，保留给插件等调用方）。

        优先级：当前说话人的相关记忆 → 本群 scope 的相关记忆 → 最近记忆兜底。
        记忆 v2 的读取侧（:class:`MemoryReader`）改用 :meth:`score` 并砍掉了
        时间兜底——只注入真命中；这里保留兜底是为了兼容这些旧调用方。
        结果去重，总量不超过 limit。
        """
        # 旧同步接口没有可信的来源/目标场合，也无法等待裁定；保护开启时
        # 调用方必须改用 MemoryReader 的异步读取接口，而不能在这里兜底披露。
        if limit <= 0 or self.store.privacy_enabled:
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


# ======================================================================
# 记忆 v2 读取侧：T0 索引 / T1 预拉取 / 跨会话摘要 / T2 recall
#
# 事实层的 IDF 打分仍用上面的 Retriever；下面是读取与注入的组装层。
# 分层见 SPEC「记忆 v2 设计」：隐私判断全部交给 gate，这里只负责
# 「按场合筛出候选、按优先级拼装、按预算裁剪」。
# ======================================================================

# 记忆区块固定文案：说明这是背景了解、不是谈话素材，并给出提及许可规则
MEMORY_HEADER = """【关于一些人的背景记忆】
这些是你对在场一些人的背景了解，用来帮你听懂他们在聊什么、别踩到人家不爱提的事。
这不是谈话素材：不要复述，不要用「你是一个……的人对吧」这种念档案的句式。
只有当前话题正好 cue 到的时候，才可以自然地带出来一点；对很重的事可以低频主动关心，
但同一件事别反复提。被当面追问别人的私事，不确认也不否认，说「这个你问他自己」。"""

# 私聊跨会话摘要的说明
CROSS_SESSION_HEADER = "他最近在别处聊到过的背景（只是背景，不必主动提起）："

# 自我时间线注入的说明：只放本会话里自己说过/做过的事，提醒别把自己说成别人
SELF_HEADER = "【你自己最近在这里说过/做过的事】（自己的旧话只说明说过，不证明话里的事实或经历）"

# 私聊里被问「你记得我什么」时的行为约束（画像照常注入，这里只给口径）
SELF_INQUIRY_NOTE = (
    "他正在问你还记得他什么：照着你了解的如实回答，有多少说多少，别编；"
    "不用把档案一条条念出来，像聊天一样说就好。"
)
_SELF_INQUIRY_RE = re.compile(
    r"(你(还)?记得我(什么|哪些|啥|多少)|关于我的(记忆|事)|你都(记得|知道)我(什么|哪些|啥)|"
    r"记得我(什么|哪些|啥|多少))"
)

_AT_RE = re.compile(r"@([0-9A-Za-z_\-]+)")
# 话题匹配时的轻量停用字
_STOP_HINT = set("的那了是我你他她它们这有在和也就还吗呢吧啊呀哦嗯")
# 模型/用户在 recall 里指代「当前说话人」的说法
_SELF_QUERIES = {"我", "自己", "本人", "me", "myself", "self"}


def estimate_tokens(text: str) -> int:
    """粗估 token：中日韩字符按 1 个算，其余按 4 字符 1 个算。"""
    text = text or ""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "㐀" <= ch <= "鿿" or "぀" <= ch <= "ヿ")
    return max(1, cjk + (len(text) - cjk + 3) // 4)


def format_relative_time(ts: float, now: float) -> str:
    """把时间戳模糊成「今天/昨天/前天/上周/上个月」级粒度。

    注入记忆时不报精确时间：像人一样只记得「大概什么时候」。
    """
    if not ts:
        return ""
    delta = max(0.0, float(now) - float(ts))
    days = int(delta // 86400)
    if delta < 86400:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 7:
        return f"{days} 天前"
    if days < 14:
        return "上周"
    if days < 31:
        return f"{days // 7} 周前"
    if days < 60:
        return "上个月"
    if days < 365:
        return f"{days // 30} 个月前"
    return "更早之前"


def audience_for_chat(chat_type: str, chat_id: str) -> str:
    """按会话类型判定受众：私聊 ``private``，群聊 ``group:<id>``。

    读取侧与写入侧必须用同一套判定，这里定义唯一实现；``memory.extract``
    再导出同一个函数，调用方不必关心从哪 import。
    """
    if str(chat_type).lower() == "private":
        return "private"
    return f"group:{chat_id}"


def is_self_inquiry(text: str) -> bool:
    """这句是不是在问「你记得我什么」——私聊自查的显性入口。"""
    return bool(_SELF_INQUIRY_RE.search(text or ""))


@dataclass(frozen=True)
class MemberRow:
    """索引里的一行：昵称｜一句话身份｜钩子。"""

    person_id: str
    name: str
    identity: str = ""
    hooks: tuple[str, ...] = ()
    last_ts: float = 0.0

    def render(self) -> str:
        identity = self.identity or "还不太熟"
        hook_text = "、".join(self.hooks[:5])
        parts = [self.name, identity]
        if hook_text:
            parts.append(f"话题：{hook_text}")
        return "｜".join(parts)


class MemberIndex:
    """按群维护的成员索引，带 TTL 缓存。

    重建时从 ``raw_messages`` 找近 N 天在本群活跃的人，再关联 people 与
    profiles 生成索引行。画像 ``updated_at`` 变了会直接让缓存失效。
    """

    def __init__(self, store: MemoryStore, config=None, *, now_fn=time.time):
        self.store = store
        self.config = config
        self.now_fn = now_fn
        # (platform, chat_id) -> (cached_at, profile_signature, rows)
        self._cache: dict[tuple[str, str], tuple[float, float, list[MemberRow]]] = {}

    def invalidate(self, platform: str | None = None, chat_id: str | None = None) -> None:
        """清缓存；给定平台/会话只清那一个，不给就全清。"""
        if platform is None and chat_id is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if (platform is None or key[0] == platform) and (chat_id is None or key[1] == str(chat_id)):
                self._cache.pop(key, None)

    def get(self, platform: str, chat_id: str, *, now: float | None = None) -> list[MemberRow]:
        """取本群索引；缓存命中且未过期、画像也没更新时直接返回。"""
        now = self.now_fn() if now is None else now
        key = (platform, str(chat_id))
        ttl = float(getattr(self.config, "index_ttl_seconds", 600.0))
        signature = self._profile_signature()
        cached = self._cache.get(key)
        if cached is not None:
            cached_at, cached_signature, rows = cached
            if now - cached_at < ttl and signature == cached_signature:
                return rows
        rows = self._build(platform, str(chat_id), now)
        self._cache[key] = (now, signature, rows)
        return rows

    def evidence_of(self, person_id: str) -> list[str]:
        """钩子没有独立证据，索引行只有在整份画像的来源都允许时才能展示。"""
        return [ref for refs in self.store.get_profile_evidence(person_id).values() for ref in refs]

    def _profile_signature(self) -> float:
        """最新一次画像更新时间；任何画像更新都会让索引重建。"""
        try:
            top = self.store.list_profiles(limit=1)
        except Exception:  # noqa: BLE001 - 读不到就当作没变
            return 0.0
        return top[0].updated_at if top else 0.0

    def _build(self, platform: str, chat_id: str, now: float) -> list[MemberRow]:
        days = float(getattr(self.config, "index_active_days", 30.0))
        limit = max(1, int(getattr(self.config, "index_max_members", 40)))
        since = now - days * 86400
        active = self.store.active_senders(platform=platform, chat_id=chat_id, chat_type="group", since=since)
        rows: list[MemberRow] = []
        for entry in active:
            person_id = make_person_id(platform, entry["sender_id"])
            try:
                if self.store.is_norecord(person_id):
                    continue
            except Exception:  # noqa: BLE001
                pass
            person = self.store.get_person(person_id)
            name = entry["sender_name"] or (person.display_name if person else "") or entry["sender_id"]
            profile = self.store.get_profile(person_id)
            identity = ""
            hooks: tuple[str, ...] = ()
            if profile is not None:
                identity = self._identity(profile)
                hooks = tuple(profile.hooks[:5])
            rows.append(
                MemberRow(
                    person_id=person_id,
                    name=name,
                    identity=identity,
                    hooks=hooks,
                    last_ts=entry["last_ts"],
                )
            )
            # 先按最近活跃截断、再筛掉不记模式，避免被跳过的人挤掉活跃名额
            if len(rows) >= limit:
                break
        rows.sort(key=lambda row: row.last_ts, reverse=True)
        return rows

    def _identity(self, profile: Profile) -> str:
        lines = profile.stable_lines
        if not lines:
            return ""
        text = lines[0].strip()
        # 去掉「TA 是」这类主语，索引里读起来更像人话
        for prefix in ("TA 是一名", "TA 是", "TA是一名", "TA是", "TA ", "他", "她"):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
                break
        limit = max(8, int(getattr(self.config, "index_identity_chars", 40)))
        return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class _Candidate:
    """一条待审查、待拼装的候选。"""

    item: ReviewItem
    section: str  # speaker_profile / mentioned_profile / timeline / fact / cross_session
    text: str
    priority: int  # 数字越大越先保留
    score: float = 0.0
    protected: bool = False
    label: str = ""


@dataclass
class MemoryBlock:
    """组装好的记忆区块（渲染成一段文本放进系统提示）。"""

    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    allowed: int = 0
    withheld: int = 0
    trimmed: int = 0
    privacy_enabled: bool = False

    def add(self, title: str, lines: Iterable[str]) -> None:
        lines = [line for line in lines if line]
        if lines:
            self.sections.append((title, lines))

    def render(self) -> str:
        if not self.sections:
            return ""
        body = "\n\n".join(f"{title}\n" + "\n".join(lines) for title, lines in self.sections)
        header = MEMORY_HEADER if self.privacy_enabled else "【背景记忆】这些是过去了解的事，不是当前发言。注意区分说话人和场合。"
        return header + "\n\n" + body

    def __bool__(self) -> bool:
        return bool(self.sections)


class MemoryReader:
    """读取侧总入口：T0 索引、T1 预拉取、跨会话摘要与 T2 recall。"""

    def __init__(
        self,
        store: MemoryStore,
        llm=None,
        config=None,
        *,
        gate: PrivacyGate | None = None,
        retriever: Retriever | None = None,
        now_fn=time.time,
    ):
        self.store = store
        self.llm = llm
        self.config = config
        self.now_fn = now_fn
        self.retriever = retriever or Retriever(store)
        self.gate = gate
        self.index = MemberIndex(store, config, now_fn=now_fn)

    # ------------------------------------------------------------------
    # T1 预拉取 + 注入
    # ------------------------------------------------------------------

    async def assemble(
        self,
        *,
        text: str,
        person_id: str,
        platform: str,
        chat_id: str,
        chat_type: str,
        window_texts: Sequence[str] = (),
        owner_person_ids: Collection[str] = (),
        now: float | None = None,
    ) -> MemoryBlock:
        """组装本轮记忆区块；任何内部失败都退化为尽量少注入，不向上抛。"""
        now = self.now_fn() if now is None else now
        audience_now = audience_for_chat(chat_type, chat_id)
        block = MemoryBlock(privacy_enabled=self.gate is not None)
        candidates: list[_Candidate] = []

        # ---- T0：群成员索引（常驻）。身份/钩子来自画像，也要过审查口：
        # 隐私模式且画像源自私聊的人不会出现在索引里。----
        index_rows: list[MemberRow] = []
        if str(chat_type).lower() != "private":
            try:
                index_rows = self.index.get(platform, chat_id, now=now)
            except Exception as exc:  # noqa: BLE001 - 索引失败不能拖垮回复
                logger.warning("生成群成员索引失败：%s", exc)
                index_rows = []
        for row in index_rows:
            audience, sensitivity = self._refs_context(
                self.index.evidence_of(row.person_id) if self.gate is not None else (), text=row.render()
            )
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=row.person_id,
                        text=row.render(),
                        audience=audience,
                        sensitivity=sensitivity,
                        subject="index",
                        category="index",
                        askable=False,
                    ),
                    section="index",
                    text=f"- {row.render()}",
                    priority=90,
                    label=row.name,
                )
            )

        # ---- 说话人：核心画像 + 与话题相关的旧事（画像 > 时间线 > 事实） ----
        speaker_name = self._display_name(person_id)
        topic = "\n".join([text, *[t for t in window_texts if t]])
        try:
            candidates.extend(
                self._profile_candidates(
                    person_id, now=now, section="speaker_profile", priority=100, protected=True
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取说话人画像失败：%s", exc)
        try:
            candidates.extend(self._timeline_candidates(person_id, topic, now=now, priority=60))
        except Exception as exc:  # noqa: BLE001
            logger.warning("检索说话人时间线失败：%s", exc)

        # ---- 被提及者：@ / 昵称 / 钩子 ----
        mentioned = self._mentioned_people(
            text=text,
            window_texts=window_texts,
            person_id=person_id,
            platform=platform,
            index_rows=index_rows,
            now=now,
        )
        for other_id in dict.fromkeys([*mentioned, *sorted(set(owner_person_ids) - {person_id})]):
            try:
                candidates.extend(
                    self._profile_candidates(
                        other_id,
                        now=now,
                        section="mentioned_profile",
                        priority=80,
                        label=self._display_name(other_id),
                    )
                )
                candidates.extend(self._timeline_candidates(other_id, topic, now=now, priority=50))
                if self.gate is None:
                    candidates.extend(self._fact_candidates(other_id, topic, now=now, priority=20))
                elif other_id in owner_person_ids:
                    candidates.extend(c for c in self._fact_candidates(other_id, topic, now=now, priority=20)
                                      if c.item.audience == OWNER_AUDIENCE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取被提及者记忆失败（%s）：%s", other_id, exc)

        # ---- 事实层：只留真命中 ----
        try:
            candidates.extend(self._fact_candidates(person_id, topic, now=now, priority=20))
        except Exception as exc:  # noqa: BLE001
            logger.warning("检索事实层失败：%s", exc)

        # ---- 自我：bot 自己在本会话的最近行为（自我时间线，同场合，不过审查口） ----
        try:
            candidates.extend(
                self._self_candidates(
                    platform, chat_id, audience_now=audience_now, now=now, priority=70
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取自我时间线失败：%s", exc)

        # ---- 私聊跨会话接续（群聊永不组装） ----
        if self.gate is not None and str(chat_type).lower() == "private":
            try:
                candidates.extend(
                    self._cross_session_candidates(person_id, platform, chat_id, now=now, priority=30)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("组装跨会话摘要失败：%s", exc)

        # ---- 唯一审查口 ----
        if self.gate is None:
            block.allowed = len(candidates)
        elif candidates:
            try:
                allowed, withheld = await self.gate.review(
                    [c.item for c in candidates], audience_now=audience_now, speaker=person_id, now=now,
                    owner_person_ids=owner_person_ids, for_chat=True, target_platform=platform,
                )
            except Exception as exc:  # noqa: BLE001 - gate 自身已 fail closed，这里再兜一层
                logger.warning("分寸审查异常，本轮不注入记忆：%s", exc)
                return block
            allowed_ids = {id(item) for item in allowed}
            candidates = [c for c in candidates if id(c.item) in allowed_ids]
            block.allowed = len(allowed)
            block.withheld = len(withheld)

        block.trimmed = self._apply_budget(candidates)
        self._render(block, candidates, speaker_name=speaker_name)
        # 私聊自查「你记得我什么」：本人画像照常注入，另给一句口径提示
        if block.sections and str(chat_type).lower() == "private" and is_self_inquiry(text):
            block.add("【他在问你记得他什么】", [SELF_INQUIRY_NOTE])
        return block

    def _render(
        self,
        block: MemoryBlock,
        candidates: list[_Candidate],
        *,
        speaker_name: str,
    ) -> None:
        by_section: dict[str, list[_Candidate]] = {}
        owner_lines: dict[tuple[str, str], str] = {}
        for cand in candidates:
            if cand.item.audience == OWNER_AUDIENCE:
                name = self._display_name(cand.item.person_id)
                line = cand.text if cand.section == "timeline" else f"- {name}：{cand.text.removeprefix('- ')}"
                owner_lines.setdefault((cand.item.person_id, cand.item.ref or cand.item.text), line)
        block.add("【机主自述记忆】", owner_lines.values())
        for cand in candidates:
            if cand.item.audience != OWNER_AUDIENCE:
                by_section.setdefault(cand.section, []).append(cand)
        index_lines = [c.text for c in by_section.get("index", [])]
        if index_lines:
            block.add(
                "【本群常驻成员】（昵称｜身份｜话题钩子；只是让你知道谁在这个群里）", index_lines
            )
        speaker_lines = [c.text for c in by_section.get("speaker_profile", [])]
        if speaker_lines:
            block.add(f"关于 {speaker_name}（现在跟你说话的人）：", speaker_lines)
        mentioned_profiles = by_section.get("mentioned_profile", [])
        if mentioned_profiles:
            grouped: dict[str, list[str]] = {}
            for cand in mentioned_profiles:
                grouped.setdefault(cand.label or self._display_name(cand.item.person_id), []).append(
                    # 候选行自带「- 」前缀，拼进同一行时去掉，避免「：- 」
                    cand.text[2:] if cand.text.startswith("- ") else cand.text
                )
            block.add(
                "【这轮提到/聊到的人】",
                [f"- {name}：" + "；".join(lines) for name, lines in grouped.items()],
            )
        timeline_lines = [c.text for c in by_section.get("timeline", [])]
        if timeline_lines:
            block.add("【相关的旧事】（时间已经模糊化，别提具体哪天）", timeline_lines)
        self_lines = [c.text for c in by_section.get("self", [])]
        if self_lines:
            block.add(SELF_HEADER, self_lines)
        fact_lines = [c.text for c in by_section.get("fact", [])]
        if fact_lines:
            block.add("你记得的事：", fact_lines)
        cross_lines = [c.text for c in by_section.get("cross_session", [])]
        if cross_lines:
            block.add(CROSS_SESSION_HEADER, cross_lines)

    # ------------------------------------------------------------------
    # 候选构造
    # ------------------------------------------------------------------

    def _profile_candidates(
        self,
        person_id: str,
        *,
        now: float,
        section: str,
        priority: int,
        protected: bool = False,
        label: str = "",
    ) -> list[_Candidate]:
        """把一个人的画像拆成候选行：stable 全文 + 未过期 status。"""
        profile = self.store.get_profile(person_id)
        if profile is None:
            return []
        evidence = self.store.get_profile_evidence(person_id) if self.gate is not None else {}
        max_chars = max(120, int(getattr(self.config, "profile_inject_max_chars", 600)))
        lines: list[tuple[str, str, list[str]]] = []
        for index, line in enumerate(profile.stable_lines):
            lines.append(("stable", line, evidence.get(("stable", index), [])))
        status_active = profile.status_expires_at is None or profile.status_expires_at > now
        if status_active:
            for index, line in enumerate(profile.status_lines):
                lines.append(("status", line, evidence.get(("status", index), [])))

        candidates: list[_Candidate] = []
        used = 0
        for kind_, text, refs in lines:
            if used >= max_chars:
                break
            remaining = max_chars - used
            trimmed = text if len(text) <= remaining else text[: max(1, remaining - 1)] + "…"
            used += len(trimmed)
            audience, sensitivity = self._refs_context(refs, text=text)
            prefix = "近况：" if kind_ == "status" else ""
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=person_id,
                        text=text,
                        audience=audience,
                        sensitivity=sensitivity,
                        subject="profile",
                        category="profile",
                        kind=kind_,
                    ),
                    section=section,
                    text=f"- {prefix}{trimmed}",
                    priority=priority,
                    protected=protected,
                    label=label,
                )
            )
        return candidates

    def _timeline_candidates(
        self, person_id: str, topic: str, *, now: float, priority: int
    ) -> list[_Candidate]:
        """按话题关键词匹配某人的时间线事件，时间模糊化。"""
        tokens = {tok for tok in tokenize(topic) if tok not in _STOP_HINT}
        if not tokens:
            return []
        limit = max(1, int(getattr(self.config, "timeline_hit_limit", 6)))
        events = self.store.list_events(
            person_id=person_id,
            since=now - 365 * 86400,
            include_compressed=False,
            kinds=("event", "digest"),
            limit=400,
        )
        scored: list[tuple[float, Event]] = []
        for event in events:
            hit = len(tokens & set(tokenize(event.summary)))
            if hit <= 0:
                continue
            scored.append((hit + 0.5 * event.salience, event))
        scored.sort(key=lambda pair: (pair[0], pair[1].ts), reverse=True)
        name = self._display_name(person_id)
        candidates: list[_Candidate] = []
        for score, event in scored[:limit]:
            when = format_relative_time(event.ts, now)
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=person_id,
                        text=event.summary,
                        audience=event.audience,
                        origin=event.source_chat,
                        sensitivity=event.sensitivity,
                        subject=f"event:{event.id}",
                        ref=f"event:{event.id}",
                        kind=event.kind,
                        salience=event.salience,
                    ),
                    section="timeline",
                    text=f"- {name}（{when}）：{event.summary}",
                    priority=priority,
                    score=score,
                )
            )
        return candidates

    def _fact_candidates(
        self, person_id: str | None, topic: str, *, now: float, priority: int
    ) -> list[_Candidate]:
        """事实层只留真命中；已写进人物画像的事实不重复注入。空人物表示全域工具查询。

        - 过期的事实（``expires_at`` 已过，如「明天出差」）不再参与注入；
        - ``uncertain`` 的带一句不确定标注，别让模型当成确凿事实说出去。
        """
        limit = max(0, int(getattr(self.config, "inject_limit", 20)))
        if limit == 0 or not (topic or "").strip():
            return []
        memories = self.store.list_memories(person_id=person_id, limit=300 if person_id else None)
        if not memories:
            return []
        distilled = self.store.profile_evidence_refs(person_id) if person_id else set()
        candidates: list[_Candidate] = []
        for item in self.retriever.score(topic, memories):
            if len(candidates) >= limit:
                break
            mem = item.memory
            if mem.id is not None and f"memory:{mem.id}" in distilled:
                continue  # 已提炼进画像，不重复占预算
            if mem.expires_at is not None and mem.expires_at <= now:
                continue  # 临时事实已过期（如「明天出差」）
            text = mem.content
            if mem.stability == "uncertain":
                text += "（这条不太确定）"
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=mem.person_id,
                        text=mem.content,
                        audience=mem.audience,
                        origin=mem.origin_chat,
                        sensitivity=mem.sensitivity,
                        subject="fact",
                        ref=f"memory:{mem.id}",
                        category=mem.category,
                        kind="memory",
                    ),
                    section="fact",
                    text=f"- {text}" if person_id else f"- {self._display_name(mem.person_id)}：{text}",
                    priority=priority,
                    score=item.score,
                )
            )
        return candidates

    def _cross_session_candidates(
        self, person_id: str, platform: str, chat_id: str, *, now: float, priority: int
    ) -> list[_Candidate]:
        """私聊里的跨会话接续：本人近 24h 在其他会话的事件。

        只取 audience 非 private 的（群聊里说过的公开背景），或本人在私聊里
        自己产生的；组装后仍要过 gate。群聊侧永远不组装这段。
        """
        hours = float(getattr(self.config, "cross_session_hours", 24.0))
        since = now - hours * 3600
        current = f"{platform}:{chat_id}"
        limit = max(1, int(getattr(self.config, "timeline_hit_limit", 6)))
        events = self.store.list_events(
            person_id=person_id, since=since, include_compressed=False, limit=limit * 2
        )
        candidates: list[_Candidate] = []
        for event in events:
            if len(candidates) >= limit:
                break
            if event.source_chat and event.source_chat == current:
                continue  # 当前会话里的内容历史里就有，不重复
            # 这里的人就是本人，事件要么来自别的群（audience=group:*），要么是
            # 本人在私聊里自己说的（audience=private），都属于本人视角可回看的
            # 背景；群聊组装根本走不到这里，私聊来源不会漏到群里。
            when = format_relative_time(event.ts, now)
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=person_id,
                        text=event.summary,
                        audience=event.audience,
                        origin=event.source_chat,
                        sensitivity=event.sensitivity,
                        subject=f"event:{event.id}",
                        ref=f"event:{event.id}",
                        kind=event.kind,
                        salience=event.salience,
                    ),
                    section="cross_session",
                    text=f"- （{when}）{event.summary}",
                    priority=priority,
                    score=event.salience,
                )
            )
        return candidates

    def _self_candidates(
        self,
        platform: str,
        chat_id: str,
        *,
        audience_now: str,
        now: float,
        priority: int,
    ) -> list[_Candidate]:
        """自我注入：bot 自己在本会话时间线里的最近行为（说过的话、做过的决定）。

        「自我」和「别人」不是一件事：这些是 bot 自己的经历，同场合天然可回看，
        不需要过审查口（``person_id='self'``、``audience`` 用当前会话）。
        只取本会话、近 ``self_inject_hours`` 小时、未压缩的事件，最多几条，
        时间同样模糊化。
        """
        hours = float(getattr(self.config, "self_inject_hours", 24.0))
        limit = max(0, int(getattr(self.config, "self_inject_limit", 3)))
        if limit == 0:
            return []
        current = f"{platform}:{chat_id}"
        events = self.store.list_events(
            person_id=SELF_PERSON_ID,
            since=now - hours * 3600,
            include_compressed=False,
            limit=limit * 3,
        )
        candidates: list[_Candidate] = []
        for event in events:
            if event.source_chat != current or (event.audience and event.audience != audience_now):
                continue  # 只回看本会话里自己做过的事
            if len(candidates) >= limit:
                break
            when = format_relative_time(event.ts, now)
            candidates.append(
                _Candidate(
                    item=ReviewItem(
                        person_id=SELF_PERSON_ID,
                        text=event.summary,
                        audience=audience_now,
                        sensitivity=event.sensitivity,
                        subject="self",
                        category="self",
                        ref=f"event:{event.id}",
                        kind=event.kind,
                        askable=False,
                    ),
                    section="self",
                    text=f"- （{when}）{event.summary}",
                    priority=priority,
                    score=event.salience,
                )
            )
        candidates.reverse()  # 时间线按从旧到新读起来更自然
        return candidates

    # ------------------------------------------------------------------
    # 提及者识别
    # ------------------------------------------------------------------

    def _mentioned_people(
        self,
        *,
        text: str,
        window_texts: Sequence[str],
        person_id: str,
        platform: str,
        index_rows: list[MemberRow],
        now: float,
    ) -> list[str]:
        """从本轮窗口里找出被 @、被昵称命中、或被钩子词命中的人。"""
        window = "\n".join([text, *[t for t in window_texts if t]])
        if not window.strip():
            return []
        found: dict[str, float] = {}

        # @uid：适配器把 @ 段渲染成 "@<uid>"
        for uid in _AT_RE.findall(window):
            candidate = make_person_id(platform, uid)
            try:
                if self.store.get_person(candidate) is not None:
                    found.setdefault(candidate, now)
            except Exception:  # noqa: BLE001
                continue

        # 昵称 / 钩子：群聊里除索引里的活跃成员外，也看最近出现过的其他人；
        # 私聊没有常驻索引，但「阿草最近怎么样」这种人名/钩子命中同样该拉画像。
        pool: list[MemberRow] = list(index_rows)
        known = {row.person_id for row in pool}
        for person in self.store.list_people(limit=300):
            if person.person_id in known:
                continue
            profile = self.store.get_profile(person.person_id)
            pool.append(
                MemberRow(
                    person_id=person.person_id,
                    name=person.display_name or person.platform_uid,
                    hooks=tuple(profile.hooks[:5]) if profile else (),
                    last_ts=person.last_seen,
                )
            )

        for row in pool:
            if row.person_id == person_id:
                continue
            names = {row.name, row.person_id.split(":", 1)[-1]}
            if any(name and len(name) >= 2 and name in window for name in names):
                found.setdefault(row.person_id, row.last_ts or now)
                continue
            if any(hook and len(hook) >= 2 and hook in window for hook in row.hooks):
                found.setdefault(row.person_id, (row.last_ts or now) - 1)

        ordered = sorted(found.items(), key=lambda pair: pair[1], reverse=True)
        cap = max(0, int(getattr(self.config, "mentioned_max_people", 3)))
        return [pid for pid, _ in ordered[:cap]]

    # ------------------------------------------------------------------
    # 证据 / 预算
    # ------------------------------------------------------------------

    def _refs_context(self, refs: Sequence[str], *, text: str) -> tuple[str, str]:
        if self.gate is None:
            return "", "normal"
        return self.gate.evidence_context(refs, text=text)

    def timeline_reviewer(self, audience_now, speaker, owner_person_ids, now, *, platform, chat_id):
        """仅开启隐私时才向时间线传入裁定函数，关闭不查裁定、不请求模型。"""
        if self.gate is None:
            return None
        return partial(self.gate.review_timeline, audience_now=audience_now, speaker=speaker,
                       owner_person_ids=owner_person_ids, now=now,
                       target_platform=platform, target_chat_id=chat_id)

    def _apply_budget(self, candidates: list[_Candidate]) -> int:
        """按「画像 > 时间线 > 事实」裁剪；返回被裁掉的条数。

        顺序是硬约定：事实 → 时间线 → 跨会话 → 自我 → 索引 → 被提及者画像；
        说话人核心画像（protected）永远不裁。
        """
        budget = int(getattr(self.config, "inject_budget_tokens", 1200))
        if budget <= 0:
            return 0

        def total() -> int:
            return sum(estimate_tokens(c.text) for c in candidates)

        trimmed = 0
        if total() <= budget:
            return 0
        for section in ("fact", "timeline", "cross_session", "self", "index", "mentioned_profile"):
            if total() <= budget:
                break
            group = sorted(
                (c for c in candidates if c.section == section and not c.protected),
                key=lambda c: c.score,
            )
            for cand in group:
                if total() <= budget:
                    break
                candidates.remove(cand)
                trimmed += 1
        return trimmed

    # ------------------------------------------------------------------
    # T2：recall
    # ------------------------------------------------------------------

    async def recall(
        self, person_query: str = "", topic: str = "", time_range: str = "", *,
        audience_now: str = "unknown", speaker: str = "", owner_person_ids: Collection[str] = (),
        target_platform: str = "", target_chat_id: str = "", now: float | None = None,
    ) -> str:
        """按时间和话题深查全部会话；指定了人物时，另附该人的画像与相关事实。"""
        now = self.now_fn() if now is None else now
        platform = target_platform or self._platform_of(speaker) or ""
        person_id = self.resolve_person(person_query, platform_hint=platform, viewer=speaker)
        if person_query.strip() and person_id is None:
            return f"没找到和「{person_query}」对应的人。"
        if self.gate and person_id and person_id == speaker and person_id not in owner_person_ids:
            if audience_now.startswith("group"):
                return "这个在群里不方便展开，你私聊问我吧。"
            if audience_now == "private" and not topic and not time_range:
                return self.self_report(person_id, now=now)

        since = self._parse_time_range(time_range, now)
        limit = max(1, int(getattr(self.config, "timeline_hit_limit", 6)))
        name = self._display_name(person_id) if person_id else "这段时间线"
        review = self.timeline_reviewer(audience_now, speaker, owner_person_ids, now,
                                        platform=platform, chat_id=target_chat_id)
        hits = await search_timeline(self.store, topic, person_id=person_id, since=since,
                                     limit=limit, allow=review, now=now)
        lines = [hit.line(now=now) for hit in hits]
        if since is None:
            candidates = (self._profile_candidates(person_id, now=now, section="speaker_profile", priority=100)
                          if person_id else [])
            candidates.extend(self._fact_candidates(person_id, topic, now=now, priority=20))
            if candidates and self.gate is not None:
                allowed, _ = await self.gate.review([c.item for c in candidates], audience_now=audience_now,
                                                    speaker=speaker, owner_person_ids=owner_person_ids,
                                                    for_chat=True, target_platform=platform, now=now)
                allowed_ids = {id(item) for item in allowed}
                candidates = [c for c in candidates if id(c.item) in allowed_ids]
            lines.extend(c.text for c in candidates)
        if not lines:
            return f"关于 {name}，没有找到可用的相关记忆。"
        return "\n".join([f"关于 {name} 的记忆：", *lines])

    def self_report(
        self,
        person_id: str,
        *,
        topic: str = "",
        now: float | None = None,
    ) -> str:
        """发问的人就是当事人：「你记得我什么」的如实摊开。

        查看自己的画像不属于跨场合披露，**不走审查口**（否则隐私模式、
        高敏感默认扣下都会让本人看不到自己的东西，与「任何人可私聊查询、
        如实摊开其画像」的规格相反）。格式与核心隐私命令的 ``/隐私`` 一致，
        方便两边对得上。
        """
        now = self.now_fn() if now is None else now
        if not person_id:
            return "我还不知道你是谁。"
        profile = self.store.get_profile(person_id)
        events = self.store.count_events(person_id=person_id)
        memories = self.store.count_memories(person_id=person_id)
        lines = ["我目前记得的关于你的事："]
        if profile is None or (not profile.stable_lines and not profile.status_lines):
            lines.append("（还没有形成画像）")
        else:
            if profile.stable_lines:
                lines.append("· 稳定印象：")
                lines.extend(f"  - {line}" for line in profile.stable_lines)
            status_active = profile.status_expires_at is None or profile.status_expires_at > now
            if profile.status_lines and status_active:
                lines.append("· 最近近况：")
                lines.extend(f"  - {line}" for line in profile.status_lines)
        lines.append(f"另外还有 {events} 条关于你的事件、{memories} 条零散记忆。")
        if (topic or "").strip():
            extra = self._timeline_candidates(person_id, topic, now=now, priority=50)
            if extra:
                lines.append("和这个话题相关的旧事：")
                lines.extend(cand.text for cand in extra[:5])
        return "\n".join(lines)

    def resolve_person(
        self,
        query: str,
        *,
        platform_hint: str | None = None,
        viewer: str = "",
        audience_now: str = "",
    ) -> str | None:
        """把模型给的模糊称呼解析成 person_id：id 精确 → 昵称 → 平台 uid。

        模型常用「我」「自己」指代当前说话人，这里直接还原成 ``viewer``；
        查不到时再按平台/昵称/uid 依次匹配。
        """
        query = (query or "").strip()
        if not query:
            return None
        if viewer and query in _SELF_QUERIES:
            return viewer
        try:
            if self.store.get_person(query) is not None:
                return query
        except Exception:  # noqa: BLE001
            pass
        lowered = query.lower()
        # 全域找人：同名优先本平台，但不能因当前场合不同就找不到别处的说话人。
        people = sorted(self.store.list_people(limit=1000), key=lambda p: p.platform != platform_hint)
        for person in people:
            if query == person.platform_uid or lowered == (person.display_name or "").strip().lower():
                return person.person_id
        for person in people:
            name = (person.display_name or "").strip().lower()
            if name and (lowered in name or name in lowered):
                return person.person_id
        return None

    @staticmethod
    def _parse_time_range(value: str, now: float) -> float | None:
        """解析「7d / 24h / 3天 / 2周 / 1个月」等；不写则查所有留存记录。"""
        text = (value or "").strip().lower()
        if not text:
            return None
        match = re.match(r"^(\d+(?:\.\d+)?)\s*(d|day|days|h|hour|hours|天|日|周|个月|月|年)?$", text)
        if not match:
            return now - 365 * 86400
        number = float(match.group(1))
        unit = match.group(2) or "d"
        seconds = {
            "h": 3600.0,
            "hour": 3600.0,
            "hours": 3600.0,
            "d": 86400.0,
            "day": 86400.0,
            "days": 86400.0,
            "天": 86400.0,
            "日": 86400.0,
            "周": 7 * 86400.0,
            "个月": 30 * 86400.0,
            "月": 30 * 86400.0,
            "年": 365 * 86400.0,
        }.get(unit, 86400.0)
        return now - number * seconds

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _display_name(self, person_id: str) -> str:
        if not person_id:
            return "某个人"
        if person_id == "self":
            return "我"
        try:
            person = self.store.get_person(person_id)
        except Exception:  # noqa: BLE001
            person = None
        return (person.display_name if person and person.display_name else "") or person_id

    @staticmethod
    def _platform_of(person_id: str) -> str | None:
        if not person_id or ":" not in person_id:
            return None
        return person_id.split(":", 1)[0]
