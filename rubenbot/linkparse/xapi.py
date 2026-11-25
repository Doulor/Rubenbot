"""fxtwitter 公开 JSON 的取值（core 解析与 link_detail 插件共用）。

x.com 在部分出口上完全连不上，publish.twitter.com 的 oEmbed 也可能返回空；
fxtwitter 的公开 JSON 能给出推文本身的作者、正文、时间与媒体。这里只读它
实际返回的字段：结构不符、状态不是成功、或 ID 不是所请求的那条时不猜、不
用别的推文顶替，返回空结果。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# 家里云实测：fxtwitter 的推文 JSON 单次要 5～6 秒，默认的 6 秒总预算
# （含 DNS、TLS 与读正文）几乎没有余量。镜像请求单独给到这个下限，
# 仍受配置里 30 秒的总上限约束。
TIMEOUT_SECONDS = 10.0


def parse(source) -> dict:
    """把 fxtwitter 响应解成 JSON 对象；不是对象或明确报错时返回空字典。"""
    try:
        data = json.loads(source) if isinstance(source, str) else None
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) and data.get("code") == 200 else {}


def _text(value, limit: int = 4000) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _published(tweet: dict) -> str:
    """优先用 Unix 时间戳统一成 UTC；没有时保留镜像给的原始时间串。"""
    stamp = tweet.get("created_timestamp")
    if type(stamp) in (int, float) and 0 < stamp <= 4_102_444_800:  # 2100-01-01
        return datetime.fromtimestamp(int(stamp), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _text(tweet.get("created_at"), 40)


def _media(tweet: dict) -> tuple[str, str]:
    """返回 ``(媒体概述, 封面地址)``；没有媒体时都是空串。"""
    media = tweet.get("media")
    if not isinstance(media, dict):
        return "", ""
    photos = [item for item in media.get("photos") or [] if isinstance(item, dict)]
    videos = [item for item in media.get("videos") or [] if isinstance(item, dict)]
    parts = []
    if photos:
        parts.append(f"图片 {len(photos)} 张")
    if videos:
        parts.append(f"视频 {len(videos)} 个")
    cover = _text(photos[0].get("url"), 2000) if photos else ""
    if not cover and videos:
        cover = _text(videos[0].get("thumbnail_url"), 2000)
    return " · ".join(parts), cover


def fields(data: dict, status_id: str) -> dict:
    """取一条推文的统一字段；不是所请求的那条时返回空字典。

    统一字段：``作者``、``正文``、``发布时间``、``媒体``、``封面``，以及 ``点赞`` /
    ``转发`` / ``回复`` / ``查看``（镜像没给或为负时不出现）。只有媒体没有
    文字的推文，正文用媒体概述，不编造句子。
    """
    item = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(item, dict) or str(item.get("id") or "") != str(status_id):
        return {}
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    name, handle = _text(author.get("name"), 120), _text(author.get("screen_name"), 60)
    byline = f"{name} (@{handle})" if name and handle else name or (f"@{handle}" if handle else "")
    media, cover = _media(item)
    body = _text(item.get("text")) or media
    values = {
        "作者": byline,
        "正文": body,
        "发布时间": _published(item),
        "媒体": media,
        "封面": cover,
        "点赞": _count(item.get("likes")),
        "转发": _count(item.get("reposts") if item.get("reposts") is not None else item.get("retweets")),
        "回复": _count(item.get("replies")),
        "查看": _count(item.get("views")),
    }
    return {key: value for key, value in values.items() if value not in ("", None)}
