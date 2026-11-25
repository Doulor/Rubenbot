"""群聊场景的槽位回归；显式指定模型通道时，另跑脱敏消息的现场回放。"""

import asyncio
import json
import os
import random

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.persona import load_persona


def event(text, sender="甲", **kwargs):
    return MessageEvent("recording", "group", "回放", sender, sender, text, **kwargs)


def make_bot(config, store, llm):
    bot = RubenBot(config, store, llm, load_persona("data/SOUL.md"), rng=random.Random(36))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def test_register_stays_with_scene(config, store):
    llm = ScriptedLLM({"fast": ['{"basis":"common","speak":true,"intent":"补充新信息",'
                                '"increment":"给词义","reason":"点名问"}'], "chat": ["是褒义词"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(event("贤惠是褒义还是贬义？", mentions_bot=True)))
    requests = [c for c in llm.calls if c['task'] in ('gen_stage1', 'gen_stage2')]
    assert [c['task'] for c in requests] == ['gen_stage1', 'gen_stage2']
    for c in requests:
        scene = c['messages'][0]['content']
        assert '本批 1 条消息，有人 @ 我' in scene and '其中有疑问字样；全批未见求助字样' in scene
        assert scene.count('口吻举例') == 1 and scene.count(' → ') == 4

    # 上轮真实 @ 和问句不能带入本批；本批「问别人」也不能与单独的 @ 我 拼起来。
    msg = event('坚定')
    batch = [event('在呢', mentions_bot=True), event('帮我看看？', sender='乙', mentions={'丙': ''}), msg]
    scene = asyncio.run(bot.build_context(msg, batch=batch))[0]['content']
    assert '本批 3 条消息，有人 @ 我；直接呼叫我的消息 1 条，其中未见疑问字样；全批有求助字样' in scene
    assert '本批 1 条消息，未见 @ 我；直接呼叫我的消息 0 条' in asyncio.run(bot.build_context(msg))[0]['content']
    for direct in (event('Ruben，帮忙看看怎么回事'),
                   event('帮忙看看怎么回事', reply_to_id='1', reply_sender_id=BOT_SENDER_ID, mentions_bot=True)):
        assert '未见 @ 我；直接呼叫我的消息 1 条，其中有疑问字样；全批有求助字样' in bot._room_state([direct])
    private = MessageEvent('recording', 'private', '甲', '甲', '甲', '在吗')
    private_scene = asyncio.run(bot.build_context(private))[0]['content']
    assert '房间状态' not in private_scene and '口吻举例' not in private_scene


class ReplayChannel:
    """仅在手动回放中用无工具、无历史的 CLI 承接真实策划/生成请求。"""

    async def complete(self, messages, *, task, **kwargs):
        assert task in ('gen_stage1', 'gen_stage2', 'gen_agreement_retry')
        system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
        turn = '\n'.join(m['content'] if m['role'] == 'user' else '先前的回复：' + m['content']
                         for m in messages if m['role'] != 'system')
        # 只送临时测试库的消息，连人设中的部署者与框架名字也匿名化。
        def anonymous(text):
            return text.replace('RubenBot', '聊天框架').replace('Ruben', '小测').replace('多萝', '机主')

        proc = await asyncio.create_subprocess_exec(
            'claude', '-p', '--bare', '--settings', os.environ['REGISTER36_SETTINGS'], '--tools', '',
            '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence',
            '--system-prompt', anonymous(system), '--output-format', 'json',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(anonymous(turn).encode()), 120)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        assert proc.returncode == 0, '指定的模型通道未成功返回'
        result = json.loads(out)
        assert not result['is_error'] and result['result'].strip()
        return result['result']


@pytest.mark.skipif(not os.getenv('REGISTER36_SETTINGS'), reason='手动真实模型回放，会计费')
@pytest.mark.parametrize('case', ['交易起哄', '贤惠追问', '世界知识'])
def test_live_register(case, config, store, monkeypatch):
    # 每例独立内存库。不改产品超时，测试留出 CLI 启动的时间。
    config.decision.merge_window_seconds = config.decision.hard_merge_window_seconds = 2
    config.memory.recall_tool_max_rounds = 0
    bot, adapter = make_bot(config, store, ReplayChannel())
    monkeypatch.setattr(bot, 'guidance_timeout', lambda: 130)
    if case == '交易起哄':
        batch = [event(s, sender='甲乙丙'[i % 3]) for i, s in enumerate(('12张', '抄底了', '坚定', '+1', '直接吃波段'))]
    elif case == '贤惠追问':
        config.computer.whitelist = ['recording:甲']
        batch = [event('贤惠是褒义还是贬义？', mentions_bot=True), event('正面回答我的问题')]
    else:
        batch = [event('月食发生时日地月怎么排列？为什么不是每次满月都有月食？', mentions_bot=True)]

    async def replay():
        for item in batch:
            await bot.handle_event(item)
        await bot.flush_merger(all_rounds=True)

    asyncio.run(replay())
    rows = store.list_decision_logs(platform='recording', chat_id='回放', chat_type='group')
    assert len(rows) == 1
    row = rows[0]
    planner = row['factors']['planner']
    reply = ''.join(adapter.texts)
    print(f"{case}：{planner['result_code']} / {planner['reason']} / {row['reason']}；出站：{reply or '（无）'}")
    assert planner['result_code'] == 'ok' and row['factors']['expression_decision']['source'] == 'fast'
    assert bool(row['hard_trigger']) == (case != '交易起哄')
    if case == '交易起哄':
        if not reply:
            assert row['reason'] in ('planner_silence', 'veto_marker')
        else:  # 有出站还须人工读原话核对是态度，而非换一套说辞讲原理。
            assert not any(s in reply for s in ('爆仓线', '止盈', '止损', '杠杆率'))
    elif case == '贤惠追问':
        assert '褒义' in reply
    else:
        compact = reply.translate(str.maketrans('', '', '、，, —-→'))
        assert '日地月' in compact or '太阳地球月' in compact or ('地球' in reply and '中间' in reply)
        assert any(word in reply for word in ('倾', '交点', '夹角', '不共面'))
