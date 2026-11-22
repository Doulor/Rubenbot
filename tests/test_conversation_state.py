"""阶段 B：后台证据→第一人称短文；只给裁定，失去时效或来源就不再注入。"""

import asyncio
import json
import time

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.memory.state import REFRESH_INTERVAL, STATE_LIFETIME, StateDistiller
from rubenbot.memory.store import ProfileLine
from rubenbot.persona import Persona


NOW = 1_000_000.0
KEY = ('recording', 'group', 'g')
STATE = '我这会儿想听听剩下的故事'
AIR = '我觉得这里刚刚安静下来'
ANSWER = json.dumps({'state': STATE, 'atmosphere': AIR}, ensure_ascii=False)


def event(store, text='聊到野餐', *, now=NOW, audience='group:g', source='recording:g', pid='recording:u'):
    return store.add_event(pid, text, source_chat=source, audience=audience, ts=now, now=now)


def update(worker, *, now=NOW):
    asyncio.run(worker.update_chat(*KEY, now=now))


def test_distills_only_local_event_and_profile_evidence(store, config):
    local = event(store)
    secret = event(store, '私聊里的消息', audience='private')  # 同号异场合
    event(store, '另一平台的消息', source='other:g')
    event(store, '自己的旧回复', pid='self')
    store.add_event(local.person_id, '封存了的事', source_chat='recording:g', audience='group:g',
                    sensitivity='sealed', ts=NOW)
    protected = store.add_memory(local.person_id, '不能复述的事实', sensitivity='high',
                                 origin_chat='recording:g', audience='group:g')
    event(store, '隐私模式的消息', pid='recording:private')
    store.set_privacy_mode('recording:private', 'private')
    event(store, '不记模式的消息', pid='recording:hidden')
    store.set_privacy_mode('recording:hidden', 'norecord')
    memory = store.add_memory(local.person_id, '偏好户外', origin_chat='recording:g', audience='group:g')
    store.save_profile(local.person_id, stable_lines=[
        ProfileLine('有空就出门', [f'event:{local.id}', f'memory:{memory.id}']),
        ProfileLine('混入别处的画像', [f'event:{local.id}', f'event:{secret.id}']),
        ProfileLine('敏感的画像行', [f'memory:{protected.id}']),
    ], status_lines=[ProfileLine('已经过期的近况', [f'event:{local.id}'])], status_expires_at=NOW-1, now=NOW)
    llm = ScriptedLLM({'summary': [ANSWER]})
    worker = StateDistiller(llm, store, config.memory)
    update(worker)
    update(worker, now=NOW + REFRESH_INTERVAL * 2)
    call, = llm.calls
    assert (call['role'], call['task']) == ('summary', 'conversation_state')
    system, material = [m['content'] for m in call['messages']]
    assert '第一人称、现在时' in system
    assert all(x in material for x in ('聊到野餐', '有空就出门'))
    assert all(x not in material for x in ('私聊里的消息', '另一平台', '自己的旧回复', '不记模式', '混入别处',
                                           '已经过期', '隐私模式', '封存了的事', '敏感的画像行'))
    row = store.get_conversation_state(*KEY)
    assert (row['state'], row['atmosphere']) == (STATE, AIR)
    assert worker.planner_text('recording', 'private', 'g', now=NOW) == ''
    assert worker.planner_text('other', 'group', 'g', now=NOW) == ''
    config.memory.group_profile.enabled = False
    local_text = worker.planner_text(*KEY, now=NOW)
    assert STATE in local_text and AIR not in local_text


def test_refresh_decays_then_retires_old_material(store, config):
    event(store, '昨天一起聊野餐')
    llm = ScriptedLLM({'summary': [ANSWER] * 3})
    worker = StateDistiller(llm, store, config.memory)
    update(worker)
    later = NOW + 86400
    event(store, '今天话题变了', now=later)
    update(worker, now=later)
    request = llm.calls[-1]['messages'][-1]['content']
    assert f'上次的感觉（24 小时前）：{STATE}' in request
    assert '24 小时前）：昨天一起聊野餐' in request
    assert '0 小时前）：今天话题变了' in request
    after_gap = later + STATE_LIFETIME + 1
    assert worker.planner_text(*KEY, now=after_gap) == ''
    event(store, '新一轮碰面', now=after_gap)
    update(worker, now=after_gap)
    request = llm.calls[-1]['messages'][-1]['content']
    assert '新一轮碰面' in request
    assert all(x not in request for x in (STATE, AIR, '昨天一起聊野餐', '今天话题变了'))


@pytest.mark.parametrize('failure', ['invalid_json', 'third_person', 'exception'])
def test_failed_synthesis_keeps_old_state_and_timestamp(store, config, failure):
    event(store)
    worker = StateDistiller(ScriptedLLM({'summary': [ANSWER]}), store, config.memory)
    update(worker)
    old = store.get_conversation_state(*KEY)
    later = NOW + REFRESH_INTERVAL
    event(store, '后来聊到电影', now=later)
    store.add_raw_message(platform='recording', chat_id='g', sender_id='u', sender_name='友', text='在吗', ts=later)

    class FailedLLM:
        async def complete(self, *args, **kwargs):
            if failure == 'exception':
                raise TimeoutError()
            return '{' if failure == 'invalid_json' else json.dumps({'state': '她有点困', 'atmosphere': AIR})

    worker.llm = FailedLLM()
    asyncio.run(worker.maybe_update(now=later))  # 原因不同，旧状态与更新时间都不得变化
    assert store.get_conversation_state(*KEY) == old


@pytest.mark.parametrize('action', ['purge', 'norecord', 'private'])
def test_withdrawal_invalidates_reflection(store, config, action):
    event(store)
    worker = StateDistiller(ScriptedLLM({'summary': [ANSWER]}), store, config.memory)
    update(worker)
    assert STATE in worker.planner_text(*KEY, now=NOW)
    if action == 'purge':
        store.purge_person('recording:u')
    else:
        store.set_privacy_mode('recording:u', action)
    assert worker.planner_text(*KEY, now=NOW) == ''


def test_private_mode_changed_during_synthesis_discards_result(store, config):
    event(store)

    class ChangedLLM:
        async def complete(self, *args, **kwargs):
            store.set_privacy_mode('recording:u', 'private')
            return ANSWER

    worker = StateDistiller(ChangedLLM(), store, config.memory)
    update(worker)
    assert store.get_conversation_state(*KEY) is None


@pytest.mark.parametrize('manual', ['', '约好这里只讨论项目'])
def test_actual_planner_request_has_state_but_stage2_has_neither(store, config, manual):
    now = time.time()
    event(store, now=now)
    store.add_raw_message(platform='recording', chat_id='g', sender_id='u', sender_name='友', text='在吗', ts=now)
    store.set_group_auto_summary('recording', 'g', '这是开源项目群', 1, now=now)
    store.set_group_manual_note('recording', 'g', manual)
    llm = ScriptedLLM({'summary': [ANSWER], 'fast': [json.dumps({'speak': True, 'intent': '短附和'})], 'chat': ['在呢']})
    bot = RubenBot(config, store, llm, Persona(name='Ruben', text='你是 Ruben。'))
    bot.register_adapter(RecordingAdapter())
    asyncio.run(bot.run_maintenance(now=now))
    llm.calls.clear()
    asyncio.run(bot.handle_event(MessageEvent(platform='recording', chat_type='group', chat_id='g', sender_id='u',
                                              sender_name='友', text='在吗？', mentions_bot=True)))
    planner, generator = llm.calls
    assert [planner['task'], generator['task']] == ['gen_stage1', 'gen_stage2']
    request = planner['messages'][-1]['content'].split('【自身状态】')[-1]
    assert STATE in request and (AIR in request) == (not manual)
    assert (manual or '这是开源项目群') in planner['messages'][0]['content']
    assert all(STATE not in m['content'] and AIR not in m['content'] for m in generator['messages'])


def test_private_chat_has_only_first_person_state_limited_to_200_chars(store, config):
    event(store, audience='private')
    llm = ScriptedLLM({'summary': [json.dumps({'state': '我' * 320, 'atmosphere': AIR})]})
    worker = StateDistiller(llm, store, config.memory)
    asyncio.run(worker.update_chat('recording', 'private', 'g', now=NOW))
    row = store.get_conversation_state('recording', 'private', 'g')
    assert len(row['state']) == 200 and row['atmosphere'] == ''
    assert store.get_conversation_state(*KEY) is None
