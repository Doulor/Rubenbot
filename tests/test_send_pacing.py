"""发送节奏与防抖上限（借鉴 MaiBot 研读 #2/#3 的最小版）。"""

import asyncio

import pytest

from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.core import (
    RubenBot,
    estimate_typing_seconds,
    split_sentences,
    strip_action_parens,
    strip_transcript_prefix,
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
# 打字时间估算
# ----------------------------------------------------------------------


def test_typing_estimate_chinese_and_mixed():
    # 4 个中文字 ×0.3 + 回车 0.3
    assert estimate_typing_seconds([Segment.text("你好呀啊")]) == pytest.approx(1.5)
    # 1 个中文字 ×0.3 + 4 个英文/数字字符 ×0.15 + 回车 0.3
    assert estimate_typing_seconds([Segment.text("好 ok12")]) == pytest.approx(0.3 + 0.6 + 0.3)


def test_typing_estimate_single_char_tripled():
    # 单字：0.3 ×3 + 回车 0.3
    assert estimate_typing_seconds([Segment.text("嗯")]) == pytest.approx(1.2)


def test_typing_estimate_sticker_only():
    assert estimate_typing_seconds([Segment.image("http://x/1.png")]) == pytest.approx(1.0)
    # 文本段全是空白也按纯表情处理
    assert estimate_typing_seconds([Segment.text("  ")]) == pytest.approx(1.0)


def test_typing_delay_applies_speed_and_cap(config, store):
    bot, _ = make_bot(config, store)
    config.chat.type_speed = 1.0
    config.chat.type_delay_max_seconds = 4.0
    assert bot.typing_delay([Segment.text("嗯")]) == pytest.approx(1.2)
    # 超长文本截到单条上限
    assert bot.typing_delay([Segment.text("字" * 100)]) == pytest.approx(4.0)
    # 全局倍率
    config.chat.type_speed = 0.5
    assert bot.typing_delay([Segment.text("嗯")]) == pytest.approx(0.6)
    # 倍率为 0 表示不等
    config.chat.type_speed = 0
    assert bot.typing_delay([Segment.text("嗯")]) == 0.0


def test_send_reply_waits_between_parts(config, store, monkeypatch):
    """第一条立即发，后续条之间按估算等待。"""
    config.chat.split_part_chars = 10
    config.chat.split_max_parts = 3
    config.chat.type_speed = 1.0
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "第一句话。第二句话。第三句话。"))
    assert len(adapter.sent) >= 2
    # 每条之间正好一次停顿
    assert len(sleeps) == len(adapter.sent) - 1
    assert all(s > 0 for s in sleeps)


# ----------------------------------------------------------------------
# 断句保护
# ----------------------------------------------------------------------


def test_split_sentences_keeps_bracketed_emoticon_whole():
    text = "太好了（≧▽≦。笑死）我们走。"
    parts = split_sentences(text)
    assert parts == ["太好了（≧▽≦。笑死）我们走。"]


def test_split_reply_does_not_break_emoticon(config, store):
    config.chat.split_part_chars = 10
    config.chat.split_max_parts = 3
    bot, _ = make_bot(config, store)
    reply = "好开心(≧▽≦)！周末一起去玩吧。"
    parts = bot.split_reply(reply)
    for part in parts:
        # 括号必须成对，不能从颜文字中间切开
        assert part.count("(") == part.count(")")
    assert any("(≧▽≦)" in part for part in parts)
    assert "".join(parts) == reply


def test_split_reply_does_not_break_kaomoji_letters(config, store):
    config.chat.split_part_chars = 12
    config.chat.split_max_parts = 3
    bot, _ = make_bot(config, store)
    reply = "今天真的绝了QAQ！但是没关系OTZ。"
    parts = bot.split_reply(reply)
    assert any("QAQ" in part for part in parts)
    assert any("OTZ" in part for part in parts)
    assert "".join(parts) == reply


def test_split_sentences_keeps_url_whole():
    url = "https://example.com/a?b=1&c=2!d=3"
    parts = split_sentences(f"看这个！{url}。好好玩。")
    assert any(url in part for part in parts)
    assert all("https://" not in part or url in part for part in parts)


def test_split_sentences_keeps_inline_code_whole():
    text = "试一下 `print('a。b')` 这句。好。"
    parts = split_sentences(text)
    assert parts == ["试一下 `print('a。b')` 这句。", "好。"]


def test_split_sentences_keeps_quotes_whole():
    text = "他说「快点。等一下」然后走了。"
    assert split_sentences(text) == ["他说「快点。等一下」然后走了。"]


def test_split_sentences_handles_unmatched_bracket():
    # 找不到配对符号时按普通字符处理，不吞掉后面的内容
    assert split_sentences("前面（没闭合。后面") == ["前面（没闭合。", "后面"]


def test_short_reply_not_split_even_with_breaks(config, store):
    config.chat.split_part_chars = 30
    bot, _ = make_bot(config, store)
    assert bot.split_reply("好。走。") == ["好。走。"]


# ----------------------------------------------------------------------
# 转录行前缀清洗（模型模仿历史格式的兜底）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # 带时间戳和发言人前缀：剥掉
        ("[10:00] [我] 刚才说到哪了", "刚才说到哪了"),
        ("[9:05] [阿花] 在吗", "在吗"),
        ("[10:00] [我]  两个空格也行", "两个空格也行"),
        # 正常正文，没有前缀
        ("我觉得挺好", "我觉得挺好"),
        # 正文以时间开头但后面不是 [名字] 形式：不剥
        ("[10:00] 开会了", "[10:00] 开会了"),
        ("[10:00][我] 中间没空格？剥", "中间没空格？剥"),
        # 只剥开头，正文中间同样样式不动
        ("先说这个 [10:00] [我] 后面不动", "先说这个 [10:00] [我] 后面不动"),
        ("", ""),
    ],
)
def test_strip_transcript_prefix(text, expected):
    assert strip_transcript_prefix(text) == expected


def test_send_reply_strips_transcript_prefix(config, store):
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), "[10:00] [我] 全局记忆这块你真搞出来了？"))
    assert adapter.texts == ["全局记忆这块你真搞出来了？"]


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
