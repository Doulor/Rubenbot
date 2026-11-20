"""ConsoleAdapter：stdin/stdout，本地调试用，不依赖任何平台。

每行输入视为一条消息，格式：
  ``text``            —— 以 ``console:me`` 在 private 会话里发言
  ``[群名] text``     —— 在名为 ``群名`` 的群聊里发言（同一群名 = 同一 chat_id）
  ``[群名|昵称] text``—— 指定群和昵称

bot 的回复打印到 stdout，以 ``Ruben:`` 开头。
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any, TextIO

from .base import (
    SEND_STICKER,
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    EventCallback,
    MessageEvent,
    Segment,
    render_segments,
    text_segments,
)

_LINE_RE = re.compile(r"^\[(?P<chat>[^\]]+)\]\s*(?P<text>.*)$")
_GROUP_USER_RE = re.compile(r"^(?P<chat>[^|\]]+)\|(?P<name>[^|\]]+)$")


class ConsoleAdapter(BaseAdapter):
    """从 stdin 读消息、往 stdout 写回复。"""

    name = "console"

    # 本地调试也把「发表情」演一遍：没有富媒体，就打一行 [表情] 占位
    capabilities: frozenset[str] = frozenset({SEND_STICKER})

    def __init__(
        self,
        *,
        reader: asyncio.StreamReader | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        user_id: str = "console:me",
        user_name: str = "你",
        bot_name: str = "Ruben",
    ):
        self.reader = reader
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.user_id = user_id
        self.user_name = user_name
        self.bot_name = bot_name
        self.sent: list[tuple[str, str, list[Segment]]] = []  # 测试/调试用
        # 本地递增消息 id：入站事件与出站回执都用它，便于去重/引用解析
        self._next_id = 1
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._on_event: EventCallback | None = None

    async def start(self, on_event: EventCallback) -> None:
        self._on_event = on_event
        self._stop.clear()
        if self.reader is not None:
            await self._read_stream(self.reader)
            return
        # 同步 stdin 放到线程里读，避免阻塞事件循环
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, self.stdin.readline)
            if not line:
                break
            await self.feed_line(line.rstrip("\n"))
            if self._stop.is_set():
                break

    async def _read_stream(self, reader: asyncio.StreamReader) -> None:
        while not self._stop.is_set():
            line = await reader.readline()
            if not line:
                break
            await self.feed_line(line.decode("utf-8", errors="replace").rstrip("\n"))
            if self._stop.is_set():
                break

    async def feed_line(self, line: str) -> None:
        """处理一行输入；空行忽略。测试可直接调用（需先 bind 或 start）。"""
        line = line.strip()
        if not line:
            return
        await self.dispatch(self.parse_line(line))

    def bind(self, on_event: EventCallback) -> None:
        """不启动 start() 时，手动绑定事件回调，便于嵌入测试。"""
        self._on_event = on_event

    async def dispatch(self, event: MessageEvent) -> None:
        """把事件交给回调（若已绑定）。"""
        if self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception as exc:  # noqa: BLE001 - 调试适配器不应崩掉
                self.write(f"[error] {exc}")

    def parse_line(self, line: str) -> MessageEvent:
        chat_type = "private"
        chat_id = self.user_id
        sender_name = self.user_name
        text = line
        match = _LINE_RE.match(line)
        if match:
            chat_type = "group"
            chat_raw = match.group("chat").strip()
            text = match.group("text").strip()
            group_match = _GROUP_USER_RE.match(chat_raw)
            if group_match:
                chat_id = group_match.group("chat").strip()
                sender_name = group_match.group("name").strip()
            else:
                chat_id = chat_raw
            sender_name = sender_name or self.user_name
        return MessageEvent(
            platform="console",
            chat_type=chat_type,
            chat_id=chat_id,
            sender_id=self.user_id,
            sender_name=sender_name,
            text=text,
            mentions_bot=True if chat_type == "private" else False,
            raw={"console": True},
            segments=text_segments(text),
            platform_msg_id=self._local_id(),
        )

    def _local_id(self) -> str:
        """本地递增消息 id（单进程内唯一即可）。"""
        value = self._next_id
        self._next_id += 1
        return f"console-{value}"

    async def send(
        self, chat_id: str, segments: list[Segment], *, chat_type: str = "group"
    ) -> str | None:
        # 非文本段渲染成占位符（[图片]、[语音] 等），Console 没有富媒体
        text = render_segments(segments)
        self.sent.append((chat_id, chat_type, segments))
        self.write(f"{self.bot_name}: {text}")
        return self._local_id()

    async def send_file(self, chat_id: str, filename: str, content: bytes, *, chat_type: str) -> None:
        # 调试适配器保留附件段供本地消费者读取，终端本身只打印占位符。
        await self.send(chat_id, [Segment("file", {"name": filename, "content": content})], chat_type=chat_type)

    def write(self, text: str) -> None:
        try:
            self.stdout.write(text + "\n")
            self.stdout.flush()
        except Exception:  # noqa: BLE001 - 输出失败不影响主循环
            pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        """Console 只演 send_sticker（文本占位）；其余能力优雅失败。"""
        if name != SEND_STICKER:
            raise CapabilityNotSupported(self.name, name)
        chat_id = kwargs.get("chat_id")
        if chat_id in (None, ""):
            raise CapabilityError("send_sticker 需要 chat_id")
        # 与普通出站一致：占位符走 render，消息进 sent 便于测试/调试
        segment = Segment.sticker()
        self.sent.append((str(chat_id), str(kwargs.get("chat_type") or "group"), [segment]))
        self.write(f"{self.bot_name}: {segment.render()}")
        return {"sent": True}
