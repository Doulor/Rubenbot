"""轻量调度器：到期执行、重启恢复、迟到标记、取消不执行。

全部用假时钟直接调 ``Scheduler.scan``，不真的 sleep。
"""

from __future__ import annotations

import asyncio

import pytest

from rubenbot.memory.store import MemoryStore
from rubenbot.scheduler import Scheduler

NOW = 1_795_000_000.0


def add_job(store, *, fire_at: float, text: str = "吃药", kind: str = "reminder"):
    return store.add_scheduled_job(
        kind=kind,
        fire_at=fire_at,
        platform="recording",
        chat_id="u1",
        chat_type="private",
        payload={"text": text},
        created_by="recording:u1",
        now=NOW,
    )


def run_scan(scheduler: Scheduler, now: float) -> tuple[int, list[tuple[int, bool]]]:
    """跑一轮扫描并记录 handler 收到的 (任务 id, late)。"""
    seen: list[tuple[int, bool]] = []

    async def handler(job, *, late=False):
        seen.append((job.id, late))

    scheduler.handler = handler
    count = asyncio.run(scheduler.scan(now=now))
    return count, seen


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def test_due_job_runs_once_and_marked_done(store):
    job = add_job(store, fire_at=NOW + 60)
    scheduler = Scheduler(store, handler=None)

    count, seen = run_scan(scheduler, NOW + 30)
    assert count == 0 and seen == []

    count, seen = run_scan(scheduler, NOW + 61)
    assert count == 1 and seen == [(job.id, False)]
    assert store.get_scheduled_job(job.id).status == "done"

    # 再扫一次不会重复执行
    count, seen = run_scan(scheduler, NOW + 90)
    assert count == 0 and seen == []


def test_restart_recovers_pending_jobs(store):
    job = add_job(store, fire_at=NOW + 120)
    # 模拟进程重启：新的调度器实例从同一份 SQLite 恢复
    restarted = Scheduler(store, handler=None)
    # 重启时还没到点
    count, _ = run_scan(restarted, NOW + 60)
    assert count == 0
    assert store.get_scheduled_job(job.id).status == "pending"
    # 到点后照常执行
    count, seen = run_scan(restarted, NOW + 130)
    assert count == 1 and seen[0][0] == job.id


def test_overdue_job_runs_with_late_flag(store):
    """过期超过一小时：仍然执行一次，但 handler 知道它迟到了。"""
    job = add_job(store, fire_at=NOW - 2 * 3600)
    scheduler = Scheduler(store, handler=None, late_seconds=3600)
    count, seen = run_scan(scheduler, NOW)
    assert count == 1
    assert seen == [(job.id, True)]
    assert store.get_scheduled_job(job.id).status == "done"


def test_slightly_late_job_not_flagged(store):
    """只晚了几分钟不算迟到（进程刚重启、扫描间隔内都常见）。"""
    add_job(store, fire_at=NOW - 120)
    scheduler = Scheduler(store, handler=None, late_seconds=3600)
    _, seen = run_scan(scheduler, NOW)
    assert seen and seen[0][1] is False


def test_cancelled_job_does_not_run(store):
    job = add_job(store, fire_at=NOW - 10)
    assert store.finish_scheduled_job(job.id, status="cancelled", now=NOW - 5)
    scheduler = Scheduler(store, handler=None)
    count, seen = run_scan(scheduler, NOW)
    assert count == 0 and seen == []
    assert store.get_scheduled_job(job.id).status == "cancelled"


def test_handler_failure_does_not_stop_other_jobs(store):
    first = add_job(store, fire_at=NOW - 20, text="第一件")
    second = add_job(store, fire_at=NOW - 10, text="第二件")
    seen: list[int] = []

    async def handler(job, *, late=False):
        seen.append(job.id)
        if job.id == first.id:
            raise RuntimeError("发送失败")

    scheduler = Scheduler(store, handler=handler)
    count = asyncio.run(scheduler.scan(now=NOW))
    assert count == 2
    assert seen == [first.id, second.id]
    # 失败的任务也算处理过，不重试轰炸
    assert store.get_scheduled_job(first.id).status == "done"
    assert store.get_scheduled_job(second.id).status == "done"


def test_job_stays_pending_until_handler_finishes(store):
    """执行途中进程被终止：任务仍是 pending，下次启动补发一次。"""
    job = add_job(store, fire_at=NOW - 10)
    observed: list[str] = []

    async def handler(job, *, late=False):
        observed.append(store.get_scheduled_job(job.id).status)
        raise asyncio.CancelledError()

    scheduler = Scheduler(store, handler=handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.scan(now=NOW))
    assert observed == ["pending"]
    assert store.get_scheduled_job(job.id).status == "pending"
    # 重启后的新调度器能重新捡起它
    count, seen = run_scan(Scheduler(store, handler=None), NOW + 5)
    assert count == 1 and seen[0][0] == job.id


def test_overlapping_scan_does_not_run_same_job_twice(store):
    """handler 里再触发一轮扫描时，正在执行的任务不会被重复捡起。"""
    job = add_job(store, fire_at=NOW - 10)
    executions: list[int] = []
    scheduler: Scheduler

    async def handler(job, *, late=False):
        executions.append(job.id)
        # 模拟执行期间另一轮扫描（例如插件触发了 scan）
        inner = await scheduler.scan(now=NOW)
        assert inner == 0

    scheduler = Scheduler(store, handler=handler)
    count = asyncio.run(scheduler.scan(now=NOW))
    assert count == 1 and executions == [job.id]
    assert store.get_scheduled_job(job.id).status == "done"


def test_unknown_kind_is_left_to_core(store):
    """调度器不按 kind 过滤：任务类型由 core 的 handler 自己判断。"""
    add_job(store, fire_at=NOW - 10, kind="unknown")
    count, seen = run_scan(Scheduler(store, handler=None), NOW)
    assert count == 1 and seen[0][1] is False


def test_due_jobs_ordered_by_fire_at(store):
    late = add_job(store, fire_at=NOW - 10, text="晚点的")
    early = add_job(store, fire_at=NOW - 100, text="早该发的")
    scheduler = Scheduler(store, handler=None)
    _, seen = run_scan(scheduler, NOW)
    assert [job_id for job_id, _ in seen] == [early.id, late.id]


def test_scan_limit(store):
    for index in range(5):
        add_job(store, fire_at=NOW - index)
    scheduler = Scheduler(store, handler=None)
    count = asyncio.run(scheduler.scan(now=NOW, limit=2))
    assert count == 2
    assert store.count_pending_jobs(kind="reminder") == 3


def test_run_loop_stops(store):
    """run() 跟随 stop 事件退出；scan_interval 设很小让循环至少跑一轮。"""
    called = asyncio.Event()

    async def handler(job, *, late=False):
        called.set()

    add_job(store, fire_at=0)
    scheduler = Scheduler(store, handler=handler, scan_interval=0.05)

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run(stop))
        await asyncio.wait_for(called.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(main())
