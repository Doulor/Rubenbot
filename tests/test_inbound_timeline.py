"""入站即时记账；离线回放检查实际送达的回答，不调用外网。"""
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import MemoryConfig
from rubenbot.core import RubenBot
from rubenbot.memory.extract import MemoryExtractor
from rubenbot.memory.store import MemoryStore, ScheduledJob
from rubenbot.memory.timeline import recent_related_events, recent_timeline
from rubenbot.plugins.loader import PluginManager
from test_music_plugin import RecordingAdapter as MusicAdapter
from test_music_plugin import plugin as music
from rubenbot.persona import Persona


def incoming(place, speaker, text, **kwargs):
    return MessageEvent('recording', 'private' if place == speaker else 'group', place,
                        speaker, speaker, text, **kwargs)


def setup_bot(config, store, model=None):
    bot = RubenBot(config, store, model or ScriptedLLM(), Persona('Ruben', '一个群友'))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def test_placeholder_and_late_visual_update_same_event(config, store):
    bot, _ = setup_bot(config, store)
    event = incoming('group', 'author', '看这张 [图片]', segments=[Segment('image', {})])
    asyncio.run(bot._record(event, 100))
    first, = store.list_events(person_id='recording:author')
    assert first.source_raw_id == event.raw_message_id and first.ts == 100
    assert first.summary == event.text and first.salience < 0.5
    assert store.set_raw_visual_text(first.source_raw_id, '微博截图写着：你不理她是装的')
    enriched, = store.list_events(person_id='recording:author')
    assert enriched.id == first.id and enriched.ts == first.ts
    assert '你不理她是装的' in enriched.summary and '看这张' in enriched.summary


def test_voice_visible_before_transcription_and_updates_in_place(config, store, monkeypatch):
    bot, _ = setup_bot(config, store)
    msg = incoming('group', 'author', '', platform_msg_id='voice1', segments=[Segment('record', {})])
    ids = []

    async def transcribe(event):
        pending, = store.list_events(person_id='recording:author')
        assert pending.summary == '[语音]' and pending.source_raw_id == event.raw_message_id
        ids.append(pending.id)
        event.text = '[语音] 我刚到了公园'

    monkeypatch.setattr(bot, 'prepare_voice', transcribe)
    asyncio.run(bot.handle_event(msg))
    finished, = store.list_events(person_id='recording:author')
    assert ids == [finished.id] and finished.summary == store.raw_message_by_id(msg.raw_message_id).text == msg.text


def test_related_uses_structured_participants_not_names(config, store):
    bot, _ = setup_bot(config, store)
    now = time.time()
    target = incoming('u', 'u', '刚刚提到我的事')
    for src in (incoming('g', 'a', '分给你那份', mentions={'u': '某人'}),
                incoming('g', 'b', '确认收到了', reply_sender_id='u'), incoming('g', 'c', 'u同名'),
                incoming('g', 'u', '我也在场'), incoming('g', 'z', '不相干')):
        asyncio.run(bot._record(src, now - 1))
    asyncio.run(bot._record(incoming('g', 'u', '已经过期'), now - 86401))
    lines = asyncio.run(recent_related_events(store, target, now=now))
    assert len(lines) == 3 and len('\n'.join(lines)) <= 600
    assert all(any(text in line for line in lines) for text in ('分给你那份', '确认收到了', '我也在场'))
    assert not asyncio.run(recent_related_events(store, incoming('g', 'u', '问'), now=now))
    store.add_self_event('我也回了', source_chat='recording:g', audience='group:g', now=now)
    assert any('我也回了' in line for line in asyncio.run(recent_timeline(store, target, now=now)))


class ReplayModel(ScriptedLLM):
    """只从本次实际收到的历史证据摘句；未找到时明确答不出。"""

    async def complete(self, messages, *, role='chat', task=None, **kwargs):
        contents = '\n'.join(m['content'] for m in messages)
        self.calls.append({'task': task, 'messages': messages})
        if task == 'gen_stage1':
            return json.dumps({'basis': 'ledger', 'speak': True, 'intent': '补充新信息', 'increment': '复述所见'})
        if task == 'gen_stage2':
            for evidence in ('你不理她是装的，但她不理你是真的', '10块一年的.com域名'):
                if evidence in contents:
                    return evidence
        return '没有找到原话'


@pytest.mark.parametrize('media', ['image', 'bilibili'])
def test_private_question_replay(config, store, monkeypatch, media):
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now)
    config.chat.history_size = 2
    config.memory.timeline_max_chars = 0  # 主预算耗尽仍能接续
    bot, adapter = setup_bot(config, store, ReplayModel())
    if media == 'image':
        answer = '你不理她是装的，但她不理你是真的'
        text = '微博帖子截图。' + '画面顶部有状态栏与发帖导航。' * 10 + '正文：' + answer
        photo = incoming('room', 'author', '@Ruben [图片]', segments=[Segment('image', {'description': text})])
        asyncio.run(bot._record(photo, now - 74))
        question = 'ruben你记得我刚刚给你发了什么吗'
    else:
        answer = '10块一年的.com域名'
        age = now - 14.5 * 3600
        # 私聊链接无标题；用合成 BV 号代替真实跟踪参数，不取网页正文。
        asyncio.run(bot._record(incoming('author', 'author', 'https://www.bilibili.com/video/BVexample'), age))
        for i, text in enumerate(('这是白洛奇那条', answer)):
            store.add_raw_message(platform='recording', chat_type='private', chat_id='author', sender_id='__bot__',
                                  text=text, ts=age + 28 + i)
        # 同窗还有一条原库已有的群事件（对应的群流水已裁剪）。它是独立于 bot 旧话的证据。
        store.add_event('recording:author', '分享了一个关于「10块一年的.com域名」的B站视频，并问群友有没有试过',
                        source_chat='recording:room', source_chat_type='group', audience='group:room', ts=age + 30)
        for i in range(5):
            asyncio.run(bot._record(incoming('author', 'author', f'另外一件事{i}'), now - 50 + i))
            asyncio.run(bot._record(incoming('room', 'author', '今天随手记下了什么' * 20), now - 50 + i))
        question = '我昨晚在私聊发的bilibili链接讲了啥？'
    asyncio.run(bot.handle_event(incoming('author', 'author', question)))
    assert adapter.texts[-1] == answer
    stages = [c for c in bot.llm.calls if c['task'] in ('gen_stage1', 'gen_stage2')]
    assert [c['task'] for c in stages] == ['gen_stage1', 'gen_stage2']
    for call in stages:
        assert any(answer in m['content'] and '【与你对话的人在别处参与的事】' in m['content']
                   and m['role'] == 'user' for m in call['messages'])


@pytest.fixture
def db():
    store = MemoryStore(':memory:', privacy_enabled=True)
    store.upsert_person('console', 'a', '甲')
    yield store
    store.close()


def raw(db, text='我喜欢唱歌'):
    return db.add_raw_message(platform='console', chat_id='g', chat_type='group', sender_id='a', text=text, ts=700000)


def prewrite(db, rid, text='我喜欢唱歌', person='console:a', kind='inbound', **fields):
    return db.add_event(person, text, source_raw_id=rid, source_chat='console:g', audience='group:g',
                        salience=0.2, ts=700000, now=700010, kind=kind, **fields)


def extract(db, events, facts=(), owner=False):
    class Fake:
        async def complete(self, *args, **kwargs):
            return json.dumps(dict(events=events, facts=facts), ensure_ascii=False)

    return asyncio.run(MemoryExtractor(Fake(), db, MemoryConfig(), is_owner=lambda _: owner)
                       .extract_sender(db.pending_senders()[0], now=990000))


def test_extract_updates_only_matching_event(db):
    rid = raw(db)
    original = prewrite(db, rid)
    digest = prewrite(db, rid, kind='digest')
    other = prewrite(db, rid, person='console:b')
    missing = prewrite(db, None)
    historic = prewrite(db, rid, kind='event')
    result = extract(db, [dict(source_msg_id=rid, summary='TA 喜欢唱歌', salience=0.9, time_hint='前天',
                               sensitivity='high'),
                          dict(source_msg_id=rid, summary='TA 获得了比赛冠军', salience=1.0),
                          dict(source_msg_id=rid, summary='TA 喜欢唱歌', salience=0.4)], owner=True)
    assert not result.added_events and result.skipped_events == 3
    assert db.get_event(original.id) == replace(original, salience=0.9, sensitivity='high')
    others = [digest, other, missing, historic]
    assert [db.get_event(e.id) for e in others] == others


@pytest.mark.parametrize(('text', 'summary', 'owner', 'target'), [
    ('我喜欢唱歌', 'TA 喜欢唱歌', True, 'owner'),
    ('我喜欢唱歌', 'TA 喜欢跳舞', True, 'group:g'),
    ('我喜欢唱歌', 'TA 喜欢唱歌', False, 'group:g'),
    ('我喜欢唱歌，我的地址在湖边', 'TA 喜欢唱歌', True, 'group:g'),
])
def test_extract_promotes_only_verified_whole_owner_statement(db, text, summary, owner, target):
    rid = raw(db, text)
    original = prewrite(db, rid, text)
    extract(db, [dict(summary=summary, source_msg_id=rid, self_reported=True)], owner=owner)
    expected_salience = 0.2 if summary == 'TA 喜欢跳舞' else 0.5
    assert db.get_event(original.id) == replace(original, salience=expected_salience, audience=target)


@pytest.mark.parametrize('source', [None, 999])
def test_extract_refuses_unattributed_event_from_prewritten_batch(db, source):
    rid = raw(db)
    original = prewrite(db, rid)
    legacy = raw(db, '我去年买了自行车')
    result = extract(db, [dict(source_msg_id=source, summary='TA 喜欢古典音乐'),
                          dict(source_msg_id=legacy, summary='TA 买了自行车'),
                          dict(source_msg_id=legacy, summary='TA 去年开始骑行')])
    assert result.skipped_events == 1
    assert result.added_events == ['TA 买了自行车', 'TA 去年开始骑行']
    assert db.get_event(original.id) == original


def test_visual_evidence_can_raise_salience_without_replacing_the_inbound_summary(db):
    rid = raw(db, '[图片]')
    assert db.set_raw_visual_text(rid, '图中有钢琴')
    original = prewrite(db, rid, '[图片] 图中有钢琴')
    extract(db, [dict(source_msg_id=rid, summary='图中有钢琴', salience=0.8, self_reported=True)], owner=True)
    assert db.get_event(original.id) == replace(original, salience=0.8)


def test_extract_restrictions_apply_even_without_valid_event_proposal(db):
    rid = raw(db, '我昨天回来了')
    original = prewrite(db, rid, '我昨天回来了')
    result = extract(db, [dict(source_msg_id=rid, summary='觉得它不错', salience=1)],
                     facts=[dict(source_msg_id=rid, content='TA 最近在治疗', sensitivity='high')])
    assert result.dropped == ['觉得它不错']
    assert db.get_event(original.id) == replace(original, sensitivity='high')


def test_extract_does_not_relax_sealed_prewrite(db):
    rid = raw(db)
    original = prewrite(db, rid, sensitivity='sealed')
    extract(db, [dict(source_msg_id=rid, summary='TA 喜欢唱歌', sensitivity='high', self_reported=True)], owner=True)
    assert db.get_event(original.id) == replace(original, salience=0.5)


def _action_bot(config, store, plugin_name=None):
    manager = None
    if plugin_name:
        config.plugins.dir = str(Path.cwd() / 'plugins')
        config.plugins.enabled = [plugin_name]
        manager = PluginManager(config, store)
        manager.load_all()
    bot = RubenBot(config, store, ScriptedLLM(), Persona('Ruben', '你是 Ruben'), plugins=manager)
    return bot


def _action_message(text, platform):
    return MessageEvent(platform=platform, chat_type='group', chat_id='g1', sender_id='u1', sender_name='群友', text=text)


@pytest.mark.parametrize('succeeds', [True, False])
def test_action_qzone_publish_only(config, store, succeeds):
    bot = _action_bot(config, store, 'qzone_post')
    adapter = RecordingAdapter()
    adapter.capabilities = {'qzone.publish'}
    adapter.call_capability = AsyncMock(side_effect=None if succeeds else RuntimeError('发布被拒'))
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(_action_message('/说说 云散了', adapter.name)))
    adapter.call_capability.assert_awaited_once_with('qzone.publish', content='云散了')
    events = store.list_events(person_id='self')
    assert [(e.summary, e.source_chat, e.audience) for e in events] == (
        [('发了空间说说：云散了', 'recording:qzone', 'unknown')] if succeeds else []
    )


@pytest.mark.parametrize(('failed', 'expected'), [
    (set(), ['记得喝水', '水杯在桌上']), ({'记得喝水'}, ['水杯在桌上']), ({'记得喝水', '水杯在桌上'}, []),
])
def test_action_reminder_records_only_delivered_parts(config, store, failed, expected):
    bot = _action_bot(config, store)
    bot.generate = AsyncMock(return_value='记得喝水\n水杯在桌上')
    adapter = RecordingAdapter()

    async def deliver(chat_id, segments, *, chat_type):
        if segments[0].data['text'] in failed:
            raise RuntimeError('投递失败')

    adapter.send = deliver
    bot.register_adapter(adapter)
    job = ScheduledJob(id=12, platform=adapter.name, chat_id='u1', chat_type='private', payload={'text': '喝水'})
    asyncio.run(bot.handle_scheduled_job(job))
    events = store.list_events(person_id='self')
    assert [(e.summary, e.source_chat, e.audience) for e in events] == (
        [('投递了提醒：' + '\n'.join(expected), 'recording:u1', 'private')] if expected else []
    )
    assert sorted(m.text for m in store.recent_messages('u1')) == sorted(expected)


@pytest.mark.parametrize(('mode', 'summary'), [
    ('record', '点歌，发了一首歌'), ('card', '点歌，发了一首歌'),
    ('file', '点歌，发了《晴天》'), ('text', None), ('failed', None), ('denied', None), ('all_failed', None),
])
def test_action_music_after_selection(config, store, mode, summary):
    bot = _action_bot(config, store, 'music')
    player = bot.plugins.extras['music']
    modes = ('record', 'file', 'text') if mode in ('file', 'failed', 'all_failed', 'denied') else (mode,)
    player.sender.config = music.MusicConfig(send_modes=modes, enable_comments=False, enable_lyrics=False)
    songs = [music.Song(str(i), name, source='netease', audio_url='https://example.org/song.mp3')
             for i, name in enumerate(['雨天', '晴天'])]
    player.search = AsyncMock(return_value=songs)
    player.sender.download_audio = AsyncMock(return_value=b'mp3')
    adapter = MusicAdapter(fail_record=mode in ('file', 'failed', 'all_failed'), fail_file=mode in ('failed', 'all_failed'))
    bot.register_adapter(adapter)

    async def scenario():
        try:
            await bot.handle_event(_action_message('点歌 天', adapter.name))
            assert store.list_events(person_id='self') == []
            assert len(player._pending) == 1
            if mode == 'denied':
                bot.plugins.permissions.revoke('music', 'send_messages')
            if mode == 'all_failed':
                adapter.send = AsyncMock(side_effect=RuntimeError('平台拒投'))
            await bot.handle_event(_action_message('2', adapter.name))
            assert not player._pending
        finally:
            await player.aclose()

    asyncio.run(scenario())
    events = store.list_events(person_id='self')
    assert [(e.summary, e.source_chat, e.audience) for e in events] == (
        [(summary, 'snowluma:g1', 'group:g1')] if summary else []
    )
    if mode in ('denied', 'all_failed'):
        assert len(adapter.sent) == 1  # 只有选歌列表
    else:
        assert any('晴天' in text for text in adapter.texts[1:])
