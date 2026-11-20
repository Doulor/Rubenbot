"""替换为配置的 MCP 子进程；SDK 的默认环境白名单比本项目宽。"""

import json
import os
import sys


if __name__ == "__main__":
    # 只在 argv 上传键名，不把密钥值暴露给进程列表，也不经 shell。
    keys = json.loads(sys.argv[1])
    env = {key: os.environ[key] for key in keys if key in os.environ}
    os.execvpe(sys.argv[2], sys.argv[2:], env)
