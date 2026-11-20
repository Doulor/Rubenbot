"""生成后的附和闸门：只拦高重合、没有新信息的长复述，不猜同义句。"""

import re
import unicodedata

from .memory.retrieve import tokenize


# 单字「对」只当独立分句来剥，不能删掉「对方」「对比」的字头。
_FILLER = r"(?:确实如此|确实是这样|确实|的确|没错|你说得对|说得也是|我也这么觉得|我也觉得|我同意|对对对|对啊|对呀|是啊|是呀|是的|那就是|可不是嘛)"
_PREFIX = re.compile(rf"^(?:{_FILLER})+")
_PARTICLES = re.compile(r"[啊呀]+$")
_SEPARATORS = re.compile(r"[,。!、;\n]+")
_QUOTES = str.maketrans("", "", '\"“”「」『』')
_MIN_OVERLAP = 0.80


def _clauses(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().translate(_QUOTES)
    clauses = []
    for part in _SEPARATORS.split(text.rstrip(".")):
        part = part.strip()
        if part in {"对", "嗯", "嗯嗯"}:
            continue
        body = _PARTICLES.sub("", _PREFIX.sub("", part)).strip()
        if body:
            clauses.append(body)
    return clauses


def is_repetitive_agreement(source: str, reply: str) -> bool:
    """短呼应无条件放行；长句需无新关键词且 Jaccard 达到 0.8 才可能被拦。"""
    if len(reply.strip()) <= 10:
        return False
    original, draft = _clauses(source), _clauses(reply)
    if not original or not draft:
        return False

    # 只改断句时以原文的分句切词，避免凭空多出一个跨标点的 bigram。
    if "".join(draft) == "".join(original):
        draft = original
    source_words = set(tokenize("，".join(original)))
    reply_words = set(tokenize("，".join(draft)))
    if not reply_words or not reply_words <= source_words:
        return False
    # 已知回复是子集，此时交集就是回复词集、并集就是来源词集。
    if len(reply_words) / len(source_words) < _MIN_OVERLAP:
        return False

    # 词袋看不出调换主客体、删「不」、新加问号等变化。允许跳过整句，但保留原句顺序与含义。
    remaining = iter(original)
    return all(part in remaining for part in draft)
