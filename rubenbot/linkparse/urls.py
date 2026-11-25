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

# X（Twitter）的推文地址；``i/web/status`` 是网页端的中转形态，与 ``<user>/status`` 同义。
# fxtwitter 的公开 JSON 只认这个形态，t.co 展开后的落地地址同样适用。
_X_STATUS = re.compile(r"/([A-Za-z0-9_]+|i/web)/status/(\d+)(?:/(?:photo|video)/\d+)?/?$", re.ASCII)
_X_JSON = "https://api.fxtwitter.com/"


def x_status(url: str) -> tuple[str, str] | None:
    """``https://x.com/<user>/status/<id>`` → ``(用户名段, 状态 ID)``。

    只做纯字符串判断，不联网；t.co 之类短链要先由抓取层展开再调用。
    不是 X 的推文地址时返回 ``None``。
    """
    if platform_for(url).key != "x":
        return None
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = _X_STATUS.fullmatch(path)
    return (match[1], match[2]) if match else None


def x_json_api(url: str) -> str:
    """该推文的 fxtwitter 公开 JSON 地址；不是推文地址时返回空串。

    x.com 在部分出口上完全连不上，fxtwitter 的 JSON 能给出作者、正文、时间与
    媒体；这是读取手段，不是对外的链接改写（对外仍报原地址）。

    ``i/web`` 形态没有用户名，fxtwitter 的接口按 ``<用户名>/status/<id>`` 取帖，
    这里如实返回空串，让调用方改走 oEmbed 或直连网页，不拿别的名字去猜。
    """
    status = x_status(url)
    if not status or status[0] == "i/web":
        return ""
    return f"{_X_JSON}{status[0]}/status/{status[1]}"


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
