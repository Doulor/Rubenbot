"""从真实 loader 到出站段、core 预取与原文流水的离线回放。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.linkparse.fetch import FetchError, FetchResult
from rubenbot.persona import Persona
from rubenbot.plugins.config_schema import normalize_schema
from rubenbot.plugins.loader import PluginContext, PluginManager
from rubenbot.plugins.manifest import load_manifest

DIR = Path(__file__).resolve().parents[1] / 'plugins' / 'link_detail'
BILI = 'https://www.bilibili.com/video/BV1xx411c7mD'
X = 'https://x.com/author/status/123456'
NEWS = 'https://news.example/story'
COVER = 'https://i0.hdslb.com/bfs/archive/cover.jpg'


def message(text, group='g', num='1', **kwargs):
    return MessageEvent('recording', 'group', group, 'u', '分享者', text, platform_msg_id=num, **kwargs)


@pytest.fixture
def setup(config, store, monkeypatch):
    config.plugins.dir = str(DIR.parent)
    config.plugins.enabled = ['link_detail']
    config.link.mode = 'auto'
    reads, images, generic = [], [], []

    async def page(self, url, *, allow_url, **kwargs):
        assert allow_url(url)
        reads.append(url)
        if url.startswith('https://api.bilibili.com/x/web-interface/view?bvid='):
            bvid = url.split('bvid=')[-1]
            data = {'bvid': bvid, 'title': '实录', 'pic': COVER, 'owner': {'name': '汀兰'},
                    'duration': 81, 'stat': {'view': 230, 'like': 0}}
            return FetchResult(url, 'application/json', json.dumps({'code': 0, 'data': data}))
        return FetchResult(url, 'text/html', '<title>请登录</title>')

    async def cover(self, url, **kwargs):
        images.append(url)
        assert url == COVER
        return b'cover'

    async def common(url):
        generic.append(url)
        return FetchResult(url, 'text/html', '<title>文章</title><article>普通正文</article>')

    monkeypatch.setattr(PluginContext, 'fetch_public_page', page)
    monkeypatch.setattr(PluginContext, 'fetch_public_cover', cover)
    loader = PluginManager(config, store)
    assert [p.name for p in loader.load_all()] == ['link_detail']
    loader.set_permissions('link_detail', ['read_messages', 'network', 'send_messages'])
    llm = ScriptedLLM({'fast': ['{"basis":"common","speak":false}']})
    bot = RubenBot(config, store, llm, Persona(name='Ruben', text='测试'), plugins=loader)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    bot.link_service.fetch = common
    return SimpleNamespace(bot=bot, adapter=adapter, reads=reads, images=images, generic=generic,
                           loader=loader, llm=llm, hook=loader.find_plugin('link_detail').message_hooks[0])


def test_bili_group_reply_without_mention_and_cooldown(setup):
    s = setup

    async def replay():
        await s.bot.handle_event(message(BILI))
        await s.bot.handle_event(message(BILI + '?utm_source=share', num='2'))
        await s.bot.handle_event(message(BILI, group='other'))
    asyncio.run(replay())
    assert len(s.reads) == len(s.images) == len(s.adapter.sent) == 2
    assert not s.generic and not s.llm.chat_messages
    group, mode, segments = s.adapter.sent[0]
    assert (group, mode) == ('g', 'group')
    assert [seg.type for seg in segments] == ['reply', 'image', 'text']
    assert segments[0].data['id'] == '1' and segments[1].data['file'] == 'base64://Y292ZXI='
    assert all(word in segments[-1].render() for word in ('实录', '汀兰', '1:21', '230', '点赞：0', BILI))


def test_x_shell_only_sends_failure(setup):
    s = setup
    asyncio.run(s.bot.handle_event(message(X)))
    assert len(s.adapter.texts) == 1 and '抓不到' in s.adapter.texts[0]
    assert not s.images and not s.generic and '请登录' not in s.adapter.texts[0]


def test_mixed_urls_only_unclaimed_go_to_core(setup, store):
    s = setup
    text = f'看看 ({BILI}?utm_source=chat)，另一个 {NEWS}'
    asyncio.run(s.bot.handle_event(message(text)))
    assert s.generic == [NEWS]
    assert store.message_by_platform_id('recording', '1', chat_id='g').text == text
    assert s.images == [COVER]


def test_limit_and_config_hot_apply(setup):
    s = setup
    assert s.loader.apply_config_change('link_detail', {}, {'max_links': 1, 'send_video': True})[0]
    event = message(f'{BILI} {X}')
    asyncio.run(s.bot.handle_event(event))
    # X 超出插件上限后交给 core：core 先读 fxtwitter 镜像，再读 x.com 网页。
    assert s.generic == ['https://api.fxtwitter.com/author/status/123456', X] and s.images == [COVER]
    assert all(seg.type != 'video' for _, _, segs in s.adapter.sent for seg in segs)
    assert s.loader.apply_config_change('link_detail', {}, {'enable_bilibili': False})[0]
    asyncio.run(s.bot.handle_event(message(BILI, group='another')))
    assert s.generic[-1] == BILI and len(s.images) == 1


@pytest.mark.parametrize('missing', ['read_messages', 'send_messages', 'network'])
def test_no_permission_does_not_claim(setup, missing):
    s = setup
    s.loader.set_permissions('link_detail', {'read_messages', 'send_messages', 'network'} - {missing})
    asyncio.run(s.bot.handle_event(message(BILI)))
    assert s.generic == [BILI] and not s.reads and not s.images


def test_private_and_non_content_urls_do_not_claim(setup):
    s = setup
    private = MessageEvent('recording', 'private', 'user', 'user', '分享者', BILI)
    runtime = SimpleNamespace(adapters={'recording': s.adapter})
    async def replay():
        for event in [private, message('https://www.bilibili.com/'), message('https://pixiv.net.evil.test/artworks/12')]:
            assert await s.hook(runtime, event) is None
    asyncio.run(replay())
    assert not s.reads and not s.adapter.sent


def test_inflight_dedup_and_failed_cover_still_returns_text(setup, monkeypatch):
    s = setup
    runtime = SimpleNamespace(adapters={'recording': s.adapter})

    async def replay():
        started, resume = asyncio.Event(), asyncio.Event()
        async def blocked(self, url, **kwargs):
            started.set()
            await resume.wait()
            raise FetchError('封面不是公开图片')
        monkeypatch.setattr(PluginContext, 'fetch_public_cover', blocked)
        one = asyncio.create_task(s.hook(runtime, message(BILI)))
        await asyncio.wait_for(started.wait(), 2)
        two = asyncio.create_task(s.hook(runtime, message(BILI, num='2')))
        resume.set()
        assert await one is False and await two is False
    asyncio.run(replay())
    assert len(s.reads) == len(s.adapter.sent) == 1 and '封面抓不到' in s.adapter.texts[0]


def test_delivery_failure_returns_ownership_to_core(setup, monkeypatch):
    s = setup
    async def fail(*args, **kwargs):
        raise RuntimeError('适配器未连接')
    monkeypatch.setattr(s.adapter, 'send', fail)
    asyncio.run(s.bot.handle_event(message(BILI)))
    assert s.generic == [BILI] and not s.adapter.sent


def test_message_to_bot_with_single_link_reaches_verdict(setup, store):
    """@bot 提问 + 单链接：插件不认领，整条交给 core 进裁定（第 38 项）。

    线上事故：群 1059662053 里 @bot 让总结一条 X 链接，插件把整条消息当
    「作品分享」回复详情并返回 False，消息从此不进合批与裁定。
    """
    s = setup
    text = f'@Ruben 总结一下这个的内容\r\n{X}'
    asyncio.run(s.bot.handle_event(message(text, mentions_bot=True)))
    # 插件既没读详情也没发回执：这条消息不是一次分享，是一次提问
    assert not s.reads
    assert not any('抓不到' in text or '【X】' in text for text in s.adapter.texts)
    # 链接交给 core 感知层读取，消息进了裁定并留下 decision_log
    assert X in s.generic
    logs = store.list_decision_logs(chat_id='g')
    assert logs and logs[0]['chat_id'] == 'g'
    assert not logs[0]['reason'].startswith('plugin_intercept')


def test_reply_to_bot_with_single_link_reaches_verdict(setup, store):
    """回复 bot 的消息同样是指向 bot 的提问，插件不认领。"""
    s = setup
    asyncio.run(s.bot.handle_event(message(BILI, reply_to_id='99', reply_sender_id='__bot__')))
    assert not s.reads
    assert not any('【B站】' in text for text in s.adapter.texts)
    logs = store.list_decision_logs(chat_id='g')
    assert logs and not logs[0]['reason'].startswith('plugin_intercept')


def test_plain_share_still_expanded(setup, store):
    """回归：不指向 bot 的普通分享链接照旧由插件展开，core 不重复读。

    插件认领后仍会拦截这条消息（插件已回详情），但按第 38 项留下一条
    ``plugin_intercept:link_detail`` 记录，不再是无痕的静默拦截。
    """
    s = setup
    asyncio.run(s.bot.handle_event(message(BILI)))
    assert s.reads == ['https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD']
    assert len(s.adapter.sent) == 1 and '实录' in s.adapter.texts[0]
    assert not s.generic
    log, = store.list_decision_logs(chat_id='g')
    assert log['reason'] == 'plugin_intercept:link_detail'


def test_manifest_schema_and_defaults(config):
    manifest = load_manifest(DIR)
    schema = {f.key: f for f in normalize_schema(manifest.config_schema)}
    platforms = [f for k, f in schema.items() if k.startswith('enable_')]
    assert len(platforms) == 17 and all(f.type == 'bool' and f.default for f in platforms)
    assert schema['max_links'].max == 3 and not schema['send_video'].default
    assert manifest.id not in config.plugins.enabled
