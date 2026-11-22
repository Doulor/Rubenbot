"""从消息流批量抽取：稳定事实（memories）+ 具体事件（events）。

流程：消息先落 raw_messages；后台任务按阈值（条数或会话静默）触发；
对每个有未处理消息的发送者，让模型把其消息分成两类——

- **事件**：发生过的具体事，带时间线索，近原文、不抽象加工（时间线层严格侧）
- **事实**：耐久的稳定事实（画像的原料），保持原有去重

入库前的元数据（来源会话、受众、敏感度、稳定性）由代码按来源会话与模型
标注共同决定；「不记模式」的人在入口处直接跳过。事件/事实的进一步提炼
由 ``distill`` 作业负责（画像），这里只保证原料干净。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..llm import LLMProtocol, parse_json_payload
from ..memory.retrieve import audience_for_chat, jaccard, normalize
from ..privacy.gate import OWNER_AUDIENCE
from ..privacy.sealing import batch_source_ref, register_extraction
from ..memory.owner_statement import is_owner_self_statement
from ..memory.entities import EntityCandidate, EntityLedger
from ..memory.store import (
    VALID_SENSITIVITY,
    VALID_STABILITY,
    MemoryStore,
    PendingSender,
    RawMessage,
    make_person_id,
)

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """你是一个记忆整理员。你会看到某个会话里某个人最近说的话（带上下文），
请把它分成两类信息，只提炼关于「这个人」的内容：

一、facts（耐久事实）：长期有效的稳定信息
- 身份（职业、所在地、年龄段等）
- 稳定的偏好（喜欢/讨厌什么）
- 习惯（作息、爱好）
- 人际关系（家人、朋友、同事等）
- 明确做出的承诺

二、events（具体事件）：发生过的具体事
- 每条一句话，尽量贴近原话，不许抽象加工、不许补充原话没有的细节
- 带时间线索：原话里说了什么时候就填 time_hint（如“昨天下午”“2026-11-18”），
  说不清就留空
- salience 是这件事对这个人的重要程度（0-1）：日常闲聊 0.2~0.4，
  重要的生活事件 0.7~1.0

标注规则（facts 与 events 都要）：
- source_msg_id：该条信息的原始流水 id（[id=...] 的整数，不是平台 id）。
- self_reported：仅当该发言人直接陈述关于自己的事且这条原话足以支持整条结论时为 true。
  转述、引用、他人经历和推测一律 false。自述的事实和事件请沿用原话用词。
- sensitivity：normal 普通；high 涉及健康/财务/感情/身份等敏感类目；
  sealed 当事人明确要求保密（如“别跟别人说”）
- facts 的 stability：stable 长期有效；temporal 临时状态（如“明天出差”），
  同时给 expires_in_days（估计几天后失效，1~90 的整数）；uncertain 拿不准真假

不要记录：
- 一次性的请求或问题、当天的天气、临时情绪、纯语气词
- 对 bot 或他人的评价
- 消息里没有依据的推测

facts 以「TA」作主语，避免「我」。facts 的 category 取
identity / preference / habit / relationship / experience / promise 之一；
scope 为 global（长期有效）或 group（只在当前会话有意义）。

三、entities（会话里的专名账）：只填原话或图片视觉描述中逐字出现的站名、人名、项目名、梗。
- name 是专名原文；gloss 是同一会话原文/视觉描述中已有的一句解释，不能推断；没有解释就留空
- gloss_msg_id 是提供 gloss 的消息 id；message_ids 是出现该实体的消息 id 列表；aliases 只填同一消息与 name 共现的原文别名
- 图片只有在已有视觉描述明确写出专名或解释时才登记，不根据图片内容猜测

严格输出 JSON 对象：
{"facts": [{"content": "...", "category": "identity", "scope": "global",
            "stability": "stable", "expires_in_days": 0, "sensitivity": "normal", "source_msg_id": 123, "self_reported": true}],
 "events": [{"summary": "...", "time_hint": "", "salience": 0.4, "sensitivity": "normal", "source_msg_id": 123, "self_reported": true}],
 "entities": [{"name": "...", "gloss": "", "gloss_msg_id": "", "message_ids": [], "aliases": []}]}
没有内容就输出 {"facts": [], "events": [], "entities": []}。
"""


@dataclass
class ExtractedFact:
    content: str
    category: str = "fact"
    scope_type: str = "global"
    stability: str = "stable"
    expires_in_days: int | None = None
    sensitivity: str = "normal"
    source_msg_id: int | None = None
    self_reported: bool = False


@dataclass
class ExtractedEvent:
    summary: str
    time_hint: str = ""
    ts: float | None = None  # 代码解析后的绝对时间；None 表示用消息时间
    salience: float = 0.5
    sensitivity: str = "normal"
    source_msg_id: int | None = None
    self_reported: bool = False


@dataclass
class Extraction:
    facts: list[ExtractedFact] = field(default_factory=list)
    events: list[ExtractedEvent] = field(default_factory=list)
    entities: list[EntityCandidate] = field(default_factory=list)


@dataclass
class ExtractResult:
    person_id: str
    added: list[str] = field(default_factory=list)
    skipped: int = 0
    added_events: list[str] = field(default_factory=list)
    skipped_events: int = 0
    skipped_norecord: bool = False
    error: str | None = None


class MemoryExtractor:
    """按人批量抽取记忆与事件。"""

    def __init__(self, llm: LLMProtocol, store: MemoryStore, config, *, is_owner=None):
        self.llm = llm
        self.store = store
        self.config = config
        self.is_owner = is_owner

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
        """满足触发条件时执行抽取；异常只记录，不抛出（由调用方兜底）。

        抽取结束后顺带跑写入侧下游（时间线压缩 + 画像提炼），它们各自带触发
        条件与兜底，所以即使没有待抽取消息也会被调用（压缩仍需定期检查）。
        """
        now = time.time() if now is None else now
        pending = self.store.pending_senders()
        # 即使全是不记消息、抽取队列为空，末尾也要照常裁剪短期历史。
        if force:
            chats = sorted({p.chat_id for p in pending})
        else:
            chats = self._chats_to_extract(now) if pending else []
        results: list[ExtractResult] = []
        for chat_id in chats:
            for sender in [p for p in pending if p.chat_id == chat_id]:
                if not force and sender.count < self.config.extract_min_pending:
                    continue
                result = await self.extract_sender(sender, now=now)
                results.append(result)
        self.store.trim_raw(self.config.raw_keep_per_chat)
        await self.run_followups(now=now)
        return results

    async def run_followups(self, *, now: float | None = None) -> None:
        """抽取之后的下游维护：画像提炼（异步）+ 时间线压缩（同步、无 LLM）。

        挂在抽取流程末尾（不新增 core 调用点）；两者各自检查触发条件（阈值/
        间隔），不满足时几乎零成本，任何一侧失败都不影响抽取结果。
        先提炼后压缩：提炼吃的是原始事件，压缩生成的 digest 留给下一轮。
        """
        now = time.time() if now is None else now
        # 局部导入避免与 distill 模块的循环依赖
        from ..memory.distill import ProfileDistiller
        from ..memory.timeline import TimelineCompressor

        try:
            await ProfileDistiller(self.llm, self.store, self.config).maybe_distill(now=now)
        except Exception as exc:  # noqa: BLE001 - 提炼失败不能拖垮抽取
            logger.warning("画像提炼异常：%s", exc)
        try:
            TimelineCompressor(self.store, self.config).run(now=now)
        except Exception as exc:  # noqa: BLE001 - 压缩失败不能拖垮抽取
            logger.warning("时间线压缩异常：%s", exc)

    # ------------------------------------------------------------------
    # 单人抽取
    # ------------------------------------------------------------------

    async def extract_sender(self, sender: PendingSender, *, now: float | None = None) -> ExtractResult:
        now = time.time() if now is None else now
        person_id = make_person_id(sender.platform, sender.sender_id)
        messages = self.store.pending_messages(
            sender.platform, sender.chat_id, sender.sender_id, limit=self.config.extract_window, chat_type=sender.chat_type
        )
        if not messages:
            return ExtractResult(person_id=person_id)
        # 不记模式：不建画像、不进时间线；消息仍标记已处理，避免反复查库
        if self._skip_norecord(person_id, messages):
            return ExtractResult(person_id=person_id, skipped_norecord=True)
        try:
            extraction = await self._ask_model(sender, messages)
        except Exception as exc:  # noqa: BLE001 - 单次抽取失败不应影响其他
            logger.warning("抽取失败（%s）：%s", person_id, exc, extra={"person_id": person_id})
            return ExtractResult(person_id=person_id, error=str(exc))

        # 模型等待期间可能切到不记；在封存登记和任何抽取产物落库前再检查一次。
        if self._skip_norecord(person_id, messages):
            return ExtractResult(person_id=person_id, skipped_norecord=True)
        if len(self.store.available_raw_ids(m.id for m in messages)) != len(messages):
            # 等待模型时本人可能已清空；不能再把旧的异步快照写回去。
            return ExtractResult(person_id=person_id)
        audience = audience_for_chat(sender.chat_type, sender.chat_id)
        origin_chat = f"{sender.platform}:{sender.chat_id}"
        by_id = {m.id: m for m in messages}
        source_ref = batch_source_ref(origin_chat, by_id)
        owner_source = (self.is_owner is not None and not sender.sender_id.startswith("__")
                        and self.is_owner(messages[0]))
        sealed_sources = register_extraction(self.store, messages, [*extraction.facts, *extraction.events])

        def route(item: ExtractedFact | ExtractedEvent, text: str) -> tuple[str, str]:
            source = by_id.get(item.source_msg_id)
            if (source and source.id in sealed_sources) or (not source and sealed_sources):
                return audience, "sealed"
            if (owner_source and item.self_reported and source and item.sensitivity != "sealed"
                    and is_owner_self_statement(source.text, allow_implicit=True) and _grounded(text, source.text)):
                return OWNER_AUDIENCE, item.sensitivity
            return audience, item.sensitivity

        base_ts = messages[-1].ts

        existing = self.store.list_memories(person_id=person_id)
        added: list[str] = []
        skipped = 0
        for fact in extraction.facts:
            duplicate = self._is_duplicate(fact.content, existing)
            if duplicate:
                skipped += 1
                continue
            target, sensitivity = route(fact, fact.content)
            ref = (f"{origin_chat}:{fact.source_msg_id}-{fact.source_msg_id}"
                   if fact.source_msg_id in by_id else source_ref)
            mem = self.store.add_memory(
                person_id,
                fact.content,
                scope_type=fact.scope_type,
                scope_id=sender.chat_id if fact.scope_type == "group" else "",
                category=fact.category,
                source="extracted",
                source_ref=ref,
                origin_chat=origin_chat,
                audience=target,
                sensitivity=sensitivity,
                stability=fact.stability,
                expires_at=self._expiry_ts(fact, base_ts),
                now=now,
            )
            existing.append(mem)
            added.append(mem.content)

        added_events: list[str] = []
        skipped_events = 0
        for event in extraction.events:
            ts = resolve_event_ts(event.time_hint, base_ts) if event.ts is None else event.ts
            if self._event_exists(person_id, event.summary, ts):
                skipped_events += 1
                continue
            target, sensitivity = route(event, event.summary)
            self.store.add_event(
                person_id,
                event.summary,
                ts=ts,
                source_chat=origin_chat,
                source_chat_type=sender.chat_type,
                source_raw_id=event.source_msg_id if event.source_msg_id in by_id else None,
                source_ref=source_ref if event.source_msg_id not in by_id else '',
                audience=target,
                sensitivity=sensitivity,
                salience=event.salience,
                now=now,
            )
            added_events.append(event.summary)

        # 实体结果随原有抽取返回；旧版数组或没有候选时既有存储语义不变。
        if sender.chat_type == "group":
            ledger = EntityLedger(self.store)
            for message in messages:
                ledger.observe_message(platform=sender.platform, chat_id=sender.chat_id,
                                       message=message, candidates=extraction.entities)
            # 同批里刚学到的新名也覆盖其它逐字提及；按消息幂等。
            for message in messages:
                ledger.record_known_mentions(platform=sender.platform, chat_id=sender.chat_id, message=message)
        self.store.mark_extraction_snapshot(messages)
        return ExtractResult(
            person_id=person_id,
            added=added,
            skipped=skipped,
            added_events=added_events,
            skipped_events=skipped_events,
        )

    def _skip_norecord(self, person_id: str, messages: list[RawMessage]) -> bool:
        if not self.store.privacy_enabled or not self.store.is_norecord(person_id):
            return False
        self.store.mark_processed((m.id for m in messages), block_extraction=True)
        logger.info("跳过不记模式的用户：%s", person_id, extra={"person_id": person_id})
        return True

    def _expiry_ts(self, fact: ExtractedFact, base_ts: float) -> float | None:
        """临时事实的过期时间：天数夹到配置区间；非 temporal 一律无过期。"""
        if fact.stability != "temporal":
            return None
        days = fact.expires_in_days
        low = max(1, int(getattr(self.config, "status_expiry_min_days", 1)))
        high = max(low, int(getattr(self.config, "status_expiry_max_days", 90)))
        if days is None:
            days = 3
        days = min(high, max(low, int(days)))
        return base_ts + days * 86400.0

    def _event_exists(self, person_id: str, summary: str, ts: float) -> bool:
        """同一人一天内同一句话（归一化后相同）的事件视为重复。"""
        normalized = normalize(summary)
        if not normalized:
            return True
        for event in self.store.list_events(person_id=person_id, since=ts - 86400, until=ts + 86400):
            if event.kind != "event":
                continue
            if normalize(event.summary) == normalized:
                return True
        return False

    async def _ask_model(self, sender: PendingSender, messages: list[RawMessage]) -> Extraction:
        transcript = "\n".join(
            f"[id={m.id};平台id={m.platform_msg_id or '-'}] [{format_ts(m.ts)}] [{m.sender_name or m.sender_id}] {m.text}"
            + (f" [图片视觉描述：{m.visual_text}]" if m.visual_text else "")
            for m in messages
        )
        user_prompt = (
            f"平台：{sender.platform}\n"
            f"会话：{sender.chat_id}（{'私聊' if sender.chat_type == 'private' else '群聊'}）\n"
            f"要了解的人：{sender.sender_name or sender.sender_id}（id={sender.sender_id}）\n\n"
            f"TA 最近说的话：\n{transcript}\n\n"
            "请只针对这个人，分别输出 facts、events 与 entities，JSON 对象格式。实体必须逐字有原话或视觉描述证据。"
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
        return parse_extraction(raw)

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


# ----------------------------------------------------------------------
# 解析与时间推断
# ----------------------------------------------------------------------


def _grounded(content: str, source: str) -> bool:
    """跨场合只接受近原文：去掉整理用的主语、助词后实词须按原顺序出现。"""
    text = re.sub(r"^(?:机主|本人|ta|他|她|我)(?:说自己|自己)?", "", normalize(content))
    words = re.sub(r"[的了是\W_]", "", text)
    raw = re.sub(r"[的了是我\W_]", "", normalize(source))
    # 不允许从长句里跳过否定词、拼凑分散的字冒充来源。
    return len(words) >= 2 and words in raw and not any(
        word in raw and word not in words for word in ("不", "没", "并非", "never", "not")
    )


def parse_extraction(raw: str) -> Extraction:
    """解析模型输出：支持 {"facts": [...], "events": [...]} 与旧的纯数组。"""
    data = parse_json_payload(raw)
    if isinstance(data, list):
        return Extraction(facts=_parse_facts(data), events=[])
    if not isinstance(data, dict):
        return Extraction()
    facts_data = data.get("facts") or data.get("memories") or []
    events_data = data.get("events") or []
    entities_data = data.get("entities") or []
    return Extraction(
        facts=_parse_facts(facts_data if isinstance(facts_data, list) else []),
        events=_parse_events(events_data if isinstance(events_data, list) else []),
        entities=_parse_entities(entities_data if isinstance(entities_data, list) else []),
    )


def parse_extracted_facts(raw: str) -> list[ExtractedFact]:
    """兼容旧接口：只取事实。"""
    return parse_extraction(raw).facts


def _parse_facts(data: list) -> list[ExtractedFact]:
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
        stability = str(item.get("stability") or "stable").strip().lower()
        if stability not in VALID_STABILITY:
            stability = "stable"
        sensitivity = str(item.get("sensitivity") or "normal").strip().lower()
        if sensitivity not in VALID_SENSITIVITY:
            sensitivity = "normal"
        facts.append(
            ExtractedFact(
                content=content,
                category=category,
                scope_type="group" if scope == "group" else "global",
                stability=stability,
                expires_in_days=_parse_days(item.get("expires_in_days")),
                sensitivity=sensitivity,
                self_reported=item.get("self_reported") is True and str(item.get("sensitivity", "normal")) in VALID_SENSITIVITY,
                source_msg_id=item.get("source_msg_id") if type(item.get("source_msg_id")) is int else None,
            )
        )
    return facts


def _parse_events(data: list) -> list[ExtractedEvent]:
    events: list[ExtractedEvent] = []
    for item in data:
        if isinstance(item, str):
            item = {"summary": item}
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or item.get("content") or "").strip()
        if len(summary) < 2 or len(summary) > 300:
            continue
        sensitivity = str(item.get("sensitivity") or "normal").strip().lower()
        if sensitivity not in VALID_SENSITIVITY:
            sensitivity = "normal"
        try:
            salience = float(item.get("salience", 0.5))
        except (TypeError, ValueError):
            salience = 0.5
        events.append(
            ExtractedEvent(
                summary=summary,
                time_hint=str(item.get("time_hint") or item.get("time") or "").strip(),
                salience=min(1.0, max(0.0, salience)),
                sensitivity=sensitivity,
                self_reported=item.get("self_reported") is True and str(item.get("sensitivity", "normal")) in VALID_SENSITIVITY,
                source_msg_id=item.get("source_msg_id") if type(item.get("source_msg_id")) is int else None,
            )
        )
    return events


def _parse_entities(data: list) -> list[EntityCandidate]:
    """只解析实体候选的事实字段，证据真实性由 EntityLedger 再核验。"""
    entities: list[EntityCandidate] = []
    for item in data:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("canonical_name") or "").strip()
        if not name or len(name) > 120:
            continue
        def _strings(value) -> tuple[str, ...]:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(part).strip() for part in value if str(part).strip())
        entities.append(EntityCandidate(
            name=name,
            gloss=str(item.get("gloss") or "").strip()[:500],
            gloss_msg_id=str(item.get("gloss_msg_id") or "").strip(),
            message_ids=_strings(item.get("message_ids") or item.get("message_id")),
            aliases=_strings(item.get("aliases")),
        ))
    return entities


def _parse_days(value) -> int | None:
    """解析模型给的天数；缺省返回 None（由夹取逻辑给默认值）。"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def format_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


_WEEKDAY_WORDS = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6}
_PERIOD_HOURS = {"凌晨": 3, "早上": 8, "上午": 10, "中午": 12, "下午": 15, "傍晚": 18, "晚上": 20, "夜里": 22}
_TIME_RE = re.compile(r"(\d{1,2})[:：点时](\d{1,2})?分?")
_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_RECENT_DAYS = {
    "今天": 0,
    "今日": 0,
    "昨天": 1,
    "昨日": 1,
    "前天": 2,
}
_DAYS_AGO_RE = re.compile(r"(\d{1,3})\s*天[前以]前?")


def resolve_event_ts(time_hint: str, message_ts: float) -> float:
    """把模型给的时间线索解析成绝对时间；推不出就用消息时间。

    支持：``2026-11-18`` / ``2026/11/18 20:30``、今天/昨天/前天（可带时段与
    钟点）、``N天前``；解析结果离消息时间太远（未来 7 天以外或一年前）时
    回退到消息时间，防止模型瞎编年份。
    """
    hint = (time_hint or "").strip()
    if not hint:
        return message_ts
    base = _local(message_ts)
    ts: float | None = None

    date_match = _DATE_RE.search(hint)
    if date_match:
        year, month, day = (int(part) for part in date_match.groups())
        hour, minute = _clock(hint)
        try:
            ts = datetime(year, month, day, hour, minute).timestamp()
        except ValueError:
            ts = None

    if ts is None:
        days_back: int | None = None
        for word, offset in _RECENT_DAYS.items():
            if word in hint:
                days_back = offset
                break
        if days_back is None:
            ago = _DAYS_AGO_RE.search(hint)
            if ago:
                days_back = int(ago.group(1))
        if days_back is None:
            for word, weekday in _WEEKDAY_WORDS.items():
                if word in hint:
                    # 本周内最近的那个周几（含今天）
                    days_back = (base.weekday() - weekday) % 7
                    break
        if days_back is not None:
            hour, minute = _clock(hint)
            target = (base - timedelta(days=days_back)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            ts = target.timestamp()

    if ts is None:
        return message_ts
    if ts > message_ts + 7 * 86400 or ts < message_ts - 366 * 86400:
        return message_ts
    return float(ts)


def _local(ts: float) -> datetime:
    try:
        return datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return datetime.now()


def _clock(hint: str) -> tuple[int, int]:
    """从时间线索里取钟点；只说「晚上」就按该时段代表时间。"""
    match = _TIME_RE.search(hint)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if ("下午" in hint or "晚上" in hint) and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    for word, hour in _PERIOD_HOURS.items():
        if word in hint:
            return hour, 0
    return 12, 0


# ``audience_for_chat`` 定义在读取侧（retrieve），写入侧与读取侧共用同一份判定；
# 这里继续导出同名符号（见文件顶部的 import），旧调用方无需改动。
