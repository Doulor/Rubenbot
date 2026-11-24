"""平台专用抓取提示：同一页面用哪个地址、带什么请求头去读。

这不是准入条件，只是让主链路更容易读到结构化数据：B 站桌面视频页在
不少出口上直接回 412（风控），移动站仍正常返回 ``__INITIAL_STATE__``。
提示只改「怎么请求」，不改任何安全校验——改写后的地址照样逐跳做
DNS / SSRF 检查与大小、超时限额。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from .urls import platform_for

# 移动端 UA：B 站桌面页与移动页的风控口径不同，移动页对无 Cookie 请求更宽松。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_MOBILE_HEADERS = {"User-Agent": MOBILE_UA}


def _bilibili(url: str) -> tuple[str, dict[str, str]]:
    """B 站视频页改走移动站；其它 B 站页面原样请求。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not parts.path.startswith("/video/"):
        return url, {}
    if host == "www.bilibili.com":
        netloc = "m.bilibili.com" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), dict(_MOBILE_HEADERS)
    if host == "m.bilibili.com":
        return url, dict(_MOBILE_HEADERS)
    return url, {}


def prepare(url: str) -> tuple[str, dict[str, str]]:
    """返回这一跳实际请求的地址与额外请求头；没有专用提示时原样返回。"""
    if platform_for(url).key == "bilibili":
        return _bilibili(url)
    return url, {}
