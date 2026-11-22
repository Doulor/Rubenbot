"""实体账只认本群有出处的资料；本地计数与后台重放共用唯一键。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.entities import EntityCandidate, EntityLedger, FamiliarityTag
from rubenbot.memory.extract import MemoryExtractor
from rubenbot.memory.store import MemoryStore


@pytest.fixture
def store():
    with_store = MemoryStore(':memory:')
    yield with_store
    with_store.close()


def message(store, text='', *, group='g', sender='a', **kwargs):
    raw_id = store.add_raw_message(platform='qq', chat_id=group, sender_id=sender, text=text, **kwargs)
    return store.raw_message_by_id(raw_id)


def observe(ledger, row, candidate):
    return ledger.observe_message(platform='qq', chat_id='g', message=row, candidates=[candidate])


def test_exact_evidence_and_idempotent_alias_counts(store):
    ledger = EntityLedger(store)
    row = message(store, visual_text='星驰（也叫小星）是本站名', platform_msg_id='p42', ts=4)
    candidate = EntityCandidate('星驰', '星驰（也叫小星）是本站名', 'p42', ('p42',), ('小星', '模型脑补'))
    record, = observe(ledger, row, candidate)
    assert record.gloss == row.visual_text and record.gloss_msg_id == 'p42'
    assert record.aliases == ('小星',)
    row2 = message(store, '小星要关站了', sender='b', ts=6)
    assert ledger.familiarity('qq', 'g', row2.text) == [FamiliarityTag('星驰', 1, True)]
    ledger.record_known_mentions(platform='qq', chat_id='g', message=row2)
    observe(ledger, row2, EntityCandidate('小星', '永久关闭'))
    observe(ledger, row, candidate)
    record, = ledger.list_entities('qq', 'g')
    assert ledger.lookup('qq', 'g', '小星') == record
    assert (record.occurrence_count, record.first_seen, record.last_seen) == (2, 4, 6)
    assert store.search_raw_messages('本站名', platform='qq', chat_id='g') == [row]
    assert store.search_raw_messages('%', platform='qq', chat_id='g') == []


def test_bot_messages_never_contribute_to_entity_ledger(store):
    ledger = EntityLedger(store)
    human = message(store, '有人听过星驰吗？', sender='a', ts=1)
    before, = observe(ledger, human, EntityCandidate('星驰'))

    # 即使重放到未标为已处理的 bot 流水，也不能把自述变成提及、别名或说明。
    bot = message(store, '星驰也叫小星，我用过', sender='__bot__', ts=2)
    ledger.record_known_mentions(platform='qq', chat_id='g', message=bot)
    assert not observe(ledger, bot, EntityCandidate('星驰', bot.text, aliases=('小星',)))

    assert ledger.lookup('qq', 'g', '星驰') == before
    assert ledger.lookup('qq', 'g', '小星') is None


@pytest.mark.parametrize('context', ['private', 'other_group', '__bot__', '__tool__', 'privacy', 'norecord'])
def test_ineligible_source_cannot_seed_entity_or_supply_gloss(store, context):
    ledger = EntityLedger(store)
    row = message(store, '星驰今天关站', sender='a', platform_msg_id='p1')
    other = message(store, '星驰是旧站', sender=context, group='g2' if context == 'other_group' else 'g',
                    chat_type='private' if context == 'private' else 'group', platform_msg_id='p2')
    if context in {'privacy', 'norecord'}:
        store.set_privacy_mode('qq:' + context, 'private' if context == 'privacy' else 'norecord')
    candidate = EntityCandidate('星驰', '星驰是旧站', 'p2')
    assert not observe(ledger, other, candidate)
    assert not observe(ledger, row, candidate)[0].gloss
    assert not observe(ledger, row, EntityCandidate('未出现'))
    assert not ledger.familiarity('qq', 'g2', '星驰')
    assert not ledger.familiarity('tg', 'g', '星驰')


@pytest.mark.parametrize('action', ['purge', 'private', 'norecord'])
def test_forget_withdraws_gloss_alias_and_contribution(store, action):
    ledger = EntityLedger(store)
    row = message(store, visual_text='星驰又名小星，是电影站', sender='a', ts=1)
    observe(ledger, row, EntityCandidate('星驰', '是电影站', aliases=('小星',)))
    row2 = message(store, '我看到星驰了', sender='b', ts=2)
    ledger.record_known_mentions(platform='qq', chat_id='g', message=row2)
    if action == 'purge':
        store.purge_person('qq:a')
    else:
        store.set_privacy_mode('qq:a', action)
        store.set_privacy_mode('qq:a', 'normal')
    assert not observe(ledger, row, EntityCandidate('星驰', '是电影站'))
    assert not store.set_raw_visual_text(row.id, '又落地了')
    assert not store.search_raw_messages('电影站', platform='qq', chat_id='g')
    assert not ledger.lookup('qq', 'g', '小星')
    record = ledger.lookup('qq', 'g', '星驰')
    assert (record.gloss, record.gloss_msg_id, record.occurrence_count, record.first_seen) == ('', '', 1, 2)


def test_rare_entity_outside_display_page_tagged_in_one_query(store):
    ledger = EntityLedger(store)
    names = ['星驰', *(f'项目{i:03}' for i in range(205))]
    for i, name in enumerate(names):
        row = message(store, name, ts=i)
        observe(ledger, row, EntityCandidate(name))
    assert '星驰' not in {record.canonical_name for record in ledger.list_entities('qq', 'g')}
    statements = []
    store._conn.set_trace_callback(statements.append)
    assert ledger.familiarity('qq', 'g', '星驰关站') == [FamiliarityTag('星驰', 1, False)]
    assert len(statements) == 1


def test_same_id_private_and_group_are_separate_extraction_batches(store):
    from conftest import ScriptedLLM

    message(store, '私聊专名', chat_type='private')
    message(store, '群聊专名')
    llm = ScriptedLLM({'extract': ['{}', '{}']})
    worker = MemoryExtractor(llm, store, MemoryConfig())
    asyncio.run(worker.maybe_extract(force=True))
    inputs = [c['messages'][-1]['content'] for c in llm.calls if c['role'] == 'extract']
    assert sorted(('私聊专名' in text, '群聊专名' in text) for text in inputs) == [(False, True), (True, False)]


def test_caption_arriving_during_extract_stays_pending_until_seen(store):
    ledger = EntityLedger(store)
    row = message(store, '[图片]')

    class VisionRace:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            assert kwargs['role'] == 'extract'
            if self.calls == 1:
                assert '星驰' not in messages[-1]['content']
                assert store.set_raw_visual_text(row.id, '图上写着星驰')
                return '{}'
            assert '图上写着星驰' in messages[-1]['content']
            return json.dumps({'entities': [{'name': '星驰', 'message_ids': [row.id]}]}, ensure_ascii=False)

    llm = VisionRace()
    extractor = MemoryExtractor(llm, store, MemoryConfig())
    for _ in range(2):
        sender, = store.pending_senders()
        assert not asyncio.run(extractor.extract_sender(sender)).error
    assert not store.set_raw_visual_text(row.id, '图上写着星驰')
    assert store.pending_senders() == [] and llm.calls == 2
    assert ledger.familiarity('qq', 'g', '星驰') == [FamiliarityTag('星驰', 1, False)]


def test_existing_alias_cited_as_new_canonical_and_late_vision_privacy(store):
    ledger = EntityLedger(store)
    observe(ledger, message(store, '小星'), EntityCandidate('小星'))
    row = message(store, '星驰这个站也叫小星', sender='b')
    observe(ledger, row, EntityCandidate('星驰', aliases=('小星',)))
    record, = ledger.list_entities('qq', 'g')
    assert ledger.lookup('qq', 'g', '星驰') == record and record.occurrence_count == 2
    for kwargs in (dict(sender='__bot__'), dict(chat_type='private')):
        other = message(store, '[图片]', **kwargs)
        assert not store.set_raw_visual_text(other.id, '异步描述')
    store.set_privacy_mode('qq:b', 'private')
    assert not store.set_raw_visual_text(row.id, '异步描述')
