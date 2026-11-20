"""离线测试：保留真实 HTTP 客户端，把 DNS、TCP/TLS 的网络边界换成内存记录器。"""

import asyncio
import socket
import ssl
import zlib

import httpcore
import pytest

from rubenbot.computer_fetch import fetch_url


class Wire(httpcore.AsyncNetworkStream):
    def __init__(self, reply):
        self.reply = iter(reply)
        self.sent = bytearray()
        self.tls = None
        self.closed = False

    async def read(self, max_bytes, timeout=None):
        return next(self.reply, b"")

    async def write(self, buffer, timeout=None):
        self.sent.extend(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls = (server_hostname, ssl_context.check_hostname, ssl_context.verify_mode)
        return self

    def get_extra_info(self, info):
        return None


class Network:
    def __init__(self):
        self.dns = {}
        self.queries = []
        self.connections = []
        self.replies = []

    async def resolve(self, host, port, **kwargs):
        self.queries.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, "", (ip, port)) for ip in self.dns[host].pop(0)]

    async def connect(self, host, port, **kwargs):
        wire = Wire(self.replies.pop(0))
        self.connections.append((host, port, wire))
        return wire


@pytest.fixture
def network(monkeypatch):
    net = Network()

    async def resolve(loop, host, port, **kwargs):
        return await net.resolve(host, port, **kwargs)

    async def connect(backend, host, port, **kwargs):
        return await net.connect(host, port, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8888")
    monkeypatch.setenv("NO_PROXY", "")
    return net


def fetch(url="https://page.test/", sandbox=True, output_limit=256):
    return asyncio.run(fetch_url(url, sandbox=sandbox, output_limit=output_limit, timeout=2))


def test_pins_connection_preserves_tls_and_extracts_body(network):
    network.dns["page.test"] = [["2606:4700:4700::1111", "1.1.1.1"], ["127.0.0.1"]]
    network.replies = [[b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
                        "<head><title>标题</title></head><p>甲&amp;乙</p><script>隐藏</script><p>丙</p>".encode()]]
    assert fetch("https://page.test:8443/a?q=1#frag") == "甲&乙\n丙"
    assert network.queries == ["page.test"]
    (ip, port, wire), = network.connections
    assert (ip, port) == ("2606:4700:4700::1111", 8443)
    assert wire.sent.startswith(b"GET /a?q=1 HTTP/1.1\r\n")
    assert b"host: page.test:8443\r\n" in wire.sent.lower()
    assert wire.tls == ("page.test", True, ssl.CERT_REQUIRED)
    assert wire.closed


@pytest.mark.parametrize("denied", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "::1", "fd00::1", "fe80::1", "fec0::1", "ff02::1",
    "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::1"])
def test_one_nonpublic_answer_blocks_whole_request(network, denied):
    network.dns["page.test"] = [["1.1.1.1", denied]]
    with pytest.raises(ValueError, match="公网"):
        fetch()
    assert not network.connections


def test_relative_redirect_resolves_again_before_connecting(network):
    network.dns["page.test"] = [["1.1.1.1"], ["10.0.0.1"]]
    network.replies = [[b"HTTP/1.1 302 Found\r\nLocation: ../other\r\n\r\n"]]
    with pytest.raises(ValueError, match="公网"):
        fetch()
    assert network.queries == ["page.test"] * 2
    assert len(network.connections) == 1
    assert network.connections[0][2].closed


def test_redirect_changes_tls_identity_even_on_same_ip(network):
    network.dns = {"page.test": [["1.1.1.1"]], "other.test": [["1.1.1.1"]]}
    network.replies = [[b"HTTP/1.1 301 Moved\r\nLocation: //other.test/end\r\nContent-Length: 0\r\n\r\n"],
                       [b"HTTP/1.1 200 OK\r\n\r\nfinal"]]
    assert fetch() == "final"
    assert [wire.tls[0] for _, _, wire in network.connections] == ["page.test", "other.test"]


@pytest.mark.parametrize("location", ["file:///etc/passwd", "https://u:p@other.test/x"])
def test_redirect_cannot_switch_to_disallowed_url(network, location):
    network.dns["page.test"] = [["1.1.1.1"]]
    network.replies = [[f"HTTP/1.1 307 Redirect\r\nLocation: {location}\r\n\r\n".encode()]]
    with pytest.raises(ValueError, match="HTTP"):
        fetch()
    assert len(network.connections) == 1


def test_host_allows_loopback_and_skips_proxy(network):
    network.replies = [[b"HTTP/1.1 200 OK\r\n\r\nhost page"]]
    assert fetch("http://127.0.0.1:9090", sandbox=False) == "host page"
    assert network.connections[0][:2] == ("127.0.0.1", 9090)


def test_truncates_utf8_and_stops_reading(network):
    def reply():
        yield b"HTTP/1.1 200 OK\r\n\r\n"
        yield "正文".encode() * 2048
        pytest.fail("达到字节上限后仍在读取响应")

    network.replies = [reply()]
    result = fetch(sandbox=False, output_limit=32)
    assert result.startswith("正文") and result.endswith("[已截断]")
    assert len(result.encode()) <= 32 and "�" not in result
    assert network.connections[0][2].closed


@pytest.mark.parametrize(('encoding', 'wbits'), [('gzip', 31), ('deflate', 15), ('deflate', -15)])
def test_decodes_compressed_html(network, encoding, wbits):
    packed = zlib.compress('<p>正文</p>'.encode('gbk'), wbits=wbits)
    network.dns['page.test'] = [['1.1.1.1']]
    network.replies = [[f'HTTP/1.1 200 OK\r\nContent-Encoding: {encoding}\r\n'
                        'Content-Type: text/html; charset=gbk\r\n\r\n'.encode(), packed[:1], packed[1:]]]
    assert fetch() == '正文'
    wire = network.connections[0][2]
    assert b'accept-encoding: gzip, deflate\r\n' in wire.sent.lower() and wire.closed


@pytest.mark.parametrize(('encoding', 'wbits'), [('gzip', 31), ('deflate', 15), ('deflate', -15)])
def test_compressed_body_stops_at_decoded_limit(network, check_inflate_budget, encoding, wbits):
    packed = zlib.compress(b'x' * 65536, wbits=wbits)
    assert len(packed) < 256

    def reply():
        yield f'HTTP/1.1 200 OK\r\nContent-Encoding: {encoding}\r\n\r\n'.encode()
        yield packed[:32]
        yield packed[32:]
        pytest.fail('超过正文读取上限后仍在读取响应')

    sizes = check_inflate_budget(8192)
    network.replies = [reply()]
    with pytest.raises(ValueError, match='正文过大'):
        fetch(sandbox=False, output_limit=256)
    assert sum(sizes) == 8193 and network.connections[0][2].closed


def test_compressed_html_at_body_limit_still_clips_display(network):
    body = b'<p>' + b'x' * (8192 - 7) + b'</p>'
    network.replies = [[b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Type: text/html\r\n\r\n',
                        zlib.compress(body, wbits=31)]]
    text = fetch(sandbox=False, output_limit=32)
    assert text.startswith('x') and text.endswith('[已截断]') and len(text.encode()) == 32


@pytest.mark.parametrize('damage', ['truncated', 'corrupt'])
def test_bad_compressed_stream_rejected(network, damage):
    packed = zlib.compress(b'partial page', wbits=31)
    packed = packed[:-1] if damage == 'truncated' else packed[:-1] + b'\xff'
    network.replies = [[b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n', packed]]
    with pytest.raises(ValueError, match='压缩正文'):
        fetch(sandbox=False)
    assert network.connections[0][2].closed


def test_rejects_unknown_encoding_without_reading_body(network):
    def reply():
        yield b"HTTP/1.1 200 OK\r\nContent-Encoding: br\r\n\r\n"
        pytest.fail("不应读取未知编码正文")

    network.replies = [reply()]
    with pytest.raises(ValueError, match="压缩"):
        fetch(sandbox=False)
    assert network.connections[0][2].closed


def test_redirect_loop_is_capped(network):
    from rubenbot.computer_fetch import _MAX_REDIRECTS

    network.replies = [[b"HTTP/1.1 302 Found\r\nLocation: /again\r\n\r\n"]] * (_MAX_REDIRECTS + 1)
    with pytest.raises(ValueError, match="重定向"):
        fetch(sandbox=False)
    assert all(wire.closed for _, _, wire in network.connections)


def test_body_cannot_stall_after_headers(network, monkeypatch):
    read = Wire.read

    async def delayed(self, max_bytes, timeout=None):
        if next_read := await read(self, max_bytes, timeout):
            return next_read
        await asyncio.Future()

    monkeypatch.setattr(Wire, "read", delayed)
    network.replies = [[b"HTTP/1.1 200 OK\r\n\r\nbegin"]]
    with pytest.raises(ValueError, match="超时"):
        asyncio.run(fetch_url("http://page.test/", sandbox=False, timeout=.15, output_limit=32))
    assert network.connections[0][2].closed


def test_timeout_includes_dns(network, monkeypatch):
    async def delayed(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(network, "resolve", delayed)
    with pytest.raises(ValueError, match="超时"):
        asyncio.run(fetch_url("http://page.test/", sandbox=True, timeout=.1, output_limit=32))
    assert not network.connections
