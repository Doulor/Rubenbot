"""适配器基类与统一事件模型。

各平台适配器只负责：把平台消息转成 :class:`MessageEvent`，以及把
:class:`Segment` 列表发出去。主循环不感知任何平台细节。

消息段统一用 ``{"type": ..., "data": {...}}`` 表示。:class:`Segment` 只是
这个 dict 的轻量包装（可以直接 JSON 序列化、直接与 dict 字面量比较），
不引入额外的类层级。

平台特有能力（贴表情、发说说等）由适配器在 ``capabilities`` 里声明，
核心按名调用；未声明的能力一律优雅失败，不影响主循环。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

# 非文本消息段在纯文本投影里的占位符；text 直接取 data["text"]
SEGMENT_PLACEHOLDERS: dict[str, str] = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


class CapabilityError(RuntimeError):
    """平台能力调用失败（适配器已声明该能力，但执行出错）。"""


class CapabilityNotSupported(CapabilityError):
    """适配器没有声明某个能力。"""

    def __init__(self, adapter_name: str, capability: str):
        super().__init__(f"适配器 {adapter_name} 不支持能力 {capability}")
        self.adapter_name = adapter_name
        self.capability = capability


class Segment(dict):
    """消息段：``{"type": ..., "data": {...}}`` 的轻量包装。

    本质上就是 dict：``segment["type"]`` 与 ``segment.type`` 等价，也能直接
    和 dict 字面量比较或交给 ``json.dumps``。
    """

    def __init__(self, type: str = "text", data: Mapping[str, Any] | None = None):  # noqa: A002 - 与消息段字段同名
        super().__init__(type=str(type or "text"), data=dict(data or {}))

    @property
    def type(self) -> str:  # noqa: A003 - 与消息段字段同名
        return str(self.get("type") or "text")

    @property
    def data(self) -> dict[str, Any]:
        value = self.get("data")
        return value if isinstance(value, dict) else {}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Segment":
        data = value.get("data")
        return cls(
            str(value.get("type") or "text"),
            data if isinstance(data, Mapping) else {},
        )

    @classmethod
    def text(cls, content: str) -> "Segment":
        return cls(type="text", data={"text": content})

    @classmethod
    def image(cls, url: str) -> "Segment":
        return cls(type="image", data={"url": url})

    @classmethod
    def at(cls, qq: str | int) -> "Segment":
        return cls(type="at", data={"qq": str(qq)})

    @classmethod
    def face(cls, face_id: str | int) -> "Segment":
        return cls(type="face", data={"id": str(face_id)})

    @classmethod
    def reply(cls, message_id: str | int) -> "Segment":
        return cls(type="reply", data={"id": str(message_id)})

    def render(self) -> str:
        """纯文本渲染，供 Console、日志和 ``MessageEvent.text`` 投影使用。"""
        if self.type == "text":
            return str(self.data.get("text", ""))
        if self.type == "at":
            qq = self.data.get("qq")
            return f"@{qq} " if qq not in (None, "") else " "
        if self.type == "reply":
            # reply 段单独通过 reply_to_id 表达，不进入正文
            return ""
        return SEGMENT_PLACEHOLDERS.get(self.type, f"[{self.type}]")


def as_segment(segment: "Segment | Mapping[str, Any]") -> Segment:
    """把 Segment 或普通 dict 统一成 Segment（不复制已归一化的对象）。"""
    if isinstance(segment, Segment):
        return segment
    if isinstance(segment, Mapping):
        return Segment.from_dict(segment)
    return Segment.text(str(segment))


def render_segment(segment: "Segment | Mapping[str, Any]") -> str:
    """单个消息段的纯文本投影。"""
    return as_segment(segment).render()


def render_segments(segments: Iterable["Segment | Mapping[str, Any]"] | None) -> str:
    """把消息段列表渲染成纯文本。"""
    return "".join(render_segment(segment) for segment in segments or [])


def text_segments(text: str) -> list[Segment]:
    """便捷方法：一段纯文本 -> Segment 列表。"""
    return [Segment.text(text)]


@dataclass
class MessageEvent:
    """统一事件模型。

    ``text`` 是 ``segments`` 的纯文本投影（图片→[图片]、表情→[表情] 等）；
    记忆、上下文与意愿判断仍只依赖 ``text``，行为不变。

    ``platform_msg_id`` 是平台侧的消息 id（拿不到时为 None），用于入站去重
    与引用解析；``reply_to_id`` 是这条消息引用的那条消息的平台 id。
    """

    platform: str
    chat_type: str  # private / group
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    reply_to_id: str | None = None
    mentions_bot: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    platform_msg_id: str | None = None

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

    # 平台特有能力（按名调用）；默认什么都不支持
    capabilities: frozenset[str] = frozenset()
    # 能力名 -> 配置里 ``adapters.<name>.capabilities`` 段的开关键
    capability_config_keys: dict[str, str] = {}
    # 配置未写开关时的默认值（不在表里的能力默认开启）
    capability_defaults: dict[str, bool] = {}

    @abc.abstractmethod
    async def start(self, on_event: EventCallback) -> None:
        """启动并持续把统一事件交给 ``on_event``；应阻塞直到 stop。"""

    @abc.abstractmethod
    async def send(
        self, chat_id: str, segments: list[Segment], *, chat_type: str = "group"
    ) -> str | None:
        """发送消息，返回平台侧的消息 id；平台不提供或不在响应里时返回 None。

        返回值可以忽略——core 用它把 bot 自己发出的消息连同 id 落进流水，
        以便后续解析「这条消息引用了谁」和拼 bot 自己的时间线。
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止适配器，释放资源。"""

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        """调用平台特有能力；默认实现表示不支持。

        子类声明了能力就应覆盖本方法。异常由核心入口捕获并转成失败原因。
        """
        raise CapabilityNotSupported(self.name, name)
