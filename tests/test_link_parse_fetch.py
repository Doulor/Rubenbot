"""假 DNS + 假字节流，覆盖实际发出的 HTTP 请求、隧道目标及 TLS 主机名。"""

import asyncio
import socket
import ssl
import zlib

import httpcore
import pytest

from plugins.link_parse import fetch as mod


PUBLIC = '1.1.1.1'


def reply(status=200, headers=(), body=b''):
    lines = [f'HTTP/1.1 {status} Test', *headers]
    return ('\r\n'.join(lines) + '\r\n\r\n').encode() + body


class Stream(httpcore.AsyncMockStream):
    def __init__(self, packets):
        super().__init__(packets)
        self.writes = bytearray()
        self.tls = []
        self.closed = False

    async def write(self, buffer, timeout=None):
        self.writes.extend(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
        self.tls.append(server_hostname)
        return self

    async def aclose(self):
        self.closed = True
        await super().aclose()


class Network:
    def __init__(self):
        self.answers = {}
        self.lookups = []
        self.connections = []
        self.pending = []

    def serve(self, *packets):
        stream = Stream(list(packets))
        self.pending.append(stream)
        return stream

    async def resolve(self, host, port, **kwargs):
        self.lookups.append((host, port))
        return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM,
                 6, '', (ip, port)) for ip in self.answers[host].pop(0)]

    async def connect_tcp(self, host, port):
        self.connections.append((host, port))
        return self.pending.pop(0)


@pytest.fixture
def net(monkeypatch):
    network = Network()
    for key in ['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy']:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)
    async def resolve(_loop, *args, **kwargs):
        return await network.resolve(*args, **kwargs)
    monkeypatch.setattr(asyncio.BaseEventLoop, 'getaddrinfo', resolve)
    monkeypatch.setattr(httpcore, 'AnyIOBackend', lambda: network)
    return network


def run(url, size=1024, timeout=2, **kwargs):
    return asyncio.run(mod.fetch(url, size, timeout, **kwargs))


@pytest.mark.parametrize('url', [
    'file:///etc/test', 'ftp://example.org/file', 'https://:pw@example.org/',
    'http://u@example.org', 'https://@example.org', 'https://localhost.',
    'http://service.local', 'http://127.0.0.2', 'http://169.254.1.1',
    'http://100.64.0.1', 'http://10.0.0.1', 'http://192.168.1.1', 'http://224.0.0.1',
    'http://[::1]', 'http://[::ffff:127.0.0.1]', 'http://[fc00::1]',
    'http://[fe80::1%25eth0]', 'http://[64:ff9b::7f00:1]', 'http://[2002:7f00:1::]',
])
def test_reject_before_connection(net, url):
    with pytest.raises(mod.FetchError):
        run(url)
    assert not net.connections and not net.lookups


@pytest.mark.parametrize('answers', [[], [PUBLIC, '10.0.0.2'], ['169.254.1.2', PUBLIC]])
def test_reject_entire_dns_answer(net, answers):
    net.answers['test.example'] = [answers]
    with pytest.raises(mod.FetchError):
        run('https://test.example/')
    assert not net.connections


def test_pinned_connection_original_host_tls_and_charset(net):
    net.answers['site.example'] = [[PUBLIC], ['127.0.0.1']]
    s = net.serve(reply(headers=['Content-Type: text/html; charset=gbk', 'Content-Length: 2'],
                        body='页'.encode('gbk')))
    result = run('https://site.example:8443/page?a=1#s', size=2)
    assert result == mod.FetchResult('https://site.example:8443/page?a=1', 'text/html', '页')
    assert net.lookups == [('site.example', 8443)]
    assert net.connections == [(PUBLIC, 8443)]
    assert b'GET /page?a=1 HTTP/1.1\r\n' in s.writes and b'Host: site.example:8443\r\n' in s.writes
    assert s.tls == ['site.example'] and s.closed


def test_dns_rechecked_after_relative_redirect(net):
    net.answers['site.example'] = [[PUBLIC], ['127.0.0.1']]
    s = net.serve(reply(302, ['Location: /next', 'Content-Length: 1000000']))
    with pytest.raises(mod.FetchError, match='公网'):
        run('http://site.example/')
    assert len(net.lookups) == 2 and net.connections == [(PUBLIC, 80)] and s.closed


@pytest.mark.parametrize('location', ['http://169.254.1.1/', '//u:pw@site.example', 'file:///etc/test'])
def test_check_redirect_target_before_connecting(net, location):
    net.serve(reply(307, [f'Location: {location}']))
    with pytest.raises(mod.FetchError):
        run('https://' + PUBLIC)
    assert len(net.connections) == 1


@pytest.mark.parametrize('limit', [0, 2])
def test_redirect_limit_and_no_follow(net, limit):
    for _ in range(limit + 1):
        net.serve(reply(301, ['Location: /loop']))
    with pytest.raises(mod.FetchError, match='重定向'):
        run('http://' + PUBLIC, max_redirects=limit)
    assert len(net.connections) == limit + 1


def test_site_filter_blocks_redirect_before_dns(net):
    net.serve(reply(302, ['Location: https://blocked.example/page']))
    seen = []
    def allow(url):
        seen.append(url)
        return url == 'https://' + PUBLIC
    with pytest.raises(mod.FetchError, match='站点策略'):
        run('https://' + PUBLIC, allow_url=allow)
    assert seen == ['https://' + PUBLIC, 'https://blocked.example/page']
    assert not net.lookups and net.connections == [(PUBLIC, 443)]


def test_proxy_tunnel_and_redirect_no_proxy(net, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'https://operator:pw@proxy.example:3128')
    monkeypatch.setenv('NO_PROXY', 'second.example')
    net.answers.update({'first.example': [['2606:4700:4700::1111']], 'second.example': [[PUBLIC]]})
    first = net.serve(reply(), reply(303, ['Location: https://second.example/page', 'Set-Cookie: token=1']))
    second = net.serve(reply(308, ['Location: ../final', 'Set-Cookie: token=2; Path=/']))
    net.answers['second.example'].append([PUBLIC])
    last = net.serve(reply(body=b'ok'))
    result = run('https://first.example/s')
    assert result.final_url == 'https://second.example/final' and result.text == 'ok'
    assert net.connections == [('proxy.example', 3128), (PUBLIC, 443), (PUBLIC, 443)]
    connect, get = first.writes.split(b'GET ', 1)
    assert b'CONNECT [2606:4700:4700::1111]:443 HTTP/1.1' in connect
    assert b'Proxy-Authorization: Basic ' in connect and b'first.example' not in connect
    assert b'Host: first.example' in get and b'Authorization:' not in get
    assert first.tls == ['proxy.example', 'first.example']
    assert all(b'Cookie:' not in s.writes and b'Authorization:' not in s.writes for s in (second, last))
    assert all(s.closed for s in (first, second, last))


def test_proxy_refusal_never_falls_back(net, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.example:8080')
    s = net.serve(reply(403))
    with pytest.raises(mod.FetchError, match='代理'):
        run('http://' + PUBLIC)
    assert net.connections == [('proxy.example', 8080)] and b'GET ' not in s.writes and s.closed


@pytest.mark.parametrize(('packets', 'reason'), [
    ([reply(headers=['Content-Length: 99999'])], '正文过大'),
    ([reply(headers=['Transfer-Encoding: chunked']), b'2\r\nab\r\n', b'2\r\ncd\r\n'], '正文过大'),
    ([reply(headers=['Content-Encoding: gzip'])], '压缩'),
    ([reply(403)], '403'), ([reply(302)], '重定向'),
])
def test_response_limits_and_cleanup(net, packets, reason):
    s = net.serve(*packets)
    with pytest.raises(mod.FetchError, match=reason):
        run('http://' + PUBLIC, size=3)
    assert s.closed


@pytest.mark.parametrize(('encoding', 'wbits'), [('gzip', 31), ('deflate', 15), ('deflate', -15)])
def test_decodes_compressed_response_at_limit(net, encoding, wbits):
    body = '网页'.encode('gbk')
    packed = zlib.compress(body, wbits=wbits)
    headers = [f'Content-Encoding: {encoding}', f'Content-Length: {len(packed)}',
               'Content-Type: text/html; charset=gbk']
    wire = net.serve(reply(headers=headers), packed[:1], packed[1:])
    assert run('http://' + PUBLIC, size=len(body)).text == '网页'
    assert b'accept-encoding: gzip, deflate\r\n' in wire.writes.lower() and wire.closed


@pytest.mark.parametrize(('encoding', 'wbits'), [('gzip', 31), ('deflate', 15), ('deflate', -15)])
def test_compressed_body_stops_at_decoded_limit(net, check_inflate_budget, encoding, wbits):
    packed = zlib.compress(b'x' * 65536, wbits=wbits)
    assert len(packed) < 1024
    sizes = check_inflate_budget(1024)
    wire = net.serve(reply(headers=[f'Content-Encoding: {encoding}']), packed[:32], packed[32:])
    with pytest.raises(mod.FetchError, match='正文过大'):
        run('http://' + PUBLIC)
    assert sum(sizes) == 1025 and wire.closed


@pytest.mark.parametrize('split', [True, False])
def test_gzip_members_share_the_same_limit(net, split):
    first = zlib.compress(b'abc', wbits=31)
    second = zlib.compress(b'def', wbits=31)
    packets = (first, second) if split else (first + second,)
    net.serve(reply(headers=['Content-Encoding: gzip']), *packets)
    assert run('http://' + PUBLIC, size=6).text == 'abcdef'
    net.serve(reply(headers=['Content-Encoding: gzip']), *packets)
    with pytest.raises(mod.FetchError, match='正文过大'):
        run('http://' + PUBLIC, size=5)


def test_rejects_unknown_encoding(net):
    wire = net.serve(reply(headers=['Content-Encoding: br'], body=b'ok'))
    with pytest.raises(mod.FetchError, match='编码'):
        run('http://' + PUBLIC)
    assert wire.closed


def test_untrusted_charset_is_not_a_python_transform(net):
    net.serve(reply(headers=['Content-Type: text/plain; charset=zlib_codec'], body=b'ok'))
    assert run('http://' + PUBLIC).text == 'ok'


@pytest.mark.parametrize('block_at', ['dns', 'body'])
def test_total_deadline_includes_dns_and_body(net, monkeypatch, block_at):
    async def stall(*args, **kwargs):
        await asyncio.Event().wait()
    net.answers['site.example'] = [[PUBLIC]]
    s = net.serve(reply(headers=['Content-Length: 3']))
    if block_at == 'dns':
        monkeypatch.setattr(net, 'resolve', stall)
    else:
        read = s.read
        async def slow_body(*args, **kwargs):
            if s._buffer:
                return await read(*args, **kwargs)
            return await stall()
        monkeypatch.setattr(s, 'read', slow_body)
    with pytest.raises(mod.FetchError, match='超时'):
        run('http://site.example/', timeout=0.03)
    assert not net.connections if block_at == 'dns' else s.closed
