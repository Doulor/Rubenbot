"""发送节奏、多段拆分与防抖上限（借鉴 MaiBot 研读 #2/#3 的最小版）。"""

import asyncio

import pytest

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.core import (
    REPLY_SEGMENT_CONTRACT,
    RubenBot,
    strip_action_parens,
    clean_reply_patterns,
)
from rubenbot.decision import MessageMerger
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


def make_bot(config, store, llm=None, *, rng=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(
        config,
        store,
        llm or ScriptedLLM(),
        Persona(name="Ruben", text="你是 Ruben。"),
        rng=rng,
    )
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def group_event(text="你好呀", sender="u1", name="阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender,
        sender_name=name,
        text=text,
    )


# ----------------------------------------------------------------------
# 段间打字延迟（按下一段字数估算）
# ----------------------------------------------------------------------


def test_segment_delay_by_chars_with_bounds(config, store):
    bot, _ = make_bot(config, store)
    config.reply.segment_delay_enabled = True
    config.reply.segment_delay_per_char_ms = 60
    config.reply.segment_delay_min_ms = 100
    config.reply.segment_delay_max_ms = 1000
    # 3 个非空白字 ×60ms，空白不计
    assert bot.segment_delay([Segment.text(" 好的 呀 ")]) == pytest.approx(0.18)
    # 一个字太短：抬到下限
    assert bot.segment_delay([Segment.text("嗯")]) == pytest.approx(0.1)
    # 长文截到上限
    assert bot.segment_delay([Segment.text("字" * 100)]) == pytest.approx(1.0)
    # 纯图片段按一次固定停顿（下限）
    assert bot.segment_delay([Segment.image("http://x/1.png")]) == pytest.approx(0.1)


def test_segment_delay_can_be_disabled(config, store):
    bot, _ = make_bot(config, store)
    config.reply.segment_delay_per_char_ms = 60
    config.reply.segment_delay_enabled = False
    assert bot.segment_delay([Segment.text("好的呀")]) == 0.0
    config.reply.segment_delay_enabled = True
    config.reply.segment_delay_per_char_ms = 0
    assert bot.segment_delay([Segment.text("好的呀")]) == 0.0


def test_send_reply_waits_between_parts(config, store, monkeypatch):
    """第一段立即发，后续段之间按下一段字数等待。"""
    config.reply.max_segments = 3
    config.reply.segment_delay_enabled = True
    config.reply.segment_delay_per_char_ms = 100
    config.reply.segment_delay_min_ms = 100
    config.reply.segment_delay_max_ms = 5000
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "第一段\n第二段\n第三段"))
    assert len(adapter.sent) == 3
    # 每条之间正好一次停顿，且按下一段长度
    assert sleeps == pytest.approx([0.3, 0.3])


def test_send_reply_no_sleep_when_delay_disabled(config, store, monkeypatch):
    config.reply.segment_delay_enabled = False
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "第一段\n第二段"))
    assert len(adapter.sent) == 2
    assert sleeps == []


# ----------------------------------------------------------------------
# 断句保护（详细覆盖见 test_reply_segments.py）
# ----------------------------------------------------------------------


def test_short_reply_not_split_even_with_breaks(config, store):
    bot, _ = make_bot(config, store)
    assert bot.split_reply("好。走。") == ["好。走。"]


def test_generation_contract_mentions_segments(config, store):
    """生成端契约：一次回复 1~3 段，用换行分隔（代码侧，不改人设文件）。"""
    assert "1~3 段" in REPLY_SEGMENT_CONTRACT and "换行" in REPLY_SEGMENT_CONTRACT


def test_send_reply_strips_transcript_prefix(config, store):
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "[10:00] [我] 全局记忆这块你真搞出来了？"))
    assert adapter.texts == ["全局记忆这块你真搞出来了？"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("**重点**内容", "重点内容"),
        ("# 标题\n正文", "标题\n正文"),
        ("1. 第一条\n2、第二条", "第一条\n第二条"),
        ("（小声）先说。\n\n再说。", "先说。\n再说。"),
        ("正常（补充）标点", "正常（补充）标点"),
    ],
)
def test_clean_reply_patterns_only_removes_high_confidence_formats(text, expected):
    assert clean_reply_patterns(text) == expected


def test_send_reply_cleans_patterns_before_splitting(config, store):
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "**第一句。**\n\n第二句。"))
    assert "**" not in "".join(adapter.texts)
    assert "\n\n" not in "".join(adapter.texts)


def test_send_reply_only_prefix_not_sent(config, store):
    """剥完为空则整条不发，也不记流水。"""
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "[10:00] [我] "))
    assert adapter.texts == []
    assert store.recent_messages("g1", platform="recording") == []


# ----------------------------------------------------------------------
# 括号动作清理
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("我回来啦（笑）", "我回来啦"),
        ("（点头）知道了", "知道了"),
        ("好呀（小声）", "好呀"),
        # 颜文字保护
        ("好耶(≧▽≦)", "好耶(≧▽≦)"),
        ("开心（＾ω＾）", "开心（＾ω＾）"),
        # 含数字/英文内容的正常括号不删
        ("版本（v2.0）上线了", "版本（v2.0）上线了"),
        # 拿不准的长短语不删
        ("（这件事情有点复杂）算了", "（这件事情有点复杂）算了"),
        ("（真的吗）", "（真的吗）"),
        # 含标点的括号不删
        ("（笑。）", "（笑。）"),
        # 语气词删除，语义短语保留
        ("（哈哈）好", "好"),
        ("（妙啊）", "（妙啊）"),
    ],
)
def test_strip_action_parens(text, expected):
    assert strip_action_parens(text) == expected


def test_strip_action_parens_disabled_by_default(config, store):
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["我回来啦（笑）"]}))
    assert bot.config.chat.strip_action_parens is False
    asyncio.run(bot.send_reply(group_event(), "我回来啦（笑）"))
    assert adapter.texts == ["我回来啦（笑）"]


def test_strip_action_parens_enabled(config, store):
    config.chat.strip_action_parens = True
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "我回来啦（笑），好开心(≧▽≦)"))
    assert adapter.texts == ["我回来啦，好开心(≧▽≦)"]


def test_strip_action_parens_segments(config, store):
    config.chat.strip_action_parens = True
    bot, adapter = make_bot(config, store)
    segments = [Segment.text("（点头）"), Segment.text("在的"), Segment.text("（笑）")]
    asyncio.run(bot.send_reply(group_event(), segments))
    assert adapter.texts == ["在的"]


def test_strip_action_parens_only_actions_keeps_reply(config, store):
    """整条都是动作描写时清空，不发空消息。"""
    config.chat.strip_action_parens = True
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "（点头）"))
    assert adapter.sent == []


# ----------------------------------------------------------------------
# 防抖整批上限 / @ 上限 / 到期不续命
# ----------------------------------------------------------------------


def test_batch_max_caps_continuous_extension():
    merger = MessageMerger(window=4.0, hard_window=1.0, batch_max=10.0, urgent=2.0)
    assert merger.add(group_event("一"), 100.0) == []
    assert merger.add(group_event("二"), 103.0) == []
    assert merger.add(group_event("三"), 106.0) == []
    assert merger.add(group_event("四"), 109.0) == []
    # 一直续命也最多等到 110
    assert merger.next_deadline() == pytest.approx(110.0)
    assert merger.pop_ready(109.9) == []
    rounds = merger.pop_ready(110.0)
    assert len(rounds) == 1
    assert len(rounds[0].events) == 4


def test_batch_max_zero_means_no_cap():
    merger = MessageMerger(window=4.0, batch_max=0.0, urgent=2.0)
    merger.add(group_event("一"), 100.0)
    merger.add(group_event("二"), 109.0)
    # 没有整批上限时照常按窗口续命
    assert merger.next_deadline() == pytest.approx(113.0)


def test_urgent_mention_caps_batch_deadline():
    merger = MessageMerger(window=4.0, hard_window=1.0, batch_max=10.0, urgent=2.0)
    merger.add(group_event("我先说两句"), 100.0)
    merger.add(group_event("然后再问你"), 101.0)
    # @bot：硬窗口本应到 102（101+1），urgent 把截止时间压到首条 + 2s = 102
    assert merger.add(group_event("@Ruben 在吗"), 101.0, hard=True, urgent=True) == []
    assert merger.next_deadline() == pytest.approx(102.0)
    rounds = merger.pop_ready(102.0)
    assert len(rounds) == 1
    assert rounds[0].text == "我先说两句\n然后再问你\n@Ruben 在吗"
    assert len(merger) == 0


def test_urgent_prefers_earlier_of_window_and_cap():
    merger = MessageMerger(window=4.0, hard_window=3.0, batch_max=10.0, urgent=5.0)
    merger.add(group_event("一"), 100.0)
    # 普通窗口 4s 比 urgent 5s 更早，截止时间仍是 104
    merger.add(group_event("@Ruben"), 100.5, hard=True, urgent=True)
    assert merger.next_deadline() == pytest.approx(103.5)


def test_urgent_cap_sticks_after_mention():
    """出现过 @ 后，后续普通消息不能把截止时间往 @ 上限之后拖。"""
    merger = MessageMerger(window=4.0, hard_window=1.0, batch_max=10.0, urgent=2.0)
    merger.add(group_event("@Ruben 你看"), 100.0, hard=True, urgent=True)
    # 硬窗口 1s 比 @ 上限 2s 更早
    assert merger.next_deadline() == pytest.approx(101.0)
    # 后续普通消息仍走硬触发短窗口（100.5+1s），且不超过 @ 上限（首条 + 2s = 102）
    merger.add(group_event("这个咋弄"), 100.5)
    assert merger.next_deadline() == pytest.approx(101.5)
    # 到点仍未续命：下一条消息到达时先放出旧批次
    ready = merger.add(group_event("还在吗"), 101.5)
    assert [r.text for r in ready] == ["@Ruben 你看\n这个咋弄"]


def test_urgent_cap_does_not_extend_earlier_deadline():
    merger = MessageMerger(window=4.0, hard_window=1.0, batch_max=10.0, urgent=2.0)
    merger.add(group_event("一"), 100.0)
    merger.add(group_event("二"), 103.0)
    # @ 的消息本身硬窗口是 1s，本应 104；urgent 把它压到 102，已经来不及，直接放出
    merger.add(group_event("@Ruben"), 103.0, hard=True, urgent=True)
    assert merger.next_deadline() is None


def test_urgent_zero_disables_cap():
    merger = MessageMerger(window=4.0, hard_window=1.0, batch_max=10.0, urgent=0.0)
    merger.add(group_event("普通"), 100.0)
    assert merger.add(group_event("@Ruben"), 100.5, hard=True, urgent=False) == []
    assert merger.next_deadline() == pytest.approx(101.5)


def test_expired_batch_not_extended():
    """到期封口后不再续命：旧批次先放出，新消息进新批次。"""
    merger = MessageMerger(window=4.0, batch_max=10.0, urgent=2.0)
    assert merger.add(group_event("旧批次"), 100.0) == []
    assert merger.next_deadline() == pytest.approx(104.0)
    # 到期后同人再发：旧批次立刻放出，新消息另起一批
    ready = merger.add(group_event("新批次"), 105.0)
    assert len(ready) == 1
    assert ready[0].text == "旧批次"
    assert [r.text for r in merger.pending] == ["新批次"]
    assert merger.next_deadline() == pytest.approx(109.0)


def test_expired_batch_released_even_if_consumer_late():
    """消费方来晚了也没关系：到期批次在 add 时一并放出，不和新消息混在一起。"""
    merger = MessageMerger(window=4.0, batch_max=10.0)
    merger.add(group_event("一"), 100.0)
    ready = merger.add(group_event("二"), 120.0)
    assert [r.text for r in ready] == ["一"]
    assert [r.text for r in merger.pop_ready(124.0)] == ["二"]


def test_hard_window_still_immediate_when_zero():
    merger = MessageMerger(window=4.0, hard_window=0.0, batch_max=10.0, urgent=2.0)
    ready = merger.add(group_event("@Ruben"), 100.0, hard=True, urgent=True)
    assert len(ready) == 1


# ----------------------------------------------------------------------
# 主循环接线：@ 的消息要带着 urgent 标记进合并窗口
# ----------------------------------------------------------------------


def test_handle_event_marks_mentions_urgent(config, store):
    config.decision.merge_window_seconds = 5.0
    config.decision.hard_merge_window_seconds = 5.0
    config.decision.batch_max_seconds = 10.0
    config.decision.urgent_seconds = 2.0
    bot, _ = make_bot(config, store)

    event = group_event("@Ruben 在吗", sender="u1")
    event.mentions_bot = True
    asyncio.run(bot.handle_event(event))
    round_ = bot.merger.pending[0]
    assert round_.urgent is True
    # 硬窗口 5s 会被 @ 上限压到「首条 + 2s」
    assert round_.deadline == pytest.approx(round_.first_at + 2.0)
    asyncio.run(bot.flush_merger(all_rounds=True))


def test_handle_event_plain_message_not_urgent(config, store):
    config.decision.merge_window_seconds = 5.0
    config.decision.hard_merge_window_seconds = 1.0
    config.decision.urgent_seconds = 2.0
    bot, _ = make_bot(config, store)
    asyncio.run(bot.handle_event(group_event("随便说说")))
    round_ = bot.merger.pending[0]
    assert round_.urgent is False
    assert round_.deadline == pytest.approx(round_.first_at + 5.0)
    asyncio.run(bot.flush_merger(all_rounds=True))
