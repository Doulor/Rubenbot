"""从网页文本里提取可读内容：结构化数据优先，正文兜底。

分层与来源设计一致（不复制上游代码）：

1. 平台专用提取器（目前是 B 站的 ``__INITIAL_STATE__`` 结构化数据）；
2. 通用元数据（Open Graph / Twitter Card / ``<meta>`` / JSON-LD）；
3. readability 式正文（优先 ``<article>`` / ``<main>``，其次较长的段落）。

这里只处理已经抓到的字符串，不执行脚本、不访问页面声明的任何资源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .urls import Platform

_SKIP = {"script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "form", "header"}
_BLOCK = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "br", "blockquote", "tr", "td"}
_STATE = re.compile(r"(?:window\s*\.\s*)?\b__INITIAL_STATE__\s*=\s*")
# 正文里常见的组件文案：单看长度会误收，短段落再按这些前缀过滤。
_BOILERPLATE = re.compile(
    r"^(?:登录|注册|分享|收藏|点赞|评论|转发|查看更多|展开全文|阅读原文|返回顶部|广告|扫码|下载客户端"
    r"|sign in|log in|share|subscribe|advertisement)",
    re.I,
)
_MIN_PARAGRAPH = 18
_MIN_LIST_ITEM = 8
_MAX_JSONLD_CHARS = 200_000
_LD_TYPES = ("article", "newsarticle", "blogposting", "videoobject", "webpage", "discussionforumposting")


def clean(value, limit: int = 800) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _duration(value) -> str:
    """秒数转为分:秒或时:分:秒，保留页面提供的其他时长格式。"""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999_999_999:
        seconds = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value.strip()):
        seconds = int(value.strip())
    elif isinstance(value, str) and value.strip():
        return clean(value, 32)
    else:
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class _Page(HTMLParser):
    """收集可见文字、元数据、JSON-LD 与（可选）内联脚本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: dict[str, int] = {}
        self.skipped = 0
        self.meta: dict[str, str] = {}
        self.title: list[str] = []
        self.heading: list[str] = []
        self.article: list[str] = []
        self.paragraphs: list[str] = []
        self.scripts: list[str] = []
        self.jsonld: list[str] = []
        self._script_chunks: list[str] | None = None
        self._script_is_ld = False
        self._buffer: list[str] = []
        self._buffer_target: list[str] | None = None

    # ---- HTMLParser 回调 ----
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "script":
            self.skipped += 1
            self.open["script"] = self.open.get("script", 0) + 1
            attributes = dict(attrs)
            self._script_chunks = []
            self._script_is_ld = (attributes.get("type") or "").strip().lower() == "application/ld+json"
            return
        if tag in _SKIP:
            self.skipped += 1
            self.open[tag] = self.open.get(tag, 0) + 1
            return
        if self.skipped:
            return
        if tag in ("title", "article", "main", "p", "h1"):
            self.open[tag] = self.open.get(tag, 0) + 1
        if tag in _BLOCK:
            self._flush()
        if tag == "meta":
            self._read_meta(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._finish_script()
            return
        if tag in _SKIP:
            if self.skipped and self.open.get(tag):
                self.skipped -= 1
                self.open[tag] -= 1
            return
        if self.skipped:
            return
        if tag in _BLOCK:
            self._flush()
        if self.open.get(tag):
            self.open[tag] -= 1
            if tag != "p":
                self._flush()

    def handle_data(self, data: str) -> None:
        if self.skipped:
            if self._script_chunks is not None:
                self._script_chunks.append(data)
            return
        if self.open.get("title"):
            self.title.append(data)
            return
        if self.open.get("h1"):
            self.heading.append(data)
            return
        if self.open.get("article") or self.open.get("main"):
            self._buffer_target = self.article
        elif self.open.get("p") or self.open.get("li"):
            self._buffer_target = self.paragraphs
        else:
            self._buffer_target = None
        if self._buffer_target is not None:
            self._buffer.append(data)

    # ---- 内部 ----
    def _flush(self) -> None:
        text = clean("".join(self._buffer), 4000)
        self._buffer.clear()
        if text and self._buffer_target is not None:
            self._buffer_target.append(text)

    def _finish_script(self) -> None:
        text = "".join(self._script_chunks or [])
        self._script_chunks = None
        if self.skipped and self.open.get("script"):
            self.skipped -= 1
            self.open["script"] -= 1
        stripped = text.strip()
        if self._script_is_ld or stripped.startswith(("{", "[")) and "__INITIAL_STATE__" not in stripped:
            self.jsonld.append(text)
        self.scripts.append(text)

    def _read_meta(self, attrs) -> None:
        attributes = dict(attrs)
        value = clean(attributes.get("content"), 2000)
        if not value:
            return
        for key in ("property", "name", "itemprop"):
            name = attributes.get(key)
            if name:
                self.meta.setdefault(name.strip().lower(), value)

    # ---- 取值 ----
    def metadata(self, *keys: str) -> str:
        return next((self.meta[key] for key in keys if self.meta.get(key)), "")

    def title_text(self) -> str:
        return (
            self.metadata("og:title", "twitter:title")
            or clean("".join(self.heading), 240)
            or clean("".join(self.title), 240)
        )

    def description_text(self) -> str:
        return self.metadata("og:description", "twitter:description", "description", "weibo:article:description")

    def site_text(self) -> str:
        return self.metadata("og:site_name", "application-name", "twitter:site")

    def body_text(self, limit: int) -> str:
        if self.article:
            return _readable(self.article, limit, keep_short=True)
        return _readable(self.paragraphs, limit)


def _readable(parts: list[str], limit: int, *, keep_short: bool = False) -> str:
    """拼出正文：过滤短句与导航式样板，按出现顺序保留。"""
    if not parts:
        return ""
    picked: list[str] = []
    seen: set[str] = set()
    for part in parts:
        minimum = _MIN_LIST_ITEM if keep_short else _MIN_PARAGRAPH
        if len(part) < minimum and not keep_short:
            continue
        if _BOILERPLATE.match(part):
            continue
        key = part[:60]
        if key in seen:
            continue
        seen.add(key)
        picked.append(part)
    if not picked:
        return clean(max(parts, key=len), limit)
    return clean(" ".join(picked), limit)


def _flatten_jsonld(value) -> list:
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_jsonld(item))
        return result
    if not isinstance(value, dict):
        return []
    result = [value]
    if isinstance(value.get("@graph"), list):
        result.extend(_flatten_jsonld(value["@graph"]))
    return result


def _jsonld_fields(raw_blocks: list[str]) -> dict[str, str]:
    """从 JSON-LD 取标题、摘要、作者、发布时间；坏块忽略。"""
    fields: dict[str, str] = {}
    for raw in raw_blocks[:8]:
        if len(raw) > _MAX_JSONLD_CHARS:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(raw.strip())
        except (ValueError, RecursionError):
            continue
        items = [item for item in _flatten_jsonld(data) if isinstance(item, dict)]
        items.sort(key=lambda item: _LD_TYPES.index(type_name)
                   if (type_name := str(item.get("@type", "")).lower()) in _LD_TYPES else len(_LD_TYPES))
        for item in items:
            author = item.get("author")
            if isinstance(author, list):
                author = author[0] if author else ""
            if isinstance(author, dict):
                author = author.get("name")
            candidates = {
                "标题": clean(item.get("headline") or item.get("name"), 240),
                "摘要": clean(item.get("description"), 800),
                "作者": clean(author, 120),
                "发布时间": clean(item.get("datePublished") or item.get("uploadDate"), 40),
                "正文": clean(item.get("articleBody"), 4000),
            }
            for key, value in candidates.items():
                if value and not fields.get(key):
                    fields[key] = value
    return fields


def _bilibili_state(scripts: list[str]) -> dict:
    """取 B 站页面内联状态里的视频信息；桌面版与移动版结构不同，两个都认。"""
    for text in scripts:
        match = _STATE.search(text)
        if not match:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(text, match.end())
        except (ValueError, RecursionError):
            continue
        if not isinstance(state, dict):
            continue
        # 桌面版：__INITIAL_STATE__.videoData
        # 移动版：__INITIAL_STATE__.video.viewInfo（upInfo 在 m 站常为空）
        video = state.get("videoData")
        if isinstance(video, dict):
            return video
        page = state.get("video")
        view = page.get("viewInfo") if isinstance(page, dict) else None
        if isinstance(view, dict):
            return view
    return {}


def _bilibili_fields(video: dict, page: _Page) -> dict[str, str]:
    owner = video.get("owner")
    author = clean(owner.get("name"), 120) if isinstance(owner, dict) else ""
    duration = _duration(video.get("duration")) or _duration(
        page.metadata("video:duration", "og:video:duration", "duration")
    )
    return {
        "标题": clean(video.get("title"), 240) or page.title_text(),
        "作者": author or page.metadata("author", "og:video:actor", "video:actor"),
        "简介": clean(video.get("desc")) or page.description_text(),
        "时长": duration,
    }


_LOGIN_MARKERS = (
    "请登录", "登录后查看", "登录后继续", "enable javascript", "javascript is disabled",
    "verify you are human", "captcha", "验证码", "请开启 javascript", "sign in to continue",
    "log in to continue", "visitor system",
)
_LOGIN_TITLES = frozenset({
    "登录", "登陆", "log in", "login", "sign in", "x", "captcha", "sina visitor system",
})
# 这些平台的正文只在登录态或脚本渲染后才出现，页面本身往往只剩一个站点名标题。
# 只拿到标题时按「抓不到」处理，不让营销标题冒充帖子内容。
_SHELL_PLATFORMS = frozenset({"x", "weibo", "douyin", "xiaohongshu"})
# 只按路径判断是不是视频页：B 站专栏 / 动态 / 个人页仍走通用提取。
_VIDEO_PATH = re.compile(r"^/video/(?:BV[0-9A-Za-z]+|av[0-9]+)/?$", re.I)


def _is_video_page(url: str) -> bool:
    try:
        from urllib.parse import urlsplit

        return bool(_VIDEO_PATH.match(urlsplit(url).path or ""))
    except ValueError:
        return False


def _login_shell(page: _Page) -> bool:
    """只剩站点名和登录/开脚本提示时，明确按读不到处理，不把提示词当正文。"""
    title = page.title_text().strip().lower()
    description = page.description_text().lower()
    if title in _LOGIN_TITLES:
        return True
    return any(marker in description for marker in _LOGIN_MARKERS)


@dataclass(frozen=True)
class Extracted:
    """一次页面提取的结果；``fields`` 为空表示没有可读内容。"""

    fields: dict[str, str] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return bool(self.fields.keys() & {"标题", "摘要", "正文", "简介"})


def extract(url: str, html: str, *, platform: Platform, max_body_chars: int = 1200) -> Extracted:
    """提取一页的内容；平台专用字段优先，通用元数据与正文随后补齐。"""
    parser = _Page()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:
        # HTMLParser 对无法识别的声明可能抛断言错误；按无内容处理。
        return Extracted()

    fields: dict[str, str] = {}
    video_page = platform.key == "bilibili" and _is_video_page(url)
    if video_page:
        # 没拿到结构化数据时也用页面元数据填同一组字段，字段口径与插件旧版一致。
        video = _bilibili_state(parser.scripts) or {}
        fields.update({key: value for key, value in _bilibili_fields(video, parser).items() if value})

    jsonld = _jsonld_fields(parser.jsonld)
    body = parser.body_text(max_body_chars) or jsonld.get("正文", "")
    generic = {
        "标题": parser.title_text() or jsonld.get("标题", ""),
        "摘要": parser.description_text() or jsonld.get("摘要", ""),
        "作者": parser.metadata("author", "article:author", "twitter:creator") or jsonld.get("作者", ""),
        "发布时间": parser.metadata(
            "article:published_time", "og:article:published_time", "date", "pubdate",
        ) or jsonld.get("发布时间", ""),
        "正文": body,
        "站点": parser.site_text(),
    }
    if video_page:
        # 视频页的 description 常是「播放量 / 弹幕量 / 相关视频」统计串，正文是
        # 页脚法务链接；结构化数据已经给了简介，这两项只会变成噪声。
        generic.pop("摘要", None)
        generic.pop("正文", None)
    for key, value in generic.items():
        if value and not fields.get(key):
            fields[key] = clean(value, max_body_chars if key == "正文" else 800)

    if not fields.keys() & {"标题", "摘要", "正文", "简介"}:
        return Extracted()
    if _login_shell(parser) and not fields.get("正文") and not fields.get("简介"):
        return Extracted()
    if (platform.key in _SHELL_PLATFORMS and not fields.keys() & {"正文", "简介", "摘要"}
            and len(fields.get("标题", "")) < 100):
        # 这类平台的正文在登录态或脚本渲染后才出现；只剩标题（往往是站点名或
        # 营销文案）时如实按读不到处理，不让它冒充「读到了」。
        return Extracted()
    return Extracted(fields)
