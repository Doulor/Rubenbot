"""平台适配器。"""

from .base import (
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    MessageEvent,
    SEGMENT_PLACEHOLDERS,
    Segment,
    as_segment,
    render_segment,
    render_segments,
    text_segments,
)
from .console import ConsoleAdapter
from .qzone import QzoneClient
from .snowluma import SnowLumaAdapter

__all__ = [
    "BaseAdapter",
    "CapabilityError",
    "CapabilityNotSupported",
    "ConsoleAdapter",
    "MessageEvent",
    "QzoneClient",
    "SEGMENT_PLACEHOLDERS",
    "Segment",
    "SnowLumaAdapter",
    "as_segment",
    "render_segment",
    "render_segments",
    "text_segments",
]
