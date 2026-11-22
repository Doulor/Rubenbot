"""用事件栅栏准确地把补充消息放进策划、生成或发送准备期间。"""

import asyncio

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.persona import Persona


def event(text, sender='a', **kwargs):
    return MessageEvent('recording', 'group', 'room', sender, sender, text, **kwargs)


@pytest.mark.parametrize('blocked_stage, update', [
    ('gen_stage1', event('同一个人的结论')),
    ('gen_stage2', event('更正', 'b', mentions_bot=True)),
    ('gen_stage2', event('Ruben 先等等', 'c')),
    ('gen_stage2', event('看这里', 'd', reply_to_id='42', reply_sender_id='__bot__')),
])
def test_stale_replans_even_when_hard_trigger_now_silent(config, store, blocked_stage, update):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Paused(ScriptedLLM):
            async def complete(self, messages, *, max_tokens=None, **kwargs):
                result = await super().complete(messages, **kwargs)
                if kwargs['task'] == blocked_stage and not started.is_set():
                    started.set()
                    await release.wait()
                return result

        llm = Paused({'fast': ['{"speak":true,"intent":"提问","increment":"问结果"}', '{"speak":false,"intent":"沉默"}'],
                      'chat': ['作废的草稿']})
        bot = RubenBot(config, store, llm, Persona('Ruben', '群友'))
        adapter = RecordingAdapter()
        bot.register_adapter(adapter)
        task = asyncio.create_task(bot.handle_event(event('请帮我看看？', mentions_bot=True)))
        await asyncio.wait_for(started.wait(), 2)
        await bot.handle_event(update)
        release.set()
        await task
        assert not adapter.sent and not bot.merger.pending
        assert [c['task'] for c in llm.calls].count('gen_stage1') == 2
        assert [c['task'] for c in llm.calls].count('gen_stage2') == int(blocked_stage == 'gen_stage2')
        prompt = next(m['content'] for m in llm.calls[-1]['messages'] if '【本批消息·' in m['content'])
        assert '请帮我看看' in prompt and update.text in prompt
        assert [m.text for m in store.recent_messages('room')] == ['请帮我看看？', update.text]
        log, = store.list_decision_logs()
        assert log['reason'] == 'planner_silence' and 'planner_override' not in log['factors']

    asyncio.run(scenario())


def test_retry_budget_and_noninterrupting_chatter(config, store):
    async def scenario():
        llm = ScriptedLLM({'fast': ['{"speak":true,"intent":"提问","increment":"问结果"}'] * 2})
        bot = RubenBot(config, store, llm, Persona('Ruben', '群友'))
        adapter = RecordingAdapter()
        bot.register_adapter(adapter)
        # 新到的非打断消息以及重跑期间的消息进下一批，不丢弃、也不再重跑本批。
        bot.merger.window = bot.merger.hard_window = 30
        await bot.handle_event(event('原问题', mentions_bot=True))
        calls = []

        async def generate(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                await bot.handle_event(event('无关插话', 'b'))
                await bot.handle_event(event('补充说明'))
            else:
                await bot.handle_event(event('更晚的消息'))
            return '旧草稿' if len(calls) == 1 else '按补充说明回答'

        bot.generate = generate
        await bot.flush_merger(now=bot.merger.next_deadline())
        assert len(calls) == 2 and adapter.texts == ['按补充说明回答']
        pending, = bot.merger.pending
        assert [e.text for e in pending.events] == ['无关插话', '更晚的消息']
        assert len(store.list_decision_logs()) == 1

    asyncio.run(scenario())
