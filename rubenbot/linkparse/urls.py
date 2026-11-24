"""链接规范化、平台识别与正文里的链接抽取。

链接理解不设站点白名单：任何 HTTP(S) 地址都会尝试读取，平台专用提取器
只是解析手段，不是准入条件。这里只做纯字符串处理，不发网络请求。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class Platform:
    """一个平台的识别规则；``hosts`` 精确匹配域名或其子域名。"""

    key: str
    name: str
    hosts: tuple[str, ...]
    short_hosts: tuple[str, ...] = ()


PLATFORMS: tuple[Platform, ...] = (
    Platform("bilibili", "哔哩哔哩", ("bilibili.com",), ("b23.tv",)),
    Platform("weibo", "微博", ("weibo.com", "weibo.cn")),
    Platform("x", "X", ("x.com", "twitter.com"), ("t.co",)),
    Platform("youtube", "YouTube", ("youtube.com",), ("youtu.be",)),
    Platform("douyin", "抖音", ("douyin.com", "iesdouyin.com"), ("v.douyin.com",)),
    Platform("xiaohongshu", "小红书", ("xiaohongshu.com",), ("xhslink.com",)),
    Platform("zhihu", "知乎", ("zhihu.com",)),
    Platform("wechat", "微信公众号", ("mp.weixin.qq.com",)),
    Platform("kuaishou", "快手", ("kuaishou.com",)),
    Platform("acfun", "A 站", ("acfun.cn",)),
    Platform("nga", "NGA", ("nga.cn", "bbs.nga.cn")),
    Platform("netease_music", "网易云音乐", ("music.163.com",)),
)
GENERIC = Platform("generic", "网页", ())

# 常见的短链服务：先请求一次，用最终地址重新识别平台并解析。
SHORT_HOSTS: frozenset[str] = frozenset({
    "b23.tv", "t.co", "xhslink.com", "v.douyin.com", "dwz.cn", "url.cn",
    "bit.ly", "tinyurl.com", "t.cn", "s.weibo.com",
})

# 跟踪参数：不影响页面内容，去掉后便于缓存复用。
_TRACKING = frozenset({
    "fbclid", "gclid", "yclid", "igshid", "spm", "spm_id_from", "vd_source",
    "share_source", "share_medium", "share_plat", "share_session_id", "share_tag",
    "from_source", "from_spmid", "buvid", "is_story_h5", "share_from",
    "wxfid", "scene", "clicktime", "enterid", "sessionid",
})
_TRACKING_PREFIX = ("utm_",)

_URL = re.compile(r'''https?://[^\s<>"'`，。！？；、【】「」《》]+''', re.I)


def _is_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def platform_for(url: str) -> Platform:
    """按域名识别平台；认不出时返回 :data:`GENERIC`（照样会尝试读取）。"""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return GENERIC
    for platform in PLATFORMS:
        if any(_is_domain(host, domain) for domain in platform.hosts + platform.short_hosts):
            return platform
    return GENERIC


def is_short(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in SHORT_HOSTS or any(host.endswith("." + item) for item in SHORT_HOSTS)


def normalize(url: str) -> str:
    """去片段、去跟踪参数、统一主机大小写；不是 HTTP(S) 时原样返回。

    只做不改变页面内容的整理：保留路径、查询顺序与其余参数，短链不在这里
    展开（展开需要网络，由抓取层跟随跳转完成）。端口不是数字或越界时
    ``urlsplit`` 会在取 ``port`` 时抛 ``ValueError``，这种链接原样保留，
    交给抓取层按「链接格式无效」处理。
    """
    value = (url or "").strip()
    try:
        parts = urlsplit(value)
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return value
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        return value
    netloc = host
    if ":" in host:  # IPv6
        netloc = f"[{host}]"
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _TRACKING and not key.lower().startswith(_TRACKING_PREFIX)
    ]
    return urlunsplit((scheme, netloc, parts.path, urlencode(query, doseq=True), ""))


def extract(text: str, *, limit: int = 3) -> list[str]:
    """抽取正文里的 HTTP(S) 链接：去聊天标点、片段和尾括号，按出现顺序去重。"""
    urls: list[str] = []
    for match in _URL.finditer(text or ""):
        if match.end() - match.start() > 4096:
            continue
        url = match[0].rstrip(".,;:!").partition("#")[0]
        for left, right in (("(", ")"), ("[", "]"), ("（", "）")):
            extra = min(url.count(right) - url.count(left), len(url) - len(url.rstrip(right)))
            if extra > 0:
                url = url[:-extra]
        url = normalize(url)
        if url not in urls:
            urls.append(url)
        if len(urls) >= max(1, limit):
            break
    return urls


def strip_urls(text: str) -> str:
    """把链接从正文里抹掉：URL 自带的词不是点名或提问。"""
    return _URL.sub(" ", text or "")
