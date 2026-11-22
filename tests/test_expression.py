"""默认回复路径上的决策书、调制与会话隔离。"""

import asyncio
import json
from unittest.mock import Mock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.expression import choose_length_band, parse_plan
from rubenbot.persona import Persona


@pytest.mark.parametrize('mentioned,casual,expected', [
    (False, False, [40, 40, 20]), (True, False, [20, 40, 40]), (False, True, [55, 35, 10]),
])
def test_distribution(mentioned, casual, expected):
    bands = [choose_length_band((i + .5) / 100, mentioned_question=mentioned, casual_group=casual)
             for i in range(100)]
    assert [bands.count(b) for b in ('碎片', '短句', '展开')] == expected


@pytest.mark.parametrize('increment', ['', '无', None])
def test_no_increment_returns_control_without_generation(config, store, increment):
    llm = ScriptedLLM({'fast': [json.dumps({'basis':'settled', 'intent': '接梗', 'increment': increment})]})
    bot = RubenBot(config, store, llm, Persona('R', '人设'))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(MessageEvent('recording', 'group', '1', '1', '友', '今天下雨')))
    assert not llm.chat_messages and not adapter.sent
    log, = store.list_decision_logs()
    assert not log['should_reply'] and log['factors']['expression_decision']['intent'] == '沉默'
    assert not bot.last_decision['recording:1'].should_reply


def test_extract_last_three_bot_turns_before_limit_and_scope(config, store):
    for i, (sender, platform, chat, kind, text) in enumerate([
        (BOT_SENDER_ID, 'recording', '1', 'private', t) for t in ('太早', '旧城有雾', '河边落雨', '码头有船')
    ] + [('u', 'recording', '1', 'private', '不是自己的话')] * 5 + [
        (BOT_SENDER_ID, 'recording', '2', 'private', '隔壁'),
        (BOT_SENDER_ID, 'recording', '1', 'group', '同号群'),
        (BOT_SENDER_ID, 'other', '1', 'private', '其他平台'),
    ]):
        store.add_raw_message(platform=platform, chat_id=chat, chat_type=kind, sender_id=sender,
                              sender_name=sender, text=text, ts=i)
    plan = {'intent': '提问', 'increment': '问过河的路线', 'avoid_repetition': ['有雾', '编造词']}
    llm = ScriptedLLM({'fast': [json.dumps(plan)]})
    bot = RubenBot(config, store, llm, Persona('R', '人设'))
    bot.register_adapter(RecordingAdapter())
    asyncio.run(bot.handle_event(MessageEvent('recording', 'private', '1', '1', '友', '下雨了')))
    log, = store.list_decision_logs()
    saved = log['factors']['expression_decision']
    assert saved['avoid_repetition'] == ['有雾', '河边落雨', '码头有船']
    prompt = llm.chat_messages[0][-1]['content']
    assert all(t in prompt for t in [*saved['avoid_repetition'], saved['length_band'], plan['increment']])


def test_fragment_length_still_requires_planner(config, store):
    llm = ScriptedLLM({"fast": ['{"basis":"settled","intent":"短附和"}']})
    bot = RubenBot(config, store, llm, Persona('R', '人设'), rng=Mock(random=lambda: 0.0))
    bot.register_adapter(RecordingAdapter())
    asyncio.run(bot.handle_event(MessageEvent('recording', 'group', 'g', 'u', '友', '看这张图')))
    assert [c['role'] for c in llm.calls] == ['fast', 'chat']
    plan = store.list_decision_logs()[0]['factors']['expression_decision']
    assert plan['source'] == 'fast' and plan['length_band'] == '碎片'


def test_oversize_and_unstructured_plans_do_not_become_chat_instructions():
    for raw in ('照抄这段坏指导', json.dumps({'intent': '接梗', 'increment': 'a' * 900})):
        plan = parse_plan(raw, '展开', ['说过的话'])
        assert plan.intent == '短附和' and plan.length_band == '碎片'
        assert raw not in plan.prompt_block()
