"""回复门：生成前先用一次便宜调用判断这批消息的性质，再按类进主生成。

群聊里大段消息只是氛围和情绪，字面意思和实际要表达的东西没有关系；把这类
话当正经话题正式回应，人机感立刻出来。这里借用 MaiBot Planner / AstrBot
PersonaFlow 的「先便宜判一轮再生成」思路（只借思路，代码全部自写）：

- 分类四选一：灌水 / 调侃 / 真问题 / 不理；
- 结果只做两件事：给主生成加一行口吻指导，或按「不理」这一轮不生成；
- 分类输入复用第 36 项的指向事实与房间状态（``_room_state``）和固定范例，
  不另起一套上下文组装；范例只进这一次分类调用，不进别处。

它不是新闸门：不拦放行、不改策划的开口裁定，只在既定的生成步骤里决定
这次该怎么接话。分类失败、超时或输出不可解析时按原流程继续，不影响回复。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any

from .adapters.base import MessageEvent
from .core_context import _REGISTER_EXAMPLES
from .core_shared import _visual_placeholder, logger
from .llm import CompletionAudit, LLMClient, parse_json_payload, request_result_code

# 分类调用走模型池里最便宜的一档：新角色 reply_gate 默认沿回退链落到 fast
# （见 config.ROLE_FALLBACKS），要单独钉更小的模型时只配这一个角色即可。
GATE_ROLE = "reply_gate"
GATE_TASK = "reply_gate"
# 单次分类的等待上限；超时就按原流程继续，不拖慢本轮回复。
GATE_TIMEOUT_CAP = 6.0
GATE_MAX_TOKENS = 120

CATEGORIES = ("灌水", "调侃", "真问题", "不理")
# 「不理」在直接叫到 bot 的批次上不成立：那一定是冲着 bot 来的，退成灌水
# （一两个字带过），不整个不接。
FALLBACK_CATEGORY = "灌水"

# 第 36 项的四对范例是封闭清单的一部分（容量 8–10 条）。这里一次补足四类
# 各一条和一条边界，加上四对固定范例合计九条；之后不再按毛病追加。只进本
# 分类调用。
_EXTRA_EXAMPLES = """补充分类举例（与本轮无关）：
群友说「因为ruben太笨了」 → 调侃
群里刷「+1」「笑死」「哈哈哈哈」 → 灌水
有人问「贤惠是褒义还是贬义？」 → 真问题
两个群友互相约饭、没叫到你 → 不理
群友说「这玩意儿昨天又崩了」 → 灌水"""

_GATE_PROMPT = (
    "先看这批群消息的性质，再决定怎么接。只输出一个 JSON："
    '{"category":"灌水|调侃|真问题|不理","reason":"几个字"}。\n'
    "灌水：字面像在说事，实际只是气氛、情绪或起哄，没有等谁回答。\n"
    "调侃：冲着我来的玩笑、损话、起哄，不是真要答案。\n"
    "真问题：确实在问一件具体的事，等我给出答案。\n"
    "不理：没轮到我——别人互相聊天、约事、播报，或只是表情。\n"
    "直接 @ 我、回复我或叫到我名字的，归调侃或真问题。\n"
    "reason 用几个字说明依据，供审计。"
)

# 按类进主生成的口吻指导。只写正面做法，不写禁词表。
_GATE_GUIDANCE = {
    "灌水": (
        "【回复门·本批性质】这几句是凑热闹，没有要你交代的事。顺着气氛甩一两个词"
        "（最多十个字），或者只回{marker}让大家自己聊；解释和补充留给真被问到的场合。"
    ),
    "调侃": (
        "【回复门·本批性质】这几句在拿你开涮。用平视的口吻接住再顶回去，"
        "像被熟人损了一句那样，一句话就散；语气松下来就成认账了。"
    ),
    "真问题": (
        "【回复门·本批性质】这是在问一件具体的事。直接给答案，一两句话说清，"
        "只补最关键的那条理由；对方追问再展开。"
    ),
}


@dataclass(frozen=True)
class ReplyGateResult:
    """一次分类的结论与观测值；``result_code != "ok"`` 表示没有分类可用。"""

    category: str = ""
    reason: str = ""
    source: str = "fast"
    result_code: str = "ok"
    model: str = ""
    latency_ms: int = 0
    completion_tokens: int | None = None
    usage_id: int | None = None

    @property
    def usable(self) -> bool:
        return self.result_code == "ok" and self.category in CATEGORIES

    def factor(self) -> dict[str, Any]:
        """写进 ``decision_log.factors.reply_gate`` 的观测值。"""
        return {
            "category": self.category or None,
            "reason": self.reason,
            "source": self.source,
            "result_code": self.result_code,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "completion_tokens": self.completion_tokens,
            "usage_id": self.usage_id,
        }


def attach_note(event: MessageEvent, result: ReplyGateResult) -> None:
    """把本轮分类挂在这条事件上，决策日志写库时并进同一行 factors。

    事件对象就是本轮裁定与投递共用的那一个，不另建映射表，也不会串到
    下一轮。
    """
    try:
        setattr(event, "reply_gate_note", result)
    except Exception:  # noqa: BLE001 - 挂不上只少一条审计
        logger.debug("回复门结果挂载失败")


def note_for(event: MessageEvent | None) -> ReplyGateResult | None:
    if event is None:
        return None
    note = getattr(event, "reply_gate_note", None)
    return note if isinstance(note, ReplyGateResult) else None


def factor_for(event: MessageEvent | None) -> dict[str, Any] | None:
    """决策日志取本轮分类观测值；没有跑过分类时返回 ``None``。"""
    note = note_for(event)
    return note.factor() if note is not None else None


def parse_gate(raw: str) -> tuple[str, str, str]:
    """解析分类输出，返回 ``(category, reason, result_code)``。

    只认四种分类之一；多包一层 Markdown、前后夹话都能容忍，认不出来就交回
    ``invalid_json`` / ``invalid_category``，由调用方按原流程继续。
    """
    data = parse_json_payload(raw or "")
    if not isinstance(data, dict):
        return "", "", "invalid_json"
    reason = data.get("reason")
    reason = " ".join(reason.split())[:40] if isinstance(reason, str) else ""
    category = data.get("category")
    category = category.strip() if isinstance(category, str) else ""
    if category not in CATEGORIES:
        return "", reason, "invalid_category"
    return category, reason, "ok"


def guidance(category: str, marker: str) -> str:
    template = _GATE_GUIDANCE.get(category)
    return template.format(marker=marker) if template else ""


def _accepts_kwarg(callable_: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(callable_).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )


def _gate_model(bot: Any) -> str:
    llm = getattr(bot, "llm", None)
    config = getattr(llm, "config", None) or getattr(bot, "config", None)
    model_for = getattr(getattr(config, "llm", config), "model_for", None)
    if not callable(model_for):
        return ""
    try:
        return str(model_for(GATE_ROLE))
    except Exception:  # noqa: BLE001 - 只用于记账
        return ""


def _timeout(bot: Any) -> float:
    llm = getattr(getattr(bot, "config", None), "llm", None)
    try:
        configured = float(getattr(llm, "timeout", 0.0) or 0.0)
    except (TypeError, ValueError):
        configured = 0.0
    return max(0.1, min(GATE_TIMEOUT_CAP, configured) if configured > 0 else GATE_TIMEOUT_CAP)


def _room_state_line(messages: list[dict[str, str]]) -> str:
    """从已拼好的上下文里取第 36 项写好的房间状态行，不重新算一遍。

    生成上下文里系统提示带「房间状态：…」一行；直接复用这一行，分类调用
    看到的指向事实与主生成完全一致，也不必再读库、再拼历史。
    """
    for message in messages:
        if message.get("role") != "system":
            continue
        for line in str(message.get("content") or "").splitlines():
            if line.startswith("房间状态："):
                return line.strip()
    return ""


def build_gate_messages(
    bot: Any,
    event: MessageEvent,
    messages: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """分类调用的输入：36 号的房间状态 + 固定范例 + 本批原话。

    不重复拼记忆、历史或工具说明，保持这一调用便宜。房间状态优先从已拼好的
    上下文里取；调用方没给上下文时才现算一次（与 36 号同一函数）。
    """
    room = _room_state_line(messages or [])
    if not room:
        try:
            room = bot._room_state([event])
        except Exception:  # noqa: BLE001 - 少一行线索照常分类
            room = ""
    text = " ".join((event.text or "").split())
    body = f"{event.sender_name or event.sender_id}：{text}" if text else "（本批只有表情或空白）"
    return [
        {"role": "system", "content": "\n".join((_GATE_PROMPT, _REGISTER_EXAMPLES, _EXTRA_EXAMPLES))},
        {"role": "user", "content": f"{room}\n【本批原话】\n{body}".lstrip()},
    ]


async def classify_reply(
    bot: Any,
    event: MessageEvent,
    messages: list[dict[str, str]] | None = None,
) -> ReplyGateResult:
    """跑一次便宜分类；任何失败都返回不可用的结果，由调用方按原流程继续。"""
    llm = getattr(bot, "llm", None)
    complete = getattr(llm, "complete", None)
    model = _gate_model(bot)
    if not callable(complete):
        return ReplyGateResult(source="fallback", result_code="no_llm", model=model)
    request = build_gate_messages(bot, event, messages)
    audit = CompletionAudit()
    kwargs: dict[str, Any] = {"role": GATE_ROLE, "task": GATE_TASK, "json_mode": True}
    if _accepts_kwarg(complete, "max_tokens"):
        kwargs["max_tokens"] = GATE_MAX_TOKENS
    # 与策划步同口径：只有真客户端才传审计对象，旧包装器不猜。
    if isinstance(llm, LLMClient) and _accepts_kwarg(complete, "audit"):
        kwargs["audit"] = audit
    started = time.perf_counter()
    try:
        raw = await asyncio.wait_for(complete(request, **kwargs), _timeout(bot))
    except Exception as exc:  # noqa: BLE001 - 分类失败不影响主流程
        code = request_result_code(exc)
        logger.warning("回复门分类失败，按原流程继续：%s", code)
        return ReplyGateResult(
            source="fallback", result_code=code, model=model,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    category, reason, code = parse_gate(raw)
    if code != "ok":
        logger.debug("回复门分类不可用（%s）：%s/%s", code, event.platform, event.chat_id)
    return ReplyGateResult(
        category=category, reason=reason, result_code=code, model=model,
        latency_ms=latency_ms, completion_tokens=audit.completion_tokens, usage_id=audit.usage_id,
    )


def resolve_action(result: ReplyGateResult, event: MessageEvent, bot: Any) -> tuple[str, str]:
    """把分类结论落成动作：返回 ``(action, category)``。

    ``action`` 为 ``talk``（照常生成，可能带口吻指导）或 ``silent``（本轮不生成）。
    分类不可用时一律 ``talk``，不改变原行为；直接叫到 bot 的批次不会因「不理」
    整轮沉默，改按灌水短接。
    """
    if not result.usable:
        return "talk", ""
    category = result.category
    if category != "不理":
        return "talk", category
    engine = getattr(bot, "decision_engine", None)
    is_hard = getattr(engine, "is_hard_trigger", None)
    if callable(is_hard):
        try:
            if is_hard(event):
                return "talk", FALLBACK_CATEGORY
        except Exception:  # noqa: BLE001 - 判定失败不沉默
            return "talk", FALLBACK_CATEGORY
    return "silent", category


def _has_words(event: MessageEvent) -> bool:
    """这条消息是否真有可分类的文字（纯图、纯表情不算）。"""
    text = (event.text or "").strip()
    if not text or not _visual_placeholder(text):
        return bool(text)
    for token in ("[图片]", "[表情]", "[动画表情]"):
        text = text.replace(token, "")
    return bool(text.strip())


async def apply(
    bot: Any,
    event: MessageEvent,
    messages: list[dict[str, str]],
    *,
    marker: str,
) -> tuple[list[dict[str, str]], str]:
    """在生成前跑回复门，返回 ``(messages, silence)``。

    ``silence`` 非空时调用方原样返回它，让既有的否决标记通道记下这一轮；
    否则把按类口吻指导追加到生成上下文末尾。

    只在群聊、有文字可判的轮次跑（纯图、纯表情分类不了）：私聊和直接叫到 bot
    的话本来就是在找人，不需要再判一次。同一条事件只分类一次，重写草稿的
    第二次生成复用第一次结论。
    """
    if not event.is_group or not _has_words(event):
        return messages, ""
    result = note_for(event)
    if result is None:
        result = await classify_reply(bot, event, messages)
        attach_note(event, result)
    action, category = resolve_action(result, event, bot)
    if action == "silent":
        logger.info(
            "回复门静默（%s，%sms）：%s/%s", result.reason or "无理由", result.latency_ms,
            event.platform, event.chat_id,
        )
        return messages, marker
    note = guidance(category, marker)
    if not note:
        return messages, ""
    if any("【回复门·本批性质】" in str(message.get("content") or "") for message in messages):
        # 重写草稿的第二遍：同一份上下文里已经带过这句，不重复叠。
        return messages, ""
    # 并进已有的末尾指导块，不另起一条消息：生成侧的「最后一条是内部指导」形状保持
    # 不变，也免得同一条上下文里出现两段并排的系统指令。
    if messages and messages[-1].get("role") == "system":
        last = dict(messages[-1])
        last["content"] = f"{last.get('content') or ''}\n\n{note}"
        return [*messages[:-1], last], ""
    return [*messages, {"role": "system", "content": note}], ""
