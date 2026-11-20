"""表达决策书：代码定长度预算、校验增量与历史出处，模型只提议怎么接。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

INTENTS = ("接梗", "补充新信息", "反驳", "提问", "短附和", "沉默")
BUDGETS = {"碎片": "≤10 字", "短句": "11–25 字", "展开": "26–60 字"}
MAX_PLAN_CHARS = 500
MAX_INCREMENT = 50
MAX_EXCERPT = 40


@dataclass(frozen=True)
class ExpressionDecision:
    intent: str
    length_band: str
    increment: str
    avoid_repetition: tuple[str, ...]
    source: str = "fast"

    def prompt_block(self) -> str:
        return (
            "【表达决策书·内部回复指导】只发正文，不要复述这份指导。\n"
            f"意图：{self.intent}；长度档：{self.length_band}（{BUDGETS[self.length_band]}）。\n"
            f"增量点：{self.increment or '无，不展开'}\n"
            "禁复述清单（不要重复这些观点和措辞）："
            + json.dumps(self.avoid_repetition, ensure_ascii=False)
        )

    def to_dict(self) -> dict:
        return asdict(self)


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


def parse_plan(raw: str, band: str, recent: list[str]) -> ExpressionDecision:
    """非结构化输出不作指导；模型故障只允许保守的碎片呼应。"""
    try:
        data = json.loads(raw) if len(raw) <= MAX_PLAN_CHARS else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get('intent') not in INTENTS:
        return ExpressionDecision("短附和", "碎片", "", repetition_list(recent), "fallback")
    intent = data['intent']
    increment = data.get('increment')
    increment = ' '.join(increment.split())[:MAX_INCREMENT] if isinstance(increment, str) else ''
    if increment.strip('。.!！ ') in ('无', '没有', '无增量', '没有新信息', '无新信息'):
        increment = ''
    if not increment and intent != '短附和':
        intent = '沉默'
    return ExpressionDecision(intent, '碎片' if intent == '短附和' else band, increment,
                              repetition_list(recent, data.get('avoid_repetition')))
