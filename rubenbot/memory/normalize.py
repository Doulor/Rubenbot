"""召回查询的轻量语义归一（第一层，不上向量库）。

问题：账本里的动作词和提问里的动作词对不上。机主在主群「点歌 magnolia」，
随后在另一个群问「你记得我刚刚给你推荐了什么歌吗」——「推荐」在字面上
命中不了「点了」/「点歌」，召回就答「没记到」。

做法（确定性的小词表，轻量、可解释）：

1. **动作词归类**：把「推荐 / 点 / 推 / 安利 / 发了」等说法归到同一类
   规范事件（歌、吃的、链接、图片），查询侧和账本侧共用一套判据；
2. **指代解析**：「刚刚 / 那首 / 推荐的歌」这类问法，直接解析到时间线上
   最近的同类事件（:func:`latest_matching` / :func:`resolve_recent_event`）；
3. **查询扩展**：:func:`expand_query` 给 :func:`~rubenbot.memory.timeline.search_timeline`
   的查询入口追加同类动作词，让现有的字面检索也能找到异形说法。

归一只是召回层的第一步：它只负责回答「问的是哪一类事」，最终命中哪一条
仍由时间线检索和隐私审查决定。

接线（timeline.py 当前由第 17 项 lane 修改，本模块先不接线）。

- **查询入口**：`rubenbot/memory/timeline.py:219` 的 `_query_terms`（`search_timeline`
  在 `:248` 调它）。两行 diff：

  ```diff
  -    from .retrieve import normalize, tokenize
  +    from .retrieve import normalize, tokenize
  +    from .normalize import expand_query

  -    query = _FILLER.sub(' ', normalize(query)).strip()
  +    query = expand_query(_FILLER.sub(' ', normalize(query)).strip())
  ```

  函数内导入避免 retrieve ↔ timeline 的模块级循环依赖。`core.py:2482` 的自查通道
  与 `retrieve.py:1061` 的 `recall` 都经 `search_timeline`，接这一处即可覆盖。
- **指代解析（可选加深）**：`search_timeline` 拿到结果为空、且 `normalize_query(query).referential`
  为真时，可再调 `resolve_recent_event(store, query, now=now, person_id=person_id,
  exclude_chat=exclude_chat)` 补一条时间线最近的同类事件。这一步要读已提炼事件，
  与 `raw_only` 的调用方（`core.py` 自查通道）语义不同，接线时按调用方决定是否启用。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .retrieve import normalize

# 规范事件类型。名字是英文标识符，渲染文案用中文。
SONG = "song"
FOOD = "food"
LINK = "link"
IMAGE = "image"

# 「刚刚」类指代默认只看最近 24 小时；真正排序仍是「时间线上最新的同类事件」。
RECENT_WINDOW_SECONDS = 24 * 3600
# 扩展词最多追加 8 条：_query_terms 的词表有 32 条上限，别把用户原词挤掉。
MAX_EXPANSION_TERMS = 8


@dataclass(frozen=True)
class EventKind:
    """一类规范事件：名词、动作词、账本里的完整说法、查询扩展词。"""

    name: str
    label: str
    nouns: tuple[str, ...]
    actions: tuple[str, ...]
    cues: tuple[str, ...]
    terms: tuple[str, ...]


KINDS: tuple[EventKind, ...] = (
    EventKind(
        name=SONG,
        label="歌相关事件",
        nouns=("歌", "歌曲", "曲子", "音乐", "单曲", "专辑", "旋律"),
        actions=("点", "推荐", "推", "安利", "分享", "发", "放", "播", "唱", "听"),
        cues=(
            "点歌", "推歌", "点了", "点了一首", "放了", "播了", "唱了", "听了",
            "推荐了", "推荐过", "安利", "分享了", "歌曲", "单曲", "专辑",
        ),
        terms=(
            "点歌", "点了", "推歌", "推了", "安利", "分享", "放了", "播了",
            "唱了", "听了", "歌曲", "音乐", "单曲", "专辑", "推荐",
        ),
    ),
    EventKind(
        name=FOOD,
        label="吃吃喝喝",
        nouns=("饭", "菜", "吃", "外卖", "夜宵", "零食", "奶茶", "咖啡", "水果", "火锅"),
        actions=("吃", "喝", "煮", "做", "买", "尝"),
        cues=(
            "吃了", "吃的是", "喝过", "喝了", "点了外卖", "外卖", "做饭",
            "煮了", "夜宵", "零食", "火锅",
        ),
        terms=("吃了", "喝过", "外卖", "点了外卖", "做饭", "煮了", "夜宵", "零食", "火锅"),
    ),
    EventKind(
        name=LINK,
        label="链接/文章",
        nouns=("链接", "网址", "网页", "文章"),
        actions=("发", "分享", "看", "读", "转"),
        cues=("分享链接", "发链接", "链接", "网址", "网页", "文章", "转发了"),
        terms=("分享链接", "发链接", "链接", "网址", "网页", "文章", "看了", "读过"),
    ),
    EventKind(
        name=IMAGE,
        label="图片/表情",
        nouns=("图", "图片", "照片", "截图", "表情", "表情包", "视频", "动图", "头像"),
        actions=("发", "拍", "画", "存"),
        cues=("张图", "个图", "发图", "图片", "照片", "截图", "表情包", "视频", "动图"),
        terms=("发图", "张图", "图片", "照片", "截图", "表情包", "表情", "视频", "拍了"),
    ),
)


# 动作词 → 规范动作。先长后短，避免「推荐」被「推」、「点歌」被「点」截胡。
_ACTION_ALIASES: tuple[tuple[str, str], ...] = (
    ("点歌", "点"), ("点了一首", "点"), ("点了", "点"), ("点", "点"),
    ("推荐", "推荐"), ("推歌", "推荐"), ("推了", "推荐"), ("安利", "推荐"), ("推", "推荐"),
    ("分享", "分享"), ("转发", "分享"), ("发了", "分享"), ("发", "分享"),
    ("放了", "播放"), ("播了", "播放"), ("放", "播放"), ("播", "播放"),
    ("唱", "唱"), ("听了", "听"), ("听", "听"),
    ("吃了", "吃"), ("吃", "吃"), ("喝了", "喝"), ("喝", "喝"),
    ("煮", "做"), ("做", "做"), ("买了", "买"), ("买", "买"),
    ("看了", "看"), ("读了", "看"), ("读", "看"),
    ("拍了", "拍"), ("拍", "拍"), ("画了", "画"), ("画", "画"),
)

# 「刚刚」类时间指代；「刚好」不算。
_RECENT_RE = re.compile(r"刚刚|刚才|方才|刚(?![好巧])")
# 「那首 / 那张 / 那个链接」类指代。
_DEMO_RE = re.compile(
    r"那(?:首|个|张|条|顿|家|份|碗|杯|次|句|件事)|上次|刚才那"
)
# 「推荐了 / 点过 / 吃了」：动作带完成/经历标记，说明问的是发生过的事。
_PAST_RE = re.compile(
    r"(?:推荐|推|点|安利|分享|发|放|播|唱|听|吃|喝|看|读|拍|画|买)(?:了|过)"
)
# 「你记得……吗」：回忆框，本身也说明是在回指旧事。
_RECALL_RE = re.compile(r"记得|记不记得|还记得|想起来|想起")


@dataclass(frozen=True)
class QueryPlan:
    """一条查询的归一结果。``kind`` 为空表示归一器没认出事件类型。"""

    text: str
    kind: str | None
    kind_label: str
    action: str | None
    recent: bool
    demonstrative: bool
    past: bool
    referential: bool
    terms: tuple[tuple[str, int], ...]

    @property
    def expanded_query(self) -> str:
        """原查询 + 同类动作词，可直接交给 search_timeline。"""
        extra = " ".join(term for term, _ in self.terms)
        return f"{self.text} {extra}".strip()


def _kind_scores(text: str) -> list[tuple[int, int, int, EventKind]]:
    """按名词（权重 2）、完整说法（2）、泛动作词（1）给各类打分。"""
    results: list[tuple[int, int, int, EventKind]] = []
    for order, kind in enumerate(KINDS):
        nouns = sum(1 for word in kind.nouns if word in text)
        cues = sum(1 for word in kind.cues if word in text)
        actions = sum(1 for word in kind.actions if word in text)
        score = nouns * 2 + cues * 2 + actions
        if score > 0:
            # 同分时「带名词的类」优先（「点了外卖」算吃的，不算歌）。
            results.append((score, nouns, -order, kind))
    return results


def _demonstrative_kind(text: str) -> str | None:
    """没有显式名词时，从「那首 / 那顿 / 那张」里读出事件类型。"""
    if re.search(r"那(?:首|曲)", text) or "刚才那首" in text:
        return SONG
    if re.search(r"那(?:顿|家|份|碗|杯)", text):
        return FOOD
    if "那条链接" in text or "那个链接" in text:
        return LINK
    if re.search(r"那(?:张|个)图", text) or "那张照片" in text:
        return IMAGE
    return None


def _detect_kind(text: str) -> str | None:
    scores = _kind_scores(text)
    if not scores:
        return None
    return max(scores)[3].name


def canonical_action(text: str) -> str | None:
    """把动作说法归到一个规范动词：推荐 / 点 / 分享 / 播放 / 吃……"""
    text = normalize(text)
    for alias, action in _ACTION_ALIASES:
        if alias in text:
            return action
    return None


def classify_event(text: str) -> str | None:
    """判断一条时间线文本属于哪类事件；认不出返回 ``None``。

    只做轻量词表判断，不调用模型：同一句「点了 magnolia」在查询侧和
    账本侧都会被归到歌相关事件。
    """
    text = normalize(text)
    kind = _detect_kind(text)
    return kind if kind else _demonstrative_kind(text)


def normalize_query(query: str) -> QueryPlan:
    """把一条召回查询归一成「哪类事件 + 是否指代最近」的查询计划。"""
    text = normalize(query)
    kind = classify_event(text)
    if kind is None:
        kind = _demonstrative_kind(text)
    action = canonical_action(text)
    recent = bool(_RECENT_RE.search(text))
    demonstrative = bool(_DEMO_RE.search(text))
    past = bool(_PAST_RE.search(text))
    recall_frame = bool(_RECALL_RE.search(text))
    # 只有「问的是某类事」且带指代/完成标记/回忆框时，才去时间线找最近一条。
    referential = kind is not None and (recent or demonstrative or past or recall_frame)
    label = next((item.label for item in KINDS if item.name == kind), "")
    terms: tuple[tuple[str, int], ...] = ()
    if kind is not None:
        kind_def = next(item for item in KINDS if item.name == kind)
        # 已经在原查询里的词不重复追加，省下词表额度。
        picked = [word for word in kind_def.terms if word not in text][:MAX_EXPANSION_TERMS]
        terms = tuple((word, 4) for word in picked)
    return QueryPlan(
        text=text,
        kind=kind,
        kind_label=label,
        action=action,
        recent=recent,
        demonstrative=demonstrative,
        past=past,
        referential=referential,
        terms=terms,
    )


def expand_query(query: str) -> str:
    """查询扩展：给原查询追加同类动作词，供字面检索命中异形说法。

    没有认出事件类型时原样返回，不改变普通查询的检索行为。
    """
    plan = normalize_query(query)
    if not plan.terms:
        return query
    return f"{query} {' '.join(term for term, _ in plan.terms)}".strip()


def latest_matching(
    entries: Iterable,
    plan: QueryPlan,
    *,
    now: float | None = None,
    since: float | None = None,
):
    """在给定时间线条目里取时间最新的同类事件；没有就返回 ``None``。

    ``entries`` 只需带 ``text`` / ``ts``（``kind`` 可选）；同一条事件和原话
    同时存在时优先取已提炼的事件，摘要更干净。
    """
    if plan.kind is None:
        return None
    cutoff = since
    if cutoff is None and plan.recent:
        cutoff = (time.time() if now is None else now) - RECENT_WINDOW_SECONDS
    best = None
    best_key: tuple[float, int] | None = None
    for entry in entries:
        text = getattr(entry, "text", "") or ""
        if classify_event(text) != plan.kind:
            continue
        try:
            ts = float(getattr(entry, "ts", 0) or 0)
        except (TypeError, ValueError):
            continue
        if now is not None and ts > now:
            continue
        if cutoff is not None and ts < cutoff:
            continue
        key = (ts, 1 if getattr(entry, "kind", "") == "event" else 0)
        if best_key is None or key > best_key:
            best, best_key = entry, key
    return best


async def resolve_recent_event(
    store,
    query: str,
    *,
    now: float | None = None,
    since: float | None = None,
    exclude_chat=None,
    person_id: str | None = None,
    include_self: bool = True,
    limit: int = 256,
    plan: QueryPlan | None = None,
):
    """把「刚刚 / 那首 / 推荐的歌」解析成时间线上最近的同类事件。

    这是给 search_timeline 预留的深一层入口：先看查询是不是在回指某类事，
    是就取时间线最近一条同类记录；不是或没有同类记录时返回 ``None``，
    调用方照常走字面检索。
    """
    plan = plan or normalize_query(query)
    if not plan.referential:
        return None
    now = time.time() if now is None else now
    if since is None and plan.recent:
        since = now - RECENT_WINDOW_SECONDS
    # 延迟导入：timeline 之后会反过来在查询入口调用本模块，避免模块级循环。
    from .timeline import read_timeline

    entries = read_timeline(
        store,
        since=since,
        now=now,
        person_id=person_id,
        exclude_chat=exclude_chat,
        include_self=include_self,
        limit=limit,
    )
    return latest_matching(entries, plan, now=now, since=since)
