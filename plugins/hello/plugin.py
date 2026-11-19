"""示例插件：hello。

- 命令 ``/hello`` 回复一句问候
- ``on_message`` 钩子演示：忽略空消息，并在 ``/hello`` 时打印日志
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx):
    """插件入口。"""

    @ctx.command("hello")
    async def hello_command(runtime, event, args):
        name = event.sender_name or "朋友"
        return f"你好呀 {name}，我是 {runtime.persona.name}。"

    @ctx.on_message
    async def log_messages(runtime, event):
        # 返回 None/True 表示放行；返回 False 会拦截这条消息
        if not (event.text or "").strip():
            return False
        # 这个钩子只演示用法：把最近的记忆条数写进日志
        count = runtime.store.count_memories()
        logger.debug("[hello] %s 在 %s 说：%s（记忆库 %s 条）", event.sender_name, event.chat_id, event.text, count)
        return True

    @ctx.on_after_reply
    async def after_reply(runtime, event, reply):
        logger.debug("[hello] 已回复 %s：%s", event.sender_name, reply[:40])
