"""两种 MCP 传输走真实 SDK 握手、发现路径，HTTP 只替换网络层。"""

import asyncio
import json
from pathlib import Path
import sys

import httpx

from rubenbot import mcp_client
from rubenbot.config import MCPConfig, MCPServerConfig
from rubenbot.mcp_client import MCPManager

FAKE = Path(__file__).with_name("fixtures") / "mcp_echo.py"


def stdio(name="demo", *args, **kwargs):
    return MCPServerConfig(name=name, command=sys.executable, args=[str(FAKE), *args], **kwargs)


def test_stdio_discovery_and_isolated_connect_timeout():
    async def run():
        manager = MCPManager(MCPConfig(servers=[stdio("slow", "--hang"), stdio(), stdio("off", enabled=False)],
                                       connect_timeout=0.5))
        try:
            await asyncio.wait_for(manager.start(), 5)
            assert [s["state"] for s in manager.status()] == ["disconnected", "connected", "disabled"]
            assert "超时" in manager.status()[0]["last_error"]
            tools = manager.tools()
            assert len(tools) == 1
            assert tools[0]["function"]["name"] == "mcp__demo__echo"
            assert tools[0]["function"]["parameters"]["required"] == ["text"]
            tools[0]["function"]["parameters"]["required"].clear()
            assert manager.tools()[0]["function"]["parameters"]["required"] == ["text"]
        finally:
            await manager.stop()
        assert not manager.tools()
    asyncio.run(run())


def test_concurrent_start_waits_for_each_servers_discovery(monkeypatch):
    discover = mcp_client._discover

    async def run():
        discovering, release, fast_ready, second_started = (asyncio.Event() for _ in range(4))

        async def gated_discovery(session, name):
            schemas = await discover(session, name)
            if name == "slow":
                discovering.set()
                await release.wait()
            else:
                fast_ready.set()
            return schemas

        monkeypatch.setattr(mcp_client, "_discover", gated_discovery)
        manager = MCPManager(MCPConfig(servers=[stdio("slow"), stdio("fast")]))

        async def second_start():
            second_started.set()
            await manager.start()

        try:
            async with asyncio.timeout(5):
                first = asyncio.create_task(manager.start())
                await discovering.wait()
                second = asyncio.create_task(second_start())
                await second_started.wait()
                assert not second.done()
                await fast_ready.wait()
                assert not first.done()
                assert [s["state"] for s in manager.status()] == ["connecting", "connected"]
                assert [t["function"]["name"] for t in manager.tools()] == ["mcp__fast__echo"]
                release.set()
                await asyncio.gather(first, second)
                assert [s["state"] for s in manager.status()] == ["connected", "connected"]
                assert [t["function"]["name"] for t in manager.tools()] == ["mcp__slow__echo", "mcp__fast__echo"]
        finally:
            release.set()
            await manager.stop()
    asyncio.run(run())


def test_http_discovery_with_headers_and_pagination(monkeypatch):
    requests = []
    second_page, release = asyncio.Event(), asyncio.Event()

    async def endpoint(request):
        assert request.url == httpx.URL("https://mcp.invalid/api")
        assert request.headers["X-Test-Key"] == "explicit"
        msg = json.loads(request.content)
        requests.append(msg["method"])
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "http", "version": "1"}}
        else:
            second = msg.get("params", {}).get("cursor") == "second"
            result = {"tools": [{"name": "two" if second else "one", "inputSchema": {"type": "object"}}]}
            if second:
                second_page.set()
                await release.wait()
            else:
                result["nextCursor"] = "second"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})

    client = httpx.AsyncClient
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(endpoint), **kw))

    async def run():
        server = MCPServerConfig.parse({"name": "remote", "transport": "http", "url": "https://mcp.invalid/api",
                                        "headers": {"X-Test-Key": "explicit"}})
        manager = MCPManager(MCPConfig(servers=[server], connect_timeout=2))
        try:
            start = asyncio.create_task(manager.start())
            await asyncio.wait_for(second_page.wait(), 5)
            assert not start.done()
            assert manager.tools() == []
            assert manager.status()[0]["tool_count"] == 0
            release.set()
            await start
            assert [t["function"]["name"] for t in manager.tools()] == ["mcp__remote__one", "mcp__remote__two"]
            assert requests == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
        finally:
            await manager.stop()
    asyncio.run(run())
