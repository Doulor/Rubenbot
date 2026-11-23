"""在实际裁定请求里核对本批、历史及引用的图片快照；不调用真实视觉模型。"""

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
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
    # 等待上限按实测 p95 拉长到 15 秒：触发图本身就是话头，值得等描述落库。
    assert wait.await_args.kwargs['timeout'] == 15.0
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
    # 本批带图的流水；被回复的图已有描述且不在途，不再为它等。
    assert set(wait.await_args.args[0]) == {events[0].raw_message_id, events[2].raw_message_id}
    assert wait.await_args.kwargs['timeout'] == 15.0
    batch, = [part for part in contents if part.startswith('【本批消息')]
    peer, primary = batch.split('发言人：甲', 1)
    assert '发言人：乙' in peer and '绿色按钮' in peer and '挥手的小人' in peer and '非发言' in peer
    assert '蓝色按钮' in primary and '黄色按钮' in primary and '绿色按钮' not in primary
    assert '蓝色按钮' in contents[0]  # 主说话人的引用在上下文要求区也能查到图。
    assert all('别群私图' not in part and '私聊私图' not in part for part in contents)


def test_real_timeout_leaves_recognition_running(bot, store, monkeypatch):
    # 真正的识图任务在等模型；等满上限后按缺失裁定，放行后模型返回仍能写回流水。
    bot.sticker_behavior.config = _with_wait(bot.config.stickers, 0.05)
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
        await bot._record(event, NOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            await asyncio.wait_for(bot.process_round(MergedRound('recording', 'g', 'group', '甲', '甲',
                                   [event], first_at=NOW, hard=True)), timeout=2)
            call, = [call for call in bot.llm.calls if call['task'] == 'gen_stage1']
            assert any('[图片]〔视觉描述缺失〕' in part['content'] for part in call['messages'])
            audit, = store.list_decision_logs()
            assert audit['text'] == '[图片]〔视觉描述缺失〕'
            assert not store.raw_message_by_id(event.raw_message_id).visual_text
            # 等满超时要在裁定日志里留痕，不能与「压根没描述请求」混在一起。
            wait = audit['factors']['visual_wait']
            assert wait['timed_out'] == [event.raw_message_id] and wait['waited_ms'] >= 40
        finally:
            resume.set()
            await asyncio.gather(*bot.sticker_behavior._bg_tasks)
        assert store.raw_message_by_id(event.raw_message_id).visual_text == '迟到的画面'
        assert bot.sticker_behavior.visual_counts == dict(success=1, failed=0, timeout=1)

    asyncio.run(run())


def _with_wait(config, seconds):
    from dataclasses import replace

    return replace(config, vision_wait_seconds=seconds)


def test_timeout_proceeds_with_placeholder(bot, monkeypatch):
    async def wait_until_deadline(ids, *, timeout):
        # 模拟后台识图没有在上限内产出：接口到期正常返回。
        assert timeout == 15.0

    event = message('[图片]')
    wait, contents = judge(bot, [event], 8.0, wait_until_deadline, monkeypatch)
    wait.assert_awaited_once()
    assert wait.await_args.kwargs['timeout'] == 15.0
    assert any(part.endswith('[图片]') for part in contents)
    assert not any('非发言' in part for part in contents)


@pytest.mark.parametrize('intent,draft,reason', [
    ('沉默', '', 'planner_silence'),
    ('短附和', '抱抱', 'private'),
    ('短附和', '图也不说是吧', 'image_behavior_veto'),
    ('短附和', '发图不说话', 'image_behavior_veto'),
    ('短附和', '这张图里的猫怎么哭成这样', 'private'),
    ('表情回应', '', 'sticker_unavailable'),
])
def test_crying_sticker_replay(bot, store, intent, draft, reason):
    # 相同私聊贴纸可以接住情绪，也能只看不回；普通文字不被随机替换成表情。
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    bot.llm.responses = {'fast': ['{"basis":"settled","intent":"%s","increment":"接住委屈"}' % intent],
                         'chat': [draft]}
    msg = MessageEvent('recording', 'private', 'g', '甲', '甲', '',
                       segments=[Segment.sticker(description='小猫眼角挂着两串泪珠')])
    asyncio.run(bot.handle_event(msg))
    planner, = [call for call in bot.llm.calls if call['task'] == 'gen_stage1']
    note = '[表情]〔图片视觉描述·非发言：小猫眼角挂着两串泪珠〕'
    assert any(note in part['content'] for part in planner['messages'])
    audit, = store.list_decision_logs()
    assert audit['text'] == note and audit['reason'] == reason
    assert adapter.texts == ([draft] if reason == 'private' else [])
    assert bool(bot.llm.chat_messages) == bool(draft)


def test_captioned_question_keeps_normal_direct_override(bot, store):
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    bot.llm.responses = {'fast': ['{"basis":"settled","speak":false,"intent":"沉默"}'],
                         'chat': ['看起来是猫']}
    msg = message('@Ruben 这是什么？[图片]', mentions_bot=True,
                  segments=[Segment.text('@Ruben 这是什么？'), Segment('image', {'description': '一只猫'})])
    asyncio.run(bot.handle_event(msg))
    assert adapter.texts == ['看起来是猫']
    assert store.list_decision_logs()[0]['factors']['planner_override'] == 'mentioned'


def test_image_scold_is_not_sent_for_a_mixed_batch(bot, store):
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    bot.llm.responses = {'fast': ['{"basis":"settled","intent":"短附和"}'], 'chat': ['发图不说话是吧']}
    bot.merger.window = bot.merger.hard_window = 10

    async def run():
        await bot.handle_event(message('', segments=[Segment.sticker(description='红红的眼睛')]))
        await bot.handle_event(message('诶'))
        await bot.flush_merger(all_rounds=True)

    asyncio.run(run())
    assert adapter.texts == []
    assert store.list_decision_logs()[0]['reason'] == 'image_behavior_veto'
