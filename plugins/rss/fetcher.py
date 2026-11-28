"""RSS/Atom 抓取、解析与去重。

抓取走插件上下文的 ``fetch_public_page``（core 的 SSRF 守卫 fetcher，
1 MiB、超时可调），这里只做解析与清洗，不另写 fetcher。
"""

from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from typing import Any

import feedparser

# 单源抓取超时（秒）；feedparser 本身是纯解析，不出网。
FETCH_TIMEOUT = 15.0


class _TextExtractor(HTMLParser):
    """HTML 转纯文本：丢 script/style，留正文，块级元素断行。"""

    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "ul",
            "ol",
            "blockquote",
            "pre",
            "tr",
            "br",
            "hr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = html.unescape(raw)
        # 空行压成一行、行首尾去空格；正文里连续空格压成一个。
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(value: str | None) -> str:
    """HTML/带实体文本洗成纯文本；纯文本原样过一遍空白归一。"""
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return re.sub(r"[ \t]+", " ", value).strip()
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - 解析失败就退回粗暴去标签
        text = re.sub(r"<[^>]+>", "\n", value)
        return html_to_text(text) if "<" in text else text.strip()
    return parser.text()


def _entry_time(entry: Any) -> float:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return time.mktime(parsed)
            except (OverflowError, ValueError):
                continue
    return 0.0


def parse_feed(text: str) -> tuple[str, list[dict]]:
    """解析 feed 文本，返回（源标题，条目列表，按发布时间倒序）。

    条目字段：guid（没有就用链接）、title、link、content（纯文本）、
    published（时间戳，取不到为 0）。解析失败（bozo 且无条目）抛 ValueError。
    """
    parsed = feedparser.parse(text)
    entries = parsed.get("entries") or []
    if not entries and parsed.get("bozo"):
        raise ValueError(f"解析失败：{parsed.get('bozo_exception') or '不是有效的 feed'}")
    title = html_to_text(parsed.feed.get("title") or "") or "未命名订阅源"
    items: list[dict] = []
    for entry in entries:
        link = str(entry.get("link") or "").strip()
        guid = str(entry.get("id") or entry.get("guid") or link).strip()
        if not guid and not link:
            continue
        content = ""
        for content_part in entry.get("content") or []:
            value = content_part.get("value") if isinstance(content_part, dict) else None
            if value:
                content = str(value)
                break
        if not content:
            content = str(entry.get("summary") or entry.get("description") or "")
        items.append(
            {
                "guid": guid or link,
                "title": html_to_text(str(entry.get("title") or "无标题")),
                "link": link,
                "content": html_to_text(content),
                "published": _entry_time(entry),
            }
        )
    items.sort(key=lambda item: item["published"], reverse=True)
    return title, items


def new_entries(items: list[dict], last_seen_guid: str, max_items: int) -> list[dict]:
    """取比水位新的条目（按时间正序，方便按顺序推送），最多 max_items 条。

    水位为空表示首次订阅：不推旧条目，调用方应直接把水位设到最新一条。
    """
    if not last_seen_guid:
        return []
    fresh: list[dict] = []
    for item in items:
        if item["guid"] == last_seen_guid:
            break
        fresh.append(item)
        if len(fresh) >= max_items:
            break
    # items 是倒序（新在前），fresh 也是新在前；推送按旧→新排。
    return list(reversed(fresh))


async def fetch_feed_text(fetch_public_page: Any, url: str) -> str:
    """经受检入口抓 feed 文本；失败直接抛异常，由调用方记失败计数。"""
    result = await fetch_public_page(url, timeout=FETCH_TIMEOUT)
    return result.text
