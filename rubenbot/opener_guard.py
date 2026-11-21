"""开头反重复：跟踪 bot 最近几条回复的开头模式，必要时给一句临时纠偏。

只做程序侧治理，不往人设文件里写禁令：

- 按会话保留 bot 最近 ``window`` 条回复的开头模式（首个语气词，或开头 2 字）；
- 同一模式在窗口里出现 ``threshold`` 次及以上时，下一轮生成提示尾部临时
  加一句纠偏（例如「这轮别用『草』开头，换个说法」）；
- 纠偏只注入一轮：发过之后要等新回复进来、且模式仍然超标才会再次触发。

状态全在内存里，进程重启后从零开始；表情条等非文字回复不参与统计。
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

# 首个语气词/口头禅：命中就以它作为开头模式（长词优先匹配）
TONE_OPENINGS: tuple[str, ...] = (
    "离谱", "就这", "不是吧", "啊这", "笑死", "哈哈哈", "草", "操", "靠",
    "？", "?", "6", "哈", "唉", "哎",
)
_TONE_SORTED = tuple(sorted(TONE_OPENINGS, key=len, reverse=True))


def opening_pattern(text: str) -> str:
    """取一条回复的开头模式；空回复、纯表情条返回空串（不参与统计）。"""
    body = (text or "").strip()
    if not body:
        return ""
    if body.startswith("[") and body.endswith("]") and len(body) <= 8:
        # 「[表情]」「[图片]」这类占位不是自然的开头
        return ""
    for word in _TONE_SORTED:
        if body.startswith(word):
            return word
    # 没有明显语气词时用开头 2 个字；只剩一个字就用这一个字
    return body[:2]


@dataclass
class _State:
    patterns: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    notes: int = 0
    # 上一次纠偏时所记录的回复条数（-1 表示从没纠偏过）
    corrected_at: int = -1
    # 已经加进提示、但对应回复还没发出来的纠偏行（避免同一轮重复注入）
    pending_note: str = ""


class OpenerGuard:
    """按会话统计开场模式，返回可注入的一行纠偏。"""

    def __init__(self, *, window: int = 10, threshold: int = 3):
        self.window = max(1, int(window))
        self.threshold = max(2, int(threshold))
        self._states: dict[str, _State] = {}

    def _state(self, key: str) -> _State:
        state = self._states.get(key)
        if state is None:
            state = _State(patterns=deque(maxlen=self.window))
            self._states[key] = state
        return state

    def note_reply(self, key: str, text: str) -> None:
        """记录 bot 的一条真实回复（按会话）。"""
        pattern = opening_pattern(text)
        if not pattern:
            return
        state = self._state(key)
        state.patterns.append(pattern)
        state.notes += 1
        # 纠偏对应的那条回复已经出来了，这一轮的「一次性」额度用掉
        state.pending_note = ""

    def pending_correction(self, key: str) -> str:
        """本轮已加入提示的纠偏行（没有就是空串）；供日志/审计。"""
        state = self._states.get(key)
        return state.pending_note if state is not None else ""

    def dominant(self, key: str) -> tuple[str, int] | None:
        """窗口里出现次数达标的开头模式；没有返回 None。"""
        state = self._states.get(key)
        if state is None or len(state.patterns) < self.threshold:
            return None
        pattern, count = Counter(state.patterns).most_common(1)[0]
        if count >= self.threshold:
            return pattern, count
        return None

    def correction(self, key: str) -> str | None:
        """本轮要不要加纠偏；返回提示行。

        同一轮（还没等到新回复）只给一次，避免多段/重试时反复叠加；
        发出去的真实回复由 :meth:`note_reply` 记账，本轮没发出来则
        :meth:`clear_pending` 清掉，下轮还能再提醒。
        """
        state = self._states.get(key)
        if state is None:
            return None
        if state.pending_note:
            return None
        hit = self.dominant(key)
        if hit is None:
            return None
        pattern, _count = hit
        state.corrected_at = state.notes
        state.pending_note = f"这轮别用「{pattern}」开头，换个说法。"
        return state.pending_note

    def clear_pending(self, key: str) -> None:
        """本轮没生成出来（沉默/否决）时清掉待用纠偏，下轮还能再提醒。"""
        state = self._states.get(key)
        if state is not None:
            state.pending_note = ""

    def reset(self, key: str | None = None) -> None:
        """清空某个会话（或全部）的统计；配置热更新/测试用。"""
        if key is None:
            self._states.clear()
        else:
            self._states.pop(key, None)
