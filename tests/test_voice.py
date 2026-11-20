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

from rubenbot.adapters.base import MessageEvent, Segment
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
        self.synthesize_calls: list[str] = []

    async def fetch_audio(self, data):
        raw = data.get("base64")
        if not raw:
            return None
        return base64.b64decode(raw), "voice.amr", "audio/amr"

    async def transcribe_voice(self, audio, *, filename="", mime=""):
        self.transcribe_calls += 1
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
    config.chat.split_part_chars = 60
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "这是一句明显超过五个字的回复"))

    assert voice.synthesize_calls == []
    assert adapter.voice_calls == []
    assert adapter.texts == ["这是一句明显超过五个字的回复"]


def test_split_reply_stays_text(config, store):
    enable_tts(config, "reply_voice")
    config.chat.split_part_chars = 8
    voice = FakeVoice()
    adapter = VoiceAdapter()
    bot, _ = make_bot(config, store, adapter=adapter, voice=voice)
    asyncio.run(bot.send_reply(voice_event(), "第一句话。第二句话。"))

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
