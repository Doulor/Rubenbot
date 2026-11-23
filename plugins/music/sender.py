"""歌曲发送链：按 ``send_modes`` 有序尝试 card / record / file / text。

链序的取舍来自实测（2026-11-23/24，SnowLuma 2.1.1 + 新版 QQ）：

- ``record``（OneBot 语音段，``data.file`` 直接放音频 URL）协议端会自己下载、
  转码并发成语音条，群里点开就能听——这是默认链的第一档。实测 QQ 源的 m4a 与
  网易云可播曲的 mp3 都被完整转成语音条（SILK 帧数换算的时长与源一致）。
- ``file``（下载后 base64 上传）作为第二档，语音转码失败时仍能把音频送出去。
- ``text`` 永远兜底：歌名、歌手、时长、链接，保证最差也有可点的信息。
- ``card``（OneBot music 段）在新版 QQ 客户端只显示「发送者版本过低，无法
  展示内容」，协议端回执却是 200——按第 26 项的教训，默认**不放进链里**，
  只在配置显式写了 ``card`` 时尝试。

``record`` 只对 OneBot（SnowLuma）这类接受音频 URL 的适配器尝试；Telegram 等
平台要的是上传字节，交给 ``file`` 档处理，免得「[语音]」占位被当成发送成功。

音频发送成功后再补一条信息文字（歌名 / 歌手 / 时长 / 链接）并附封面图，
这是机主验收口径：能直接播放的音频 + 信息文字 + 封面。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from rubenbot.adapters.base import Segment

from .sources import Song

logger = logging.getLogger(__name__)

# 音频下载上限：一首歌 20 MiB 足够，超了退回文字（不把大文件塞进群）
MAX_AUDIO_BYTES = 20 * 1024 * 1024
AUDIO_TIMEOUT = 30.0
# 封面按 500 见方缩略图下载；超过 5 MiB 或不是图片就退回图片链接
MAX_COVER_BYTES = 5 * 1024 * 1024
COVER_TIMEOUT = 5.0
COVER_THUMB_PARAM = "?param=500y500"

SUPPORTED_MODES = ("card", "record", "file", "text")
# 接受「音频 URL 直接进 record 段」的适配器；其余平台走 file 上传
RECORD_URL_ADAPTERS = ("snowluma",)


@dataclass(frozen=True)
class SendOutcome:
    """一次发歌的结果：实际走通的模式，以及音频是否真的发出去了。"""

    mode: str
    audio_sent: bool


class MusicSender:
    """发歌与发歌词图；HTTP 客户端由播放器统一持有（便于测试注入）。"""

    def __init__(
        self,
        config: Any,
        get_client: Callable[[], Awaitable[httpx.AsyncClient]],
        renderer: Any = None,
    ):
        self.config = config
        self._get_client = get_client
        self.renderer = renderer

    # ------------------------------------------------------------------
    # 发歌
    # ------------------------------------------------------------------

    async def send_song(self, adapter, event, song: Song, source: Any) -> SendOutcome:
        """按配置链序发送；音频成功后补信息文字 + 封面，全部失败退回文字。"""
        audio_mode = ""
        for mode in self.config.send_modes:
            try:
                if mode == "card" and song.id and _adapter_name(adapter) in RECORD_URL_ADAPTERS:
                    await adapter.send(
                        event.chat_id,
                        [music_segment(song)],
                        chat_type=event.chat_type,
                    )
                    audio_mode = "card"
                    break
                if mode == "record" and song.audio_url:
                    if _adapter_name(adapter) not in RECORD_URL_ADAPTERS:
                        continue
                    await adapter.send(
                        event.chat_id,
                        [Segment("record", {"file": song.audio_url})],
                        chat_type=event.chat_type,
                    )
                    audio_mode = "record"
                    break
                if mode == "file" and song.audio_url:
                    data = await self.download_audio(song.audio_url)
                    if data:
                        await adapter.send_file(
                            event.chat_id,
                            audio_filename(song),
                            data,
                            chat_type=event.chat_type,
                        )
                        audio_mode = "file"
                        break
                    logger.warning("music 音频下载失败，改用下一档发送：%s", song.name)
                if mode == "text":
                    await self.send_text_info(adapter, event, song)
                    await self._send_extras(adapter, event, song, source)
                    return SendOutcome("text", False)
            except Exception as exc:  # noqa: BLE001 - 某一档失败就试下一档
                logger.warning("music %s 档发送失败（%s），尝试下一档", mode, exc)

        if not audio_mode:
            # 配置里没有任何能发音频的档：仍然要把歌名和链接发出去
            await self.send_text_info(adapter, event, song)
            await self._send_extras(adapter, event, song, source)
            return SendOutcome("text", False)

        # 音频已发出：补一条信息文字 + 封面（发不出去也不能把歌丢了）
        try:
            await self.send_text_info(adapter, event, song)
        except Exception as exc:  # noqa: BLE001 - 附加信息失败不影响已发出的歌
            logger.warning("music 信息文字发送失败：%s", exc)
        await self._send_extras(adapter, event, song, source)
        return SendOutcome(audio_mode, True)

    async def send_text_info(self, adapter, event, song: Song, *, note: str = "") -> None:
        """歌名 / 歌手 / 时长 / 链接，附封面图；封面发不出去就只发文字。"""
        segments = [Segment.text(text_info(song, note=note))]
        cover = await self.cover_segment(song)
        if cover is not None:
            try:
                await adapter.send(
                    event.chat_id, [*segments, cover], chat_type=event.chat_type
                )
                return
            except Exception as exc:  # noqa: BLE001 - 封面挂了也要把文字发出去
                logger.warning("music 封面发送失败（%s），改发纯文字", exc)
        await adapter.send(event.chat_id, segments, chat_type=event.chat_type)

    # ------------------------------------------------------------------
    # 热评 / 歌词
    # ------------------------------------------------------------------

    async def _send_extras(self, adapter, event, song: Song, source: Any) -> None:
        if getattr(self.config, "enable_comments", False):
            try:
                comment = await source.hot_comment(await self._get_client(), song)
                if comment:
                    await adapter.send(
                        event.chat_id,
                        [Segment.text(f"💬 热评：{comment}")],
                        chat_type=event.chat_type,
                    )
            except Exception as exc:  # noqa: BLE001 - 热评是锦上添花
                logger.warning("music 热评发送失败：%s", exc)
        if getattr(self.config, "enable_lyrics", False):
            await self.send_lyrics_image(adapter, event, song, source)

    async def send_lyrics_image(self, adapter, event, song: Song, source: Any) -> bool:
        """歌词渲染成图发送；渲染不可用或没有歌词时返回 False。"""
        if self.renderer is None or not getattr(self.renderer, "available", False):
            return False
        try:
            lyrics = song.lyrics or await source.lyrics(await self._get_client(), song)
            if not lyrics:
                return False
            image = self.renderer.render(lyrics)
            if not image:
                return False
            await adapter.send(
                event.chat_id,
                [
                    Segment(
                        "image",
                        {
                            "base64": base64.b64encode(image).decode("ascii"),
                            "mime_type": "image/png",
                        },
                    )
                ],
                chat_type=event.chat_type,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 歌词图失败不影响主流程
            logger.warning("music 歌词图发送失败：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    async def download_audio(self, url: str) -> bytes | None:
        if not url.lower().startswith(("http://", "https://")):
            return None
        try:
            client = await self._get_client()
            async with client.stream("GET", url, timeout=AUDIO_TIMEOUT) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_AUDIO_BYTES:
                        logger.warning(
                            "music 音频超过 %d MiB，放弃文件档", MAX_AUDIO_BYTES // 1048576
                        )
                        return None
            return bytes(body) if body else None
        except Exception as exc:  # noqa: BLE001 - 下载失败交给下一档
            logger.warning("music 音频下载失败（%s）：%s", url[:80], exc)
            return None

    async def cover_segment(self, song: Song) -> Segment | None:
        """封面优先下载成内联图片；失败或超限时退回图片链接。"""
        if not song.cover_url:
            return None
        try:
            client = await self._get_client()
            async with client.stream(
                "GET", cover_download_url(song.cover_url), timeout=COVER_TIMEOUT
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
                "music 封面不是可用图片（%s，%d 字节），改发图片链接",
                content_type,
                len(body),
            )
        except Exception as exc:  # noqa: BLE001 - 封面失败不影响点歌
            logger.warning("music 封面下载失败（%s），改发图片链接", exc)
        return Segment.image(song.cover_url)


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


def text_info(song: Song, *, note: str = "") -> str:
    lines = [f"🎵 {song.name} - {song.artist_text}"]
    if song.duration_text:
        lines.append(f"⏱️ 时长：{song.duration_text}")
    link = song.link or song.audio_url
    if link:
        lines.append(f"🔗 {link}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def music_segment(song: Song) -> Segment:
    """OneBot music 段：网易云 / QQ 用平台卡；其余平台用 custom 卡兜底。

    平台卡的 ``type`` 按 OneBot v11 的取值写：网易云是 ``163``（不是
    ``netease``），QQ 音乐是 ``qq``。
    """
    platform = (song.platform_type or song.source or "").lower()
    if platform in ("netease", "163"):
        return Segment("music", {"type": "163", "id": song.id})
    if platform == "qq":
        return Segment("music", {"type": "qq", "id": song.id})
    data: dict[str, Any] = {
        "type": "custom",
        "url": song.link or song.audio_url,
        "audio": song.audio_url,
        "title": song.name,
        "content": song.artist_text,
    }
    if song.cover_url:
        data["image"] = song.cover_url
    return Segment("music", data)


def audio_filename(song: Song) -> str:
    suffix = ".m4a" if ".m4a" in song.audio_url.lower() else ".mp3"
    safe = "".join(ch for ch in song.name if ch not in '\\/:*?"<>|').strip() or "song"
    return f"{safe}{suffix}"


def cover_download_url(url: str) -> str:
    """网易云封面加缩略图参数；别的来源或已带查询串的原样下载。"""
    if "music.126.net" in url and "param=" not in url:
        return url + ("&" if "?" in url else COVER_THUMB_PARAM)
    return url


def parse_send_modes(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """归一化 send_modes：接受中英文、带括号说明的写法，未知模式忽略。"""
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return default
    mapping = {
        "card": "card",
        "卡片": "card",
        "record": "record",
        "语音": "record",
        "file": "file",
        "文件": "file",
        "text": "text",
        "文本": "text",
    }
    modes: list[str] = []
    for item in value:
        key = str(item or "").split("(", 1)[0].split("（", 1)[0].strip().lower()
        mode = mapping.get(key)
        if mode and mode not in modes:
            modes.append(mode)
    return tuple(modes) or default


def _adapter_name(adapter) -> str:
    return str(getattr(adapter, "name", "") or "")
