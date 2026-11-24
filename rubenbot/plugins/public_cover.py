"""宿主读取静态封面：复用网页的公网连接守卫，只给插件图片字节。"""

import asyncio
from collections.abc import Callable

import httpcore
import httpx

from ..linkparse import fetch as public_web
from ..linkparse.fetch import FetchError
from ..vision_image import image_size

MAX_BYTES = 4 << 20
TIMEOUT_SECONDS = 6.0


def _headers(host: str) -> dict[str, str]:
    headers = {"Accept": "image/jpeg,image/png,image/webp", "Accept-Encoding": "identity"}
    host = host.rstrip(".")
    for domain, referer in (("pximg.net", "https://www.pixiv.net/"),
                            ("hdslb.com", "https://www.bilibili.com/")):
        if host == domain or host.endswith("." + domain):
            headers["Referer"] = referer
    return headers


def _static_cover(data: bytes) -> bool:
    # 只辨认容器、文件头与动画标志，不解码远端像素，也不按 URL 或 MIME 猜格式。
    size = image_size(data)
    if not size or min(size) <= 0:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return data.endswith(b"\xff\xd9")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            return False
        return data[12:16] != b"VP8X" or not data[20] & 0x02
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        # APNG 与 PNG 文件头相同，合法 APNG 的 acTL 块一定在首个 IDAT 之前。
        pos = 8
        while pos + 12 <= len(data):
            chunk = data[pos + 4:pos + 8]
            pos += 12 + int.from_bytes(data[pos:pos + 4], "big")
            if pos > len(data) or chunk == b"acTL":
                return False
            if chunk == b"IDAT":
                return True
    return False


async def fetch(url: str, *, allow_url: Callable[[str], bool] | None = None) -> bytes:
    """单跳 GET；DNS、建连与流式读响应共用一个时限，错误口径同公网网页。"""
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            target = public_web._url(url)
            if allow_url is not None and not allow_url(str(target)):
                raise FetchError("站点策略拒绝封面链接")
            host = target.raw_host.decode("ascii")
            port = target.port or (443 if target.scheme == "https" else 80)
            ip = await public_web._resolve(host, port)
            async with public_web._open(target, ip, port, _headers(host)) as response:
                if 300 <= response.status < 400:
                    raise FetchError("封面不跟随重定向")
                if response.status != 200:
                    raise FetchError(f"封面站点返回 HTTP {response.status}")
                headers = httpx.Headers(response.headers)
                if headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise FetchError("不接受压缩传输的封面")
                if int(headers.get("content-length", "0")) > MAX_BYTES:
                    raise FetchError("封面过大")
                body = bytearray()
                async for chunk in response.aiter_stream():
                    if len(chunk) > MAX_BYTES - len(body):
                        raise FetchError("封面过大")
                    body.extend(chunk)
            data = bytes(body)
            if not _static_cover(data):
                raise FetchError("封面不是支持的静态图片")
            return data
    except FetchError:
        raise
    except (TimeoutError, httpcore.TimeoutException) as exc:
        raise FetchError("封面抓取超时") from exc
    except (ValueError, OSError, httpx.InvalidURL, httpcore.NetworkError, httpcore.ProtocolError) as exc:
        raise FetchError("封面链接、连接或响应无效") from exc
