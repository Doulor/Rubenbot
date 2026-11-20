"""事件通道的统一事件模型（平台无关）。

适配器把平台推送的非消息事件（通知、申请）归一化成 :class:`PlatformEvent`；
核心层只认这里的字段，不出现任何 OneBot/QQ 专有词。原始载荷只留一份
紧凑摘要（``raw``），避免把整包 JSON 落库。

事件种类（``kind``）与去重/路由口径：

- ``notice``：平台通知（戳一戳、入群/退群、禁言、撤回、管理员变动……），
  ``type`` 是通知子类，``sub_type`` 进一步细分；
- ``request``：需要人处理的申请（加好友、入群/邀请），核心层会给它
  ``pending`` 状态，WebUI 可据此显示「红点」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 事件种类
KIND_NOTICE = "notice"
KIND_REQUEST = "request"

# 路由结论（第二层闸）
ROUTE_JUDGE = "judge"  # 必判
ROUTE_CONDITIONAL = "conditional"  # 条件判
ROUTE_RECORD = "record"  # 只记录


@dataclass
class PlatformEvent:
    """一条归一化后的平台事件。

    ``actor_id`` / ``actor_name`` 是事件的发起人（谁戳的、谁申请的、谁进群）；
    ``target_id`` 是被作用的人或消息（被戳的、被撤回消息的发送者、被禁言的人）。
    ``conversation_type`` 与 ``conversation_id`` 表示事件发生的会话：群事件
    用群 id，好友申请这类没有群的事件用 ``private`` + 申请人 id。
    """

    platform: str
    kind: str
    type: str
    sub_type: str = ""
    actor_id: str = ""
    actor_name: str = ""
    target_id: str = ""
    target_name: str = ""
    conversation_type: str = "private"
    conversation_id: str = ""
    # 一句话的事件描述（供模型快速判断、也用于合并后的展示）
    description: str = ""
    # 原始载荷摘要（截断后的少量关键字段），供排查问题
    raw: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
    # 平台侧事件 id / 申请标记（OneBot 的 flag），用于申请去重与后续处理
    platform_event_id: str = ""
    # 申请类事件的 pending 标记（落库时写 1）
    pending: bool = False
    # 事件是否直接冲着 bot 来的（戳一戳戳到 bot 等）；由适配器按 self_id 判定，
    # 核心层不关心平台 id 怎么表示
    targets_bot: bool = False

    @property
    def conversation(self) -> str:
        """``platform:conversation_id`` 形式的会话键。"""
        return f"{self.platform}:{self.conversation_id}"

    @property
    def is_request(self) -> bool:
        return self.kind == KIND_REQUEST

    @property
    def is_group_event(self) -> bool:
        return self.conversation_type == "group"

    def self_generated(self, self_id: str | None) -> bool:
        """是否 bot 自己引发的（actor == bot 自己）；入口据此过滤，不自言自语。"""
        if not self_id or not self.actor_id:
            return False
        return str(self.actor_id) == str(self_id)

    def dedupe_key(self) -> tuple[str, str, str]:
        """同类合并用的键：同一会话 + 同类事件。"""
        return (self.conversation, self.kind, self.type)
