"""点歌插件：群聊/私聊里「点歌 <歌名>」或「我想听 <歌名>」，搜网易云后发一首。

流程：识别触发词 → 单人冷却 → 网易云公开搜索接口（cloudsearch，免 key）
→ 取第一首 → 默认发「歌名 — 歌手 + 链接」文字并附一张封面图（人人可见）。

为什么不默认发 OneBot ``music`` 卡：2026-11-23 实测协议端回执 200，但新版 QQ
客户端不渲染这种卡，只显示「发送者版本过低，无法展示内容」——协议成功不等于
用户可见。卡片降为可选（``music_card: true``，且只在 SnowLuma 上尝试），
发不出去时照样降级成文字 + 封面。

失败一律降级成一句话，不吞请求、不抛异常打断对话：

- 搜索接口挂了/超时：回「搜不到，换个关键词试试」，日志里留下错误
- 搜索成功但没有结果：明确说没找到，不随便塞一首不相关的
- 音乐卡发送失败：降级成文字 + 封面；封面也发不出去：只发文字（歌名 + 链接）

配置在本目录的 ``config.yaml``（没有就读 ``config.example.yaml``，都没有
也能跑，缺省值在代码里）；嵌入式场景可用 ``ctx.extras["music_config"]``
传一份映射覆盖。
"""

from __future__ import annotations

import base64
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import yaml

from rubenbot.adapters.base import Segment
from rubenbot.expression import CapabilityNote

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
# 封面图本体上限：下载下来按 base64 内联发送，超过这个体积就不带图，只发文字
MAX_COVER_BYTES = 5 * 1024 * 1024
# 网易云原图动辄 3000×3000（近 1 MB），要 500 见方的缩略图就够群里看
COVER_THUMB_PARAM = "?param=500y500"
# 封面是锦上添花：下载最多等 5 秒，慢就退回交链接，不拖着点歌回复
COVER_TIMEOUT = 5.0

DEFAULT_TRIGGERS = ("点歌", "我想听")
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_SOURCE = "netease"
# 目前只接网易云；manifest 里留 source 方便以后加音源
SUPPORTED_SOURCES = ("netease",)
# 默认不发 OneBot 音乐卡：新版 QQ 只显示「发送者版本过低，无法展示内容」
DEFAULT_MUSIC_CARD = False

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
    # 默认 False：新版 QQ 不渲染 OneBot 音乐卡，只显示「发送者版本过低」
    music_card: bool = DEFAULT_MUSIC_CARD

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
            music_card=_as_bool(raw.get("music_card"), DEFAULT_MUSIC_CARD),
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
        self._verified_adapter: dict[tuple[str, str], Any] = {}

    def capability_note(self, event, adapter) -> CapabilityNote | None:
        route = (event.platform, event.chat_type)
        last = self._last_played_at.get((event.platform, event.sender_id))
        if (self._verified_adapter.get(route) is not adapter or not self.config.triggers
                or (last is not None and self.clock() - last < self.config.cooldown_seconds)):
            return None
        return CapabilityNote('点歌', '我能按歌名分享歌曲。',
                              f'有人想听一首歌时，可以让对方发「{self.config.triggers[0]} 歌名」，收到的是歌名、链接和封面图。')

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
        route = (event.platform, event.chat_type)
        self._verified_adapter.pop(route, None)

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

        adapter = _adapter_for(runtime, event)
        if adapter is not None:
            await self._send_song(adapter, event, song)
            self._verified_adapter[route] = adapter
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

    async def _send_song(self, adapter, event, song: Song) -> None:
        """默认发文字 + 封面；配置打开 ``music_card`` 时才先试 OneBot 音乐卡。

        卡片只是「协议回执成功」，接收端渲染与否无法验证（新版 QQ 会显示
        「发送者版本过低」），所以不做默认；卡片发不出去就照常走文字 + 封面。
        """
        if self.config.music_card and getattr(adapter, "name", "") == "snowluma":
            # OneBot v11 音乐卡片：type=netease + 歌曲 id，由协议端渲染成音乐分享
            segment = Segment("music", {"type": self.config.source, "id": song.song_id})
            try:
                await adapter.send(event.chat_id, [segment], chat_type=event.chat_type)
                return
            except Exception as exc:  # noqa: BLE001 - 卡片发不出去就降级发文字
                logger.warning("music 音乐段发送失败（%s），降级发文字 + 封面", exc)
        await self._send_fallback(adapter, event, song)

    async def _send_fallback(self, adapter, event, song: Song) -> None:
        """文字（歌名 + 链接）+ 封面图；封面发不出去就只发文字，别整条消息丢掉。"""
        text = Segment.text(f"{song.summary()}\n{song.link}")
        cover = await self._cover_segment(song)
        if cover is not None:
            try:
                await adapter.send(event.chat_id, [text, cover], chat_type=event.chat_type)
                return
            except Exception as exc:  # noqa: BLE001 - 封面挂了也要把歌名和链接发出去
                logger.warning("music 封面图发送失败（%s），改发纯文字", exc)
        await adapter.send(event.chat_id, [text], chat_type=event.chat_type)

    async def _cover_segment(self, song: Song) -> Segment | None:
        """封面优先下载成内联图片（协议端不必再出网抓图）。

        下载失败、返回的不是图片或体积超限时退回 URL 图源——最差也和以前
        一样把链接交出去，不因为封面问题丢掉文字。
        """
        if not song.cover_url:
            return None
        try:
            client = await self._get_client()
            async with client.stream(
                "GET", _cover_download_url(song.cover_url), timeout=COVER_TIMEOUT
            ) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_COVER_BYTES:
                        break
            if content_type.startswith("image/") and 0 < len(body) <= MAX_COVER_BYTES:
                return Segment(
                    "image",
                    {
                        "base64": base64.b64encode(bytes(body)).decode("ascii"),
                        "mime_type": content_type.split(";")[0].strip(),
                    },
                )
            logger.warning(
                "music 封面不是可用图片（%s，%d 字节），改发图片链接", content_type, len(body)
            )
        except Exception as exc:  # noqa: BLE001 - 封面下载失败不阻断点歌
            logger.warning("music 封面下载失败（%s），改发图片链接", exc)
        return Segment.image(song.cover_url)

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


def _cover_download_url(url: str) -> str:
    """网易云封面加缩略图参数；别的来源或已带查询串的原样下载。

    原图实测 3000×3000、近 1 MB，群里看 500 见方足够。
    """
    if "music.126.net" in url and "?" not in url:
        return url + COVER_THUMB_PARAM
    return url


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


def _as_bool(value: Any, default: bool) -> bool:
    """只认真布尔（与项目里其他配置口保持一致）。"""
    return value if isinstance(value, bool) else default


def register(ctx) -> None:
    """插件入口：注册消息钩子与停用清理。"""
    player = MusicPlayer(load_config(ctx))
    ctx.extras["music"] = player
    ctx.on_message(player.on_message)
    ctx.on_stop(player.aclose)
    ctx.capability_note(player.capability_note)
    logger.debug(
        "music 已注册（冷却 %.0fs，音源 %s，音乐卡 %s）",
        player.config.cooldown_seconds,
        player.config.source,
        "开" if player.config.music_card else "关",
    )
