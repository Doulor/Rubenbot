"""按会话观察 bot 最近发出的文字；给生成端频次事实，不拦截或改写回复。"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Hashable, Iterator
from dataclasses import dataclass

# 首个语气词/口头禅：命中就以它作为开头模式（长词优先匹配）
TONE_OPENINGS: tuple[str, ...] = (
    "离谱", "就这", "不是吧", "啊这", "笑死", "哈哈哈", "草", "操", "靠",
    "？", "?", "6", "哈", "唉", "哎",
)
_TONE_SORTED = tuple(sorted(TONE_OPENINGS, key=len, reverse=True))
_WORD = re.compile(r"[㐀-鿿]+|[A-Za-z]+")
_ENDING = re.compile(r"([㐀-鿿])[\s。，、；！？…!?.~～]*\Z")


def opening_pattern(text: str) -> str:
    """取一条回复的开头模式；空回复、纯表情条返回空串（不参与统计）。"""
    body = (text or "").strip()
    if not body:
        return ""
    if body.startswith("[") and body.endswith("]") and len(body) <= 8:
        return ""
    for word in _TONE_SORTED:
        if body.startswith(word):
            return word
    return body[:2]


def _fragments(text: str) -> Iterator[str]:
    """不预设口头禅词库。中文找重复的连续短片段，其余按完整词计。"""
    for word in _WORD.findall(text):
        if "㐀" <= word[0] <= "鿿":
            for length in range(2, min(6, len(word)) + 1):
                for start in range(len(word) - length + 1):
                    yield word[start:start + length]
        elif 2 <= len(word) <= 16:
            yield word


@dataclass
class _State:
    replies: deque[str]
    pending_note: str = ""


class OpenerGuard:
    """在短滚动窗口内统计开头和不限位置的短语，只向下一轮生成报告。"""

    def __init__(self, *, window: int = 10, threshold: int = 3):
        self.window = max(1, int(window))
        self.threshold = max(2, int(threshold))
        self._states: dict[Hashable, _State] = {}

    def note_reply(self, key: Hashable, text: str) -> None:
        """只记录 bot 实际发出的自然文字。"""
        if not opening_pattern(text):
            return
        if key not in self._states:
            self._states[key] = _State(deque(maxlen=self.window))
        state = self._states[key]
        state.replies.append(text)
        state.pending_note = ""

    def pending_correction(self, key: Hashable) -> str:
        state = self._states.get(key)
        return state.pending_note if state is not None else ""

    def correction(self, key: Hashable) -> str | None:
        """只报告跨多条消息的重复；单条里重叠的字串不冒充多次口头禅。"""
        state = self._states.get(key)
        if state is None or state.pending_note or len(state.replies) < self.threshold:
            return None
        lines = []
        (opening, count), = Counter(map(opening_pattern, state.replies)).most_common(1)
        if count >= self.threshold:
            lines.append(f"最近有 {count} 条以「{opening}」开头。这轮换个说法，从想说的内容起头。")

        counts: Counter[str] = Counter()
        endings: Counter[str] = Counter()
        for text in state.replies:
            counts.update(set(_fragments(text)))
            # 单字语尾也可能反复出现；只统计句末，不把正文里的普通单字算进口头禅。
            if match := _ENDING.search(text):
                endings[match[1]] += 1
        # 同频时优先保留完整短语，不同时报「笑死我了」「笑死我」「笑死」。
        repeated = sorted((p for p, n in counts.items() if n >= self.threshold),
                          key=lambda p: (-counts[p], -len(p), p))
        selected: list[str] = []
        for phrase in repeated:
            if (phrase == opening and count == counts[phrase]) or any(phrase in prior for prior in selected):
                continue
            selected.append(phrase)
            if len(selected) == 3:
                break
        facts = [f"{counts[p]} 条用过「{p}」" for p in selected]
        for ending, n in endings.most_common(1):
            if n >= self.threshold and not any(p.endswith(ending) and counts[p] == n for p in selected):
                facts.append(f"{n} 条以「{ending}」收尾")
        if facts:
            lines.append(f"本会话你最近 {len(state.replies)} 条发言里，{'，'.join(facts)}。留意口头禅和收尾，按这次想说的内容自然措辞。")
        state.pending_note = "\n".join(lines)
        return state.pending_note or None

    def clear_pending(self, key: Hashable) -> None:
        """本轮没生成出来（沉默/否决）时清掉待用纠偏，下轮还能再提醒。"""
        state = self._states.get(key)
        if state is not None:
            state.pending_note = ""

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._states.clear()
        else:
            self._states.pop(key, None)
