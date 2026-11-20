"""点歌插件：群聊/私聊里「点歌 <歌名>」或「我想听 <歌名>」，搜网易云后发一首。

流程：识别触发词 → 单人冷却 → 网易云公开搜索接口（cloudsearch，免 key）
→ 取第一首 → 用 OneBot 的 ``music`` 段发送。SnowLuma 之外的平台没有音乐卡片，
降级成「歌名 — 歌手 + 链接」的文字，有封面再附一张封面图。

失败一律降级成一句话，不吞请求、不抛异常打断对话：

- 搜索接口挂了/超时：回「搜不到，换个关键词试试」，日志里留下错误
- 搜索成功但没有结果：明确说没找到，不随便塞一首不相关的
- 音乐段发送失败：自动改用文字 + 封面链接

配置在本目录的 ``config.yaml``（没有就读 ``config.example.yaml``，都没有
也能跑，缺省值在代码里）；嵌入式场景可用 ``ctx.extras["music_config"]``
传一份映射覆盖。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import yaml

from rubenbot.adapters.base import Segment

logger = logging.getLogger(__name__)

# 网易云公开搜索接口（cloudsearch/pc）：免 key，但要求浏览器 UA 与 Referer
SEARCH_URL = "https://music.163.com/api/cloudsearch/pc"
SONG_WEB_URL = "https://music.163.com/#/song?id={song_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}
REQUEST_TIMEOUT = 10.0

DEFAULT_TRIGGERS = ("点歌", "我想听")
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_SOURCE = "netease"
# 目前只接网易云；manifest 里留 source 方便以后加音源
SUPPORTED_SOURCES = ("netease",)

# 歌名两端要剥掉的空白与标点
_TRIM_CHARS = " \t\r\n：:，,、。.!！?？~～…—-"
# 触发词后面只跟这些字时按「没说要听哪首」处理，不拿它们去搜
_HOLLOW_KEYWORDS = frozenset({"歌", "一首歌", "首歌", "一个", "个"})


@dataclass(frozen=True)
class Song:
    """一首歌的最小信息：发送与降级文案都只用这些字段。"""

    song_id: str
    name: str
    artists: tuple[str, ...] = ()
    cover_url: str = ""

    @property
    def artist_text(self) -> str:
        return "、".join(self.artists) if self.artists else "未知歌手"

    @property
    def link(self) -> str:
        return SONG_WEB_URL.format(song_id=self.song_id)

    def summary(self) -> str:
        return f"{self.name} — {self.artist_text}"


@dataclass(frozen=True)
class MusicConfig:
    """归一化后的插件配置；类型不对的项退回默认值，不让配置错误炸掉插件。"""

    triggers: tuple[str, ...] = DEFAULT_TRIGGERS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    source: str = DEFAULT_SOURCE

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "MusicConfig":
        raw = data if isinstance(data, Mapping) else {}
        triggers = _as_triggers(raw.get("triggers"))
        source = str(raw.get("source") or DEFAULT_SOURCE).strip().lower()
        if source not in SUPPORTED_SOURCES:
            logger.warning("music 的音源 %s 暂不支持，改用 %s", source, DEFAULT_SOURCE)
            source = DEFAULT_SOURCE
        return cls(
            triggers=triggers if triggers is not None else DEFAULT_TRIGGERS,
            cooldown_seconds=_as_non_negative(
                raw.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS
            ),
            source=source,
        )


def read_config(directory: str | Path | None = None) -> MusicConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；没有或损坏时用默认值。"""
    if directory is None:
        return MusicConfig()
    base = Path(directory)
    file = next(
        (item for item in (base / "config.yaml", base / "config.example.yaml") if item.exists()),
        None,
    )
    if file is None:
        return MusicConfig()
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能启动
        logger.warning("music 读取配置 %s 失败，使用默认配置：%s", file, exc)
        return MusicConfig()
    return MusicConfig.from_mapping(data)


def load_config(ctx: Any = None) -> MusicConfig:
    """优先用 ``ctx.extras["music_config"]`` 覆盖，其次读插件目录 config.yaml。"""
    extras = getattr(ctx, "extras", None) if ctx is not None else None
    if isinstance(extras, Mapping):
        override = extras.get("music_config")
        if isinstance(override, Mapping):
            return MusicConfig.from_mapping(override)
    directory = getattr(getattr(ctx, "plugin", None), "path", None)
    return read_config(directory)


def parse_trigger(text: str, triggers: tuple[str, ...] | list[str]) -> str | None:
    """识别点歌触发词，返回歌名；未命中返回 None。

    命中但没带歌名时返回空串（由调用方回一句提示）。触发词按长度从长到短
    匹配，避免「点歌」抢先吃掉「点歌单」这类更长的词。
    """
    value = (text or "").strip()
    if not value:
        return None
    for trigger in sorted((item for item in triggers if item), key=len, reverse=True):
        if value.startswith(trigger):
            keyword = value[len(trigger) :].strip().strip(_TRIM_CHARS).strip()
            # 「我想听歌」「点首歌」这类只表达想听、没带歌名的说法按空歌名处理
            if keyword in _HOLLOW_KEYWORDS:
                return ""
            return keyword
    return None


def parse_song(item: Any) -> Song | None:
    """把 cloudsearch 里的一首歌解析成 :class:`Song`；缺 id/歌名时返回 None。"""
    if not isinstance(item, Mapping):
        return None
    song_id = item.get("id")
    name = str(item.get("name") or "").strip()
    if song_id in (None, "") or not name:
        return None
    raw_artists = item.get("ar") or item.get("artists") or []
    artists: list[str] = []
    if isinstance(raw_artists, (list, tuple)):
        for artist in raw_artists:
            if not isinstance(artist, Mapping):
                continue
            artist_name = str(artist.get("name") or "").strip()
            if artist_name:
                artists.append(artist_name)
    album = item.get("al") or item.get("album") or {}
    cover = ""
    if isinstance(album, Mapping):
        cover = str(album.get("picUrl") or "").strip()
    return Song(song_id=str(song_id), name=name, artists=tuple(artists), cover_url=cover)


def parse_search_response(data: Any) -> Song | None:
    """从搜索响应里取第一首；没有结果或结构不对时返回 None。

    cloudsearch 的正式结构是 ``{"result": {"songs": [...]}}``；部分镜像实现
    会把 ``songs`` 放在顶层，两种都认。
    """
    if not isinstance(data, Mapping):
        return None
    result = data.get("result")
    container = result if isinstance(result, Mapping) else data
    songs = container.get("songs") if isinstance(container, Mapping) else None
    if not isinstance(songs, list) or not songs:
        return None
    return parse_song(songs[0])


class MusicPlayer:
    """点歌的识别、搜索与发送；时钟与 HTTP 客户端可注入，便于测试。"""

    def __init__(
        self,
        config: MusicConfig | Mapping[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if isinstance(config, MusicConfig):
            self.config = config
        else:
            self.config = MusicConfig.from_mapping(config)
        self._client = client
        self._owns_client = client is None
        self.clock = clock or time.monotonic
        # (平台, 点歌人) -> 上次点歌时间
        self._last_played_at: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    async def on_message(self, runtime, event):
        """命中「点歌/我想听」就处理并拦截消息，不让它再走模型回复。"""
        text = getattr(event, "text", "") or ""
        matched = parse_trigger(text, self.config.triggers) is not None
        try:
            return await self._handle(runtime, event)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.exception("music 处理点歌异常：%s", exc)
            # 只有本插件认领的消息才回一句；无关消息的异常直接放行
            if matched:
                await self._reply(runtime, event, "点歌出了点问题，稍后再试试")
                return False
            return None

    async def _handle(self, runtime, event):
        keyword = parse_trigger(getattr(event, "text", "") or "", self.config.triggers)
        if keyword is None:
            return None
        if not keyword:
            await self._reply(runtime, event, "想听哪首歌？说「点歌 歌名」就好")
            return False

        key = (str(getattr(event, "platform", "") or ""), str(getattr(event, "sender_id", "")))
        now = self.clock()
        last = self._last_played_at.get(key)
        if last is not None:
            remaining = self.config.cooldown_seconds - (now - last)
            if remaining > 0:
                await self._reply(
                    runtime, event, f"刚点过啦，{math.ceil(remaining)} 秒后再点吧"
                )
                return False
        # 先记冷却再请求：接口挂了也不在冷却期内反复打它
        self._last_played_at[key] = now

        try:
            song = await self.search(keyword)
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败都降级
            logger.warning("music 搜索「%s」失败：%s", keyword, exc)
            await self._reply(runtime, event, "搜不到，换个关键词试试")
            return False

        if song is None:
            # 明确说没找到，绝不随便塞一首不相关的
            logger.info("music 搜索「%s」没有结果", keyword)
            await self._reply(runtime, event, f"没找到《{keyword}》，换个关键词试试")
            return False

        await self._send_song(runtime, event, song)
        return False

    async def aclose(self) -> None:
        """插件停用时释放自己创建的 HTTP 连接池（注入的客户端留给调用方）。"""
        client = self._client
        if client is None or not self._owns_client:
            return
        self._client = None
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不致命
            logger.debug("music 关闭 HTTP 客户端失败：%s", exc)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
            self._owns_client = True
        return self._client

    async def search(self, keyword: str) -> Song | None:
        """搜一首歌；网络/HTTP 错误向上抛，由调用方统一降级。"""
        client = await self._get_client()
        response = await client.get(
            SEARCH_URL,
            params={"s": keyword, "type": 1, "limit": 5, "offset": 0},
            headers=HEADERS,
        )
        response.raise_for_status()
        return parse_search_response(response.json())

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _send_song(self, runtime, event, song: Song) -> None:
        adapter = _adapter_for(runtime, event)
        if adapter is None:
            logger.debug("music：没有平台 %s 的适配器，发不出去", getattr(event, "platform", ""))
            return
        if getattr(adapter, "name", "") == "snowluma":
            # OneBot v11 音乐卡片：type=netease + 歌曲 id，由协议端渲染成音乐分享
            segment = Segment("music", {"type": self.config.source, "id": song.song_id})
            try:
                await adapter.send(event.chat_id, [segment], chat_type=event.chat_type)
                return
            except Exception as exc:  # noqa: BLE001 - 卡片发不出去就降级发文字
                logger.warning("music 音乐段发送失败（%s），降级发文字 + 封面", exc)
        await self._send_fallback(adapter, event, song)

    @staticmethod
    async def _send_fallback(adapter, event, song: Song) -> None:
        segments = [Segment.text(f"{song.summary()}\n{song.link}")]
        if song.cover_url:
            segments.append(Segment.image(song.cover_url))
        await adapter.send(event.chat_id, segments, chat_type=event.chat_type)

    async def _reply(self, runtime, event, text: str) -> None:
        adapter = _adapter_for(runtime, event)
        if adapter is None:
            return
        try:
            await adapter.send(event.chat_id, [Segment.text(text)], chat_type=event.chat_type)
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮主流程
            logger.warning("music 回复发送失败：%s", exc)


def _adapter_for(runtime, event):
    adapters = getattr(runtime, "adapters", None)
    if not isinstance(adapters, Mapping):
        return None
    return adapters.get(getattr(event, "platform", ""))


def _as_triggers(value: Any) -> tuple[str, ...] | None:
    """显式写成列表（哪怕空）则尊重配置；字段缺失返回 None 表示用默认。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(
        text for text in (str(item).strip() for item in value if item is not None) if text
    )


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def register(ctx) -> None:
    """插件入口：注册消息钩子与停用清理。"""
    player = MusicPlayer(load_config(ctx))
    ctx.extras["music"] = player
    ctx.on_message(player.on_message)
    ctx.on_stop(player.aclose)
    logger.debug(
        "music 已注册（冷却 %.0fs，音源 %s）",
        player.config.cooldown_seconds,
        player.config.source,
    )
