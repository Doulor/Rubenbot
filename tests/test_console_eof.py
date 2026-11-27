"""ConsoleAdapter stdin EOF 回归：stdin 关闭时适配器挂起等待 stop()，不直接返回。

背景：只配 console 的 bot 在 nohup/systemd/docker 下启动时 stdin 从一开始就
关着，旧行为是 start() 读到 EOF 就返回，core 的 gather 认为适配器全部结束而
关停整个 bot（「WebUI 已启动」后半秒就退，全程 0 个 ERROR）。修法是 stdin
EOF 后挂起等待 stop()，WebUI 与其他适配器继续工作；stop() 仍能正常唤醒退出。

交互终端仍能正常收发由 test_stdin_lines_still_delivered 覆盖；reader（流）
路径 EOF 仍返回，由 tests/test_e2e.py 的 test_console_adapter_reads_lines 覆盖。
"""

import asyncio
import io

import pytest

from rubenbot.adapters.console import ConsoleAdapter


async def _wait_for(predicate, timeout=2.0):
    """轮询直到 predicate 为真，超时抛 AssertionError。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


def test_stdin_eof_parks_adapter_until_stop():
    """stdin 一开始就关着：start() 不返回，直到 stop() 才退出。"""
    adapter = ConsoleAdapter(stdin=io.StringIO(""), stdout=io.StringIO())

    async def scenario():
        task = asyncio.create_task(adapter.start(lambda event: asyncio.sleep(0)))
        await asyncio.sleep(0.5)
        assert not task.done(), "stdin EOF 后 start() 不应返回，否则 bot 会被 core 关停"
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()

    asyncio.run(scenario())


def test_stdin_lines_still_delivered_then_parks():
    """交互输入仍能正常收发；行读完、遇到 EOF 后挂起，不退出。"""
    stdin = io.StringIO("你好\n[群] 大家好\n")
    seen = []

    async def on_event(event):
        seen.append(event.text)

    adapter = ConsoleAdapter(stdin=stdin, stdout=io.StringIO())

    async def scenario():
        task = asyncio.create_task(adapter.start(on_event))
        await _wait_for(lambda: len(seen) >= 2)
        assert seen == ["你好", "大家好"]
        await asyncio.sleep(0.3)
        assert not task.done(), "行读完后遇到 EOF 应挂起，不应直接返回"
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert seen == ["你好", "大家好"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
