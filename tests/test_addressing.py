"""群内指向的离线回放：策划读事实决定是否接话，正文和记忆仍属于发言人。"""

import asyncio
import time

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.snowluma import parse_onebot_event
from rubenbot.core import RubenBot
from rubenbot.decision import MergedRound
from rubenbot.persona import Persona


def event(text, **kwargs):
    return MessageEvent('recording', 'group', 'room', 'A', '阿甲', text, **kwargs)


def test_argon_peer_question_and_shared_mention(config, store):
    llm = ScriptedLLM({'fast': ['{"speak":false,"intent":"沉默"}'] * 2, 'chat': ['我用过']})
    bot = RubenBot(config, store, llm, Persona('Ruben', '我是群友。'))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    store.upsert_identity('recording', 'B', group_id='room', name='阿乙')
    asyncio.run(bot.handle_event(event('Argon 和 Ruben 哪个更好用？', mentions={'B': ''})))
    assert not adapter.sent and [c['task'] for c in llm.calls] == ['gen_stage1']
    prompt = llm.calls[-1]['messages'][-1]['content']
    assert '本轮合并指向：发言人：阿甲（A）；@ 目标：阿乙（B）；回复对象：无' in prompt
    assert '无 @ 提及' not in prompt
    row, = store.recent_messages('room')
    assert row.sender_id == 'A' and row.mentions == {'B': '阿乙'}
    assert [s.sender_id for s in store.pending_senders()] == ['A']
    asyncio.run(bot.handle_event(event('你们用过吗？', mentions={'B': '阿乙', '__bot__': ''})))
    assert adapter.texts == ['我用过']
    generator = '\n'.join(m['content'] for m in llm.chat_messages[0])
    assert '（@ 目标：阿乙（B）、我）' in generator and '用群友的名字区分人' in generator


def test_scoped_reply_resolution_and_dedup(config, store):
    now = time.time()
    bot = RubenBot(config, store, ScriptedLLM(), Persona('Ruben', '群友'))
    for room, kind in [('elsewhere', 'group'), ('room', 'private')]:
        store.add_raw_message(platform='recording', chat_id=room, chat_type=kind, sender_id='__bot__',
                              text='其他会话的内容', platform_msg_id='m', ts=now)
    quoted = event('你说的是这个吗？', reply_to_id='m')
    assert not bot._is_duplicate(event('新消息', platform_msg_id='m'), now)
    asyncio.run(bot._record(quoted, now))
    assert not quoted.reply_sender_id and not bot._reply_target_section(quoted)
    messages = asyncio.run(bot.build_context(quoted, now=now))
    assert all('其他会话的内容' not in m['content'] for m in messages)
    store.add_raw_message(platform='recording', chat_id='room', sender_id='B', sender_name='阿乙',
                          text='这个', platform_msg_id='m', ts=now)
    asyncio.run(bot._record(quoted, now + 1))
    assert quoted.reply_sender_id == 'B'
    assert bot._is_duplicate(event('重复', platform_msg_id='m'), now)
    assert store.recent_messages('room', limit=1)[0].reply_sender_id == 'B'


def test_merge_preserves_address_facts_without_inferring_from_text():
    first = event('一问', mentions={'B': '阿乙'}, reply_to_id='m1', reply_sender_id='C')
    second = event('@不存在 算了', reply_to_id='m2')
    merged = MergedRound('recording', 'room', 'group', 'A', '阿甲', [first, second]).to_event()
    assert merged.mentions == {'B': '阿乙'}
    assert merged.reply_to_id == 'm2' and not merged.reply_sender_id
    assert not merged.mentions_bot


def test_cq_mentions_and_reply_author_are_separate():
    msg = parse_onebot_event({'post_type': 'message', 'message_type': 'group', 'group_id': 18,
                              'user_id': 1, 'self_id': 9, 'message': '[CQ:at,qq=2,name=乙][CQ:reply,id=77,qq=9]好'})
    assert msg.mentions == {'2': '乙'} and msg.mentions_bot
    assert (msg.reply_to_id, msg.reply_sender_id) == ('77', '__bot__')
