"""测试共享工具：假 LLM、假适配器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rubenbot.adapters.base import BaseAdapter, MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.memory.store import MemoryStore


class ScriptedLLM:
    """按角色返回预设回复的假 LLM。

    ``responses`` 形如 ``{"chat": [...], "extract": [...], "consolidate": [...]}``；
    某角色用完则返回 ``default``。
    """

    def __init__(self, responses: dict[str, list[str]] | None = None, default: str = "嗯嗯"):
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.default = default
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        self.calls.append({"role": role, "task": task, "messages": messages, "json_mode": json_mode})
        queue = self.responses.get(role)
        if queue:
            return queue.pop(0)
        return self.default

    @property
    def chat_messages(self) -> list[list[dict[str, str]]]:
        return [c["messages"] for c in self.calls if c["role"] == "chat"]


class RecordingAdapter(BaseAdapter):
    """记录发出的消息，可手动注入事件的假适配器。"""

    name = "recording"

    def __init__(self):
        self.sent: list[tuple[str, str, list[Segment]]] = []
        self.events: list[MessageEvent] = []
        self.on_event = None
        self.started = False
        self.stopped = False

    async def start(self, on_event):
        self.on_event = on_event
        self.started = True

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.sent.append((chat_id, chat_type, segments))

    async def stop(self):
        self.stopped = True

    @property
    def texts(self) -> list[str]:
        return ["".join(s.render() for s in segs) for _, _, segs in self.sent]


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def config() -> Config:
    cfg = Config()
    cfg.reply.segment_delay_enabled = False
    cfg.chat.bot_name = "Ruben"
    # 单测默认关掉合并窗口，让 handle_event 同步处理完
    cfg.decision.merge_window_seconds = 0
    cfg.decision.hard_merge_window_seconds = 0

    # 只测裁定本身，不让查看掷点随机跳过本用例。
    cfg.attention.talk_value = 1.0
    return cfg


@pytest.fixture
def check_inflate_budget(monkeypatch):
    """检查实际交给 zlib 的输出额度，避免先全量展开再报超限也能通过测试。"""
    import zlib

    inflate = zlib.decompressobj

    def check(limit):
        produced = []

        class Meter:
            def __init__(self, *args, **kwargs):
                self.inner = inflate(*args, **kwargs)

            def decompress(self, data, max_length=0):
                assert 0 < max_length <= limit + 1 - sum(produced)
                part = self.inner.decompress(data, max_length)
                produced.append(len(part))
                return part

            def __getattr__(self, name):
                return getattr(self.inner, name)

        monkeypatch.setattr(zlib, 'decompressobj', Meter)
        return produced

    return check


def json_array(*items) -> str:
    return json.dumps(items, ensure_ascii=False)


def write_manifest(directory, plugin_id: str | None = None, **overrides) -> Path:
    """在插件目录里写一份合规的最小 manifest（测试用）。

    loader 现在要求每个插件目录根部有 manifest.json，测试里临时造的插件
    目录统一用这个函数补上一份；不用关心的字段给默认值。
    """
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    name = plugin_id or path.name
    data = {
        "id": name,
        "name": overrides.pop("display_name", name),
        "version": overrides.pop("version", "1.0.0"),
        "description": overrides.pop("description", f"测试插件 {name}"),
        "author": overrides.pop("author", "test"),
        "min_core_version": overrides.pop("min_core_version", "0.0.0"),
        "permissions": overrides.pop("permissions", ["read_messages"]),
        "hooks": overrides.pop("hooks", []),
    }
    data.update(overrides)
    (path / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path / "manifest.json"
