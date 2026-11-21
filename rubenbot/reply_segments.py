"""回复多段化：规则断句 + 按长度概率合并。

思路借鉴 MaiBot 的「断句 → 概率合并」：先把回复切成以标点/空白为界的
短段，再按总长度决定合并概率，让发出去的条数自然浮动。断句完全用规则
实现，不额外调用模型，也不引入串行延迟。

保护规则：URL、行内代码、成对括号/引号（颜文字通常在括号里）、英文/数字
词内部都不作为切点。换行是硬边界：生成端用换行表达「另发一条」，这里
一定拆开，不受合并概率影响。

代码为原创实现，仅参考设计思路（不复制 GPL 源码）。
"""

from __future__ import annotations

import random
import re

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
_URL_RE = re.compile(
    r"https?://[^\s<>\"'（）()【】「」『』，。！？；：、]+",
    re.IGNORECASE,
)

# 句末标点：切完作为上一段的结尾保留
_SENTENCE_ENDS = frozenset("。！？!?…~～")
# 段内可切点（半句边界）；切在标点之后
_SOFT_CUTS = frozenset("，,；;、")
# 空白切点：中文里常用来分隔两个短句；英文词之间不切
_SPACE_CUTS = frozenset(" \t　")
# 纯标点/空白段直接丢弃（连续标点不单独成条）
_DROP_ONLY = frozenset("，,。；;、：:！？!?…~～.．-—()（）[]【】「」『』\"'“”‘’ \t\n\r　")
# 只有逗号/句号这类连接性标点时整条丢弃；问号、省略号等语气标点保留
_SEPARATOR_ONLY = frozenset("，,。；;、：:.． \t\n\r　")

# 总长度 → 软切点保留概率（即「不合并」强度）。越短越倾向并回一条。
_SHORT_SEGMENT_CHARS = 20
_LONG_SEGMENT_CHARS = 35


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


def _is_cjk(char: str) -> bool:
    """是否中文汉字（标点、英文、数字都不算）。"""
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def _split_strength(length: int) -> float:
    """软切点的保留强度：短回复几乎总是并回一条，长文更倾向多条。"""
    if length < _SHORT_SEGMENT_CHARS:
        return 0.0
    if length < _LONG_SEGMENT_CHARS:
        return 0.35
    return 0.7


def _pieces(text: str) -> list[tuple[str, str]]:
    """切成 ``(内容, 尾随分隔符)``；换行分隔符用于强制分段。

    分隔符只记录空格/换行（标点本身留在内容里），供合并阶段恢复停顿。
    """
    text = re.sub(r"\n[ \t　]*\n+", "\n", text)
    pieces: list[tuple[str, str]] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    def flush(separator: str = "") -> None:
        piece = "".join(buffer).strip()
        buffer.clear()
        if piece:
            pieces.append((piece, separator))
        elif separator == "\n" and pieces:
            # 换行前刚在标点处切过：把换行记到前一段上，保证它是硬边界
            content, last_separator = pieces[-1]
            pieces[-1] = (content, "\n")

    while index < length:
        char = text[index]
        # URL：整段跳过，避免在 ? ! 等处切断
        if char in "hH":
            match = _URL_RE.match(text, index)
            if match:
                buffer.append(match.group(0))
                index = match.end()
                continue
        # 行内代码：`...`
        if char == "`":
            end = text.find("`", index + 1)
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        # 括号/引号：整段保留到配对的结束符号（颜文字、引用不拆开）
        if char in _BRACKET_PAIRS:
            end = _find_closing(text, index + 1, char, _BRACKET_PAIRS[char])
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        if char == "\n":
            # 硬边界：生成端写换行就是要另发一条
            flush("\n")
            index += 1
            while index < length and text[index] in _SPACE_CUTS:
                index += 1
            continue
        if char in _SPACE_CUTS:
            # 空白只在两个中文字之间当半句边界：「我卡得 PPT 一样」、
            # 「试一下 `code` 这句」这类中英混排的空格是排版，不切。
            prev_char = text[index - 1] if index > 0 else ""
            next_char = text[index + 1] if index + 1 < length else ""
            if not (_is_cjk(prev_char) and _is_cjk(next_char)):
                buffer.append(char)
                index += 1
                continue
            flush(" ")
            index += 1
            continue
        buffer.append(char)
        index += 1
        if char in _SENTENCE_ENDS or char in _SOFT_CUTS:
            # 标点留在上一段；后面紧跟的空白一并吃掉
            while index < length and text[index] in _SPACE_CUTS:
                index += 1
            flush("")
    flush()
    return pieces


def _merge(pieces: list[tuple[str, str]], strength: float, rng: random.Random) -> list[str]:
    """按强度合并相邻段；换行边界永不合并。"""
    merged: list[str] = []
    index = 0
    while index < len(pieces):
        content, separator = pieces[index]
        # 只要下一段是换行分隔的，就不合并
        if index + 1 < len(pieces) and separator != "\n":
            next_content, _ = pieces[index + 1]
            if rng.random() >= strength:
                merged.append(content + separator + next_content)
                index += 2
                continue
        merged.append(content)
        index += 1
    return merged


def split_segments(
    text: str,
    *,
    max_segments: int = 3,
    rng: random.Random | None = None,
) -> list[str]:
    """把一条回复拆成若干条短消息。

    - 换行处一定拆（生成契约用换行表示「另发一条」）；
    - 逗号/顿号/分号/空格处按总长度概率合并，短回复通常保持一条；
    - 总条数不超过 ``max_segments``，多出来的并入最后一条；
    - 纯标点/空白段丢弃。
    """
    body = (text or "").strip()
    if not body:
        return []
    limit = max(1, int(max_segments))
    rng = rng or random.Random()
    pieces = _pieces(body)
    if not pieces:
        return []
    strength = _split_strength(len(body))
    if limit == 1:
        parts = [body]
    else:
        parts = _merge(pieces, strength, rng)
    # 丢弃纯标点段，并去掉段首尾空白
    cleaned = [part.strip() for part in parts if part.strip() and any(ch not in _DROP_ONLY for ch in part)]
    if not cleaned:
        # 整条只有标点：分隔符（「，。」）没有内容可丢；「？」「……」这类
        # 是刻意的语气，保留成单独一条。
        return [body] if body.strip("".join(_SEPARATOR_ONLY)) else []
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + ["".join(cleaned[limit - 1 :])]
    return cleaned


def split_sentences(text: str) -> list[str]:
    """按句末标点/换行切句，但不在括号、引号、URL、行内代码中间断开。

    保留给需要「纯断句、不合并」的调用方与测试；发送链用的是
    :func:`split_segments`（带概率合并）。
    """
    parts: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    def flush() -> None:
        piece = "".join(buffer).strip()
        if piece:
            parts.append(piece)
        buffer.clear()

    while index < length:
        char = text[index]
        if char in "hH":
            match = _URL_RE.match(text, index)
            if match:
                buffer.append(match.group(0))
                index = match.end()
                continue
        if char == "`":
            end = text.find("`", index + 1)
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        if char in _BRACKET_PAIRS:
            end = _find_closing(text, index + 1, char, _BRACKET_PAIRS[char])
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        buffer.append(char)
        index += 1
        if char in _SENTENCE_ENDS or char == "\n":
            while index < length and text[index].isspace():
                index += 1
            flush()
    flush()
    return parts
