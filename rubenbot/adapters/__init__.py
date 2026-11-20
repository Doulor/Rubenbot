"""平台适配器。"""

from .base import (
    ActionNotSupported,
    SEND_STICKER,
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    MessageEvent,
    PlatformEventCallback,
    SEGMENT_PLACEHOLDERS,
    Segment,
    as_segment,
    render_segment,
    render_segments,
    sticker_native_ref,
    text_segments,
)
from .console import ConsoleAdapter
from .platform import PlatformEvent
from .qzone import QzoneClient
from .snowluma import SnowLumaAdapter

__all__ = [
    "ActionNotSupported",
    "BaseAdapter",
    "CapabilityError",
    "CapabilityNotSupported",
    "ConsoleAdapter",
    "MessageEvent",
    "PlatformEvent",
    "PlatformEventCallback",
    "QzoneClient",
    "SEGMENT_PLACEHOLDERS",
    "SEND_STICKER",
    "Segment",
    "SnowLumaAdapter",
    "as_segment",
    "render_segment",
    "render_segments",
    "sticker_native_ref",
    "text_segments",
]
