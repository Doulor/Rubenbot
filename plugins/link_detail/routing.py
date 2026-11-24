"""按作品路由认领链接；其余 URL 和文字仍交给通用层。"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from rubenbot.linkparse import urls

from . import primary, public

NAMES = {
    "bilibili": "B站", "douyin": "抖音", "xiaohongshu": "小红书", "xiaoheihe": "小黑盒",
    "acfun": "A站", "instagram": "Instagram", "iwara": "iwara", "kuaishou": "快手",
    "netease_music": "网易云音乐", "nga": "NGA", "shipinhao": "微信视频号", "tiktok": "TikTok",
    "x": "X", "weibo": "微博", "youtube": "YouTube", "zhihu": "知乎", "pixiv": "Pixiv",
}


def allowed(key: str, url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme not in {"http", "https"} or parts.port not in {None, 80, 443}
                or "@" in parts.netloc or "\\" in url or any(ord(c) < 32 for c in url)):
            return False
    except ValueError:
        return False
    hosts = extractor(key).HOSTS[key]
    return any(host == d or host.endswith("." + d) for d in hosts)


def extractor(key):
    return primary if key in primary.HOSTS else public


@dataclass(frozen=True)
class Link:
    start: int
    end: int
    url: str
    normalized: str


def links(text):
    # 共用 core 的地址边界；保留原始字符位置，不能用规范化 URL 直接替换原文。
    for match in urls._URL.finditer(text):
        raw = match[0].rstrip(".,;:!")
        for left, right in (("(", ")"), ("[", "]"), ("（", "）")):
            excess = min(raw.count(right) - raw.count(left), len(raw) - len(raw.rstrip(right)))
            if excess > 0:
                raw = raw[:-excess]
        if len(raw) > 4096:
            continue
        try:
            parts = urlsplit(raw)
            if (not parts.hostname or '@' in parts.netloc or parts.port not in (None, 80, 443)
                    or '\\' in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
                continue
            # 网易云网页版把作品路由放在片段中；请求要转成公开的普通路径。
            target = (raw.partition("#")[0].rstrip("/") + parts.fragment
                      if parts.hostname == "music.163.com" and parts.path in ("", "/")
                      and parts.fragment.startswith("/") else raw)
            normalized = urls.normalize(target)
        except ValueError:
            continue
        yield Link(match.start(), match.start() + len(raw), raw, normalized)


def identify(url, enabled):
    return next((key for key in enabled if allowed(key, url) and extractor(key).matches(key, url)), None)
