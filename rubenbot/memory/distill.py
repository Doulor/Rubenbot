"""画像提炼（沉淀流水线）：把时间线事件与稳定事实炼成「画像」。

输入：现有画像 + 上次提炼之后的新事件与新事实。
输出：更新后的 ``stable_text`` / ``status_text`` / ``hooks``。

分层校验：
- 时间线（事件摘要）严格近原文；画像允许抽象，靠证据链防编造——
  **每行结论必须带 evidence_refs，指向本次输入里的 event/memory id，
  没有有效证据的行代码侧直接丢弃**。
- 与旧画像矛盾时，新行替换旧行，并在 ``memory_log`` 留痕（``profile_update``）。
- ``status`` 只收时效性近况，模型给估计天数，代码夹到配置区间。

「输入」包括新事件、新事实，以及现有画像里已经挂着的历史证据 id（它们会
随画像一起出现在提示词里）——模型保留仍有效的旧行时可以直接沿用旧证据，
但不能凭空空造证据 id。

触发沿用归纳节奏：新事件+事实达阈值，或距上次提炼超过间隔。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from ..llm import LLMProtocol, parse_json_payload
from ..memory.anchor import annotate, evaluate
from ..memory.retrieve import jaccard
from ..memory.store import DistillInput, Event, Memory, MemoryStore, Profile, ProfileLine

logger = logging.getLogger(__name__)

DISTILL_SYSTEM_PROMPT = """你是一个人物画像整理员。你会看到关于某个人的：
- 现有画像（稳定特性 stable / 当前近况 status）
- 上次提炼之后的新时间线事件（id 以 event: 开头）
- 上次提炼之后的新事实（id 以 memory: 开头）

请据此给出这个人的**新画像**，分两节：

1. stable（稳定特性）：职业、住址、性格、长期偏好、人际关系等长期不变的东西。
   重复出现的行为可以抽象成习惯（画像侧允许抽象，但必须有依据）。
2. status（当前近况）：近期状态、临时安排、正在做的事，只收有时效性的。
   每条给 expires_in_days：估计多少天后失效（1~90 的整数）。

**每一行都必须带 evidence**：列出支撑这句话的输入 id 列表，形如
["event:12", "memory:34"]。只能引用输入里真实存在的 id；没有证据的支持
宁可不要写这一行。**不许编造输入里没有的事实。**

**每一行也必须写清所指的实体**：职业、住址这类自带主语的句子不用另填；
「对某个东西/平台/项目的评价」必须把名字写进正文（如「认为 Claude Code 推单向
消息不错」），并在 entity 填同一个专名。名字只能来自这条行的 evidence 原文里
逐字出现过的专名，不能用「它」「这个东西」「那个平台」代替主语，也不要凭常识补全
（输入没点名就不写这一行，代码会按指称不完整丢掉并记一条丢弃记录）。
现有画像里仍然有效的评价行，正文已经带「关于「名字」：」前缀的，照原样保留，
entity 也照抄。

与现有画像矛盾时（比如现画像说在杭州、新事件说搬到北京），
用新结论替换旧结论，不要两句都写。现有画像里**仍然有效的行要原样保留**，
并把原来的 evidence 一并带上；只有确实过时或矛盾的行才删掉。

再给出 3~8 个话题关键词 hooks：从画像内容里提炼，用于群聊索引与记忆触发。

严格输出 JSON 对象：
{"stable": [{"text": "...", "evidence": ["event:12"], "entity": ""}],
 "status": [{"text": "...", "expires_in_days": 7, "evidence": ["event:15"], "entity": ""}],
 "hooks": ["爬山", "猫"]}
"""


@dataclass
class ProfileUpdate:
    person_id: str
    changed: bool = False
    stable_lines: int = 0
    status_lines: int = 0
    dropped_lines: int = 0
    hooks: list[str] = field(default_factory=list)
    error: str | None = None
    log_id: int | None = None


@dataclass
class _CandidateLine:
    text: str
    refs: list[str]
    expires_in_days: int | None = None
    entity: str = ""


def parse_profile(raw: str) -> tuple[list[_CandidateLine], list[_CandidateLine], list[str]]:
    """解析画像提议；结构不合法的行直接丢掉。"""
    data = parse_json_payload(raw)
    if not isinstance(data, dict):
        return [], [], []
    stable = _parse_lines(data.get("stable"), with_expiry=False)
    status = _parse_lines(data.get("status"), with_expiry=True)
    hooks: list[str] = []
    for item in data.get("hooks") or []:
        if isinstance(item, str):
            hook = item.strip()
        elif isinstance(item, dict):
            hook = str(item.get("text") or item.get("hook") or "").strip()
        else:
            continue
        if 0 < len(hook) <= 20 and hook not in hooks:
            hooks.append(hook)
    return stable, status, hooks[:8]


def _parse_lines(items, *, with_expiry: bool) -> list[_CandidateLine]:
    if not isinstance(items, list):
        return []
    lines: list[_CandidateLine] = []
    for item in items:
        if isinstance(item, str):
            item = {"text": item, "evidence": []}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if len(text) < 2 or len(text) > 200:
            continue
        raw_refs = item.get("evidence") or item.get("evidence_refs") or []
        if isinstance(raw_refs, str):
            raw_refs = [raw_refs]
        if not isinstance(raw_refs, list):
            raw_refs = []
        refs = [str(ref).strip() for ref in raw_refs if str(ref).strip()]
        expires = None
        if with_expiry:
            try:
                expires = int(float(item.get("expires_in_days")))
            except (TypeError, ValueError):
                expires = None
        entity = str(item.get("entity") or item.get("entity_name") or "").strip()[:120]
        lines.append(_CandidateLine(text=text, refs=refs, expires_in_days=expires, entity=entity))
    return lines


def clamp_expiry_days(days: int | None, low: int = 1, high: int = 90) -> int:
    """近况过期天数夹到 [low, high]；缺省给 7 天。"""
    low = max(1, int(low))
    high = max(low, int(high))
    if days is None:
        days = 7
    return min(high, max(low, int(days)))


class ProfileDistiller:
    """把事件/事实沉淀成画像。模型提议，代码校验证据链与格式。"""

    def __init__(self, llm: LLMProtocol, store: MemoryStore, config):
        self.llm = llm
        self.store = store
        self.config = config

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------

    def due_people(self, now: float | None = None) -> list[str]:
        """有新事件/事实且达阈值（或到间隔）的人。"""
        now = time.time() if now is None else now
        cfg = self.config
        last_at = self.store.get_meta_float("distill:last_at", 0.0)
        interval_due = (now - last_at) >= cfg.distill_interval_hours * 3600
        people: list[str] = []
        for person in self.store.list_people(limit=10000):
            person_id = person.person_id
            if self.store.is_norecord(person_id):
                continue
            new_events = self.store.count_events(person_id=person_id, since=last_at)
            new_facts = self.store.count_memories(person_id=person_id, since=last_at)
            if not new_events and not new_facts:
                continue
            if new_events + new_facts >= cfg.distill_new_threshold or interval_due:
                people.append(person_id)
        return people

    async def maybe_distill(self, now: float | None = None, force: bool = False) -> list[ProfileUpdate]:
        """后台任务入口；单人失败不影响其他人。"""
        now = time.time() if now is None else now
        if force:
            people = [
                person.person_id
                for person in self.store.list_people(limit=10000)
                if not self.store.is_norecord(person.person_id)
                and (
                    self.store.count_events(person_id=person.person_id) > 0
                    or self.store.count_memories(person_id=person.person_id) > 0
                )
            ]
        else:
            people = self.due_people(now)
        results: list[ProfileUpdate] = []
        for person_id in people:
            try:
                results.append(await self.distill_person(person_id, now=now))
            except Exception as exc:  # noqa: BLE001 - 单人失败不影响其他人
                logger.warning("画像提炼失败（%s）：%s", person_id, exc, extra={"person_id": person_id})
                results.append(ProfileUpdate(person_id=person_id, error=str(exc)))
        if results and all(r.error is None for r in results):
            self.store.set_meta("distill:last_at", str(now))
        return results

    # ------------------------------------------------------------------
    # 单人提炼
    # ------------------------------------------------------------------

    async def distill_person(self, person_id: str, *, now: float | None = None) -> ProfileUpdate:
        now = time.time() if now is None else now
        inputs = self.store.distill_input(person_id, self.config.distill_max_events, self.config.distill_max_facts)
        if inputs is None:
            return ProfileUpdate(person_id=person_id)
        old = inputs.reusable_profile
        raw = await self.llm.complete(
            [
                {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": self._render_input(inputs)},
            ],
            role="distill",
            temperature=0.3,
            json_mode=True,
        )
        stable, status, hooks = parse_profile(raw)
        # 私聊画像行照常保留证据，钩子没有逐条证据，不能用私聊原料发布新钩子。
        if any(item.audience == "private" for item in inputs.sources.values()):
            hooks = []
        if not stable and not status and not hooks:
            # 模型什么都没给（多半是坏输出）：保留旧画像，别把已有的画像清空
            logger.info("画像提炼没有产出有效内容（%s），保留旧画像", person_id, extra={"person_id": person_id})
            return ProfileUpdate(person_id=person_id)

        # 校验只读请求前的快照；丢弃行的正文也要等整轮通过事务复核后才可留痕。
        discarded: list[tuple[str, str, str]] = []
        stable_lines, dropped_stable = self._validate(stable, inputs.sources, discarded)
        status_lines, dropped_status = self._validate(status, inputs.sources, discarded)
        dropped = dropped_stable + dropped_status
        keep_old = old is not None and not stable_lines and not status_lines and dropped > 0

        expires_days = [
            clamp_expiry_days(
                line.expires_in_days,
                getattr(self.config, "status_expiry_min_days", 1),
                getattr(self.config, "status_expiry_max_days", 90),
            )
            for line in status_lines
        ]
        status_expires_at = max(expires_days) * 86400.0 + now if expires_days else None

        changed = not keep_old and (
            old is None
            or old.stable_text != "\n".join(line.text for line in stable_lines)
            or old.status_text != "\n".join(line.text for line in status_lines)
            or old.hooks != hooks
        )
        hooks = hooks or (old.hooks if old else [])
        accepted, log_id = self.store.commit_distillation(
            inputs, stable_lines=stable_lines, status_lines=status_lines, status_expires_at=status_expires_at,
            hooks=hooks, now=now, discarded=discarded,
            log_reason=self._change_reason(old, stable_lines, status_lines) if changed else "",
        )
        if not accepted:
            return ProfileUpdate(person_id=person_id)
        if keep_old:
            return ProfileUpdate(person_id=person_id, dropped_lines=dropped)
        return ProfileUpdate(
            person_id=person_id,
            changed=changed,
            stable_lines=len(stable_lines),
            status_lines=len(status_lines),
            dropped_lines=dropped,
            hooks=hooks,
            log_id=log_id,
        )

    # ------------------------------------------------------------------

    def _validate(
        self,
        lines: list[_CandidateLine],
        sources: dict[str, Event | Memory],
        discarded: list[tuple[str, str, str]],
    ) -> tuple[list[ProfileLine], int]:
        """只保留证据链指向输入（或旧画像已有证据）的行；同节近重复也丢掉。

        画像行同样过指称完整性：只有评价、没有主语的句子不许独立成行，
        找不到可回指的实体就丢弃并在 ``memory_log`` 留痕。
        """
        kept: list[ProfileLine] = []
        dropped = 0
        for line in lines:
            refs = [ref for ref in dict.fromkeys(line.refs) if ref in sources]
            if not refs:
                dropped += 1
                continue
            evidence = [sources[ref] for ref in refs]
            entities = {item.entity for item in evidence if isinstance(item, Memory) and item.entity}
            decision = evaluate(
                line.text,
                source_text="\n".join(e.summary if isinstance(e, Event) else e.content for e in evidence),
                # 证据记忆自己已经锚在实体上时，直接沿用，别让画像行再猜一次。
                entity=entities.pop() if len(entities) == 1 else "",
                model_entity=line.entity,
            )
            if not decision.ok:
                dropped += 1
                discarded.append((self._origin_hint(refs), line.text, decision.reason or "无实体锚点"))
                continue
            if any(jaccard(line.text, existing.text) >= 0.9 for existing in kept):
                dropped += 1
                continue
            kept.append(
                ProfileLine(
                    text=annotate(line.text, decision.anchor),
                    evidence_refs=refs,
                    line_no=len(kept),
                    expires_in_days=line.expires_in_days,
                    entity=decision.anchor.name if decision.anchor else "",
                )
            )
        return kept, dropped

    @staticmethod
    def _origin_hint(refs: list[str]) -> str:
        """丢弃记录里的来源提示：列出证据引用（event/memory id），方便回查原话。"""
        return ",".join(refs[:3])

    def _render_input(self, inputs: DistillInput) -> str:
        from ..memory.extract import format_ts

        parts: list[str] = []
        old = inputs.reusable_profile
        if old is not None:
            evidence = inputs.evidence

            def render(section: str, lines: list[str]) -> str:
                if not lines:
                    return "- （无）"
                return "\n".join(
                    f"- {line}  证据：{', '.join(evidence.get((section, index), [])) or '（无）'}"
                    for index, line in enumerate(lines)
                )

            parts.append(
                "现有画像：\n稳定特性：\n"
                + render("stable", old.stable_lines)
                + "\n当前近况：\n"
                + render("status", old.status_lines)
            )
        else:
            parts.append("现有画像：（还没有）")
        parts.append(
            "新时间线事件：\n"
            + (
                "\n".join(
                    f"- event:{event.id} [{format_ts(event.ts)}] {event.summary}" for event in inputs.events
                )
                or "- （无）"
            )
        )
        parts.append(
            "新事实：\n"
            + ("\n".join(f"- memory:{mem.id} {mem.content}" for mem in inputs.facts) or "- （无）")
        )
        return "\n\n".join(parts)

    @staticmethod
    def _change_reason(
        old: Profile | None, stable_lines: list[ProfileLine], status_lines: list[ProfileLine]
    ) -> str:
        if old is None:
            return "首次提炼画像"
        old_lines = set(old.stable_lines) | set(old.status_lines)
        new_lines = {line.text for line in stable_lines} | {line.text for line in status_lines}
        added = len(new_lines - old_lines)
        replaced = len(old_lines - new_lines)
        return f"画像更新：新增 {added} 行，替换/移除 {replaced} 行"


def format_update(update: ProfileUpdate) -> str:
    return json.dumps(
        {
            "person_id": update.person_id,
            "changed": update.changed,
            "stable_lines": update.stable_lines,
            "status_lines": update.status_lines,
            "dropped_lines": update.dropped_lines,
        },
        ensure_ascii=False,
    )
