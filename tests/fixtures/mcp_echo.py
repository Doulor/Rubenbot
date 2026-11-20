"""最小的测试用 MCP 对端，stdin/stdout 每行一个 JSON-RPC 消息。"""

import json
import os
import sys


def send(**fields):
    print(json.dumps({"jsonrpc": "2.0", **fields}), flush=True)


tool = {"name": "echo", "description": "复述文本", "inputSchema": {
    "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
}}
extra_tool = {**tool, "name": "another"}
changed = False
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    method = req["method"]
    if method == "initialize":
        if "--hang" in sys.argv:
            continue
        result = {"protocolVersion": req["params"]["protocolVersion"],
                  "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [tool, extra_tool] if changed else [tool]}
    elif method == "tools/call":
        text = req["params"]["arguments"]["text"]
        if text == "超时":
            continue
        if text == "启动参数":
            text = json.dumps(sys.argv[1:])
        if text == "环境":
            text = json.dumps(dict(os.environ))
        if text == "刷新":
            changed = True
            send(method="notifications/tools/list_changed")
        result = {"content": [{"type": "text", "text": text}], "isError": text == "失败"}
        if text == "断线":
            send(id=req["id"], result=result)
            break
    elif method == "ping":
        result = {}
    else:
        send(id=req["id"], error={"code": -32601, "message": "未知方法"})
        continue
    send(id=req["id"], result=result)
