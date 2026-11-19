"""从消息流批量抽取耐久事实。

流程：消息先落 raw_messages；后台任务按阈值（条数或会话静默）触发；
对每个有未处理消息的发送者，让模型从其消息里提炼「关于这个人的耐久事实」，
解析 JSON、与已有记忆做精确/近似去重，然后入库并标记消息已处理。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..llm import LLMProtocol, parse_json_payload
from ..memory.retrieve import jaccard, normalize
from ..memory.store import MemoryStore, PendingSender, RawMessage, make_person_id

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """你是一个记忆整理员。你会看到某个群聊里某个人最近说的话（带上下文），
请从中提炼关于「这个人」的**耐久事实**，写成简短的中文陈述句。

只保留：
- 身份（职业、所在地、年龄段等）
- 稳定的偏好（喜欢/讨厌什么）
- 习惯（作息、爱好、常用口头禅之外的稳定行为）
- 人际关系（家人、朋友、同事等）
- 重要经历（发生过的事、计划中的事）
- 明确做出的承诺

不要保留：
- 一次性的请求或问题（“帮我查一下”“今天吃什么”）
- 当天的天气、临时情绪、纯语气词
- 对 bot 或他人的评价
- 消息里没有依据的推测

每条事实以「TA」作主语，避免使用「我」。事实应当长期有效。

严格输出 JSON 数组，每个元素形如：
{"content": "TA 是杭州的一名后端工程师", "category": "identity", "scope": "global"}
category 取 identity / preference / habit / relationship / experience / promise 之一。
scope 为 global（长期有效）或 group（只在当前群有意义）。
没有值得记录的事实就输出 []。
"""


@dataclass
class ExtractedFact:
    content: str
    category: str = "fact"
    scope_type: str = "global"


@dataclass
class ExtractResult:
    person_id: str
    added: list[str]
    skipped: int = 0
    error: str | None = None


class MemoryExtractor:
    """按人批量抽取记忆。"""

    def __init__(self, llm: LLMProtocol, store: MemoryStore, config):
        self.llm = llm
        self.store = store
        self.config = config

    # ------------------------------------------------------------------
    # 触发判断
    # ------------------------------------------------------------------

    def chat_pending_count(self, chat_id: str) -> int:
        return sum(p.count for p in self.store.pending_senders() if p.chat_id == str(chat_id))

    def _chats_to_extract(self, now: float) -> list[str]:
        """找出满足触发条件的会话：未处理消息 ≥ N，或会话已静默足够久。"""
        cfg = self.config
        by_chat: dict[str, int] = {}
        for sender in self.store.pending_senders():
            by_chat[sender.chat_id] = by_chat.get(sender.chat_id, 0) + sender.count
        chosen: list[str] = []
        for chat_id, count in by_chat.items():
            if count >= cfg.extract_every_n:
                chosen.append(chat_id)
                continue
            last_ts = self.store.last_message_ts(chat_id)
            if last_ts is not None and now - last_ts >= cfg.extract_silence_seconds:
                chosen.append(chat_id)
        return chosen

    async def maybe_extract(self, *, now: float | None = None, force: bool = False) -> list[ExtractResult]:
        """满足触发条件时执行抽取；异常只记录，不抛出（由调用方兜底）。"""
        now = time.time() if now is None else now
        pending = self.store.pending_senders()
        if not pending:
            return []
        if force:
            chats = sorted({p.chat_id for p in pending})
        else:
            chats = self._chats_to_extract(now)
        results: list[ExtractResult] = []
        for chat_id in chats:
            for sender in [p for p in pending if p.chat_id == chat_id]:
                if not force and sender.count < self.config.extract_min_pending:
                    continue
                result = await self.extract_sender(sender, now=now)
                results.append(result)
        self.store.trim_raw(self.config.raw_keep_per_chat)
        return results

    # ------------------------------------------------------------------
    # 单人抽取
    # ------------------------------------------------------------------

    async def extract_sender(self, sender: PendingSender, *, now: float | None = None) -> ExtractResult:
        now = time.time() if now is None else now
        person_id = make_person_id(sender.platform, sender.sender_id)
        messages = self.store.pending_messages(
            sender.platform, sender.chat_id, sender.sender_id, limit=self.config.extract_window
        )
        if not messages:
            return ExtractResult(person_id=person_id, added=[])
        try:
            facts = await self._ask_model(sender, messages)
        except Exception as exc:  # noqa: BLE001 - 单次抽取失败不应影响其他
            logger.warning("抽取失败（%s）：%s", person_id, exc)
            return ExtractResult(person_id=person_id, added=[], error=str(exc))

        existing = self.store.list_memories(person_id=person_id)
        added: list[str] = []
        skipped = 0
        for fact in facts:
            duplicate = self._is_duplicate(fact.content, existing)
            if duplicate:
                skipped += 1
                continue
            mem = self.store.add_memory(
                person_id,
                fact.content,
                scope_type=fact.scope_type,
                scope_id=sender.chat_id if fact.scope_type == "group" else "",
                category=fact.category,
                source="extracted",
                source_ref=f"{sender.platform}:{sender.chat_id}:{messages[0].id}-{messages[-1].id}",
                now=now,
            )
            existing.append(mem)
            added.append(mem.content)
        # 无论有没有新事实，这批消息都算处理过，避免反复请求模型
        self.store.mark_processed([m.id for m in messages])
        return ExtractResult(person_id=person_id, added=added, skipped=skipped)

    async def _ask_model(self, sender: PendingSender, messages: list[RawMessage]) -> list[ExtractedFact]:
        transcript = "\n".join(f"[{m.sender_name or m.sender_id}] {m.text}" for m in messages)
        user_prompt = (
            f"平台：{sender.platform}\n"
            f"会话：{sender.chat_id}（{'私聊' if sender.chat_type == 'private' else '群聊'}）\n"
            f"要了解的人：{sender.sender_name or sender.sender_id}（id={sender.sender_id}）\n\n"
            f"TA 最近说的话：\n{transcript}\n\n"
            "请只提炼关于这个人的耐久事实，输出 JSON 数组。"
        )
        raw = await self.llm.complete(
            [
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            role="extract",
            task="extract",
            temperature=0.2,
            json_mode=True,
        )
        return parse_extracted_facts(raw)

    def _is_duplicate(self, content: str, existing) -> bool:
        """精确重复直接跳过；token 重合度 ≥ 阈值也跳过。"""
        normalized = normalize(content)
        if not normalized:
            return True
        for mem in existing:
            if normalize(mem.content) == normalized:
                return True
            if jaccard(content, mem.content) >= self.config.dedupe_threshold:
                return True
        return False


VALID_CATEGORIES = {"identity", "preference", "habit", "relationship", "experience", "promise", "fact"}


def parse_extracted_facts(raw: str) -> list[ExtractedFact]:
    """解析模型输出；兼容数组或 {"facts": [...]}，并过滤空项/非法项。"""
    data = parse_json_payload(raw)
    if isinstance(data, dict):
        data = data.get("facts") or data.get("memories") or []
    if not isinstance(data, list):
        return []
    facts: list[ExtractedFact] = []
    for item in data:
        if isinstance(item, str):
            item = {"content": item}
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("fact") or "").strip()
        if len(content) < 2 or len(content) > 200:
            continue
        category = str(item.get("category") or "fact").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "fact"
        scope = str(item.get("scope") or "global").strip().lower()
        facts.append(
            ExtractedFact(
                content=content,
                category=category,
                scope_type="group" if scope == "group" else "global",
            )
        )
    return facts
