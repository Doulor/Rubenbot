#!/usr/bin/env python3
"""从 raw_messages SQLite 流水计算活人感指标。

脚本只依赖 Python 标准库，输出 JSON，便于在调参前后复跑比较。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

BOT_SENDER_ID = "__bot__"
_TONE_WORDS = ("啊", "吧", "呢", "哦", "呀", "诶", "欸", "哎", "唉", "呃", "嘛")
_SENTENCE_RE = re.compile(r"[^。！？!?…~～\n]+[。！？!?…~～]?|[^。！？!?…~～\n]+$")


@dataclass(frozen=True)
class Message:
    platform: str
    chat_id: str
    sender_id: str
    text: str
    ts: float
    row_id: int = 0


def load_messages(database: str | Path) -> list[Message]:
    """读取 raw_messages；保留 id 作为同一时间戳下的稳定排序键。"""
    connection = sqlite3.connect(str(database))
    try:
        rows = connection.execute(
            "SELECT id, platform, chat_id, sender_id, text, ts "
            "FROM raw_messages ORDER BY platform, chat_id, ts, id"
        ).fetchall()
    finally:
        connection.close()
    return [
        Message(
            platform=str(row[1]),
            chat_id=str(row[2]),
            sender_id=str(row[3]),
            text=str(row[4] or ""),
            ts=float(row[5]),
            row_id=int(row[0]),
        )
        for row in rows
    ]


def _sentence_lengths(text: str) -> list[int]:
    """按聊天句末标点/换行取非空句，避免空白影响 burstiness。"""
    return [len(part.strip()) for part in _SENTENCE_RE.findall(text) if part.strip()]


def _burstiness(messages: Sequence[Message]) -> float:
    lengths = [length for message in messages for length in _sentence_lengths(message.text)]
    if not lengths:
        return 0.0
    mean = sum(lengths) / len(lengths)
    if mean == 0:
        return 0.0
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    return math.sqrt(variance) / mean


def _message_metrics(messages: Sequence[Message]) -> dict[str, float | int]:
    count = len(messages)
    lengths = [len(message.text.strip()) for message in messages]
    tone_count = sum(
        1 for message in messages if any(word in message.text for word in _TONE_WORDS)
    )
    return {
        "messages": count,
        "average_chars": (sum(lengths) / count) if count else 0.0,
        "short_le_10_ratio": (
            sum(length <= 10 for length in lengths) / count if count else 0.0
        ),
        "average_commas": (
            sum(message.text.count("，") + message.text.count(",") for message in messages) / count
            if count
            else 0.0
        ),
        "tone_word_rate": tone_count / count if count else 0.0,
        "sentence_length_burstiness": _burstiness(messages),
    }


def _max_rolling_count(messages: Sequence[Message], seconds: float = 600.0) -> int:
    timestamps = sorted(message.ts for message in messages)
    left = 0
    maximum = 0
    for right, timestamp in enumerate(timestamps):
        while timestamp - timestamps[left] >= seconds:
            left += 1
        maximum = max(maximum, right - left + 1)
    return maximum


def _max_consecutive_bot(messages: Sequence[Message]) -> int:
    maximum = current = 0
    for message in sorted(messages, key=lambda item: (item.ts, item.row_id)):
        if message.sender_id == BOT_SENDER_ID:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _bot_density(messages: Sequence[Message]) -> dict[str, float | int]:
    bot = [message for message in messages if message.sender_id == BOT_SENDER_ID]
    if not bot:
        return {
            "bot_messages": 0,
            "bot_messages_per_10_minutes": 0.0,
            "bot_max_in_10_minute_window": 0,
            "bot_max_consecutive_messages": 0,
        }
    span = max(600.0, max(message.ts for message in messages) - min(message.ts for message in messages))
    return {
        "bot_messages": len(bot),
        "bot_messages_per_10_minutes": len(bot) * 600.0 / span,
        "bot_max_in_10_minute_window": _max_rolling_count(bot),
        "bot_max_consecutive_messages": _max_consecutive_bot(messages),
    }


def measure(messages: Iterable[Message]) -> dict[str, dict[str, object]]:
    """按平台和会话输出 bot/真人对比及 bot 发言密度。"""
    grouped: dict[tuple[str, str], list[Message]] = {}
    for message in messages:
        grouped.setdefault((message.platform, message.chat_id), []).append(message)
    result: dict[str, dict[str, object]] = {}
    for (platform, chat_id), session in sorted(grouped.items()):
        humans = [message for message in session if message.sender_id != BOT_SENDER_ID]
        bots = [message for message in session if message.sender_id == BOT_SENDER_ID]
        key = f"{platform}:{chat_id}"
        result[key] = {
            "platform": platform,
            "chat_id": chat_id,
            "bot": _message_metrics(bots),
            "human": _message_metrics(humans),
            "bot_density": _bot_density(session),
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="计算 RubenBot 活人感指标")
    parser.add_argument("database", type=Path, help="SQLite 数据库路径")
    args = parser.parse_args(argv)
    print(json.dumps(measure(load_messages(args.database)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
