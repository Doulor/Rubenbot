"""查看率只省裁定，不吞消息；投入状态严格按平台、会话类型和会话隔离。"""

import asyncio
import random
import time
from unittest.mock import Mock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.memory.entities import EntityCandidate
from rubenbot.persona import Persona


def event(text, sender='A', kind='group', room='g', **kwargs):
    return MessageEvent('recording', kind, room, sender, sender, text, **kwargs)


def build(config, store, probability, seed=0):
    config.planner.planner_gate = True
    config.attention.talk_value = probability
    llm = ScriptedLLM({'fast': ['{"speak":false,"intent":"沉默"}']})
    bot = RubenBot(config, store, llm, Persona('Ruben', '群友'), rng=random.Random(seed))
    bot.register_adapter(RecordingAdapter())
    return bot, llm


@pytest.mark.parametrize('seed, viewed', [(0, False), (1, True)])
def test_attention_draw_once_per_batch_and_keeps_accounts(config, store, seed, viewed):
    bot, llm = build(config, store, 0.75, seed)
    bot.merger.window = 2.5
    raw = store.add_raw_message(platform='recording', chat_id='g', sender_id='史料', text='星驰')
    bot.entities.observe_message(platform='recording', chat_id='g', message=store.raw_message_by_id(raw),
                                 candidates=[EntityCandidate('星驰')])
    draw = Mock(wraps=bot.rng.random)
    bot.rng.random = draw
    config.chat.history_size = 1  # 即使近期历史限一条，本批两条仍都给策划。

    async def scenario():
        await bot.handle_event(event('星驰怎么了？'))
        await bot.handle_event(event('星驰恢复了', sender='B'))
        assert not llm.calls and len(store.recent_messages('g')) == 3
        await bot.flush_merger(now=bot.merger.next_deadline())

    asyncio.run(scenario())
    assert draw.call_count == (2 if viewed else 1)  # 通过后另掷一次表达长度档
    assert [(m.sender_id, m.text) for m in store.recent_messages('g')][-2:] == [('A', '星驰怎么了？'), ('B', '星驰恢复了')]
    assert {s.sender_id for s in store.pending_senders()} >= {'A', 'B'}
    assert bot.entities.lookup('recording', 'g', '星驰').occurrence_count == 3
    log, = store.list_decision_logs()
    if viewed:
        call, = llm.calls
        assert call['task'] == 'gen_stage1' and log['reason'] == 'planner_silence'
        batch, = [m['content'] for m in call['messages'] if m['content'].startswith('【本批消息')]
        assert batch.index('星驰怎么了？') < batch.index('星驰恢复了')
    else:
        assert not llm.calls and log['reason'] == 'attention_skip'
        assert log['factors']['effective_talk_value'] == 0.75


@pytest.mark.parametrize('msg', [event('在吗', mentions_bot=True), event('Ruben 在吗'),
                                 event('在吗', reply_to_id='1', reply_sender_id='__bot__'),
                                 event('在吗', sender='主人'), event('在吗', kind='private')])
def test_priority_batch_no_attention_draw(config, store, msg):
    bot, llm = build(config, store, 0)
    config.computer.whitelist = ['recording:主人']
    bot.decision_engine.note_bot_reply(bot.decision_engine.chat_key(msg), time.time())  # 普通批次会被护栏拦下
    bot.merger.window, bot.merger.hard_window = 2.5, 1
    bot.rng.random = Mock(wraps=bot.rng.random)

    async def scenario():
        await bot.handle_event(msg)
        if msg.is_group:
            batch, = bot.merger.pending
            assert batch.hard and batch.urgent and batch.deadline == batch.first_at + 1
            await bot.flush_merger(now=batch.deadline)
        assert llm.calls[0]['task'] == 'gen_stage1'

    asyncio.run(scenario())
    assert bot.rng.random.call_count == 1  # 只有长度档掷点


@pytest.mark.parametrize('cause', ['被点名', '实际出站'])
def test_engagement_expires_without_crossing_conversations(config, store, monkeypatch, cause):
    bot, llm = build(config, store, 0)
    now = 1000.0
    monkeypatch.setattr('rubenbot.core.time.time', lambda: now)
    config.decision.min_reply_interval_seconds = 0
    config.decision.refractory_enabled = False
    if cause == '被点名':
        # 不配置适配器以确保只有点名、没有成功出站，也能进入投入状态。
        bot.adapters.clear()
        asyncio.run(bot.handle_event(event('Ruben？')))
    else:
        asyncio.run(bot.send_reply(event('早'), '早呀'))
    now += 119.9
    assert bot.effective_talk_value(event('问候', room='隔壁'), now) == 0
    llm.calls.clear()
    asyncio.run(bot.handle_event(event('还有一件事')))
    assert llm.calls and llm.calls[0]['task'] == 'gen_stage1'
    assert bot.effective_talk_value(event('私聊', kind='private'), now) == 1
    assert bot.effective_talk_value(event('私聊', kind='private'), now + 9999) == 1
    assert bot.effective_talk_value(event('后来'), 1120.0) == 0
