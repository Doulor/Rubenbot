"""第 19 项：触发消息本身是图时，首轮裁定等视觉描述落库。

回放四类现场：
- 描述在途（实测 5.5–13.4 秒）时首轮裁定等到描述，回复能答对图里内容；
- 等满上限仍无描述时按缺失裁定，`decision_log` 留痕为「等满超时」；
- 配图只在历史/上下文里（触发消息是文字）时不触发等待；
- `image_bytes` 拉取失败有限次重试；重试仍失败把失败阶段留进裁定日志。

全部用假 LLM 与缩小的等待上限，不联网、不跑满真实十几秒。
"""

import asyncio
import base64
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.core import RubenBot
from rubenbot.decision import MergedRound
from rubenbot.persona import Persona

NOW = 1_800_000_000.0
IMAGE = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
BANK_SMS = "中国银行短信通知：您尾号 1234 的账户收入 1 元"


@pytest.fixture
def bot(config, store):
    return RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "群友"))


def message(text, sender="甲", **kwargs):
    return MessageEvent("recording", "group", "g", sender, sender, text, **kwargs)


def picture_event(text="[图片]", sender="甲", **kwargs):
    return message(text, sender, segments=[Segment("image", {"base64": IMAGE})], **kwargs)


def make_round(events, *, elapsed=0.0):
    return MergedRound("recording", "g", "group", "甲", "甲", events,
                       first_at=time.time() - elapsed, hard=True)


def planner_text(bot):
    call, = [call for call in bot.llm.calls if call["task"] == "gen_stage1"]
    return "\n".join(part["content"] for part in call["messages"])


def audit(bot):
    return bot.store.list_decision_logs()[0]


def test_default_wait_covers_measured_p95(config):
    # 实测视觉描述 6.0 / 6.3 / 9.4 / 12.5 秒（单张到 13.4 秒）；默认上限须覆盖，
    # 不能回到旧版 5 秒硬预算。
    assert config.stickers.vision_wait_seconds >= 13.4


def test_triggered_image_waits_for_description_and_replies_content(bot, store):
    """4 秒后才裁定的触发图（4741 案）：等到描述，首轮就能说出图里内容。"""
    bot.sticker_behavior.config = replace(bot.config.stickers, vision_wait_seconds=1.0)
    release = asyncio.Event()
    started = asyncio.Event()
    original = bot.llm.complete

    async def delayed(messages, *, max_tokens=None, **kwargs):
        if kwargs.get("task") == "image_description":
            started.set()
            await release.wait()  # 模拟视觉模型 5.5–13.4 秒才返回
            return json.dumps({"description": BANK_SMS, "emotions": []}, ensure_ascii=False)
        return await original(messages, **kwargs)

    bot.llm.complete = delayed
    bot.llm.responses = {
        "fast": [json.dumps({"basis": "settled", "speak": True, "intent": "补充新信息",
                             "increment": "说出短信内容"}, ensure_ascii=False)],
        "chat": ["是中国银行的短信，说入账 1 元"],
    }
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    event = picture_event()

    async def scenario():
        await bot._record(event, NOW)
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.2)  # 图到后约 0.2 秒才轮到裁定（真实约 4 秒）
        task = asyncio.create_task(bot.process_round(make_round([event], elapsed=0.2)))
        await asyncio.sleep(0.2)
        assert not task.done()  # 裁定在等描述，没有按「缺描述」提前走
        release.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert "中国银行短信通知" in planner_text(bot)
    row = audit(bot)
    wait = row["factors"]["visual_wait"]
    assert wait["ready"] and wait["waited_ms"] >= 150
    assert adapter.texts == ["是中国银行的短信，说入账 1 元"]


def test_wait_upper_bound_still_marks_timeout(bot, store):
    """等满上限仍无描述：按缺失裁定，但不能与「压根没描述请求」混为一谈。"""
    bot.sticker_behavior.config = replace(bot.config.stickers, vision_wait_seconds=0.05)
    hang = asyncio.Event()
    original = bot.llm.complete

    async def delayed(messages, *, max_tokens=None, **kwargs):
        if kwargs.get("task") == "image_description":
            await hang.wait()
            return '{"description":"终于看清了"}'
        return await original(messages, **kwargs)

    bot.llm.complete = delayed
    event = picture_event()

    async def run():
        await bot._record(event, NOW)
        try:
            await asyncio.wait_for(bot.process_round(make_round([event])), timeout=2)
            wait = audit(bot)["factors"]["visual_wait"]
            assert wait["timed_out"] == [event.raw_message_id]
            assert wait["no_request"] == [] and wait["ready"] == []
        finally:
            hang.set()
            await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    assert "〔视觉描述缺失〕" in audit(bot)["text"]


def test_text_trigger_does_not_wait_for_historical_picture(bot, store, monkeypatch):
    """触发消息是文字、配图只在历史里：不触发等待，裁定时延不增加。"""
    history = picture_event("[图片]", sender="乙")
    await_marker = AsyncMock()
    monkeypatch.setattr(bot.sticker_behavior, "wait_for_visuals", await_marker, raising=False)

    async def run():
        await bot._record(history, NOW - 30)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)  # 历史图早已描述完
        event = message("今天好热")
        await bot._record(event, NOW)
        await bot.process_round(make_round([event]))

    asyncio.run(run())
    await_marker.assert_not_awaited()


def test_image_bytes_retries_then_succeeds(bot, store):
    """QQ 媒体字节偶发拉不下来：有限重试后成功，照常走描述流程。"""
    bot.sticker_behavior.config = replace(
        bot.config.stickers, vision_fetch_retries=1, vision_fetch_backoff_seconds=0,
    )
    calls = []

    async def flaky(url, limit):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("media download failed")
        return b"\x89PNG\r\n\x1a\nsecond-try"

    bot.sticker_behavior._downloader = flaky
    bot.llm.responses = {"vision": ['{"description":"第二次拉到的图"}']}
    event = message("", segments=[Segment("image", {"url": "https://example.invalid/a.png"})])

    async def run():
        await bot._record(event, NOW)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    assert len(calls) == 2
    assert store.raw_message_by_id(event.raw_message_id).visual_text == "第二次拉到的图"
    assert bot.sticker_behavior.visual_counts == dict(success=1, failed=0, timeout=0)


def test_image_bytes_failure_is_logged_with_stage(bot, store):
    """重试仍失败：裁定日志把失败阶段写清，不冒充「等满超时」。"""
    bot.sticker_behavior.config = replace(
        bot.config.stickers, vision_fetch_retries=1, vision_fetch_backoff_seconds=0,
        vision_wait_seconds=0.5,
    )
    calls = []

    async def broken(url, limit):
        calls.append(url)
        raise OSError("media download failed")

    bot.sticker_behavior._downloader = broken
    event = message("", segments=[Segment("image", {"url": "https://example.invalid/a.png"})])

    async def run():
        await bot._record(event, NOW)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)

    asyncio.run(run())
    assert len(calls) == 2  # 首次 + 1 次重试
    assert bot.sticker_behavior.visual_counts == dict(success=0, failed=1, timeout=0)
    wait = bot.sticker_behavior._visual_status[event.raw_message_id]
    assert wait == "image_bytes"


def test_visual_wait_report_marks_fetch_failure(bot, store):
    """拉取失败在裁定的 visual_wait 里记为 failed，而不是 timed_out/no_request。"""
    bot.sticker_behavior.config = replace(
        bot.config.stickers, vision_fetch_retries=0, vision_wait_seconds=0.5,
    )

    async def broken(url, limit):
        raise OSError("media download failed")

    bot.sticker_behavior._downloader = broken
    event = message("", segments=[Segment("image", {"url": "https://example.invalid/a.png"})])

    async def run():
        await bot._record(event, NOW)
        await asyncio.gather(*bot.sticker_behavior._bg_tasks)
        await bot.process_round(make_round([event]))

    asyncio.run(run())
    wait = audit(bot)["factors"]["visual_wait"]
    assert wait["failed"] == [{"raw_id": event.raw_message_id, "stage": "image_bytes"}]
    assert wait["timed_out"] == [] and wait["no_request"] == []


def test_visual_wait_report_marks_no_request(bot, store):
    """正文只有占位却没有视觉段（压根没描述请求）：单独记 no_request，不冒充超时。"""
    event = message("[图片]")  # 没有 image 段，入站就没有描述任务

    async def run():
        await bot._record(event, NOW)
        await bot.process_round(make_round([event]))

    asyncio.run(run())
    wait = audit(bot)["factors"]["visual_wait"]
    assert wait["no_request"] == [event.raw_message_id]
    assert wait["timed_out"] == [] and wait["failed"] == []
    assert audit(bot)["text"] == "[图片]"  # 没有消息段，图中内容从头到尾不可见
