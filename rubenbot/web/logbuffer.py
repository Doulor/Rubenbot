"""进程内日志环形缓冲，供 WebUI 的「日志」页读取。

只在内存里保留最近若干条记录，不落盘；重启后清空。主程序
``setup_logging`` 会挂上这个 handler，WebUI 通过 ``records()`` 读取。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

DEFAULT_CAPACITY = 1000

_lock = threading.Lock()
_buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=DEFAULT_CAPACITY)
_handler: "RingBufferHandler | None" = None


class RingBufferHandler(logging.Handler):
    """把日志记录压进共享环形缓冲。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            item = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
        except Exception:  # noqa: BLE001 - 日志处理器自己不能抛异常
            return
        with _lock:
            _buffer.append(item)


def install(level: int = logging.NOTSET) -> RingBufferHandler:
    """安装（或复用）环形缓冲 handler；重复调用不会重复挂载。"""
    global _handler
    with _lock:
        if _handler is not None:
            return _handler
    handler = RingBufferHandler(level)
    logging.getLogger().addHandler(handler)
    _handler = handler
    return handler


def records(
    *,
    limit: int = 200,
    level: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """读取最近日志；``level`` 为最低级别，``query`` 为子串过滤。"""
    min_level = logging.getLevelName((level or "").upper()) if level else None
    if not isinstance(min_level, int):
        min_level = None
    keyword = (query or "").strip().lower()
    with _lock:
        items = list(_buffer)
    if min_level is not None:
        items = [
            item
            for item in items
            if logging.getLevelName(item["level"]) >= min_level
        ]
    if keyword:
        items = [
            item
            for item in items
            if keyword in item["message"].lower() or keyword in item["logger"].lower()
        ]
    return items[-max(1, limit):][::-1]


def clear() -> None:
    with _lock:
        _buffer.clear()


def size() -> int:
    with _lock:
        return len(_buffer)


def now() -> float:
    """便于测试替换的时钟。"""
    return time.time()
