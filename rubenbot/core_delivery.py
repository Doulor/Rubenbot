"""发送航班、分段投递、出站记账与后置钩子。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import MessageEvent


@dataclass
class ReplyFlight:
    """持锁的首稿在飞期间，接走会使该草稿过时的入站消息。"""

    primary: str
    updates: list[MessageEvent] = field(default_factory=list)
    collecting: bool = True
    used_action: bool = False
    isolated_sender: bool = False
    computer_failed: bool = False

    def take(self, event: MessageEvent, direct: bool) -> bool:
        if not self.collecting or (not self.updates and event.sender_id != self.primary and not direct):
            return False
        self.updates.append(event)
        return True

    def begin_send(self) -> bool:
        # 从实际首段送出起不可撤回；此后到达的事件正常排到下一批。
        if self.updates:
            return False
        self.collecting = False
        return True
