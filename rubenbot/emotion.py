"""情绪与好感度：确定性规则、可审计流水、只做背景注入。

设计原则（见 SPEC「情绪与好感度」）：

- **状态变化全部由代码按规则算**，不让模型凭感觉打分；关键词规则宁可漏判，
  不可误判。
- 每笔好感度变化（含惰性折算出来的衰减）都写 ``affinity_log``，可追溯。
- 影响生成只靠两处：上下文里注入一句状态描述、意愿阈值小幅微调；不硬控文本。
- 衰减、无聊、情绪回落全部**惰性计算**：读时按 ``updated_at`` / ``since``
  折算，不单开定时线程。

一期心情是全局一份（不分群）；好感度按人（跨群共享）。隐私档位为
``norecord`` 的人既不记录也不注入——那和「不建画像与时间线」是同一件事。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import EmotionConfig
from .memory.store import BOT_SENDER_ID, MemoryStore

logger = logging.getLogger(__name__)

# 心情枚举（一期固定这几种）
MOOD_CALM = "平静"
MOODS = ("平静", "开心", "委屈", "无聊", "兴奋", "低落")

# 好感度档位：从高到低比较，先命中先返回
AFFINITY_BANDS = ((80.0, "亲近"), (60.0, "友好"), (40.0, "普通"), (20.0, "冷淡"))
LOWEST_BAND = "疏远"
# 默认档位：心情平静 + 这个档位时，注入行整行省略
NEUTRAL_BAND = "普通"

# 好感度流水的原因标记（每日上限只统计「对话」这一种）
REASON_CHAT = "对话"
REASON_PRAISE = "被夸"
REASON_INSULT = "被骂"
REASON_DECAY = "衰减"

WEEK_SECONDS = 7 * 86400.0
# 最近一次「有人理 bot」的时间（被 @/叫名/私聊/回复，或 bot 自己发了言）
LAST_ENGAGED_KEY = "emotion:last_engaged"

# 无聊只顶上「情绪已经基本回落」的空档：高于这个强度时先让原情绪自然淡下去
BOREDOM_FADE_INTENSITY = 0.1
# 已有情绪强到这个程度时，兴奋只算锦上添花，不覆盖（例如刚被夸的开心）
EXCITEMENT_KEEP_STRONG = 0.5

# ---- 关键词规则（保守：宁可漏判）----
# 感谢类：本身就是对着听话人说的，只要确认消息是发给 bot 的就算
GRATITUDE_KEYWORDS = (
    "谢谢",
    "感谢",
    "多谢",
    "辛苦了",
    "辛苦啦",
    "有你真好",
)
# 夸奖类：必须出现第二人称「你」，且「你」和关键词之间不能夹着第三人称
PRAISE_KEYWORDS = (
    "好棒",
    "真棒",
    "很棒",
    "太棒了",
    "太棒",
    "棒极了",
    "厉害",
    "太强了",
    "好强",
    "真强",
    "聪明",
    "好可爱",
    "真可爱",
    "可爱",
    "贴心",
    "温柔",
    "靠谱",
    "喜欢你",
    "爱你",
    "做得好",
    "干得好",
    "写得好",
    "说得好",
    "给力",
    "专业",
    "好人",
)
# 骂人类：同样要求第二人称；命令式的几个词可以不带「你」
INSULT_KEYWORDS = (
    "傻逼",
    "煞笔",
    "傻比",
    "沙比",
    "傻叉",
    "蠢货",
    "蠢死",
    "笨蛋",
    "白痴",
    "智障",
    "弱智",
    "废物",
    "垃圾",
    "恶心",
    "讨厌",
    "讨厌你",
    "讨厌死",
    "烦人",
    "有病",
    "神经病",
    "脑残",
    "混蛋",
    "王八蛋",
    "狗东西",
    "畜生",
)
INSULT_IMPERATIVES = ("滚开", "滚蛋", "滚吧", "闭嘴", "去死", "死开")
# 这些词后面接上常见搭配时是在说东西，不是在骂人（垃圾袋 / 废物利用）
INSULT_COMPOUND_NEXT: dict[str, tuple[str, ...]] = {
    "垃圾": ("袋", "桶", "站", "车", "箱", "分", "食", "短", "话", "清", "收"),
    "废物": ("利", "回", "再"),
}

# 「你」与关键词之间允许的最大距离（覆盖「你真的很厉害」这类插入语）
_YOU_WINDOW = 8
# 否定词：紧挨关键词前面出现就不算夸奖/骂人（「不厉害」「没觉得你厉害」）
_NEGATIONS = "不没别未"
# 第三人称：说明这句话在说别人，不是在说 bot
_THIRD_PERSON = ("他", "她", "它", "别人", "你们")
# 话题转换标记：「你看这个好恶心」说的是那个东西，不是在骂 bot。
# 紧跟在「你」后面的不算（「你这个笨蛋」是在骂 bot 本人）。
_TOPIC_SWITCHES = ("这个", "那个", "这些", "那些", "这种", "那种", "这事", "那事", "这玩意", "那玩意")
# 命令式骂人词前面出现这些就当是在说第三人称（「他滚了」）
_DIRECT_THIRD_PERSON = ("他", "她", "它")


@dataclass
class MoodState:
    """折算过衰减后的当前心情。"""

    mood: str = MOOD_CALM
    intensity: float = 0.0
    since: float = 0.0

    @property
    def is_calm(self) -> bool:
        return self.mood == MOOD_CALM or self.intensity <= 0.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def affinity_band(score: float) -> str:
    """好感度 → 档位：≥80 亲近 / ≥60 友好 / ≥40 普通 / ≥20 冷淡 / <20 疏远。"""
    for threshold, name in AFFINITY_BANDS:
        if score >= threshold:
            return name
    return LOWEST_BAND


def decay_toward_baseline(
    score: float,
    elapsed: float,
    *,
    baseline: float = 50.0,
    weekly: float = 0.05,
) -> float:
    """好感度向基线回归：每过一周，与基线的距离乘以 ``1 - weekly``。"""
    weeks = max(0.0, float(elapsed)) / WEEK_SECONDS
    factor = (1.0 - clamp(weekly, 0.0, 1.0)) ** weeks
    return baseline + (float(score) - baseline) * factor


def decay_intensity(intensity: float, elapsed: float, half_life: float) -> float:
    """情绪强度按半衰期指数回落。"""
    half = max(1e-6, float(half_life))
    return max(0.0, float(intensity)) * (0.5 ** (max(0.0, float(elapsed)) / half))


def intensity_phrase(intensity: float) -> str:
    """强度的文字描述（只用于注入，不给模型看数字）。"""
    if intensity >= 0.75:
        return "很强烈"
    if intensity >= 0.5:
        return "挺明显"
    if intensity >= 0.25:
        return "淡淡的"
    return "若隐若现"


def local_day_start(now: float) -> float:
    """本地时间的当天零点（每日上限按自然日重置）。"""
    local = time.localtime(now)
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )


# ----------------------------------------------------------------------
# 关键词匹配（纯函数，方便单测）
# ----------------------------------------------------------------------


def _negated(text: str, start: int, window: int = 4) -> bool:
    return any(char in _NEGATIONS for char in text[max(0, start - window) : start])


def _suspect_compound(text: str, keyword: str, index: int) -> bool:
    """关键词后面跟着「的」或常见搭配，说明它在修饰别的东西。"""
    tail = text[index + len(keyword) : index + len(keyword) + 1]
    if tail == "的":
        return True
    blocked = INSULT_COMPOUND_NEXT.get(keyword, ())
    return tail in blocked


def _topic_switch(prefix: str) -> bool:
    """前缀里是否出现了指向别的东西的话题标记（「看这个」算，「你这个」不算）。"""
    for word in _TOPIC_SWITCHES:
        start = 0
        while True:
            index = prefix.find(word, start)
            if index < 0:
                break
            start = index + 1
            if index == 0 or prefix[index - 1] != "你":
                return True
    return False


def _you_directed(text: str, keyword: str) -> bool:
    """关键词前面不远处有「你」，且中间没有第三人称或话题转换。"""
    start = 0
    while True:
        index = text.find(keyword, start)
        if index < 0:
            return False
        start = index + 1
        prefix = text[max(0, index - _YOU_WINDOW) : index]
        if "你" not in prefix:
            continue
        if any(word in prefix for word in _THIRD_PERSON):
            continue
        if _topic_switch(prefix):
            continue
        if _negated(text, index):
            continue
        if _suspect_compound(text, keyword, index):
            continue
        return True


def _directed_at_listener(text: str, keyword: str) -> bool:
    """命令式词：不要求「你」，但前面不能是第三人称，也不能是否定。"""
    start = 0
    while True:
        index = text.find(keyword, start)
        if index < 0:
            return False
        start = index + 1
        prefix = text[max(0, index - 4) : index]
        if any(word in prefix for word in _DIRECT_THIRD_PERSON):
            continue
        if _negated(text, index):
            continue
        return True


def _any_keyword(text: str, keywords: tuple[str, ...], matcher) -> bool:
    return any(matcher(text, keyword) for keyword in keywords)


def detect_praise(text: str) -> bool:
    """是否在夸/感谢（调用方已确认消息是发给 bot 的）。"""
    body = (text or "").strip()
    if not body:
        return False
    if _any_keyword(body, GRATITUDE_KEYWORDS, _directed_at_listener):
        return True
    return _any_keyword(body, PRAISE_KEYWORDS, _you_directed)


def detect_insult(text: str) -> bool:
    """是否在骂（调用方已确认消息是发给 bot 的；保守规则）。"""
    body = (text or "").strip()
    if not body:
        return False
    if _any_keyword(body, INSULT_KEYWORDS, _you_directed):
        return True
    return _any_keyword(body, INSULT_IMPERATIVES, _directed_at_listener)


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------


class EmotionEngine:
    """好感度与心情的规则引擎；所有时间都从外部传入，测试完全确定。"""

    def __init__(
        self,
        store: MemoryStore,
        config: EmotionConfig | None = None,
        *,
        now_fn=time.time,
    ):
        self.store = store
        self.config = config or EmotionConfig()
        self.now_fn = now_fn

    # ------------------------------------------------------------------
    # 好感度
    # ------------------------------------------------------------------

    def current_affinity(self, person_id: str, *, now: float | None = None) -> float:
        """当前好感度（惰性折算衰减，不写库）。"""
        now = self.now_fn() if now is None else now
        stored = self.store.get_affinity(person_id)
        if stored is None:
            return self.config.affinity_baseline
        updated = self.store.affinity_updated_at(person_id) or now
        return decay_toward_baseline(
            stored,
            now - updated,
            baseline=self.config.affinity_baseline,
            weekly=self.config.decay_weekly,
        )

    def band_for(self, person_id: str, *, now: float | None = None) -> str:
        return affinity_band(self.current_affinity(person_id, now=now))

    def _fold_and_apply(
        self,
        person_id: str,
        delta: float,
        *,
        reason: str,
        place: str = "",
        now: float,
    ) -> float:
        """把惰性衰减折进存值，再应用本次变化并写流水。"""
        cfg = self.config
        stored = self.store.get_affinity(person_id)
        if stored is None:
            stored = cfg.affinity_baseline
            updated = now
        else:
            updated = self.store.affinity_updated_at(person_id) or now
        decayed = decay_toward_baseline(
            stored,
            now - updated,
            baseline=cfg.affinity_baseline,
            weekly=cfg.decay_weekly,
        )
        # 衰减日志用「小数位保留到 4 位」判断，避免每秒读一次就刷出无数
        # 0.0000 的噪音流水；真正的加减不受这个门槛影响。
        decay_delta = round(decayed - stored, 4)
        if decay_delta:
            self.store.add_affinity_log(
                person_id, decay_delta, reason=REASON_DECAY, place=place, now=now
            )
        new_score = clamp(decayed + float(delta), cfg.affinity_min, cfg.affinity_max)
        applied = round(new_score - decayed, 4)
        self.store.set_affinity(person_id, new_score, now=now)
        if applied:
            self.store.add_affinity_log(
                person_id, applied, reason=reason, place=place, now=now
            )
        return new_score

    def apply_delta(
        self,
        person_id: str,
        delta: float,
        *,
        reason: str,
        place: str = "",
        now: float | None = None,
    ) -> float:
        """按规则加减好感度（公开入口，供测试与插件使用）。"""
        now = self.now_fn() if now is None else now
        if not self.config.enabled or not delta:
            return self.current_affinity(person_id, now=now)
        if self.store.is_norecord(person_id):
            return self.current_affinity(person_id, now=now)
        return self._fold_and_apply(person_id, delta, reason=reason, place=place, now=now)

    def chat_round_allowance(self, person_id: str, *, now: float) -> float:
        """今天还剩多少「对话轮」加分额度（每人每日上限）。"""
        gained = self.store.daily_affinity_gain(
            person_id, local_day_start(now), reason=REASON_CHAT
        )
        return max(0.0, float(self.config.chat_daily_cap) - gained)

    def record_chat_round(
        self,
        person_id: str,
        *,
        place: str = "",
        now: float | None = None,
    ) -> float:
        """与 bot 完成一轮对话：+0.2，超出每日上限就不再加。返回实际增加量。"""
        now = self.now_fn() if now is None else now
        cfg = self.config
        if not cfg.enabled or self.store.is_norecord(person_id):
            return 0.0
        allowance = self.chat_round_allowance(person_id, now=now)
        delta = min(float(cfg.chat_delta), allowance)
        if delta <= 1e-9:
            return 0.0
        before = self.current_affinity(person_id, now=now)
        after = self._fold_and_apply(person_id, delta, reason=REASON_CHAT, place=place, now=now)
        return after - before

    # ------------------------------------------------------------------
    # 心情
    # ------------------------------------------------------------------

    def current_mood(self, *, now: float | None = None) -> MoodState:
        """当前心情：先按半衰期折算，再惰性判断「太久没人理」。"""
        now = self.now_fn() if now is None else now
        cfg = self.config
        row = self.store.get_mood()
        if row is None:
            state = MoodState()
        else:
            intensity = decay_intensity(
                row.intensity, now - row.updated_at, cfg.half_life_seconds
            )
            if intensity < cfg.min_intensity or row.mood not in MOODS:
                state = MoodState(MOOD_CALM, 0.0, row.since)
            else:
                state = MoodState(row.mood, intensity, row.since)
        # 无聊是惰性状态：超过阈值没人理、且当前情绪已经基本回落时才顶上，
        # 不让「三小时前的开心」被立刻改写成无聊。
        last_engaged = self.store.get_meta_float(LAST_ENGAGED_KEY, 0.0)
        if (
            last_engaged > 0
            and now - last_engaged >= cfg.boredom_after_seconds
            and state.intensity < BOREDOM_FADE_INTENSITY
        ):
            return MoodState(cfg.boredom_mood, cfg.boredom_intensity, last_engaged)
        return state

    def react(self, mood: str, intensity: float, *, now: float | None = None) -> None:
        """事件驱动地设置心情；同一种情绪叠加时保留开始时间、取较大强度。"""
        cfg = self.config
        if not cfg.enabled or mood not in MOODS or mood == MOOD_CALM:
            return
        now = self.now_fn() if now is None else now
        current = self.current_mood(now=now)
        value = clamp(float(intensity), 0.0, 1.0)
        since = now
        if current.mood == mood and current.intensity > 0:
            value = max(value, current.intensity)
            since = current.since
        self.store.set_mood(mood, value, since=since, now=now)

    def note_engaged(self, *, now: float | None = None) -> None:
        """记下「有人理 bot」的时刻（无聊判定用）。"""
        now = self.now_fn() if now is None else now
        self.store.set_meta(LAST_ENGAGED_KEY, str(float(now)))

    def ensure_tracking(self, *, now: float | None = None) -> None:
        """冷启动时补一个「有人理」的基准时刻，否则刚启动永远判不出无聊。

        只在从来没有记录时写一次；之后完全由真实互动推进。
        """
        if not self.config.enabled:
            return
        now = self.now_fn() if now is None else now
        if self.store.get_meta_float(LAST_ENGAGED_KEY, 0.0) <= 0:
            self.note_engaged(now=now)

    def maybe_excited(self, platform: str, chat_id: str, *, now: float | None = None) -> bool:
        """群里最近热闹且 bot 参与多 → 兴奋；调用时机是 bot 刚发完言。"""
        cfg = self.config
        if not cfg.enabled or cfg.excitement_window_seconds <= 0:
            return False
        now = self.now_fn() if now is None else now
        try:
            recent = self.store.recent_messages(
                chat_id,
                limit=200,
                since=now - cfg.excitement_window_seconds,
                platform=platform,
                real_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - 统计失败只是不触发
            logger.warning("统计群活跃度失败：%s", exc)
            return False
        if len(recent) < cfg.excitement_min_messages:
            return False
        bot_messages = sum(1 for msg in recent if msg.sender_id == BOT_SENDER_ID)
        if bot_messages < cfg.excitement_min_bot_messages:
            return False
        # 已经有更强的情绪（例如刚被夸的开心）时不覆盖，兴奋只是锦上添花
        current = self.current_mood(now=now)
        if current.mood not in (MOOD_CALM, cfg.boredom_mood) and current.intensity >= EXCITEMENT_KEEP_STRONG:
            return False
        self.react(cfg.excitement_mood, cfg.excitement_intensity, now=now)
        return True

    def threshold_adjust(self, *, now: float | None = None) -> float:
        """当前心情对意愿阈值的微调量（加到阈值上；负数更容易开口）。"""
        cfg = self.config
        if not cfg.enabled:
            return 0.0
        state = self.current_mood(now=now)
        if state.mood == MOOD_CALM or state.intensity < cfg.min_intensity:
            return 0.0
        return cfg.adjust_for(state.mood)

    # ------------------------------------------------------------------
    # 观测与注入
    # ------------------------------------------------------------------

    def observe_message(
        self,
        *,
        person_id: str,
        text: str,
        addressed: bool,
        place: str = "",
        now: float | None = None,
    ) -> dict[str, bool]:
        """处理一条发给 bot 的消息：记互动、识别被夸/被骂。

        ``addressed`` 由调用方按硬触发判断（私聊、@、回复、叫名字）。没对着
        bot 说的话一律不记——群里互骂、夸别人都不算。
        """
        result = {"praise": False, "insult": False}
        cfg = self.config
        if not cfg.enabled or not addressed:
            return result
        if self.store.is_norecord(person_id):
            return result
        now = self.now_fn() if now is None else now
        self.note_engaged(now=now)
        if detect_insult(text):
            self.apply_delta(
                person_id, cfg.insult_delta, reason=REASON_INSULT, place=place, now=now
            )
            self.react(cfg.insult_mood, cfg.insult_intensity, now=now)
            result["insult"] = True
            return result
        if detect_praise(text):
            self.apply_delta(
                person_id, cfg.praise_delta, reason=REASON_PRAISE, place=place, now=now
            )
            self.react(cfg.praise_mood, cfg.praise_intensity, now=now)
            result["praise"] = True
        return result

    def injection_line(self, person_id: str, *, now: float | None = None) -> str:
        """生成上下文里的状态行；平静且普通档位时返回空串（整行省略）。"""
        cfg = self.config
        if not cfg.enabled or self.store.is_norecord(person_id):
            return ""
        now = self.now_fn() if now is None else now
        state = self.current_mood(now=now)
        band = self.band_for(person_id, now=now)
        parts: list[str] = []
        if state.mood != MOOD_CALM and state.intensity >= cfg.min_intensity:
            parts.append(f"心情：{state.mood}（{intensity_phrase(state.intensity)}）")
        if band != NEUTRAL_BAND:
            parts.append(f"对当前说话人：{band}")
        if not parts:
            return ""
        return "【你现在的状态】（只是背景，不必说出来）\n" + "；".join(parts) + "。"

    # ------------------------------------------------------------------
    # WebUI 只读状态
    # ------------------------------------------------------------------

    def status(self, *, now: float | None = None, recent_limit: int = 12) -> dict:
        """给 WebUI 的心情 + 好感 Top5/Bottom3（分数已折算衰减）+ 最近流水。"""
        now = self.now_fn() if now is None else now
        state = self.current_mood(now=now)
        items = []
        for person_id, _score, _updated in self.store.list_affinities():
            score = self.current_affinity(person_id, now=now)
            person = self.store.get_person(person_id)
            items.append(
                {
                    "person_id": person_id,
                    "display_name": (person.display_name if person else "") or person_id,
                    "score": round(score, 1),
                    "band": affinity_band(score),
                }
            )
        top = sorted(items, key=lambda item: (-item["score"], item["person_id"]))[:5]
        top_ids = {item["person_id"] for item in top}
        # Bottom3 只从 Top5 之外的人里取：人数少时同一个人不该在两栏里各出现一次
        rest = [item for item in items if item["person_id"] not in top_ids]
        bottom = sorted(rest, key=lambda item: (item["score"], item["person_id"]))[:3]
        recent = []
        for entry in self.store.list_affinity_logs(limit=max(1, int(recent_limit))):
            person = self.store.get_person(entry.person_id)
            recent.append(
                {
                    "person_id": entry.person_id,
                    "display_name": (person.display_name if person else "") or entry.person_id,
                    "delta": round(entry.delta, 2),
                    "reason": entry.reason,
                    "place": entry.place,
                    "created_at": entry.created_at,
                }
            )
        return {
            "enabled": bool(self.config.enabled),
            "mood": {
                "mood": state.mood,
                "intensity": round(state.intensity, 2),
                "phrase": "" if state.is_calm else intensity_phrase(state.intensity),
                "since": state.since,
            },
            "threshold_adjust": round(self.threshold_adjust(now=now), 3),
            "affinity": {"top": top, "bottom": bottom, "tracked": len(items)},
            "recent_changes": recent,
        }
