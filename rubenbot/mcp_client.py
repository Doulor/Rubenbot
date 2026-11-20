"""MCP 客户端：每台服务器独立持有一个会话，工具目录随会话发布和撤销。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

from .config import MCPConfig, MCPServerConfig

if TYPE_CHECKING:
    from mcp import ClientSession

logger = logging.getLogger(__name__)
_HEARTBEAT = 5.0
_BACKOFF = 1.0
_MAX_BACKOFF = 60.0


@dataclass
class _Server:
    config: MCPServerConfig
    state: str = "disconnected"
    last_error: str = ""
    session: ClientSession | None = None
    schemas: dict[str, dict] = field(default_factory=dict)
    refresh: asyncio.Event = field(default_factory=asyncio.Event)


async def _open_transport(stack: AsyncExitStack, config: MCPServerConfig, limits: MCPConfig):
    if config.transport == "http":
        from mcp.client.streamable_http import streamable_http_client

        client = await stack.enter_async_context(httpx.AsyncClient(
            headers=config.headers,
            timeout=httpx.Timeout(max(limits.connect_timeout, limits.call_timeout), connect=limits.connect_timeout),
        ))
        read, write, _ = await stack.enter_async_context(streamable_http_client(config.url, http_client=client))
        return read, write

    from mcp.client.stdio import StdioServerParameters, stdio_client

    # SDK 在 env 上叠加自己的白名单。隔离启动一个极小的 exec 包装器，再收窄为本项目的白名单。
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ} | config.env
    helper = Path(__file__).with_name("_mcp_stdio.py")
    params = StdioServerParameters(command=sys.executable, env=env, args=[
        "-I", "-S", str(helper), json.dumps(list(env)), config.command, *config.args,
    ])
    return await stack.enter_async_context(stdio_client(params))


async def _discover(session: ClientSession, server: str) -> dict[str, dict]:
    from mcp.types import PaginatedRequestParams

    schemas = {}
    seen = set()
    params = None
    while True:
        page = await session.list_tools(params=params)
        for tool in page.tools:
            name = f"mcp__{server}__{tool.name}"
            # 不让畸形函数名使整个模型请求（包括内置工具）被端点拒绝。
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                logger.warning("MCP %s：跳过模型不支持的工具名", server)
                continue
            schemas[name] = {"type": "function", "function": {
                "name": name, "description": tool.description or tool.name, "parameters": tool.inputSchema,
            }}
        if not page.nextCursor:
            return schemas
        if page.nextCursor in seen:
            raise ValueError("工具分页游标重复")
        seen.add(page.nextCursor)
        params = PaginatedRequestParams(cursor=page.nextCursor)


def _diagnostic(exc: Exception) -> str:
    # SDK 的任务组可能包裹原始异常；不把 URL、headers 或命令参数带入状态页和审计日志。
    while isinstance(exc, ExceptionGroup):
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "连接或心跳超时"
    if isinstance(exc, ImportError):
        return "缺少 MCP 依赖，请安装 rubenbot[mcp]"
    return f"连接失败或已断开（{type(exc).__name__}）"


class MCPManager:
    def __init__(self, config: MCPConfig):
        self.config = config
        self._servers = {s.name: _Server(s, "disconnected" if s.enabled else "disabled") for s in config.servers}
        self._tasks: list[asyncio.Task] = []

    def status(self) -> list[dict]:
        return [dict(name=s.config.name, transport=s.config.transport, state=s.state,
                     tool_count=len(s.schemas), last_error=s.last_error) for s in self._servers.values()]

    def tools(self) -> list[dict]:
        return [schema for s in self._servers.values() for schema in s.schemas.values()]

    async def start(self) -> None:
        if self._tasks:
            return
        for s in self._servers.values():
            if not s.config.enabled:
                continue
            attempted = asyncio.Event()
            self._tasks.append(asyncio.create_task(self._supervise(s, attempted), name=f"mcp:{s.config.name}"))
            await attempted.wait()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _supervise(self, s: _Server, attempted: asyncio.Event) -> None:
        delay = _BACKOFF
        while True:
            s.state = "connecting"
            started = monotonic()
            try:
                await self._serve(s, attempted)
            except Exception as exc:
                s.last_error = _diagnostic(exc)
                logger.warning("MCP %s：%s", s.config.name, s.last_error)
            finally:
                s.state = "disconnected"
                attempted.set()
            if monotonic() - started >= _MAX_BACKOFF:
                delay = _BACKOFF
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF)

    async def _serve(self, s: _Server, attempted: asyncio.Event) -> None:
        from mcp import ClientSession, types

        async def on_message(message):
            if isinstance(message, Exception) or (
                isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification)
            ):
                s.refresh.set()

        # SDK 的 anyio 上下文必须由同一任务进出，不能用 wait_for 在另一任务中 enter。
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(self.config.connect_timeout):
                    read, write = await _open_transport(stack, s.config, self.config)
                    session = await stack.enter_async_context(ClientSession(read, write, message_handler=on_message))
                    await session.initialize()
                    s.refresh.clear()
                    s.schemas = await _discover(session, s.config.name)
                s.session = session
                s.state = "connected"
                s.last_error = ""
                attempted.set()
                logger.info("MCP %s 已连接，%d 个工具", s.config.name, len(s.schemas))
                while True:
                    try:
                        async with asyncio.timeout(_HEARTBEAT):
                            await s.refresh.wait()
                    except TimeoutError:
                        async with asyncio.timeout(self.config.connect_timeout):
                            await session.send_ping()
                    else:
                        s.refresh.clear()
                        async with asyncio.timeout(self.config.connect_timeout):
                            s.schemas = await _discover(session, s.config.name)
            finally:
                # 先撤销清单，再等待传输关闭，避免模型继续拿到失效工具。
                s.schemas = {}
                s.session = None
                s.state = "disconnected"
                attempted.set()
