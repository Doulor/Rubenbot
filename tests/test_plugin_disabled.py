"""插件未启用时，能力索引如实标注启用状态（第 24 项回放）。

现场：机主在群里点歌，music 插件没在 ``plugins.enabled`` 里，指令掉进普通
聊天被策划沉默，无从分辨是没开还是坏了。这里回放两遍：关着时被 @ 要歌必须
明说「还没开」，开着时照常由插件出歌（第 26 项后默认发文字 + 封面，不再默认发音乐卡）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx

from rubenbot.adapters.base import BaseAdapter, MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.expression import CapabilityNote, capability_index
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.plugins.loader import Plugin, PluginManager, load_manifest
from rubenbot.plugins.permissions import PluginPermissionStore

from conftest import ScriptedLLM, write_manifest


MUSIC_DIR = Path(__file__).resolve().parents[1] / "plugins" / "music"

SEARCH_HIT = {
    "result": {
        "songs": [
            {
                "id": 186016,
                "name": "晴天",
                "ar": [{"name": "周杰伦"}],
                "al": {"picUrl": "https://p1.music.126.net/cover.jpg"},
            }
        ]
    }
}


class SnowlumaRecorder(BaseAdapter):
    """名字是 snowluma 的假适配器：音乐段走正常发送，不发文字降级。"""

    name = "snowluma"

    def __init__(self):
        self.sent: list[tuple[str, str, list]] = []

    async def start(self, on_event):
        self.on_event = on_event

    async def stop(self):
        self.on_event = None

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.sent.append((chat_id, chat_type, list(segments)))

    @property
    def texts(self):
        return ["".join(segment.render() for segment in segments) for _, _, segments in self.sent]


def install_music(tmp_path: Path) -> Path:
    """把真实的 music 插件复制进临时插件目录（冷却设 0，便于连点）。"""
    root = tmp_path / "plugins"
    target = root / "music"
    target.mkdir(parents=True)
    for source in MUSIC_DIR.iterdir():
        if source.suffix in (".py", ".json") and source.is_file():
            shutil.copy(source, target / source.name)
    (target / "config.yaml").write_text("cooldown_seconds: 0\n", encoding="utf-8")
    return root


def make_bot(tmp_path: Path, *, enabled: list[str], fast: str, chat: str = "嗯"):
    config = Config()
    config.plugins.dir = str(install_music(tmp_path))
    config.plugins.enabled = list(enabled)
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    config.attention.talk_value = 1.0
    store = MemoryStore(":memory:")
    manager = PluginManager(config, store, permissions=PluginPermissionStore(None))
    manager.load_all()
    llm = ScriptedLLM({"fast": [fast], "chat": [chat]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = SnowlumaRecorder()
    bot.register_adapter(adapter)
    manager.extras["bot"] = bot
    return bot, manager, llm, adapter, store


def group_song_request() -> MessageEvent:
    return MessageEvent(
        platform="snowluma",
        chat_type="group",
        chat_id="1125257581",
        sender_id="u-owner",
        sender_name="多萝",
        text="点歌 晴天",
        mentions_bot=True,
    )


# ----------------------------------------------------------------------
# 渲染：能做的与没开的分开
# ----------------------------------------------------------------------


def test_index_keeps_unavailable_notes_out_of_ready_section():
    ready = CapabilityNote("搜网", "我能搜公开资料。", "拿不准就查。")
    off = CapabilityNote("点歌", "还没开", "有人要就说没开。", available=False)
    text = capability_index((ready, off))
    ready_part, off_part = text.split("【现在还做不了的】")
    assert "〔搜网〕我能搜公开资料。" in ready_part
    assert "点歌" not in ready_part
    assert off_part.startswith("点歌还没开。")
    assert "settled" in off_part and "speak 用 true" in off_part


def test_index_groups_unavailable_titles_by_state():
    notes = (
        CapabilityNote("点歌", "还没开", "有人要就说没开。", available=False),
        CapabilityNote("画图", "还没开", "有人要就说没开。", available=False),
        CapabilityNote("空间", "这会儿用不了", "有人要就说用不了。", available=False),
    )
    text = capability_index(notes)
    assert "【现在还做不了的】点歌、画图还没开。空间这会儿用不了。" in text
    assert text.count("【现在还做不了的】") == 1


def test_index_does_not_repeat_title_listed_as_ready():
    # 同一显示名确实能做时，不能同时又报成没开
    ready = CapabilityNote("点歌", "我能按歌名分享歌曲。", "想听时用。")
    off = CapabilityNote("点歌", "还没开", "有人要就说没开。", available=False)
    text = capability_index((ready, off))
    assert "【现在还做不了的】" not in text
    assert "〔点歌〕我能按歌名分享歌曲。" in text


def test_index_with_only_unavailable_notes_has_no_selection_hint():
    off = CapabilityNote("点歌", "还没开", "有人要就说没开。", available=False)
    text = capability_index((off,))
    assert "【这轮我能做的事】" not in text
    assert "capabilities 数组" not in text
    assert "【现在还做不了的】点歌还没开。" in text
    assert capability_index(()) == ""


# ----------------------------------------------------------------------
# loader：启用状态只认 loader 自己
# ----------------------------------------------------------------------


def _disabled_music_manager(tmp_path: Path) -> tuple[PluginManager, Path]:
    root = install_music(tmp_path)
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = []
    manager = PluginManager(config, MemoryStore(":memory:"), permissions=PluginPermissionStore(None))
    manager.load_all()
    return manager, root


def _question() -> MessageEvent:
    return MessageEvent(
        platform="snowluma", chat_type="group", chat_id="g1",
        sender_id="u1", sender_name="阿花", text="点歌 晴天",
    )


def test_disabled_plugin_is_reported_as_not_enabled_not_available(tmp_path):
    manager, _ = _disabled_music_manager(tmp_path)
    plugin = manager.plugins["music"]
    assert not plugin.enabled and not plugin.loaded
    notes = manager.capability_notes(_question(), SnowlumaRecorder())
    assert [(n.title, n.available) for n in notes] == [("点歌", False)]
    assert notes[0].summary == "还没开"


def test_invalid_manifest_is_not_claimed_by_directory_name(tmp_path):
    root = tmp_path / "plugins"
    write_manifest(root / "broken", "broken")
    (root / "broken" / "plugin.py").write_text("def register(ctx):\n    raise AssertionError('不该被加载')\n",
                                               encoding="utf-8")
    (root / "broken" / "manifest.json").write_text("{", encoding="utf-8")
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = []
    manager = PluginManager(config, MemoryStore(":memory:"), permissions=PluginPermissionStore(None))
    manager.load_all()
    assert manager.plugins["broken"].manifest is None
    assert manager.capability_notes(_question(), SnowlumaRecorder()) == []


def test_enabled_but_unloaded_plugin_is_not_called_not_enabled(tmp_path):
    """卸载残留：启用标记还在、代码已卸下，如实说用不了，不冒充「还没开」。"""
    _, root = _disabled_music_manager(tmp_path)
    plugin = Plugin(name="music", path=root / "music", enabled=True, loaded=False)
    plugin.manifest = load_manifest(root / "music")
    note = PluginManager._unavailable_note(plugin)
    assert note.available is False
    assert note.summary == "这会儿用不了"


def test_network_permission_missing_is_not_reported_as_available(tmp_path):
    """启用并加载了，但 network 未授权 → 钩子不分发，索引不能还说能做。"""
    root = install_music(tmp_path)
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["music"]
    manager = PluginManager(config, MemoryStore(":memory:"), permissions=PluginPermissionStore(None))
    manager.load_all()
    plugin = manager.plugins["music"]
    assert plugin.enabled and plugin.loaded
    # 首次发现会把已装插件按 manifest 全授；这里模拟机主撤掉 network
    manager.permissions.migrate({})
    manager.permissions.set_permissions("music", ["read_messages", "send_messages"])
    notes = manager.capability_notes(_question(), SnowlumaRecorder())
    assert [(n.title, n.summary, n.available) for n in notes] == [("点歌", "这会儿用不了", False)]
    # 授权后不再报「用不了」；music 自己按「验通过才列」决定什么时候给出可用说明
    manager.permissions.set_permissions("music", plugin.manifest.permissions)
    assert not [n for n in manager.capability_notes(_question(), SnowlumaRecorder()) if not n.available]


# ----------------------------------------------------------------------
# 回放一：music 没启用，群里 @bot 点歌 → 明说没开，不走插件
# ----------------------------------------------------------------------


def test_group_song_request_with_disabled_music_answers_honestly(tmp_path):
    plan = json.dumps(
        {"basis": "settled", "speak": True, "intent": "补充新信息", "increment": "说点歌还没开"},
        ensure_ascii=False,
    )
    reply = "点歌还没开，等机主打开就能听了"
    bot, manager, llm, adapter, store = make_bot(tmp_path, enabled=[], fast=plan, chat=reply)

    asyncio.run(bot.handle_event(group_song_request()))

    # 不该沉默：策划被叫到，回的是如实说明
    assert adapter.texts == [reply]
    log, = store.list_decision_logs()
    assert log["should_reply"] is True
    assert log["hard_trigger"] == "mentioned"
    # 插件没加载，连实例都不存在，不可能走插件调用
    assert not manager.plugins["music"].loaded
    assert "music" not in manager.extras
    assert [call["task"] for call in llm.calls] == ["gen_stage1", "gen_stage2"]
    assert all(segment["type"] != "music" for _, _, segments in adapter.sent for segment in segments)

    # 策划步拿到的索引把点歌放在「现在还做不了的」里，并给出照实说的依据与开口
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "【现在还做不了的】点歌还没开。" in prompt
    assert "〔点歌〕" not in prompt
    assert "basis 用 settled，speak 用 true" in prompt


# ----------------------------------------------------------------------
# 回放二：music 启用，群里点歌 → 照常由插件出歌（语音条 + 信息文字 + 封面）
# ----------------------------------------------------------------------


COVER_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 8


def music_net(request: httpx.Request) -> httpx.Response:
    """点歌插件的假网络：搜索、探活、封面各按 URL 分派。"""
    url = str(request.url)
    if "cloudsearch" in url:
        return httpx.Response(200, json=SEARCH_HIT)
    if "song/media/outer/url" in url:
        return httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg"})
    if "cover" in url or "music.126.net" in url:
        return httpx.Response(200, content=COVER_BYTES, headers={"content-type": "image/jpeg"})
    return httpx.Response(404, content=b"not found")


def test_group_song_request_with_enabled_music_sends_playable_record(tmp_path):
    plan = json.dumps({"basis": "settled", "speak": True, "intent": "短附和", "increment": "不用答"},
                      ensure_ascii=False)
    bot, manager, llm, adapter, _ = make_bot(tmp_path, enabled=["music"], fast=plan, chat="不该出现")
    client = httpx.AsyncClient(transport=httpx.MockTransport(music_net), follow_redirects=True)
    manager.extras["music"]._client = client
    try:
        asyncio.run(bot.handle_event(group_song_request()))
    finally:
        asyncio.run(client.aclose())

    # 第一段是能直接播放的语音条（音频直链交给协议端转码）
    assert adapter.sent[0] == ("1125257581", "group", [
        {"type": "record", "data": {"file": "http://music.163.com/song/media/outer/url?id=186016.mp3"}},
    ])
    # 随后补信息文字 + 封面（封面以内联图片发出，协议端不必再出网抓图）
    chat_id, chat_type, segments = adapter.sent[1]
    assert (chat_id, chat_type) == ("1125257581", "group")
    text = segments[0]["data"]["text"]
    assert "晴天 - 周杰伦" in text and "https://music.163.com/#/song?id=186016" in text
    assert segments[1]["type"] == "image" and segments[1]["data"]["mime_type"] == "image/jpeg"
    # 插件拦下了消息，策划一次都没跑；启用状态下也不会把它报成没开
    assert llm.calls == []
    notes = manager.capability_notes(group_song_request(), adapter)
    assert [(n.title, n.available) for n in notes] == [("点歌", True)]
