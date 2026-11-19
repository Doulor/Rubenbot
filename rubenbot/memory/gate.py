"""分寸审查口：所有跨场合记忆注入前的唯一隐私判断入口。

设计要点（见 SPEC「分寸审查」）：

- 只有**跨场合**的候选项才需要判断；同场合或公开来源直接放行，不调模型，
  省钱也省延迟。
- 判断顺序：同场合放行 → 不记/隐私档位 → 封存 → 已有裁定 → 高敏感默认扣下
  → 其余的打包给模型按准则权衡。模型拿不准（或回复异常）一律扣下。
- 裁定落 ``rulings`` 表复用，键是「人 × 类目/事件 × 受众类型」，同一项不会
  对同一类受众反复问模型。
- 灰区（高敏感、或模型低置信）顺带进 ``pending_asks``，由 privacy 插件在
  本人私聊里找自然时机问一次；本人的回答经 ``store.answer_pending_ask``
  写回同一把裁定键，之后这里直接命中。
- **fail closed**：审查任何一步异常，剩余跨场合项全部扣下，绝不外泄。

这个模块以外的地方不许写隐私判断逻辑——谁可以看什么，只有这里说了算。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

from ..llm import LLMProtocol, parse_json_payload
from .store import MemoryStore

logger = logging.getLogger(__name__)

# 每人的待询问上限，防止夹带太多问题去烦人
MAX_PENDING_PER_PERSON = 5

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
    """唯一的分寸审查口。``review`` 是本模块对外的全部判断能力。"""

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

    async def review(
        self,
        items: Sequence[ReviewItem],
        *,
        audience_now: str,
        speaker: str = "",
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

        for item in items:
            if self._same_site(item, audience_now):
                allowed.append(item)
                continue
            try:
                verdict = self._local_verdict(item, audience_now, now=now)
            except Exception as exc:  # noqa: BLE001 - 查库失败按扣下处理
                logger.warning("分寸审查本地判定失败，扣下：%s", exc)
                item.withhold_reason = "审查异常（fail closed）"
                withheld.append(item)
                continue
            if verdict == "allow":
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
                        self.store.set_ruling(item.person_id, key, verdict, now=now)
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
        if mode == "private":
            # 隐私模式：跨场合硬沉默，只有本人白名单裁定 allow 的具体项例外
            if ruling is not None and ruling.verdict == "allow":
                return "allow"
            item.withhold_reason = "隐私模式"
            return "withhold"

        # ③ 已落过的裁定直接用
        if ruling is not None:
            if ruling.verdict == "allow":
                return "allow"
            item.withhold_reason = "已有裁定：不可说"
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

        # ⑤ 其余交给模型按准则权衡
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
            if self.store.get_ruling(item.person_id, key) is not None:
                return
            existing = self.store.list_pending_asks(item.person_id, status="pending", limit=200)
            if any(ask.item_ref == key for ask in existing):
                return
            if len(existing) >= MAX_PENDING_PER_PERSON:
                logger.debug("待询问已达上限，跳过：%s %s", item.person_id, key)
                return
            scope = "群里" if audience_kind(audience_now) == "group" else "别人面前"
            question = (
                f"你之前说过的「{self._snippet(item.text, 50)}」，"
                f"我可以在{scope}提到吗？回我「可以」或「不行」就行。"
            )
            self.store.add_pending_ask(item.person_id, key, question)
            logger.info("分寸拿不准，加入待询问：%s %s（%s）", item.person_id, key, reason)
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
