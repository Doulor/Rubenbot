"""由离线 HTTP 响应走到决策行：不把服务故障算作模型的社交判断。"""

import asyncio
import json

import httpx
import pytest

from conftest import RecordingAdapter
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.llm import LLMClient, make_usage_recorder
from rubenbot.persona import Persona


@pytest.mark.parametrize('status,content,finish,tokens,code,reason', [
    (401, '', None, None, 'http_401', ''),
    ('timeout', '', None, None, 'timeout', ''),
    ('deadline', '', None, None, 'timeout', ''),
    (200, '{"basis":', 'length', 100, 'truncated', ''),
    (200, '{"basis":', None, 256, 'output_limit_hit', ''),
    (200, '{"basis":', 'stop', 40, 'invalid_json', ''),
    ('malformed_envelope', '', None, None, 'invalid_response', ''),
    (200, '[]', 'stop', 40, 'invalid_structure', ''),
    (200, '{}', 'stop', 40, 'missing_intent', ''),
    (200, '{"intent":"未知","reason":"在看"}', 'stop', 40, 'invalid_intent', '在看'),
    (200, '{"basis":"settled","speak":false,"intent":"沉默"}', 'stop', 40, 'missing_reason', ''),
    (200, '{"basis":"settled","intent":"沉默","reason":"在听"}', 'stop', 40, 'missing_speak', '在听'),
    (200, '{"basis":"settled","speak":false,"intent":"沉默","reason":"在听"}', 'stop', 256, 'ok', '在听'),
])
def test_stage1_decision_row(config, store, caplog, status, content, finish, tokens, code, reason):
    config.llm.max_retries = 0
    if status == 'deadline':
        config.llm.timeout = 0.1

    async def respond(request):
        assert json.loads(request.content)['max_tokens'] == 256
        if status == 'timeout':
            raise httpx.ReadTimeout('上游故障正文')
        if status == 'deadline':
            await asyncio.Event().wait()
        if status == 'malformed_envelope':
            return httpx.Response(200, json={'choices': [None]})
        if status != 200:
            return httpx.Response(status, text='上游故障正文')
        return httpx.Response(200, json={
            'choices': [{'message': {'content': content}, 'finish_reason': finish}],
            'usage': {'completion_tokens': tokens},
        })

    client = LLMClient(config.llm, transport=httpx.MockTransport(respond), usage_recorder=make_usage_recorder(store))
    bot = RubenBot(config, store, client, Persona('Ruben', '群友'))
    bot.register_adapter(RecordingAdapter())

    async def run():
        try:
            await bot.handle_event(MessageEvent('recording', 'group', 'g', 'u', '群友', '有人在听吗'))
        finally:
            await client.close()

    asyncio.run(run())
    row, = store.list_decision_logs()
    assert not row['should_reply'] and row['reason'] == 'planner_silence'
    planner = row['factors']['planner']
    assert (planner['result_code'], planner['reason']) == (code, reason)
    assert (planner['max_tokens'], planner.get('finish_reason')) == (256, finish)
    usage, = store._conn.execute('SELECT id, task, completion_tokens, ok FROM llm_usage')
    assert tuple(usage) == (planner['usage_id'], 'gen_stage1', tokens, int(status == 200))
    assert '上游故障正文' not in caplog.text and '上游故障正文' not in str(row['factors'])


def test_retry_links_final_attempt(config, store, caplog):
    config.llm.max_retries = 1
    responses = iter([
        httpx.Response(503, text='上游故障正文'),
        httpx.Response(401, text='上游鉴权页'),
    ])
    client = LLMClient(config.llm, transport=httpx.MockTransport(lambda _: next(responses)),
                       usage_recorder=make_usage_recorder(store), retry_delays=(0,))
    bot = RubenBot(config, store, client, Persona('Ruben', '群友'))
    bot.register_adapter(RecordingAdapter())

    async def run():
        try:
            await bot.handle_event(MessageEvent('recording', 'group', 'g', 'u', '群友', '有人在听吗'))
        finally:
            await client.close()

    asyncio.run(run())
    first, final = store._conn.execute("SELECT id FROM llm_usage WHERE task='gen_stage1' ORDER BY id")
    planner = store.list_decision_logs()[0]['factors']['planner']
    assert first[0] < final[0] == planner['usage_id']
    assert planner['result_code'] == 'http_401' and planner['reason'] == ''
    assert '上游故障正文' not in caplog.text and '上游鉴权页' not in caplog.text
