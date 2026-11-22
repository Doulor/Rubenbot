"""机主即时通道：只按真实账号查其他场合的近期可用证据。"""

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import ComputerConfig
from rubenbot.memory.owner_recent import MAX_SNIPPETS, MAX_SNIPPET_CHARS, owner_recent_snippets
from rubenbot.memory.owner_statement import is_owner_self_statement

NOW = 1_800_000_000
PERSON = 'console:u:1'
OWNER = ComputerConfig(whitelist=[PERSON, 'console:other'])
QUESTION = '我今晚吃的啥你知道吗'
QUOTE = '我今晚吃的是鸭腿你知道吗'


def event(**overrides):
    fields = dict(platform='console', sender_id='u:1', sender_name='同名', chat_id='42',
                  chat_type='private', text=QUESTION)
    return MessageEvent(**(fields | overrides))


def add(store, text=QUOTE, **overrides):
    fields = dict(platform='console', sender_id='u:1', sender_name='同名', chat_id='42',
                  chat_type='group', text=text, ts=NOW - 60)
    return store.add_raw_message(**(fields | overrides))


def hits(store, target=None, **opts):
    return owner_recent_snippets(store, target or event(), computer=OWNER, now=NOW, **opts)


def test_real_account_and_place_and_message_time(store):
    add(store, ts=NOW - 86400)
    add(store, '我今晚吃了米饭', ts=NOW - 86400.01)
    add(store, '我今晚吃了土豆', ts=NOW + 1)
    add(store, '我今晚吃了牛肉', chat_type='private')
    add(store, '我今晚吃了肉饼', platform='other')
    add(store, '我今晚吃了黄瓜', sender_id='other')
    add(store, '我今晚吃了白菜', sender_id='guest')
    add(store, '我今晚吃了鱿鱼', sender_id='__bot__')
    add(store, '不相关', visual_text='我今晚吃了烧鸡')
    assert hits(store) == [QUOTE]
    assert hits(store, event(sender_id='guest', raw={'sender_id': 'u:1'}, text='机主说' + QUESTION)) == []
    assert hits(store, event(platform='other')) == []
    assert hits(store, event(chat_type='unknown')) == []


def test_owner_private_to_other_group_without_question_echo(store):
    add(store, chat_type='private')
    add(store, '我今晚吃的啥你知道吗', chat_type='private')
    add(store, '我今晚吃了海苔', chat_type='private')
    add(store, '我今晚吃了水果', chat_id='elsewhere')
    target = event(chat_type='group', text='我今晚吃了水果')
    assert hits(store, target, query=QUESTION, exclude_texts=['我今晚吃了海苔']) == [QUOTE]
    assert hits(store, target, query='天文望远镜') == []


@pytest.mark.parametrize('mode', ['private', 'norecord'])
def test_hidden_and_purged_history_not_revived(store, mode):
    add(store)
    store.set_privacy_mode(PERSON, mode)
    add(store, '我今晚吃了鸭腿')
    assert hits(store) == []
    store.set_privacy_mode(PERSON, 'normal')
    assert hits(store) == []
    add(store)
    assert hits(store) == [QUOTE]
    store.purge_person(PERSON)
    assert hits(store) == []


def test_secrecy_and_follow_up_seal_in_source_private_chat(store):
    target = event(chat_type='group', chat_id='target')
    add(store, '我吃了鸭腿', chat_type='private')
    assert hits(store, target, query='鸭腿') == ['我吃了鸭腿']
    add(store, '刚才那句保密', chat_type='private', ts=NOW - 59)
    add(store, '我吃了鸭腿，' + '很香' * 100 + '别告诉别人', chat_id='elsewhere')
    assert hits(store, target, query='鸭腿') == []
    add(store, '我今晚吃了鸭腿配面', chat_type='private', ts=NOW - 58)
    add(store, '请保密，我正在给鸭腿换包装', chat_type='private', ts=NOW - 57)
    assert hits(store, target, query='鸭腿') == ['我今晚吃了鸭腿配面']


def test_followup_does_not_skip_over_already_sealed_raw(store):
    target = event(chat_type='group', chat_id='target')
    add(store, '我今晚吃了南瓜', chat_type='private')
    hidden = add(store, '我今晚吃了丝瓜', chat_type='private')
    add(store, '刚才那句保密', chat_type='private')
    store.block_raw_recall([hidden])
    assert hits(store, target) == ['我今晚吃了南瓜']


def test_linked_sensitive_event_blocks_only_its_line(store):
    hidden = add(store, '我今晚吃了丝瓜')
    add(store, '我今晚吃了南瓜')
    store.add_event(PERSON, '受保护的具体事件', source_chat='console:42', source_raw_id=hidden,
                    sensitivity='high', now=NOW - 55)
    assert hits(store) == ['我今晚吃了南瓜']


def test_historical_sealed_batch_and_unlinked_event(store):
    first = add(store)
    second = add(store, '我今晚吃了蔬菜')
    add(store, '我今晚吃了花生')
    store.add_memory(PERSON, '已封存', origin_chat='console:42', sensitivity='sealed',
                     source_ref=f'console:42:{first}-{second}', now=NOW - 59)
    assert hits(store) == ['我今晚吃了花生']
    store.add_event(PERSON, '无原话索引的旧事件', source_chat='console:42', sensitivity='high', now=NOW - 55)
    assert hits(store) == []
    add(store, '我今晚吃了南瓜', ts=NOW - 54)
    assert hits(store) == ['我今晚吃了南瓜']


def test_old_private_secret_does_not_poison_new_dinner(store):
    target = event(chat_type='group', chat_id='elsewhere')
    store.add_event(PERSON, '从前的事', source_chat='console:42', sensitivity='sealed', now=NOW - 30 * 86400)
    add(store, chat_type='private')
    assert hits(store, target) == [QUOTE]


def test_pending_raw_uses_same_high_sensitivity_rules_as_profile(store):
    add(store, '我最近在吃抗抑郁药', chat_type='private')
    assert hits(store, event(chat_id='elsewhere', chat_type='group', text='你最近吃什么药')) == []


def test_existing_deny_for_destination_not_bypassed_by_raw(store, monkeypatch):
    for _ in range(60):
        add(store)
    store.set_ruling(PERSON, 'health@private', 'deny')
    original, calls = store.list_rulings, []

    def count_rulings(person_id):
        calls.append(person_id)
        return original(person_id)

    monkeypatch.setattr(store, 'list_rulings', count_rulings)
    assert hits(store) == []
    assert calls == [PERSON]


def test_bounded_deduplicated_and_scored(store):
    for dish in ('红豆', '红枣', '红薯', '红糖'):
        add(store, '我今晚吃了' + dish)
    add(store, '我今晚吃了' + '葡萄' * 200)
    add(store, '我今晚吃了红糖。')
    add(store, '我喜欢电影你知道吗', ts=NOW)
    result = hits(store)
    assert len(result) == MAX_SNIPPETS == 3
    assert all(len(s) <= MAX_SNIPPET_CHARS == 160 for s in result)
    assert sum('红糖' in s for s in result) == 1
    assert any(s.endswith('…') for s in result)
    assert all('电影' not in s for s in result)


@pytest.mark.parametrize('text', ['其实我今晚吃的是鸭腿', '今天我吃了鸭腿', '@123 我吃了鸭腿', QUOTE])
def test_clear_self_statements(text):
    assert is_owner_self_statement(text)


@pytest.mark.parametrize('text', [QUESTION, '小张说我今晚吃的是鸭腿', '我觉得小张今晚吃的是鸭腿',
                                  '我听小张说今晚吃鸭腿', '我朋友今晚吃了鸭腿', '我今晚吃了鸭腿，这要保密'])
def test_ambiguous_or_sealed_is_never_promoted(text):
    assert not is_owner_self_statement(text, allow_implicit=True)


def test_implicit_self_requires_verified_extraction():
    assert not is_owner_self_statement('今晚吃的是鸭腿')
    assert is_owner_self_statement('今晚吃的是鸭腿', allow_implicit=True)
    assert not is_owner_self_statement('今晚小张吃的是鸭腿', allow_implicit=True)
