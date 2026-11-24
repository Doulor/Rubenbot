"""跨群追封回放：登记不随开关消失，放行的模型也不能绕过来源证据。"""

import asyncio
from copy import deepcopy
import json
import time

import pytest

from conftest import ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.persona import Persona


@pytest.mark.parametrize('initially_enabled', [False, True])
def test_followup_after_missing_id_extraction(config, store, initially_enabled):
    config.privacy.enabled = initially_enabled
    llm = ScriptedLLM({'extract': [json.dumps({
        'facts': [{'content': '在灯塔项目负责蓝杉配色'}],
        'events': [{'summary': '已为灯塔项目选定蓝杉色'}],
    }, ensure_ascii=False)], 'gate': [json.dumps({'items': [
        {'id': i, 'verdict': 'allow', 'confidence': 1} for i in range(20)
    ]})] * 20})
    bot = RubenBot(config, store, llm, Persona('Ruben', '群友。'))
    now = time.time()
    source = MessageEvent('test', 'group', 'A', 'u', '小舟', '灯塔项目用的是蓝杉色。')
    target = MessageEvent('test', 'group', 'B', 'u', '小舟', '灯塔项目的配色是什么？')

    async def replay():
        await bot._record(source, now - 3)
        await bot.extractor.maybe_extract(force=True, now=now - 2)
        await bot._record(MessageEvent('test', 'group', 'A', 'u', '小舟', '刚才那句保密'), now - 1)
        for enabled in (False, True):
            changed = deepcopy(bot.config)
            changed.privacy.enabled = enabled
            bot.apply_config(changed, rebuild_llm=False)
            context = '\n'.join(m['content'] for m in await bot.build_context(target, now=now))
            recall = await bot.recall('', '灯塔项目', event=target, now=now)
            # 无出处的模型改写不另立一条账；可见性只围绕真正写入的事件与事实判断。
            assert '已为灯塔项目选定蓝杉色' not in context + recall
            for text in [source.text, '在灯塔项目负责蓝杉配色']:
                assert (text in recall) is not enabled, (enabled, recall)
                assert (text in context) is not enabled, (enabled, context)
        assert not store.available_raw_ids([source.raw_message_id], unblocked=True)
        assert store.list_events(person_id='test:u')[0].sensitivity == 'sealed'
        assert store.list_memories(person_id='test:u')[0].sensitivity == 'sealed'

    asyncio.run(replay())


def test_merge_and_restore_after_raw_expiry(config, store):
    from rubenbot.memory.consolidate import Consolidator
    from rubenbot.privacy.sealing import register_message

    origin = 'test:A'
    person = 'test:u'
    for text in ('喜欢唱歌', '在学素描', '喜欢跳舞'):
        rid = store.add_raw_message(platform='test', chat_id='A', sender_id='u', text=text, processed=True)
        store.add_memory(person, text, origin_chat=origin, audience='group:A', source_ref=f'{origin}:{rid}-{rid}')
    first, other, last = sorted(store.list_memories(), key=lambda m: m.id)
    llm = ScriptedLLM({'consolidate': [json.dumps([{'op': 'merge', 'ids': [first.id, last.id],
                                                   'content': '喜欢唱歌和跳舞'}])]})
    result = asyncio.run(Consolidator(llm, store, config.memory).consolidate_person(person))
    assert result.merged == 1
    cmd = MessageEvent('test', 'group', 'A', 'u', '小舟', '刚才那句保密')
    rid = store.add_raw_message(platform='test', chat_id='A', sender_id='u', text=cmd.text, processed=True)
    register_message(store, cmd, rid)
    merged, = [m for m in store.list_memories() if m.id != other.id]
    assert merged.sensitivity == 'sealed' and merged.source_ref == 'test:A:1-1,3-3'
    assert store.get_memory(other.id).sensitivity == 'normal'
    store.trim_raw(0)
    assert store.restore(result.applied_log_ids[0])
    assert store.get_memory(first.id).sensitivity == 'normal'
    assert store.get_memory(last.id).sensitivity == 'sealed'
    store.purge_person(person)
    assert not store._conn.execute('SELECT 1 FROM sealed_sources WHERE person_id=?', (person,)).fetchone()


def test_legacy_followup_backfill_is_scoped_and_idempotent(tmp_path):
    import sqlite3
    from rubenbot.memory.store import MemoryStore

    path = tmp_path / 'legacy.db'
    store = MemoryStore(path)
    origin = 'test:A'
    def raw(text, ts, chat_type='group'):
        return store.add_raw_message(platform='test', chat_id='A', sender_id='u', chat_type=chat_type,
                                     text=text, ts=ts, processed=True)

    rid = raw('蓝杉配色', 30)
    raw('别的私聊', 31, 'private')
    raw('刚才那句保密', 32)
    expired = raw('已裁剪的原话', 15)
    store.add_memory('test:u', '仅剩来源', origin_chat=origin, audience='group:A', sensitivity='sealed',
                     source_ref=f'{origin}:{expired}-{expired}', now=16)
    derived = store.add_event('test:u', '已裁剪原话的旧事件', source_chat=origin, audience='group:A',
                              source_raw_id=expired, now=16)
    fact = store.add_memory('test:u', '配色蓝杉', source_ref=f'{origin}:{rid}-{rid}', now=31)
    event = store.add_event('test:u', '旧抽取无批次', source_chat=origin, audience='group:A', now=31, ts=5)
    same_number = store.add_event('test:u', '同号私聊', source_chat=origin, audience='private', now=31)
    earlier = store.add_event('test:u', '发言前的群记录', source_chat=origin, audience='group:A', now=10)
    peer = store.add_event('test:peer', '同场另一人', source_chat=origin, audience='group:A', now=31)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM raw_messages WHERE id=?', (expired,))
        conn.execute('ALTER TABLE events DROP COLUMN source_ref')
        conn.execute('DROP TABLE sealed_sources')
        conn.execute("DELETE FROM meta WHERE key='sealing:backfilled:v1'")
    for _ in range(2):
        store = MemoryStore(path)
        assert store.get_memory(fact.id).sensitivity == store.get_event(event.id).sensitivity == 'sealed'
        assert all(store.get_event(item.id).sensitivity == 'normal' for item in (same_number, earlier, peer))
        assert not store.available_raw_ids([rid], unblocked=True)
        assert store.get_event(derived.id).sensitivity == 'sealed'
        assert store._conn.execute('SELECT count(*) FROM sealed_sources').fetchone()[0] == 3
        store.close()
