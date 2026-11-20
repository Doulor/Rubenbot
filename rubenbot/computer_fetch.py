"""电脑工具的只读 HTTP(S) 抓取；访客的 DNS 检查与实际连接使用同一地址。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser

import httpx

from .http_body import read_compressed_body

_MAX_REDIRECTS = 5
_MAX_URL = 8192
_IPV6_UNICAST = ipaddress.IPv6Network("2000::/3")
_TRUNCATED = "…[已截断]"


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    # 只放行原生全球单播；不经映射、地址转换或隧道间接访问 IPv4。
    return isinstance(ip, ipaddress.IPv4Address) or (
        ip in _IPV6_UNICAST and ip.sixtofour is None and ip.teredo is None
    )


async def _pinned_url(url: httpx.URL) -> httpx.URL:
    host = url.raw_host.decode("ascii")
    if "%" in host:
        raise ValueError("访客不能使用带区域标识的地址")
    answers = await asyncio.get_running_loop().getaddrinfo(
        host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM,
    )
    if not answers:
        raise ValueError("域名没有可用地址")
    addresses = [ipaddress.ip_address(addr[0]) for _, _, _, _, addr in answers]
    if not all(_public(ip) for ip in addresses):
        raise ValueError("访客仅可访问公网地址")
    return url.copy_with(host=str(addresses[0]))


class _BodyText(HTMLParser):
    """保留正文文字和段落边界，不执行页面中的脚本。"""

    hidden = {"head", "script", "style", "template", "noscript", "title"}
    blocks = {"p", "br", "div", "li", "tr", "section", "article", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skipped: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skipped.append(tag)
        elif not self.skipped and tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
        elif tag in self.blocks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


async def _body(response: httpx.Response, output_limit: int) -> str:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    cap = min(256 * 1024, max(8192, output_limit * 4))
    clipped = False
    if encoding == "identity":
        data = bytearray()
        async for chunk in response.aiter_raw():
            left = cap - len(data)
            data.extend(chunk[:left])
            if len(chunk) > left:
                clipped = True
                break
    else:
        data = await read_compressed_body(response.aiter_raw(), encoding, cap)
    charset = (response.charset_encoding or "utf-8").lower()
    if charset not in {"utf-8", "us-ascii", "iso-8859-1", "windows-1252", "gbk",
                       "gb2312", "gb18030", "big5", "shift_jis"}:
        charset = "utf-8"
    text = data.decode(charset, errors="replace")
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() in {
        "text/html", "application/xhtml+xml",
    }:
        parser = _BodyText()
        try:
            parser.feed(text)
            parser.close()
        except AssertionError:
            # HTMLParser 遇到非法标记声明会抛 AssertionError，而非解析异常。
            raise ValueError("网页 HTML 格式无效") from None
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    encoded = text.encode("utf-8")
    if not clipped and len(encoded) <= output_limit:
        return text
    marker = _TRUNCATED.encode()[:output_limit].decode(errors="ignore")
    return encoded[:output_limit - len(marker.encode())].decode(errors="ignore") + marker


async def fetch_url(url: str, *, sandbox: bool, output_limit: int, timeout: float) -> str:
    """读取网页正文；DNS、重定向、正文读取共用一次超时预算。"""
    if output_limit <= 0 or timeout <= 0:
        raise ValueError("抓取限额必须为正数")
    if len(url) > _MAX_URL:
        raise ValueError("网页地址过长")
    try:
        async with asyncio.timeout(timeout):
            target = httpx.URL(url)
            # 直接使用无代理传输：不读环境代理、不带 cookie、不自动重定向。
            # 不复用连接，避免两个域名解析到同一 IP 时复用对方的 TLS 会话。
            async with httpx.AsyncHTTPTransport(
                trust_env=False, verify=True, limits=httpx.Limits(max_keepalive_connections=0),
            ) as transport:
                for hop in range(_MAX_REDIRECTS + 1):
                    if (target.scheme not in {"http", "https"} or not target.raw_host
                            or target.userinfo or len(str(target)) > _MAX_URL
                            or (target.port is not None and not 1 <= target.port <= 65535)):
                        raise ValueError("需要不含登录凭据的 HTTP(S) 网页地址")
                    destination = await _pinned_url(target) if sandbox else target
                    request = httpx.Request(
                        "GET", destination,
                        headers={b"Host": target.netloc, b"Accept-Encoding": b"gzip, deflate"},
                        extensions={"sni_hostname": target.raw_host.decode("ascii")},
                    )
                    response = await transport.handle_async_request(request)
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or len(location) > _MAX_URL or hop == _MAX_REDIRECTS:
                                raise ValueError("重定向无效或次数过多")
                            target = target.join(location)
                            continue
                        if not 200 <= response.status_code < 300:
                            raise ValueError(f"网页返回 HTTP {response.status_code}")
                        return await _body(response, output_limit)
                    finally:
                        await response.aclose()
    except TimeoutError:
        raise ValueError("网页抓取超时") from None
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError):
        # 不把含查询参数、重定向凭据或网络环境细节的异常交给模型。
        raise ValueError("网页地址无效或连接失败") from None
