"""核心隐私模块：裁定与封存，命令不依赖插件。"""

from .gate import PrivacyGate, ReviewItem
from .commands import PrivacyCommands
from .sealing import register_message

__all__ = ["PrivacyGate", "ReviewItem", "PrivacyCommands", "register_message"]
