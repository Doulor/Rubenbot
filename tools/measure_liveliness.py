#!/usr/bin/env python3
"""只读测量 raw_messages 或离线 JSON 中的真人常模。用法见 docs/真人常模.md。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any

BOT_SENDER_ID = "__bot__"
BURST_WINDOW_SECONDS = 4.0
VOCAB_WINDOW = 50
_TONE_WORDS = ("啊", "吧", "呢", "哦", "呀", "诶", "欸", "哎", "唉", "呃", "嘛")
_SENTENCE_RE = re.compile(r"[^。！？!?…~～\n]+[。！？!?…~～]?|[^。！？!?…~～\n]+$")
_TOKEN_RE = re.compile(r"([㐀-䶿一-鿿豈-﫿]+)|[A-Za-z0-9]+(?:[_'’-][A-Za-z0-9]+)*")

METRIC_DEFINITIONS = {
    "message_length": "去首尾空白后的字符数；p50/p90/p95 线性插值。",
    "burst_rate": "同一会话相邻、同一人、间隔最多 4 秒的连发组（至少两条）；组内消息数 / 连续流水样本消息数。",
    "tone_word_rate": "含至少一个「" + "/".join(_TONE_WORDS) + "」的消息占比。",
    "reply_interval": "同一会话换人发言时，后一句到前一句的秒数；不代表显式回复延迟。",
    "vocabulary_repetition": "中文用重叠双字（孤字保留），拉丁字母数字词转小写；同一人同会话此前 50 个 token 内见过的 token 占比。",
    "comparison": "每项用两组经验分布的最大累计概率差（KS 距离，0–1）；对是/否事件等于两侧发生率之差的绝对值。缺样不排名。",
}


@dataclass(frozen=True)
class Message:
    platform: str
    chat_id: str
    sender_id: str
    text: str
    ts: float
    row_id: int = 0
    continuous: bool = True  # 离散抽样片段之间没有已知先后邻接关系
    chat_type: str = "group"


def load_messages(database: str | Path) -> list[Message]:
    """以只读 URI 打开；即使路径含 ?/# 也不会误建文件或篡改打开模式。"""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        # 早期导出没有 chat_type；新库则必须区分同号群聊与私聊。
        columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_messages)")}
        kind = "chat_type" if "chat_type" in columns else "'group'"
        return [
            Message(str(platform), str(chat_id), str(sender_id), text or "", float(ts), row_id, chat_type=chat_type)
            for platform, chat_id, sender_id, text, ts, row_id, chat_type in conn.execute(
                f"SELECT platform, chat_id, sender_id, text, ts, id, {kind} FROM raw_messages"
            )
        ]


def load_corpus(paths: str | Path | Sequence[str | Path]) -> list[Message]:
    """平铺数组视为会话流水；bot 带上下文导出只取抽样目标，不重复计数。"""
    messages = []
    for path in [paths] if isinstance(paths, (str, Path)) else paths:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"语料必须是 JSON 数组：{path}")
        for idx, item in enumerate(data, start=1):
            try:
                sampled = "bot" in item
                row = item["bot"] if sampled else item
                ts = float(row["ts"])
                sender = BOT_SENDER_ID if sampled else row["sender_id"]
                if (
                    sender is None
                    or sender == ""
                    or not math.isfinite(ts)
                    or not isinstance(row["text"], str)
                ):
                    raise ValueError
                messages.append(
                    Message(
                        str(row.get("platform") or "corpus"),
                        str(row.get("chat_id") or path.stem),
                        str(sender),
                        row["text"],
                        ts,
                        int(row.get("id", idx)),
                        not sampled,
                        str(row.get("chat_type") or "group"),
                    )
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(
                    f"{path} 第 {idx} 项需含 sender_id、text 和有限的秒时间戳 ts"
                ) from exc
    return messages


def _percentiles(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    result = {}
    for q in (50, 90, 95):
        pos = (len(ordered) - 1) * q / 100
        lo = math.floor(pos)
        result[f"p{q}"] = (
            ordered[lo] + (ordered[math.ceil(pos)] - ordered[lo]) * (pos - lo) if ordered else None
        )
    return result


def _tokens(text: str) -> Iterable[str]:
    for match in _TOKEN_RE.finditer(text):
        run = match[0]
        if match[1] and len(run) > 1:
            yield from (run[i : i + 2] for i in range(len(run) - 1))
        else:
            yield run.lower()


@dataclass
class Samples:
    """保留分布而非先算会话均值，汇总时只合并样本，不跨会话造间隔。"""

    lengths: list[int] = field(default_factory=list)
    sentences: list[int] = field(default_factory=list)
    intervals: list[float] = field(default_factory=list)
    bursts: list[int] = field(default_factory=list)  # 含大小为 1 的非连发组
    tone: int = 0
    commas: int = 0
    tokens: int = 0
    repeated: int = 0

    def merge(self, other: Samples) -> None:
        self.lengths.extend(other.lengths)
        self.sentences.extend(other.sentences)
        self.intervals.extend(other.intervals)
        self.bursts.extend(other.bursts)
        self.tone += other.tone
        self.commas += other.commas
        self.tokens += other.tokens
        self.repeated += other.repeated

    def metrics(self) -> dict[str, Any]:
        n = len(self.lengths)
        mean = sum(self.sentences) / len(self.sentences) if self.sentences else 0
        burst_n = sum(self.bursts)
        singles = self.bursts.count(1)
        burst_rate = (burst_n - singles) / burst_n if burst_n else None
        return {
            "messages": n,
            "average_chars": sum(self.lengths) / n if n else 0.0,
            "short_le_10_ratio": sum(v <= 10 for v in self.lengths) / n if n else 0.0,
            "average_commas": self.commas / n if n else 0.0,
            "tone_word_rate": self.tone / n if n else 0.0,
            "sentence_length_burstiness": (
                math.sqrt(sum((v - mean) ** 2 for v in self.sentences) / len(self.sentences)) / mean
                if mean
                else 0.0
            ),
            "message_length_percentiles": _percentiles(self.lengths),
            "burst_rate": burst_rate,
            "burst": {
                "samples": burst_n,
                "burst_rate": burst_rate,
                "burst_groups": len(self.bursts) - singles,
                "max_burst_size": max(self.bursts, default=0),
            },
            "reply_interval_distribution": {
                "samples": len(self.intervals),
                "percentiles": _percentiles(self.intervals),
            },
            "vocabulary_repetition": {
                "tokens": self.tokens,
                "repeated_tokens": self.repeated,
                "repetition_rate": self.repeated / self.tokens if self.tokens else None,
            },
        }


def _session_samples(session: Sequence[Message]) -> tuple[Samples, Samples]:
    human, bot = Samples(), Samples()
    vocab: dict[str, deque[str]] = {}
    previous = None
    for msg in session:
        side = bot if msg.sender_id == BOT_SENDER_ID else human
        side.lengths.append(len(msg.text.strip()))
        side.sentences.extend(
            len(part.strip()) for part in _SENTENCE_RE.findall(msg.text) if part.strip()
        )
        side.tone += any(word in msg.text for word in _TONE_WORDS)
        side.commas += msg.text.count(",") + msg.text.count("，")
        if msg.sender_id not in vocab:
            vocab[msg.sender_id] = deque(maxlen=VOCAB_WINDOW)
        window = vocab[msg.sender_id]
        for token in _tokens(msg.text):
            side.tokens += 1
            side.repeated += token in window
            window.append(token)
        if not msg.continuous:
            previous = None
            continue
        if previous is not None and previous.sender_id != msg.sender_id:
            side.intervals.append(msg.ts - previous.ts)
        if (
            previous is not None
            and previous.sender_id == msg.sender_id
            and msg.ts - previous.ts <= BURST_WINDOW_SECONDS
        ):
            side.bursts[-1] += 1
        else:
            side.bursts.append(1)
        previous = msg
    return human, bot


def _bot_density(session: Sequence[Message]) -> dict[str, Any]:
    bots = [msg for msg in session if msg.sender_id == BOT_SENDER_ID]
    if any(not msg.continuous for msg in session):
        return {
            "bot_messages": len(bots),
            "bot_messages_per_10_minutes": None,
            "bot_max_in_10_minute_window": None,
            "bot_max_consecutive_messages": None,
        }
    left = maximum = consecutive = run = 0
    for right, msg in enumerate(bots):
        while msg.ts - bots[left].ts >= 600:
            left += 1
        maximum = max(maximum, right - left + 1)
    for msg in session:
        run = run + 1 if msg.sender_id == BOT_SENDER_ID else 0
        consecutive = max(consecutive, run)
    return {
        "bot_messages": len(bots),
        "bot_messages_per_10_minutes": len(bots) * 600 / max(600, session[-1].ts - session[0].ts),
        "bot_max_in_10_minute_window": maximum,
        "bot_max_consecutive_messages": consecutive,
    }


def _ks(a: Sequence[float], b: Sequence[float]) -> float | None:
    """经验 CDF 的最大垂直距离；相等取值同时推进。"""
    if not a or not b:
        return None
    counts_a, counts_b = Counter(a), Counter(b)
    seen_a = seen_b = maximum = 0
    for v in sorted(counts_a.keys() | counts_b.keys()):
        seen_a += counts_a[v]
        seen_b += counts_b[v]
        maximum = max(maximum, abs(seen_a * len(b) - seen_b * len(a)))
    return maximum / (len(a) * len(b))


def _describe(human: Samples, bot: Samples) -> dict[str, Any]:
    h, b = human.metrics(), bot.metrics()

    def gap(x: float | None, y: float | None) -> float | None:
        return abs(x - y) if x is not None and y is not None else None

    distances = {
        "消息长度": _ks(human.lengths, bot.lengths),
        "连发率": gap(h["burst_rate"], b["burst_rate"]),
        "语气词率": gap(h["tone_word_rate"], b["tone_word_rate"])
        if h["messages"] and b["messages"]
        else None,
        "回复间隔": _ks(human.intervals, bot.intervals),
        "词汇重复度": gap(
            h["vocabulary_repetition"]["repetition_rate"],
            b["vocabulary_repetition"]["repetition_rate"],
        ),
    }
    largest = max((v for v in distances.values() if v is not None), default=0)
    farthest = [
        k for k, v in distances.items() if largest and v is not None and math.isclose(largest, v)
    ]
    return {
        "human": h,
        "bot": b,
        "comparison": {
            "distances": distances,
            "farthest_metrics": farthest,
            "farthest_metric": "、".join(farthest) or None,
        },
    }


def build_report(messages: Iterable[Message]) -> dict[str, Any]:
    """系统/工具伪发送者以 __ 开头，不作真人发言。"""
    ordered = sorted(
        (m for m in messages if m.sender_id and (m.sender_id == BOT_SENDER_ID or not m.sender_id.startswith("__"))),
        key=lambda m: (m.platform, m.chat_type, m.chat_id, m.ts, m.row_id),
    )
    sessions = {}
    total_human, total_bot = Samples(), Samples()
    for (platform, chat_type, chat_id), rows in groupby(ordered, key=lambda m: (m.platform, m.chat_type, m.chat_id)):
        session = list(rows)
        human, bot = _session_samples(session)
        total_human.merge(human)
        total_bot.merge(bot)
        parts = (platform, chat_id) if chat_type == "group" else (platform, chat_type, chat_id)
        key = ":".join(part.replace("%", "%25").replace(":", "%3A") for part in parts)
        sessions[key] = dict(
            _describe(human, bot),
            platform=platform,
            chat_type=chat_type,
            chat_id=chat_id,
            bot_density=_bot_density(session),
        )
    summary = dict(_describe(total_human, total_bot), sessions=len(sessions))
    return {
        "sessions": sessions,
        "summary": summary,
        "conclusion": render_conclusion(summary),
        "metric_definitions": METRIC_DEFINITIONS,
    }


def measure(messages: Iterable[Message]) -> dict[str, dict[str, Any]]:
    """兼容原函数：按会话返回两侧指标和 bot 发言密度。"""
    return build_report(messages)["sessions"]


def render_conclusion(summary: dict[str, Any]) -> str:
    human, bot = summary["human"], summary["bot"]
    comparison = summary["comparison"]

    def fmt(value: float | None, spec: str) -> str:
        return format(value, spec) if value is not None else "无样本"

    def describe(side: dict[str, Any]) -> str:
        lengths = "/".join(fmt(v, ".1f") for v in side["message_length_percentiles"].values())
        interval = side["reply_interval_distribution"]
        seconds = fmt(interval["percentiles"]["p50"], ".1f")
        tone = side["tone_word_rate"] if side["messages"] else None
        return (
            f"{side['messages']} 条；长度 p50/p90/p95：{lengths} 字；连发率 {fmt(side['burst_rate'], '.1%')}；"
            f"语气词率 {fmt(tone, '.1%')}；换人间隔 p50（秒）{seconds}（{interval['samples']} 个间隔）；"
            f"词汇重复度 {fmt(side['vocabulary_repetition']['repetition_rate'], '.1%')}。"
        )

    if comparison["farthest_metrics"]:
        maximum = comparison["distances"][comparison["farthest_metrics"][0]]
        result = (
            f"可比较项中偏离真人分布最远：{comparison['farthest_metric']}（距离 {maximum:.3f}）。"
        )
    elif any(v is not None for v in comparison["distances"].values()):
        result = "可比较项的分布均相同，没有偏离最远项。"
    else:
        result = "缺少可比较的两侧样本，无法判断哪项偏离最远。"
    return "\n".join(
        [
            f"真人常模测量结论（{summary['sessions']} 个会话）",
            "真人：" + describe(human),
            "bot：" + describe(bot),
            result,
            "这是当前样本的描述性差异，不代表显著性或人设优劣。",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读测量真人与 bot 的发言分布")
    parser.add_argument("database", nargs="?", type=Path, help="SQLite 数据库路径")
    parser.add_argument("--corpus", nargs="+", type=Path, help="离线 JSON 语料路径")
    parser.add_argument(
        "--text", action="store_true", help="只打印一页中文结论（默认输出含结论的 JSON）"
    )
    args = parser.parse_args(argv)
    if bool(args.database) == bool(args.corpus):
        parser.error("请提供一个 SQLite database，或使用 --corpus 提供 JSON 语料")
    try:
        messages = load_corpus(args.corpus) if args.corpus else load_messages(args.database)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    report = build_report(messages)
    print(report["conclusion"] if args.text else json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
