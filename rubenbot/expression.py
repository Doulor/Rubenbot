"""表达决策书：代码定长度预算、校验增量与历史出处，模型只提议怎么接。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

COMPUTER_INTENT = '操作电脑'
INTENTS = ("接梗", "补充新信息", "反驳", "提问", "短附和", "表情回应", "沉默")
BUDGETS = {"碎片": "≤10 字", "短句": "11–25 字", "展开": "26–60 字"}
MAX_PLAN_CHARS = 500
MAX_INCREMENT = 50
MAX_EXCERPT = 40
MAX_REASON = 60


@dataclass(frozen=True)
class CapabilityNote:
    """索引只给标题和自述；策划步选中标题，生成步才读到对应情境说明。

    ``available=False`` 表示这一项此刻做不了（例如插件没启用）。这时 ``title``
    是能力名，``summary`` 是它此刻的短状态（如「还没开」）；索引把同一状态的
    合进「现在还做不了的」一句，不混进「这轮我能做的事」，并要求被明确要时
    照那句话开口，不沉默、也不装作能做。
    """

    title: str
    summary: str
    detail: str
    available: bool = True


def capability_index(notes: tuple[CapabilityNote, ...]) -> str:
    """把本轮能力渲染成策划步的一段自述。

    能做的逐项列标题与自述；做不了的（没启用的插件）按同一状态并成一句
    「点歌、画图还没开。」，并要求被明确要时照着那句话开口——否则这类请求
    会掉进普通聊天，被策划当成闲聊沉默掉，机主分不清是没开还是坏了。
    """
    if not notes:
        return ''
    ready = [n for n in notes if n.available]
    ready_titles = {n.title for n in ready}
    # 同一状态归一句；summary 对不可用条目就是状态短语（如「还没开」）
    off: dict[str, list[str]] = {}
    for note in notes:
        if note.available or note.title in ready_titles:
            continue
        titles = off.setdefault(note.summary, [])
        if note.title not in titles:
            titles.append(note.title)
    blocks = []
    if ready:
        blocks.append('【这轮我能做的事】\n' + '\n'.join(f'〔{n.title}〕{n.summary}' for n in ready))
        blocks.append('需要哪项的具体说明，就把标题列入 JSON 的 capabilities 数组；不用可省略。')
    if off:
        blocks.append('【现在还做不了的】' + ''.join(f'{"、".join(titles)}{state}。'
                                                    for state, titles in off.items()) +
                      '只有这条消息确实是在要其中某项，才照着这句话开口：一句话说完，不列清单，'
                      '也别许诺以后能办；平时不用主动提。'
                      '这是本轮索引里的确定事实：basis 用 settled，speak 用 true。'
                      '别沉默，也别装作能做或拿别的凑数。')
    return '\n' + '\n'.join(blocks)


@dataclass(frozen=True)
class ExpressionDecision:
    intent: str
    length_band: str
    increment: str
    avoid_repetition: tuple[str, ...]
    source: str = "fast"
    # 策划步的开口裁定：None 表示没有裁定（未返回该字段）
    speak: bool | None = None
    # 裁定理由摘要（进 decision_log 的 factors，便于审计）
    reason: str = ""
    capabilities: tuple[str, ...] = ()
    # 缺报、非法值或策划失败都没有提供依据，不能默认为常识。
    basis: str = "guess"
    deliver_file: bool = False

    def prompt_block(self) -> str:
        uncertainty = "没把握就明说不确定，不把推测写成事实。" if self.basis == "guess" else ""
        return (
            "【表达决策书·内部回复指导】发出去的只有聊天正文。\n"
            f"依据：{self.basis}。{uncertainty}只有 ledger 或 settled 有对应记录才可说自己的动作、所有物或经历。\n"
            f"意图：{self.intent}；长度档：{self.length_band}（{BUDGETS[self.length_band]}）。\n"
            f"这次想接的那一点：{self.increment or '简短回应'}\n"
            "已经说过的部分（背景）："
            + json.dumps(self.avoid_repetition, ensure_ascii=False)
        )

    def to_dict(self) -> dict:
        return asdict(self)


def coerce_speak(value: object) -> bool | None:
    """把模型给的 speak 字段归一成布尔；认不出来返回 None（不给裁定）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "1", "说", "回", "开口"):
            return True
        if text in ("false", "no", "0", "不说", "不回", "沉默"):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return None


def choose_length_band(roll: float, *, mentioned_question: bool, casual_group: bool) -> str:
    """按累积分界掷档：基础 40/40/20，点名提问 20/40/40，闲聊 55/35/10。"""
    cuts = (0.2, 0.6) if mentioned_question else (0.55, 0.9) if casual_group else (0.4, 0.8)
    return "碎片" if roll < cuts[0] else "短句" if roll < cuts[1] else "展开"


def is_question(text: str) -> bool:
    return bool(re.search(r"[？?]|为什么|什么|怎么|为何|哪个|多少|能不能|是不是|[吗么][。！\s]*$", text))


def repetition_list(recent: list[str], proposed: object = None) -> tuple[str, ...]:
    """每条历史最多摘一处；模型的摘录须有原文出处，否则用原句的短片段。"""
    excerpts = proposed if isinstance(proposed, list) else []
    result = []
    for text in recent[-3:]:
        if not text or text in ("[表情]", "[图片]"):
            continue
        phrase = next((s for s in excerpts if isinstance(s, str) and 2 <= len(s) <= MAX_EXCERPT and s in text), None)
        phrase = phrase or " ".join(text.split())[:MAX_EXCERPT]
        if phrase and phrase not in result:
            result.append(phrase)
    return tuple(result)


def parse_plan(raw: str, band: str, recent: list[str], notes: tuple[CapabilityNote, ...] = (),
               *, computer_route: bool = False) -> ExpressionDecision:
    """非结构化输出不作指导；模型故障只允许保守的碎片呼应。

    ``speak`` 是策划步的开口裁定：模型给了就照它的；只写 intent=沉默 也
    视为不说；两样都没有时视为「没裁定」（None，交回调用方兜底）。
    """
    try:
        text = raw.strip()
        # 部分兼容中转即使请求 JSON 也会包一层 Markdown；缺尾或截断仍算无裁定。
        if text.startswith('```'):
            match = re.match(r'```(?:json)?\s*(.*?)\s*```', text, re.S | re.I)
            text = match[1] if match else ''
        # 裁定后偶尔夹一行聊天正文，不执行、不采用，只解析首个完整 JSON 对象。
        data, end = json.JSONDecoder().raw_decode(text[:MAX_PLAN_CHARS + 1])
        if end > MAX_PLAN_CHARS:
            data = None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return ExpressionDecision("短附和", "碎片", "", repetition_list(recent), "fallback")
    basis = data.get('basis')
    if basis not in ('ledger', 'settled', 'common', 'guess'):
        basis = 'guess'
    speak = coerce_speak(data.get('speak'))
    intent = data.get('intent')
    # 有些旧裁定用空意图表示「看过就划走」；明确的 false 仍是有效裁定。
    if speak is False and intent in (None, ''):
        intent = '沉默'
    if intent == COMPUTER_INTENT and computer_route and speak is not False:
        return ExpressionDecision(COMPUTER_INTENT, band, '', repetition_list(recent), speak=True, basis=basis,
                                  deliver_file=data.get('deliver_file') is True)
    if intent not in INTENTS:
        return ExpressionDecision("短附和", "碎片", "", repetition_list(recent), "fallback")
    increment = data.get('increment')
    increment = ' '.join(increment.split())[:MAX_INCREMENT] if isinstance(increment, str) else ''
    if increment.strip('。.!！ ') in ('无', '没有', '无增量', '没有新信息', '无新信息'):
        increment = ''
    if not increment and intent not in ('短附和', '表情回应'):
        intent = '沉默'
    if speak is None:
        speak = False if intent == '沉默' else True
    reason = data.get('reason')
    reason = ' '.join(reason.split())[:MAX_REASON] if isinstance(reason, str) else ''
    return ExpressionDecision(intent, '碎片' if intent == '短附和' else band, increment,
                              repetition_list(recent, data.get('avoid_repetition')),
                              basis=basis, speak=speak, reason=reason,
                              capabilities=tuple(n.title for n in notes if isinstance(data.get('capabilities'), list)
                                                 and n.title in data['capabilities']))
