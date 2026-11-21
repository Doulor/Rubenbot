"""语音双向：配置、STT/TTS 后端、模式矩阵、降级路径与适配器翻译。

音频转写/合成全部走假传输层或注入的假实现，不发真请求。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from rubenbot.adapters.base import CapabilityError, MessageEvent, Segment
from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.adapters.telegram import TelegramAdapter
from rubenbot.config import Config, VoiceConfig
from rubenbot.core import RubenBot
from rubenbot.persona import Persona
from rubenbot.voice import (
    SILICONFLOW_STT_URL,
    SILICONFLOW_TTS_URL,
    VOICE_PLACEHOLDER,
    VOICE_PREFIX,
    VoiceService,
    build_voice,
)

from conftest import RecordingAdapter, ScriptedLLM

STT_BODY = {"text": "  今天天气不错  "}
TTS_AUDIO = b"ID3-fake-mp3-bytes"


def make_service(handler, **overrides) -> VoiceService:
    """用 MockTransport 造语音服务；未写的字段沿用默认值。"""
    config = VoiceConfig(**{"stt_api_key": "k-stt", **overrides})
    return VoiceService(config, transport=httpx.MockTransport(handler))


def enable_tts(config, mode: str) -> None:
    """打开语音回复并配好 custom 后端（发请求由假语音服务兜住）。"""
    config.voice.tts_mode = mode
    config.voice.tts_provider = "custom"
    config.voice.tts_base_url = "https://tts.example/v1"
    config.voice.tts_model = "my-tts"


def audio_segment(data: bytes = b"amr-bytes", filename: str = "voice.amr") -> Segment:
    return Segment(
        "record",
        {
            "base64": base64.b64encode(data).decode("ascii"),
            "filename": filename,
            "mime_type": "audio/amr",
        },
    )


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_voice_config_defaults_and_fallbacks():
    config = VoiceConfig()
    assert config.stt_provider == "siliconflow"
    assert config.tts_mode == "off"
    assert config.tts_provider == "custom"
    assert config.stt_max_bytes == 5 * 1024 * 1024
    assert config.stt_timeout_seconds == 30.0
    assert config.tts_max_chars == 60
    assert config.stt_configured is False
    assert config.tts_enabled is False
    assert config.tts_configured is False
    # 写错的后端/模式回落默认值，不报错
    assert VoiceConfig(stt_provider="whisper").stt_provider == "siliconflow"
    assert VoiceConfig(tts_provider="edge_tts").tts_provider == "custom"
    assert VoiceConfig(tts_mode="always").tts_mode == "off"
    # 兼容模式看 base_url；siliconflow 的 TTS 可以复用 STT 的 key
    assert VoiceConfig(stt_provider="openai_compatible").stt_configured is False
    assert (
        VoiceConfig(
            stt_provider="openai_compatible", stt_base_url="https://relay.example/v1"
        ).stt_configured
        is True
    )
    assert VoiceConfig(tts_mode="reply_voice", tts_provider="siliconflow").tts_configured is False
    assert (
        VoiceConfig(
            tts_mode="reply_voice", tts_provider="siliconflow", stt_api_key="k"
        ).tts_configured
        is True
    )
    # custom TTS 要 base_url + model 才算配置好；缺一个都降级发文字
    assert VoiceConfig(tts_mode="reply_voice").tts_configured is False
    assert (
        VoiceConfig(
            tts_mode="reply_voice", tts_base_url="https://tts.example/v1"
        ).tts_configured
        is False
    )
    assert (
        VoiceConfig(
            tts_mode="reply_voice", tts_base_url="https://tts.example/v1", tts_model="m"
        ).tts_configured
        is True
    )


def test_stt_provider_chain_order():
    """首选后端在前，其余已配置的 HTTP 后端按固定顺序顺延。"""
    assert VoiceConfig(stt_api_key="k").stt_providers == ("siliconflow",)
    assert (
        VoiceConfig(
            stt_provider="openai_compatible",
            stt_api_key="k",
            stt_base_url="https://relay.example/v1",
        ).stt_providers
        == ("openai_compatible", "siliconflow")
    )
    assert (
        VoiceConfig(stt_provider="qq_native", stt_api_key="k").stt_providers
        == ("qq_native", "siliconflow")
    )
    # qq_native 不是通用后端：没被选为首选时不进链
    assert VoiceConfig(stt_api_key="k").stt_providers == ("siliconflow",)
    # 要选中 qq_native 但没配其他后端也能用（不需要 key）
    assert VoiceConfig(stt_provider="qq_native").stt_providers == ("qq_native",)
    assert VoiceConfig(stt_provider="qq_native").stt_configured is True
    assert VoiceConfig().stt_providers == ()


def test_config_loads_voice_section_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RUBEN_STT_KEY", "sk-stt-env")
    path = tmp_path / "config.yaml"
    path.write_text(
        "voice:\n"
        "  stt_provider: openai_compatible\n"
        "  stt_api_key: ${RUBEN_STT_KEY}\n"
        "  stt_base_url: https://relay.example/v1\n"
        "  tts_mode: on_request\n"
        "  tts_max_chars: 40\n"
        "  tts_timeout_seconds: 12\n",
        encoding="utf-8",
    )
    config = Config.load(path)
    assert config.voice.stt_provider == "openai_compatible"
    assert config.voice.stt_api_key == "sk-stt-env"
    assert config.voice.stt_base_url == "https://relay.example/v1"
    assert config.voice.tts_mode == "on_request"
    assert config.voice.tts_max_chars == 40
    assert config.voice.tts_timeout_seconds == 12.0


def test_build_voice_skips_when_unconfigured(caplog):
    with caplog.at_level(logging.INFO, logger="rubenbot.voice"):
        assert build_voice(VoiceConfig()) is None
        # 只开了 TTS、但 custom 没有 base_url/model：服务整体不可用，也只记一行日志
        assert build_voice(VoiceConfig(tts_mode="reply_voice")) is None
        service = build_voice(VoiceConfig(stt_api_key="k"))
    assert isinstance(service, VoiceService)
    assert any("语音转写未配置" in record.message for record in caplog.records)


# ----------------------------------------------------------------------
# STT 后端
# ----------------------------------------------------------------------


def test_siliconflow_transcribe_parses_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["ctype"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json=STT_BODY)

    service = make_service(handler)
    text = asyncio.run(service.transcribe_voice(b"amr-bytes", filename="voice.amr"))
    assert text == "今天天气不错"
    assert seen["url"] == SILICONFLOW_STT_URL
    assert seen["auth"] == "Bearer k-stt"
    assert seen["ctype"].startswith("multipart/form-data")
    assert b'name="model"' in seen["body"]
    assert b"FunAudioLLM/SenseVoiceSmall" in seen["body"]
    assert b'name="file"' in seen["body"]
    assert b"amr-bytes" in seen["body"]
    assert b'filename="voice.amr"' in seen["body"]


def test_openai_compatible_transcribe_uses_base_url():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "收到啦"})

    service = make_service(
        handler,
        stt_provider="openai_compatible",
        stt_base_url="https://relay.example/v1/",
        stt_model="whisper-1",
    )
    assert asyncio.run(service.transcribe_voice(b"mp3", filename="a.mp3")) == "收到啦"
    assert seen["url"] == "https://relay.example/v1/audio/transcriptions"
    assert b"whisper-1" in seen["body"]


def test_transcribe_failures_return_none():
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    assert asyncio.run(make_service(server_error).transcribe_voice(b"x")) is None
    assert asyncio.run(make_service(empty).transcribe_voice(b"x")) is None
    assert asyncio.run(make_service(not_json).transcribe_voice(b"x")) is None
    # 未配置时不发请求
    calls = []

    def spy(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=STT_BODY)

    service = VoiceService(VoiceConfig(), transport=httpx.MockTransport(spy))
    assert asyncio.run(service.transcribe_voice(b"x")) is None
    assert calls == []


def test_transcribe_oversize_skips_without_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=STT_BODY)

    service = make_service(handler, stt_max_bytes=4)
    assert asyncio.run(service.transcribe_voice(b"12345")) is None
    assert calls == []


def test_transcribe_reads_local_file(tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"local-audio")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"local-audio" in request.content
        return httpx.Response(200, json={"text": "文件里的声音"})

    service = make_service(handler)
    assert asyncio.run(service.transcribe_voice(source)) == "文件里的声音"


def test_qq_native_transcribes_by_message_id_without_download():
    """qq_native 打头：只用 message_id 换文本，不下载音频、不发 HTTP 请求。"""
    calls = []
    seen = []

    async def native(message_id):
        calls.append(message_id)
        return "  群里说的  "

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        seen.append(request)
        return httpx.Response(200, json={"text": "不该走这里"})

    service = make_service(handler, stt_provider="qq_native")
    text = asyncio.run(
        service.transcribe_voice(None, message_id="12345", native=native)
    )
    assert text == "群里说的"
    assert calls == ["12345"]
    assert seen == []


def test_qq_native_failure_falls_back_to_http_backend():
    """原生转写抛错或返回空文本：顺延到下一个已配置的 HTTP 后端。"""
    for behaviour in ("raise", "empty"):
        hit = []

        async def native(message_id, behaviour=behaviour):
            if behaviour == "raise":
                raise RuntimeError("fetch_ptt_text failed")
            return "   "

        def handler(request: httpx.Request) -> httpx.Response:
            hit.append(str(request.url))
            return httpx.Response(200, json={"text": "HTTP 转过来了"})

        service = make_service(handler, stt_provider="qq_native")
        text = asyncio.run(
            service.transcribe_voice(
                b"amr-bytes", filename="voice.amr", message_id="1", native=native
            )
        )
        assert text == "HTTP 转过来了"
        assert hit == [SILICONFLOW_STT_URL]


def test_qq_native_skipped_when_source_not_supported():
    """非 SnowLuma 来源（没有 native 口）：qq_native 直接跳过，走 HTTP 后端。"""
    hit = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit.append(str(request.url))
        return httpx.Response(200, json={"text": "来自 HTTP"})

    service = make_service(handler, stt_provider="qq_native")
    assert (
        asyncio.run(service.transcribe_voice(b"amr-bytes", filename="voice.amr", message_id="1"))
        == "来自 HTTP"
    )
    assert hit == [SILICONFLOW_STT_URL]


def test_qq_native_all_fail_returns_none():
    """全链失败（原生失败 + 唯一 HTTP 后端也没配好）：返回 None，上层留占位。"""
    async def native(message_id):
        raise RuntimeError("boom")

    service = VoiceService(
        VoiceConfig(stt_provider="qq_native"), transport=httpx.MockTransport(lambda r: None)
    )
    assert (
        asyncio.run(service.transcribe_voice(None, message_id="1", native=native)) is None
    )


def test_fetch_audio_sources(tmp_path):
    service = make_service(lambda request: httpx.Response(200, content=b"from-url"))

    async def scenario():
        from_b64 = await service.fetch_audio(
            {"base64": base64.b64encode(b"b64").decode(), "filename": "a.mp3"}
        )
        from_url = await service.fetch_audio({"url": "https://cdn.example/voice.ogg"})
        local = tmp_path / "b.silk"
        local.write_bytes(b"silk-bytes")
        from_file = await service.fetch_audio({"file": str(local)})
        missing = await service.fetch_audio({"file": str(tmp_path / "nope.amr")})
        return from_b64, from_url, from_file, missing

    from_b64, from_url, from_file, missing = asyncio.run(scenario())
    assert from_b64[0] == b"b64" and from_b64[2] == "audio/mpeg"
    assert from_url[0] == b"from-url" and from_url[1].endswith(".ogg")
    assert from_file[0] == b"silk-bytes" and from_file[2] == "audio/silk"
    assert missing is None


# ----------------------------------------------------------------------
# TTS 后端
# ----------------------------------------------------------------------


def test_custom_tts_posts_and_returns_audio():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=TTS_AUDIO)

    service = make_service(
        handler,
        tts_mode="reply_voice",
        tts_base_url="https://tts.example/v1/",
        tts_api_key="k-tts",
        tts_model="my-tts",
        tts_voice="alice",
    )
    assert asyncio.run(service.synthesize("你好呀")) == TTS_AUDIO
    assert seen["url"] == "https://tts.example/v1/audio/speech"
    assert seen["auth"] == "Bearer k-tts"
    assert seen["body"] == {"model": "my-tts", "input": "你好呀", "voice": "alice"}


def test_custom_tts_unconfigured_degrades_without_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=TTS_AUDIO)

    # 缺 model / 缺 base_url 都视为未配置：不发请求，返回 None 让上层发文字
    for overrides in (
        {"tts_mode": "reply_voice", "tts_base_url": "https://tts.example/v1"},
        {"tts_mode": "reply_voice", "tts_model": "my-tts"},
        {"tts_mode": "reply_voice"},
    ):
        service = make_service(handler, **overrides)
        assert asyncio.run(service.synthesize("你好")) is None
        assert asyncio.run(service.synthesize("   ")) is None
    assert calls == []


def test_custom_tts_http_failure_degrades():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    service = make_service(
        handler,
        tts_mode="reply_voice",
        tts_base_url="https://tts.example/v1",
        tts_model="my-tts",
    )
    assert asyncio.run(service.synthesize("你好")) is None


def test_siliconflow_tts_posts_and_returns_audio():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=TTS_AUDIO)

    service = make_service(
        handler,
        tts_mode="reply_voice",
        tts_provider="siliconflow",
    )
    assert asyncio.run(service.synthesize("你好呀")) == TTS_AUDIO
    assert seen["url"] == SILICONFLOW_TTS_URL
    # 没有单独的 TTS key 时复用 STT 的 key
    assert seen["auth"] == "Bearer k-stt"
    assert seen["body"]["input"] == "你好呀"
    # 硅基流动的 model 留空时用预置模型
    assert seen["body"]["model"] == "FunAudioLLM/CosyVoice2-0.5B"
    assert seen["body"]["voice"] == "FunAudioLLM/CosyVoice2-0.5B:alex"


def test_siliconflow_tts_failure_degrades():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    service = make_service(handler, tts_mode="reply_voice", tts_provider="siliconflow")
    assert asyncio.run(service.synthesize("你好")) is None
    assert asyncio.run(service.synthesize("   ")) is None


# ----------------------------------------------------------------------
# 主循环：模式矩阵与降级
# ----------------------------------------------------------------------


class FakeVoice:
    """记录调用、按预设返回的假语音服务（不碰网络）。"""

    def __init__(self, *, transcript="你好呀", audio=TTS_AUDIO):
        self.transcript = transcript
        self.audio = audio
        self.transcribe_calls = 0
        self.transcribe_kwargs: list[dict] = []
        self.load_calls = 0
        self.synthesize_calls: list[str] = []

    async def fetch_audio(self, data):
        raw = data.get("base64")
        if not raw:
            return None
        return base64.b64decode(raw), "voice.amr", "audio/amr"

    async def transcribe_voice(self, audio=None, *, filename="", mime="", **kwargs):
        self.transcribe_calls += 1
        self.transcribe_kwargs.append(kwargs)
        native = kwargs.get("native")
        if audio is None and native is not None:
            # 模拟 qq_native：只用 message_id 换文本；失败则顺延到音频来源
            try:
                text = await native(kwargs.get("message_id"))
                if text:
                    return text
            except Exception:  # noqa: BLE001 - 与真实实现一致：只顺延
                pass
        if audio is None and kwargs.get("audio_loader") is not None:
            self.load_calls += 1
            source = await kwargs["audio_loader"]()
            if source is None:
                return None
        return self.transcript

    async def synthesize(self, text):
        self.synthesize_calls.append(text)
        return self.audio

    async def close(self):
        pass


class VoiceAdapter(RecordingAdapter):
    """声明了 message.voice 的假适配器。"""

    name = "recording"
    capabilities = frozenset({"message.voice"})
    capability_config_keys = {"message.voice": "voice"}

    def __init__(self, *, fail: bool = False):
        super().__init__()
        self.voice_calls: list[dict] = []
        self.fail = fail

    async def call_capability(self, name, **kwargs):
        self.voice_calls.append({"name": name, **kwargs})
        if self.fail:
            raise RuntimeError("平台发送失败")
        return {"message_id": "v-1"}


def make_bot(config, store, *, adapter=None, voice=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, ScriptedLLM({"chat": ["收到啦"]}), Persona("Ruben", "你是 Ruben"))
    adapter = adapter or RecordingAdapter()
    bot.register_adapter(adapter)
    bot.voice = voice
    return bot, adapter


def voice_event(text="[语音]", *, sender="u1", segments=None, has_voice=True) -> MessageEvent:
    event = MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender,
        sender_id=sender,
        sender_name="阿花",
        text=text,
        segments=[Segment("record", {"base64": "QUJD"})] if segments is None else segments,
    )
    if has_voice:
        event.raw["has_voice"] = True
    return event


def text_event(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
        segments=[Segment.text(text)],
    )


def test_inbound_voice_transcribed_into_text_and_store(config, store):
    voice = FakeVoice(transcript="我明天去杭州")
    bot, _ = make_bot(config, store, voice=voice)
    asyncio.run(bot.handle_event(voice_event()))

    assert voice.transcribe_calls == 1
    messages = store.recent_messages("u1", platform="recording")
    # 第一条是用户的语音转写，正文带 [语音] 前缀
    assert messages[0].text.startswith(VOICE_PREFIX)
    assert "我明天去杭州" in messages[0].text


def test_inbound_voice_falls_back_to_placeholder(config, store):
    bot, _ = make_bot(config, store, voice=FakeVoice(transcript=None))
    event = voice_event()
    asyncio.run(bot.handle_event(event))
    assert event.text == VOICE_PLACEHOLDER
    messages = store.recent_messages("u1", platform="recording")
    # 纯语音条不会被当成空消息丢掉
    assert messages[0].text == VOICE_PLACEHOLDER


def test_inbound_voice_without_service_keeps_placeholder(config, store):
    """未配置 STT：不发任何请求，仍记 [语音] 占位，消息照常进流水。"""
    bot, _ = make_bot(config, store, voice=None)
    event = voice_event(text="[语音]")
    asyncio.run(bot.handle_event(event))
    assert store.recent_messages("u1", platform="recording")[0].text == VOICE_PLACEHOLDER


def test_inbound_voice_over_duration_limit_skips(config, store):
    """语音段带时长且超过上限：不转写，正文留 [语音] 占位。"""
    config.voice.stt_max_seconds = 60
    voice = FakeVoice()
    bot, _ = make_bot(config, store, voice=voice)
    event = MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text="[语音]",
        segments=[Segment("record", {"base64": "QUJD", "duration": 95})],
    )
    asyncio.run(bot.handle_event(event))
    assert voice.transcribe_calls == 0
    assert event.text == VOICE_PLACEHOLDER


def test_voice_reply_only_to_voice_message(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.handle_event(voice_event()))

    assert len(adapter.voice_calls) == 1
    assert adapter.voice_calls[0]["audio"] == TTS_AUDIO
    assert voice.synthesize_calls == ["收到啦"]
    # 语音发出后不再补发文字
    assert adapter.sent == []
    # bot 的消息照常写进流水（正文，供后续上下文）
    bot_messages = [m for m in store.recent_messages("u1", platform="recording") if m.sender_id.startswith("__")]
    assert bot_messages[0].text == "收到啦"
    assert bot_messages[0].platform_msg_id == "v-1"


def test_text_message_not_voiced_in_reply_voice_mode(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.handle_event(text_event()))

    assert adapter.voice_calls == []
    assert voice.synthesize_calls == []
    assert adapter.texts == ["收到啦"]


@pytest.mark.parametrize(
    "mode, has_voice, text, expected",
    [
        ("off", True, "[语音]", False),
        ("off", False, "用语音说一句", False),
        ("reply_voice", True, "[语音]", True),
        ("reply_voice", False, "用语音说一句", False),
        ("on_request", True, "[语音]", True),
        ("on_request", False, "用语音说一句", True),
        ("on_request", False, "我发了条语音你听", False),
        ("on_request", False, "今天好热", False),
    ],
)
def test_voice_mode_matrix(config, store, mode, has_voice, text, expected):
    enable_tts(config, mode)
    if mode == "on_request" and has_voice:
        text = "[语音]"
    bot, _ = make_bot(config, store)
    event = voice_event(text=text) if has_voice else text_event(text)
    if not has_voice:
        event.raw.pop("has_voice", None)
    assert bot.wants_voice_reply(event) is expected


def test_no_voice_service_never_synthesizes(config, store):
    """服务不可用（没配任何转写后端、custom TTS 也没配好）：不合成，照常发文字。"""
    enable_tts(config, "on_request")
    bot, _ = make_bot(config, store, voice=None)
    assert asyncio.run(bot.synthesize_reply(voice_event(), "好呀")) == (None, "")


def test_long_reply_stays_text(config, store):
    enable_tts(config, "reply_voice")
    config.voice.tts_max_chars = 5
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "这是一句明显超过五个字的回复"))

    assert voice.synthesize_calls == []
    assert adapter.voice_calls == []
    assert adapter.texts == ["这是一句明显超过五个字的回复"]


def test_split_reply_stays_text(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "第一句话。\n第二句话。"))

    # 被拆成多条：不合成语音，全部发文字（避免半句语音半句字）
    assert voice.synthesize_calls == []
    assert adapter.voice_calls == []
    assert len(adapter.texts) == 2


def test_adapter_without_voice_capability_sends_text(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice()
    adapter = RecordingAdapter()  # capabilities 为空
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "好呀"))

    assert voice.synthesize_calls == ["好呀"]
    assert adapter.texts == ["好呀"]


def test_synthesis_failure_sends_text(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice(audio=None)
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "好呀"))

    assert adapter.voice_calls == []
    assert adapter.texts == ["好呀"]


def test_platform_send_failure_sends_text(config, store):
    enable_tts(config, "reply_voice")
    voice = FakeVoice()
    adapter = VoiceAdapter(fail=True)
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "好呀"))

    assert len(adapter.voice_calls) == 1
    assert adapter.texts == ["好呀"]


def test_voice_capability_disabled_in_config_sends_text(config, store):
    enable_tts(config, "reply_voice")
    config.adapters["recording"] = {"capabilities": {"voice": False}}
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "好呀"))

    assert adapter.voice_calls == []
    assert adapter.texts == ["好呀"]


# ----------------------------------------------------------------------
# 适配器翻译：SnowLuma / Telegram
# ----------------------------------------------------------------------


def test_snowluma_voice_capability_sends_record_file():
    calls = []
    sent_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        sent_paths.append(payload["message"][0]["data"]["file"])
        return httpx.Response(200, json={"status": "ok", "data": {"message_id": 7}})

    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(handler)
    )
    adapter = SnowLumaAdapter(http_client=client)

    async def scenario():
        result = await adapter.call_capability(
            "message.voice", audio=b"amr-bytes", chat_id="55555", chat_type="group"
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    path, payload = calls[0]
    assert path == "/send_group_msg"
    assert payload["group_id"] == 55555
    assert payload["message"][0]["type"] == "record"
    assert sent_paths[0].endswith(".amr")
    # 临时文件发送后清理，不留垃圾
    assert not Path(sent_paths[0]).exists()
    assert result == {"message_id": 7}
    assert "message.voice" in SnowLumaAdapter.capabilities


def test_snowluma_voice_requires_audio():
    adapter = SnowLumaAdapter(
        http_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:3000",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
    )

    async def scenario():
        try:
            await adapter.call_capability("message.voice", audio=b"", chat_id="1")
        finally:
            await adapter._client.aclose()

    with pytest.raises(Exception, match="非空音频"):
        asyncio.run(scenario())


class FakeTelegramVoice:
    """记录请求并返回成功的假 Bot API（语音出站用）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.file_bytes = b"ogg-voice-bytes"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/bot"):
            return httpx.Response(200, content=self.file_bytes)
        method = path.rsplit("/", 1)[-1]
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            boundary = content_type.split("boundary=", 1)[1].strip('"')
            fields = {}
            for part in request.content.split(b"--" + boundary.encode()):
                if b"\r\n\r\n" not in part:
                    continue
                head, _, value = part.partition(b"\r\n\r\n")
                if value.endswith(b"\r\n"):
                    value = value[:-2]
                headers = head.decode("utf-8", "replace")
                if 'name="' not in headers:
                    continue
                name = headers.split('name="', 1)[1].split('"', 1)[0]
                if "filename=" in headers:
                    fields[name] = (
                        headers.split('filename="', 1)[1].split('"', 1)[0],
                        value,
                    )
                else:
                    fields[name] = value.decode()
            self.calls.append((method, fields))
        else:
            self.calls.append((method, json.loads(request.content or b"{}")))
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.ogg"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    def methods(self):
        return [name for name, _ in self.calls]


def make_telegram(fake) -> TelegramAdapter:
    client = httpx.AsyncClient(
        base_url="https://api.telegram.org", transport=httpx.MockTransport(fake.handler)
    )
    return TelegramAdapter(bot_token="123:T", offset_file=None, http_client=client)


def test_telegram_parse_voice_downloads_record_segment():
    fake = FakeTelegramVoice()
    adapter = make_telegram(fake)

    async def scenario():
        event = await adapter.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 111, "type": "private"},
                    "from": {"id": 555, "is_bot": False, "first_name": "阿花"},
                    "voice": {"file_id": "AF1", "duration": 3, "mime_type": "audio/ogg"},
                },
            }
        )
        await adapter._client.aclose()
        return event

    event = asyncio.run(scenario())
    assert event is not None
    assert event.text == "[语音]"
    segment = event.segments[0]
    assert segment.type == "record"
    assert base64.b64decode(segment.data["base64"]) == b"ogg-voice-bytes"
    assert segment.data["mime_type"] == "audio/ogg"
    assert "getFile" in fake.methods()


def test_telegram_send_record_segment_uses_send_voice():
    fake = FakeTelegramVoice()
    adapter = make_telegram(fake)

    async def scenario():
        await adapter.send(
            "111",
            [
                Segment(
                    "record",
                    {
                        "base64": base64.b64encode(b"mp3-bytes").decode(),
                        "mime_type": "audio/mpeg",
                    },
                )
            ],
            chat_type="private",
        )
        await adapter._client.aclose()

    asyncio.run(scenario())
    # 非 OGG 走 sendAudio，仍需带音频文件
    assert "sendAudio" in fake.methods()
    method, fields = [call for call in fake.calls if call[0] == "sendAudio"][0]
    assert fields["chat_id"] == "111"
    assert fields["audio"][1] == b"mp3-bytes"


def test_telegram_voice_capability_records_send_voice():
    fake = FakeTelegramVoice()
    adapter = make_telegram(fake)

    async def scenario():
        await adapter.call_capability(
            "message.voice", audio=b"ogg-bytes", mime="audio/ogg", chat_id="111", text="听我说"
        )
        await adapter._client.aclose()

    asyncio.run(scenario())
    method, fields = [call for call in fake.calls if call[0] == "sendVoice"][0]
    assert fields["caption"] == "听我说"
    assert fields["voice"][1] == b"ogg-bytes"


def test_telegram_voice_message_end_to_end(config, store):
    """Telegram 收到语音条 → 转写 → 语音回复：能力回执里的 message_id 落流水。"""
    fake = FakeTelegramVoice()
    adapter = make_telegram(fake)
    enable_tts(config, "reply_voice")
    config.voice.stt_api_key = "k"
    voice = FakeVoice(transcript="你好呀")
    bot = RubenBot(config, store, ScriptedLLM({"chat": ["在呢"]}), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(adapter)
    bot.voice = voice

    async def scenario():
        bot_id = adapter.bot_id
        adapter.bot_id = "999"
        event = await adapter.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 111, "type": "private"},
                    "from": {"id": 555, "is_bot": False, "first_name": "阿花"},
                    "voice": {"file_id": "AF1", "duration": 3, "mime_type": "audio/ogg"},
                },
            }
        )
        adapter.bot_id = bot_id
        await bot.handle_event(event)
        await adapter._client.aclose()

    asyncio.run(scenario())
    # 转写文本进了流水
    user_messages = [m for m in store.recent_messages("111", platform="telegram") if m.sender_id == "555"]
    assert user_messages[0].text == "[语音] 你好呀"
    # 回复走了 sendVoice（音频是 FakeVoice 给的 mp3，走 sendAudio）
    assert any(method in ("sendVoice", "sendAudio") for method, _ in fake.calls)
    bot_messages = [m for m in store.recent_messages("111", platform="telegram") if m.sender_id.startswith("__")]
    assert bot_messages[0].text == "在呢"
    # 假 Bot API 回的 message_id 已落库（sendAudio/sendVoice 都返回 9）
    assert bot_messages[0].platform_msg_id == "9"


def test_qq_official_does_not_declare_voice():
    from rubenbot.adapters.qq_official import QQOfficialAdapter

    assert "message.voice" not in QQOfficialAdapter.capabilities
    assert "message.transcribe" not in QQOfficialAdapter.capabilities


# ----------------------------------------------------------------------
# STT 原生转写：SnowLuma fetch_ptt_text 能力
# ----------------------------------------------------------------------


def make_snowluma(handler, **kwargs) -> SnowLumaAdapter:
    client = httpx.AsyncClient(
        base_url="http://127.0.0.1:3000", transport=httpx.MockTransport(handler)
    )
    return SnowLumaAdapter(http_client=client, **kwargs)


def snowluma_voice_event(message_id="777", text="[语音]") -> MessageEvent:
    return MessageEvent(
        platform="snowluma",
        chat_type="group",
        chat_id="55555",
        sender_id="10001",
        sender_name="阿花",
        text=text,
        segments=[Segment("record", {"file": "voice.amr", "base64": "QUJD"})],
        platform_msg_id=message_id,
    )


def test_snowluma_declares_transcribe_capability():
    assert "message.transcribe" in SnowLumaAdapter.capabilities
    assert SnowLumaAdapter.capability_config_keys["message.transcribe"] == "transcribe"


def test_snowluma_fetch_ptt_text_success_by_message_id():
    """SnowLuma 来源：直接用消息 id 调 fetch_ptt_text，不下载音频。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/fetch_ptt_text":
            return httpx.Response(200, json={"status": "ok", "data": {"text": "  明天见  "}})
        return httpx.Response(200, content=b"audio-bytes")

    async def scenario():
        adapter = make_snowluma(handler)
        try:
            result = await adapter.call_capability("message.transcribe", message_id="777")
        finally:
            await adapter._client.aclose()
        return result

    assert asyncio.run(scenario()) == {"text": "明天见"}
    assert calls == [("/fetch_ptt_text", {"message_id": 777})]


def test_snowluma_fetch_ptt_text_empty_raises():
    """上游线格式未经真机验证：空文本如实报错，由语音层顺延而不是编一个。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": {}})

    async def scenario():
        adapter = make_snowluma(handler)
        try:
            await adapter.call_capability("message.transcribe", message_id="777")
        finally:
            await adapter._client.aclose()

    with pytest.raises(CapabilityError, match="没有返回文本"):
        asyncio.run(scenario())


def test_snowluma_fetch_ptt_text_requires_message_id():
    async def scenario():
        adapter = make_snowluma(lambda request: httpx.Response(200, json={}))
        try:
            await adapter.call_capability("message.transcribe", message_id="")
        finally:
            await adapter._client.aclose()

    with pytest.raises(CapabilityError, match="message_id"):
        asyncio.run(scenario())


def test_prepare_voice_uses_native_transcribe_for_snowluma(config, store):
    """主循环接线：SnowLuma 来源打标后走适配器能力，转写结果进正文。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "data": {"text": "走官方转写"}})

    adapter = make_snowluma(handler)
    bot, _ = make_bot(config, store, adapter=adapter, voice=FakeVoice(transcript="不该用这个"))
    config.voice.stt_provider = "qq_native"
    event = snowluma_voice_event()

    async def scenario():
        await bot.handle_event(event)
        await adapter._client.aclose()

    asyncio.run(scenario())
    assert event.text == "[语音] 走官方转写"
    assert calls == ["/fetch_ptt_text"]
    messages = store.recent_messages("55555", platform="snowluma")
    assert messages[0].text == "[语音] 走官方转写"


def test_prepare_voice_falls_back_when_native_fails(config, store):
    """原生转写失败：顺延到已配置的 HTTP 后端，正文仍是转写文本。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, text="fetch_ptt_text unsupported")

    adapter = make_snowluma(handler)
    voice = FakeVoice(transcript="HTTP 后端转的")
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    config.voice.stt_provider = "qq_native"
    event = snowluma_voice_event()

    async def scenario():
        await bot.handle_event(event)
        await adapter._client.aclose()

    asyncio.run(scenario())
    assert event.text == "[语音] HTTP 后端转的"
    # 原生动作尝试过一次，随后走了 HTTP 后端（假服务的 audio_loader）
    assert calls == ["/fetch_ptt_text"]
    assert voice.load_calls == 1


def test_prepare_voice_skips_native_for_other_platforms(config, store):
    """来源不是 SnowLuma：不调原生能力，直接走 HTTP 后端。"""
    voice = FakeVoice(transcript="HTTP 转的")
    bot, adapter = make_bot(config, store, voice=voice)
    config.voice.stt_provider = "qq_native"
    event = voice_event()
    asyncio.run(bot.handle_event(event))
    assert event.text == "[语音] HTTP 转的"
    assert voice.load_calls == 1
    assert voice.transcribe_kwargs[0]["native"] is None


def test_prepare_voice_respects_transcribe_capability_switch(config, store):
    """配置里关掉原生转写开关：不再调能力，直接走 HTTP 后端。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "data": {"text": "不该被调用"}})

    adapter = make_snowluma(handler)
    bot, _ = make_bot(config, store, adapter=adapter, voice=FakeVoice(transcript="HTTP 转的"))
    config.voice.stt_provider = "qq_native"
    config.adapters["snowluma"] = {"capabilities": {"transcribe": False}}
    event = snowluma_voice_event()

    async def scenario():
        await bot.handle_event(event)
        await adapter._client.aclose()

    asyncio.run(scenario())
    assert event.text == "[语音] HTTP 转的"
    assert calls == []
