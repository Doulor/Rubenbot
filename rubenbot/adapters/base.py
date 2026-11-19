"""适配器基类与统一事件模型。

各平台适配器只负责：把平台消息转成 :class:`MessageEvent`，以及把
:class:`Segment` 列表发出去。主循环不感知任何平台细节。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Segment:
    """消息段。v1 以文本为主，图片/表情先占位。"""

    type: str = "text"  # text / image / face
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text(cls, content: str) -> "Segment":
        return cls(type="text", data={"text": content})

    @classmethod
    def image(cls, url: str) -> "Segment":
        return cls(type="image", data={"url": url})

    def render(self) -> str:
        """纯文本渲染，供 Console 和日志使用。"""
        if self.type == "text":
            return str(self.data.get("text", ""))
        if self.type == "image":
            return "[图片]"
        return f"[{self.type}]"


def text_segments(text: str) -> list[Segment]:
    """便捷方法：一段纯文本 -> Segment 列表。"""
    return [Segment.text(text)]


@dataclass
class MessageEvent:
    """统一事件模型。"""

    platform: str
    chat_type: str  # private / group
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    reply_to_id: str | None = None
    mentions_bot: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


EventCallback = Callable[[MessageEvent], Awaitable[None]]


class BaseAdapter(abc.ABC):
    """适配器接口。"""

    name: str = "base"

    @abc.abstractmethod
    async def start(self, on_event: EventCallback) -> None:
        """启动并持续把统一事件交给 ``on_event``；应阻塞直到 stop。"""

    @abc.abstractmethod
    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> None:
        """发送消息。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止适配器，释放资源。"""
