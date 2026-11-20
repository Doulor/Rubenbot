"""MCP 真实 stdio 会话：调用边界、目录变更和断线恢复。"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys

from rubenbot import mcp_client
from rubenbot.config import MCPConfig, MCPServerConfig
from rubenbot.mcp_client import MCPManager

ECHO = "mcp__demo__echo"
FAKE = Path(__file__).with_name("fixtures") / "mcp_echo.py"


@asynccontextmanager
async def connected(*, env=None, **limits):
    config = MCPServerConfig(name="demo", command=sys.executable, args=[str(FAKE)], env=env or {})
    manager = MCPManager(MCPConfig(servers=[config], **limits))
    try:
        await manager.start()
        assert manager.status()[0]["state"] == "connected"
        yield manager
    finally:
        await manager.stop()


def test_only_allowlisted_environment_reaches_stdio(monkeypatch):
    for key in ["HOST_SECRET", "USER", "LOGNAME", "SHELL", "TERM"]:
        monkeypatch.setenv(key, "host-only")

    async def run():
        async with connected(env={"APP_TOKEN": "explicit"}) as manager:
            env = json.loads(await manager.call(ECHO, {"text": "环境"}))
            assert env["APP_TOKEN"] == "explicit"
            assert all(env[k] == os.environ[k] for k in ("HOME", "PATH"))
            assert not {"HOST_SECRET", "USER", "LOGNAME", "SHELL", "TERM"}.intersection(env)
    asyncio.run(run())


def test_timeout_and_tool_error_are_audited_without_payload(caplog):
    caplog.set_level(logging.INFO, logger="rubenbot.mcp_client")

    async def run():
        async with connected(call_timeout=0.1) as manager:
            assert "超时" in await manager.call(ECHO, {"text": "超时"})
            assert "工具错误：失败" == await manager.call(ECHO, {"text": "失败"})
            assert "工具错误" in await manager.call(ECHO, {"typo": True})
            assert "工具错误" in await manager.call("mcp__other__hidden", {})
            assert await manager.call(ECHO, {"text": "CONFIDENTIAL"}) == "CONFIDENTIAL"
    asyncio.run(run())
    logs = [r.getMessage() for r in caplog.records if "elapsed_ms=" in r.getMessage()]
    assert len(logs) == 5 and sum("success=False" in s for s in logs) == 4
    assert "server='demo' tool='echo'" in logs[-1] and "success=True" in logs[-1]
    assert "CONFIDENTIAL" not in str(logs)


def test_byte_limit_respects_multibyte_text():
    async def run():
        async with connected(max_result_bytes=32) as manager:
            text = await manager.call(ECHO, {"text": "测试" * 12})
            assert text.startswith("测试") and text.endswith("[结果已截断]")
            assert len(text.encode()) <= 32 and "�" not in text
    asyncio.run(run())


def test_list_change_disconnect_and_reconnect(monkeypatch):
    monkeypatch.setattr(mcp_client, "_HEARTBEAT", 0.05)
    monkeypatch.setattr(mcp_client, "_BACKOFF", 0.25)

    async def until(check):
        async with asyncio.timeout(4):
            while not check():
                await asyncio.sleep(0.01)

    async def run():
        async with connected(connect_timeout=1) as manager:
            assert await manager.call(ECHO, {"text": "刷新"}) == "刷新"
            await until(lambda: len(manager.tools()) == 2)
            assert await manager.call(ECHO, {"text": "断线"}) == "断线"
            await until(lambda: manager.status()[0]["state"] == "disconnected" and not manager.tools())
            assert "工具错误" in await manager.call(ECHO, {"text": "旧调用"})
            await until(lambda: len(manager.tools()) == 1)
            assert await manager.call(ECHO, {"text": "重连"}) == "重连"
    asyncio.run(run())
