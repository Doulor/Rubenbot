"""QQ 空间说说插件：决定「什么时候发、发什么」，发送一律走核心能力入口。

触发方式（全部由插件配置控制，可自由组合）：

- **命令**：聊天里发 ``/说说 [内容]``。带内容直接发；不带内容则由 LLM
  按当前人格生成一条短说说。命令是明确指令，不走草稿确认，也不受节制三件套限制。
- **间隔定时** ``interval_hours``：后台按固定间隔自动发一条。
- **时间点定时** ``times``：每天固定时间点（可带 ``jitter_minutes`` 抖动）。
  ``schedule_mode``（``interval`` / ``times`` / ``both``）决定两者怎么并存。
- **概率触发** ``prob``：bot 在任意会话完成一轮回复后（以及后台检查 tick），
  按 ``post_chance`` 掷骰决定要不要进入发说说流程。
- **LLM 触发** ``llm_decide``：后台每 ``check_interval_minutes`` 分钟问一次模型
  「现在有没有值得发的事」，模型返回 ``{"post": bool, "content": str}``；
  ``llm_then_prob`` 开启时，模型说发之后还要再过一次概率门。

所有自动模式都强制经过「节制三件套」：冷却 ``cooldown_minutes``、每日上限
``daily_cap``、安静时段 ``quiet_hours``。

**草稿确认** ``draft_confirm``（默认开）：自动模式生成的内容不直接发，先私聊
发给主人，主人回「发」才真正调用 ``qzone.publish``；回其他内容或超时
（``draft_timeout_minutes``）作废。主人用 ``owner_ids`` 识别；没配置时退化为
第一个活跃私聊会话并在日志里注明。

插件不碰任何平台协议，发布只调用核心能力 ``qzone.publish``；适配器没声明、
配置里关了（``adapters.<name>.capabilities.qzone``）或调用出错时，都会把
原因原样告诉用户并记日志。

**空间互动**（后台巡检，写操作默认全关）：配置 ``interact_uins`` 白名单后，
按 ``interact_interval_minutes`` 巡检这些 QQ 最近 ``interact_days`` 天的说说，
可以按概率给他们点赞（``like_enabled`` / ``like_chance``）、让 LLM 写一句短
评论（``comment_enabled`` / ``comment_chance``）；``reply_enabled`` 打开后还会
巡检 ``qzone.my_comments``，用 LLM 回复别人在自己说说下的新评论。所有动作都走
核心能力入口（``qzone.feeds`` / ``qzone.like`` / ``qzone.comment`` /
``qzone.my_comments`` / ``qzone.reply``），按 tid / 评论 id 去重、每轮和每帖
都有上限，安静时段（沿用 ``quiet_hours``）不做任何动作；能力失败只记日志
并退避，不会循环猛打。去重状态落在插件目录下的 ``interact_state.json``。

配置放在插件目录下的 ``config.yaml``（可选，未写用默认值）；嵌入式场景也
可以直接把同结构的 dict 放进 ``ctx.extras["qzone_post_config"]`` 覆盖。
字段见 README.md 与 :class:`QzonePostConfig`。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rubenbot.adapters.base import Segment

logger = logging.getLogger(__name__)

# 适配器里实际注册的能力名（SnowLuma 的 send_qzone_msg）
CAPABILITY = "qzone.publish"
# 空间互动能力（SnowLuma 的 qzone 客户端）
CAP_FEEDS = "qzone.feeds"
CAP_LIKE = "qzone.like"
CAP_COMMENT = "qzone.comment"
CAP_MY_COMMENTS = "qzone.my_comments"
CAP_REPLY = "qzone.reply"
# 核心流水里 bot 自己的发送者 id
_BOT_SENDER_ID = "__bot__"
_QUOTES = "「」『』“”\"'‘’"
_SPACE_RE = re.compile(r"\s+")
# 生成结果的兜底截断：句末标点优先
_SENTENCE_END = "。！？!?…~～；;，,"
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
_SCHEDULE_MODES = ("interval", "times", "both")
# 草稿批准口令（去掉首尾标点后精确匹配）
_APPROVE_WORDS = frozenset({"发"})
# 互动去重状态的默认文件名（放在插件目录下，和 config.yaml 并列）
_INTERACT_STATE_FILE = "interact_state.json"
# 去重状态里最多留多少条记录；超出时按时间淘汰最旧的
_STATE_KEEP = 2000
# 状态里按 tid 记住已点赞/已评论的说说；按评论 id 记住已回复的评论
_STATE_LIKES = "liked_tids"
_STATE_COMMENTS = "commented_tids"
_STATE_REPLIES = "replied_comments"


@dataclass
class QzonePostConfig:
    """插件配置。"""

    # ---- 基础 ----
    # 定时发布间隔（小时）；0 表示关闭间隔定时
    interval_hours: float = 0.0
    # 只允许 owner_ids 里的用户触发命令
    owner_only: bool = False
    # 主人 QQ / 平台 uid 列表；owner_only 开启时必须配置；草稿确认也用它识别主人
    owner_ids: list[str] = field(default_factory=list)
    # 命令名（不带前缀），默认「说说」，即 /说说
    command: str = "说说"
    # 生成的说说最长字数
    max_chars: int = 60
    # 取热点时回看最近多少小时的群聊
    topic_window_hours: float = 6.0
    # 一次取多少条群聊消息作为题材
    topic_limit: int = 20

    # ---- 调度 ----
    # interval | times | both
    schedule_mode: str = "interval"
    # 每天固定时间点，如 ["08:30", "21:00"]
    times: list[str] = field(default_factory=list)
    # 每个时间点的随机抖动（±分钟）
    jitter_minutes: float = 10.0
    # 后台检查 tick 的间隔（分钟），供 prob / llm_decide 使用
    check_interval_minutes: float = 60.0

    # ---- 概率触发 ----
    prob: bool = False
    # 概率门：0~1
    post_chance: float = 0.3

    # ---- LLM 触发 ----
    llm_decide: bool = False
    # 模型说发之后是否再过一次概率门
    llm_then_prob: bool = True

    # ---- 节制三件套（所有自动模式强制生效）----
    cooldown_minutes: float = 480.0
    # 0 表示不限制
    daily_cap: int = 2
    # 形如 "23:00-07:00"；空串关闭
    quiet_hours: str = "23:00-07:00"

    # ---- 草稿确认 ----
    draft_confirm: bool = True
    draft_timeout_minutes: float = 30.0

    # ---- 空间互动（后台巡检；写操作默认全关）----
    # 巡检间隔（分钟）
    interact_interval_minutes: float = 60.0
    # 白名单 QQ；空列表表示不做互动巡检
    interact_uins: list[str] = field(default_factory=list)
    # 只看最近几天的说说/评论，窗口外的整体跳过
    interact_days: float = 3.0
    # 每个 QQ 一次拉多少条说说；自己说说下的评论一次拉多少条
    interact_feed_num: int = 10
    interact_comment_num: int = 10
    # 能力失败后的退避时长（分钟）；期间整个巡检跳过
    interact_backoff_minutes: float = 30.0
    # 自动点赞
    like_enabled: bool = False
    like_chance: float = 0.8
    # 每轮最多赞几条；0 表示不限
    like_cap_per_round: int = 2
    # 每天最多赞几条（跨轮累计，落状态文件）；0 表示不限
    like_daily_cap: int = 20
    # LLM 评论
    comment_enabled: bool = False
    comment_chance: float = 0.3
    comment_cap_per_round: int = 1
    # 每天最多评几条；0 表示不限
    comment_daily_cap: int = 5
    comment_max_chars: int = 30
    # 回复自己说说下的评论
    reply_enabled: bool = False
    reply_cap_per_post: int = 3
    reply_cap_per_round: int = 3
    # 每天最多回几条；0 表示不限
    reply_daily_cap: int = 10
    reply_max_chars: int = 30

    def __post_init__(self) -> None:
        # 配置里手写错的值（越界、非法时段）在这里归一；非法项退回默认值
        self.post_chance = normalize_chance(self.post_chance)
        self.like_chance = normalize_chance(self.like_chance, default=0.8)
        self.comment_chance = normalize_chance(self.comment_chance)
        self.interact_uins = normalize_uins(self.interact_uins)
        self.quiet_hours = str(self.quiet_hours or "").strip()
        if self.quiet_hours and parse_hour_range(self.quiet_hours) is None:
            logger.warning(
                "[qzone_post] quiet_hours 格式不对（%s），已关闭安静时段", self.quiet_hours
            )
            self.quiet_hours = ""
        if self.schedule_mode not in _SCHEDULE_MODES:
            self.schedule_mode = "interval"
        self.times = normalize_times(self.times)


# ----------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------


def read_config(directory: str | Path) -> QzonePostConfig:
    """读取插件目录下的 ``config.yaml``；不存在或格式不对时用默认值。"""
    path = Path(directory) / "config.yaml"
    if not path.exists():
        return QzonePostConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("[qzone_post] 读取 %s 失败，使用默认配置：%s", path, exc)
        return QzonePostConfig()
    return config_from_dict(data if isinstance(data, dict) else {})


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _as_bool(value: Any, default: bool) -> bool:
    """只认真布尔（与项目里其他配置口保持一致）。"""
    return value if isinstance(value, bool) else default


def normalize_chance(value: Any, default: float = 0.3) -> float:
    """概率统一落到 [0, 1]。"""
    try:
        chance = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, chance))


def normalize_uins(value: Any) -> list[str]:
    """把白名单 QQ 规范化成去重后的字符串列表。

    QQ 号可能被写成数字、字符串或 ``"o12345"``；统一去掉前缀 ``o`` 并去掉空项。
    """
    if isinstance(value, (str, int)):
        items: list[Any] = str(value).replace("，", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item).strip().lstrip("oO")
        if text and text not in result:
            result.append(text)
    return result


def normalize_times(value: Any) -> list[str]:
    """把 ``["08:30", ...]`` 或 ``"08:30,21:00"`` 规范化成 ``["08:30", ...]``。"""
    if isinstance(value, str):
        items: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        match = _TIME_RE.match(str(item).strip())
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            normalized = f"{hour:02d}:{minute:02d}"
            if normalized not in result:
                result.append(normalized)
    return result


def config_from_dict(data: dict[str, Any]) -> QzonePostConfig:
    """把配置 dict 转成 :class:`QzonePostConfig`；非法值退回默认值。"""
    defaults = QzonePostConfig()

    command = data.get("command", defaults.command)
    command = str(command).strip().lstrip("/") if command is not None else defaults.command
    owner_ids = data.get("owner_ids", defaults.owner_ids)
    if isinstance(owner_ids, (str, int)):
        owner_ids = [owner_ids]
    if not isinstance(owner_ids, (list, tuple)):
        owner_ids = []
    schedule_mode = str(data.get("schedule_mode", defaults.schedule_mode) or "").lower()
    if schedule_mode not in _SCHEDULE_MODES:
        schedule_mode = defaults.schedule_mode
    quiet_hours = data.get("quiet_hours", defaults.quiet_hours)
    quiet_hours = str(quiet_hours).strip() if quiet_hours is not None else defaults.quiet_hours
    if quiet_hours and parse_hour_range(quiet_hours) is None:
        logger.warning("[qzone_post] quiet_hours 格式不对（%s），已关闭安静时段", quiet_hours)
        quiet_hours = ""

    return QzonePostConfig(
        interval_hours=_as_float(data.get("interval_hours"), defaults.interval_hours),
        owner_only=_as_bool(data.get("owner_only"), defaults.owner_only),
        owner_ids=[str(item).strip() for item in owner_ids if str(item).strip()],
        command=command or defaults.command,
        max_chars=_as_int(data.get("max_chars"), defaults.max_chars, minimum=1),
        topic_window_hours=_as_float(
            data.get("topic_window_hours"), defaults.topic_window_hours, minimum=0.1
        ),
        topic_limit=_as_int(data.get("topic_limit"), defaults.topic_limit, minimum=1),
        schedule_mode=schedule_mode,
        times=normalize_times(data.get("times", defaults.times)),
        jitter_minutes=_as_float(data.get("jitter_minutes"), defaults.jitter_minutes),
        check_interval_minutes=_as_float(
            data.get("check_interval_minutes"), defaults.check_interval_minutes, minimum=0.1
        ),
        prob=_as_bool(data.get("prob"), defaults.prob),
        post_chance=normalize_chance(data.get("post_chance"), defaults.post_chance),
        llm_decide=_as_bool(data.get("llm_decide"), defaults.llm_decide),
        llm_then_prob=_as_bool(data.get("llm_then_prob"), defaults.llm_then_prob),
        cooldown_minutes=_as_float(data.get("cooldown_minutes"), defaults.cooldown_minutes),
        daily_cap=_as_int(data.get("daily_cap"), defaults.daily_cap),
        quiet_hours=quiet_hours,
        draft_confirm=_as_bool(data.get("draft_confirm"), defaults.draft_confirm),
        draft_timeout_minutes=_as_float(
            data.get("draft_timeout_minutes"), defaults.draft_timeout_minutes, minimum=0.1
        ),
        # ---- 空间互动 ----
        interact_interval_minutes=_as_float(
            data.get("interact_interval_minutes"),
            defaults.interact_interval_minutes,
            minimum=0.1,
        ),
        interact_uins=normalize_uins(data.get("interact_uins", defaults.interact_uins)),
        interact_days=_as_float(
            data.get("interact_days"), defaults.interact_days, minimum=0.1
        ),
        interact_feed_num=_as_int(
            data.get("interact_feed_num"), defaults.interact_feed_num, minimum=1
        ),
        interact_comment_num=_as_int(
            data.get("interact_comment_num"), defaults.interact_comment_num, minimum=1
        ),
        interact_backoff_minutes=_as_float(
            data.get("interact_backoff_minutes"), defaults.interact_backoff_minutes
        ),
        like_enabled=_as_bool(data.get("like_enabled"), defaults.like_enabled),
        like_chance=normalize_chance(data.get("like_chance"), defaults.like_chance),
        like_cap_per_round=_as_int(
            data.get("like_cap_per_round"), defaults.like_cap_per_round
        ),
        like_daily_cap=_as_int(data.get("like_daily_cap"), defaults.like_daily_cap),
        comment_enabled=_as_bool(data.get("comment_enabled"), defaults.comment_enabled),
        comment_chance=normalize_chance(
            data.get("comment_chance"), defaults.comment_chance
        ),
        comment_cap_per_round=_as_int(
            data.get("comment_cap_per_round"), defaults.comment_cap_per_round
        ),
        comment_daily_cap=_as_int(
            data.get("comment_daily_cap"), defaults.comment_daily_cap
        ),
        comment_max_chars=_as_int(
            data.get("comment_max_chars"), defaults.comment_max_chars, minimum=1
        ),
        reply_enabled=_as_bool(data.get("reply_enabled"), defaults.reply_enabled),
        reply_cap_per_post=_as_int(
            data.get("reply_cap_per_post"), defaults.reply_cap_per_post
        ),
        reply_cap_per_round=_as_int(
            data.get("reply_cap_per_round"), defaults.reply_cap_per_round
        ),
        reply_daily_cap=_as_int(data.get("reply_daily_cap"), defaults.reply_daily_cap),
        reply_max_chars=_as_int(
            data.get("reply_max_chars"), defaults.reply_max_chars, minimum=1
        ),
    )


# ----------------------------------------------------------------------
# 时间与文本小工具
# ----------------------------------------------------------------------


def parse_hour_range(text: str) -> tuple[int, int] | None:
    """把 ``"23:00-07:00"`` 解析成一天内的分钟数区间 ``(1380, 420)``。"""
    match = _RANGE_RE.match(text or "")
    if not match:
        return None
    values = [int(item) for item in match.groups()]
    for hour in (values[0], values[2]):
        if not 0 <= hour < 24:
            return None
    for minute in (values[1], values[3]):
        if not 0 <= minute < 60:
            return None
    return values[0] * 60 + values[1], values[2] * 60 + values[3]


def in_quiet_hours(now: float, quiet_hours: str) -> bool:
    """``now``（时间戳）是否落在安静时段；空串或非法配置视为不安静。"""
    bounds = parse_hour_range(quiet_hours)
    if bounds is None:
        return False
    start, end = bounds
    if start == end:
        return False  # 起止相同视为关闭，而不是全天
    local = time.localtime(now)
    minutes = local.tm_hour * 60 + local.tm_min
    if start < end:
        return start <= minutes < end
    # 跨午夜，如 23:00-07:00
    return minutes >= start or minutes < end


def local_date(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def parse_llm_decision(raw: str, max_chars: int = 60) -> tuple[bool, str]:
    """解析 LLM 触发模式的 JSON；格式不对时视为「不发」。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        logger.warning("[qzone_post] LLM 判断结果不是 JSON：%s", text[:80])
        return False, ""
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError) as exc:
        logger.warning("[qzone_post] 解析 LLM 判断结果失败：%s", exc)
        return False, ""
    if not isinstance(data, dict):
        return False, ""
    post = data.get("post") is True
    content = clean_generated(str(data.get("content") or ""), max_chars)
    return post, content


def clean_generated(text: str, max_chars: int = 60) -> str:
    """整理 LLM 输出：去掉包装引号/代码块，合并空白，超长时截断。"""
    value = (text or "").strip()
    if value.startswith("```"):
        # 去掉偶发的代码块包装
        value = re.sub(r"^```[^\n]*\n?", "", value)
        value = re.sub(r"\n?```$", "", value).strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    value = " ".join(lines) if lines else ""
    value = _SPACE_RE.sub(" ", value).strip()
    while len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    if max_chars > 0 and len(value) > max_chars:
        head = value[:max_chars]
        # 尽量在句末标点处收住，别把半句话硬切
        cut = max(head.rfind(ch) for ch in _SENTENCE_END)
        if cut >= max_chars // 2:
            head = head[: cut + 1]
        value = head.rstrip()
    return value


@dataclass
class Draft:
    """等待主人确认的一条说说草稿。"""

    platform: str
    chat_id: str
    content: str
    created_at: float
    expires_at: float


@dataclass
class InteractReport:
    """一轮互动巡检的结果；只用于日志与测试排查。"""

    liked: int = 0
    commented: int = 0
    replied: int = 0
    # 整轮被跳过的原因（安静时段、退避中、没有白名单……）
    skipped: str = ""
    # 能力失败导致本轮提前结束
    stopped: bool = False

    @property
    def acted(self) -> bool:
        return bool(self.liked or self.commented or self.replied)


class InteractState:
    """互动的去重状态与每日计数，落插件目录下的 JSON 文件。

    - 点赞/评论按 ``tid`` 去重，回复按 ``tid:comment_id`` 去重；
    - 每日计数按本地日期滚动，重启后沿用当天已用掉的额度；
    - 文件损坏或不可写只记日志，退化成内存状态，绝不打断巡检。
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.tids: dict[str, dict[str, float]] = {
            _STATE_LIKES: {},
            _STATE_COMMENTS: {},
            _STATE_REPLIES: {},
        }
        self.daily_date = ""
        self.daily: dict[str, int] = {"like": 0, "comment": 0, "reply": 0}

    @classmethod
    def load(cls, path: Path | None) -> "InteractState":
        state = cls(path)
        if path is None or not path.exists():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("[qzone_post] 读取互动状态 %s 失败，从头开始：%s", path, exc)
            return state
        if not isinstance(data, dict):
            return state
        for kind in state.tids:
            raw = data.get(kind)
            if not isinstance(raw, dict):
                continue
            for key, value in raw.items():
                try:
                    state.tids[kind][str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        state.daily_date = str(data.get("daily_date") or "")
        daily = data.get("daily")
        if isinstance(daily, dict):
            for kind in state.daily:
                try:
                    state.daily[kind] = max(0, int(daily.get(kind) or 0))
                except (TypeError, ValueError):
                    state.daily[kind] = 0
        return state

    def has(self, kind: str, key: str) -> bool:
        return key in self.tids.get(kind, {})

    def mark(self, kind: str, key: str, now: float) -> None:
        items = self.tids.setdefault(kind, {})
        items[key] = now
        if len(items) > _STATE_KEEP:
            # 只留最近的记录，别让状态文件无限长
            stale = sorted(items, key=items.get)[: len(items) - _STATE_KEEP]
            for item in stale:
                items.pop(item, None)

    def count_replies(self, tid: str) -> int:
        """这条说说累计回过多少条评论（跨轮）。"""
        prefix = f"{tid}:"
        return sum(1 for key in self.tids.get(_STATE_REPLIES, {}) if key.startswith(prefix))

    def roll_day(self, now: float) -> None:
        today = local_date(now)
        if today != self.daily_date:
            self.daily_date = today
            self.daily = {"like": 0, "comment": 0, "reply": 0}

    def daily_count(self, kind: str, now: float) -> int:
        self.roll_day(now)
        return int(self.daily.get(kind, 0))

    def note_action(self, kind: str, now: float) -> None:
        self.roll_day(now)
        self.daily[kind] = self.daily.get(kind, 0) + 1

    def save(self) -> None:
        if self.path is None:
            return
        payload: dict[str, Any] = {
            "version": 1,
            "daily_date": self.daily_date,
            "daily": self.daily,
        }
        payload.update(self.tids)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("[qzone_post] 保存互动状态 %s 失败：%s", self.path, exc)


class QzonePostPlugin:
    """说说插件实例；loader 只调 :func:`register`，其余由实例自己管。"""

    def __init__(self, ctx: Any, config: QzonePostConfig):
        self.ctx = ctx
        self.config = config
        # 调度用到的后台任务：interval / times / tick
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event: asyncio.Event | None = None
        # 随机源可替换，测试里用固定值
        self.rng: Any = random.Random()
        # 草稿只放内存；重启即丢弃（自动功能默认关，丢了也不可惜）
        self._draft: Draft | None = None
        # 草稿超时计时器
        self._draft_timer: asyncio.Task | None = None
        # 节制状态：最近一次发布、当日计数、已触发过的 times 槽位
        self._last_post_ts: float | None = None
        self._daily_date: str = ""
        self._daily_count: int = 0
        self._fired_slots: dict[str, float] = {}
        # 每个时间点当天掷出的抖动偏移（秒）：同一槽位重算时沿用，别每次都变
        self._slot_offsets: dict[str, float] = {}
        # 互动去重状态：插件目录下的 interact_state.json（没有目录时纯内存）
        self._interact_state: InteractState | None = None
        # 能力失败后的退避截止时间；之前的一轮直接跳过
        self._interact_backoff_until: float = 0.0
        # 自己空间的 QQ 号（首次读「自己说说下的评论」时从 feed_uin 记住）
        self._self_uin: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[asyncio.Task]:
        """当前还活着的全部后台任务。"""
        return [task for task in self._tasks.values() if not task.done()]

    @property
    def task(self) -> asyncio.Task | None:
        """任一在跑的后台任务；没有则为 None（第九轮测试与排查沿用）。"""
        tasks = self.tasks
        return tasks[0] if tasks else None

    async def start(self) -> None:
        """插件启用：按配置拉起后台任务。"""
        self._ensure_task()

    async def stop(self) -> None:
        """插件停用：让所有后台任务干净退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        timer = self._draft_timer
        self._draft_timer = None
        if timer is not None and not timer.done():
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 停用清理不应抛给宿主
                logger.warning("[qzone_post] 草稿计时器退出异常：%s", exc)
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._stop_event = None
        # 草稿只存内存：插件停用后不再有计时器，留着会一直挡住重启后的自动发布
        self._draft = None
        for task in tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 停用清理不应抛给宿主
                logger.warning("[qzone_post] 后台任务退出异常：%s", exc)

    def ensure_started(self) -> None:
        """懒启动：宿主第一次分发消息时如果后台任务还没起来，在这里补上。

        不需要宿主改动启动流程；没有启用任何自动模式时什么也不做。
        """
        self._ensure_task()

    def _want_interval(self) -> bool:
        return (
            self.config.schedule_mode in ("interval", "both")
            and self.config.interval_hours > 0
        )

    def _want_times(self) -> bool:
        return (
            self.config.schedule_mode in ("times", "both")
            and bool(self.config.times)
        )

    def _want_tick(self) -> bool:
        return self.config.prob or self.config.llm_decide

    def _want_interact(self) -> bool:
        """互动巡检：必须配了白名单；只读巡检默认开，写动作各自开关。"""
        return bool(self.config.interact_uins)

    def _ensure_task(self) -> None:
        if not getattr(self.ctx.plugin, "enabled", True):
            return
        if not (
            self._want_interval()
            or self._want_times()
            or self._want_tick()
            or self._want_interact()
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环（例如同步加载），等首次分发时再起
            return
        if self._stop_event is None or self._stop_event.is_set():
            self._stop_event = asyncio.Event()
        stop_event = self._stop_event
        wanted: dict[str, tuple[bool, Any, str]] = {
            "interval": (self._want_interval(), self._interval_loop, "qzone_post_interval"),
            "times": (self._want_times(), self._times_loop, "qzone_post_times"),
            "tick": (self._want_tick(), self._tick_loop, "qzone_post_tick"),
            "interact": (self._want_interact(), self._interact_loop, "qzone_post_interact"),
        }
        for key, (enabled, factory, name) in wanted.items():
            if not enabled:
                continue
            current = self._tasks.get(key)
            if current is not None and not current.done():
                continue
            self._tasks[key] = loop.create_task(factory(stop_event), name=name)

    def _bot_stop_event(self) -> asyncio.Event | None:
        """宿主 bot 的停止事件；插件据此随 bot 停机干净退出。"""
        event = getattr(self.ctx.bot, "_stop", None)
        return event if isinstance(event, asyncio.Event) else None

    def _should_exit(self, stop_event: asyncio.Event) -> bool:
        if stop_event.is_set() or not getattr(self.ctx.plugin, "enabled", True):
            return True
        bot_stop = self._bot_stop_event()
        return bot_stop is not None and bot_stop.is_set()

    async def _wait_tick(self, stop_event: asyncio.Event, timeout: float) -> bool:
        """等一次间隔或任一停止信号；返回 True 表示应该退出。

        分小段等待（最长 60 秒一段）：即使宿主没跑 on_stop，插件被禁用或
        bot 停机后，任务也能在一个小段内自己发现并退出。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._should_exit(stop_event):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            waiters: list[asyncio.Future] = [asyncio.ensure_future(stop_event.wait())]
            bot_stop = self._bot_stop_event()
            if bot_stop is not None:
                waiters.append(asyncio.ensure_future(bot_stop.wait()))
            try:
                await asyncio.wait(
                    waiters,
                    timeout=min(remaining, 60.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    # ------------------------------------------------------------------
    # 后台循环
    # ------------------------------------------------------------------

    async def _interval_loop(self, stop_event: asyncio.Event) -> None:
        """按间隔发一条；任何异常只记录，绝不让任务死掉或带崩主循环。"""
        interval = max(0.1, float(self.config.interval_hours) * 3600)
        while not stop_event.is_set():
            try:
                if await self._wait_tick(stop_event, interval):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 调度异常只记录，重新等待
                logger.warning("[qzone_post] 定时等待异常：%s", exc)
                await asyncio.sleep(min(interval, 60.0))
                continue
            if self._should_exit(stop_event):
                return
            try:
                await self.publish_scheduled()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次失败不影响下一轮
                logger.warning("[qzone_post] 定时发说说失败：%s", exc)

    async def _times_loop(self, stop_event: asyncio.Event) -> None:
        """每天按固定时间点（带抖动）发；每个槽位一天只触发一次。"""
        while not stop_event.is_set():
            now = time.time()
            target = self._next_time_target(now)
            if target is None:
                return
            fire_at, slot = target
            try:
                if await self._wait_tick(stop_event, max(0.05, fire_at - now)):
                    return
                # 睡醒后重新校一次时钟：系统时间被调过、本机挂起过都可能让
                # 原定时刻错位，到点了才发，别提前把槽位吃掉
                while time.time() < fire_at - 1.0:
                    if await self._wait_tick(stop_event, max(0.05, fire_at - time.time())):
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[qzone_post] 时间点等待异常：%s", exc)
                await asyncio.sleep(60.0)
                continue
            if self._should_exit(stop_event):
                return
            self._fired_slots[slot] = time.time()
            try:
                ok, detail = await self.auto_post(source="times")
                if not ok:
                    logger.info("[qzone_post] 时间点 %s 没有发布：%s", slot, detail)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次失败不影响后续槽位
                logger.warning("[qzone_post] 时间点发说说失败：%s", exc)

    async def _tick_loop(self, stop_event: asyncio.Event) -> None:
        """后台检查：概率触发与 LLM 判断都挂在这个 tick 上。"""
        period = max(0.1, float(self.config.check_interval_minutes) * 60)
        while not stop_event.is_set():
            try:
                if await self._wait_tick(stop_event, period):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[qzone_post] 检查 tick 等待异常：%s", exc)
                await asyncio.sleep(min(period, 60.0))
                continue
            if self._should_exit(stop_event):
                return
            try:
                await self.run_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次检查失败不影响下一轮
                logger.warning("[qzone_post] 后台检查失败：%s", exc)

    def _next_time_target(self, now: float) -> tuple[float, str] | None:
        """下一个待触发的时间点 ``(时间戳, 槽位 id)``；没有则 None。"""
        if not self.config.times:
            return None
        for slot, fired in list(self._fired_slots.items()):
            if fired < now - 3 * 86400:  # 只留最近三天，防止字典无限长
                self._fired_slots.pop(slot, None)
                self._slot_offsets.pop(slot, None)
        local_now = _dt.datetime.fromtimestamp(now)
        stale_before = (local_now.date() - _dt.timedelta(days=3)).isoformat()
        for slot in list(self._slot_offsets):
            if slot[:10] < stale_before:  # 没触发就停掉的日子，别一直攒着
                self._slot_offsets.pop(slot, None)
        jitter = max(0.0, float(self.config.jitter_minutes))
        best: tuple[float, str] | None = None
        for day_offset in (0, 1):
            day = local_now.date() + _dt.timedelta(days=day_offset)
            for hhmm in self.config.times:
                slot = f"{day.isoformat()} {hhmm}"
                if slot in self._fired_slots:
                    continue
                hour, minute = int(hhmm[:2]), int(hhmm[3:])
                base = _dt.datetime.combine(day, _dt.time(hour, minute)).timestamp()
                if slot not in self._slot_offsets:
                    self._slot_offsets[slot] = (
                        self.rng.uniform(-jitter, jitter) * 60 if jitter > 0 else 0.0
                    )
                fire_at = base + self._slot_offsets[slot]
                if fire_at <= now + 0.5:  # 今天这个点已经过了
                    continue
                if best is None or fire_at < best[0]:
                    best = (fire_at, slot)
        return best

    # ------------------------------------------------------------------
    # 自动发布主流程
    # ------------------------------------------------------------------

    async def run_tick(self, *, now: float | None = None) -> tuple[bool, str] | None:
        """后台检查 tick：LLM 判断优先，否则走概率门。返回 None 表示没进入流程。

        顺便把到期的草稿作废；没有自动模式时草稿确认本来就无从触发。
        """
        now = time.time() if now is None else now
        await self._expire_draft(now)
        if self.config.llm_decide:
            blocked = self._block_reason(now)
            if blocked:
                logger.debug("[qzone_post] LLM 检查跳过：%s", blocked)
                return None
            should_post, content = await self.decide_with_llm(now=now)
            if not should_post:
                logger.info("[qzone_post] LLM 判断现在没什么值得发的")
                return None
            if self.config.llm_then_prob and not self._roll(self.config.post_chance):
                logger.info("[qzone_post] LLM 说想发，但没过概率门")
                return None
            return await self.auto_post(source="llm", content=content or None, now=now)
        if self.config.prob:
            if not self._roll(self.config.post_chance):
                return None
            return await self.auto_post(source="prob", now=now)
        return None

    async def on_opportunity(self, *, now: float | None = None) -> tuple[bool, str] | None:
        """机会事件（bot 完成一轮回复后）：按概率决定是否发说说。"""
        if not self.config.prob:
            return None
        if not self._roll(self.config.post_chance):
            return None
        return await self.auto_post(source="opportunity", now=now)

    async def auto_post(
        self,
        *,
        source: str,
        content: str | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """自动模式统一入口：节制检查 → 生成内容 → 草稿确认或直接发布。

        ``content`` 为空时按人格生成；返回 ``(流程是否成功, 说明/拦截原因)``。
        """
        now = time.time() if now is None else now
        await self._expire_draft(now)
        blocked = self._block_reason(now)
        if blocked:
            logger.info("[qzone_post] %s 触发被拦下：%s", source, blocked)
            return False, blocked
        if content is None:
            topic = self._recent_topic(now=now)
            content = await self.generate_post(topic=topic)
            if not content:
                logger.warning("[qzone_post] %s 触发：没能生成内容，跳过这一轮", source)
                return False, "没能生成内容"
        if self.config.draft_confirm:
            return await self._send_draft(content, now=now)
        ok, detail = await self._call_publish(content)
        if ok:
            self._note_published(now)
            logger.info("[qzone_post] %s 触发的说说发布成功：%s", source, content)
        else:
            logger.warning("[qzone_post] %s 触发的说说发布失败：%s", source, detail)
        return ok, detail

    def _block_reason(self, now: float) -> str | None:
        """节制三件套 + 待确认草稿的拦截原因；可发布时返回 None。"""
        if self._draft is not None:
            return "还有一条草稿在等主人确认"
        if in_quiet_hours(now, self.config.quiet_hours):
            return f"安静时段（{self.config.quiet_hours} 不发）"
        cooldown = max(0.0, float(self.config.cooldown_minutes)) * 60
        if self._last_post_ts is not None and cooldown > 0:
            elapsed = now - self._last_post_ts
            if elapsed < cooldown:
                remain = (cooldown - elapsed) / 60
                return f"冷却中（还要 {remain:.0f} 分钟）"
        if self.config.daily_cap > 0:
            self._roll_daily_date(now)
            if self._daily_count >= self.config.daily_cap:
                return "今天的自动说说已经到上限了"
        return None

    def _roll_daily_date(self, now: float) -> None:
        today = local_date(now)
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0

    def _note_published(self, now: float) -> None:
        self._roll_daily_date(now)
        self._daily_count += 1
        self._last_post_ts = now

    def _roll(self, chance: float) -> bool:
        return float(chance) > 0 and self.rng.random() < float(chance)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    async def handle_command(self, runtime: Any, event: Any, args: str) -> str:
        if self.config.owner_only:
            if not self.config.owner_ids:
                return "只有主人能让我发说说，但插件还没配置主人（owner_ids）"
            if str(getattr(event, "sender_id", "")) not in self.config.owner_ids:
                return "只有主人能让我发说说"

        # 命令是明确指令：有待确认的草稿时直接顶掉，免得它继续挡着自动发布
        if self._draft is not None:
            await self._discard_draft()

        content = (args or "").strip()
        if content:
            return await self._publish(content, prefix="说说已经发了：")
        content = await self.generate_post()
        if not content:
            return "这次没想出要说什么，晚点再试试？"
        return await self._publish(content, prefix="说说已经发了：")

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    async def _call_publish(self, content: str) -> tuple[bool, str]:
        return await self.ctx.call_capability(CAPABILITY, content=content)

    async def _publish(self, content: str, *, prefix: str = "") -> str:
        ok, detail = await self._call_publish(content)
        if not ok:
            logger.warning("[qzone_post] 发布失败：%s", detail)
            return f"发不了说说：{detail}"
        return f"{prefix}{content}" if prefix else detail

    async def publish_scheduled(self, *, now: float | None = None) -> tuple[bool, str]:
        """间隔定时路径：走自动流程（节制检查 + 草稿确认）。"""
        return await self.auto_post(source="interval", now=now)

    # ------------------------------------------------------------------
    # 草稿确认
    # ------------------------------------------------------------------

    async def _send_draft(self, content: str, *, now: float) -> tuple[bool, str]:
        """把内容私聊发给主人等待确认；找不到主人私聊就放弃这一条。"""
        target = self._owner_target()
        if target is None:
            logger.warning(
                "[qzone_post] 草稿确认已开启，但找不到主人的私聊会话；这条不发了：%s", content
            )
            return False, "找不到主人的私聊，草稿没有发出"
        platform, chat_id = target
        # 新草稿顶掉旧草稿（正常情况下有草稿时 auto_post 已被拦下）
        self._cancel_draft_timer()
        self._draft = Draft(
            platform=platform,
            chat_id=chat_id,
            content=content,
            created_at=now,
            expires_at=now + self.config.draft_timeout_minutes * 60,
        )
        minutes = self.config.draft_timeout_minutes
        text = (
            "【说说草稿】自动模式下想发这条：\n"
            f"{content}\n\n"
            f"回「发」就发出去；回「算了」或其他内容作废。{minutes:g} 分钟内有效。"
        )
        if not await self._send_private(platform, chat_id, text):
            self._draft = None
            return False, "给主人发草稿失败"
        self._schedule_draft_expiry(now)
        logger.info("[qzone_post] 说说草稿已发给主人确认（%s/%s）", platform, chat_id)
        return True, f"已发给主人确认（{chat_id}）"

    def _schedule_draft_expiry(self, now: float) -> None:
        """给当前草稿挂一个到点作废的计时器；没有事件循环时靠 tick 兜底。"""
        self._cancel_draft_timer()
        draft = self._draft
        if draft is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, draft.expires_at - now)
        self._draft_timer = loop.create_task(
            self._draft_expiry_after(delay), name="qzone_post_draft_expiry"
        )

    def _cancel_draft_timer(self) -> None:
        timer = self._draft_timer
        self._draft_timer = None
        if timer is None or timer.done():
            return
        try:
            if timer is asyncio.current_task():
                return
        except RuntimeError:
            pass
        timer.cancel()

    async def _draft_expiry_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._expire_draft(time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 计时器异常不能带崩宿主
            logger.warning("[qzone_post] 草稿超时计时器异常：%s", exc)

    def _owner_target(self) -> tuple[str, str] | None:
        """草稿发给谁：优先主人的活跃私聊，其次第一个活跃私聊。"""
        try:
            chats = self.ctx.store.list_chats(limit=50)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_post] 读取会话列表失败：%s", exc)
            chats = []
        privates = [chat for chat in chats if chat.get("chat_type") == "private"]
        owners = set(self.config.owner_ids)
        if owners:
            for chat in privates:
                if str(chat.get("chat_id")) in owners:
                    return str(chat["platform"]), str(chat["chat_id"])
            # 没有主人的活跃私聊：平台取最近会话（或唯一的适配器），
            # 目标直接按 owner_ids[0] 发私聊
            platform = self._guess_platform(chats)
            if platform is not None:
                logger.info(
                    "[qzone_post] 没找到主人 %s 的活跃私聊，按平台 %s 直接私聊",
                    self.config.owner_ids[0],
                    platform,
                )
                return platform, self.config.owner_ids[0]
            return None
        if privates:
            chat = privates[0]
            logger.info(
                "[qzone_post] owner_ids 未配置，草稿发给第一个活跃私聊 %s/%s",
                chat["platform"],
                chat["chat_id"],
            )
            return str(chat["platform"]), str(chat["chat_id"])
        runtime = getattr(self.ctx, "runtime", None)
        event = getattr(runtime, "event", None)
        if event is not None and getattr(event, "is_private", False):
            return str(event.platform), str(event.chat_id)
        return None

    def _guess_platform(self, chats: list[dict]) -> str | None:
        if chats:
            return str(chats[0]["platform"])
        runtime = getattr(self.ctx, "runtime", None)
        event = getattr(runtime, "event", None)
        if event is not None:
            return str(event.platform)
        bot = self.ctx.bot
        adapters = getattr(bot, "adapters", None) or {}
        if len(adapters) == 1:
            return str(next(iter(adapters)))
        return None

    async def _send_private(self, platform: str, chat_id: str, text: str) -> bool:
        bot = self.ctx.bot
        adapters = getattr(bot, "adapters", None) or {}
        adapter = adapters.get(platform)
        if adapter is None and len(adapters) == 1:
            adapter = next(iter(adapters.values()))
        if adapter is None:
            logger.warning("[qzone_post] 平台 %s 没有在线适配器，私聊发不出去", platform)
            return False
        try:
            await adapter.send(chat_id, [Segment.text(text)], chat_type="private")
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主循环
            logger.warning("[qzone_post] 发送私聊通知失败：%s", exc)
            return False
        return True

    async def handle_draft_message(self, event: Any) -> bool:
        """处理主人对草稿的回复；返回 True 表示这条消息被插件吃掉。"""
        if self._draft is None:
            return False
        await self._expire_draft(time.time())
        draft = self._draft
        if draft is None:
            return False
        if not getattr(event, "is_private", False):
            return False
        if str(getattr(event, "platform", "")) != draft.platform:
            return False
        if str(getattr(event, "chat_id", "")) != draft.chat_id:
            return False
        if self.config.owner_ids and str(getattr(event, "sender_id", "")) not in set(
            self.config.owner_ids
        ):
            return False
        text = (event.text or "").strip()
        prefix = self.ctx.config.plugins.command_prefix
        if not text or (prefix and text.startswith(prefix)):
            return False  # 空消息 / 命令不当成对草稿的回复
        if text.strip("。！!，,. 　") in _APPROVE_WORDS:
            await self._approve_draft()
            return True
        await self._discard_draft()
        return True

    async def _approve_draft(self, *, now: float | None = None) -> tuple[bool, str]:
        draft = self._draft
        self._draft = None
        self._cancel_draft_timer()
        if draft is None:
            return False, "没有待确认的草稿"
        now = time.time() if now is None else now
        ok, detail = await self._call_publish(draft.content)
        if ok:
            self._note_published(now)
            logger.info("[qzone_post] 主人确认，说说已发布：%s", draft.content)
            await self._send_private(draft.platform, draft.chat_id, f"发好了：{draft.content}")
        else:
            logger.warning("[qzone_post] 主人确认后发布失败：%s", detail)
            await self._send_private(draft.platform, draft.chat_id, f"发不出去：{detail}")
        return ok, detail

    async def _discard_draft(self) -> None:
        draft = self._draft
        self._draft = None
        self._cancel_draft_timer()
        if draft is None:
            return
        logger.info("[qzone_post] 主人没确认，草稿作废：%s", draft.content)
        await self._send_private(draft.platform, draft.chat_id, "好，这条不发了。")

    async def _expire_draft(self, now: float) -> Draft | None:
        """超时作废；返回被作废的草稿（调用方可自行通知，这里已经通知过）。"""
        draft = self._draft
        if draft is None or now < draft.expires_at:
            return None
        self._draft = None
        self._cancel_draft_timer()
        logger.info("[qzone_post] 草稿超时作废：%s", draft.content)
        await self._send_private(
            draft.platform,
            draft.chat_id,
            f"刚才那条说说草稿超过 {self.config.draft_timeout_minutes:g} 分钟没确认，作废了。",
        )
        return draft

    async def check_draft_timeout(self, *, now: float | None = None) -> bool:
        """供 tick / 测试调用：到点就作废草稿。返回是否作废了草稿。"""
        now = time.time() if now is None else now
        return await self._expire_draft(now) is not None

    @property
    def pending_draft(self) -> Draft | None:
        return self._draft

    # ------------------------------------------------------------------
    # 空间互动：后台巡检
    # ------------------------------------------------------------------

    @property
    def interact_state(self) -> InteractState:
        """互动去重状态；首次访问时从插件目录的 JSON 读入。"""
        if self._interact_state is None:
            path = getattr(getattr(self.ctx, "plugin", None), "path", None)
            state_path = Path(path) / _INTERACT_STATE_FILE if path is not None else None
            self._interact_state = InteractState.load(state_path)
        return self._interact_state

    def _interact_block_reason(self, now: float) -> str | None:
        """这一轮不做任何互动的原因；可以做时返回 None。"""
        if not self.config.interact_uins:
            return "没有配置互动白名单（interact_uins）"
        if in_quiet_hours(now, self.config.quiet_hours):
            return f"安静时段（{self.config.quiet_hours} 不互动）"
        if now < self._interact_backoff_until:
            remain = (self._interact_backoff_until - now) / 60
            return f"能力失败后还在退避（还要 {remain:.0f} 分钟）"
        return None

    def _back_off(self, now: float) -> None:
        """能力失败：记日志并推迟下一轮，绝不重试猛打。"""
        minutes = max(0.0, float(self.config.interact_backoff_minutes))
        self._interact_backoff_until = now + minutes * 60
        if minutes > 0:
            logger.warning("[qzone_post] 互动能力失败，暂停巡检 %g 分钟", minutes)

    async def run_interact(self, *, now: float | None = None) -> InteractReport:
        """巡检一轮：白名单动态（点赞/评论）+ 回复自己说说下的评论。

        只读巡检默认开（没有任何写开关时只拉说说、不做任何写操作）；安静时段、
        退避中或没配白名单时整轮跳过；能力失败只记日志并退避，不会循环猛打。
        """
        now = time.time() if now is None else now
        report = InteractReport()
        blocked = self._interact_block_reason(now)
        if blocked:
            report.skipped = blocked
            logger.debug("[qzone_post] 互动巡检跳过：%s", blocked)
            return report
        write = (
            self.config.like_enabled
            or self.config.comment_enabled
            or self.config.reply_enabled
        )
        try:
            if not await self._scan_feeds(now, report, write=write):
                return report
            if write and self.config.reply_enabled and not report.stopped:
                await self._reply_comments(now, report)
        finally:
            self.interact_state.save()
        if report.acted:
            logger.info(
                "[qzone_post] 互动巡检完成：赞 %s、评 %s、回 %s",
                report.liked,
                report.commented,
                report.replied,
            )
        return report

    async def _scan_feeds(self, now: float, report: InteractReport, *, write: bool) -> bool:
        """拉白名单 QQ 最近几天的新说说；``write`` 为真时按开关点赞/评论。

        返回 False 表示能力失败（调用方据此结束本轮）。
        """
        since = now - max(0.1, float(self.config.interact_days)) * 86400
        like_cap = self._cap(self.config.like_cap_per_round)
        comment_cap = self._cap(self.config.comment_cap_per_round)
        for uin in self.config.interact_uins:
            ok, result = await self._call_capability(
                CAP_FEEDS, uin=uin, num=self.config.interact_feed_num
            )
            if not ok:
                self._fail(f"读取 {uin} 的说说", result, now, report)
                return False
            for feed in _as_items(result):
                tid = str(feed.get("tid") or "").strip()
                created = _to_timestamp(feed.get("created_time"))
                if not tid or created is None or created < since:
                    continue  # 窗口外的老说说整体跳过
                if not write:
                    logger.debug("[qzone_post] 只读巡检：看到 %s 的新说说 %s", uin, tid)
                    continue
                if (
                    self.config.like_enabled
                    and feed.get("is_liked") is not True
                    and (like_cap == 0 or report.liked < like_cap)
                    and self._daily_ok("like", self.config.like_daily_cap, now)
                    and not self.interact_state.has(_STATE_LIKES, tid)
                ):
                    if not await self._try_like(uin, tid, now, report):
                        return False
                if (
                    self.config.comment_enabled
                    and (comment_cap == 0 or report.commented < comment_cap)
                    and self._daily_ok("comment", self.config.comment_daily_cap, now)
                    and not self.interact_state.has(_STATE_COMMENTS, tid)
                ):
                    if not await self._try_comment(uin, tid, feed, now, report):
                        return False
        return True

    async def _try_like(
        self, uin: str, tid: str, now: float, report: InteractReport
    ) -> bool:
        """按概率给一条新说说点赞；返回 False 表示能力失败。"""
        if not self._roll(self.config.like_chance):
            return True
        ok, detail = await self._call_capability(CAP_LIKE, uin=uin, tid=tid)
        if not ok:
            self._fail(f"给 {tid} 点赞", detail, now, report)
            return False
        state = self.interact_state
        state.mark(_STATE_LIKES, tid, now)
        state.note_action("like", now)
        report.liked += 1
        logger.info("[qzone_post] 给 %s 的说说 %s 点了赞", uin, tid)
        return True

    async def _try_comment(
        self, uin: str, tid: str, feed: dict[str, Any], now: float, report: InteractReport
    ) -> bool:
        """按概率让 LLM 写一句短评论；返回 False 表示能力失败。"""
        if not self._roll(self.config.comment_chance):
            return True
        text = await self._generate_interact_text(
            kind="comment",
            content=str(feed.get("content") or ""),
            max_chars=self.config.comment_max_chars,
        )
        if not text:
            return True
        ok, detail = await self._call_capability(
            CAP_COMMENT, uin=uin, tid=tid, content=text
        )
        if not ok:
            self._fail(f"评论 {tid}", detail, now, report)
            return False
        state = self.interact_state
        state.mark(_STATE_COMMENTS, tid, now)
        state.note_action("comment", now)
        report.commented += 1
        logger.info("[qzone_post] 在 %s 的说说 %s 下评论：%s", uin, tid, text)
        return True

    async def _reply_comments(self, now: float, report: InteractReport) -> None:
        """回复自己说说下别人发的新评论；同一评论只回一次。"""
        ok, result = await self._call_capability(
            CAP_MY_COMMENTS, num=self.config.interact_comment_num
        )
        if not ok:
            self._fail("读取自己说说下的评论", result, now, report)
            return
        state = self.interact_state
        since = now - max(0.1, float(self.config.interact_days)) * 86400
        round_cap = self._cap(self.config.reply_cap_per_round)
        post_cap = self._cap(self.config.reply_cap_per_post)
        for comment in _as_items(result):
            if round_cap and report.replied >= round_cap:
                break
            tid = str(comment.get("tid") or "").strip()
            cid = str(comment.get("comment_id") or "").strip()
            if not tid or not cid or not self._is_other_comment(comment):
                continue  # 只回别人的评论
            created = _to_timestamp(comment.get("created_time"))
            if created is not None and created < since:
                continue  # 窗口外的老评论同样跳过
            key = f"{tid}:{cid}"
            if state.has(_STATE_REPLIES, key):
                continue
            if post_cap and state.count_replies(tid) >= post_cap:
                logger.debug("[qzone_post] 说说 %s 下已经回过 %s 条，跳过", tid, post_cap)
                continue
            if not self._daily_ok("reply", self.config.reply_daily_cap, now):
                logger.debug("[qzone_post] 今天回复的额度用完了")
                break
            text = await self._generate_interact_text(
                kind="reply",
                content=str(comment.get("content") or ""),
                nickname=str(comment.get("nickname") or ""),
                max_chars=self.config.reply_max_chars,
            )
            if not text:
                continue
            kwargs: dict[str, Any] = {
                "uin": _clean_uin(comment.get("feed_uin")),
                "tid": tid,
                "comment_id": cid,
                "content": text,
            }
            if comment.get("comment_uin"):
                # 显式带上被回复者，省掉适配器回读说说的那次请求
                kwargs["comment_uin"] = str(comment["comment_uin"])
            if comment.get("nickname"):
                kwargs["comment_nickname"] = str(comment["nickname"])
            ok, detail = await self._call_capability(CAP_REPLY, **kwargs)
            if not ok:
                self._fail(f"回复评论 {cid}", detail, now, report)
                return
            state.mark(_STATE_REPLIES, key, now)
            state.note_action("reply", now)
            report.replied += 1
            logger.info("[qzone_post] 回复了说说 %s 下的评论 %s：%s", tid, cid, text)

    def _fail(self, action: str, detail: Any, now: float, report: InteractReport) -> None:
        logger.warning("[qzone_post] %s失败：%s", action, detail)
        self._back_off(now)
        report.stopped = True

    def _is_other_comment(self, comment: dict[str, Any]) -> bool:
        """只回别人发的评论：跳过自己的、以及本说说作者发的。"""
        author = _clean_uin(comment.get("comment_uin"))
        if not author:
            return False
        feed_uin = _clean_uin(comment.get("feed_uin"))
        if feed_uin and not self._self_uin:
            # my_comments 读的是自己的说说，说说主人就是自己
            self._self_uin = feed_uin
        if author == (self._self_uin or feed_uin):
            return False
        return True

    @staticmethod
    def _cap(value: int) -> int:
        """每轮上限；0 表示不限。"""
        return max(0, int(value))

    def _daily_ok(self, kind: str, cap: int, now: float) -> bool:
        """每日额度是否还有；``cap`` 为 0 表示不限。"""
        cap = max(0, int(cap))
        return cap == 0 or self.interact_state.daily_count(kind, now) < cap

    async def _call_capability(self, name: str, **kwargs: Any) -> tuple[bool, Any]:
        """经能力入口调用平台能力；读能力尽量拿回结构化结果。

        插件不碰任何平台协议，只按名字调能力：

        - 写能力（点赞/评论/回复）统一走宿主给的 ``ctx.call_capability``，
          能力开关、失败原因都由核心入口裁决；
        - 读能力（``qzone.feeds`` / ``qzone.my_comments``）需要列表数据，而核心
          入口只回 ``(是否成功, 说明)``，所以先找声明了该能力且配置开关打开的
          适配器，调用它的 ``call_capability`` 拿原始返回值（开关判断与核心
          入口一致，不会绕过 ``adapters.<name>.capabilities``）；
        - 找不到这样的适配器时退回 ``ctx.call_capability``（例如测试里的假通道、
          或宿主自定义的能力通道直接返回数据）。

        两条路都失败时只返回 ``(False, 原因)``，绝不抛异常。
        """
        if name in (CAP_FEEDS, CAP_MY_COMMENTS):
            adapter = self._find_adapter(name)
            if adapter is not None:
                try:
                    return True, await adapter.call_capability(name, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 能力失败不能拖垮插件
                    logger.warning("[qzone_post] 调用能力 %s 失败：%s", name, exc)
                    return False, str(exc) or exc.__class__.__name__
        call = getattr(self.ctx, "call_capability", None)
        if not callable(call):
            return False, "宿主未就绪，无法调用平台能力"
        try:
            result = await call(name, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 能力失败不能拖垮插件
            logger.warning("[qzone_post] 调用能力 %s 异常：%s", name, exc)
            return False, str(exc) or exc.__class__.__name__
        if isinstance(result, tuple) and len(result) == 2:
            ok, detail = result
            if not ok:
                return False, detail
            if name in (CAP_FEEDS, CAP_MY_COMMENTS) and not isinstance(
                detail, (list, dict)
            ):
                # 读能力没拿到结构化结果：按空列表处理，不误判成写操作
                logger.debug("[qzone_post] 能力 %s 没有结构化结果", name)
                return True, []
            return True, detail
        return True, result

    def _find_adapter(self, name: str) -> Any | None:
        """声明了该能力、且配置开关打开的适配器；没有则 None。"""
        sources = (
            getattr(getattr(self.ctx, "runtime", None), "adapters", None),
            getattr(getattr(self.ctx, "bot", None), "adapters", None),
        )
        config = getattr(self.ctx, "config", None)
        for source in sources:
            if not isinstance(source, dict):
                continue
            for adapter in source.values():
                if name not in set(getattr(adapter, "capabilities", frozenset()) or ()):
                    continue
                key = (getattr(adapter, "capability_config_keys", None) or {}).get(name)
                if key and hasattr(config, "adapter_capability_enabled"):
                    default = (getattr(adapter, "capability_defaults", None) or {}).get(
                        name, True
                    )
                    if not config.adapter_capability_enabled(
                        adapter.name, key, default=default
                    ):
                        continue
                return adapter
        return None

    async def _generate_interact_text(
        self,
        *,
        kind: str,
        content: str,
        nickname: str = "",
        max_chars: int = 30,
    ) -> str:
        """让 LLM 按人格写一句短评/短回复；失败返回空串。"""
        llm = self.ctx.llm
        if llm is None:
            logger.warning("[qzone_post] 宿主还没就绪，拿不到 LLM")
            return ""
        persona = self._persona_text()
        if kind == "comment":
            task = (
                "刷空间看到朋友发的一条说说，想留一句评论。\n"
                f"说说内容：{content or '（只有图片，没写文字）'}\n"
            )
        else:
            who = nickname or "一个朋友"
            task = (
                f"你在自己空间发的说说下面，{who} 给你留了条评论：{content}\n"
                "想回一句。\n"
            )
        instruction = (
            f"{persona}\n\n{task}"
            "要求：\n"
            "- 用上面这个人的口吻，像真人随手回一句，口语、别端着\n"
            f"- 不超过 {max_chars} 字，一句话就够\n"
            "- 不提「我是机器人」「AI」，不解释、不加引号、不加话题标签\n"
            "- 只输出这一句话"
        )
        try:
            raw = await llm.complete(
                [{"role": "user", "content": instruction}], role="chat"
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败不拖垮插件
            logger.warning("[qzone_post] 生成互动文案失败：%s", exc)
            return ""
        text = clean_generated(raw, max_chars)
        if not text:
            logger.warning("[qzone_post] LLM 返回了空的互动文案")
        return text

    async def _interact_loop(self, stop_event: asyncio.Event) -> None:
        """后台互动巡检；停用后干净退出，单轮异常不影响下一轮。"""
        period = max(0.1, float(self.config.interact_interval_minutes) * 60)
        while not stop_event.is_set():
            try:
                if await self._wait_tick(stop_event, period):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[qzone_post] 互动巡检等待异常：%s", exc)
                await asyncio.sleep(min(period, 60.0))
                continue
            if self._should_exit(stop_event):
                return
            try:
                await self.run_interact()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不影响下一轮
                logger.warning("[qzone_post] 互动巡检失败：%s", exc)

    # ------------------------------------------------------------------
    # 内容生成
    # ------------------------------------------------------------------

    async def generate_post(self, *, topic: str | None = None) -> str:
        """按当前人格让核心 LLM 写一条短说说；失败/空内容返回空串。"""
        llm = self.ctx.llm
        if llm is None:
            logger.warning("[qzone_post] 宿主还没就绪，拿不到 LLM")
            return ""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._generation_prompt()},
        ]
        if topic:
            messages.append({"role": "user", "content": f"可以顺着最近这些聊天写，别照搬：\n{topic}"})
        messages.append({"role": "user", "content": "直接写说说正文，只输出正文。"})
        try:
            raw = await llm.complete(messages, role="chat")
        except Exception as exc:  # noqa: BLE001 - LLM 失败不拖垮插件
            logger.warning("[qzone_post] 生成说说失败：%s", exc)
            return ""
        content = clean_generated(raw, self.config.max_chars)
        if not content:
            logger.warning("[qzone_post] LLM 返回了空内容")
        return content

    async def decide_with_llm(self, *, now: float | None = None) -> tuple[bool, str]:
        """LLM 触发：让模型自己判断现在有没有值得发的事。"""
        llm = self.ctx.llm
        if llm is None:
            logger.warning("[qzone_post] 宿主还没就绪，拿不到 LLM")
            return False, ""
        topic = self._recent_topic(now=now)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._decision_prompt()},
        ]
        if topic:
            messages.append({"role": "user", "content": f"最近看到的事：\n{topic}"})
        messages.append(
            {
                "role": "user",
                "content": (
                    "只输出 JSON，不要解释。想发就输出 "
                    '{"post": true, "content": "说说正文"}；'
                    '不想发就输出 {"post": false, "content": ""}。'
                ),
            }
        )
        try:
            raw = await llm.complete(messages, role="chat", json_mode=True)
        except Exception as exc:  # noqa: BLE001 - LLM 失败不拖垮插件
            logger.warning("[qzone_post] LLM 判断失败：%s", exc)
            return False, ""
        return parse_llm_decision(raw, self.config.max_chars)

    def _generation_prompt(self) -> str:
        persona = self._persona_text()
        instruction = (
            "现在你要在 QQ 空间发一条说说，是发在自己空间里、所有人都能看到的动态，"
            "不是对着某个人聊天。\n"
            f"要求：\n"
            f"- 用上面这个人的口吻，口语、随手写，别端着\n"
            f"- 不超过 {self.config.max_chars} 字，越短越好\n"
            "- 不用话题标签，不要 #…#，不要 emoji 刷屏\n"
            "- 不解释、不总结、不加引号，只写正文\n"
            "- 不知道写什么就写点日常：天气、路上看到的、吃的、困、无聊，都行"
        )
        return f"{persona}\n\n{instruction}".strip()

    def _decision_prompt(self) -> str:
        persona = self._persona_text()
        instruction = (
            "你正在刷自己的 QQ 空间，想判断此刻有没有值得发一条说说的新鲜事。\n"
            "有就写一条很短的说说（用上面这个人的口吻，口语、随手，"
            f"不超过 {self.config.max_chars} 字，不加引号、不加话题标签）；"
            "没有值得发的事就明确说没有，不要为了发而硬写。\n"
            "只输出 JSON："
            '{"post": true/false, "content": "说说正文或空串"}。'
        )
        return f"{persona}\n\n{instruction}".strip()

    def _persona_text(self) -> str:
        runtime = getattr(self.ctx, "runtime", None)
        persona = getattr(runtime, "persona", None)
        if persona is None:
            persona = getattr(self.ctx.bot, "persona", None)
        prompt = getattr(persona, "system_prompt", None)
        if callable(prompt):
            try:
                return str(prompt()).strip()
            except Exception:  # noqa: BLE001
                pass
        return getattr(persona, "text", "") or ""

    # ------------------------------------------------------------------
    # 题材
    # ------------------------------------------------------------------

    def _recent_topic(self, *, now: float | None = None) -> str:
        """最近群聊优先，其次最近记忆；都没有返回空串（写日常向）。"""
        store = self.ctx.store
        now = time.time() if now is None else now
        since = now - self.config.topic_window_hours * 3600
        try:
            chats = store.list_chats(limit=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_post] 读取会话列表失败：%s", exc)
            chats = []
        for chat in chats:
            if chat.get("chat_type") != "group":
                continue
            if float(chat.get("last_ts") or 0) < since:
                continue
            try:
                messages = store.recent_messages(
                    chat["chat_id"],
                    limit=self.config.topic_limit,
                    since=since,
                    platform=chat.get("platform"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[qzone_post] 读取最近消息失败：%s", exc)
                continue
            lines = [
                f"{msg.sender_name or msg.sender_id}：{msg.text}"
                for msg in messages
                if msg.sender_id != _BOT_SENDER_ID and (msg.text or "").strip()
            ]
            if len(lines) >= 2:
                return "\n".join(lines[-self.config.topic_limit :])
        try:
            memories = store.list_memories(limit=6)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_post] 读取记忆失败：%s", exc)
            memories = []
        if memories:
            return "\n".join(f"- {memory.content}" for memory in memories)
        return ""


# ----------------------------------------------------------------------
# 互动用的小工具
# ----------------------------------------------------------------------


def _as_items(result: Any) -> list[dict[str, Any]]:
    """把能力返回值规整成 ``list[dict]``；其它类型一律当空列表。"""
    if isinstance(result, dict):
        for key in ("items", "feeds", "comments", "msglist", "list"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(result, (list, tuple)):
        return [item for item in result if isinstance(item, dict)]
    return []


def _to_timestamp(value: Any) -> float | None:
    """说说/评论时间：接受秒级时间戳或数字字符串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _clean_uin(value: Any) -> str:
    """QQ 号归一：去掉 ``o`` 前缀与空白。"""
    return str(value if value is not None else "").strip().lstrip("oO")


def load_config(ctx: Any) -> QzonePostConfig:
    """优先用 ``ctx.extras["qzone_post_config"]`` 覆盖，其次读插件目录 config.yaml。"""
    override = ctx.extras.get("qzone_post_config")
    if isinstance(override, dict):
        return config_from_dict(override)
    path = getattr(getattr(ctx, "plugin", None), "path", None)
    return read_config(path) if path is not None else QzonePostConfig()


def register(ctx: Any) -> None:
    """插件入口。"""
    config = load_config(ctx)
    plugin = QzonePostPlugin(ctx, config)
    # 便于测试和宿主排查；插件名在本目录里唯一
    ctx.extras["qzone_post"] = plugin

    async def handle_command(runtime: Any, event: Any, args: str) -> str:
        plugin.ensure_started()
        return await plugin.handle_command(runtime, event, args)

    @ctx.on_message
    async def draft_reply(runtime: Any, event: Any):
        # 草稿确认：主人回「发」才发布，回其他内容作废
        plugin.ensure_started()
        if await plugin.handle_draft_message(event):
            return False
        return None

    @ctx.on_message
    async def ensure_running(runtime: Any, event: Any):
        # 宿主是同步加载插件的，这里没有事件循环；第一条消息到来时懒启动后台任务
        plugin.ensure_started()
        return None

    @ctx.on_after_reply
    async def maybe_post_after_reply(runtime: Any, event: Any, reply: Any):
        # 机会事件：bot 在任意会话完成一轮回复后，按概率决定要不要发说说
        try:
            await plugin.on_opportunity()
        except Exception as exc:  # noqa: BLE001 - 自动路径异常不能带崩主循环
            logger.warning("[qzone_post] 机会触发异常：%s", exc)

    ctx.command(config.command, handle_command)
    # 宿主加载/重新启用插件时会跑 on_start，在事件循环里可直接拉起任务
    ctx.on_start(plugin.start)
    ctx.on_stop(plugin.stop)
