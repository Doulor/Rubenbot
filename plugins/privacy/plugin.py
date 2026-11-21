"""内置插件：隐私命令与灰区异步询问。

两类功能：

1. **命令**（任何人可在私聊或群里发）：
   - ``/隐私``：查看自己当前的档位，以及「bot 记得我什么」（画像全文 + 事件计数）
   - ``/隐私 模式 正常|隐私|不记``：切换档位；切到「不记」时说明存量数据保留但
     不再新增
   - ``/隐私 清空``：删除自己的全部画像/事件/事实，并在 ``memory_log`` 留痕
   - ``/隐私 可以`` / ``/隐私 不行``：回答待确认的询问（不想打字时的命令入口）

2. **待询问投递**（仅私聊）：
   - 本人与 bot 私聊、bot 正常回复后，如果这人还有 ``pending_asks``，在回复后
     附一条自然的确认问句（一次最多问一条，且同一问题不反复打扰）
   - 本人的下一条回复按「可以/不行」解析：清楚的就落裁定（``rulings``）并把
     待询问标记为已答复；拿不准的保持 pending 并说明

隐私判断本身不在这里：拿到「能不能说」的结论一律来自
``rubenbot.memory.gate``，这个插件只负责问、记、以及把命令翻译成对
``store`` 的操作。
"""

from __future__ import annotations

import logging
import time

from rubenbot.adapters.base import Segment
from rubenbot.memory.store import VALID_PRIVACY_MODES, make_person_id

logger = logging.getLogger(__name__)

MODE_NAMES = {"normal": "正常", "private": "隐私", "norecord": "不记"}
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}

# 同一条询问的投递冷却：隔这么久还没回答才允许再问一次
ASK_COOLDOWN_SECONDS = 12 * 3600
# 投递出去后，只有这段时间内的回复会被当作「对询问的回答」
ASK_ANSWER_WINDOW_SECONDS = 24 * 3600
# 短于这个长度的回复才当作「对询问的回答」；更长的按正常聊天处理
ANSWER_MAX_CHARS = 12

# 只认这些短答复：先精确匹配，再在极短回复里做包含匹配，避免把
# 「我去银行」「别开玩笑了」这类正常聊天误判成回答。
_ALLOW_EXACT = {"可以", "行", "好", "好的", "好啊", "好吧", "没问题", "说吧", "能说", "当然", "可以啊", "可以吧", "ok", "okay", "yes"}
_DENY_EXACT = {"不行", "不可以", "不要", "别说", "不能说", "不能", "不好", "不用", "别", "no", "nope"}
_ALLOW_HINTS = ("可以", "能说", "说吧", "没问题", "当然")
_DENY_HINTS = ("不行", "不要", "别说", "不能说", "不能", "不用", "不许")


def parse_answer(text: str) -> str | None:
    """把本人对询问的回复解析成 allow / deny；认不出返回 None。"""
    value = "".join((text or "").split()).strip("。.!！~～,，")
    if not value:
        return None
    lowered = value.lower()
    if lowered in _DENY_EXACT or value in _DENY_EXACT:
        return "deny"
    if lowered in _ALLOW_EXACT or value in _ALLOW_EXACT:
        return "allow"
    # 极短回复（如「那可以吧」「不行啦」）才做包含匹配；否定优先
    if len(value) <= 6:
        if any(hint in value for hint in _DENY_HINTS):
            return "deny"
        if any(hint in value for hint in _ALLOW_HINTS):
            return "allow"
    return None


def is_command(text: str, prefix: str = "/") -> bool:
    return bool(prefix) and (text or "").strip().startswith(prefix)


def register(ctx):  # noqa: C901 - 插件入口，分支清晰即可
    store = ctx.store
    # 每人最近一次投递的询问：(ask_id, delivered_at)
    last_ask: dict[str, tuple[int, float]] = {}
    # 每人上次投递时间，用于投递冷却
    last_delivered: dict[str, float] = {}

    def person_of(event) -> str:
        return make_person_id(event.platform, event.sender_id)

    async def send(runtime, event, text: str) -> None:
        """插件自己发一条消息（命令回复之外的补充消息）。"""
        adapter = runtime.adapters.get(event.platform)
        if adapter is None:
            logger.debug("[privacy] 没有平台 %s 的适配器，发不出去", event.platform)
            return
        try:
            await adapter.send(event.chat_id, [Segment.text(text)], chat_type=event.chat_type)
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮主循环
            logger.warning("[privacy] 发送失败：%s", exc)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @ctx.command("隐私")
    async def privacy_command(runtime, event, args):
        person_id = person_of(event)
        text = (args or "").strip()
        if not text:
            return _report(store, person_id, private=event.is_private)
        parts = text.split()
        head = parts[0]
        if head in ("模式", "mode"):
            return _set_mode(store, person_id, parts[1] if len(parts) > 1 else "")
        if head in ("清空", "删除", "clear"):
            return _purge(store, person_id)
        if head in ("可以", "不行"):
            return _answer_by_command(
                store, event, person_id, "allow" if head == "可以" else "deny", last_ask
            )
        return (
            "用法：/隐私 查看｜/隐私 模式 正常|隐私|不记｜/隐私 清空\n"
            "（有想问你的待确认事项时，也可以直接回 /隐私 可以 或 /隐私 不行）"
        )

    # ------------------------------------------------------------------
    # 待询问投递
    # ------------------------------------------------------------------

    @ctx.on_after_reply
    async def deliver_pending_ask(runtime, event, reply):
        if not event.is_private:
            return
        person_id = person_of(event)
        try:
            if store.is_norecord(person_id):
                return
            asks = store.list_pending_asks(person_id, status="pending", limit=10)
        except Exception as exc:  # noqa: BLE001 - 投递失败不影响正常聊天
            logger.warning("[privacy] 读取待询问失败：%s", exc)
            return
        if not asks:
            return
        now = time.time()
        if now - last_delivered.get(person_id, 0.0) < ASK_COOLDOWN_SECONDS:
            # 刚问过，别连着追问；只刷新“最近一次投递”的指向，让短答复仍能对上
            return
        ask = asks[0]
        last_delivered[person_id] = now
        last_ask[person_id] = (ask.id, now)
        await send(runtime, event, ask.question)

    @ctx.on_message
    async def answer_pending_ask(runtime, event):
        if not event.is_private:
            return True
        if is_command(event.text, runtime.config.plugins.command_prefix):
            return True  # 命令先交给命令通道（/隐私 可以 等）
        person_id = person_of(event)
        pending = last_ask.get(person_id)
        if pending is None:
            return True
        ask_id, delivered_at = pending
        if time.time() - delivered_at > ASK_ANSWER_WINDOW_SECONDS:
            last_ask.pop(person_id, None)
            return True
        text = (event.text or "").strip()
        verdict = parse_answer(text)
        if verdict is not None:
            await _apply_answer(runtime, event, ask_id, verdict, person_id, last_ask)
            return False
        if len(text) <= ANSWER_MAX_CHARS:
            # 短回复但没看懂：不落裁定，保持 pending，并说明一下
            await send(runtime, event, "我没太看明白——可以直接回我「可以」或「不行」。")
            return False
        # 正常聊天：放行，等本人有空再答
        return True

    return {
        "commands": ["隐私"],
        "hooks": ["on_after_reply", "on_message"],
    }


# ----------------------------------------------------------------------
# 命令实现（纯函数 + store 操作，方便测试）
# ----------------------------------------------------------------------


def _report(store, person_id: str, *, private: bool = True) -> str:
    """``/隐私``：当前档位 + bot 记得我什么。

    画像属于**只在私聊里摊开**的东西：群里发 ``/隐私`` 只回报档位并指路，
    免得自己的档案被群里所有人看到（档位本身不算隐私）。
    """
    mode = store.get_privacy_mode(person_id)
    lines = [f"你现在的隐私档位：{MODE_NAMES.get(mode, mode)}"]
    if not private:
        lines.append("想看「我记得你什么」，私聊里发 /隐私。")
        return "\n".join(lines)
    profile = store.get_profile(person_id)
    events = store.count_events(person_id=person_id)
    memories = store.count_memories(person_id=person_id)
    if mode == "norecord":
        lines.append("（不记模式下不新增画像与时间线；之前留下的记录还保留着，可以发 /隐私 清空 删除）")

    if profile is None or (not profile.stable_lines and not profile.status_lines):
        lines.append("我还没有关于你的画像。")
    else:
        lines.append("我目前记得的关于你的事：")
        if profile.stable_lines:
            lines.append("· 稳定印象：")
            lines.extend(f"  - {line}" for line in profile.stable_lines)
        if profile.status_lines:
            lines.append("· 最近近况：")
            lines.extend(f"  - {line}" for line in profile.status_lines)
    lines.append(f"另外还有 {events} 条关于你的事件、{memories} 条零散记忆。")
    return "\n".join(lines)


def _set_mode(store, person_id: str, name: str) -> str:
    key = name.strip()
    if key in VALID_PRIVACY_MODES:
        mode = key
    else:
        mode = MODE_BY_NAME.get(key, "")
    if not mode:
        return "档位只能是：正常 / 隐私 / 不记。例如：/隐私 模式 隐私"
    old = store.get_privacy_mode(person_id)
    store.set_privacy_mode(person_id, mode)
    label = MODE_NAMES[mode]
    if mode == "norecord":
        return (
            f"已经切到「{label}」：以后不再给你建画像、记时间线（你说的内容只在短期上下文里用）。\n"
            "之前留下的记录还保留着；如果想删干净，发 /隐私 清空。"
        )
    if mode == "private":
        return (
            f"已经切到「{label}」：关于你的事我不会在别人面前提起，"
            "除非你之前明确说过某件事可以说。"
        )
    if old == "norecord":
        return f"已经切回「{label}」：之后会正常记录。"
    return f"已经切到「{label}」：我会像平常一样了解你、记着你的事。"


def _purge(store, person_id: str) -> str:
    try:
        counts = store.purge_person(person_id, reason="本人通过 /隐私 清空")
    except Exception as exc:  # noqa: BLE001 - 删除失败要如实告诉本人
        logger.warning("[privacy] 清空失败：%s", exc)
        return "清空失败了，稍后再试一次吧。"
    return (
        "已经清空：删掉了关于你的画像、"
        f"{counts.get('events', 0)} 条事件、{counts.get('memories', 0)} 条记忆，"
        "以及相关的裁定和待确认事项。"
    )


def _answer_by_command(
    store, event, person_id: str, verdict: str, last_ask: dict
) -> str:
    """命令形式的答复（``/隐私 可以`` / ``/隐私 不行``）。

    命令回复会被插件命令通道发出去，这里只写裁定并返回一句确认，不自己
    再发一条——否则本人会收到两遍同样的回答。
    """
    pending = last_ask.get(person_id)
    ask_id = pending[0] if pending else None
    if ask_id is None:
        asks = store.list_pending_asks(person_id, status="pending", limit=1)
        ask_id = asks[0].id if asks else None
    if ask_id is None:
        return "现在没有等你确认的事。"
    try:
        ask = store.answer_pending_ask(ask_id, verdict)
    except Exception as exc:  # noqa: BLE001 - 写裁定失败要如实说
        logger.warning("[privacy] 记录裁定失败：%s", exc)
        return "刚才没记下来，稍后再跟我说一次吧。"
    if ask is None:
        return "现在没有等你确认的事。"
    last_ask.pop(person_id, None)
    if verdict == "allow":
        return "好，记住了：这件事可以在别人面前提。"
    return "好，记住了：这件事我不会跟别人提。"


async def _apply_answer(runtime, event, ask_id: int, verdict: str, person_id: str, last_ask: dict) -> None:
    try:
        ask = runtime.store.answer_pending_ask(ask_id, verdict)
    except Exception as exc:  # noqa: BLE001 - 写裁定失败要说一声，不能假装成功
        logger.warning("[privacy] 记录裁定失败：%s", exc)
        await _send_safe(runtime, event, "刚才没记下来，稍后再跟我说一次吧。")
        return
    if ask is None:
        last_ask.pop(person_id, None)
        return
    last_ask.pop(person_id, None)
    if verdict == "allow":
        await _send_safe(runtime, event, "好，记住了：这件事可以在别人面前提。")
    else:
        await _send_safe(runtime, event, "好，记住了：这件事我不会跟别人提。")


async def _send_safe(runtime, event, text: str) -> None:
    adapter = runtime.adapters.get(event.platform)
    if adapter is None:
        return
    try:
        await adapter.send(event.chat_id, [Segment.text(text)], chat_type=event.chat_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[privacy] 发送失败：%s", exc)
