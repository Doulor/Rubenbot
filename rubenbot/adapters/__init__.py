"""平台适配器。"""

from .base import BaseAdapter, MessageEvent, Segment, text_segments
from .console import ConsoleAdapter
from .snowluma import SnowLumaAdapter

__all__ = [
    "BaseAdapter",
    "ConsoleAdapter",
    "MessageEvent",
    "Segment",
    "SnowLumaAdapter",
    "text_segments",
]
