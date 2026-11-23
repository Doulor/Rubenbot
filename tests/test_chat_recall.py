"""从 SnowLuma 入站复刻群聊撤回，核对平台调用、出站和两份审计。"""

import asyncio
import json
import sqlite3

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.snowluma import parse_onebot_event
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona


class ChatAdapter(RecordingAdapter):
    name = 'snowluma'

    def __init__(self):
        super().__init__()
        self.calls = []

    async def call_action(self, name, params):
        self.calls.append((name, params))

    async def send(self, chat_id, segments, *, chat_type='group'):
        await super().send(chat_id, segments, chat_type=chat_type)
        return str(300 + len(self.sent))


@pytest.fixture
def store(tmp_path):
    db = MemoryStore(tmp_path / 'recall.db')
    yield db
    db.close()


@pytest.fixture
def chat(config, store):
    config.computer.enabled = True
    config.computer.whitelist = ['snowluma:11']
    config.gateway.enabled = True
    config.gateway.enabled_conversations = ['snowluma:77']
    llm = ScriptedLLM(default='{"basis":"settled","speak":true,"intent":"操作电脑"}')
    bot = RubenBot(config, store, llm, Persona('Ruben', '一个群友'))
    adapter = ChatAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, llm


def request(text='撤回这条消息', sender=12, reply=None, *, mentions=('99',), trailing=False):
    tags = [{'type': 'at', 'data': {'qq': uid}} for uid in mentions]
    body = [{'type': 'text', 'data': {'text': text}}]
    message = body + tags if trailing else tags + body
    if reply is not None:
        message.insert(0, {'type': 'reply', 'data': {'id': reply}})
    return parse_onebot_event({'post_type': 'message', 'message_type': 'group', 'group_id': 77,
                               'user_id': sender, 'self_id': 99, 'message_id': 999, 'message': message})


def add(store, sender=BOT_SENDER_ID, mid='131', kind='group', room='77'):
    store.add_raw_message(platform='snowluma', chat_type=kind, chat_id=room, sender_id=sender,
                          sender_name='同名', text='此前发言', platform_msg_id=mid)


def test_group_mention_recalls_actual_delivery_before_computer_planner(chat, store):
    bot, adapter, llm = chat
    event = request()
    asyncio.run(bot.send_reply(event, '我刚才说错了'))
    asyncio.run(bot.handle_event(event))
    assert adapter.calls == [('delete_msg', {'message_id': '301'})]
    assert adapter.texts[-1] == '已撤回。'
    assert not llm.calls and not store.recent_computer_actions()
    audit, = store.list_gateway_audit()
    assert (audit['source'], audit['sensitive'], audit['ok']) == ('chat', False, True)
    assert store.list_decision_logs()[0]['reason'] == 'gateway_recall'


@pytest.mark.parametrize('sender,text', [(11, '撤回这条'), (12, '请把你刚才发的那条消息撤回吧')])
def test_known_implicit_target_ignores_intervening_people_and_other_rooms(chat, store, sender, text):
    bot, adapter, llm = chat
    add(store)
    add(store, sender='18', mid='132')
    add(store, mid='133', kind='private')
    add(store, mid='134', room='88')
    asyncio.run(bot.handle_event(request(text, sender)))
    assert adapter.calls == [('delete_msg', {'message_id': '131'})] and not llm.calls


@pytest.mark.parametrize('sender,reply,mentions,ok', [
    (11, '132', ('18', '99'), True),   # 回复优先于 @ 的另一人
    (12, '132', ('99',), True),        # 作者本人
    (13, '132', ('99',), False),       # 普通成员，无权撤他人的消息
    (11, None, ('99', '12'), True),    # 没有引用，明确 @ 的作者
    (13, None, ('12', '99'), False),
])
def test_member_recall_replay_and_committed_audit(chat, store, tmp_path, sender, reply, mentions, ok):
    bot, adapter, llm = chat
    add(store, sender='12', mid='132')
    add(store)
    asyncio.run(bot.handle_event(request('撤回一下这条消息', sender, reply, mentions=mentions, trailing=True)))
    assert not llm.calls and not store.recent_computer_actions()
    assert adapter.calls == ([('delete_msg', {'message_id': '132'})] if ok else [])
    assert ('已撤回' if ok else '敏感') in adapter.texts[-1]
    # 独立连接验证同一笔已提交到磁盘，不依赖服务内连接的事务或 WebUI 投影。
    with sqlite3.connect(tmp_path / 'recall.db') as conn:
        row, = conn.execute('SELECT source, action, platform, chat_type, chat_id, params, ok, sensitive, '
                            'reject_reason FROM gateway_audit').fetchall()
    assert row[:5] == ('chat', 'delete_msg', 'snowluma', 'group', '77')
    assert json.loads(row[5]) == {'message_id': '132'}
    assert row[6:] == (ok, True, '' if ok else adapter.texts[-1])


@pytest.mark.parametrize('text', ['撤回我刚发的那条', '撤回'])
def test_member_latest_own_excludes_command_and_other_conversations(chat, store, text):
    bot, adapter, llm = chat
    add(store, sender='12', mid='130')
    add(store, sender='12', mid='131')
    add(store, sender='12', mid='132', kind='private')
    add(store, sender='12', mid='133', room='88')
    add(store)
    asyncio.run(bot.handle_event(request(text)))
    assert adapter.calls == [('delete_msg', {'message_id': '131'})] and not llm.calls
    assert adapter.texts[-1] == '已撤回。'


@pytest.mark.parametrize('text,sender,reply', [('撤回这条', 12, None), ('撤回我刚发的那条', 11, None),
                                              ('撤回这条', 11, '404'), ('撤回这条', 11, '999'),
                                              ('请把倒数第二条消息撤回', 11, None), ('撤回那张图片', 11, None),
                                              ('那你撤吧', 11, None), ('撤回这个小老鼠', 11, None)])
def test_uncertain_target_asks_without_falling_back(chat, store, text, sender, reply):
    bot, adapter, llm = chat
    add(store)
    add(store, sender='18', mid='132')
    add(store, mid='404', kind='private')
    asyncio.run(bot.handle_event(request(text, sender, reply)))
    assert not adapter.calls and not llm.calls
    audit, = store.list_gateway_audit()
    assert not audit['ok'] and audit['reject_reason'] and json.loads(audit['params']) == {}
    assert '请回复那条' in adapter.texts[-1]


def test_missing_latest_delivery_id_does_not_fall_back_to_older_one(chat, store):
    bot, adapter, llm = chat
    add(store)
    add(store, mid='')
    asyncio.run(bot.handle_event(request(sender=11)))
    assert not adapter.calls and not llm.calls and '请回复那条' in adapter.texts[-1]


@pytest.mark.parametrize('text', ['不要撤回这条', '甲说「撤回这条消息」', '怎么撤回这条消息？'])
def test_discussion_is_not_a_recall_command(chat, config, store, text):
    bot, adapter, llm = chat
    config.computer.enabled = False
    add(store)
    asyncio.run(bot.handle_event(request(text)))
    assert llm.calls and not adapter.calls and not store.list_gateway_audit()


def test_owner_addressing_someone_else_not_a_bot_command(chat, store):
    bot, adapter, _ = chat
    add(store)
    asyncio.run(bot.handle_event(MessageEvent('snowluma', 'group', '77', '11', '机主', '撤回这条', mentions={'12': '乙'})))
    assert not adapter.calls and not store.list_gateway_audit()
