"""回复决策：群聊里决定「要不要开口」。

这个模块只做判断，不发消息、不碰适配器：

- :class:`MessageMerger`：同一群聊短时间连发的消息先攒着，合并成一轮再决策。
  纯逻辑、时钟外部传入，测试可以完全确定。
- :class:`DecisionEngine`：硬触发直通（私聊、@、回复 bot、叫名字），普通群消息
  经过节奏护栏后交给策划。冷场主动开口另行评分。
- :func:`paraphrase_verdict`：拿草稿与触发消息做改写复述判定，供发送前复核压稿。
- :func:`parse_generation_output`：解析模型输出里的「不回复」标记。

设计上刻意保持轻量：所有状态都在内存里，进程重启后从零开始；没有额外的表。
"""

from __future__ import annotations

import logging
import random
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .adapters.base import MessageEvent
from .config import DecisionConfig, ProactiveConfig

logger = logging.getLogger(__name__)

# bot 自己发出的消息在流水里的 sender_id（与 core 保持一致）
BOT_SENDER_ID = "__bot__"

# 硬触发的短窗口默认值在这里只做兜底，真正取值来自 DecisionConfig
_DEFAULT_ALIASES = ("Ruben",)

# 除了配置里的 veto_marker，也接受几种常见写法，避免模型自由发挥时漏判
_VETO_ALIASES = {"不回复", "（不回复）", "(不回复)", "【不回复】", "[[不回复]]"}

# 相似度只比较归一化后的短文本：数字、时刻的小变化不是新模板。
_TEMPLATE_VARIABLE = re.compile(r"\d+(?:[.:/-]\d+)*")


def _template(text: str) -> str:
    text = "".join(text[:512].lower().split())
    text = _TEMPLATE_VARIABLE.sub("#", text)
    # 纯数字、表情占位和「好」「哈哈」等短接话不能单独证明存在模板。
    if sum(ch.isalpha() for ch in text) < 4 or text in {"[图片]", "[表情]", "[sticker]"}:
        return ""
    return text


def _similar_template(a: str, b: str) -> bool:
    if a == b:
        return True
    # 字三元组重合度，线性开销；短文本有一处实质差别就宁可放行。
    left = {a[i:i+3] for i in range(len(a) - 2)}
    right = {b[i:i+3] for i in range(len(b) - 2)}
    return len(left & right) >= 0.85 * len(left | right)


# ----------------------------------------------------------------------
# 增量核验：改写复述判定
# ----------------------------------------------------------------------
#
# 附和闸门（:mod:`rubenbot.agreement`）只认字面高重合的长复述，对方换几个
# 词、拆一下断句就能溜过去。这里补的是「改写复述」：草稿的意思基本被触发
# 消息包含、又没有带来新东西时判为复述。做法是纯结构化、可解释的：
#
# 1. 去掉标点与口头填充词（「也就是说」「确实」「啊」等），留下骨架；
# 2. 只看骨架里的实词字（泛词「的了」「天天」「可以」不算信息）：草稿的实词
#    必须都出现在原句里，且实词二元组的顺序基本没变；顺序大乱就是调换了
#    主客体（「小陈接小李」→「小李接小陈」），同样放行；
# 3. 草稿带上新事实（数字、具体某一天）、新态度、第一人称打算、新问法或
#    否定的增减，一律放行。
#
# 不判断「同义换词」（如「推迟一小时」→「晚点一小时」），那是 embedding 的
# 活；这里不引重依赖，宁可漏判也不误杀有信息的接话。

# 泛词：这些字不算信息量，草稿里出现不构成新内容，也不参与顺序判断。
_GENERIC_CHARS = frozenset(
    "的了是我你他她它们咱您这那哪谁啥什么么呢吧啊呀哦喔噢嗯哈嘿哟啦咯喽嘛着得地之与及或而"
    "并且但却还也就都又再才只光仅总一直从来很太真挺蛮好更最特非常比较可以能会该应需要打算准备"
    "不没别未勿被把让给向从对关于因为如果虽然然后但是而且于是就是只是还是还有没有一下子"
    "些个种位块条件次回遍趟点儿样种事情东西地方方式方面问题情况时候时间大家自己别人人家"
    "上下左右前后里外中内间旁边附近周围里面外面上面下面前面后面现在目前刚才马上"
    # 具体某一天/某一年是新事实，不算泛词；「每天」「天天」这类频率才是泛词。
    "早上午中午下午晚上夜里凌晨平日每天一天天天"
    "大小多高低长短快慢新旧老轻重"
    "说看想开玩弄搞做用帮找换带买拿做弄挺好行成变干来去起住存放"
)
# 数字是事实，不进泛词表。「下」也不进：「线上」与「线下」只差一个字，
# 却是完全不同的渠道；「上」保留为泛词，因为「晚上」「上午」里太常见。
_GENERIC_CHARS -= frozenset("一二三四五六七八九十百千万亿两0123456789下")

_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:~～…—―·「」『』“”‘’\"'（）()【】\[\]<>《》]+")
_NUMBER_RE = re.compile(r"\d+")
_NUMERALS = frozenset("零一二三四五六七八九十百千万亿两0123456789")
_NEGATIONS = frozenset("不没别未勿")
_QUESTION_WORDS = ("吗", "什么", "怎么", "为什么", "为啥", "哪", "几", "多少", "如何",
                   "是不是", "有没有", "要不要", "能不能", "何时", "几点")
# 明确的态度/评价词：原句没有、草稿加了，就算带来了新立场。
# 含「快/慢/新鲜」这类单字评价：光秃秃的复述加一句「更新挺快」「那也挺好」
# 也是表态。太常用的泛字（如单字「好」）不单列，改用下面这些带语境的评价说法。
_ATTITUDE_WORDS = ("我觉得", "我感觉", "我认为", "希望", "期待", "担心", "可惜", "开心", "难过",
                   "喜欢", "讨厌", "烦", "厉害", "离谱", "奇怪", "居然", "竟然", "太棒", "完了",
                   "糟糕", "不错", "心疼", "羡慕", "服了", "绝了", "悲哀", "可怜",
                   "快", "慢", "新鲜", "刺激", "无聊", "有意思", "没意思", "划算", "贵", "便宜",
                   "好吃", "难吃", "好看", "难看", "辛苦",
                   "挺好", "还行", "也不错", "太好了", "真棒", "好棒", "赞", "很难受")
# 第一人称的打算/动作：草稿自己要做点什么，是原句没有的新内容。
# 「我也」单独列出：去掉「也」是泛词后仍要看得出这是接话人自己的打算。
_VOLITION_WORDS = ("我去", "我来", "我想", "我要", "我陪", "我帮", "我看", "我试试", "我请", "我带",
                   "我也", "咱们", "我们", "一起")
# 草稿突然改用第一/第二人称称呼（原句里没有），通常是接话人自己的打算或
# 直接对某人说话，都算新增的社交信号；宁可漏判也不压掉。
_PERSON_WORDS = ("我", "咱", "俺", "你", "您")
# 口头填充：出现在句首/句尾时可以剥掉，不改变句子的信息量。
_LEAD_FILLERS = ("也就是说", "换句话说", "说白了", "说到底", "总的来说", "总之", "反正",
                 "没想到", "没成想", "原以为", "还以为", "原来如此", "原来", "其实", "就是说",
                 "说实话", "说真的", "老实说", "讲真", "看来", "所以", "因此", "不过", "可是",
                 "但是", "然后", "而且", "确实如此", "确实是这样", "确实", "的确", "没错",
                 "你说得对", "说得也是", "我也这么觉得", "我也觉得", "我同意", "对对对", "对啊",
                 "对呀", "是啊", "是呀", "是的", "那就是", "可不是嘛", "相当于", "这不就是",
                 "嗯", "哦", "噢", "啊", "唉", "诶", "欸")
_TAIL_FILLERS = ("没错", "对吧", "是吧", "啊", "呀", "呢", "吧", "嘛", "啦", "哦", "哈", "嗯",
                 "噢", "诶", "欸", "咯", "喽", "喔", "哈哈")

# 草稿太短时根本不构成「长复述」，交给原来的附和闸门和策划判断。
_MIN_DRAFT_CHARS = 10
# 原句太短（单字、两个词）时任何改写都可能只是正常接话，不判复述。
_MIN_SOURCE_CHARS = 6
# 草稿剥掉泛词后至少要有这么多实词，才谈得上「复述了原句的内容」。
_MIN_CONTENT_CHARS = 3
# 草稿实词里至少这么多比例仍按原句顺序出现，才算「复述」而不是调换主客体。
_ORDER_KEEP_RATIO = 0.6
# 只比较前这么多字符：问答草稿和触发消息都远短于此，也避免长批次拖慢剥词。
_MAX_SCAN_CHARS = 512


def _compact(text: str) -> str:
    """去掉标点与空白，全角归一，只留比较用的骨架。"""
    normalized = unicodedata.normalize("NFKC", text or "")[:_MAX_SCAN_CHARS]
    return _PUNCT_RE.sub("", normalized).casefold()


def _strip_fillers(text: str) -> str:
    """反复剥掉句首/句尾的口头填充词，剥到不能再剥为止。"""
    changed = True
    while changed and text:
        changed = False
        for filler in _LEAD_FILLERS:
            if text.startswith(filler) and len(text) - len(filler) >= 2:
                text, changed = text[len(filler):], True
                break
        for filler in _TAIL_FILLERS:
            if text.endswith(filler) and len(text) - len(filler) >= 2:
                text, changed = text[: -len(filler)], True
                break
    return text


def _numbers(text: str) -> frozenset[str]:
    """阿拉伯数字整段 + 中文数字单个字符都能构成新时间/数量。"""
    found = set(_NUMBER_RE.findall(text))
    found.update(ch for ch in text if ch in _NUMERALS)
    return frozenset(found)


def _bigrams(text: str) -> list[str]:
    """相邻两字的二元组；顺序判断用，单字文本返回空列表。"""
    return [text[i:i + 2] for i in range(len(text) - 1)]


@dataclass(frozen=True)
class ParaphraseVerdict:
    """一次「草稿是否只是复述触发消息」的判定结果。"""

    repetitive: bool
    reason: str
    # 草稿骨架里剥掉口头填充后剩下的部分，便于日志复盘。
    skeleton: str = ""

    def describe(self) -> str:
        return f"{'复述' if self.repetitive else '有增量'}（{self.reason}）"


def _content_chars(text: str) -> str:
    """只留实词字（去掉泛词与标点），用来判断「草稿说了哪些原句没有的话」。

    泛词（「的了」「天天」「可以」等）不承载信息，先全部剥掉；剩下的实词
    序列才是比对依据。这样「一天一个地区，天天有新地方可以开」的实词只剩
    「一一区」，不会被「天天」「可以」「新地方」这类填充词撑大。
    """
    return "".join(char for char in text if char not in _GENERIC_CHARS)


def paraphrase_verdict(source: str, draft: str) -> ParaphraseVerdict:
    """判断 ``draft`` 是否只是 ``source`` 的改写复述、没有新东西。

    纯函数：同样的输入永远给同样的结论，不碰配置、不调模型、不发消息。
    ``repetitive=True`` 表示草稿的实词基本逐字来自触发消息、顺序也没变，
    没有带来新事实、新问题、新态度；调用方可以在发送前据此压掉这条草稿。

    判定顺序（先放行后拦截，宁可漏判也不误杀）：

    1. 草稿太短、原句太短 → 放行（短接话不是「长复述」）；
    2. 草稿引入原句没有的数字 / 态度词 / 第一人称打算 / 问法 / 否定增减 → 放行；
    3. 剥掉泛词只剩实词；草稿实词太少 → 放行（没有可比的内容）；
    4. 草稿实词必须都来自原句，实词二元组顺序也基本不变 → 判为复述；
       有实词找不到（新人物、新地点、新动作）或顺序大乱（调换主客体）→ 放行。
    """
    raw_draft = unicodedata.normalize("NFKC", draft or "")
    src = _strip_fillers(_compact(source))
    drf = _strip_fillers(_compact(draft))
    if len(drf) < _MIN_DRAFT_CHARS:
        return ParaphraseVerdict(False, "draft_too_short", drf)
    if len(src) < _MIN_SOURCE_CHARS:
        return ParaphraseVerdict(False, "source_too_short", drf)

    # 先看草稿有没有带来新的东西；新增的数字/态度/问法/否定都算增量。
    if _numbers(drf) - _numbers(src):
        return ParaphraseVerdict(False, "new_numbers", drf)
    if any(word in drf and word not in src for word in _ATTITUDE_WORDS):
        return ParaphraseVerdict(False, "new_attitude", drf)
    if any(word in drf and word not in src for word in _VOLITION_WORDS):
        return ParaphraseVerdict(False, "new_stance", drf)
    # 原句没提到任何人，草稿却突然说「我」「你」：是接话人自己的打算或直接
    # 对某人说话，算新的社交信号（「……，你去」这类邀约不算纯复述）。
    if any(word in drf and word not in src for word in _PERSON_WORDS):
        return ParaphraseVerdict(False, "new_person", drf)
    # 否定增减都会改变判断（「不开放」→「开放」同样是新内容），按集合差异放行。
    if {ch for ch in _NEGATIONS if ch in drf} != {ch for ch in _NEGATIONS if ch in src}:
        return ParaphraseVerdict(False, "changed_negation", drf)
    if re.search(r"[？?]", raw_draft) and not re.search(r"[？?]", source or ""):
        return ParaphraseVerdict(False, "new_question", drf)
    if any(word in drf and word not in src for word in _QUESTION_WORDS):
        return ParaphraseVerdict(False, "new_question", drf)

    # 实词比对：草稿实词全部来自原句，且实词二元组的顺序基本没变，才算
    # 「没有新内容」。拆句、换语序会让少量二元组断开，但调换主客体
    # （「小陈接小李」→「小李接小陈」）会让大部分二元组断开，二者可区分。
    draft_content = _content_chars(drf)
    if len(draft_content) < _MIN_CONTENT_CHARS:
        return ParaphraseVerdict(False, "too_little_content", drf)
    source_content = _content_chars(src)
    for char in draft_content:
        if char not in source_content:
            return ParaphraseVerdict(False, f"new_content:{char}", drf)
    source_pairs = set(_bigrams(source_content))
    draft_pairs = _bigrams(draft_content)
    if draft_pairs:
        kept = sum(1 for pair in draft_pairs if pair in source_pairs) / len(draft_pairs)
        if kept < _ORDER_KEEP_RATIO:
            return ParaphraseVerdict(False, "reordered", drf)
    return ParaphraseVerdict(True, "restated_source", drf)


_QUIET_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")


# ----------------------------------------------------------------------
# 合并窗口
# ----------------------------------------------------------------------


@dataclass
class MergedRound:
    """攒在一起、等待决策的一轮消息（保留每条的真实发送者）。"""

    platform: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    events: list[MessageEvent] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    deadline: float = 0.0
    hard: bool = False
    # 批次里出现过 @/回复 bot 的消息；一旦为真，截止时间不再延后
    urgent: bool = False
    # 有真机操作资格的群消息不与其他发送者共用工具轮。
    isolated_sender: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.chat_type, self.chat_id)

    @property
    def text(self) -> str:
        return "\n".join(e.text for e in self.events if e.text and e.text.strip())

    @property
    def primary_sender(self) -> str:
        """按条数选主要发言人；并列时取最后发言的人。"""
        counts = Counter(e.sender_id for e in self.events)
        return max(reversed(self.events), key=lambda e: counts[e.sender_id]).sender_id

    def to_event(self) -> MessageEvent:
        """当前说话人的投影；不同人的正文、引用和身份绝不合成一人的请求。"""
        sender = self.primary_sender
        own = [e for e in self.events if e.sender_id == sender]
        last = own[-1]
        reply = next((e for e in reversed(own) if e.reply_to_id), None)
        raw = dict(last.raw or {})
        raw["merged_count"] = len(self.events)
        segments = [segment for event in own for segment in (event.segments or [])]
        has_voice = any(e.raw.get("has_voice") for e in own) or any(
            segment.type == "record" for segment in segments
        )
        if has_voice:
            raw["has_voice"] = True
        return MessageEvent(
            platform=last.platform,
            chat_type=last.chat_type,
            chat_id=last.chat_id,
            sender_id=last.sender_id,
            sender_name=last.sender_name,
            text="\n".join(e.text for e in own if e.text.strip()),
            reply_to_id=reply.reply_to_id if reply else None,
            reply_sender_id=reply.reply_sender_id if reply else "",
            mentions={uid: name for e in own for uid, name in e.mentions.items()},
            mentions_bot=any(e.mentions_bot or is_reply_to_bot(e) for e in own),
            raw=raw,
            segments=segments,
            familiarity=tuple({tag.entity: tag for e in self.events for tag in e.familiarity}.values()),
        )


class MessageMerger:
    """消息合并窗口。

    规则：

    - 群聊按会话收集，谁发言都刷新静默窗口；私聊立即处理；
    - 硬触发消息使用短窗口（``hard_window``），设为 0 表示立即处理；
    - **整批上限**：批次从首条消息起最多等 ``batch_max`` 秒，到点必放出
      （0 表示不设上限）；
    - **@ 上限**：批次里出现 @/回复 bot 的消息时，截止时间提前到
      「首条 + ``urgent``」秒（0 表示不启用）；
    - **到期不续命**：批次一旦到期封口，新的同人消息不再延长它，而是让
      旧批次先放出、自己进新批次；
    - 窗口设为 0 时所有消息都立即处理。

    所有时间都由调用方传入，因此单测可以用假时钟精确控制。
    """

    def __init__(
        self,
        window: float = 2.5,
        hard_window: float = 1.0,
        *,
        batch_max: float = 10.0,
        urgent: float = 2.0,
    ):
        self.window = max(0.0, float(window))
        self.hard_window = max(0.0, float(hard_window))
        self.batch_max = max(0.0, float(batch_max))
        self.urgent = max(0.0, float(urgent))
        self._pending: dict[tuple[str, str, str], MergedRound] = {}

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def pending(self) -> list[MergedRound]:
        return sorted(self._pending.values(), key=lambda r: (r.first_at, r.deadline))

    def _window_for(self, hard: bool) -> float:
        return self.hard_window if hard else self.window

    def _deadline_for(self, round_: MergedRound, now: float, hard: bool, urgent: bool) -> float:
        """统一截止时间计算：窗口 / 整批上限 / @ 上限取最早者。"""
        deadline = now + self._window_for(hard)
        if self.batch_max > 0:
            deadline = min(deadline, round_.first_at + self.batch_max)
        if urgent and self.urgent > 0:
            deadline = min(deadline, round_.first_at + self.urgent)
        return deadline

    def add(
        self,
        event: MessageEvent,
        now: float,
        *,
        hard: bool = False,
        urgent: bool = False,
        isolate_sender: bool = False,
    ) -> list[MergedRound]:
        """加入一条消息，返回此刻应当处理的轮次（按时间先后）。

        ``urgent`` 表示这条消息 @/回复了 bot，会把本批截止时间提前。
        """
        ready: list[MergedRound] = []
        key = (event.platform, event.chat_type, event.chat_id)

        round_ = self._pending.get(key)
        if round_ is not None and (round_.deadline <= now or (
            (round_.isolated_sender and event.sender_id != round_.sender_id)
            or (isolate_sender and any(item.sender_id != event.sender_id for item in round_.events))
        )):
            # 到期或跨越授权边界就封口；不能把访客的任务拼给机主。
            ready.append(self._pending.pop(key))
            round_ = None
        if round_ is None:
            round_ = MergedRound(
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                sender_id=event.sender_id,
                sender_name=event.sender_name,
                first_at=now,
            )
            self._pending[key] = round_
        round_.isolated_sender |= isolate_sender
        round_.events.append(event)
        round_.last_at = now
        round_.hard = round_.hard or hard
        round_.urgent = round_.urgent or urgent
        round_.deadline = now if event.is_private else self._deadline_for(round_, now, round_.hard, round_.urgent)

        # 窗口为 0（或硬窗口为 0 的硬触发）时立即放行
        if round_.deadline <= now:
            ready.append(self._pending.pop(key))

        ready.sort(key=lambda r: (r.first_at, r.deadline))
        return ready

    def pop_ready(self, now: float) -> list[MergedRound]:
        """取出所有已到期的轮次（不改动未到期的）。"""
        ready = [r for r in self._pending.values() if r.deadline <= now]
        for round_ in ready:
            self._pending.pop(round_.key, None)
        ready.sort(key=lambda r: (r.deadline, r.first_at))
        return ready

    def flush_all(self) -> list[MergedRound]:
        """强制放出全部待处理轮次（停机、测试用）。"""
        ready = sorted(self._pending.values(), key=lambda r: (r.first_at, r.deadline))
        self._pending.clear()
        return ready

    def next_deadline(self) -> float | None:
        if not self._pending:
            return None
        return min(r.deadline for r in self._pending.values())


# ----------------------------------------------------------------------
# 决策结果
# ----------------------------------------------------------------------


@dataclass
class OpenerFactors:
    """开口评分的明细：冷场基础分、话题相关性、指向折扣与随机扰动。

    ``addressing`` 是「这轮对话到底是不是指向 bot」的折扣：消息明确 @ 别人
    或回复别人、又没有点到 bot 时，把它算成负数，压低开口权重。冷场开口
    没有具体事件，这一项恒为 0。
    """

    base: float
    relevance: float
    noise: float = 0.0
    addressing: float = 0.0

    def describe(self) -> str:
        parts = [f"基础{self.base:.2f}", f"相关+{self.relevance:.2f}"]
        if self.addressing:
            parts.append(f"指向{self.addressing:+.2f}")
        parts.append(f"随机{self.noise:+.2f}")
        return " ".join(parts)


@dataclass
class ReplyDecision:
    """是否回复的结论。``reason`` 记录硬触发类型或主要否决因子。"""

    should_reply: bool
    reason: str = ""
    score: float = 0.0
    threshold: float = 0.0
    hard_trigger: str | None = None
    factors: OpenerFactors | None = None
    effective_talk_value: float | None = None
    # 「这轮指向谁」的结构化事实（别人互问、点名别人等）；接线时原样记入决策日志。
    addressing: AddressingFactor | None = None
    # 裁定当时该发送者的画面快照；不读回稍后才完成的视觉结果冒充当时所见。
    visual_note: str = ""

    def log_text(self) -> str:
        verdict = "回" if self.should_reply else "不回"
        if self.factors is None:
            return f"{verdict}（{self.reason}）"
        return f"{verdict}（{self.reason}）分数 {self.score:.2f}/{self.threshold:.2f} [{self.factors.describe()}]"


@dataclass
class GenerationOutput:
    """模型输出解析结果。"""

    text: str
    vetoed: bool = False
    reason: str = ""


# ----------------------------------------------------------------------
# 硬触发与生成后否决
# ----------------------------------------------------------------------


def is_reply_to_bot(event: MessageEvent) -> bool:
    """被回复到 bot：适配器通常已置位 mentions_bot，这里看 raw 兜底。"""
    if not event.reply_to_id:
        return False
    if event.reply_sender_id:
        return event.reply_sender_id == "__bot__"
    raw = event.raw if isinstance(event.raw, dict) else {}
    bot_ids = raw.get("bot_message_ids")
    if bot_ids:
        return any(str(event.reply_to_id) == str(item) for item in bot_ids)
    return bool(raw.get("reply_to_bot"))


# ----------------------------------------------------------------------
# 指向事实：这条消息在问谁
# ----------------------------------------------------------------------
#
# 「一天一个地区」那轮里，宇轩在答多萝，bot 插进去复述了一句。策划看到
# 的对话历史里其实有 @ 目标和被回复人，只是裁定权重没有用上。这里把
# 「消息有没有点到 bot、是不是别人在问别人」做成结构化事实与折扣分，
# 让接管线的裁定层能直接压低开口权重。
#
# 只认平台明确给出的指向（``mentions`` / ``reply_sender_id``），不从正文
# 里的名字猜；猜错别人在叫谁，比漏判更糟。

# 平台用 ``all`` 表示「@所有人」，那不是指向某个人。
_BROADCAST_MENTION = "all"
# 指向折扣：别人问别人时把这一轮的开口权重扣干净（1.0 = 不再查看）。
_ADDRESSING_PENALTY = 1.0


@dataclass
class AddressingFactor:
    """一条消息的指向事实与它对开口权重的折扣。

    - ``to_bot``：平台明确 @ 了 bot 或回复了 bot；
    - ``to_peer``：@ 了某个具体的人或回复了某个具体的人（不含 bot、不含 @所有人）；
    - ``replied_peer``：被回复的那条消息是别人发的——对话发生在别人之间，
      这是比单纯 @ 更强的指向信号；
    - ``penalty``：扣掉的开口权重（0~1）。``score`` 是可直接加到开口权重
      上的折扣分（``-penalty`` 或 0），``penalized`` 表示是否打折。

    ``@`` 了别人但正文没回复任何人时只记录事实（``to_peer`` 为真、``score``
    为 0），交给策划判断——「Argon 和 Ruben 哪个好用」这类话仍可能是问
    bot 的。回复别人的消息则是明确的「别人问别人」，默认在裁定层沉默。
    """

    to_bot: bool = False
    to_peer: bool = False
    replied_peer: bool = False
    # 被指向的具体账号（不含 bot 与 @所有人），只作审计展示用。
    peers: tuple[str, ...] = ()
    penalty: float = 0.0

    def to_dict(self) -> dict:
        """接线写 decision_log 用的结构化事实；不给内部折扣幅度。"""
        return {
            "to_bot": self.to_bot,
            "to_peer": self.to_peer,
            "replied_peer": self.replied_peer,
            "peers": list(self.peers),
            "penalized": self.penalized,
        }

    @property
    def penalized(self) -> bool:
        return self.score < 0

    @property
    def score(self) -> float:
        """折扣分：别人问别人、又没点到 bot 时压低开口权重。"""
        if self.to_bot or not self.replied_peer:
            return 0.0
        return -min(1.0, max(0.0, float(self.penalty or 0.0)))

    def describe(self) -> str:
        if self.to_bot:
            return "指向我"
        if self.penalized:
            return "指向别人"
        if self.to_peer:
            return "也提到别人"
        return ""


def peer_ground(event: MessageEvent) -> AddressingFactor:
    """读出这条消息的指向事实：是不是别人在问别人、有没有点到 bot。

    纯函数，只读 ``mentions`` / ``reply_sender_id`` / ``mentions_bot``；
    只报事实，不带折扣幅度（折扣由 :meth:`DecisionEngine.addressing` 加）：

    - 点到 bot（@ 或回复）→ ``to_bot=True``；
    - 只 @ 了别人 / 只回复了别人 → ``to_peer=True``；
    - 回复的是别人的消息 → ``replied_peer=True``；引用自己的话补充说明不算；
    - ``@所有人`` 不算指向任何人；没有明确指向时各项都是 False。
    """
    to_bot = bool(event.mentions_bot) or is_reply_to_bot(event)
    peers = tuple(
        sorted(str(uid) for uid in event.mentions if uid not in {BOT_SENDER_ID, _BROADCAST_MENTION})
    )
    replied = str(event.reply_sender_id or "")
    replied_peer = bool(replied) and replied not in {BOT_SENDER_ID, str(event.sender_id)}
    if replied_peer and replied not in peers:
        peers = (*peers, replied)
    return AddressingFactor(to_bot=to_bot, to_peer=bool(peers), replied_peer=replied_peer, peers=peers)


def _compile_alias(alias: str) -> re.Pattern[str]:
    """英文/数字别名按词边界匹配，中文/混合别名按子串匹配。"""
    pattern = re.escape(alias.strip())
    if alias.strip().isascii() and re.fullmatch(r"[\w.-]+", alias.strip()):
        pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
    return re.compile(pattern, re.IGNORECASE)


def parse_generation_output(reply: str | None, marker: str = "[不回复]") -> GenerationOutput:
    """解析生成结果：整条输出就是「不回复」标记时静默丢弃。

    只认「整条只有标记」的情况（允许外面包引号/括号、句末标点），避免模型
    在正常句子里提到「不回复」三个字就被误杀。
    """
    text = (reply or "").strip()
    if not text:
        return GenerationOutput("", vetoed=True, reason="empty")
    compact = text.strip(" \t\r\n　。.!！~～「」『』\"'")
    marker_compact = (marker or "").strip()
    if compact == marker_compact or compact in _VETO_ALIASES:
        return GenerationOutput("", vetoed=True, reason="veto_marker")
    # 标记外面再包一层方括号/引号，例如「【[不回复]】」
    if marker_compact and compact.strip("[]【】（）()") == marker_compact.strip("[]【】（）()"):
        return GenerationOutput("", vetoed=True, reason="veto_marker")
    return GenerationOutput(text)


# ----------------------------------------------------------------------
# 静默时段
# ----------------------------------------------------------------------


def parse_quiet_hours(values: Iterable[str] | None) -> list[tuple[int, int]]:
    """把 ``["23:00-07:30"]`` 解析成分钟区间；格式错误的项忽略。"""
    ranges: list[tuple[int, int]] = []
    for item in values or []:
        if not isinstance(item, str):
            continue
        match = _QUIET_RE.fullmatch(item)
        if not match:
            logger.warning("忽略格式错误的静默时段：%r（应为 HH:MM-HH:MM）", item)
            continue
        start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
        if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= end_h <= 23 and 0 <= end_m <= 59):
            logger.warning("忽略非法的静默时段：%r", item)
            continue
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        if start == end:
            continue
        ranges.append((start, end))
    return ranges


def in_quiet_hours(now: float, ranges: Iterable[tuple[int, int]]) -> bool:
    """按本地时间判断是否处于静默时段；支持跨午夜区间。"""
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    for start, end in ranges:
        if start < end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # 跨午夜
            return True
    return False


# ----------------------------------------------------------------------
# 会话护栏与主动冷场评分
# ----------------------------------------------------------------------


class DecisionEngine:
    """识别硬触发，按会话维护真实发言的间隔与主动发言窗口。"""

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        aliases: Iterable[str] = _DEFAULT_ALIASES,
        rng: random.Random | None = None,
    ):
        self.config = config or DecisionConfig()
        self.rng = rng or random.Random()
        self.set_aliases(aliases)
        self._last_reply_at: dict[str, float] = {}
        self._active_replies: dict[str, list[float]] = {}
        # 每笔为一次实际发出的跟评；合批中的同人多条原文仍只算一笔。
        self._source_replies: dict[tuple[str, str, str], list[tuple[float, set[str]]]] = {}
        self._source_cooldowns: dict[tuple[str, str, str], float] = {}
        self._next_source_sweep = 0.0

    def _fatigue_enabled(self) -> bool:
        cfg = self.config
        return cfg.fatigue_window_seconds > 0 and cfg.fatigue_reply_limit > 0 and cfg.fatigue_cooldown_seconds > 0

    def note_source_reply(self, event: MessageEvent, events: list[MessageEvent], now: float) -> None:
        """只在本轮实际发言后调用；不把次要发言人的话误记为被跟评对象。"""
        if not event.is_group or not self._fatigue_enabled():
            return
        templates = {t for e in events if e.sender_id == event.sender_id and (t := _template(e.text))}
        if not templates:
            return
        key = (event.platform, event.chat_id, event.sender_id)
        cutoff = now - self.config.fatigue_window_seconds
        if now >= self._next_source_sweep:
            # 人员流动时也淘汰过期正文，不等待同一账号再次出现。
            self._source_replies = {k: rows for k, rows in self._source_replies.items() if rows[-1][0] > cutoff}
            self._next_source_sweep = now + 60
        recent = self._source_replies.get(key, [])
        self._source_replies[key] = [(ts, texts) for ts, texts in recent if ts > cutoff] + [(now, templates)]

    def source_guard(self, event: MessageEvent, now: float) -> str | None:
        """逐个真实来源检查；解除也返回一次状态，以便调用方审计后继续裁定。"""
        if event.sender_id in self.config.muted_senders:
            return "muted_sender"
        if not event.is_group:
            return None
        key = (event.platform, event.chat_id, event.sender_id)
        enabled = self._fatigue_enabled()
        until = self._source_cooldowns.get(key)
        if until is not None:
            if enabled and now < until:
                return "fatigue_cooldown"
            del self._source_cooldowns[key]
            return "fatigue_released"
        if not enabled or key not in self._source_replies:
            return None
        template = _template(event.text)
        if template:
            cutoff = now - self.config.fatigue_window_seconds
            count = sum(1 for ts, texts in self._source_replies[key] if ts > cutoff
                        and any(_similar_template(template, t) for t in texts))
            if count >= self.config.fatigue_reply_limit:
                self._source_cooldowns[key] = now + self.config.fatigue_cooldown_seconds
                del self._source_replies[key]
                return "fatigue_triggered"
        return None

    def set_aliases(self, aliases: Iterable[str]) -> None:
        """替换硬触发的名字/别名（配置热更新用）。"""
        cleaned: list[str] = []
        for alias in aliases:
            if not alias or not alias.strip():
                continue
            if alias.strip() not in cleaned:
                cleaned.append(alias.strip())
        self.aliases = cleaned
        self._alias_patterns = [(_compile_alias(a), a) for a in cleaned]

    # ---- 硬触发 ----

    def match_alias(self, text: str) -> str | None:
        """消息里叫了 bot 的名字/别名则返回命中的别名。"""
        for pattern, alias in self._alias_patterns:
            if pattern.search(text or ""):
                return alias
        return None

    def is_hard_trigger(self, event: MessageEvent) -> str | None:
        """返回硬触发类型；``None`` 表示普通群消息。"""
        if event.is_private:
            return "private"
        if event.mentions_bot:
            return "mentioned"
        if is_reply_to_bot(event):
            return "reply_to_bot"
        # 有明确收件人时，正文里的名字也可能只是话题；把这种情况留给策划判断。
        if any(uid != "all" for uid in event.mentions) or event.reply_sender_id:
            return None
        alias = self.match_alias(event.text)
        return f"name:{alias}" if alias else None

    # ---- 状态 ----

    def min_reply_interval(self) -> float:
        """最小发言间隔（秒）；0 表示关闭。"""
        return max(0.0, float(getattr(self.config, "min_reply_interval_seconds", 0.0) or 0.0))

    @staticmethod
    def chat_key(event: MessageEvent) -> str:
        return f"{event.platform}:{event.chat_id}"

    def note_bot_reply(self, chat_key: str, now: float, *, proactive: bool = False) -> None:
        """成功发出才刷新冷却；只有未被叫到的主动发言占用窗口名额。"""
        self._last_reply_at[chat_key] = now
        if proactive:
            self._active_replies.setdefault(chat_key, []).append(now)

    def active_reply_count(self, chat_key: str, now: float) -> int:
        """返回滚动窗口内的主动发言次数，并顺手淘汰过期时间戳。"""
        window = max(0.0, float(self.config.proactive_window_seconds))
        values = self._active_replies.get(chat_key, [])
        if window <= 0:
            return 0
        values = [value for value in values if now - value < window]
        self._active_replies[chat_key] = values
        return len(values)

    def last_reply_at(self, chat_key: str) -> float | None:
        return self._last_reply_at.get(chat_key)

    def must_reply(self, event: MessageEvent) -> str | None:
        """必回规则：被 @/点名/回复、私聊消息一律必回；返回原因或 None。

        必回覆盖 stage1 的 ``speak=false``：模型可以说「不想接」，但被叫到
        头上还装死就是事故。``private`` 覆盖现在「私聊必回」的语义（本项目
        没有单独的主人账号配置；私聊主人也就是唯一能私聊到 bot 的人）。
        """
        return self.is_hard_trigger(event)

    def addressing(self, event: MessageEvent) -> AddressingFactor:
        """这条消息的指向事实（别人互问 / 点到 bot），供裁定层与日志直接使用。

        平台明确 @ 了别人 / 回复了别人、正文里也没有叫到 bot 名字时，返回
        带折扣的 :class:`AddressingFactor`（``penalized`` 为真）；正文里叫了
        bot 名字（例如「Argon 和 Ruben 哪个好用」）或点到 bot 本身时不打折，
        交给策划判断。私聊不适用（没有「别人」）。
        """
        ground = peer_ground(event)
        if event.is_private or ground.to_bot or not ground.to_peer or self.match_alias(event.text):
            return ground
        return AddressingFactor(to_bot=False, to_peer=True, replied_peer=ground.replied_peer,
                                peers=ground.peers, penalty=_ADDRESSING_PENALTY)

    def guard(
        self,
        event: MessageEvent,
        *,
        now: float,
        proactive: bool = False,
    ) -> ReplyDecision | None:
        """开口前的廉价护栏；返回 None 表示放行。

        - **必回**：@/点名/私聊直接放行，不管间隔与不应期；
        - **不应期**：``refractory_seconds``，仅拦普通消息（``proactive``）；
        - **最小发言间隔**：这个会话刚说过话，间隔内普通消息不开口
          （``min_reply_interval_seconds``，0 表示关闭；放在不应期之后，
          两者同时命中时以更贴切的不应期为准）；
        - **主动发言上限**：滚动窗口内主动发言到顶后拦下（防刷屏）；
        - **指向别人**：平台明确回复了别人的消息、又没点到 bot 的普通群
          消息，记 ``addressing_elsewhere`` 并压掉开口权重（与其它护栏
          并列，返回的裁定带指向折扣明细）。只 @ 了别人的只记事实。

        这些都在 stage1 之前做，省下模型调用；不改变已记录的发言状态。
        """
        hard = self.must_reply(event)
        if hard:
            return None
        chat_key = self.chat_key(event)
        cfg = self.config
        if cfg.refractory_enabled and cfg.refractory_seconds > 0 and proactive:
            refractory_at = self._last_reply_at.get(chat_key)
            if refractory_at is not None and now - refractory_at < cfg.refractory_seconds:
                return ReplyDecision(False, "refractory", 0.0, 0.0)
        interval = self.min_reply_interval()
        last = self._last_reply_at.get(chat_key)
        if interval > 0 and last is not None and now - last < interval:
            return ReplyDecision(False, "min_interval", 0.0, 0.0)
        if proactive and cfg.proactive_window_enabled and cfg.proactive_window_limit >= 0:
            if self.active_reply_count(chat_key, now) >= cfg.proactive_window_limit:
                return ReplyDecision(False, "proactive_window", 0.0, 0.0)
        if proactive and event.is_group:
            # 别人互问时 bot 没有接口：压低开口权重直接沉默，不再花一次策划调用。
            addressing = self.addressing(event)
            if addressing.penalized:
                return ReplyDecision(
                    False, "addressing_elsewhere", addressing.score, 0.0,
                    factors=OpenerFactors(base=0.0, relevance=0.0, addressing=addressing.score),
                    addressing=addressing,
                )
        return None

    # ---- 评分 ----

    def evaluate_opener(
        self,
        config: ProactiveConfig,
        *,
        relevance: float = 0.0,
    ) -> ReplyDecision:
        """只给主动冷场开口评分：基础分加话题相关性，阈值附近允许扰动。"""
        relevance_bonus = config.relevance_bonus * min(
            1.0, max(0.0, relevance) / max(1e-6, config.relevance_scale)
        )
        factors = OpenerFactors(base=config.base, relevance=relevance_bonus)
        score = factors.base + factors.relevance
        if config.threshold_noise > 0 and abs(score - config.threshold) <= config.threshold_noise_band:
            factors.noise = self.rng.uniform(-config.threshold_noise, config.threshold_noise)
            score += factors.noise
        should = score >= config.threshold
        return ReplyDecision(
            should,
            "proactive_ok" if should else "proactive_below",
            score,
            config.threshold,
            None,
            factors,
        )
