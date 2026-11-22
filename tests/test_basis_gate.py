"""依据门离线回放：模型报告依据，代码只校验分类、执行开口边界，不靠匹配聊天词句。"""

import asyncio
import json
import time

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.expression import parse_plan
from rubenbot.persona import Persona


def proposal(basis, increment):
    return json.dumps({'basis': basis, 'speak': True, 'intent': '补充新信息', 'increment': increment}, ensure_ascii=False)


def setup_bot(config, store, raw, reply):
    config.computer.whitelist = ['recording:owner']
    llm = ScriptedLLM({'fast': [raw], 'chat': [reply]})
    bot = RubenBot(config, store, llm, Persona('Ruben', '群友'))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, llm


@pytest.mark.parametrize('reported,expected', [
    ('ledger', 'ledger'), ('settled', 'settled'), ('common', 'common'), ('guess', 'guess'),
    (None, 'guess'), ('亲身经验', 'guess'), (['ledger'], 'guess'),
])
def test_parse_basis(reported, expected):
    assert parse_plan(proposal(reported, '补一点'), '短句', []).basis == expected


# 2026-11-22 晚主群记录。反例保留 bot 的原话；正例用群友的独立记录，只说其支持的那一点。
@pytest.mark.parametrize('text,unsupported,record', [
    ('来帮我点一下邀请链接', '点了，还差几个', '上次群里有人贴过邀请链接'),
    ('没小号了', '我也没几个了，攒了好久', '上次群里聊过小号用完的事'),
    ('他瞎说的', '踩过的多了／日区注册的掉得快', '之前群里说过，掉号原因没查实'),
    ('这几个你们觉得哪个实在', '你们试过的坑，我大概也踩过', '上次群里有人贴过这几个的对比'),
])
@pytest.mark.parametrize('basis', ['guess', 'ledger'])
def test_group_replay(config, store, text, unsupported, record, basis):
    increment = unsupported if basis == 'guess' else record
    bot, adapter, llm = setup_bot(config, store, proposal(basis, increment), increment)
    now = time.time()
    if basis == 'ledger':
        store.add_raw_message(platform='recording', chat_id='g', sender_id='peer', text=record, ts=now - 60)
    # 旧自述和当前的机主纠正不应被混成同一份依据；机主优先查看并不强迫开口。
    store.add_raw_message(platform='recording', chat_id='g', sender_id=BOT_SENDER_ID, text=unsupported,
                          processed=True, ts=now - 10)
    asyncio.run(bot.handle_event(MessageEvent('recording', 'group', 'g', 'owner', '群友', text)))
    row, = store.list_decision_logs()
    assert row['factors']['planner']['basis'] == basis
    assert row['factors']['planner']['speak'] is (basis == 'ledger')
    history = llm.calls[0]['messages'][:-1]
    assert any(m['role'] == 'assistant' and '自己说过的话（不作数）' in m['content'] and unsupported in m['content'] for m in history)
    assert any('机主原话·事实部分已定' in m['content'] and text in m['content'] for m in history)
    if basis == 'guess':
        assert not row['should_reply'] and not adapter.sent
        assert [call['task'] for call in llm.calls] == ['gen_stage1']
    else:
        assert any(record in m['content'] for m in history)
        assert row['should_reply'] and adapter.texts == [record]
        assert '依据：ledger' in llm.chat_messages[0][-1]['content']


@pytest.mark.parametrize('kind,mentions', [('group', True), ('private', False)])
def test_direct_guess_reaches_generator_as_uncertain(config, store, kind, mentions):
    bot, adapter, llm = setup_bot(config, store, proposal('guess', '回应服务原理的类比'), '这个原理我不确定')
    asyncio.run(bot.handle_event(MessageEvent('recording', kind, 'g', 'peer', '机主',
                                             '是不是和 wifi 万能钥匙差不多', mentions_bot=mentions)))
    assert adapter.texts == ['这个原理我不确定']
    guidance = llm.chat_messages[0][-1]['content']
    assert '依据：guess' in guidance and '明说不确定' in guidance
    assert store.list_decision_logs()[0]['factors']['planner']['basis'] == 'guess'


@pytest.mark.parametrize('raw', ['不是结构化裁定', '{"speak":true,"intent":"短附和"}', proposal('未知', '接话')])
def test_missing_or_invalid_basis_cannot_open_group_gate(config, store, raw):
    bot, adapter, llm = setup_bot(config, store, raw, '无据正文')
    asyncio.run(bot.handle_event(MessageEvent('recording', 'group', 'g', 'peer', '机主', '在聊什么')))
    assert not adapter.sent and not llm.chat_messages
    plan = store.list_decision_logs()[0]['factors']['planner']
    assert plan['basis'] == 'guess' and plan['speak'] is False
