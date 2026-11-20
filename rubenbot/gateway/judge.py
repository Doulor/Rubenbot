"""事件快判（第三层闸）：用 fast 角色给一批事件下结论。

输入只有三样：事件描述、人设一句、对方画像摘要（有才给）。输出是结构化
的 ``verdict`` 与载荷：

- ``ignore``：不理；
- ``react``：给某条消息贴表情（抽象动作 ``react``，由适配器目录解析成
  平台接口）；
- ``message``：在当前会话说一句短消息；
- ``gateway``：走网关调一个平台接口；
- ``notify_owner``：通知主人。

分寸硬规则：``request`` 类事件一期不许执行申请处理动作，只能
``ignore`` 或 ``notify_owner``。这里在代码层强制降级并记日志，不靠提示词
自觉——模型输出别的一律改成 ``notify_owner``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..adapters.platform import KIND_REQUEST, PlatformEvent
from ..llm import LLMProtocol, parse_json_payload
from ..memory.store import MemoryStore, make_person_id
from .events import EventBatch

logger = logging.getLogger(__name__)

VERDICTS = frozenset({"ignore", "react", "message", "gateway", "notify_owner"})
# 申请类事件允许的结论（一期）：不许替人做决定
REQUEST_VERDICTS = frozenset({"ignore", "notify_owner"})

JUDGE_ROLE = "fast"
JUDGE_TASK = "event_judge"

JUDGE_SYSTEM_PROMPT = """你是 Ruben 的「事件快判」助手。Ruben 是个在 QQ 上聊天的普通人，
现在它收到了一条平台通知或申请，你要替它想一下：这件事值不值得当场做点什么。

人设（只作语气与判断参照）：
{persona_line}

要求：
- 大多数通知都不值得动作，ignore 是默认答案；别为了做事而做事。
- 只有确实和 Ruben 有关、且做点小事自然合理时才行动。
- message 只写一句很短的话（不超过 30 字），不要解释、不要问候式寒暄。
- request 类（好友申请、入群申请/邀请）是替别人做决定：只能 ignore 或
  notify_owner，绝不自己同意或拒绝。
- 拿不准就 ignore。

严格输出 JSON 对象：
{{"verdict": "ignore", "payload": {{}}, "reason": "一句话理由"}}
verdict 只能是 ignore / react / message / gateway / notify_owner。
payload 按结论给：
- react：{{"message_id": 消息id, "emoji_id": "表情id"}}
- message：{{"text": "要说的话"}}
- gateway：{{"action": "接口名", "params": {{}}}}
- notify_owner：{{"text": "想告诉主人的一句话"}}
- ignore：{{}}
"""

JUDGE_USER_TEMPLATE = """事件：{description}
类型：{kind}/{sub_type}
会话：{conversation}
{profile_block}"""


@dataclass
class EventVerdict:
    """一次快判的结论。``downgraded`` 表示被分寸硬规则改写过。"""

    verdict: str = "ignore"
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    downgraded: bool = False
    raw: str = ""


def persona_line(text: str, limit: int = 80) -> str:
    """人设一句：取第一行有实义的文字，去掉标题/空行。"""
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def profile_summary(store: MemoryStore, person_id: str, limit: int = 200) -> str:
    """对方画像摘要：稳定特性 + 近况，有才给。失败返回空串。"""
    if not person_id:
        return ""
    try:
        profile = store.get_profile(person_id)
    except Exception:  # noqa: BLE001 - 画像读不到只是少一点背景
        return ""
    if profile is None:
        return ""
    lines = [line for line in profile.stable_lines + profile.status_lines if line.strip()]
    if not lines:
        return ""
    text = "；".join(lines)
    return text[:limit]


def is_request_event(event: PlatformEvent) -> bool:
    return event.kind == KIND_REQUEST


class EventJudge:
    """把一批事件交给 fast 模型，得到并校验结论。"""

    def __init__(self, llm: LLMProtocol, store: MemoryStore, persona_text: str = ""):
        self.llm = llm
        self.store = store
        self.persona_text = persona_text

    def _system_prompt(self) -> str:
        return JUDGE_SYSTEM_PROMPT.format(persona_line=persona_line(self.persona_text) or "（无）")

    def _user_prompt(self, batch: EventBatch) -> str:
        first = batch.first
        person_id = make_person_id(first.platform, first.actor_id) if first.actor_id else ""
        summary = profile_summary(self.store, person_id)
        profile_block = f"对方画像：{summary}\n" if summary else ""
        return JUDGE_USER_TEMPLATE.format(
            description=batch.description,
            kind=first.kind,
            sub_type=first.sub_type or first.type,
            conversation=first.conversation or "未知",
            profile_block=profile_block,
        )

    async def judge(self, batch: EventBatch) -> EventVerdict:
        """调用模型并解析；任何失败都退化为 ignore。"""
        try:
            raw = await self.llm.complete(
                [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": self._user_prompt(batch)},
                ],
                role=JUDGE_ROLE,
                task=JUDGE_TASK,
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:  # noqa: BLE001 - 快判失败只当没看见
            logger.warning("事件快判失败，按 ignore 处理：%s", exc)
            return EventVerdict(verdict="ignore", reason=f"judge failed: {exc}")
        return self.parse(raw, batch)

    def parse(self, raw: str | None, batch: EventBatch) -> EventVerdict:
        """解析模型输出并执行分寸硬规则（可单独测试）。"""
        data = parse_json_payload(raw or "")
        if not isinstance(data, dict):
            return EventVerdict(verdict="ignore", reason="输出不是 JSON 对象", raw=raw or "")
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            return EventVerdict(verdict="ignore", reason=f"未知结论 {verdict!r}", raw=raw or "")
        payload = data.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        reason = str(data.get("reason") or "").strip()
        result = EventVerdict(verdict=verdict, payload=payload, reason=reason, raw=raw or "")
        self._enforce_request_rule(result, batch)
        return result

    @staticmethod
    def _enforce_request_rule(result: EventVerdict, batch: EventBatch) -> None:
        """申请类事件：代码层强制只允许 ignore / notify_owner。"""
        if not is_request_event(batch.first):
            return
        if result.verdict in REQUEST_VERDICTS:
            return
        logger.warning(
            "申请类事件的结论 %s 被强制降级为 notify_owner（分寸硬规则）", result.verdict
        )
        result.verdict = "notify_owner"
        result.downgraded = True
        text = result.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            result.payload = {
                "text": f"有人发来申请，想让我处理，但这类事得你来定：{batch.description}"
            }
        else:
            result.payload = {"text": text.strip()[:200]}


def verdict_summary(verdict: EventVerdict) -> str:
    """给日志/审计看的一行摘要。"""
    try:
        payload = json.dumps(verdict.payload, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = "{}"
    return f"{verdict.verdict} {payload[:200]}"
