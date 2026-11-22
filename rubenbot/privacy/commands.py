"""核心隐私命令与灰区询问；数据删除不依赖隐私总开关。"""

from __future__ import annotations

import logging
import time

from ..adapters.base import Segment

logger = logging.getLogger(__name__)

MODE_NAMES = {"normal": "正常", "private": "隐私", "norecord": "不记"}
MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}

# 同一条询问的投递冷却：隔这么久还没回答才允许再问一次
ASK_COOLDOWN_SECONDS = 12 * 3600
# 投递出去后，只有这段时间内的回复会被当作「对询问的回答」
ASK_ANSWER_WINDOW_SECONDS = 24 * 3600
# 短于这个长度的回复才当作「对询问的回答」；更长的按正常聊天处理
ANSWER_MAX_CHARS = 12

# 同意必须是完整短答复；不能从否定、疑问或附条件的句子里截取「可以」。
_ALLOW_EXACT = {"可以", "行", "好", "好的", "好啊", "好吧", "没问题", "说吧", "能说", "当然", "可以啊", "可以吧",
                "可以的", "可以说", "那可以吧", "ok", "okay", "yes"}
_DENY_EXACT = {"不行", "不可以", "不要", "别说", "不能说", "不能", "不好", "不用", "别", "no", "nope"}
_DENY_HINTS = ("不行", "不可以", "不要", "别说", "不能", "不用", "不许", "不说")


def parse_answer(text: str) -> str | None:
    """把本人对询问的回复解析成 allow / deny；认不出返回 None。"""
    value = (text or "").strip().rstrip("。.!！~～,，").strip().lower()
    if any(mark in value for mark in ("?", "？", "吗", "么")):
        return None
    if value in _DENY_EXACT or (len(value) <= 6 and any(hint in value for hint in _DENY_HINTS)):
        return "deny"
    return "allow" if value in _ALLOW_EXACT else None


def is_command(text: str, prefix: str = "/") -> bool:
    return bool(prefix) and (text or "").strip().startswith(prefix)


class PrivacyCommands:
    """每个 bot 独享待询问投递状态，不依赖插件加载或钩子注册。"""

    def __init__(self):
        self.last_ask: dict[str, tuple[int, float]] = {}
        self.last_delivered: dict[str, float] = {}

    def purge_if_requested(self, runtime, event) -> str | None:
        """本人删除权不依赖是否允许互动；由调用方决定要不要发回执。"""
        prefix = runtime.config.plugins.command_prefix
        text = (event.text or "").strip()
        if not is_command(text, prefix):
            return None
        parts = text[len(prefix):].split()
        if parts != ["清空我的数据"] and not (
            runtime.config.privacy.enabled and len(parts) >= 2
            and parts[0] == "隐私" and parts[1] in ("清空", "删除", "clear")
        ):
            return None
        from ..memory.store import make_person_id

        return self._purge(runtime, make_person_id(event.platform, event.sender_id))

    async def handle(self, runtime, event) -> bool:
        """命中命令或待确认短答复时返回 True，普通消息留给聊天管线。"""
        prefix = runtime.config.plugins.command_prefix
        text = (event.text or "").strip()
        from ..memory.store import make_person_id

        person_id = make_person_id(event.platform, event.sender_id)
        if is_command(text, prefix):
            parts = text[len(prefix):].split()
            reply = self.purge_if_requested(runtime, event)
            if reply is None:
                if not (parts and parts[0] == "隐私" and runtime.config.privacy.enabled):
                    return False
                reply = self._command(runtime, event, person_id, parts[1:])
            await _send_safe(runtime, event, reply)
            return True
        if not runtime.config.privacy.enabled or not event.is_private:
            return False
        ask_id = self._delivered_ask(person_id)
        if ask_id is None:
            return False
        verdict = parse_answer(text)
        if verdict:
            await _send_safe(runtime, event, self._answer(runtime.store, person_id, ask_id, verdict))
            return True
        if len(text) <= ANSWER_MAX_CHARS:
            await _send_safe(runtime, event, "我没太看明白——可以直接回我「可以」或「不行」。")
            return True
        return False

    def _purge(self, runtime, person_id: str) -> str:
        try:
            counts = runtime.store.purge_person(person_id, reason="本人要求清空数据")
        except Exception as exc:  # noqa: BLE001 - 删除失败要如实告诉本人
            logger.warning("[privacy] 清空失败：%s", exc)
            return "清空失败了，稍后再试一次吧。"
        # 索引有缓存，必须同时撤下含此人身份、钩子的旧行。
        if runtime.memory is not None:
            runtime.memory.index.invalidate()
        self.last_ask.pop(person_id, None)
        self.last_delivered.pop(person_id, None)
        return (
            "已经清空：删掉了关于你的画像、"
            f"{counts.get('events', 0)} 条事件、{counts.get('memories', 0)} 条记忆，"
            "以及相关的裁定和待确认事项。"
        )

    def _command(self, runtime, event, person_id, parts):
        store = runtime.store
        if not parts or parts == ["查看"]:
            return _report(store, person_id, private=event.is_private)
        head = parts[0]
        if head in ("模式", "mode"):
            return _set_mode(store, person_id, parts[1] if len(parts) > 1 else "")
        if parts in (["可以"], ["不行"]):
            if not event.is_private:
                return "请在收到询问的私聊里答复。"
            ask_id = self._delivered_ask(person_id)
            if ask_id is None:
                return "现在没有等你确认的事。"
            return self._answer(store, person_id, ask_id, "allow" if head == "可以" else "deny")
        return (
            "用法：/隐私 查看｜/隐私 模式 正常|隐私|不记｜/隐私 清空\n"
            "（有想问你的待确认事项时，也可以直接回 /隐私 可以 或 /隐私 不行）"
        )

    def _delivered_ask(self, person_id: str) -> int | None:
        pending = self.last_ask.get(person_id)
        if pending is None:
            return None
        ask_id, delivered_at = pending
        if time.time() - delivered_at > ASK_ANSWER_WINDOW_SECONDS:
            self.last_ask.pop(person_id, None)
            return None
        return ask_id

    def _answer(self, store, person_id: str, ask_id: int, verdict: str) -> str:
        try:
            ask = store.answer_pending_ask(ask_id, verdict)
        except Exception as exc:
            logger.warning("[privacy] 记录裁定失败：%s", exc)
            return "刚才没记下来，稍后再跟我说一次吧。"
        self.last_ask.pop(person_id, None)
        if ask is None or ask.status != "answered" or ask.verdict != verdict:
            return "现在没有等你确认的事。"
        if verdict == "allow":
            return "好，记住了：这件事可以在别人面前提。"
        return "好，记住了：这件事我不会跟别人提。"

    async def after_reply(self, runtime, event) -> None:
        """仅模块开启且在本人的私聊里，才投递一条待确认。"""
        if not runtime.config.privacy.enabled or not event.is_private:
            return
        from ..memory.store import make_person_id

        person_id = make_person_id(event.platform, event.sender_id)
        now = time.time()
        if now - self.last_delivered.get(person_id, 0.0) < ASK_COOLDOWN_SECONDS:
            return
        try:
            if runtime.store.is_norecord(person_id):
                return
            asks = runtime.store.list_pending_asks(person_id, status="pending", limit=1)
        except Exception as exc:
            logger.warning("读取待询问失败：%s", exc)
            return
        if asks:
            ask = asks[0]
            if await _send_safe(runtime, event, ask.question):
                self.last_delivered[person_id] = now
                self.last_ask[person_id] = (ask.id, now)


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
    if key in MODE_NAMES:
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


async def _send_safe(runtime, event, text: str) -> bool:
    adapter = runtime.adapters.get(event.platform)
    if adapter is None:
        return False
    try:
        await adapter.send(event.chat_id, [Segment.text(text)], chat_type=event.chat_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[privacy] 发送失败：%s", exc)
        return False
    return True
