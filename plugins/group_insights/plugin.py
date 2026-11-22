"""群发言统计与群聊总结插件。

两件事，都只读现有的消息流水（``raw_messages``），不建新表、不改核心存储层：

1. **发言统计**：群里有人 @bot / 叫 bot 名字问「水群榜 / 发言统计 / 今天谁话最多」
   这类问题时，按消息流水当场聚合指定窗口内的发言 Top5（昵称 + 条数 + 占比），
   附一行点评。数据现算，只认真实群友，bot 自己与系统条目不计。
2. **群聊总结**：命中「总结一下 / 今天都聊了啥 / 补课」等触发词时，取该群
   最近 ``summary_max_messages`` 条与今日全部消息中较少的一批，交给核心 LLM
   的 ``summary`` 角色生成「分话题列点 + 关键原话」的补课材料。

节制与边界：

- 只做群聊；私聊里一律不响应。
- 同群总结冷却默认 10 分钟。冷却内重复触发回「刚总结过」+ 上次结果；结果缓存
  落在插件目录下的 ``data/summary_cache.json``，重启后当日缓存仍能复用（并据此
  恢复冷却计时）。
- 统计与总结只用文本投影：图片/表情按 ``[图片]`` / ``[表情]`` 占位计入条数，
  与 ``raw_messages`` 的现有存法一致。
- 所有异常都在插件内部吞掉并记日志，绝不影响主回复流程。

配置读插件目录下的 ``config.yaml``（其次 ``config.example.yaml``），字段见
``manifest.json`` 的 ``config_schema``；测试/嵌入式场景可通过
``ctx.extras["group_insights_config"]`` 直接传同结构的 dict。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from rubenbot.adapters.base import Segment
from rubenbot.decision import is_reply_to_bot
from rubenbot.memory.store import BOT_SENDER_ID, SYSTEM_SENDER_PREFIX, RawMessage

logger = logging.getLogger(__name__)

# 发言榜固定取前几名
TOP_N = 5

# 触发词默认表（可在 config.yaml 里整组替换）
DEFAULT_STATS_TRIGGERS: tuple[str, ...] = (
    "水群榜",
    "水群排行",
    "发言统计",
    "发言榜",
    "发言排行",
    "活跃榜",
    "活跃排行",
    "谁话最多",
    "谁发言最多",
    "谁最话痨",
    "话最多",
)
DEFAULT_SUMMARY_TRIGGERS: tuple[str, ...] = (
    "总结一下",
    "总结下",
    "群总结",
    "今天都聊了啥",
    "今天聊了啥",
    "都聊了啥",
    "聊了啥",
    "补课",
)

# 问法里出现这些词就按近 7 天算；出现「今天」类词则压回 1 天
WEEK_HINTS: tuple[str, ...] = (
    "近7天", "近七天", "最近7天", "最近七天", "这周", "本周", "一周", "七天",
)
TODAY_HINTS: tuple[str, ...] = ("今天", "今日", "当天", "今儿")

# 喂给总结模型的单条原文最多保留多少字（与滚动摘要保持一致的口径）
MAX_MESSAGE_CHARS = 300

# 统计口径下一次最多捞多少条流水（每个会话的 raw 流水本身也有保留上限）
MAX_SCAN_MESSAGES = 2000

# 总结回复的抬头：告诉群友这次覆盖了多少条、什么时段
SUMMARY_HEADER = "🧾 总结了群里最近 {count} 条消息（{start}–{end}）："

SUMMARY_SYSTEM_PROMPT = """你是群聊记录员。下面是一段群聊记录，请帮群友快速「补课」。

输出要求：
- 按话题分点，每个话题一行：谁在聊什么、聊到什么结论（没结论就写「还没定」）。
- 话题控制在 3~6 个，按重要性排序；同义话题合并，不要按时间流水账复述。
- 最多引用 2 条关键原话，用「」括起来并注明是谁说的；没有值得引的就不要引。
- 只依据给出的记录，不要补充记录里没有的人和事，不要下记录之外的结论。
- 直接输出话题列表，不要前言、不要总结陈词、不要代码块。"""


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InsightsConfig:
    """归一化后的插件配置。"""

    # 统计窗口默认几天（问法区分不出「近 7 天」时用它）
    stats_window_days: int = 1
    stats_triggers: tuple[str, ...] = DEFAULT_STATS_TRIGGERS
    summary_triggers: tuple[str, ...] = DEFAULT_SUMMARY_TRIGGERS
    # 单次总结最多取多少条（今日消息更少时按今日全部）
    summary_max_messages: int = 200
    summary_cooldown_minutes: float = 10.0
    # 是否必须 @bot / 叫名字 / 回复 bot 才触发（默认要，避免群友自言自语被接话）
    require_addressing: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "InsightsConfig":
        raw = data if isinstance(data, Mapping) else {}
        return cls(
            stats_window_days=_as_days(raw.get("stats_window_days"), 1),
            stats_triggers=_as_triggers(raw.get("stats_triggers"), DEFAULT_STATS_TRIGGERS),
            summary_triggers=_as_triggers(raw.get("summary_triggers"), DEFAULT_SUMMARY_TRIGGERS),
            summary_max_messages=_as_positive_int(raw.get("summary_max_messages"), 200),
            summary_cooldown_minutes=_as_non_negative(
                raw.get("summary_cooldown_minutes"), 10.0
            ),
            require_addressing=_as_bool(raw.get("require_addressing"), True),
        )


def load_config(path: str | Path | None = None) -> InsightsConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；没有就用默认值。"""
    candidates: list[Path] = []
    if path is not None:
        here = Path(path)
        if here.is_file():
            candidates = [here]
        else:
            candidates = [here / "config.yaml", here / "config.example.yaml"]
    else:
        here = Path(__file__).parent
        candidates = [here / "config.yaml", here / "config.example.yaml"]
    for file in candidates:
        if not file.exists():
            continue
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("[group_insights] 读取配置 %s 失败：%s", file, exc)
            return InsightsConfig()
        return InsightsConfig.from_mapping(data)
    return InsightsConfig()


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return number if number > 0 else default


def _as_days(value: Any, default: int) -> int:
    """统计窗口：只接受 1~30 天的整数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return number if 1 <= number <= 30 else default


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def _as_triggers(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """未配置用默认表；显式写空列表表示对应功能关闭。"""
    if value is None:
        return default
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(item).strip() for item in value if str(item or "").strip())


# ----------------------------------------------------------------------
# 纯函数：时间、触发识别、聚合与拼装（便于单测）
# ----------------------------------------------------------------------


def local_day_start(ts: float) -> float:
    """某时刻所在自然日的本地零点时间戳。"""
    local = time.localtime(ts)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def window_start(now: float, days: int) -> float:
    """统计窗口起点：含今天在内、往前数 ``days`` 个自然日的零点。

    按日历日回推而不是硬减 86400，跨夏令时也不会把窗口错开一小时。
    """
    local = time.localtime(now)
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday - (max(1, int(days)) - 1), 0, 0, 0, 0, 0, -1)
    )


def stats_window_days(text: str, default: int) -> int:
    """按问法判断统计窗口：明确说「近 7 天 / 这周」用 7 天，说「今天」用 1 天。"""
    body = (text or "").strip()
    if any(hint in body for hint in WEEK_HINTS):
        return 7
    if any(hint in body for hint in TODAY_HINTS):
        return 1
    return max(1, int(default))


def detect_intent(text: str, config: InsightsConfig) -> tuple[str, int] | None:
    """识别触发意图，返回 ``(kind, window_days)``；认不出返回 None。

    统计优先于总结（「总结一下今天的发言统计」这类问法按统计处理）。
    """
    body = (text or "").strip()
    if not body:
        return None
    lowered = body.lower()
    if any(trigger.lower() in lowered for trigger in config.stats_triggers):
        return ("stats", stats_window_days(body, config.stats_window_days))
    if any(trigger.lower() in lowered for trigger in config.summary_triggers):
        return ("summary", 1)
    return None


def mentions_any(text: str, names: Iterable[str]) -> bool:
    """文本里是否点名了 ``names`` 里的任意一个（英文不区分大小写）。"""
    body = (text or "").lower()
    return any(name and name.lower() in body for name in names)


@dataclass
class SpeakerStat:
    """一个发言者的聚合结果。"""

    sender_id: str
    name: str
    count: int
    share: float


@dataclass
class StatsResult:
    """一次发言统计的聚合结果。"""

    total: int
    speakers: list[SpeakerStat] = field(default_factory=list)
    # 参与统计的总人数（speakers 只保留 Top N，人数要另外记，否则会少报）
    speaker_count: int = 0

    @property
    def top(self) -> list[SpeakerStat]:
        return self.speakers[:TOP_N]


def usable_message(message: RawMessage) -> bool:
    """是否是参与统计/总结的真实消息（排除 bot 自己与系统条目）。"""
    sender = str(message.sender_id or "")
    return bool(sender) and sender != BOT_SENDER_ID and not sender.startswith(
        SYSTEM_SENDER_PREFIX
    )


def aggregate_speakers(messages: Sequence[RawMessage], *, top_n: int = TOP_N) -> StatsResult:
    """按发送者聚合条数，返回 Top N 与占比。

    同一个人以最近一条消息里的昵称为准（改名后显示新名字）；昵称为空时退回
    sender_id。bot 自己与系统条目不计入总数。
    """
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    order: dict[str, int] = {}
    for index, message in enumerate(messages):
        if not usable_message(message):
            continue
        sender = str(message.sender_id)
        counts[sender] = counts.get(sender, 0) + 1
        name = (message.sender_name or "").strip()
        if name:
            names[sender] = name
        order.setdefault(sender, index)
    total = sum(counts.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], order.get(item[0], 0)))
    speakers = [
        SpeakerStat(
            sender_id=sender,
            name=names.get(sender) or sender,
            count=count,
            share=(count / total) if total else 0.0,
        )
        for sender, count in ranked[: max(1, top_n)]
    ]
    return StatsResult(total=total, speakers=speakers, speaker_count=len(counts))


def comment_line(result: StatsResult) -> str:
    """一句话点评（不超过一行），全部由数据决定，不调模型。"""
    top = result.top
    if not top:
        return ""
    leader = top[0]
    if leader.share >= 0.5:
        return f"点评：{leader.name} 一个人承包了半壁江山。"
    if len(top) >= 2 and top[1].share >= leader.share * 0.8:
        return f"点评：{leader.name} 和 {top[1].name} 咬得很紧。"
    return f"点评：{leader.name} 话最多。"


def format_stats(result: StatsResult, *, window_days: int) -> str:
    """把聚合结果拼成回复文本：Top5 + 一句话点评。"""
    label = "今日" if window_days <= 1 else f"近 {window_days} 天"
    if result.total <= 0 or not result.top:
        return f"📊 {label}发言榜：群里还没人说话，先放个空榜。"
    lines = [f"📊 {label}发言榜 · 共 {result.total} 条 / {result.speaker_count} 人"]
    for index, speaker in enumerate(result.top, start=1):
        share = f"{speaker.share * 100:.1f}%"
        lines.append(f"{index}. {speaker.name} — {speaker.count} 条（{share}）")
    comment = comment_line(result)
    if comment:
        lines.append(comment)
    return "\n".join(lines)


def select_summary_messages(
    messages: Sequence[RawMessage], *, max_messages: int
) -> list[RawMessage]:
    """从给定消息里取最近 ``max_messages`` 条（调用方已按「今日」筛过流水）。

    今日消息不足 ``max_messages`` 条时就是今日全部，天然满足「取较少者」。
    """
    # bot 自己说的话保留（总结时能看出它接了哪些话），系统条目（``__`` 开头）剔除
    keep = [
        message
        for message in messages
        if message.sender_id == BOT_SENDER_ID
        or (message.sender_id and not message.sender_id.startswith(SYSTEM_SENDER_PREFIX))
    ]
    limit = max(1, int(max_messages))
    return keep[-limit:]


def format_material(messages: Sequence[RawMessage]) -> str:
    """把待总结的消息渲染成「时间 + 昵称 + 正文」的行，供模型阅读。"""
    lines: list[str] = []
    for message in messages:
        name = "我" if message.sender_id == BOT_SENDER_ID else (
            (message.sender_name or "").strip() or message.sender_id
        )
        clock = time.strftime("%H:%M", time.localtime(message.ts))
        text = " ".join((message.text or "").split())
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS].rstrip() + "…"
        lines.append(f"[{clock}] {name}：{text}")
    return "\n".join(lines)


def build_summary_prompt(messages: Sequence[RawMessage]) -> list[dict[str, str]]:
    """组装喂给 summary 角色模型的 messages。"""
    material = format_material(messages)
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": f"群聊记录（按时间正序）：\n{material}"},
    ]


def format_summary_reply(messages: Sequence[RawMessage], summary: str) -> str:
    """给模型输出加上「总结了几条、覆盖什么时段」的抬头。"""
    body = (summary or "").strip()
    if not body:
        return ""
    start = time.strftime("%H:%M", time.localtime(messages[0].ts))
    end = time.strftime("%H:%M", time.localtime(messages[-1].ts))
    header = SUMMARY_HEADER.format(count=len(messages), start=start, end=end)
    return f"{header}\n{body}"


# ----------------------------------------------------------------------
# 总结缓存
# ----------------------------------------------------------------------


class SummaryCache:
    """按会话缓存最近一次总结；只复用当日缓存，落插件 data 目录。"""

    def __init__(self, path: Path | None):
        self.path = path
        self._entries: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("[group_insights] 读取总结缓存失败：%s", exc)
                data = {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict) and isinstance(value.get("text"), str):
                        entries[str(key)] = value
        self._entries = entries
        return entries

    def get(self, key: str, *, now: float) -> dict[str, Any] | None:
        """取当日缓存；跨天（按本地自然日）视为无效。"""
        entry = self._load().get(key)
        if not entry:
            return None
        try:
            ts = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            return None
        if local_day_start(ts) != local_day_start(now):
            return None
        return entry

    def last_ts(self) -> dict[str, float]:
        """所有缓存条目的时间戳，用于重启后恢复冷却计时。"""
        result: dict[str, float] = {}
        for key, entry in self._load().items():
            try:
                result[key] = float(entry.get("ts") or 0.0)
            except (TypeError, ValueError):
                continue
        return result

    def put(self, key: str, text: str, *, now: float) -> None:
        entry = {"ts": now, "text": text}
        self._load()[key] = entry
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._entries or {}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("[group_insights] 保存总结缓存失败：%s", exc)


# ----------------------------------------------------------------------
# 插件主体
# ----------------------------------------------------------------------


class GroupInsights:
    """群发言统计与群聊总结。"""

    def __init__(
        self,
        ctx: Any,
        config: InsightsConfig | None = None,
        *,
        clock=time.time,
        cache_path: Path | None = None,
    ):
        self.ctx = ctx
        self.config = config or load_config(_plugin_path(ctx))
        self._clock = clock
        if cache_path is None:
            base = _plugin_path(ctx)
            cache_path = Path(base) / "data" / "summary_cache.json" if base else None
        self.cache = SummaryCache(cache_path)
        # 同群最近一次总结时间（内存状态；初次用当日缓存回填，重启后冷却不丢）
        self._last_summary: dict[str, float] = dict(self.cache.last_ts())
        # 同一会话串行生成总结：两个触发同时进来时第二个会看到更新后的冷却
        self._summary_locks: dict[str, asyncio.Lock] = {}

    # ---- 钩子入口 ----

    async def on_message(self, runtime: Any, event: Any) -> Any:
        """消息钩子：命中触发词就自己处理并拦截消息（返回 False）。"""
        try:
            return await self._handle(runtime, event)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主循环
            logger.warning("[group_insights] 处理消息异常：%s", exc)
            return None

    async def _handle(self, runtime: Any, event: Any) -> Any:
        if getattr(event, "is_private", False):
            return None
        sender = str(getattr(event, "sender_id", "") or "")
        if not sender or sender == BOT_SENDER_ID or sender.startswith(SYSTEM_SENDER_PREFIX):
            return None
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return None
        intent = detect_intent(text, self.config)
        if intent is None:
            return None
        if self.config.require_addressing and not self._addressed(event, text):
            return None
        kind, window_days = intent
        if kind == "stats":
            reply = self.stats_reply(event, window_days=window_days)
        else:
            reply = await self.summary_reply(event)
        if not reply:
            return None
        if not await self._send(runtime, event, reply):
            # 发不出去就别拦消息，让普通回复通道兜底
            return None
        return False

    def _addressed(self, event: Any, text: str) -> bool:
        """是否 @bot、回复 bot，或点名了 bot。"""
        if getattr(event, "mentions_bot", False):
            return True
        try:
            if is_reply_to_bot(event):
                return True
        except Exception:  # noqa: BLE001 - 判断失败按没回复到 bot 处理
            pass
        return mentions_any(text, self._bot_names())

    def _bot_names(self) -> list[str]:
        config = self._config()
        if config is None:
            return []
        aliases = list(getattr(config.decision, "aliases", None) or [])
        name = getattr(config.chat, "bot_name", "") if config is not None else ""
        if name and name not in aliases:
            aliases.append(name)
        return aliases

    def _config(self) -> Any:
        """优先用宿主当前的配置（热更新后仍是最新），拿不到再退回 ctx.config。"""
        bot = getattr(self.ctx, "bot", None)
        return getattr(bot, "config", None) or getattr(self.ctx, "config", None)

    # ---- 取数 ----

    def _fetch(self, event: Any, *, since: float, limit: int) -> list[RawMessage]:
        """按会话读消息流水（只读接口，不建表）。"""
        store = getattr(self.ctx, "store", None)
        if store is None:
            return []
        try:
            return list(
                store.recent_messages(
                    event.chat_id,
                    limit=limit,
                    since=since,
                    platform=getattr(event, "platform", None),
                    chat_type="group",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 读失败按空数据处理
            logger.warning("[group_insights] 读取消息流水失败：%s", exc)
            return []

    # ---- 发言统计 ----

    def stats_reply(self, event: Any, *, window_days: int) -> str:
        now = self._clock()
        since = window_start(now, window_days)
        messages = self._fetch(event, since=since, limit=MAX_SCAN_MESSAGES)
        result = aggregate_speakers(messages)
        return format_stats(result, window_days=window_days)

    # ---- 群聊总结 ----

    def _chat_key(self, event: Any) -> str:
        return f"{getattr(event, 'platform', '')}:{getattr(event, 'chat_id', '')}"

    def cooldown_remaining(self, key: str, *, now: float | None = None) -> float:
        """同群总结剩余的冷却秒数；0 表示可以总结。"""
        now = self._clock() if now is None else now
        last = self._last_summary.get(key)
        if last is None:
            return 0.0
        remain = self.config.summary_cooldown_minutes * 60 - (now - last)
        return max(0.0, remain)

    async def summary_reply(self, event: Any) -> str:
        key = self._chat_key(event)
        async with self._summary_locks.setdefault(key, asyncio.Lock()):
            return await self._summary_reply_locked(key, event)

    async def _summary_reply_locked(self, key: str, event: Any) -> str:
        now = self._clock()
        remaining = self.cooldown_remaining(key, now=now)
        if remaining > 0:
            entry = self.cache.get(key, now=now)
            elapsed = max(0, int((now - self._last_summary.get(key, now)) // 60))
            ago = "刚刚" if elapsed <= 0 else f"约 {elapsed} 分钟前"
            if entry:
                return f"刚总结过（{ago}），这是上次的结果：\n\n{entry['text']}"
            return f"刚总结过（{ago}），等会儿再来吧。"

        today = local_day_start(now)
        scan_limit = max(MAX_SCAN_MESSAGES, self.config.summary_max_messages)
        messages = self._fetch(event, since=today, limit=scan_limit)
        selected = select_summary_messages(
            messages, max_messages=self.config.summary_max_messages
        )
        if not selected:
            return "今天群里还没什么消息，暂时没什么好总结的。"
        if len(selected) < 2:
            return "今天群里才聊了一句，还不够总结，晚点再来？"

        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            logger.warning("[group_insights] 宿主还没就绪，拿不到 LLM")
            return "模型还没配好，暂时总结不了。"

        messages_for_llm = build_summary_prompt(selected)
        try:
            raw = await llm.complete(messages_for_llm, role="summary", task="group_summary")
        except Exception as exc:  # noqa: BLE001 - 模型失败只回复原因
            logger.warning("[group_insights] 生成总结失败：%s", exc)
            return "总结的时候模型没回上来，过会儿再试一次吧。"
        reply = format_summary_reply(selected, raw)
        if not reply:
            logger.warning("[group_insights] 模型返回了空的总结")
            return "模型这次没总结出内容，过会儿再试一次吧。"

        self.cache.put(key, reply, now=now)
        self._last_summary[key] = now
        return reply

    # ---- 发送 ----

    async def _send(self, runtime: Any, event: Any, text: str) -> bool:
        """发一条消息；返回是否发成功（失败只记日志）。"""
        adapters = getattr(runtime, "adapters", None)
        if not isinstance(adapters, dict) or not adapters:
            bot = getattr(self.ctx, "bot", None)
            adapters = getattr(bot, "adapters", None)
        adapter = (adapters or {}).get(getattr(event, "platform", ""))
        if adapter is None:
            logger.debug("[group_insights] 没有平台 %s 的适配器，发不出去", getattr(event, "platform", ""))
            return False
        try:
            await adapter.send(
                event.chat_id,
                [Segment.text(text)],
                chat_type=getattr(event, "chat_type", "group"),
            )
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮主循环
            logger.warning("[group_insights] 发送失败：%s", exc)
            return False
        return True


def _plugin_path(ctx: Any) -> Path | None:
    """插件目录（缓存落它的 data 子目录）；拿不到就只留内存缓存。"""
    path = getattr(getattr(ctx, "plugin", None), "path", None)
    return Path(path) if path is not None else None


def register(ctx: Any) -> None:
    """插件入口。"""
    override = ctx.extras.get("group_insights_config")
    if isinstance(override, Mapping):
        config = InsightsConfig.from_mapping(override)
    else:
        config = load_config(_plugin_path(ctx))
    insights = GroupInsights(ctx, config)
    # 便于测试和宿主排查
    ctx.extras["group_insights"] = insights

    @ctx.on_message
    async def handle(runtime: Any, event: Any):
        return await insights.on_message(runtime, event)

    logger.debug(
        "[group_insights] 已注册（窗口默认 %s 天，总结上限 %s 条，冷却 %s 分钟）",
        config.stats_window_days,
        config.summary_max_messages,
        config.summary_cooldown_minutes,
    )
