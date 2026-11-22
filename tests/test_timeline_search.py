"""时间线查账与近况。原机主的隐私边界回归用例在显式开启裁定后运行。"""

import asyncio
from functools import partial

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.memory.timeline import MAX_SNIPPETS, MAX_SNIPPET_CHARS, read_timeline, recent_timeline, search_timeline
from rubenbot.privacy import PrivacyGate
from rubenbot.memory.owner_statement import is_owner_self_statement

NOW = 1_800_000_000
PERSON = 'console:u:1'
OWNERS = {PERSON, 'console:other'}
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


def hits(store, target=None, *, privacy=True, query=None, exclude_texts=(), limit=MAX_SNIPPETS):
    store.privacy_enabled = privacy
    target = target or event()
    speaker = f'{target.platform}:{target.sender_id}'
    allow = None
    if privacy:
        allow = partial(PrivacyGate(store, None).review_timeline, speaker=speaker,
                        audience_now=('private' if target.chat_type == 'private' else f'group:{target.chat_id}'),
                        owner_person_ids={speaker} & OWNERS, now=NOW,
                        target_platform=target.platform, target_chat_id=target.chat_id)
    rows = asyncio.run(search_timeline(store, target.text if query is None else query, exclude_chat=target,
                                       exclude_texts=exclude_texts, raw_only=True, limit=limit, allow=allow, now=NOW))
    return [r.excerpt() for r in rows]


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
    # 与旧机主专线不同：任何人、任何在场的平台都可成为来源，不从 raw 伪造发送人身份。
    expected = {QUOTE, '我今晚吃了米饭', '我今晚吃了肉饼', '我今晚吃了黄瓜', '我今晚吃了白菜'}
    assert set(hits(store, privacy=False, limit=20)) == expected
    guest = event(sender_id='guest', raw={'sender_id': 'u:1'}, text='机主说' + QUESTION)
    assert set(hits(store, guest, privacy=False, limit=20)) == expected
    assert '我今晚吃了牛肉' in hits(store, event(platform='other'), privacy=False, limit=20)
    assert all('土豆' not in r for r in hits(store, privacy=False))  # 未来消息不应召回
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
    store.privacy_enabled = True
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


def test_recent_fallback_uses_one_row_per_place_and_real_sources(store, monkeypatch):
    for i in range(320):
        add(store, f'流水{i}', ts=NOW - 321 + i)
    store.add_self_event('我复述的是别人的近况', source_chat='console:42', audience='group:42', now=NOW - 1)
    store.upsert_identity('console', '42', kind='group', name='周末聚会')
    calls = []
    read = store.timeline_rows

    def count(**kw):
        rows = read(**kw)
        calls.append(len(rows))
        return rows

    monkeypatch.setattr(store, 'timeline_rows', count)
    lines = asyncio.run(recent_timeline(store, event(), now=NOW))
    assert len(lines) == 1
    assert all(word in lines[0] for word in ['流水319', '群聊', '周末聚会', 'console:42', '同名'])
    assert calls == [1]


def test_read_and_recall_keep_historical_place_type_after_trim(store):
    rid = add(store, '这条流水将被淘汰', chat_type='private', processed=True)
    store.add_event(PERSON, '古老的赤松游记', source_chat='console:42', source_raw_id=rid,
                    audience='owner', ts=NOW - 30 * 86400)
    add(store, '同场合新消息', chat_type='private', ts=NOW - 1, processed=True)
    assert store.trim_raw(keep_per_chat=1) == 1
    store.add_event(PERSON, '赤松新游记', ts=NOW, audience='group:历史记录')
    rows = asyncio.run(search_timeline(store, '赤松', now=NOW, person_id=PERSON))
    assert [(r.text, r.chat_type) for r in rows] == [('赤松新游记', 'group'), ('古老的赤松游记', 'private')]
    assert 'console:42' in rows[-1].line()
    assert '历史记录' in rows[0].line()
    assert not asyncio.run(recent_timeline(store, event(), now=NOW))
    assert [r.text for r in read_timeline(store, now=NOW)] == ['赤松新游记', '同场合新消息', '古老的赤松游记']


def test_multiline_quote_is_verbatim_but_recent_event_stays_one_line(store):
    quote = '晚餐选项：\n意面  或者米饭'
    add(store, quote)
    hit, = asyncio.run(search_timeline(store, '晚餐', raw_only=True, now=NOW))
    assert hit.line().endswith(' ' + quote)
    recent, = asyncio.run(recent_timeline(store, event(), now=NOW))
    assert recent.endswith(' 晚餐选项： 意面 或者米饭') and '\n' not in recent


def test_search_can_match_explicit_account_without_text_overlap(store):
    add(store, '码头停着白帆船')
    add(store, '朱红色的桥', platform='other', ts=NOW)
    add(store, '橙黄的日落', sender_id='other', ts=NOW)
    rows = asyncio.run(search_timeline(store, '听听看', mentioned={PERSON}, raw_only=True, now=NOW))
    assert [(row.person_id, row.text) for row in rows] == [(PERSON, '码头停着白帆船')]


def test_raw_retention_uses_full_conversation_identity(store):
    for platform, kind in [('console', 'private'), ('console', 'group'), ('other', 'group')]:
        for n in range(2):
            add(store, f'第{n}次探访', platform=platform, chat_type=kind, ts=NOW - 1 + n, processed=True)
    assert store.trim_raw(1) == 3
    remaining = read_timeline(store, now=NOW)
    assert {(r.platform, r.chat_type, r.text) for r in remaining} == {
        ('console', 'private', '第1次探访'), ('console', 'group', '第1次探访'), ('other', 'group', '第1次探访')}


def test_date_budget_and_keyword_order(store):
    add(store, '秋游，红枫落叶配晨雾。', chat_id='details', ts=NOW - 1)
    add(store, '只提红枫', chat_id='recent', ts=NOW)
    old = add(store, '秋游清单：红枫落叶配晨雾。', chat_id='old', ts=NOW - 200 * 86400)
    query = '红枫落叶配晨雾'
    result = asyncio.run(search_timeline(store, query, now=NOW))
    assert [r.chat_id for r in result] == ['details', 'old', 'recent']
    assert result[1].id == old
    lines = asyncio.run(recent_timeline(store, event(), char_limit=80, now=NOW))
    assert lines and 'recent' in lines[0] and 'old' not in '\n'.join(lines)
    assert len('\n'.join(lines)) <= 80


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
