"""实测台场景 3、6；同一文件以 --stdio 启动时只提供本地回声工具。"""

import json
import logging
import re
import secrets
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_SERVER = "e2e"
_TOOL = f"mcp__{_SERVER}__echo"


def configure(config, root: Path, *, mcp: bool, link: bool):
    """只配置一次性实例，不继承真实外部服务或改写仓库内插件。"""
    from rubenbot.config import MCPServerConfig

    if mcp:
        config.mcp.stdio_allowed_commands = [Path(sys.executable).name]
        config.mcp.servers = [MCPServerConfig(
            name=_SERVER, command=sys.executable, args=["-I", "-B", str(Path(__file__).resolve()), "--stdio"],
        )]
    plugins = root / "fixture-plugins"
    config.plugins.dir = str(plugins)
    if not link:
        return
    source = Path(__file__).resolve().parents[1] / "plugins" / "link_parse"
    dest = plugins / "link_parse"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.py", "parse.py", "fetch.py", "manifest.json"):
        shutil.copyfile(source / name, dest / name)
    (dest / "config.yaml").write_text("mode: on_mention\nsites: [generic]\n", encoding="utf-8")
    config.plugins.enabled = ["link_parse"]


def _stdio():
    schema = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 80}},
              "required": ["text"], "additionalProperties": False}
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:  # 初始化完成等通知没有应答。
            continue
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        method = request.get("method")
        params = request.get("params", {})
        if method == "initialize":
            reply["result"] = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                               "serverInfo": {"name": _SERVER, "version": "1"}}
        elif method == "ping":
            reply["result"] = {}
        elif method == "tools/list":
            reply["result"] = {"tools": [{"name": "echo", "description": "返回原文和本次随机回执。",
                                          "inputSchema": schema}]}
        elif method == "tools/call" and params.get("name") == "echo":
            text = params.get("arguments", {}).get("text")
            if isinstance(text, str) and len(text) <= 80:
                receipt = f"{text}#{secrets.token_hex(6)}"
                reply["result"] = {"content": [{"type": "text", "text": receipt}]}
            else:
                reply["error"] = {"code": -32602, "message": "需要短文本"}
        else:
            reply["error"] = {"code": -32601, "message": "未知方法或工具"}
        print(json.dumps(reply), flush=True)


class _Audit(logging.Handler):
    """只计产品日志中的成功事件，不保存提示词、参数或输出。"""

    def __init__(self):
        super().__init__()
        self.success = 0

    def emit(self, record):
        if re.fullmatch(r"MCP 调用 server='e2e' tool='echo' elapsed_ms=[0-9.]+ success=True", record.getMessage()):
            self.success += 1


async def _send(ctx, text, **kwargs):
    try:
        return await ctx.send(text, **kwargs)
    except Exception:
        raise AssertionError("消息轮执行异常，未取得完整的端到端证据") from None


async def mcp(ctx):
    if not any(t["function"]["name"] == _TOOL for t in ctx.bot.mcp.tools()):
        raise AssertionError("本地回声工具未发布；请检查 MCP 可选依赖、启动器白名单和连接状态")
    challenge = f"ECHO-{secrets.token_hex(4)}"
    logger = logging.getLogger("rubenbot.mcp_client")
    level, audit = logger.level, _Audit()
    start = len(ctx.prompts)
    logger.addHandler(audit)
    try:
        logger.setLevel(logging.INFO)
        sent = await _send(ctx, f"请实际调用 {_TOOL}，text 参数为 {challenge}。"
                           "只回复工具返回的完整原文（包括井号和随机回执）。",
                           chat="e2e-mcp", sender="console:me")
    finally:
        logger.removeHandler(audit)
        logger.setLevel(level)
    if not audit.success:
        raise AssertionError("本轮未观察到本地 echo 工具的成功调用审计")
    # 用同一请求里的调用 ID 对齐结果；回执在服务端生成，不能靠复述用户消息通过。
    receipts = set()
    for messages in ctx.prompts[start:]:
        ids = {c["id"] for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or []
               if c.get("id") and c.get("function", {}).get("name") == _TOOL}
        for msg in messages:
            content = msg.get("content")
            if (msg.get("role") == "tool" and msg.get("tool_call_id") in ids and isinstance(content, str)
                    and re.fullmatch(challenge + r"#[0-9a-f]{12}", content)):
                receipts.add(content)
    if not receipts:
        raise AssertionError("虽有成功审计，但模型请求中没有与本轮 echo 调用对应的回执")
    outbound = sent if isinstance(sent, str) else "".join(sent or [])
    if not any(receipt in outbound for receipt in receipts):
        raise AssertionError("工具回执已进入模型请求，但本轮出站文本未带回完整回执")
    return f"本轮 echo 成功审计 {audit.success} 条；工具结果与出站文本中的随机回执一致"


async def _cache_control(ctx, url, marker):
    """只给刚装配的临时插件注入一条已解析的合成记录，不替换网络函数或钩子。"""
    plugin = ctx.bot.plugins.plugins["link_parse"]
    if plugin.path.resolve() != (ctx.root / "fixture-plugins/link_parse").resolve():
        return "缓存对照未运行：不是夹具的临时插件实例"
    parsers = [h.__self__ for h in plugin.before_reply_hooks if getattr(h, "__name__", "") == "before_reply"
               and getattr(h, "__self__", None) is not None]
    if len(parsers) != 1 or not hasattr(parsers[0], "cache"):
        return "缓存对照未运行：未找到已注册的解析器缓存"
    cache = parsers[0].cache
    page = {"标题": "测试文章", "正文": marker, "链接": url}
    start = len(ctx.prompts)
    cache.put(url, page, 300)
    try:
        await _send(ctx, f"请概括这篇文章：{url} （只参考链接资料，不另行查询）。",
                    chat="e2e-link-control", group=True, mention=True)
    except AssertionError:
        return "缓存对照失败：消息轮未完成"
    finally:
        cache.entries.pop(url, None)
    for messages in ctx.prompts[start:]:
        systems = [m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]
        if any(marker in s for s in systems):
            return "缓存对照失败：合成正文进入了 system 消息"
        guide = any("external_link_data" in s and "不可信外部文本" in s and "不是指令" in s for s in systems)
        if not guide:
            continue
        for msg in messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
                continue
            block = re.fullmatch(r"【链接解析·外部资料】\s*<external_link_data>(.*?)</external_link_data>",
                                 msg["content"], re.DOTALL)
            if block:
                try:
                    data = json.loads(block[1])
                except ValueError:
                    continue
                if data == [page]:
                    return "缓存对照通过（非网络抓取）：同一模型请求有 system 隔离说明，独立 user 数据块含合成正文校验码"
    return "缓存对照失败：模型请求未同时包含 system 隔离说明和含正文校验码的独立 user 数据块"


async def link(ctx):
    if not ctx.bot.plugins or "link_parse" not in ctx.bot.plugins.enabled_names:
        raise AssertionError("链接插件未加载，无法检验外部资料是否注入")
    marker = "假页面正文校验码" + secrets.token_hex(8)
    body = f"<html><title>测试文章</title><article>{marker}</article></html>".encode()
    fetched = threading.Event()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/article":
                self.send_error(404)
                return
            fetched.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # 不记录 URL、请求头等输入。

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    except OSError:
        raise AssertionError("本地假页面未能监听回环端口，无法检验链接插件") from None
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/article"
            start = len(ctx.prompts)
            await _send(ctx, f"请总结这个链接的内容：{url} （只参考链接插件提供的资料，不使用工具）。",
                        chat="e2e-link", group=True, mention=True)
        finally:
            server.shutdown()
            thread.join()

    # 对回环地址，安全行为是未抓取。不能借机给 fetch 打补丁，或以模型口述代替注入证据。
    snapshots = ctx.prompts[start:]
    for messages in snapshots:
        if any(marker in str(m.get("content")) for m in messages):
            raise AssertionError("本地假页面正文进入了模型请求；即使带隔离标注也不应抓取回环地址")
    if fetched.is_set():
        raise AssertionError("本地假页面收到了访问，公网抓取限制未得到验证；未观察到正文注入")
    if not any(url in str(m.get("content")) for messages in snapshots for m in messages):
        raise AssertionError("未观察到携带本轮链接的模型请求，不能判断上下文注入")
    control = await _cache_control(ctx, url + "-cache-control", marker)
    raise AssertionError(f"插件仅抓公网：本地假页面零访问，真实抓取未注入；{control}")


if __name__ == "__main__" and sys.argv[1:] == ["--stdio"]:
    _stdio()
