"""模型测评回放：验证真实两阶段路径；不把模拟裁定当成线上模型评测。"""

import asyncio
from pathlib import Path

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.persona import Persona, load_persona


DATA = Path(__file__).resolve().parents[1] / "data"


def test_model_discussion_and_owner_mention(config, store):
    config.planner.identity_triggers = []  # 关闭旧的关键词注入，以免它兜底
    config.llm.models["chat"] = "offline-backend"
    config.llm.api_key = "test-key-not-for-the-planner"
    cfg_persona = load_persona(DATA / "SOUL.md", examples_enabled=False)
    llm = ScriptedLLM({"fast": ['{"speak":false,"intent":"沉默"}'] * 2, "chat": ["你又逗我"]})
    bot = RubenBot(config, store, llm, cfg_persona)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)

    def receive(text, mentioned=False):
        event = MessageEvent(platform="recording", chat_type="group", chat_id="g", sender_id="owner",
                             sender_name="多萝", text=text, mentions_bot=mentioned)
        asyncio.run(bot.handle_event(event))

    receive("Argon 说人话，Claude 做前端强")
    assert not adapter.sent
    stage1 = llm.calls[0]["messages"]
    assert cfg_persona.file_text("identity").strip() not in stage1[0]["content"]
    state = stage1[-1]["content"].split("【自身状态】")[1]
    assert all(s in state for s in ("我叫 Ruben", "赛博生命", "多萝把我部署", "本群的一名群友", "offline-backend"))
    assert "Argon" not in state and "Claude" not in state and config.llm.api_key not in state
    receive("小猪，这回听懂了没", mentioned=True)
    assert adapter.texts == ["你又逗我"]
    assert all("offline-backend" not in m["content"] for m in llm.chat_messages[0])


def test_custom_persona_selects_only_declared_anchor(config, store):
    p = Persona("霜", "我是霜。", parts={"identity": "# 身份\n## 常驻身份\n我在驿站。\n## 待人\n只在追问时讲。"})
    bot = RubenBot(config, store, ScriptedLLM(), p)
    event = MessageEvent(platform="x", chat_type="private", chat_id="1", sender_id="1", sender_name="人", text="在吗")
    state = bot._planner_self_state(event, [], 1)
    assert all(s in state for s in ("我叫 霜", "我在驿站。", "私聊的对话方"))
    assert "群友。" not in state and "只在追问时讲" not in state and "多萝" not in state
    assert Persona("旧人设", "旧人格").identity_anchor() == ""
