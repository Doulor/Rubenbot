"""语音双向能力层：入站语音条转写（STT）与出站语音合成（TTS）。

这一层只管两件事：把一段音频变成文字、把一段文字变成音频字节；什么时候
转写、什么时候发语音由 :mod:`rubenbot.core` 决定，各平台怎么发由适配器的
``message.voice`` 能力翻译。provider 可插拔，风格与 :mod:`rubenbot.search`
保持一致：没配凭据就什么都不做（``build_voice`` 返回 ``None``），不发起请求。

- STT：``siliconflow``（默认，SenseVoiceSmall）、``openai_compatible``
  （任意 OpenAI 兼容 base_url，给中转站留口），都是 multipart 上传、
  取响应里的 ``text``；``qq_native`` 不下载音频，改由 SnowLuma 适配器的
  ``fetch_ptt_text`` 动作取 QQ 官方转写。多个后端按 :attr:`VoiceConfig.stt_providers`
  的顺序顺延，全失败才留 ``[语音]`` 占位。
- TTS：``custom``（默认，任意 OpenAI 兼容 ``/audio/speech`` 接口）与
  ``siliconflow``（CosyVoice2，配了 key 才可用）。

所有网络失败都返回 ``None`` 并记日志，绝不向上抛——语音是锦上添花，
失败时上层照常发文字。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .config import VoiceConfig

logger = logging.getLogger(__name__)

SILICONFLOW_STT_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_TTS_URL = "https://api.siliconflow.cn/v1/audio/speech"
# siliconflow 的预置模型：配置里 tts_model 留空时用它（custom 后端不兜底）
SILICONFLOW_TTS_MODEL = "FunAudioLLM/CosyVoice2-0.5B"

# 转写文本进入正文时的固定前缀：让模型知道这是「说出来的话」
VOICE_PREFIX = "[语音] "
# 语音段在纯文本投影里的占位（与 adapters.base.SEGMENT_PLACEHOLDERS 一致）
VOICE_PLACEHOLDER = "[语音]"

_DEFAULT_MIME = "audio/amr"
# 常见音频扩展名 -> MIME；其余交给 mimetypes 兜底
_AUDIO_MIME = {
    ".amr": "audio/amr",
    ".silk": "audio/silk",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}

# 平台原生转写：用消息 id 换转写文本，失败时抛异常让调用方顺延
NativeTranscriber = Callable[[str], Awaitable[str | None]]
# 惰性取音频：只在轮到 HTTP 后端时调用，返回 ``(bytes, filename, mime)``
AudioLoader = Callable[[], Awaitable[tuple[bytes, str, str] | None]]


def build_voice(config: VoiceConfig) -> "VoiceService | None":
    """按配置造语音服务；转写与合成都不可用时返回 None。

    和搜索一样：能力不可用就不注册，不会等调用之后才炸。只打一行日志说明
    当前处于什么降级状态，不阻塞启动。
    """
    if not config.stt_configured:
        logger.info("语音转写未配置，语音消息只记 %s 占位", VOICE_PLACEHOLDER)
    elif config.tts_enabled and not config.tts_configured:
        logger.info("语音回复未配置（%s），将照常发文字", config.tts_provider)
    if not config.stt_configured and not config.tts_configured:
        return None
    return VoiceService(config)


def _guess_mime(filename: str, declared: str = "") -> str:
    """确定音频 MIME：调用方/服务器声明的 ``audio/*`` 优先，否则按扩展名猜。

    扩展名与声明冲突时以声明为准——例如 Telegram 的语音条文件名可能不带
    后缀，但 ``mime_type`` 明确是 ``audio/ogg``，不能被默认的 ``.amr`` 盖掉。
    """
    declared = (declared or "").split(";")[0].strip().lower()
    if declared.startswith("audio/"):
        return declared
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix in _AUDIO_MIME:
        return _AUDIO_MIME[suffix]
    guessed = mimetypes.guess_type(filename)[0] if filename else None
    if guessed and guessed.startswith("audio/"):
        return guessed
    return declared or _DEFAULT_MIME


def _audio_filename(mime: str, fallback: str = "voice.amr") -> str:
    """给上传文件起个带正确扩展名的名字（转写接口按扩展名认格式）。

    保留调用方给的 basename，只把扩展名换成本 MIME 对应的；没给名字就生成
    ``voice.<ext>``。扩展名不认识时原样用 fallback。
    """
    for suffix, value in _AUDIO_MIME.items():
        if value == mime:
            return (Path(fallback).stem if fallback else "voice") + suffix
    return fallback or "voice.amr"


class VoiceService:
    """STT / TTS 的实现集合；客户端惰性创建，任何一个 provider 失败都只降级。"""

    def __init__(
        self,
        config: VoiceConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def stt_configured(self) -> bool:
        return self.config.stt_configured

    @property
    def tts_configured(self) -> bool:
        return self.config.tts_configured

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=max(
                    self.config.stt_timeout_seconds, self.config.tts_timeout_seconds
                ),
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------
    # 音频来源
    # ------------------------------------------------------------------

    async def fetch_audio(self, data: Mapping[str, Any]) -> tuple[bytes, str, str] | None:
        """从消息段的 data 里取音频字节：base64 优先，其次 http url，最后本地文件。

        返回 ``(bytes, filename, mime)``；没有可用来源、读取失败或超过单条上限
        时返回 None（调用方保留 ``[语音]`` 占位）。
        """
        filename = str(data.get("filename") or data.get("name") or "")
        mime = str(data.get("mime_type") or data.get("content_type") or "")
        content: bytes | None = None

        raw = data.get("base64")
        if raw:
            value = str(raw)
            if value.startswith("data:"):
                header, _, value = value.partition(",")
                mime = mime or header[5:].split(";")[0]
            elif value.startswith("base64://"):
                value = value[len("base64://") :]
            try:
                content = base64.b64decode(value, validate=False)
            except Exception as exc:  # noqa: BLE001 - 坏数据只降级
                logger.debug("语音段 base64 无效：%s", exc)
                return None
        if content is None:
            url = str(data.get("url") or "")
            # OneBot 的 record 段有时把 base64 数据塞在 file 字段里
            file_value = str(data.get("file") or "")
            if file_value.startswith("base64://"):
                try:
                    content = base64.b64decode(file_value[len("base64://") :], validate=False)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("语音段 base64 无效：%s", exc)
                    return None
            elif url.startswith(("http://", "https://")):
                try:
                    client = await self._get_client()
                    response = await client.get(url)
                    response.raise_for_status()
                    content = response.content
                    mime = mime or response.headers.get("content-type", "").split(";")[0]
                    if not filename:
                        filename = Path(httpx.URL(url).path).name
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("下载语音失败（%s）：%s", url, exc)
                    return None
            else:
                path = file_value
                if path.startswith("file://"):
                    path = path[len("file://") :]
                candidate = Path(path) if path else None
                if candidate is None or not candidate.is_file():
                    return None
                try:
                    content = candidate.read_bytes()
                except OSError as exc:
                    logger.warning("读取语音文件失败（%s）：%s", candidate, exc)
                    return None
                filename = filename or candidate.name

        if not content:
            return None
        if len(content) > self.config.stt_max_bytes:
            logger.info(
                "语音超过单条上限（%s 字节 > %s 字节），跳过转写",
                len(content),
                self.config.stt_max_bytes,
            )
            return None
        mime = _guess_mime(filename, mime)
        return content, _audio_filename(mime, filename or "voice.amr"), mime

    # ------------------------------------------------------------------
    # STT
    # ------------------------------------------------------------------

    async def transcribe_voice(
        self,
        audio: bytes | str | Path | None = None,
        *,
        filename: str = "",
        mime: str = "",
        message_id: str | None = None,
        native: NativeTranscriber | None = None,
        audio_loader: AudioLoader | None = None,
    ) -> str | None:
        """把一段语音转成文字；失败/未配置/超限都返回 None，不抛异常。

        ``audio`` 可以是字节，也可以是本地文件路径（例如已经落到临时文件的
        语音条）；传 ``None`` 并给出 ``audio_loader`` 时，只有轮到 HTTP 后端
        才真正去下载音频——``qq_native`` 打头时能省掉一次下载。

        ``message_id`` + ``native`` 由调用方在来源平台支持原生转写
        （SnowLuma 的 ``fetch_ptt_text``）时提供：链上轮到 ``qq_native`` 就用
        消息 id 直接换文本；原生转写失败或返回空文本时照常顺延到下一个
        已配置的后端。
        """
        providers = self.config.stt_providers
        if not providers:
            return None
        content: bytes | None = None
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            filename = filename or path.name
            try:
                content = path.read_bytes()
            except OSError as exc:
                logger.warning("读取待转写音频失败（%s）：%s", path, exc)
                return None
        elif audio is not None:
            content = bytes(audio)
        if content is not None:
            if not content:
                content = None
            elif self._audio_too_large(content):
                return None
            else:
                mime = _guess_mime(filename, mime)
                filename = _audio_filename(mime, filename or "voice.amr")

        errors: list[str] = []
        for provider in providers:
            if provider == "qq_native":
                text, reason = await self._transcribe_native(native, message_id)
            else:
                if content is None and audio_loader is not None:
                    source = await self._load_audio(audio_loader)
                    if source is not None:
                        content, filename, mime = source
                if content is None:
                    text, reason = None, "没有可转写的音频"
                else:
                    text, reason = await self._transcribe_http(
                        provider, content, filename, mime
                    )
            if text:
                logger.info("语音转写成功（provider=%s）", provider)
                return text
            errors.append(f"{provider}: {reason}")
            logger.info("语音转写降级（%s）：%s", provider, reason)
        logger.warning("语音转写全部失败（%s）", "；".join(errors))
        return None

    async def _load_audio(
        self, audio_loader: AudioLoader
    ) -> tuple[bytes, str, str] | None:
        """惰性取音频（只在轮到 HTTP 后端时调用）；空/超限按失败处理。"""
        try:
            source = await audio_loader()
        except Exception as exc:  # noqa: BLE001 - 取音频失败只顺延
            logger.warning("读取语音条失败：%s", exc)
            return None
        if source is None:
            return None
        content, filename, mime = source
        if not content or self._audio_too_large(content):
            return None
        mime = _guess_mime(filename, mime)
        return bytes(content), _audio_filename(mime, filename or "voice.amr"), mime

    def _audio_too_large(self, content: bytes) -> bool:
        limit = self.config.stt_max_bytes
        if len(content) <= limit:
            return False
        logger.info(
            "语音超过单条上限（%s 字节 > %s 字节），跳过转写",
            len(content),
            limit,
        )
        return True

    async def _transcribe_native(
        self,
        native: NativeTranscriber | None,
        message_id: str | None,
    ) -> tuple[str | None, str]:
        """平台原生转写：来源不是 SnowLuma / 拿不到消息 id 时返回跳过原因。"""
        if native is None or not message_id:
            return None, "来源平台不支持原生转写（或缺少 message_id）"
        try:
            text = await native(str(message_id))
        except Exception as exc:  # noqa: BLE001 - 原生转写失败只顺延
            return None, f"原生转写失败：{exc}"
        value = str(text or "").strip()
        if not value:
            return None, "原生转写返回空文本"
        return value, ""

    async def _transcribe_http(
        self,
        provider: str,
        audio: bytes,
        filename: str,
        mime: str,
    ) -> tuple[str | None, str]:
        """HTTP 转写（siliconflow / openai_compatible）：返回 ``(文本, 失败原因)``。"""
        url, headers = self._stt_endpoint(provider)
        try:
            client = await self._get_client()
            response = await client.post(
                url,
                data={"model": self.config.stt_model},
                files={"file": (filename, audio, mime)},
                headers=headers,
                timeout=self.config.stt_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return None, f"HTTP 转写失败：{exc}"
        text = ""
        if isinstance(payload, Mapping):
            text = str(payload.get("text") or "").strip()
        if not text:
            return None, "HTTP 转写返回空文本"
        return text, ""

    def _stt_endpoint(self, provider: str) -> tuple[str, dict[str, str]]:
        """转写接口地址与鉴权头：siliconflow 固定地址，兼容模式用 base_url。"""
        headers: dict[str, str] = {}
        if self.config.stt_api_key:
            headers["Authorization"] = f"Bearer {self.config.stt_api_key}"
        if provider == "openai_compatible":
            base = self.config.stt_base_url.rstrip("/")
            return base + "/audio/transcriptions", headers
        return SILICONFLOW_STT_URL, headers

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> bytes | None:
        """把一段文字合成语音字节；失败/未配置/空文本都返回 None，不抛异常。"""
        text = (text or "").strip()
        if not text or not self.config.tts_configured:
            return None
        try:
            if self.config.tts_provider == "siliconflow":
                return await self._synthesize_siliconflow(text)
            return await self._synthesize_custom(text)
        except Exception as exc:  # noqa: BLE001 - 合成失败只降级发文字
            logger.warning("语音合成失败（%s）：%s", self.config.tts_provider, exc)
            return None

    async def _synthesize_custom(self, text: str) -> bytes | None:
        """自定义 TTS：OpenAI 兼容 ``POST {base_url}/audio/speech``，返回音频字节。"""
        base = self.config.tts_base_url.rstrip("/")
        headers: dict[str, str] = {}
        if self.config.tts_api_key:
            headers["Authorization"] = f"Bearer {self.config.tts_api_key}"
        client = await self._get_client()
        response = await client.post(
            base + "/audio/speech",
            json={
                "model": self.config.tts_model,
                "input": text,
                "voice": self.config.tts_voice,
            },
            headers=headers,
            timeout=self.config.tts_timeout_seconds,
        )
        response.raise_for_status()
        content = response.content
        if not content:
            logger.info("语音合成返回空音频（custom）")
            return None
        return content

    async def _synthesize_siliconflow(self, text: str) -> bytes | None:
        """SiliconFlow CosyVoice2：POST /v1/audio/speech，直接返回音频字节。"""
        key = self.config.tts_api_key or self.config.stt_api_key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        client = await self._get_client()
        response = await client.post(
            SILICONFLOW_TTS_URL,
            json={
                "model": self.config.tts_model or SILICONFLOW_TTS_MODEL,
                "input": text,
                "voice": self.config.tts_siliconflow_voice,
                "response_format": "mp3",
            },
            headers=headers,
            timeout=self.config.tts_timeout_seconds,
        )
        response.raise_for_status()
        content = response.content
        if not content:
            logger.info("语音合成返回空音频（siliconflow）")
            return None
        return content


__all__ = [
    "SILICONFLOW_STT_URL",
    "SILICONFLOW_TTS_URL",
    "SILICONFLOW_TTS_MODEL",
    "VOICE_PLACEHOLDER",
    "VOICE_PREFIX",
    "VoiceService",
    "build_voice",
]
