"""分寸审查口：所有跨场合记忆注入前的唯一隐私判断入口。

设计要点（见 SPEC「分寸审查」）：

- 只有**跨场合**的候选项才需要判断；同场合或公开来源直接放行，不调模型，
  省钱也省延迟。
- 判断顺序：同场合放行 → 不记/隐私档位 → 封存 → 已有裁定 → 高敏感默认扣下
  → 其余的打包给模型按准则权衡。模型拿不准（或回复异常）一律扣下。
- 裁定落 ``rulings`` 表复用，键是「人 × 类目/事件 × 受众类型」，同一项不会
  对同一类受众反复问模型。
- 灰区（高敏感、或模型低置信）顺带进 ``pending_asks``，由核心命令模块在
  本人私聊里找自然时机问一次；本人的回答经 ``store.answer_pending_ask``
  写回同一把裁定键，之后这里直接命中。
- **fail closed**：审查任何一步异常，剩余跨场合项全部扣下，绝不外泄。

这个模块以外的地方不许写隐私判断逻辑——谁可以看什么，只有这里说了算。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Collection, Sequence

from ..llm import LLMProtocol, parse_json_payload
from .sealing import register_message, requests_secrecy, restricted_entries

if TYPE_CHECKING:
    from ..memory.store import MemoryStore
    from ..memory.timeline import TimelineEntry

logger = logging.getLogger(__name__)

# 每人的待询问上限，防止夹带太多问题去烦人
MAX_PENDING_PER_PERSON = 5
OWNER_AUDIENCE = "owner"
_SENSITIVITY_RANK = {"normal": 0, "high": 1, "sealed": 2}

# 画像和未抽取原话共用的高敏信号；具体受众能否放行仍由审查口裁定。
_HIGH_SENSITIVITY_HINTS = (
    "抑郁", "焦虑症", "看病", "住院", "手术", "吃药", "心理医生", "病历", "确诊",
    "癌", "怀孕", "流产", "离婚", "分手", "出轨", "欠债", "负债", "借钱",
    "工资", "年薪", "拆迁", "遗产", "官司", "坐牢", "取向", "出柜",
    "身份证", "身分证", "身份證", "身分證", "证件号", "护照", "社保号", "真实姓名", "家庭住址",
)


def has_high_sensitivity_hint(text: str) -> bool:
    return any(hint in text for hint in _HIGH_SENSITIVITY_HINTS)


GATE_SYSTEM_PROMPT = """你在替一个聊天 bot 把关：它对某个人的记忆，能不能在此刻这些人面前提起。

你会收到若干候选项，每项有：
- 当事人（记忆是谁的）
- 来源（这段话当初是在私聊、另一个群，还是公开场合听到的）
- 敏感度（normal / high）
- 内容

请逐项判断「在这个受众面前提起它」是否合适，按这些准则权衡：
1. 在场的这些人跟当事人是什么关系？当事人会不会愿意让他们知道？
2. 这件事本身多敏感？健康、财务、感情、身份一类要格外小心。
3. 这件事是不是已经公开过，或者在场的人本来就知道？
4. 说出来对当事人有没有损害、尴尬或麻烦？
5. 拿不准就不放行——宁可少说一句，不要让人难堪。

注意：这里说的是「能不能提」，不是「要不要提」；放行只代表说了不算冒犯。

严格输出 JSON 对象：
{"items": [{"id": 0, "verdict": "allow", "confidence": 0.9}]}
verdict 只能是 allow / deny；confidence 是 0~1，表示你对自己判断的把握。
"""


@dataclass
class ReviewItem:
    """一条等待审查的候选记忆。

    ``audience`` 是**来源**受众（``private`` / ``group:<id>`` / ``public``）；
    ``subject`` 是裁定缓存里的「类目/具体事件」部分；``askable=False`` 表示
    这项就算拿不准也不去打扰本人（比如成员索引里的一行背景）。
    """

    person_id: str = ""
    text: str = ""
    audience: str = ""
    origin: str = ""
    sensitivity: str = "normal"
    subject: str = ""
    ref: str = ""
    category: str = ""
    kind: str = ""
    salience: float = 0.0
    askable: bool = True
    # 被扣下时写入原因，方便日志与测试排查（不参与指纹/缓存）
    withhold_reason: str = ""

    def fingerprint(self) -> tuple:
        """候选项身份（人 × 类目/事件 × 来源 × 敏感度），调试与去重时用。"""
        return (self.person_id, self.subject or self.category, self.audience, self.sensitivity)


def audience_kind(audience: str) -> str:
    """把来源受众归成 rulings 里的「受众类型」：private / group / public / unknown。"""
    value = (audience or "").strip()
    if not value:
        return "unknown"
    if value in ("private", "public"):
        return value
    if value.startswith("group:") or value == "group":
        return "group"
    return value


def ruling_key(item: ReviewItem, audience_now: str) -> str:
    """裁定缓存键：同一项对同一类受众只问一次。

    形如 ``health@group``、``event:12@private``、``profile@group``。
    """
    subject = (item.subject or item.category or item.ref or "general").strip()
    if subject.startswith("memory:") or subject in ("fact", "memory"):
        # 事实类用类目而不是单条事实 id：同类的偏好/身份不必逐条问
        subject = item.category or "fact"
    return f"{subject}@{audience_kind(audience_now)}"


class PrivacyGate:
    """统一检查蒸馏候选与原始时间线，按来源和目标场合给出裁定。"""

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMProtocol,
        config=None,
        *,
        now_fn=time.time,
    ):
        self.store = store
        self.llm = llm
        self.config = config
        self.now_fn = now_fn

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def register_message(self, event, raw_id: int) -> None:
        register_message(self.store, event, raw_id)

    async def review_timeline(self, entries: Sequence[TimelineEntry], *, audience_now: str, speaker: str,
                              owner_person_ids: Collection[str] = (), now: float | None = None,
                              target_platform: str = "", target_chat_id: str = "") -> list[TimelineEntry]:
        """时间线各层共用这一口。先验完整原文和旧库来源，保持输入的时间/相关性次序。"""
        entries = list(entries)
        try:
            blocked = restricted_entries(self.store, entries)
            pairs = []
            modes = {}
            for e in entries:
                is_raw = e.kind == 'raw'
                if (e.entity_blocked or (is_raw and e.id in blocked) or requests_secrecy(e.text)
                        or e.sensitivity == 'sealed'):
                    continue
                if e.person_id not in modes:
                    modes[e.person_id] = self.store.get_privacy_mode(e.person_id)
                if modes[e.person_id] == 'norecord' or (is_raw and modes[e.person_id] != 'normal'):
                    continue
                sensitivity = 'high' if has_high_sensitivity_hint(e.text) else e.sensitivity
                same_site = (bool(target_platform and target_chat_id) and e.platform == target_platform
                             and e.chat_id == target_chat_id and e.chat_type == audience_kind(audience_now))
                source = e.audience
                if not same_site and e.chat_type == 'private' and source != OWNER_AUDIENCE:
                    if not (is_raw and e.person_id in owner_person_ids):
                        continue
                if is_raw and not same_site and e.person_id in owner_person_ids:
                    source = OWNER_AUDIENCE
                if not same_site and e.chat_type == 'private' and e.person_id == 'self':
                    continue  # bot 自己的私聊记录也可能含对方的原话
                # bot 自己在群里做过的事（说话、发文件）当着整个群发生过；
                # 私聊里回看它，与原受众一致、不涉及别人的私密转述，沿用
                # 「自己的动作」类目（``local_self``）直接放行。高敏感仍走
                # 常规跨场合判定（默认扣下），封存与不记模式在上面已拦。
                owned_action = (e.person_id == 'self' and e.chat_type == 'group'
                                and audience_now == 'private' and sensitivity == 'normal')
                item = ReviewItem(person_id=e.person_id, text=e.text, audience=source,
                                  origin=f'{e.platform}:{e.chat_id}', sensitivity=sensitivity,
                                  subject='self' if owned_action else f'{e.kind}:{e.id}',
                                  ref=f'{e.kind}:{e.id}', kind=e.kind,
                                  askable=not is_raw)
                pairs.append((e, item))
            allowed, _ = await self.review([item for _, item in pairs], audience_now=audience_now,
                                           speaker=speaker, owner_person_ids=owner_person_ids, for_chat=True,
                                           now=now, target_platform=target_platform)
            allowed_ids = {id(item) for item in allowed}
            return [entry for entry, item in pairs if id(item) in allowed_ids]
        except Exception as exc:
            logger.warning("时间线隐私裁定失败，本批扣下：%s", exc)
            return []

    def evidence_context(
        self,
        refs: Sequence[str],
        *,
        text: str = "",
    ) -> tuple[str, str]:
        """由证据链推断一行画像的来源受众与敏感度（取最保守值）。

        画像允许抽象，抽象后的句子本身没有敏感度标注，只能沿证据链取最高值；
        证据被清掉/已过期的行再按文字补一次高敏感判断，
        避免「抽象一层就洗白」。
        """
        audiences: set[str] = set()
        sensitivity = "normal"
        for ref in refs:
            kind, _, raw_id = str(ref or "").partition(":")
            obj = None
            try:
                if kind == "event":
                    obj = self.store.get_event(int(raw_id))
                elif kind == "memory":
                    obj = self.store.get_memory(int(raw_id))
            except (TypeError, ValueError):
                obj = None
            except Exception:  # noqa: BLE001
                obj = None
            if obj is None:
                return "unverified", "sealed"  # 证据已删除或读不到，绝不当成旧的空来源
            audiences.add(obj.audience or "")
            if _SENSITIVITY_RANK.get(obj.sensitivity, 0) > _SENSITIVITY_RANK.get(sensitivity, 0):
                sensitivity = obj.sensitivity
        if sensitivity == "normal" and has_high_sensitivity_hint(text):
            sensitivity = "high"
        if not audiences:
            return "", sensitivity
        if OWNER_AUDIENCE in audiences and len(audiences) > 1:
            return "unverified", "sealed"  # 混合行不能靠其中一条机主自述扩受众
        if "private" in audiences:
            return "private", sensitivity
        audiences.discard("")
        if not audiences:
            return "", sensitivity
        if len(audiences) == 1:
            return audiences.pop(), sensitivity
        return "mixed", sensitivity

    async def review(
        self,
        items: Sequence[ReviewItem],
        *,
        audience_now: str,
        speaker: str = "",
        owner_person_ids: Collection[str] = (),
        for_chat: bool = False,
        target_platform: str = "",
        now: float | None = None,
    ) -> tuple[list[ReviewItem], list[ReviewItem]]:
        """返回 ``(allowed, withheld)``；被扣下的项带 ``withhold_reason``。

        ``audience_now`` 是当前场景受众（私聊 ``private``，群聊 ``group:<id>``）；
        ``speaker`` 是当前说话人的 person_id，只用于让模型知道谁在场。
        任何内部异常都只会让跨场合项被扣下，不会抛出（调用方也不必担心隐私
        因异常而失效）。
        """
        now = self.now_fn() if now is None else now
        allowed: list[ReviewItem] = []
        withheld: list[ReviewItem] = []
        pending_model: list[ReviewItem] = []

        raw_denials: dict[str, bool] = {}
        modes: dict[str, str] = {}
        for item in items:
            if for_chat and requests_secrecy(item.text):
                item.sensitivity = "sealed"
            elif for_chat and item.sensitivity == "normal" and has_high_sensitivity_hint(item.text):
                item.sensitivity = "high"
            if (for_chat and item.sensitivity == "sealed") or (
                item.sensitivity == "sealed" and item.audience not in ("", "public", audience_now)
            ):
                item.withhold_reason = "封存事项"
                withheld.append(item)
                continue
            if item.audience == OWNER_AUDIENCE:
                verdict = self._owner_verdict(item, audience_now, owner_person_ids, now=now, raw_denials=raw_denials)
                if verdict is None:
                    pending_model.append(item)
                else:
                    (allowed if verdict == "allow" else withheld).append(item)
                continue
            # 已归档的私聊中，bot 自己的回话也可能包含对方的原话。
            own_private = audience_now == "private" and item.person_id == speaker
            local_self = (item.person_id == "self" and audience_now == "private" and item.subject == "self")
            private_outside = for_chat and item.audience == "private" and not (own_private or local_self)
            same = self._same_site(item, audience_now)
            source_platform = (item.origin or item.person_id).partition(':')[0]
            if (same and target_platform and source_platform and audience_now.startswith('group:')
                    and item.audience == audience_now and source_platform != target_platform):
                same = False
            if for_chat and same:
                try:
                    if item.person_id not in modes:
                        modes[item.person_id] = self.store.get_privacy_mode(item.person_id)
                    mode = modes[item.person_id]
                except Exception as exc:
                    logger.warning("读取隐私档位失败，扣下：%s", exc)
                    item.withhold_reason = "审查异常（fail closed）"
                    withheld.append(item)
                    continue
                if mode == "norecord":
                    item.withhold_reason = "不记模式"
                    withheld.append(item)
                    continue
                if mode != "normal" and not own_private:
                    same = False
                # 群→私的无缝接续是本人的群记录，不是把甲的敏感事项给乙。
                if item.sensitivity == "high" and not own_private and item.audience in ("", "public"):
                    same = False
                if audience_now == "private" and item.audience.startswith("group:") and not (own_private or local_self):
                    same = False
            if not private_outside and same:
                allowed.append(item)
                continue
            try:
                if for_chat and item.kind == "raw" and self._raw_denied(item.person_id, audience_now, raw_denials):
                    item.withhold_reason = "已有裁定：不可说"
                    withheld.append(item)
                    continue
                verdict = self._local_verdict(item, audience_now, now=now)
            except Exception as exc:  # noqa: BLE001 - 查库失败按扣下处理
                logger.warning("分寸审查本地判定失败，扣下：%s", exc)
                item.withhold_reason = "审查异常（fail closed）"
                withheld.append(item)
                continue
            if private_outside:
                item.withhold_reason = item.withhold_reason or "私聊资料不在此处展示"
                withheld.append(item)
            elif verdict == "allow":
                allowed.append(item)
            elif verdict == "withhold":
                withheld.append(item)
            else:
                pending_model.append(item)

        if pending_model:
            try:
                verdicts = await self._judge_with_model(pending_model, audience_now, speaker)
            except Exception as exc:  # noqa: BLE001 - 模型失败一律扣下
                logger.warning("分寸审查模型判定失败，跨场合项全部扣下：%s", exc)
                for item in pending_model:
                    item.withhold_reason = "审查异常（fail closed）"
                    withheld.append(item)
                return allowed, withheld
            for item in pending_model:
                answer = verdicts.get(id(item))
                if answer is None:
                    item.withhold_reason = "模型未给出有效判断"
                    withheld.append(item)
                    continue
                verdict, confidence = answer
                key = ruling_key(item, audience_now)
                if confidence >= self._confidence_threshold():
                    try:
                        ruling = self.store.set_ruling(item.person_id, key, verdict, source="model", now=now)
                        verdict = ruling.verdict  # 等待模型期间，本人的新裁定优先
                    except Exception as exc:  # noqa: BLE001 - 缓存失败不影响本次结论
                        logger.warning("写入分寸裁定失败：%s", exc)
                    if verdict == "allow":
                        allowed.append(item)
                    else:
                        item.withhold_reason = "模型判断不宜提及"
                        withheld.append(item)
                    continue
                # 低置信：不放行；敏感度不低的攒到私聊里问本人
                item.withhold_reason = "模型判断把握不足"
                withheld.append(item)
                if item.sensitivity != "normal":
                    self._maybe_ask(item, audience_now, reason="low_confidence")

        return allowed, withheld

    # ------------------------------------------------------------------
    # 本地判定（不调模型）
    # ------------------------------------------------------------------

    def _raw_denied(self, person_id: str, audience: str, cache: dict[str, bool] | None) -> bool:
        """原话没有可靠类目；同一人对同一目标的 deny 只检查一次。"""
        if cache is None:
            cache = {}
        if person_id not in cache:
            cache[person_id] = any(
                r.verdict == "deny" and r.key.endswith("@" + audience_kind(audience))
                for r in self.store.list_rulings(person_id)
            )
        return cache[person_id]

    def _owner_verdict(self, item: ReviewItem, audience_now: str, owner_person_ids: Collection[str],
                       *, now: float, raw_denials: dict[str, bool]) -> str | None:
        """本人在场也要过硬限制；未经分类的原话不能仅凭账号直通群聊。"""
        if (item.audience != OWNER_AUDIENCE or not item.person_id or item.person_id not in owner_person_ids
                or audience_kind(audience_now) not in ("private", "group")):
            item.withhold_reason = "受众不含该机主或场合不明"
            return "withhold"
        try:
            if item.kind == "raw" and self._raw_denied(item.person_id, audience_now, raw_denials):
                item.withhold_reason = "已有裁定：不可说"
                return "withhold"
            verdict = self._local_verdict(item, audience_now, now=now)
            if verdict is not None:
                return verdict
            if item.kind == "raw" and audience_kind(audience_now) == "group":
                from ..memory.owner_statement import is_owner_self_statement

                # 沿用已定义的机主自述资格；其他原话不按句式丢弃，而是逐条送审。
                if not is_owner_self_statement(item.text):
                    return None
            return "allow"
        except Exception as exc:
            logger.warning("机主分寸审查失败，扣下：%s", exc)
            item.withhold_reason = "审查异常（fail closed）"
            return "withhold"

    @staticmethod
    def _same_site(item: ReviewItem, audience_now: str) -> bool:
        """同场合、公开来源，以及「群 → 私」方向直接放行。

        - 历史数据可能没有 audience（空串），按同场合处理，保持旧记忆可用；
        - 私聊里回看群里的背景是「无缝」的，不需要审查；
        - 反过来「私 → 群 / 群 A → 群 B」才是跨场合，要走后面的判断。
        """
        source = (item.audience or "").strip()
        if not source or source == "public":
            return True
        if source == audience_now:
            return True
        if audience_now == "private" and audience_kind(source) in ("private", "group"):
            return True
        return False

    def _local_verdict(self, item: ReviewItem, audience_now: str, *, now: float) -> str | None:
        """不需要模型的判定；返回 ``"allow"`` / ``"withhold"`` / ``None``（交给模型）。"""
        # ① 封存：本人明确要求保密的东西永远不跨场合出现
        if item.sensitivity == "sealed":
            item.withhold_reason = "封存事项"
            return "withhold"

        # ② 按人三档隐私
        mode = self.store.get_privacy_mode(item.person_id) if item.person_id else "normal"
        if mode == "norecord":
            # 不记模式本不该有数据；真出现了也只当没有
            item.withhold_reason = "不记模式无数据可审"
            return "withhold"
        key = ruling_key(item, audience_now)
        ruling = self.store.get_ruling(item.person_id, key) if item.person_id else None
        if ruling is not None and ruling.verdict == "deny":
            item.withhold_reason = "已有裁定：不可说"
            return "withhold"
        if ruling is not None and ruling.verdict == "allow" and ruling.source == "explicit":
            return "allow"
        if mode == "private":
            item.withhold_reason = "隐私模式"
            return "withhold"

        # ③.5 受众不明确（recall 没有当前会话可依据）：跨场合项一律扣下
        if audience_kind(audience_now) not in ("private", "group"):
            item.withhold_reason = "受众不明确，跨场合项不放行"
            return "withhold"

        # ④ 高敏感跨场合默认扣下，并攒到私聊里问本人
        if item.sensitivity == "high":
            item.withhold_reason = "高敏感且跨场合"
            if self._ask_high_sensitivity():
                self._maybe_ask(item, audience_now, reason="high_sensitivity")
            return "withhold"

        # 普通事项才可复用模型（或旧库来源不明）的 allow。
        if ruling is not None and ruling.verdict == "allow":
            return "allow"
        return None

    # ------------------------------------------------------------------
    # 模型判定
    # ------------------------------------------------------------------

    async def _judge_with_model(
        self,
        items: list[ReviewItem],
        audience_now: str,
        speaker: str,
    ) -> dict[int, tuple[str, float]]:
        """把跨场合项打包给模型；返回 ``{id(item): (verdict, confidence)}``。

        解析不出、字段非法的项直接不出现在返回值里，由调用方按扣下处理。
        这里**不吞异常**：模型调用失败要让上层把所有项 fail closed。
        """
        audience_text = "和一个人的私聊（只有你们两个人）"
        if audience_kind(audience_now) == "group":
            audience_text = "一个群聊（群里有好几个人）"
        speaker_name = self._display_name(speaker) if speaker else ""
        if speaker_name:
            audience_text += f"，此刻正在说话的是 {speaker_name}"

        lines = [f"当前受众：{audience_text}", "候选项："]
        for index, item in enumerate(items):
            lines.append(
                f"- id={index} 当事人={self._display_name(item.person_id)} "
                f"来源={self._describe_audience(item.audience)} "
                f"敏感度={item.sensitivity} 内容：{self._snippet(item.text, 180)}"
            )
        raw = await self.llm.complete(
            [
                {"role": "system", "content": GATE_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
            role="gate",
            temperature=0.1,
            json_mode=True,
        )
        data = parse_json_payload(raw)
        entries = data.get("items") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError("分寸审查模型输出不是有效 JSON")

        verdicts: dict[int, tuple[str, float]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(items):
                continue
            verdict = str(entry.get("verdict") or "").strip().lower()
            if verdict not in ("allow", "deny"):
                continue
            try:
                confidence = float(entry.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = min(1.0, max(0.0, confidence))
            verdicts[id(items[index])] = (verdict, confidence)
        return verdicts

    # ------------------------------------------------------------------
    # 待询问
    # ------------------------------------------------------------------

    def _maybe_ask(self, item: ReviewItem, audience_now: str, *, reason: str) -> None:
        """把拿不准的项攒进待问队列；同键只问一次，且不打扰不记模式的人。"""
        if not item.askable or not item.person_id or item.person_id == "self":
            return
        try:
            if self.store.is_norecord(item.person_id):
                return
            key = ruling_key(item, audience_now)
            ruling = self.store.get_ruling(item.person_id, key)
            if ruling is not None and (ruling.source == "explicit" or ruling.verdict == "deny"):
                return
            existing = self.store.list_pending_asks(item.person_id, status="pending", limit=200)
            if any(ask.item_ref == key for ask in existing):
                return
            if len(existing) >= MAX_PENDING_PER_PERSON:
                logger.debug("待询问已达上限，跳过：%s %s", item.person_id, key, extra={"person_id": item.person_id})
                return
            scope = "群里" if audience_kind(audience_now) == "group" else "别人面前"
            question = (
                f"你之前说过的「{self._snippet(item.text, 50)}」，"
                f"我可以在{scope}提到吗？回我「可以」或「不行」就行。"
            )
            self.store.add_pending_ask(item.person_id, key, question)
            logger.info("分寸拿不准，加入待询问：%s %s（%s）", item.person_id, key, reason, extra={"person_id": item.person_id})
        except Exception as exc:  # noqa: BLE001 - 加待问失败不能影响扣下结论
            logger.warning("写入待询问失败：%s", exc)

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    def _confidence_threshold(self) -> float:
        return float(getattr(self.config, "gate_confidence_threshold", 0.6))

    def _ask_high_sensitivity(self) -> bool:
        return bool(getattr(self.config, "gate_ask_high_sensitivity", True))

    def _display_name(self, person_id: str) -> str:
        if not person_id:
            return "某个人"
        if person_id == "self":
            return "bot 自己"
        try:
            person = self.store.get_person(person_id)
        except Exception:  # noqa: BLE001
            person = None
        return (person.display_name if person and person.display_name else person_id)

    @staticmethod
    def _describe_audience(audience: str) -> str:
        kind = audience_kind(audience)
        if kind == "private":
            return "私聊"
        if kind == "group":
            return "另一个群"
        if kind == "public":
            return "公开场合"
        return "未知场合"

    @staticmethod
    def _snippet(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"
