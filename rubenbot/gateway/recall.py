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
# 未匹配到明确目标的命令只澄清，不交给电脑策划，也不按昵称搜消息。
_OTHER_TARGET = re.compile(rf"{_POLITE}(?:撤回(?:这|那|.*(?:条|句|消息|发言|图片)).*|把.*(?:条|句|消息|发言|图片).*撤回.*)")
_FOLLOWUP = re.compile(r"(?:那|就)?你?撤(?:吧|呗)[。！!]?")
_CLARIFY = "要撤回哪条消息？请回复那条消息，再叫我撤回。"


def _target(store, event, match, owner):
    scope = dict(platform=event.platform, chat_type=event.chat_type, chat_id=event.chat_id)
    if event.reply_to_id:
        # 引用即使查不到也不能换目标。
        return store.message_by_platform_id(platform_msg_id=event.reply_to_id, **scope)
    if not match:
        return None
    description = match['before'] or match['after'] or ''
    if any(word in description for word in ('之前', '图片', '张', '图')):
        return None
    mentions = event.mentions.keys() - {BOT_SENDER_ID}
    if mentions:
        # @ 指向人而非任意一条历史消息；只取一个明确账号在本会话的最近发言。
        if len(mentions) != 1 or 'all' in mentions or description.startswith(('你', '我', '自己')):
            return None
        sender = next(iter(mentions))
    elif description.startswith(('他', '她', '对方')):
        return None
    elif description.startswith(('我', '自己')) or (not description and not owner):
        sender = event.sender_id
    elif owner or description.startswith('你'):
        sender = BOT_SENDER_ID
    else:
        # 无作者指向的「这条」只在紧接 bot 时成立。
        sender = None
    rows = store.recent_messages(**scope, sender_id=sender, real_only=True, limit=2)
    # 本条委托已经写入流水；不要撤回命令本身，也不跳过缺平台号的最近一条。
    prev = next((row for row in reversed(rows) if row.id < event.raw_message_id), None)
    return prev if prev and (sender or prev.sender_id == BOT_SENDER_ID) else None


async def route_recall(gateway: GatewayService, adapter: BaseAdapter | None, event: MessageEvent,
                       *, hard_trigger: str | None, owner: bool) -> str | None:
    """返回要发回原会话的回执；不是对 bot 的明确委托则交回正常聊天。"""
    if not (hard_trigger or owner):
        return None
    if event.is_group and event.mentions and not event.mentions_bot:
        return None
    text = event.text.strip()
    if '撤' not in text or len(text) > 256:
        return None
    if event.mentions_bot and any(s.type == 'at' for s in event.segments):
        # @ 可能在句中或句尾。去掉平台寻址段，保留正文；不把文本中的 @昵称 当账号。
        text = ''.join(s.render() for s in event.segments if s.type != 'at').strip()
    if hard_trigger and hard_trigger.startswith('name:'):
        text = re.sub('^' + re.escape(hard_trigger[5:]), '', text, count=1, flags=re.I).lstrip('，,：: ')
    text = ''.join(text.split())
    match = _RECALL.fullmatch(text)
    pointing = _FOLLOWUP.fullmatch(text)
    if not (match or pointing or _OTHER_TARGET.fullmatch(text)):
        return None

    target = None
    if event.raw_message_id is not None and (match or pointing):
        target = _target(gateway.store, event, match, owner)
    if not target or not target.platform_msg_id or target.id >= event.raw_message_id:
        gateway.gatekeeper.audit(action='delete_msg', source='chat', chat_key=f'{event.platform}:{event.chat_id}',
                                 chat_type=event.chat_type, reject_reason='撤回目标不明确', sensitive=True)
        return _CLARIFY
    return await gateway.call(adapter, 'delete_msg', {'message_id': target.platform_msg_id},
                              chat_id=event.chat_id, chat_type=event.chat_type, source='chat',
                              requester=event.sender_id, requester_owner=owner)
