"""图片摄入回归：走真实流水；模型和下载均不联网。"""

import asyncio
import base64

import pytest

from conftest import ScriptedLLM
from rubenbot.adapters.base import Segment
from test_entity_replay import bot_for, event


IMAGE = base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()


@pytest.mark.parametrize('chat_type,kind', [('group', 'image'), ('private', 'image'), ('group', 'sticker')])
def test_ingest_without_explicit_vision_or_sticker_collection(config, store, chat_type, kind):
    llm = ScriptedLLM({'vision': ['{"description":"屏幕上有一行字"}']})
    bot, _ = bot_for(config, store, llm)
    msg = event('', segments=[Segment(kind, {'base64': IMAGE})])
    msg.chat_type = chat_type

    async def run():
        await bot._record(msg, 1)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    row = store.raw_message_by_id(msg.raw_message_id)
    assert row.visual_text == '屏幕上有一行字' and row.text == msg.segments[0].render()
    assert [c['task'] for c in llm.calls] == ['image_description']
    assert bot.sticker_behavior.visual_counts['success'] == 1
    assert bot._stickers is None


def test_partial_failure_keeps_visible_parts_and_reports_stage(config, store, caplog):
    llm = ScriptedLLM({'vision': ['不是描述']})
    bot, _ = bot_for(config, store, llm)
    msg = event('', segments=[Segment.face(1), Segment('image', {'base64': IMAGE}),
                              Segment('image', {'description': '车上写着甲'})])

    async def run():
        await bot._record(msg, 1)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    assert store.raw_message_by_id(msg.raw_message_id).visual_text == '车上写着甲'
    counts = bot.sticker_behavior.visual_counts
    assert counts['failed'] == 2 and counts['success'] == 1
    assert f'raw_id={msg.raw_message_id}' in caplog.text
    assert 'image_bytes' in caplog.text and 'invalid_description' in caplog.text


def test_resolve_source_after_unavailable_download(config, store):
    llm = ScriptedLLM({'vision': ['{"description":"看见一只猫"}']})
    bot, adapter = bot_for(config, store, llm)
    msg = event('', segments=[Segment('image', {'url': 'https://example.invalid/img', 'file': 'abc.jpg'})])
    calls = []

    async def download(url, limit):
        calls.append(url)
        raise OSError('下载失败')

    async def resolve(segment):
        calls.append(segment.data['file'])
        return {'base64': IMAGE}

    bot.sticker_behavior._downloader = download
    adapter.resolve_image_source = resolve

    async def run():
        await bot._record(msg, 1)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    assert calls == ['https://example.invalid/img', 'abc.jpg']
    assert store.raw_message_by_id(msg.raw_message_id).visual_text == '看见一只猫'


def test_image_ready_does_not_wait_for_collecting_captioned_sticker(config, store, tmp_path):
    config.stickers.enabled = config.stickers.collect = True
    config.stickers.dir = str(tmp_path / 'stickers')
    bot, _ = bot_for(config, store, ScriptedLLM({'vision': ['{"description":"画着猫"}', '{"description":"笑脸"}']}))
    msg = event('', segments=[Segment.sticker(url='https://example.invalid', description='挥手'),
                              Segment('image', {'base64': IMAGE, 'summary': '[动画表情]'})])
    started, release = asyncio.Event(), asyncio.Event()

    async def download(url, limit):
        started.set()
        await release.wait()
        return b'GIF89aother'

    async def run():
        behavior = bot.sticker_behavior
        behavior._downloader = download
        await bot._record(msg, 1)
        waiting = asyncio.create_task(behavior.wait_for_visuals([msg.raw_message_id], timeout=1))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(waiting, 0.25)
            assert store.raw_message_by_id(msg.raw_message_id).visual_text == '挥手\n画着猫'
            assert behavior.visual_counts == dict(success=2, failed=0, timeout=0)
            assert any(item.description == '画着猫' for item in bot.stickers.find_by_tags(None))
        finally:
            release.set()
            while behavior._bg_tasks:
                await asyncio.gather(*behavior._bg_tasks)

    asyncio.run(run())
