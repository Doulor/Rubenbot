"""MCP 客户端：每台服务器独立持有一个会话，工具目录随会话发布和撤销。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from copy import deepcopy
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


def _bounded(text: str, limit: int) -> str:
    # SDK 已经解码了应答；只编码有可能放进结果的前缀，不另做整份大字节串副本。
    data = text[:limit + 1].encode("utf-8", errors="replace")
    if len(data) <= limit:
        return data.decode()
    suffix = "\n[结果已截断]"
    return data[:limit - len(suffix.encode())].decode(errors="ignore") + suffix


class MCPManager:
    def __init__(self, config: MCPConfig):
        self.config = config
        self._servers = {s.name: _Server(s, "disconnected" if s.enabled else "disabled") for s in config.servers}
        self._tasks: dict[asyncio.Task, asyncio.Event] = {}
        self._stopping = False

    def status(self) -> list[dict]:
        return [dict(name=s.config.name, transport=s.config.transport, state=s.state,
                     tool_count=len(s.schemas), last_error=s.last_error) for s in self._servers.values()]

    def tools(self) -> list[dict]:
        """在线工具快照：每台首次发现前为空；刷新中保留上次完整目录，断线即撤下。"""
        return deepcopy([schema for s in self._servers.values() for schema in s.schemas.values()])

    async def call(self, name: str, arguments: Any) -> str:
        """只执行在线目录里的工具；不重试调用（上次请求可能已产生副作用）。"""
        _, _, suffix = name.partition("mcp__")
        server, _, tool = suffix.partition("__")
        s = self._servers.get(server)
        started = monotonic()
        ok = False
        try:
            if not s or not s.session or name not in s.schemas:
                return _bounded("工具错误：服务器离线或工具不存在", self.config.max_result_bytes)
            if not isinstance(arguments, dict):
                return _bounded("工具错误：参数必须是 JSON 对象", self.config.max_result_bytes)
            from jsonschema import ValidationError, validate
            from referencing import Registry

            schema = s.schemas[name]["function"]["parameters"]
            try:
                # 仅解析 schema 文档内的引用，不为外部 $ref 发起网络请求。
                validate(arguments, schema, registry=Registry())
            except ValidationError as exc:
                return _bounded(f"工具错误：参数不符合 {exc.validator} 约束", self.config.max_result_bytes)
            async with asyncio.timeout(self.config.call_timeout):
                result = await s.session.call_tool(tool, arguments)
            parts = []
            for block in result.content:
                if block.type == "text":
                    parts.append(block.text)
                elif block.type == "resource" and hasattr(block.resource, "text"):
                    parts.append(block.resource.text)
                else:
                    parts.append(f"[{block.type}]")  # 文字工具通道不透传 base64
            if not parts and result.structuredContent is not None:
                parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
            text = "\n".join(parts)
            ok = not result.isError
            if not ok:
                s.last_error = "远端工具返回错误"
            return _bounded(text if ok else "工具错误：" + text, self.config.max_result_bytes)
        except TimeoutError:
            if s:
                s.last_error = "工具调用超时"
            return "工具错误：调用超时"
        except Exception as exc:
            if s:
                s.last_error = f"工具调用失败（{type(exc).__name__}）"
                s.refresh.set()  # 唤醒监视器复查目录；RPC 业务错误不应直接踢掉会话。
            return _bounded(f"工具错误：调用失败（{type(exc).__name__}）", self.config.max_result_bytes)
        finally:
            logger.info("MCP 调用 server=%r tool=%r elapsed_ms=%.1f success=%s",
                        server, tool, (monotonic() - started) * 1000, ok)

    async def start(self) -> None:
        """并行连接，所有调用者都等到每台启用服务已发布目录或已记录首次错误。"""
        if not self._tasks:
            for s in self._servers.values():
                if not s.config.enabled:
                    continue
                if (s.config.transport == "stdio"
                        and os.path.basename(s.config.command) not in self.config.stdio_allowed_commands):
                    s.state = "error"
                    s.last_error = "stdio 启动器不在 mcp.stdio_allowed_commands 白名单中"
                    logger.warning("MCP %s：%s", s.config.name, s.last_error)
                    continue
                attempted = asyncio.Event()
                task = asyncio.create_task(self._supervise(s, attempted), name=f"mcp:{s.config.name}")
                self._tasks[task] = attempted
        for attempted in tuple(self._tasks.values()):
            await attempted.wait()

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # 尚未获得调度就被取消的会话不会进入 _supervise 的 finally。
        for attempted in self._tasks.values():
            attempted.set()
        self._tasks.clear()
        self._stopping = False

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
            # 传输退出时可能把取消包成普通异常；显式停机后绝不再重连。
            if self._stopping:
                return
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
                    schemas = await _discover(session, s.config.name)
                # 同一事件循环中不让出控制权，一起发布会话、完整目录和在线状态。
                s.schemas = schemas
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
            except Exception as exc:
                s.last_error = _diagnostic(exc)
                raise
            finally:
                # 先撤销清单，再等待传输关闭，避免模型继续拿到失效工具。
                s.schemas = {}
                s.session = None
                s.state = "disconnected"
                attempted.set()
