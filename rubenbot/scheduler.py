"""轻量调度器：定时扫描 ``scheduled_jobs`` 表并执行到期任务。

设计刻意保持简单：

- 只支持**一次性时间点**，不引 cron 表达式、不引外部库；周期任务以后再说。
- 任务落在 SQLite（``scheduled_jobs``），进程重启后 pending 任务自然恢复。
- 每 ``scan_interval`` 秒扫一次；到点的任务交给 ``handler`` 执行，成功
  失败都标记 ``done``——发不出去就记日志，不重试轰炸。
- 只在 handler 返回之后才标记 done：进程若在执行途中被 kill，任务仍是
  ``pending``，下次启动会重新执行——宁可重复一次，也不默默吞掉。
- 过期超过 ``late_seconds`` 的任务仍然执行一次，但把 ``late=True`` 传给
  handler，让它自然地说一句「来晚了」，而不是默默吞掉。

所有时间都由调用方传入（``clock``），单测用假时钟直接调 :meth:`scan`，
不用真的 sleep。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .memory.store import MemoryStore

logger = logging.getLogger(__name__)

# 默认扫描间隔（秒）
DEFAULT_SCAN_INTERVAL = 10.0
# 到期超过这么久才执行的任务算「迟到」（秒）；进程重启恢复时用得上
DEFAULT_LATE_SECONDS = 3600.0

JobHandler = Callable[..., Awaitable[None]]


class Scheduler:
    """到期任务扫描循环。``handler(job, late=bool)`` 由 core 提供。"""

    def __init__(
        self,
        store: MemoryStore,
        handler: JobHandler,
        *,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        late_seconds: float = DEFAULT_LATE_SECONDS,
        clock: Callable[[], float] | None = None,
    ):
        self.store = store
        self.handler = handler
        self.scan_interval = max(0.1, float(scan_interval))
        self.late_seconds = max(0.0, float(late_seconds))
        self.clock = clock or time.time
        # 本进程内正在执行的任务：防止两轮扫描重叠时同一任务被执行两次
        self._inflight: set[int] = set()

    async def scan(self, now: float | None = None, *, limit: int = 20) -> int:
        """执行一轮到期任务，返回本轮处理的任务数。

        ``done`` 在 handler 返回之后才落库：进程若在执行途中被终止，任务
        仍是 ``pending``，下次启动会补发一次。handler 抛异常视为已处理
        （发送失败只记日志，不重试轰炸）。
        """
        now = self.clock() if now is None else now
        jobs = self.store.due_scheduled_jobs(now, limit=limit)
        handled = 0
        for job in jobs:
            if job.id in self._inflight:
                continue
            self._inflight.add(int(job.id))
            late = now - float(job.fire_at) > self.late_seconds
            handled += 1
            try:
                await self.handler(job, late=late)
            except asyncio.CancelledError:
                # 停机/取消：保持 pending，下次启动重新执行，别默默吞掉
                raise
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能停掉调度
                logger.warning("执行定时任务 #%s 失败：%s", job.id, exc)
            finally:
                self._inflight.discard(int(job.id))
            # 取消或已被别的进程处理的任务不会被改动（finish 只认 pending）
            self.store.finish_scheduled_job(job.id, status="done", now=now)
        return handled

    async def run(self, stop: asyncio.Event) -> None:
        """跟随主程序生命周期运行；``stop`` 置位后退出。"""
        while not stop.is_set():
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 扫描失败只记录，下一轮继续
                logger.warning("定时任务扫描异常：%s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.scan_interval)
            except asyncio.TimeoutError:
                pass
