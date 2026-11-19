"""测试共享工具：假 LLM、假适配器。"""

from __future__ import annotations

import json
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

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages, "json_mode": json_mode})
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
    cfg.chat.split_delay_seconds = 0
    cfg.chat.bot_name = "Ruben"
    return cfg


def json_array(*items) -> str:
    return json.dumps(items, ensure_ascii=False)
