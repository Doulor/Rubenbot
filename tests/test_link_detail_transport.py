"""宿主的受检插件网络入口；只替换 DNS 答案及已固定地址的连接，不出网。"""

import asyncio
from base64 import b64decode
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from rubenbot.linkparse import fetch as public_web
from rubenbot.linkparse import sites
from rubenbot.plugins import public_cover
from rubenbot.plugins.loader import Plugin, PluginContext, PluginManager
from rubenbot.plugins.permissions import PluginPermissionError


# 离线生成的三张 16×16 黑图。
JPEG = b64decode(
    '/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYGBgYG'
    'BgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAA'
    'AAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/A'
    'ABEIABAAEAMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z'
)
PNG = b64decode('iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAACXBIWXMAAAABAAAAAQBP'
                'JcTWAAAAFElEQVR4nGNkIBGwjGoY1TB8NQAAYgAAPn161xsAAAAASUVORK5CYII=')
WEBP = b64decode('UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoQABAAAgA0JaQAA3AA/vv9UAA=')


@pytest.fixture
def ctx(tmp_path):
    manager = PluginManager(None, None)
    plugin = Plugin('sample', tmp_path)
    manager.permissions.grant(plugin.name, 'network')
    return PluginContext(plugin, manager)


class Reply:
    def __init__(self, *chunks, status=200, **headers):
        self.status = status
        self.headers = headers
        self.chunks = chunks
        self.read = False

    async def aiter_stream(self):
        self.read = True
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def install(reply):
        @asynccontextmanager
        async def open_(url, ip, port, headers):
            calls.append((str(url), ip, port, headers))
            yield reply

        monkeypatch.setattr(public_web, '_open', open_)
        return calls

    return install


@pytest.mark.parametrize('method', ['fetch_public_page', 'fetch_public_cover'])
def test_revocation_before_url_or_network(ctx, method, monkeypatch, caplog):
    fetch = getattr(ctx, method)
    ctx.permissions.revoke('sample', 'network')
    monkeypatch.setattr(public_web, '_url', lambda *_: pytest.fail('不应检查目标地址'))
    with pytest.raises(PluginPermissionError) as exc:
        asyncio.run(fetch('https://example.com/?private=1'))
    assert exc.value.permission == 'network'
    assert '已拦截' in caplog.text and 'private' not in caplog.text


def test_page_delegates_with_host_limits(ctx, monkeypatch):
    page = public_web.FetchResult('https://example.com/', 'application/json', '{}')
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(public_web, 'fetch', fetch)
    policy = page.final_url.__eq__
    assert asyncio.run(ctx.fetch_public_page(page.final_url, allow_url=policy)) is page
    fetch.assert_awaited_once_with(page.final_url, 1 << 20, 6.0, allow_url=policy, prepare=sites.prepare)


@pytest.mark.parametrize('allow_mobile', [False, True])
def test_page_prepares_and_checks_shortlink_destination(ctx, monkeypatch, allow_mobile):
    short = 'https://b23.tv/sample'
    desktop = 'https://www.bilibili.com/video/BV123/'
    mobile = 'https://m.bilibili.com/video/BV123/'
    checked, opened = [], []

    def policy(url):
        checked.append(url)
        return url != mobile or allow_mobile

    @asynccontextmanager
    async def open_(target, ip, port, headers):
        opened.append(str(target))
        if str(target) == short:
            yield Reply(status=302, location=desktop)
        else:
            assert str(target) == mobile and headers['User-Agent'] == sites.MOBILE_UA
            yield Reply(b'page')

    resolve = AsyncMock(return_value='8.8.8.8')
    monkeypatch.setattr(public_web, '_resolve', resolve)
    monkeypatch.setattr(public_web, '_open', open_)
    if allow_mobile:
        page = asyncio.run(ctx.fetch_public_page(short, allow_url=policy))
        assert (page.final_url, page.text) == (desktop, 'page')
    else:
        with pytest.raises(public_web.FetchError, match='策略'):
            asyncio.run(ctx.fetch_public_page(short, allow_url=policy))
    assert checked == [short, desktop, mobile]
    assert opened == ([short, mobile] if allow_mobile else [short])
    assert [call.args[0] for call in resolve.await_args_list] == (
        ['b23.tv', 'm.bilibili.com'] if allow_mobile else ['b23.tv']
    )


def test_page_checks_redirect_policy(ctx, wire):
    source = 'https://8.8.8.8/'
    calls = wire(Reply(status=302, location='https://9.9.9.9/'))
    with pytest.raises(public_web.FetchError, match='策略'):
        asyncio.run(ctx.fetch_public_page(source, allow_url=source.__eq__))
    assert [c[0] for c in calls] == [source]


@pytest.mark.parametrize(('host', 'body', 'referer'), [
    ('i.pximg.net', JPEG, 'https://www.pixiv.net/'),
    ('i0.hdslb.com', WEBP, 'https://www.bilibili.com/'),
    ('notpximg.net', PNG, None),
])
def test_cover_uses_one_resolution_and_pinned_connection(ctx, wire, monkeypatch, host, body, referer):
    calls = wire(Reply(body[:20], body[20:], **{'content-type': 'application/octet-stream'}))
    dns = AsyncMock(return_value=[(0, 0, 0, '', ('8.8.8.8', 8443))])
    url = f'https://{host}:8443/img'

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        return await ctx.fetch_public_cover(url + '#fragment', allow_url=url.__eq__)

    result = asyncio.run(run())
    assert type(result) is bytes and result == body
    assert dns.await_count == 1 and dns.await_args.args == (host, 8443)
    [(target, ip, port, headers)] = calls
    assert (target, ip, port) == (url, '8.8.8.8', 8443)
    assert headers.get('Referer') == referer and headers['Accept-Encoding'] == 'identity'


def test_cover_check_order(ctx, wire, monkeypatch):
    calls = wire(Reply(PNG))
    dns = AsyncMock(return_value=[(0, 0, 0, '', (ip, 443)) for ip in ('8.8.8.8', '10.0.0.1')])

    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        with pytest.raises(public_web.FetchError, match='凭据'):
            await ctx.fetch_public_cover('https://me@cdn.example.org/')
        with pytest.raises(public_web.FetchError, match='策略'):
            await ctx.fetch_public_cover('https://cdn.example.org/', allow_url=lambda _: False)
        dns.assert_not_awaited()
        with pytest.raises(public_web.FetchError, match='公网'):
            await ctx.fetch_public_cover('https://cdn.example.org/')
        assert dns.await_count == 1 and calls == []

    asyncio.run(run())


@pytest.mark.parametrize(('reply', 'reason', 'read'), [
    (Reply(status=302, location='https://9.9.9.9/'), '重定向', False),
    (Reply(status=404), '404', False),
    (Reply(**{'content-length': '4194305'}), '过大', False),
    (Reply(b'x' * public_cover.MAX_BYTES, b'y'), '过大', True),
    (Reply(**{'content-encoding': 'gzip'}), '压缩', False),
    (Reply(b'<svg></svg>', **{'content-type': 'image/png'}), '静态图片', True),
    (Reply(b'GIF89a' + bytes(20)), '静态图片', True),
    (Reply(PNG[:33] + bytes(4) + b'acTL' + bytes(4) + PNG[33:]), '静态图片', True),
    (Reply(b'RIFF' + (22).to_bytes(4, 'little') + b'WEBPVP8X\x0a\0\0\0\x02' + bytes(9)),
     '静态图片', True),
])
def test_cover_rejections(ctx, wire, reply, reason, read):
    calls = wire(reply)
    with pytest.raises(public_web.FetchError, match=reason):
        asyncio.run(ctx.fetch_public_cover('https://8.8.8.8/'))
    assert len(calls) == 1 and reply.read is read


@pytest.mark.parametrize('phase', ['dns', 'body'])
def test_cover_total_timeout(ctx, monkeypatch, wire, phase):
    async def stalled_dns(*_):
        await asyncio.Future()

    class StalledReply(Reply):
        async def aiter_stream(self):
            yield JPEG[:20]
            await asyncio.Future()

    calls = wire(StalledReply())
    monkeypatch.setattr(public_cover, 'TIMEOUT_SECONDS', 0.01)
    if phase == 'dns':
        monkeypatch.setattr(public_web, '_resolve', stalled_dns)
    with pytest.raises(public_web.FetchError, match='超时'):
        asyncio.run(ctx.fetch_public_cover('https://8.8.8.8/'))
    assert len(calls) == (0 if phase == 'dns' else 1)
