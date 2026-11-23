"""聊天里的明确撤回委托。只分派一个固定动作，不把原始网关工具交给聊天者。"""

from __future__ import annotations

import re

from ..adapters.base import BaseAdapter, MessageEvent
from ..decision import BOT_SENDER_ID
from .service import GatewayService

# 匹配整句而非搜关键词：「不要撤回这条」「他说过撤回这条」都不是委托。
_POLITE = r"(?:麻烦|请问|请)?你?(?:能不能|能否|可不可以|可以)?(?:帮我|帮忙)?"
_TARGET = (r"(?:(?:你|我|他|她|对方|自己)(?:自己)?(?:刚刚|刚才|刚|之前)?(?:发的|发出的|说的|的))?"
           r"(?:刚刚|刚才)?(?:这|那|上|前|最后|最近)?一?(?:条|句|张)?(?:消息|信息|发言|话|图片|图)?")
_RECALL = re.compile(rf"{_POLITE}(?:把(?P<before>{_TARGET})撤回|撤回(?:一下)?(?P<after>{_TARGET}))"
                     r"(?:一下)?(?:吧|呗|好吗|可以吗|行吗|吗)?[，,。.!！?？]*(?:谢谢[。.!！]?)?")
# 看起来像委托但目标描述超出可核定范围（如「撤回倒数第二条」），宁可询问，不执行。
_MESSAGE_NOUN = r"(?:条|句|消息|发言|图片)"
_OTHER_TARGET = re.compile(rf"{_POLITE}(?:撤回.*{_MESSAGE_NOUN}.*|把.*{_MESSAGE_NOUN}.*撤回.*)")
_CLARIFY = "要撤回哪条消息？请回复那条消息，再叫我撤回。"


async def route_recall(gateway: GatewayService, adapter: BaseAdapter | None, event: MessageEvent,
                       *, hard_trigger: str | None, owner: bool) -> str | None:
    """返回要发回原会话的回执；不是对 bot 的明确委托则交回正常聊天。"""
    if not (hard_trigger or owner):
        return None
    if event.is_group and event.mentions and not event.mentions_bot:
        return None
    text = event.text.strip()
    if '撤回' not in text or len(text) > 256:
        return None
    if event.mentions_bot:
        # 只剥平台投影在句首的称呼，不从引用或转述中抽指令。
        text = re.sub(r"^@\S+\s+", "", text, count=1)
    if hard_trigger and hard_trigger.startswith('name:'):
        text = re.sub('^' + re.escape(hard_trigger[5:]), '', text, count=1, flags=re.I).lstrip('，,：: ')
    text = ''.join(text.split())
    match = _RECALL.fullmatch(text)
    if not match:
        return _CLARIFY if _OTHER_TARGET.fullmatch(text) else None

    store = gateway.store
    scope = dict(platform=event.platform, chat_type=event.chat_type, chat_id=event.chat_id)
    if event.reply_to_id:
        # 显式引用查不到就停，不能悄悄替换成另一条；也不使用本条请求自身的 ID。
        target = store.message_by_platform_id(platform_msg_id=event.reply_to_id, **scope)
    else:
        description = match['before'] or match['after'] or ''
        if description.startswith(('我', '他', '她', '对方', '自己')):
            return _CLARIFY
        # 没有引用时只能定位到最新一条，不能把「这张图」替换成最新的文字。
        if any(word in description for word in ('之前', '图片', '张', '图')):
            return _CLARIFY
        if owner or description.startswith('你'):
            rows = store.recent_messages(**scope, sender_id=BOT_SENDER_ID, limit=1)
        else:
            # 单独一个 @ 只确定听者。紧接 bot 的上一条时，「这条」才有明确指向。
            rows = store.recent_messages(**scope, real_only=True, limit=2)
            rows = rows[:1] if len(rows) == 2 and rows[-1].id == event.raw_message_id else []
        target = rows[-1] if rows and rows[-1].sender_id == BOT_SENDER_ID else None
    if (not target or not target.platform_msg_id or event.raw_message_id is None
            or target.id >= event.raw_message_id):
        return _CLARIFY
    return await gateway.call(adapter, 'delete_msg', {'message_id': target.platform_msg_id},
                              chat_id=event.chat_id, chat_type=event.chat_type, source='chat')
