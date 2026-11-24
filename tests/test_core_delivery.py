"""策划意图、适配器出站、流水和发送后钩子应能逐轮对账。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from conftest import ScriptedLLM
from rubenbot.plugins.loader import PluginManager
from rubenbot.verification import RECORD_PLACEHOLDER, WebReply
from test_core import make_bot, private_event


@pytest.mark.parametrize('speak, draft, failures, status, texts', [
    (False, '在呢', [True], 'send_failed', []),  # 3416：私聊必回覆盖策划沉默
    (True, '第一条\n第二条', [True], 'send_failed', []),   # 3417、3567：策划本来就要回
    (True, '第一条\n第二条', [False, True], 'partial', ['第一条']),
    (True, '第一条\n第二条', [False, False], 'sent', ['第一条', '第二条']),
    (True, WebReply('网页引文'), [True], 'send_failed', []),
    (True, WebReply('网页引文'), [False], 'sent', [RECORD_PLACEHOLDER]),
])
def test_delivery_matches_outbound(config, store, monkeypatch, caplog, speak, draft, failures, status, texts):
    plan = json.dumps({'speak': speak, 'intent': '提问' if speak else '沉默', 'basis': 'settled', 'increment': '问结果'})
    plugins = PluginManager(config, store)
    bot, adapter = make_bot(config, store, ScriptedLLM({'fast': [plan]}), plugins=plugins)
    monkeypatch.setattr(bot, 'generate', AsyncMock(return_value=draft))
    refused = RuntimeError("OneBot API send_private_msg 失败：{'retcode': 100, 'msg': "
                           "'result=16 err=发送失败，请先添加对方为好友', 'wording': '私密错误原文'}")
    send = AsyncMock(side_effect=[refused if fail else None for fail in failures])
    monkeypatch.setattr(adapter, 'send', send)
    after = [AsyncMock() for _ in range(3)]
    monkeypatch.setattr(bot.sticker_behavior, 'on_after_reply', after[0])
    monkeypatch.setattr(bot.privacy_commands, 'after_reply', after[1])
    monkeypatch.setattr(plugins, 'run_after_reply', after[2])
    caplog.set_level('INFO', logger='rubenbot.core')

    asyncio.run(bot.handle_event(private_event()))

    row, = store.list_decision_logs()
    assert bool(row['should_reply']) == bool(texts)
    if not texts:
        assert row['reason'] == 'send_failed'
    assert row['factors']['planner']['speak'] is speak
    assert (row['factors'].get('planner_override') == 'private') is (not speak)
    delivery = row['factors']['delivery']
    assert delivery['status'] == status
    assert (delivery['sent_parts'], delivery['failed_parts']) == (len(texts), sum(failures))
    assert delivery['reason_code'] == ('private_recipient_refused' if any(failures) else '')
    outbound = [r for r in store.recent_messages('u1') if r.sender_id == '__bot__']
    assert [r.text for r in outbound] == texts
    assert delivery['raw_ids'] == [r.id for r in outbound]
    assert send.await_count == len(failures)
    if texts and not isinstance(draft, WebReply):
        for hook in after:
            hook.assert_awaited_once()
        assert after[0].await_args.args[-1] == after[2].await_args.args[-1] == '\n'.join(texts)
    else:
        assert not any(hook.await_count for hook in after)
    assert '进入策划' in caplog.text
    assert '私密错误原文' not in caplog.text + str(row['factors'])


def test_voice_refusal_does_not_fall_back_to_text(config, store, monkeypatch):
    bot, adapter = make_bot(config, store, ScriptedLLM())
    monkeypatch.setattr(bot, 'synthesize_reply', AsyncMock(return_value=(b'audio', 'audio/mpeg')))
    monkeypatch.setattr(adapter, 'capabilities', {'message.voice'})
    call = AsyncMock(side_effect=RuntimeError('message.voice 失败：result=16 err=发送失败，请先添加对方为好友'))
    monkeypatch.setattr(adapter, 'call_capability', call)
    result = asyncio.run(bot.send_reply(private_event(), '在呢'))
    call.assert_awaited_once()
    assert result.status == 'send_failed' and result.reason_code == 'private_recipient_refused'
    assert result.failed_parts == 1 and not result.raw_ids and not adapter.sent


def test_new_input_during_send_preparation_replaces_only_first_draft(config, store, monkeypatch):
    llm = ScriptedLLM({'chat': ['旧稿', '新稿']})
    bot, adapter = make_bot(config, store, llm)

    async def synthesize(event, text):
        if text == '旧稿':
            await bot.handle_event(private_event('我补充一句'))
        return None, ''

    hook = AsyncMock()
    monkeypatch.setattr(bot, 'synthesize_reply', synthesize)
    monkeypatch.setattr(bot.sticker_behavior, 'on_after_reply', hook)
    asyncio.run(bot.handle_event(private_event()))
    latest, old = store.list_decision_logs()
    assert old['factors']['delivery']['status'] == 'superseded' and not old['should_reply']
    assert latest['factors']['delivery']['status'] == 'sent' and adapter.texts == ['新稿']
    hook.assert_awaited_once()
    assert hook.await_args.args[-1] == '新稿'


def test_sticker_delivery_has_local_raw_id(config, store, monkeypatch):
    bot, _ = make_bot(config, store, ScriptedLLM({'fast': ['{"speak":true,"intent":"短附和","basis":"settled"}']}))
    monkeypatch.setattr(bot.sticker_behavior, 'try_send', AsyncMock(return_value=True))
    asyncio.run(bot.handle_event(private_event()))
    log, = store.list_decision_logs()
    outbound, = [r for r in store.recent_messages('u1') if r.sender_id == '__bot__']
    assert outbound.text == '[表情]'
    assert log['factors']['delivery'] == {
        'status': 'sent', 'sent_parts': 1, 'failed_parts': 0, 'raw_ids': [outbound.id], 'reason_code': '',
    }
