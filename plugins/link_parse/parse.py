"""解析公开页面；网络、重定向及抓取限额由调用方提供的 fetch 负责。"""

import json
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit


_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form"}
_BLOCK = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "br"}
_STATE = re.compile(r"(?:window\s*\.\s*)?\b__INITIAL_STATE__\s*=\s*")
_VIDEO = re.compile(r"^/video/(?:BV[0-9A-Za-z]+|av[0-9]+)(?:/|$)", re.I)


def _clean(value, limit=800):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


class _Page(HTMLParser):
    """只收集指定区域的可见文字，不执行脚本，不访问页面声明的任何资源。"""

    def __init__(self, bilibili):
        super().__init__()
        self.bilibili = bilibili
        self.open = Counter()
        self.skipped = 0
        self.meta = {}
        self.title = []
        self.article = []
        self.paragraphs = []
        self.video = {}

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skipped += 1
            self.open[tag] += 1
        if self.skipped:
            return
        if tag in ("title", "article", "main", "p"):
            self.open[tag] += 1
        if tag in _BLOCK:
            self._visible(" ")
        if tag == "meta":
            attrs = dict(attrs)
            value = _clean(attrs.get("content"))
            if value:
                for key in ("property", "name", "itemprop"):
                    if name := attrs.get(key):
                        self.meta.setdefault(name.lower(), value)

    def handle_endtag(self, tag):
        if self.skipped:
            if tag in _SKIP and self.open[tag]:
                self.skipped -= 1
                self.open[tag] -= 1
            return
        if tag in _BLOCK:
            self._visible(" ")
        if self.open[tag]:
            self.open[tag] -= 1

    def _visible(self, data):
        if self.open["article"] or self.open["main"]:
            self.article.append(data)
        elif self.open["p"]:
            self.paragraphs.append(data)

    def handle_data(self, data):
        if self.skipped:
            if self.bilibili and self.skipped == 1 and self.open["script"] and not self.video:
                if match := _STATE.search(data):
                    try:
                        state, _ = json.JSONDecoder().raw_decode(data, match.end())
                    except (ValueError, RecursionError):
                        return
                    video = state.get("videoData") if isinstance(state, dict) else None
                    if isinstance(video, dict):
                        self.video = video
        elif self.open["title"]:
            self.title.append(data)
        else:
            self._visible(data)

    def metadata(self, *keys):
        return next((self.meta[k] for k in keys if k in self.meta), "")

    def fields(self):
        title = self.metadata("og:title") or _clean("".join(self.title), 240)
        description = self.metadata("og:description", "description")
        if self.bilibili:
            video = self.video
            owner = video.get("owner")
            author = _clean(owner.get("name")) if isinstance(owner, dict) else ""
            return {
                "标题": _clean(video.get("title"), 240) or title,
                "UP主": author or self.metadata("author", "og:video:actor", "video:actor"),
                "简介": _clean(video.get("desc")) or description,
                "时长": _duration(video.get("duration"))
                or _duration(self.metadata("video:duration", "og:video:duration", "duration")),
            }
        return {
            "标题": title,
            "摘要": description,
            "正文": _clean("".join(self.article), 1200) or _clean("".join(self.paragraphs), 1200),
            "站点": self.metadata("og:site_name"),
        }


def _duration(value):
    """秒数转为分:秒或时:分:秒，保留页面提供的其他时长格式。"""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999999999:
        seconds = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        seconds = int(value)
    else:
        return _clean(value, 32)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _route(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("不是 HTTP(S) 页面地址")
    host = parts.hostname.rstrip(".")
    if host == "b23.tv" or host.endswith(".b23.tv"):
        return "bilibili", "short"
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili", "bilibili" if _VIDEO.match(parts.path) else "generic"
    return "generic", "generic"


def site_allowed(url, *, sites):
    """在初始请求与每次重定向前应用同一份站点开关。"""
    site, _ = _route(url)
    return site in sites


async def resolve(url, *, sites: frozenset[str], fetch) -> dict[str, str] | None:
    """只抓取启用站点的链接；再次检查最终地址，不把关闭的站点当通用页处理。"""
    if not site_allowed(url, sites=sites):
        return None
    try:
        page = await fetch(url)
    except Exception as exc:
        raise ValueError("页面抓取失败") from exc
    final_url = str(page.final_url)
    site, adapter = _route(final_url)
    if site not in sites or adapter not in sites or adapter == "short":
        return None
    mime = page.content_type.partition(";")[0].strip().lower()
    if mime not in ("text/html", "application/xhtml+xml"):
        return None
    parser = _Page(adapter == "bilibili")
    try:
        parser.feed(page.text)
        parser.close()
    except AssertionError as exc:
        # HTMLParser 对无法识别的声明可能抛断言错误。
        raise ValueError("页面标记无效") from exc
    fields = {key: value for key, value in parser.fields().items() if value}
    if not (fields.keys() & {"标题", "摘要", "正文", "简介"}):
        return None
    return {**fields, "链接": final_url}
