"""走真实摄入/抽取/合并/裁定管线；假模型只固定回放分支，不冒充线上拟人度评测。"""

import asyncio
import base64
import json
from pathlib import Path

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.core import RubenBot
from rubenbot.persona import load_persona


def event(text, sender="master", *, mentions=False, segments=()):
    return MessageEvent("recording", "group", "room", sender, sender, text,
                        mentions_bot=mentions, segments=list(segments))


def bot_for(config, store, llm):
    persona = load_persona(Path(__file__).resolve().parents[1] / "data/SOUL.md", examples_enabled=False)
    bot = RubenBot(config, store, llm, persona)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


@pytest.mark.parametrize("case,text,answer", [
    ("星驰", "星驰今晚关站", None),
    ("panda", "@Ruben 下载 YouTube 视频用哪个工具？", "用 yt-dlp，后面接视频链接"),
    ("混合", "@Ruben YouTube 视频怎么下载？星驰也能下吗？", "YouTube 用 yt-dlp 就行\n星驰我只见过名字"),
])
def test_familiarity_replays(config, store, case, text, answer):
    script = {"fast": [json.dumps({"speak": bool(answer), "intent": "补充新信息" if answer else "沉默",
                                   "increment": "说一个工具名" if answer else ""}, ensure_ascii=False)],
              "chat": [answer] if answer else [], "extract": ['{"entities":[{"name":"星驰"}]}']}
    llm = ScriptedLLM(script)
    bot, adapter = bot_for(config, store, llm)

    async def scenario():
        # 星驰此前仅作为图片配文见过一次；平台没有给它任何解释。
        if case != "panda":
            await bot._record(event("[图片]", segments=[Segment("image", {"description": "图上写着星驰"})]), 1)
            await bot.extractor.extract_sender(store.pending_senders()[0], now=2)
            assert store.search_raw_messages("星驰", platform="recording", chat_id="room")[0].text == "[图片]"
        llm.calls.clear()
        await bot.handle_event(event(text, "panda" if answer else "master", mentions=bool(answer)))

    asyncio.run(scenario())
    planner = llm.calls[0]["messages"][-1]["content"]
    assert text in planner
    assert ('"此前出现次数": 1, "有群内说明": false' in planner) == (case != "panda")
    if answer is None:
        assert adapter.texts == [] and [c["task"] for c in llm.calls] == ["gen_stage1"]
    else:
        assert [c["task"] for c in llm.calls] == ["gen_stage1", "gen_stage2"]
        assert "yt-dlp" in "".join(adapter.texts)
        stage2 = "\n".join(m["content"] for m in llm.calls[-1]["messages"])
        assert "此前出现次数" not in stage2
        if case == "混合":
            assert '"群内说明原文": null' in stage2 and "只见过名字" in "".join(adapter.texts)


def test_merged_round_keeps_first_label_and_only_generator_gets_quote(config, store):
    gloss = "群友共用的观星相册"
    llm = ScriptedLLM({"extract": [json.dumps({"entities": [{"name": "星驰", "gloss": gloss}]})],
                       "fast": ['{"speak":true,"intent":"补充新信息","increment":"说用途"}'], "chat": ["放观星照片的"]})
    bot, _ = bot_for(config, store, llm)

    async def scenario():
        await bot._record(event("星驰是" + gloss), 1)  # 旧到不在近期上下文中
        await bot.extractor.extract_sender(store.pending_senders()[0], now=2)
        bot.merger.window = bot.merger.hard_window = 30
        first = event("星驰", "panda")
        await bot.handle_event(first)
        assert first.familiarity[0].occurrence_count == 1
        await bot.handle_event(event("放什么的？", "panda", mentions=True))
        await bot.flush_merger(all_rounds=True)

    asyncio.run(scenario())
    stage1, stage2 = llm.calls[-2:]
    prompts = ["\n".join(m["content"] for m in call["messages"]) for call in (stage1, stage2)]
    assert '"此前出现次数": 1, "有群内说明": true' in prompts[0] and gloss not in prompts[0]
    assert gloss in prompts[1] and "此前出现次数" not in prompts[1]


def test_photo_perception_runs_after_ingest_without_collecting_photo(config, store):
    config.llm.models["vision"] = "test-vision"
    started, release = asyncio.Event(), asyncio.Event()

    class PausedVision(ScriptedLLM):
        async def complete(self, messages, *, max_tokens=None, **kwargs):
            if kwargs.get("role") == "vision":
                started.set()
                await release.wait()
            return await super().complete(messages, **kwargs)

    llm = PausedVision({"vision": ['{"description":"屏幕写着星驰"}'], "fast": ['{"speak":false,"intent":"沉默"}']})
    bot, _ = bot_for(config, store, llm)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"test").decode()

    async def scenario():
        handling = asyncio.create_task(bot.handle_event(event("", segments=[Segment("image", {"base64": encoded})])))
        await asyncio.wait_for(started.wait(), 2)
        assert not handling.done()  # 流水已入库，本轮裁定仍在预算内等待
        row = store.recent_messages("room")[0]
        assert row.text == "[图片]" and not row.visual_text
        release.set()
        await asyncio.wait_for(handling, 2)
        planner, = [c for c in llm.calls if c['task'] == 'gen_stage1']
        assert any('屏幕写着星驰' in m['content'] for m in planner['messages'])
        assert store.search_raw_messages("星驰", platform="recording", chat_id="room")[0].id == row.id
        assert store.raw_message_by_id(row.id).visual_text == "屏幕写着星驰"
        assert bot._stickers is None

    asyncio.run(scenario())
