"""提醒工具：把用户明确要求的提醒落到 ``scheduled_jobs``。

模型只能通过工具调用创建提醒（回复里说「好，我 X 点提醒你」不算数），
所以每会话待执行提醒有上限，防止模型被反复要求时无限堆积。工具定义、
参数解析与文案都收在这里，core 只负责接线与到点发送。

一期只支持一次性时间点：``delay_minutes``（优先，模型填这个最不易错）
或 ``at_iso``。不引 cron、不做周期提醒。

到点提示按「请求者与被提醒者的关系」渲染：``set_reminder`` 只支持请求者
本人（提醒我），所以正常情况下两者是同一人，提示里不出现任何姓名，把
对方当在场的人用第二人称或直接说事；姓名只在「A 让提醒 B」且 A≠B 时出现
（工具目前不会产出这种 job，渲染分支留给以后），并写明是谁让提醒谁。
关系由 payload 里的结构化 id 判定，不从姓名字符串推断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

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


@dataclass(frozen=True)
class ReminderParties:
    """一条提醒的请求者与被提醒者，以及到点该怎么说这件事。

    ``same_person=True``：请求者本人就是被提醒的人（``set_reminder`` 的唯一
    形态）。这时不要任何姓名，像对在场的人说话一样直接说事。
    ``same_person=False``：A 让提醒 B（A≠B），说话对象是 B，A 以第三人称
    出现，并且写清是 A 让提醒谁——「A」托你提醒「B」这种关系事实，不让模型
    自己猜「你」是谁。
    """

    same_person: bool = True
    requester_name: str = ""
    recipient_name: str = ""

    def scene_line(self, text: str) -> str:
        """渲染「之前谁让提醒什么」这一句（不含「现在时间到了」）。"""
        if self.same_person:
            # 请求者=接收者：第二人称，不出现姓名（名字会被模型当第三人叙述）。
            return f"之前是对方亲口托你提醒这件事：{text}。"
        # A≠B：姓名只在此时出现，并写明谁托谁、该提醒谁；「你」始终指 bot。
        who = self.requester_name or "别人"
        whom = self.recipient_name or "眼前这个人"
        return (
            f"这条提醒不是你自己安排的：是「{who}」托你提醒「{whom}」，事情是：{text}。"
        )


def parties_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    platform: str = "",
    chat_type: str = "",
    chat_id: str = "",
) -> ReminderParties:
    """从 job 的结构化字段判定请求者与被提醒者是不是同一个人。

    ``requester_id`` 是创建时按平台账号写死的请求者；接收者优先取 payload
    的 ``recipient_id``，私聊时就是会话对方 ``platform:chat_id``，群聊没有
    单独的接收人就按请求者本人算（一期 ``set_reminder`` 只支持「提醒我」）。
    ``requester_name`` / ``recipient_name`` 只作台账与第三人称称呼，不参与
    判定；缺 id 或 id 相同都按本人提醒本人处理——宁可少提一个名字，也不把
    本人写成第三人称。
    """
    data = payload or {}
    requester_id = str(data.get("requester_id") or "").strip()
    recipient_id = str(data.get("recipient_id") or "").strip()
    if not recipient_id and chat_type == "private" and platform and chat_id:
        recipient_id = f"{platform}:{chat_id}"
    same = not (requester_id and recipient_id) or requester_id == recipient_id
    return ReminderParties(
        same_person=same,
        requester_name=str(data.get("requester_name") or "").strip(),
        recipient_name=str(data.get("recipient_name") or "").strip(),
    )


def build_reminder_prompt(
    *,
    persona: str,
    text: str,
    parties: ReminderParties,
    is_private: bool,
    late: bool,
) -> list[dict[str, str]]:
    """到点发送前的生成提示：让模型按提醒内容现写一句自然的话。

    调用方传 :class:`ReminderParties`（由 :func:`parties_from_payload` 从
    结构化字段判定），不传姓名字符串——请求者与被提醒者是同一人时姓名
    绝不能以第三人称直灌进来。
    """
    scene = "私聊" if is_private else "群聊"
    if parties.same_person:
        target = "对方现在就在这场对话里" if not is_private else "对方正在跟你私聊"
        ask = (
            f"{parties.scene_line(text)}现在时间到了。{target}，你直接对对方说这件事。"
        )
    else:
        ask = (
            f"{parties.scene_line(text)}现在时间到了，你该提醒的人是「"
            f"{parties.recipient_name or '眼前这个人'}」，请对这个人说这件事。"
        )
    lines = [
        persona,
        (
            f"当前是{scene}。{ask}"
            "用一句自然的话提醒，像朋友随口一说，不要用"
            "「提醒您」「温馨提示」这种模板腔，不要编造额外信息。"
            + ("" if parties.same_person else
               "被提醒的人不是托你提醒的人：说「你」时指的是被提醒者，别把请求者说的「我」当成你。")
        ),
    ]
    if late:
        lines.append(
            "注意：你的进程刚重启或忙了很久，这条提醒比约定时间晚了不少。"
            "用一句话自然说明「来晚了」，别假装准时，也别道歉个没完。"
        )
    return [{"role": "system", "content": "\n\n".join(lines)}]
