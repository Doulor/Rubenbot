"""只抓公网 HTTP(S) 文本；每跳分别决定出口、固定地址，不共享凭据。"""

import asyncio
import base64
import ipaddress
import math
import socket
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx
from httpx._utils import URLPattern, get_environment_proxies


@dataclass(frozen=True)
class FetchResult:
    final_url: str
    content_type: str
    text: str


class FetchError(ValueError):
    """链接不安全、不可达或超过限额；调用方可静默跳过。"""


def _url(value: str, base: httpx.URL | None = None) -> httpx.URL:
    if not value or value != value.strip() or '\\' in value or any(
        ord(c) < 32 or ord(c) == 127 for c in value
    ):
        raise FetchError('链接格式无效')
    if '@' in urlsplit(value).netloc:
        raise FetchError('不接受带凭据的链接')
    url = httpx.URL(value) if base is None else base.join(value)
    host = url.raw_host.lower().rstrip(b'.')
    if (url.scheme not in ('http', 'https') or not host or url.userinfo
            or b'%' in host or url.port == 0 or (url.port or 0) > 65535):
        raise FetchError('只接受无凭据的 HTTP(S) 链接')
    if host == b'localhost' or host.endswith((b'.localhost', b'.local')):
        raise FetchError('不接受本地主机名')
    return url.copy_with(fragment=None)


def _public_ip(value: str) -> str:
    ip = ipaddress.ip_address(value)
    if (not ip.is_global or ip.is_multicast or ip.is_reserved
            or isinstance(ip, ipaddress.IPv6Address) and (
                ip.scope_id or ip.is_site_local or ip.ipv4_mapped or ip.sixtofour or ip.teredo
            )):
        raise FetchError('目标不是公网单播地址')
    return str(ip)


async def _resolve(host: str, port: int) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        # 有任意非公网答案就拒绝整跳，绝不交给连接层再次解析原域名。
        addresses = [_public_ip(row[4][0]) for row in rows]
        if not addresses:
            raise FetchError('域名没有地址')
        return addresses[0]
    return _public_ip(host)


def _proxy_for(url: httpx.URL) -> httpx.Proxy | None:
    # 使用核心 httpx 客户端相同的环境代理与 NO_PROXY 匹配规则，按原域名选路。
    routes = get_environment_proxies()
    for pattern in sorted(URLPattern(key) for key in routes):
        if pattern.matches(url):
            proxy = routes[pattern.pattern]
            return httpx.Proxy(proxy) if proxy else None
    return None


@asynccontextmanager
async def _open(url: httpx.URL, ip: str, port: int):
    proxy = _proxy_for(url)
    if proxy and proxy.url.scheme not in ('http', 'https'):
        raise FetchError('不支持该代理类型，不回退直连')
    host = url.raw_host.decode('ascii')
    backend = httpcore.AnyIOBackend()
    tls = httpx.create_ssl_context() if url.scheme == 'https' or (proxy and proxy.url.scheme == 'https') else None
    if tls:
        tls.set_alpn_protocols(['http/1.1'])
    stream = await backend.connect_tcp(
        proxy.url.raw_host.decode('ascii') if proxy else ip,
        (proxy.url.port or (443 if proxy.url.scheme == 'https' else 80)) if proxy else port,
    )
    try:
        async with AsyncExitStack() as stack:
            if proxy:
                if proxy.url.scheme == 'https':
                    stream = await stream.start_tls(tls, proxy.url.raw_host.decode('ascii'))
                # 显式 CONNECT 到数值地址；不能把原域名交给代理解析，也不能用 IP 验站点证书。
                endpoint = f'[{ip}]:{port}' if ':' in ip else f'{ip}:{port}'
                proxy_url = httpcore.URL(str(proxy.url))
                headers = {'Host': endpoint}
                if proxy.raw_auth:
                    auth = base64.b64encode(b':'.join(proxy.raw_auth)).decode('ascii')
                    headers['Proxy-Authorization'] = 'Basic ' + auth
                tunnel = httpcore.AsyncHTTP11Connection(proxy_url.origin, stream)
                reply = await stack.enter_async_context(tunnel.stream(
                    'CONNECT', proxy_url, headers=headers, extensions={'target': endpoint.encode('ascii')},
                ))
                if not 200 <= reply.status < 300:
                    raise FetchError('代理拒绝连接')
                stream = reply.extensions['network_stream']
            if url.scheme == 'https':
                stream = await stream.start_tls(tls, host)
            target = httpcore.URL(str(url))
            connection = httpcore.AsyncHTTP11Connection(target.origin, stream)
            response = await stack.enter_async_context(connection.stream(
                'GET', target, headers={'Host': url.netloc, 'Accept-Encoding': 'identity'},
            ))
            yield response
    finally:
        await stream.aclose()


async def fetch(
    url: str, max_bytes: int, timeout_seconds: float, *, max_redirects: int = 5,
    allow_url: Callable[[str], bool] | None = None,
) -> FetchResult:
    """限字节、限总耗时；max_redirects=0 禁跳转，allow_url 可在每跳解析前按站点拒绝。"""
    if (not isinstance(max_bytes, int) or max_bytes <= 0 or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0 or not isinstance(max_redirects, int) or max_redirects < 0):
        raise FetchError('抓取限额无效')
    try:
        async with asyncio.timeout(timeout_seconds):
            current = _url(url)
            for hop in range(max_redirects + 1):
                if allow_url is not None and not allow_url(str(current)):
                    raise FetchError('站点策略拒绝目标链接')
                port = current.port or (443 if current.scheme == 'https' else 80)
                ip = await _resolve(current.raw_host.decode('ascii'), port)
                async with _open(current, ip, port) as response:
                    headers = httpx.Headers(response.headers)
                    if response.status in (301, 302, 303, 307, 308):
                        locations = headers.get_list('location')
                        if hop == max_redirects or len(locations) != 1:
                            raise FetchError('重定向超限或缺少唯一目标')
                        current = _url(locations[0], current)
                        continue
                    if not 200 <= response.status < 300:
                        raise FetchError(f'站点返回 HTTP {response.status}')
                    # 禁止透明解压；即使对方忽略 identity 也不能制造解压膨胀。
                    if headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise FetchError('不接受压缩正文')
                    if int(headers.get('content-length', '0')) > max_bytes:
                        raise FetchError('正文过大')
                    body = bytearray()
                    async for chunk in response.aiter_stream():
                        if len(chunk) > max_bytes - len(body):
                            raise FetchError('正文过大')
                        body.extend(chunk)
                    content_type = headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    # 只接受网页字符集，不让 charset 触发 Python 的解压等通用 codec。
                    charset = (httpx.Response(200, headers=headers).charset_encoding or '').lower()
                    if charset not in {'utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'shift_jis',
                                       'windows-1252', 'iso-8859-1', 'utf-16', 'utf-16le', 'utf-16be'}:
                        charset = 'utf-8'
                    text = body.decode(charset, errors='replace')
                    return FetchResult(str(current), content_type, text)
    except FetchError:
        raise
    except (TimeoutError, httpcore.TimeoutException) as exc:
        raise FetchError('链接抓取超时') from exc
    except (ValueError, OSError, httpx.InvalidURL, httpcore.NetworkError, httpcore.ProtocolError) as exc:
        raise FetchError('链接格式、连接或响应无效') from exc
