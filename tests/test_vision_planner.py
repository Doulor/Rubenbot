"""在实际裁定请求里核对本批、历史及引用的图片快照；不调用真实视觉模型。"""

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from conftest import ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.core import RubenBot
from rubenbot.decision import MergedRound
from rubenbot.persona import Persona


NOW = 1_800_000_000.0


@pytest.fixture
def bot(config, store, monkeypatch):
    monkeypatch.setattr('rubenbot.core.time.time', lambda: NOW)
    return RubenBot(config, store, ScriptedLLM(), Persona('Ruben', '群友'))


def message(text, sender='甲', **kwargs):
    return MessageEvent('recording', 'group', 'g', sender, sender, text, **kwargs)


def judge(bot, events, elapsed, on_wait, monkeypatch):
    wait = AsyncMock(side_effect=on_wait)
    monkeypatch.setattr(bot.sticker_behavior, 'wait_for_visuals', wait, raising=False)

    async def run():
        for event in events:
            await bot._record(event, NOW - elapsed)
        await bot.process_round(MergedRound('recording', 'g', 'group', '甲', '甲', events,
                                           first_at=NOW - elapsed, hard=True))

    asyncio.run(run())
    call, = [call for call in bot.llm.calls if call['task'] == 'gen_stage1']
    return wait, [m['content'] for m in call['messages']]


def test_single_waits_before_planner_and_labels_history(bot, store, monkeypatch):
    old = store.add_raw_message(platform='recording', chat_type='group', chat_id='g', sender_id='乙',
                                text='昨天的图 [图片]', visual_text='两棵树', ts=NOW - 60)
    event = message('看这个 [图片]')

    async def complete(ids, *, timeout):
        store.set_raw_visual_text(event.raw_message_id, '三只猫')

    wait, contents = judge(bot, [event], 0, complete, monkeypatch)
    wait.assert_awaited_once()
    assert set(wait.await_args.args[0]) == {event.raw_message_id}
    assert wait.await_args.kwargs['timeout'] == 5.0
    for text, visual in [('看这个 [图片]', '三只猫'), ('昨天的图 [图片]', '两棵树')]:
        row, = [part for part in contents if visual in part]
        assert text in row and '非发言' in row.split(text, 1)[1].split(visual, 1)[0]
    assert store.raw_message_by_id(old).text == '昨天的图 [图片]'
    assert store.raw_message_by_id(event.raw_message_id).text == event.text == '看这个 [图片]'
    assert '本轮原话："看这个 [图片]"' in contents[-1]


def test_single_displaced_from_history_during_wait(bot, store, monkeypatch):
    bot.config.chat.history_size = 1
    event = message('[图片]')

    async def complete(ids, *, timeout):
        store.set_raw_visual_text(event.raw_message_id, '白色小船')
        store.add_raw_message(platform='recording', chat_type='group', chat_id='g', sender_id='乙',
                              text='后到的消息', ts=NOW + 1)

    _, contents = judge(bot, [event], 0, complete, monkeypatch)
    assert any('白色小船' in part and '[图片]' in part and '非发言' in part for part in contents)
    assert any('后到的消息' in part for part in contents)


def test_batch_waits_once_for_every_ref_and_attributes_visuals(bot, store, monkeypatch):
    # 即使本批/被引图挤出了历史窗，仍要用同会话流水重读快照；异群、同号私聊不能串入。
    bot.config.chat.history_size = 1

    def picture(msg_id, *, chat_id='g', chat_type='group', visual=''):
        return store.add_raw_message(platform='recording', chat_type=chat_type, chat_id=chat_id,
                                     sender_id='丙', text='[图片]', platform_msg_id=msg_id,
                                     visual_text=visual, ts=NOW - 60)

    first_ref, second_ref = picture('pic1'), picture('pic2')
    picture('pic1', chat_id='别群', visual='别群私图')
    picture('pic2', chat_type='private', visual='私聊私图')
    events = [message('[表情]', '乙', reply_to_id='pic1'), message('比较一下', reply_to_id='pic2'),
              message('还有这个 [图片]')]

    async def complete(ids, *, timeout):
        for raw_id, visual in [(first_ref, '绿色按钮'), (second_ref, '蓝色按钮'),
                               (events[0].raw_message_id, '挥手的小人'), (events[2].raw_message_id, '黄色按钮')]:
            store.set_raw_visual_text(raw_id, visual)

    wait, contents = judge(bot, events, 2.5, complete, monkeypatch)
    wait.assert_awaited_once()
    assert set(wait.await_args.args[0]) == {first_ref, second_ref, *(e.raw_message_id for e in events)}
    assert wait.await_args.kwargs['timeout'] == 2.5
    batch, = [part for part in contents if part.startswith('【本批消息')]
    peer, primary = batch.split('发言人：甲', 1)
    assert '发言人：乙' in peer and '绿色按钮' in peer and '挥手的小人' in peer and '非发言' in peer
    assert '蓝色按钮' in primary and '黄色按钮' in primary and '绿色按钮' not in primary
    assert '蓝色按钮' in contents[0]  # 主说话人的引用在上下文要求区也能查到图。
    assert all('别群私图' not in part and '私聊私图' not in part for part in contents)


def test_real_timeout_leaves_recognition_running(bot, store, monkeypatch):
    # 真正的识图任务在等模型，裁定只剩 10ms；放行后模型返回，仍能写回流水。
    resume = asyncio.Event()
    started = asyncio.Event()
    complete = bot.llm.complete

    async def delayed(messages, *, max_tokens=None, **kwargs):
        if kwargs.get('task') == 'image_description':
            started.set()
            await resume.wait()
            return '{"description":"迟到的画面"}'
        return await complete(messages, **kwargs)

    monkeypatch.setattr(bot.llm, 'complete', delayed)
    image = base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
    event = message('[图片]', segments=[Segment('image', {'base64': image})])

    async def run():
        await bot._record(event, NOW - 4.99)
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            await asyncio.wait_for(bot.process_round(MergedRound('recording', 'g', 'group', '甲', '甲',
                                   [event], first_at=NOW - 4.99, hard=True)), timeout=1)
            call, = [call for call in bot.llm.calls if call['task'] == 'gen_stage1']
            assert any(part['content'].endswith('[图片]') for part in call['messages'])
            assert not store.raw_message_by_id(event.raw_message_id).visual_text
        finally:
            resume.set()
            await asyncio.gather(*bot.sticker_behavior._bg_tasks)
        assert store.raw_message_by_id(event.raw_message_id).visual_text == '迟到的画面'
        assert bot.sticker_behavior.visual_counts == dict(success=1, failed=0, timeout=1)

    asyncio.run(run())


@pytest.mark.parametrize('elapsed', [4.99, 8.0])
def test_timeout_proceeds_with_placeholder(bot, monkeypatch, elapsed):
    async def wait_until_deadline(ids, *, timeout):
        # 模拟后台识图没有在剩余预算里产出：接口到期正常返回。
        await asyncio.sleep(timeout)

    event = message('[图片]')
    wait, contents = judge(bot, [event], elapsed, wait_until_deadline, monkeypatch)
    if elapsed < 5 or wait.await_count:
        wait.assert_awaited_once()
        assert wait.await_args.kwargs['timeout'] == pytest.approx(max(0, 5 - elapsed), abs=0.00001)
    assert any(part.endswith('[图片]') for part in contents)
    assert not any('非发言' in part for part in contents)
