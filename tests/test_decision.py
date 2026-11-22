"""回复决策：合并窗口、硬触发、节奏护栏、主动冷场评分、生成否决。

所有时间都显式传入，随机源可注入，因此结果完全确定。
"""

from __future__ import annotations

import random
import time

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import DecisionConfig, ProactiveConfig
from rubenbot.decision import (
    DecisionEngine,
    MessageMerger,
    in_quiet_hours,
    is_reply_to_bot,
    parse_generation_output,
    parse_quiet_hours,
)


class FixedRandom(random.Random):
    """uniform 结果固定，且记录调用次数。"""

    def __init__(self, value: float = 0.0):
        super().__init__()
        self.value = value
        self.uniform_calls = 0

    def uniform(self, a, b):
        self.uniform_calls += 1
        return self.value


def group_event(
    text="今天好累",
    *,
    sender="u1",
    chat_id="g1",
    mentions=False,
    reply_to=None,
    raw=None,
) -> MessageEvent:
    return MessageEvent(
        platform="test",
        chat_type="group",
        chat_id=chat_id,
        sender_id=sender,
        sender_name=f"名字{sender}",
        text=text,
        mentions_bot=mentions,
        reply_to_id=reply_to,
        raw=raw or {},
    )


def private_event(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="test",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def make_engine(**overrides) -> DecisionEngine:
    cfg = DecisionConfig(**overrides) if overrides else DecisionConfig()
    return DecisionEngine(cfg, aliases=["Ruben"], rng=FixedRandom())


# ----------------------------------------------------------------------
# 消息合并窗口
# ----------------------------------------------------------------------


def test_merger_merges_same_sender_within_window():
    merger = MessageMerger(window=4.0, hard_window=1.0)
    assert merger.add(group_event("你好"), 100.0) == []
    assert merger.add(group_event("在吗"), 102.0) == []
    # 第二条把截止时间延到 106；104 时还不该放出来
    assert merger.pop_ready(104.0) == []
    rounds = merger.pop_ready(106.0)
    assert len(rounds) == 1
    assert rounds[0].text == "你好\n在吗"
    assert len(rounds[0].events) == 2
    assert len(merger) == 0


def test_merger_other_sender_extends_conversation_silence():
    merger = MessageMerger()
    merger.add(group_event("我先说", sender="u1"), 100.0)
    assert merger.add(group_event("我也来", sender="u2"), 101.0) == []
    assert merger.pop_ready(103.49) == []
    batch, = merger.pop_ready(103.5)
    assert [e.sender_id for e in batch.events] == ["u1", "u2"]
    assert batch.primary_sender == "u2"


def test_merger_hard_message_has_short_window():
    merger = MessageMerger(window=4.0, hard_window=1.0)
    assert merger.add(group_event("@Ruben", mentions=True), 100.0, hard=True) == []
    # 普通窗口要 4 秒，硬触发 1 秒就到
    assert merger.pop_ready(101.0)[0].hard is True
    assert len(merger) == 0


def test_merger_zero_hard_window_is_immediate():
    merger = MessageMerger(window=4.0, hard_window=0.0)
    ready = merger.add(group_event("@Ruben", mentions=True), 100.0, hard=True)
    assert len(ready) == 1
    assert ready[0].text == "@Ruben"


def test_merger_zero_window_processes_everything_immediately():
    merger = MessageMerger(window=0.0, hard_window=0.0)
    assert len(merger.add(group_event("一"), 10.0)) == 1
    assert len(merger.add(group_event("二"), 10.0)) == 1
    assert len(merger) == 0


def test_merged_event_keeps_mentions_and_reply_target():
    merger = MessageMerger(window=4.0)
    merger.add(group_event("普通"), 100.0)
    merger.add(
        group_event("叫你", mentions=True, reply_to="42", raw={"x": 1}),
        101.0,
    )
    round_ = merger.pop_ready(105.0)[0]
    event = round_.to_event()
    assert event.mentions_bot is True
    assert event.reply_to_id == "42"
    assert event.text == "普通\n叫你"
    assert event.raw["merged_count"] == 2


def test_next_deadline_and_flush_all():
    merger = MessageMerger(window=4.0, hard_window=0.0)
    merger.add(group_event("一条"), 100.0)
    assert merger.next_deadline() == 104.0
    flushed = merger.flush_all()
    assert [r.text for r in flushed] == ["一条"]
    assert merger.next_deadline() is None


def test_merger_batch_cap_defaults_do_not_break_normal_window():
    """默认整批上限 10s 比普通窗口长：正常对话仍按窗口续命。"""
    merger = MessageMerger(window=4.0, hard_window=1.0)
    merger.add(group_event("一"), 100.0)
    merger.add(group_event("二"), 103.0)
    assert merger.next_deadline() == 107.0


# ----------------------------------------------------------------------
# 硬触发
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, expected",
    [
        (private_event(), "private"),
        (group_event(mentions=True), "mentioned"),
        (group_event("回复你", reply_to="7", raw={"bot_message_ids": ["7"]}), "reply_to_bot"),
        (group_event("Ruben 你觉得呢"), "name:Ruben"),
    ],
)
def test_hard_triggers(event, expected):
    engine = make_engine()
    assert engine.is_hard_trigger(event) == expected
    engine.note_bot_reply(engine.chat_key(event), 99.0, proactive=True)
    assert engine.guard(event, now=100.0, proactive=True) is None


def test_name_alias_respects_word_boundary():
    engine = DecisionEngine(DecisionConfig(), aliases=["Ruben"])
    assert engine.is_hard_trigger(group_event("ruben 在吗")) == "name:Ruben"
    # 不能把 rubenbot 之类误判成叫名字
    assert engine.is_hard_trigger(group_event("rubenbot 是啥")) is None


def test_chinese_alias_substring():
    engine = DecisionEngine(DecisionConfig(), aliases=["小R", "鲁本"])
    assert engine.is_hard_trigger(group_event("小R在干嘛")) == "name:小R"
    assert engine.is_hard_trigger(group_event("鲁本帮我看看")) == "name:鲁本"
    assert engine.is_hard_trigger(group_event("这游戏真难")) is None


def test_reply_to_bot_raw_bool():
    event = group_event("回你", reply_to="9", raw={"reply_to_bot": True})
    assert is_reply_to_bot(event)


@pytest.mark.parametrize("opts,elapsed,reason", [
    ({}, 44.99, "refractory"),
    ({}, 45, None),
    ({"refractory_enabled": False, "min_reply_interval_seconds": 10}, 9.99, "min_interval"),
    ({"refractory_seconds": 0, "min_reply_interval_seconds": 10}, 10, None),
    ({"refractory_seconds": 5, "min_reply_interval_seconds": 10}, 4, "refractory"),
    ({"refractory_seconds": 5, "min_reply_interval_seconds": 10}, 6, "min_interval"),
])
def test_guard_timing_and_precedence(opts, elapsed, reason):
    engine = make_engine(**opts)
    engine.note_bot_reply("test:g1", 100)
    guard = engine.guard(group_event(), now=100 + elapsed, proactive=True)
    assert (guard.reason if guard else None) == reason


@pytest.mark.parametrize("enabled,limit,elapsed,blocked", [
    (True, 0, 60, True), (True, 1, 59, True), (True, 1, 60, False),
    (True, 2, 1, False), (False, 0, 1, False),
])
def test_active_window_boundaries(enabled, limit, elapsed, blocked):
    engine = make_engine(refractory_seconds=0, min_reply_interval_seconds=0,
                         proactive_window_enabled=enabled, proactive_window_seconds=60,
                         proactive_window_limit=limit)
    engine.note_bot_reply("test:g1", 100, proactive=True)
    guard = engine.guard(group_event(), now=100 + elapsed, proactive=True)
    assert (guard.reason if guard else None) == ("proactive_window" if blocked else None)
    assert engine.guard(group_event(mentions=True), now=100 + elapsed, proactive=True) is None


@pytest.mark.parametrize("relevance,bonus", [(-1, 0), (0, 0), (4, .3), (8, .6), (100, .6)])
def test_cold_opener_relevance_is_capped(relevance, bonus):
    engine = make_engine()
    config = ProactiveConfig(base=.2, threshold=.5, threshold_noise=0,
                             relevance_scale=8, relevance_bonus=.6)
    result = engine.evaluate_opener(config, relevance=relevance)
    assert result.score == pytest.approx(.2 + bonus)
    assert result.should_reply == (.2 + bonus >= .5)
    assert result.factors.relevance == pytest.approx(bonus)


@pytest.mark.parametrize("base,noise,should,calls", [
    (.5, -.04, False, 1), (.5, .04, True, 1), (.25, .04, False, 0), (.75, -.04, True, 0),
])
def test_cold_opener_only_draws_near_threshold(base, noise, should, calls):
    rng = FixedRandom(noise)
    engine = DecisionEngine(rng=rng)
    result = engine.evaluate_opener(ProactiveConfig(base=base, threshold=.5,
                                    threshold_noise=.05, threshold_noise_band=.1))
    assert result.should_reply is should
    assert rng.uniform_calls == calls
    assert result.reason in result.log_text() and "相关" in result.log_text()


def test_parse_quiet_hours_and_ignore_bad_values(caplog):
    ranges = parse_quiet_hours(["23:00-07:30", "12:00-13:00", "坏值", "25:00-26:00", "10:00-10:00"])
    assert ranges == [(23 * 60, 7 * 60 + 30), (12 * 60, 13 * 60)]
    assert "忽略" in caplog.text


def _local_ts(hour: int, minute: int) -> float:
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))


def test_in_quiet_hours_cross_midnight():
    ranges = parse_quiet_hours(["23:00-07:30"])
    assert in_quiet_hours(_local_ts(23, 30), ranges)
    assert in_quiet_hours(_local_ts(3, 0), ranges)
    assert in_quiet_hours(_local_ts(7, 29), ranges)
    assert not in_quiet_hours(_local_ts(7, 30), ranges)
    assert not in_quiet_hours(_local_ts(12, 0), ranges)


def test_daytime_quiet_range_has_exclusive_end():
    ranges = parse_quiet_hours(["12:30-13:00"])
    assert not in_quiet_hours(_local_ts(12, 29), ranges)
    assert in_quiet_hours(_local_ts(12, 30), ranges)
    assert not in_quiet_hours(_local_ts(13, 0), ranges)


# ----------------------------------------------------------------------
# 生成后否决
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["[不回复]", " [不回复] ", "[不回复]。", "（不回复）", "【[不回复]】", "「[不回复]」"],
)
def test_generation_veto_marker(text):
    result = parse_generation_output(text)
    assert result.vetoed
    assert result.text == ""


@pytest.mark.parametrize("text", ["", "   ", None])
def test_generation_empty_output_is_vetoed(text):
    assert parse_generation_output(text).vetoed


def test_generation_normal_reply_kept():
    result = parse_generation_output("我觉得还行吧")
    assert not result.vetoed
    assert result.text == "我觉得还行吧"


def test_generation_marker_inside_sentence_not_vetoed():
    """句子里顺带提到「不回复」不应被当成标记，避免误杀。"""
    result = parse_generation_output("他刚才说不回复，可能睡着了吧")
    assert not result.vetoed


def test_generation_custom_marker():
    result = parse_generation_output("<silent>", marker="<silent>")
    assert result.vetoed
    result = parse_generation_output("[不回复]", marker="<silent>")
    assert not result.vetoed
