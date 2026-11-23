"""指称完整性：评价型断言必须带可识别的实体锚点。

病灶（机主 2026-11-23 点名）：蒸馏产物里出现过「TA 做过一个『随手做的』东西，
硬性限制比较大，认为它用来推单向消息不错」——评价留下了，所指的实体被丢掉，
整条记忆不可检索，事后只能靠猜。

校验口径（与抽取提示词一致）：

- **需要锚点**：句子里用「它」「这个东西」「这个平台」「一个『……』的东西」这类
  指称指向句外对象，或写成「觉得不错 / 认为挺好用」把评价对象也省掉了。这时句子的
  主体不在正文里，单看这一条看不出在说谁/什么。「觉得压力很大」自带对象，不算缺主语。
- **什么算锚点**：同一句正文或出处原话里出现具体名字（引号里的专名、拉丁字母名、
  实体账里登记过的名字），或模型给出 ``entity`` 且该名字在出处原话里逐字出现。
  指向当前会话本身的「这个群 / 本会话」不算缺失主语，由 scope 承担锚点；
  「工具 / 内部系统 / 那个平台」这类泛称、「它插件」这类指代碎片不算实体。
- **不允许事后猜**：正文自带的多个名字、或出处原话里能对上多个登记实体时，
  指称不明，一律算「无锚点」；宁可丢弃留痕，也不把猜测写进记忆。

这里只做判定，不请求模型；抽取、画像提炼、归纳与存量扫描工具共用同一套判定。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

# 指向当前会话/群本身的指称：会话由 scope_type/scope_id 锚定，不需要专名。
_SELF_PLACE_RE = re.compile(r"(?:这个|那个|该|此|本)(?:群|会话|频道|房间)|(?:群里|本群)")

# 句外指称：代词「它」、泛指指示词，以及「一个『随手做的』东西」这种名词占位。
_DEICTIC_PATTERNS = (
    re.compile(r"(?<!其)它"),
    # 「这个东西 / 那个平台 / 这个项目」：泛指的头部本身就说明主体没写出来。
    re.compile(
        r"(?:这|那)(?:个|款|种|样|些)?(?:东西|玩意|玩意儿|项目|平台|工具|产品|功能|应用|服务|系统|软件|机器人|bot)",
        re.IGNORECASE,
    ),
    # 「这个不行」「那个不错」：指示词直接修饰评价形容词时，被评价对象没写出来。
    re.compile(
        r"(?:这|那)(?:个|款|种|样)(?=不错|好用|难用|好使|不好|不行|可以|挺|很|太|真|还|也|就|更|最|有点|不太|不行|一般|垃圾|鸡肋|靠谱)"
    ),
    re.compile(r"(?:该|此)(?:款|项目|平台|工具|产品|功能|应用|服务|系统|软件|站)"),
    re.compile(r"一个?[「『“\"][^」』”\"]{1,30}[」』”\"]\s*的?\s*(?:东西|玩意|玩意儿|事)"),
)

# 省略宾语的态度句：「TA 觉得不错 / 认为挺好用」——被评价的对象没写出来。
# 只认态度词后面直接跟评价、再没有名词的情况；「，觉得压力很大」这种
# 承接主语的正常写法不在此列（它本来就有对象「压力」）。
_SUBJECTLESS_EVAL_RE = re.compile(
    r"(?:^|[，,；;])\s*(?:TA|ta|他|她|我|你)?\s*(?:认为|觉得|感觉|评价|声称|表示)\s*"
    r"(?:还|也|就|都|真|很|挺|太|超|有点|有些|不太|不)?\s*并?\s*"
    r"(?:不错|好用|好使|难用|不好|不行|可以|一般|靠谱|不靠谱|厉害|划算|值得|"
    r"垃圾|鸡肋|坑|差|强|弱|棒|赞|好|香|爽|行)(?:的)?\s*[了吧啊呀呢哈]?\s*$"
)

_QUOTED_RE = re.compile(r"[「『“\"]([^」』”\"]{1,40})[」』”\"]")
# 引号内容紧跟「东西/玩意/事」时是修饰语（『随手做的』东西），不是实体名。
_VAGUE_HEAD_RE = re.compile(r"\s*的?\s*(?:东西|玩意|玩意儿|事)")
# 拉丁名：允许「Claude Code」这种空格分隔的专名，但不把整句英文当名字。
_LATIN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.+-]{1,39}(?:\s+[A-Z][A-Za-z0-9_.+-]{0,39})*(?![A-Za-z0-9_])"
)
_LATIN_STOP = frozenset(
    {"ta", "bot", "ok", "api", "url", "id", "http", "https", "www", "app", "ai", "and", "the", "not"}
)
# 中文专名：可带拉丁前缀，以产品/项目/系统等通名结尾的连续汉字（如「QQ 官方机器人桥」）。
_CJK_NAME_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_.+-]*\s+)?[一-鿿]{2,20}"
    r"(?:桥|框架|系统|平台|插件|机器人|通道|应用|软件|服务|模型|协议|工具|接口|网站|项目|引擎|脚本|程序)"
)
# 「飞书这个平台」里的「这个平台」是指示后缀，不是名字的一部分。
_CJK_HEAD_RE = re.compile(r"(?:这个|那个|该|此|这款|那款|这种|那种)[一-鿿]*$")
# 匹配从这些词开头时，前半截是主语/谓语或量词，不是名字本身。
# 例如「我做了个工具包」里的「做了个」、「TA 用飞书传文档」里的「用」。
_CJK_LEAD_RE = re.compile(
    r"^(?:TA|ta|他|她|我|你|本人|对方)?\s*"
    r"(?:(?:最近|正在|现在|之前|前阵子|一直|已经|刚|才|也|还|在)\s*)*"
    r"(?:(?:认为|觉得|感觉|评价|声称|表示|说|讲|提到|推荐|用过|做过|用|拿|玩|搞|做|写|弄|"
    r"开发|搭建|搭|建|部署|跑|装|买|学|维护|开源)"
    r"(?:了|过|着)?\s*(?:一个|一款|一套|一种|个|款|套|种|些|只|台|部|本)?\s*)*"
)
# 只由泛称组成的「名字」（工具 / 内部系统 / 那款平台）不是实体，不能当锚点。
_GENERIC_NAME_RE = re.compile(
    r"^(?:内部|外部|小型|大型|新|旧|这个|那个|一个|自家|自己|我的|他的|她的|日常|私人|某|"
    r"一些|好多|几个|啥)?"
    r"(?:工具|系统|平台|项目|网站|应用|软件|服务|功能|产品|插件|机器人|框架|接口|引擎|模型|"
    r"协议|通道|桥|程序|脚本|代码|东西|玩意)$"
)
_DEICTIC_WORDS = frozenset(
    {"它", "这个东西", "那个东西", "这东西", "那东西", "这玩意", "那玩意", "这个", "那个", "这款", "那款", "该款"}
)


@dataclass(frozen=True)
class Anchor:
    """解析出的实体锚点。"""

    name: str  # 落库的实体规范名
    surface: str  # 出处原话里的写法（正文标注用）
    reason: str  # model / known / quote / latin


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def find_deictic(text: str) -> list[str]:
    """返回句子里指向句外的指称；空列表表示正文自带主语。"""
    value = _SELF_PLACE_RE.sub("", _normalize(text))
    hits: list[str] = []
    for pattern in _DEICTIC_PATTERNS:
        for match in pattern.finditer(value):
            token = match.group(0)
            if token not in hits:
                hits.append(token)
    # 只有「觉得 XX」把对象也省掉时才算缺主语；「觉得压力很大」的对象是压力，不拦。
    for match in _SUBJECTLESS_EVAL_RE.finditer(value):
        token = match.group(0).strip()
        if token not in hits:
            hits.append(token)
    return hits


def needs_anchor(text: str) -> bool:
    """这条断言是否必须有实体锚点（只有评价、没有主语）。"""
    return bool(find_deictic(text))


# 评价/态度词：与指称同时出现时，说明留下的是「对某物的看法」，主体必须在。
_EVALUATIVE_RE = re.compile(
    r"认为|觉得|感觉|评价|看好|看衰|推荐|不推荐|满意|失望|喜欢|讨厌|嫌弃|"
    r"不错|好用|难用|好用|鸡肋|垃圾|靠谱|不靠谱|厉害|牛|坑|神器|废物|"
    r"限制|优点|缺点|好处|坏处|适合|不适合|值得|不值得|比.{0,6}好|比.{0,6}差",
    re.IGNORECASE,
)


def is_evaluation(text: str) -> bool:
    """是否带对某对象的评价/态度（事件层只拦评价型，事实层一律要锚点）。"""
    return bool(_EVALUATIVE_RE.search(_normalize(text)))


def needs_anchor_for_event(text: str) -> bool:
    """事件接近原文，只有「评价 + 指代」同时出现才要求锚点。"""
    return bool(find_deictic(text)) and is_evaluation(text)


def _quoted_names(text: str) -> list[str]:
    """正文里的专名引文；以「的」收尾或紧跟「东西/玩意」的是修饰语，不算名字。"""
    names: list[str] = []
    for match in _QUOTED_RE.finditer(text):
        if _VAGUE_HEAD_RE.match(text, match.end()):
            continue
        span = match.group(1).strip()
        # 「『随手做的』QQ 官方机器人桥」：引号里是定语，后面才是被评价的对象。
        if span.endswith(("的", "地", "得")):
            continue
        if len(span) >= 2 and span not in names:
            names.append(span)
    return names


def _latin_names(text: str) -> list[str]:
    return [
        word
        for word in _LATIN_RE.findall(text)
        if word.lower() not in _LATIN_STOP and word not in _DEICTIC_WORDS
    ]


def _cjk_names(text: str) -> list[str]:
    """中文专名：去掉主语/谓语前缀与「飞书这个平台」里的指示后缀，只留「飞书」。"""
    names: list[str] = []
    for match in _CJK_NAME_RE.finditer(text):
        name = _CJK_LEAD_RE.sub("", match.group(0))
        head = _CJK_HEAD_RE.search(name)
        if head is not None and head.start() > 0:
            name = name[: head.start()]
        # 「一个」开头的仍旧是泛指，不当作专名；只剩泛称的也不算实体。
        if _CJK_HEAD_RE.match(name) or name.startswith(("一个", "一件")):
            continue
        if _GENERIC_NAME_RE.match(name) or any(word in name for word in ("它", "这个", "那个", "这款", "那款")):
            continue
        if len(name) >= 2 and name not in names:
            names.append(name)
    return names


def _source_names(text: str) -> list[str]:
    """出处原话里可直接当锚点的名字：引号专名、中文专名、拉丁名。

    同一实体的复合写法（「QQ 官方机器人桥」）只算一个；被别的候选包含的
    拉丁名（QQ）不重复计数。引号后面紧跟另一个专名的是修饰语（『随手做的』QQ…），不算。
    """
    quoted = _quoted_names(text)
    cjk = _cjk_names(text)
    latin = _latin_names(text)
    names: list[str] = []
    for match in _QUOTED_RE.finditer(text):
        span = match.group(1).strip()
        if span not in quoted:
            continue
        tail = text[match.end() : match.end() + 8]
        if _VAGUE_HEAD_RE.match(tail) or any(
            re.match(r"\s*" + re.escape(name.split()[0]), tail) for name in cjk
        ):
            continue  # 修饰语：『随手做的』东西 / 『随手做的』QQ 官方机器人桥
        names.append(span)
    names.extend(cjk)
    names.extend(
        word for word in latin if not any(_normalize(word) in _normalize(name) for name in cjk)
    )
    return list(dict.fromkeys(names))


def _source_name_pairs(text: str) -> list[tuple[str, str]]:
    """出处原话里的 (写法, 规范名)。

    同一名字若还出现在更长候选里（引号里是简称、正文写全称），以较长的写法为准，
    避免把一个实体数成两个。
    """
    names = _source_names(text)
    merged: list[str] = []
    for name in names:
        if any(name != other and name in other for other in names):
            continue
        merged.append(name)
    return [(name, name) for name in merged]


def explicit_names(text: str) -> list[str]:
    """正文里自带的可识别名字（引号专名 + 拉丁字母名）。"""
    return list(dict.fromkeys([*_quoted_names(text), *_latin_names(text)]))


def _as_surface_map(known_names: Mapping[str, str] | Iterable[str] | None) -> dict[str, str]:
    """统一成 {出处写法: 规范名}；可迭代时规范名与写法相同。"""
    if not known_names:
        return {}
    if isinstance(known_names, Mapping):
        return {
            str(surface): str(canonical or surface)
            for surface, canonical in known_names.items()
            if str(surface).strip()
        }
    return {str(name): str(name) for name in known_names if str(name).strip()}


def _dedupe(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """规范名去重；同规范名的多个写法只留一个代表写法。"""
    result: dict[str, str] = {}
    for surface, canonical in pairs:
        result.setdefault(canonical, surface)
    return result


def resolve_anchor(
    content: str,
    source_text: str,
    *,
    known_names: Mapping[str, str] | Iterable[str] | None = None,
    model_entity: str = "",
    allow_source_default: bool = True,
) -> Anchor | None:
    """在出处原话里找这个指称指向的实体；找不到或有歧义时返回 None。

    ``model_entity`` 是模型自报的实体名，必须能在出处原话里逐字对上才算数。
    ``known_names`` 是实体账的登记名（{写法: 规范名}）；只认出现在出处原话里的。
    ``allow_source_default=False`` 时不采用「出处里只有一个名字就挂上去」的兜底，
    用于正文本来就自带主语的普通事实：只认正文里写出来的名字。
    """
    content_text = _normalize(content)
    source = _normalize(source_text)
    surfaces = _as_surface_map(known_names)

    # 1. 模型明确给出实体名：必须是出处原话里真的出现过的写法。
    candidate = _normalize(str(model_entity or "")).strip()
    if len(candidate) >= 2 and candidate not in _DEICTIC_WORDS and candidate in source:
        return Anchor(surfaces.get(candidate, candidate), candidate, "model")

    # 2. 正文自己带了名字：只认出处原话里也有的，防模型编造。
    pairs: list[tuple[str, str]] = []
    for surface, canonical in surfaces.items():
        surface_n = _normalize(surface)
        if (
            len(surface_n) >= 2
            and surface_n in source
            and (surface_n in content_text or _normalize(canonical) in content_text)
        ):
            pairs.append((surface, canonical))
    for span in _quoted_names(content_text):
        if _normalize(span) in source:
            pairs.append((span, span))
    for word in _latin_names(content_text):
        if _normalize(word) in source:
            pairs.append((word, word))
    for name in _cjk_names(content_text):
        if _normalize(name) in source:
            pairs.append((name, name))
    names = _dedupe(pairs)
    if len(names) > 1:
        # 正文出现多个名字时，若只是同一实体的全称/简称并存（QQ 官方机器人桥 / QQ），
        # 收敛到最长写法；真正有两个实体才按指称不明处理。
        covered = [name for name in names if not any(name != other and name in other for other in names)]
        if len(covered) == 1:
            canonical = covered[0]
            return Anchor(canonical, names[canonical], "known" if canonical != names[canonical] else "quote")
        return None  # 多实体对话里指称不明，宁可丢弃
    if len(names) == 1:
        canonical, surface = next(iter(names.items()))
        return Anchor(canonical, surface, "known" if canonical != surface else "quote")
    if not allow_source_default:
        return None

    # 3. 正文没写名字：出处原话里只有一个可识别对象时挂到它名下。
    #    来源里的引号专名、拉丁名、中文专名也算候选；多于一个就按指称不明处理。
    source_pairs: list[tuple[str, str]] = [
        (surface, canonical)
        for surface, canonical in surfaces.items()
        if len(_normalize(surface)) >= 2 and _normalize(surface) in source
    ]
    source_pairs.extend(_source_name_pairs(source))
    source_names = _dedupe(source_pairs)
    if len(source_names) == 1:
        canonical, surface = next(iter(source_names.items()))
        return Anchor(canonical, surface, "known")
    return None


@dataclass(frozen=True)
class AnchorDecision:
    """写前校验结论：这条断言要不要锚点、锚到了谁、能不能落账。"""

    required: bool
    anchor: Anchor | None
    ok: bool
    reason: str = ""


def evaluate(
    content: str,
    *,
    entity: str = "",
    source_text: str = "",
    known_names: Mapping[str, str] | Iterable[str] | None = None,
    model_entity: str = "",
    event: bool = False,
    allow_source_default: bool = True,
) -> AnchorDecision:
    """统一写前校验。

    ``entity`` 是已经核实过的已有锚点（存量记录或本轮已判定的结果）；
    给了它就不再要求正文里重写名字。``model_entity`` 是模型自报、仍需核实。
    ``allow_source_default=False`` 表示出处只是「整个窗口」而非某一条原话，
    这时不采用「出处里只有一个名字就挂上去」的兜底，避免在多实体对话里猜错。
    """
    required = needs_anchor_for_event(content) if event else needs_anchor(content)
    if not required:
        # 正文自带主语：不需要锚点也能落账；但正文写了唯一专名时顺手记下来，
        # 方便以后按实体检索。这里不采用「出处里只有一个名字就挂上去」的兜底。
        anchor = resolve_anchor(
            content,
            source_text,
            known_names=known_names,
            model_entity=model_entity,
            allow_source_default=False,
        )
        return AnchorDecision(False, anchor, True)
    if str(entity or "").strip():
        name = str(entity).strip()
        return AnchorDecision(True, Anchor(name, name, "stored"), True)
    anchor = resolve_anchor(
        content,
        source_text,
        known_names=known_names,
        model_entity=model_entity,
        allow_source_default=allow_source_default,
    )
    if anchor is not None:
        return AnchorDecision(True, anchor, True)
    return AnchorDecision(True, None, False, "只有指代/评价，找不到可回指的实体")


def annotate(content: str, anchor: Anchor | None) -> str:
    """正文不放名字时补一个可回指的标注，不改写原句。"""
    if anchor is None or anchor.surface in content or anchor.name in content:
        return content
    return f"关于「{anchor.surface}」：{content}"
