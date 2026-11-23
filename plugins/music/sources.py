"""点歌音源：网易云官方搜索接口与 txqq 聚合接口。

参考氢的点歌插件（hydrogen_astrbot-plugin-music，MIT）的取音源方式：

- 网易云走 ``music.163.com/api/cloudsearch/pc``（免 key，带浏览器 UA 与 Referer），
  播放地址用 ``song/media/outer/url``。**免费曲目**能 302 到 CDN 上的 mp3，
  VIP / 无版权曲目会 302 到 ``music.163.com/404`` 返回一段 HTML——协议端拿它
  转语音会直接报 ``Failed to open input``，所以结果要先探活（见
  :func:`probe_audio`），探不出音频的条目不进选歌列表。
- QQ / 酷狗 / 酷我 / 百度 / 一听 / 咪咕 / 荔枝 / 蜻蜓 / 喜马拉雅 / 5sing / 全民K歌
  走 txqq 聚合接口 ``music.txqq.pro``，返回里带音频直链（QQ 源是 CDN 的 m4a）。

所有请求都返回 :class:`Song` 列表；接口结构变化或网络失败时返回空列表，由调用方
统一回一句「没找到」，不把解析异常抛进聊天主流程。唯一例外是
:class:`SongsUnplayable`：搜到了歌，但探活后一首能放的都没有，调用方据此给
一句不一样的提示，而不是假装搜不到。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

NETEASE_SEARCH_URL = "https://music.163.com/api/cloudsearch/pc"
NETEASE_COMMENTS_URL = "https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}"
NETEASE_LYRIC_URL = "https://music.163.com/api/song/lyric"
NETEASE_SONG_URL = "https://music.163.com/#/song?id={song_id}"
# 实测 http 直链可用；https 会 302 到空响应
NETEASE_AUDIO_URL = "http://music.163.com/song/media/outer/url?id={song_id}.mp3"
NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}
TXQQ_SEARCH_URL = "https://music.txqq.pro/"
TXQQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:110.0) "
        "Gecko/20100101 Firefox/110.0"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://music.txqq.pro",
    "Referer": "https://music.txqq.pro/",
}
# 探活只等这么久：慢的 CDN 宁可当「不确定」也不能拖住点歌回复
PROBE_TIMEOUT = 8.0
PROBE_RANGE = "bytes=0-1023"


class SongsUnplayable(Exception):
    """搜到了结果，但探活后没有一首拿得到可播音频。"""

    def __init__(self, keyword: str, count: int):
        self.keyword = keyword
        self.count = count
        super().__init__(f"《{keyword}》搜到 {count} 首，但没有可播的音频")


@dataclass(frozen=True)
class Song:
    """一首歌的最小信息；音频、封面、歌词都可能为空，由发送链逐档降级。"""

    id: str
    name: str
    artists: tuple[str, ...] = ()
    duration_ms: int | None = None
    cover_url: str = ""
    audio_url: str = ""
    link: str = ""
    source: str = ""
    # txqq 的子平台（qq / kugou / kuwo ...）；网易云为 netease
    platform_type: str = ""
    lyrics: str = ""

    @property
    def artist_text(self) -> str:
        return "、".join(self.artists) if self.artists else "未知歌手"

    @property
    def duration_text(self) -> str:
        if not self.duration_ms or self.duration_ms <= 0:
            return ""
        seconds = self.duration_ms // 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class PlatformTarget:
    """一条点歌命令解析出的目标音源。"""

    source: str
    platform_type: str
    label: str


class NetEaseSource:
    """网易云音乐（官方 Web 接口）。"""

    name = "netease"
    display_name = "网易云音乐"

    async def search(
        self, client: httpx.AsyncClient, keyword: str, limit: int
    ) -> list[Song]:
        response = await client.get(
            NETEASE_SEARCH_URL,
            params={"s": keyword, "type": 1, "limit": limit, "offset": 0},
            headers=NETEASE_HEADERS,
        )
        response.raise_for_status()
        return parse_netease_songs(response.json(), limit=limit)

    async def hot_comment(self, client: httpx.AsyncClient, song: Song) -> str:
        """取一条热门评论；没有评论或接口失败返回空串。"""
        try:
            response = await client.get(
                NETEASE_COMMENTS_URL.format(song_id=song.id),
                params={"limit": 20, "offset": 0},
                headers=NETEASE_HEADERS,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - 热评是锦上添花
            logger.warning("music 取热评失败（%s）：%s", song.name, exc)
            return ""
        data = response.json()
        comments = []
        if isinstance(data, Mapping):
            comments = data.get("hotComments") or data.get("comments") or []
        contents = [
            str(item.get("content") or "").strip()
            for item in comments
            if isinstance(item, Mapping)
        ]
        contents = [item for item in contents if item]
        return random.choice(contents) if contents else ""

    async def lyrics(self, client: httpx.AsyncClient, song: Song) -> str:
        try:
            response = await client.get(
                NETEASE_LYRIC_URL,
                params={"id": song.id, "lv": 1, "kv": 1, "tv": -1},
                headers=NETEASE_HEADERS,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - 歌词失败只影响这一条
            logger.warning("music 取歌词失败（%s）：%s", song.name, exc)
            return ""
        data = response.json()
        if not isinstance(data, Mapping):
            return ""
        lrc = data.get("lrc")
        if isinstance(lrc, Mapping):
            return str(lrc.get("lyric") or "")
        return ""


class TXQQSource:
    """txqq 聚合接口：一个入口搜十几个平台，返回里带音频直链。"""

    name = "txqq"
    display_name = "QQ音乐"

    async def search(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        limit: int,
        *,
        platform_type: str = "qq",
    ) -> list[Song]:
        response = await client.post(
            TXQQ_SEARCH_URL,
            data={
                "input": keyword,
                "filter": "name",
                "type": platform_type,
                "page": 1,
            },
            headers=TXQQ_HEADERS,
        )
        response.raise_for_status()
        return parse_txqq_songs(response.json(), limit=limit, platform_type=platform_type)

    async def hot_comment(self, client: httpx.AsyncClient, song: Song) -> str:
        """聚合接口不带评论，热评只对网易云有意义。"""
        return ""

    async def lyrics(self, client: httpx.AsyncClient, song: Song) -> str:
        """搜索结果里已带 lrc，直接返回；没有就是空串。"""
        return song.lyrics


NETEASE_SOURCE = NetEaseSource()
TXQQ_SOURCE = TXQQSource()


async def probe_audio(
    client: httpx.AsyncClient, url: str, *, timeout: float = PROBE_TIMEOUT
) -> bool | None:
    """探活音频直链：``True`` 可播、``False`` 明确不可播、``None`` 不确定。

    网易云的 outer url 对 VIP 曲目会 302 到 ``music.163.com/404``（text/html），
    协议端拿 HTML 转语音会失败（实测 retcode 100）。先 HEAD 看最终响应的
    content-type；CDN 不支持 HEAD 时退回 ``Range: bytes=0-1023`` 的 GET。
    网络异常按「不确定」处理，不因为一次超时把整批结果扔掉。
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        response = await client.head(
            url, headers=NETEASE_HEADERS, timeout=timeout, follow_redirects=True
        )
    except Exception as exc:  # noqa: BLE001 - HEAD 失败再试 GET
        logger.debug("music 探活 HEAD 失败（%s）：%s", url[:80], exc)
        response = None
    if response is not None and response.status_code in (405, 501):
        # 部分 CDN 不支持 HEAD：不能据此判死，退回 GET 再问一次
        logger.debug("music 探活 HEAD 不被支持（%s）：%s", url[:80], response.status_code)
        response = None
    verdict = _audio_verdict(response)
    if verdict is not None:
        return verdict
    try:
        response = await client.get(
            url,
            headers={**NETEASE_HEADERS, "Range": PROBE_RANGE},
            timeout=timeout,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - 探不出就交给发送链降级
        logger.debug("music 探活 GET 失败（%s）：%s", url[:80], exc)
        return None
    return _audio_verdict(response)


def _audio_verdict(response: httpx.Response | None) -> bool | None:
    if response is None:
        return None
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type.startswith("audio/"):
        return True
    if content_type.startswith(("text/html", "text/plain", "application/json")):
        return False
    if response.status_code >= 400:
        return False
    return None


def parse_netease_songs(data: Any, *, limit: int = 5) -> list[Song]:
    """解析 cloudsearch 响应；结构不对或没有结果时返回空列表。"""
    if not isinstance(data, Mapping):
        return []
    result = data.get("result")
    container = result if isinstance(result, Mapping) else data
    songs = container.get("songs") if isinstance(container, Mapping) else None
    if not isinstance(songs, list):
        return []
    parsed: list[Song] = []
    for item in songs[: max(0, limit)]:
        song = _parse_netease_song(item)
        if song is not None:
            parsed.append(song)
    return parsed


def _parse_netease_song(item: Any) -> Song | None:
    if not isinstance(item, Mapping):
        return None
    song_id = item.get("id")
    name = str(item.get("name") or "").strip()
    if song_id in (None, "") or not name:
        return None
    raw_artists = item.get("ar") or item.get("artists") or []
    artists = _artist_names(raw_artists)
    album = item.get("al") or item.get("album") or {}
    cover = str(album.get("picUrl") or "").strip() if isinstance(album, Mapping) else ""
    duration = item.get("dt")
    if duration in (None, ""):
        duration = item.get("duration")
    audio = str(item.get("audio_url") or "").strip()
    if not audio:
        audio = NETEASE_AUDIO_URL.format(song_id=song_id)
    return Song(
        id=str(song_id),
        name=name,
        artists=artists,
        duration_ms=_as_int(duration),
        cover_url=cover,
        audio_url=audio,
        link=str(item.get("link") or NETEASE_SONG_URL.format(song_id=song_id)),
        source="netease",
        platform_type="netease",
    )


def parse_txqq_songs(
    data: Any, *, limit: int = 5, platform_type: str = "qq"
) -> list[Song]:
    """解析 txqq 响应；``data`` 不是列表时返回空列表。"""
    rows = data.get("data") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return []
    parsed: list[Song] = []
    for row in rows[: max(0, limit)]:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("title") or "").strip()
        if not name:
            continue
        audio = str(row.get("url") or "").strip()
        # 聚合接口偶尔给出占位地址（只有域名、没有路径），当没有音频处理
        if audio and not _looks_like_audio_url(audio):
            audio = ""
        parsed.append(
            Song(
                id=str(row.get("songid") or "").strip(),
                name=name,
                artists=_split_artists(row.get("author")),
                cover_url=str(row.get("pic") or "").strip(),
                audio_url=audio,
                link=str(row.get("link") or "").strip(),
                source="txqq",
                platform_type=str(row.get("type") or platform_type).strip(),
                lyrics=str(row.get("lrc") or ""),
            )
        )
    return parsed


def _artist_names(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return _split_artists(raw)
    if not isinstance(raw, (list, tuple)):
        return ()
    names = []
    for artist in raw:
        if isinstance(artist, Mapping):
            value = str(artist.get("name") or "").strip()
        else:
            value = str(artist or "").strip()
        if value:
            names.append(value)
    return tuple(names)


def _split_artists(raw: Any) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return ()
    for separator in ("、", "/", "&", "，", ","):
        if separator in text:
            parts = [item.strip() for item in text.split(separator)]
            return tuple(item for item in parts if item)
    return (text,)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_audio_url(url: str) -> bool:
    """聚合接口的占位地址只有域名（如 ``http://aqqmusic.tc.qq.com/``），视作无音频。"""
    without_scheme = url.split("://", 1)[-1]
    return "/" in without_scheme and bool(without_scheme.split("/", 1)[1].strip())
