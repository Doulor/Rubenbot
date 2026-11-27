"""回复分段：换行优先，长解释只在完整句界拆开，不在半句话里随机断开；两个汉字之间的空格也算一个切点。"""

from __future__ import annotations

import random
import re
from collections.abc import Iterator

# 成对符号：开 -> 关（ASCII 引号开闭同型）
_BRACKET_PAIRS = {
    "（": "）",
    "(": ")",
    "「": "」",
    "『": "』",
    "【": "】",
    "《": "》",
    "〈": "〉",
    "[": "]",
    "{": "}",
    "“": "”",
    "‘": "’",
    '"': '"',
}
_URL_RE = re.compile(r"https?://[^\s<>\"'（）()【】「」『』，。！？；：、]+", re.IGNORECASE)
_SENTENCE_ENDS = frozenset("。！？!?…~～")
_CONTINUATIONS = "，,；;、"
_DROP_ONLY = frozenset("，,。；;、：:！？!?…~～.．-—()（）[]【】「」『』\"'“”‘’ \t\n\r　")
_SEPARATOR_ONLY = "，,。；;、：:.． \t\n\r　"
_SHORT_REPLY_CHARS = 35


def _is_cjk(char: str) -> bool:
    """常用汉字（CJK 统一表意文字主区）。"""
    return "\u4e00" <= char <= "\u9fff"


def _find_closing(text: str, start: int, open_char: str, close_char: str) -> int:
    """从 ``start`` 起找配对的结束符号下标；找不到返回 -1（按普通字符处理）。"""
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if open_char != close_char and char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _boundaries(text: str, *, sentences: bool) -> Iterator[int]:
    """给出可切位置（右端不含），跳过 URL、代码、成对括号和引号。"""
    index = 0
    while index < len(text):
        char = text[index]
        if char in "hH" and (match := _URL_RE.match(text, index)):
            index = match.end()
            continue
        if char == "`" or char in _BRACKET_PAIRS:
            end = _find_closing(text, index + 1, char, _BRACKET_PAIRS.get(char, char))
            if end != -1:
                index = end + 1
                continue
        index += 1
        if char == "\n":
            yield index
        elif sentences and char in _SENTENCE_ENDS:
            while index < len(text) and text[index] in _SENTENCE_ENDS:
                index += 1
            yield index
        elif char in " 　":
            # 只有夹在两个汉字之间的空格才切：英文、数字、标点旁边的空格不动。
            prev = text[index - 2] if index >= 2 else ""
            nxt = text[index] if index < len(text) else ""
            if prev and _is_cjk(prev) and nxt and _is_cjk(nxt):
                yield index


def _cut(text: str, *, sentences: bool) -> Iterator[str]:
    start = 0
    for end in _boundaries(text, sentences=sentences):
        if part := text[start:end].strip():
            yield part
        start = end
    if part := text[start:].strip():
        yield part


def split_sentences(text: str) -> list[str]:
    """按未受保护的句末标点和换行切句，不做发送分组。"""
    return list(_cut(text, sentences=True))


def _finish(text: str) -> str:
    """没有后文可接时才去掉悬空标点；URL 尾部的 ASCII 标点可能属于地址。"""
    if not text or text[-1] not in _CONTINUATIONS:
        return text
    end = len(text)
    while end and (text[end - 1] in _CONTINUATIONS or text[end - 1].isspace()):
        end -= 1
    for match in _URL_RE.finditer(text):
        if match.end() > end:
            return text[: match.end()] + "。"
    return text[:end]


def split_segments(
    text: str,
    *,
    max_segments: int = 3,
    rng: random.Random | None = None,
) -> list[str]:
    """优先按自然段发送；单段长解释把首句与后面的事实分开发。rng 仅保留接口兼容。"""
    body = (text or "").strip()
    if not body:
        return []
    limit = max(1, int(max_segments))
    if all(ch in _DROP_ONLY for ch in body):
        return [_finish(body)] if body.strip(_SEPARATOR_ONLY) else []
    if limit == 1:
        return [_finish(body)]

    parts: list[str] = []
    for line in _cut(body, sentences=False):
        if all(ch in _DROP_ONLY for ch in line):
            continue
        if parts and parts[-1][-1] in _CONTINUATIONS:
            # URL 可以合法地以 ASCII 逗号/分号结尾，不把下一行粘进地址。
            joiner = (
                " "
                if parts[-1][-1].isascii()
                and (
                    line[0].isascii()
                    or any(m.end() == len(parts[-1]) for m in _URL_RE.finditer(parts[-1]))
                )
                else ""
            )
            parts[-1] += joiner + line
        else:
            parts.append(line)
    if not parts:
        return []
    parts[-1] = _finish(parts[-1])
    if len(parts) == 1 and len(parts[0]) >= _SHORT_REPLY_CHARS:
        # 只分离开头的完整想法，后续事实留在一起；切片保留正文里的排版空格。
        paragraph = parts[0]
        end = next(_boundaries(paragraph, sentences=True), len(paragraph))
        first, rest = paragraph[:end], paragraph[end:].strip()
        if any(ch not in _DROP_ONLY for ch in first) and any(ch not in _DROP_ONLY for ch in rest):
            return [first, rest]
    if len(parts) > limit:
        parts[limit - 1 :] = ["".join(parts[limit - 1 :])]
    return parts
