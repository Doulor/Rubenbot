"""提醒工具：把用户明确要求的提醒落到 ``scheduled_jobs``。

模型只能通过工具调用创建提醒（回复里说「好，我 X 点提醒你」不算数），
所以每会话待执行提醒有上限，防止模型被反复要求时无限堆积。工具定义、
参数解析与文案都收在这里，core 只负责接线与到点发送。

一期只支持一次性时间点：``delay_minutes``（优先，模型填这个最不易错）
或 ``at_iso``。不引 cron、不做周期提醒。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# OpenAI 兼容的工具定义；core 在模型支持工具调用时与 recall 一起下发
REMINDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "为用户设置一条一次性提醒。只有用户明确要求「提醒我」「到点告诉我」时才调用；"
            "不要在闲聊里主动设置提醒。到点后你会在原会话里说一句话提醒用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "到时候要提醒的事情，用一句话写清楚（例如「吃药」「下午三点开会」）",
                },
                "delay_minutes": {
                    "type": "number",
                    "description": "从现在起多少分钟后提醒；例如半小时后填 30，明天早上填 600",
                },
                "at_iso": {
                    "type": "string",
                    "description": (
                        "可选：绝对时间，ISO 格式如 2026-11-20T09:30；"
                        "没有时区按本地时间。给了 delay_minutes 时以它为准。"
                    ),
                },
            },
            "required": ["text"],
        },
    },
}


def resolve_fire_at(
    args: dict[str, Any],
    now: float,
    *,
    max_delay_minutes: int,
) -> tuple[float | None, str | None]:
    """把工具参数解析成（到点时间, 错误说明）。

    优先 ``delay_minutes``；没有它才看 ``at_iso``。两个都没给或格式不对时
    返回 ``(None, 给模型看的错误说明)``，由调用方原样回给模型。
    """
    delay = args.get("delay_minutes")
    if delay not in (None, ""):
        try:
            minutes = float(delay)
        except (TypeError, ValueError):
            return None, "delay_minutes 必须是数字（分钟）"
        if minutes <= 0:
            return None, "delay_minutes 必须大于 0"
        if minutes > max_delay_minutes:
            return None, f"提醒最远只能设 {max_delay_minutes} 分钟（约 {max_delay_minutes // 1440} 天）以内"
        return now + minutes * 60.0, None

    at_iso = str(args.get("at_iso") or "").strip()
    if not at_iso:
        return None, "需要给出 delay_minutes（多少分钟后）或 at_iso（绝对时间）"
    text = at_iso[:-1] + "+00:00" if at_iso.endswith("Z") else at_iso
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None, f"at_iso 格式不对：{at_iso}（应为 2026-11-20T09:30 这种 ISO 格式）"
    fire_at = moment.timestamp()
    # 先判过去再判太远：过去的时间往往同时超出上限，报「过去」对模型更好修正
    if fire_at <= now:
        return None, "at_iso 是过去的时间，请改成一个未来的时间"
    if fire_at - now > max_delay_minutes * 60.0:
        return None, f"提醒最远只能设 {max_delay_minutes} 分钟（约 {max_delay_minutes // 1440} 天）以内"
    return fire_at, None


def describe_fire_at(fire_at: float, now: float) -> str:
    """给模型看的确认文案：几分钟后 / 今天几点 / 哪天几点。"""
    seconds = fire_at - now
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} 分钟后"
    local_fire = datetime.fromtimestamp(fire_at)
    local_now = datetime.fromtimestamp(now)
    if local_fire.date() == local_now.date():
        return local_fire.strftime("%H:%M")
    return local_fire.strftime("%m-%d %H:%M")


def build_reminder_prompt(
    *,
    persona: str,
    text: str,
    requester: str,
    is_private: bool,
    late: bool,
) -> list[dict[str, str]]:
    """到点发送前的生成提示：让模型按提醒内容现写一句自然的话。"""
    scene = "私聊" if is_private else "群聊"
    lines = [
        persona,
        (
            f"当前是{scene}。之前{requester or '对方'}让你提醒一件事：{text}。"
            "现在时间到了，请用一句自然的话提醒，像朋友随口一说，不要用"
            "「提醒您」「温馨提示」这种模板腔，不要编造额外信息。"
        ),
    ]
    if late:
        lines.append(
            "注意：你的进程刚重启或忙了很久，这条提醒比约定时间晚了不少。"
            "用一句话自然说明「来晚了」，别假装准时，也别道歉个没完。"
        )
    return [{"role": "system", "content": "\n\n".join(lines)}]
