"""回复意愿系统：合并窗口、硬触发、动量/占比/冷却、阈值随机、生成否决。

所有时间都显式传入，随机源可注入，因此结果完全确定。
"""

from __future__ import annotations

import random
import time

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import DecisionConfig, GroupDecisionConfig
from rubenbot.decision import (
    BOT_SENDER_ID,
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


class Sender:
    """只带 sender_id 的假流水消息，给占比统计用。"""

    def __init__(self, sender_id: str):
        self.sender_id = sender_id


def history(bot_count: int, user_count: int) -> list[Sender]:
    return [Sender(BOT_SENDER_ID)] * bot_count + [Sender("u1")] * user_count


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


def test_merger_other_sender_flushes_previous_round():
    merger = MessageMerger(window=4.0)
    merger.add(group_event("我先说", sender="u1"), 100.0)
    # u2 插话，u1 那轮立刻放出（不必等 4 秒），u2 自己开始攒
    ready = merger.add(group_event("我也来", sender="u2"), 101.0)
    assert [r.sender_id for r in ready] == ["u1"]
    assert merger.pop_ready(104.0) == []
    ready = merger.pop_ready(105.0)
    assert [r.text for r in ready] == ["我也来"]


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
    assert engine.evaluate(event, now=100.0).should_reply


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


def test_hard_trigger_ignores_quiet_hours_and_cooldown():
    engine = make_engine(
        cooldown_seconds=30.0,
        groups={"g1": GroupDecisionConfig(quiet_hours=["00:00-23:59"])},
    )
    engine.note_reply("test:g1", 100.0)
    decision = engine.evaluate(group_event(mentions=True), now=101.0)
    assert decision.should_reply and decision.hard_trigger == "mentioned"


# ----------------------------------------------------------------------
# 动量
# ----------------------------------------------------------------------


def test_momentum_decays_by_half_life():
    engine = make_engine(
        base=0.0,
        momentum_bonus=1.0,
        momentum_half_life_seconds=60.0,
        threshold=10.0,  # 远高于分数，避免随机扰动
        threshold_noise=0.0,
        cooldown_seconds=0.0,
    )
    engine.note_reply("test:g1", 100.0)
    fresh = engine.evaluate(group_event(), now=100.0).factors.momentum
    one_half = engine.evaluate(group_event(), now=160.0).factors.momentum
    two_half = engine.evaluate(group_event(), now=220.0).factors.momentum
    assert fresh == pytest.approx(1.0)
    assert one_half == pytest.approx(0.5)
    assert two_half == pytest.approx(0.25)
    # 没有参与过会话就没有动量
    other = engine.evaluate(group_event(chat_id="g2"), now=100.0).factors.momentum
    assert other == 0.0


def test_momentum_flips_decision():
    engine = make_engine(
        base=0.10,
        momentum_bonus=1.0,
        momentum_half_life_seconds=120.0,
        threshold=0.85,
        threshold_noise=0.0,
        cooldown_seconds=0.0,
    )
    engine.note_reply("test:g1", 1000.0)
    assert engine.evaluate(group_event(), now=1010.0).should_reply
    # 很久以前参与过，动量基本衰减完，基础值不够
    assert not engine.evaluate(group_event(), now=5000.0).should_reply


# ----------------------------------------------------------------------
# 发言占比抑制
# ----------------------------------------------------------------------


def test_ratio_penalty_below_silence():
    engine = make_engine(
        base=1.0,
        threshold=1.0,
        threshold_noise=0.0,
        ratio_window=10,
        ratio_threshold=0.2,
        ratio_penalty=1.5,
        ratio_silence=0.6,
    )
    quiet = engine.evaluate(group_event(), now=100.0, recent_messages=history(1, 9))
    assert quiet.should_reply
    # bot 占 40%：减分 1.5 * (0.4-0.2)/(1-0.2) = 0.375
    noisy = engine.evaluate(group_event(), now=100.0, recent_messages=history(4, 6))
    assert not noisy.should_reply
    assert noisy.factors.ratio_penalty == pytest.approx(0.375)
    assert noisy.reason == "below_threshold"


def test_ratio_over_silence_is_hard_silence():
    engine = make_engine(base=5.0, threshold=1.0, ratio_silence=0.6, threshold_noise=0.0)
    decision = engine.evaluate(group_event(), now=100.0, recent_messages=history(7, 3))
    assert not decision.should_reply
    assert decision.reason == "ratio_silence"
    assert decision.score == 0.0


def test_ratio_uses_only_last_window_from_caller():
    engine = make_engine(base=1.0, threshold=1.0, ratio_threshold=0.2, threshold_noise=0.0)
    # 更早的发言不在窗口里，不影响
    old = history(0, 10)
    assert engine.evaluate(group_event(), now=100.0, recent_messages=old).should_reply


# ----------------------------------------------------------------------
# 话题相关性
# ----------------------------------------------------------------------


def test_relevance_bonus_normalized():
    engine = make_engine(
        base=0.0,
        threshold=10.0,
        threshold_noise=0.0,
        momentum_bonus=0.0,
        relevance_bonus=0.6,
        relevance_scale=8.0,
        cooldown_seconds=0.0,
    )
    half = engine.evaluate(group_event(), now=100.0, relevance=4.0)
    full = engine.evaluate(group_event(), now=100.0, relevance=100.0)
    none = engine.evaluate(group_event(), now=100.0, relevance=0.0)
    assert half.factors.relevance == pytest.approx(0.3)
    assert full.factors.relevance == pytest.approx(0.6)  # 封顶
    assert none.factors.relevance == 0.0


def test_relevance_can_push_over_threshold():
    engine = make_engine(
        base=0.10,
        threshold=0.6,
        threshold_noise=0.0,
        momentum_bonus=0.0,
        relevance_bonus=0.6,
        relevance_scale=8.0,
        cooldown_seconds=0.0,
    )
    assert not engine.evaluate(group_event(), now=100.0).should_reply
    assert engine.evaluate(group_event(), now=100.0, relevance=20.0).should_reply


# ----------------------------------------------------------------------
# 发言后冷却
# ----------------------------------------------------------------------


def test_refractory_blocks普通消息_but_not_hard_trigger():
    engine = make_engine(
        base=1.0, threshold=0.0, threshold_noise=0.0,
        momentum_bonus=0.0, cooldown_seconds=0.0,
        refractory_enabled=True, refractory_seconds=45.0,
    )
    engine.note_bot_reply("test:g1", 100.0)
    blocked = engine.evaluate(group_event(), now=120.0)
    assert not blocked.should_reply
    assert blocked.reason == "refractory"
    hard = engine.evaluate(group_event(mentions=True), now=120.0)
    assert hard.should_reply


def test_proactive_window_caps_active_replies_and_slides():
    engine = make_engine(
        base=1.0, threshold=0.0, threshold_noise=0.0,
        momentum_bonus=0.0, cooldown_seconds=0.0,
        refractory_enabled=False, proactive_window_enabled=True,
        proactive_window_seconds=300.0, proactive_window_limit=3,
    )
    for timestamp in (0.0, 60.0, 120.0):
        engine.note_reply("test:g1", timestamp, proactive=True)
    blocked = engine.evaluate(group_event(), now=180.0)
    assert not blocked.should_reply
    assert blocked.reason == "proactive_window"
    assert engine.evaluate(group_event(), now=301.0).should_reply
    assert engine.evaluate(group_event(mentions=True), now=180.0).should_reply


def test_bot_momentum_can_be_disabled_without_counting_message_history():
    engine = make_engine(
        base=0.0, threshold=0.5, threshold_noise=0.0,
        momentum_bonus=1.0, cooldown_seconds=0.0,
        refractory_enabled=False, exclude_bot_momentum=False,
    )
    engine.note_reply("test:g1", 100.0)
    engine._last_momentum_at["test:g1"] = 100.0
    assert engine.evaluate(group_event(), now=100.0).factors.momentum == pytest.approx(1.0)
    engine.config.exclude_bot_momentum = True
    assert engine.evaluate(group_event(), now=100.0).factors.momentum == pytest.approx(1.0)


def test_cooldown_penalizes_recent_reply():
    engine = make_engine(
        base=0.9,
        threshold=0.5,
        threshold_noise=0.0,
        momentum_bonus=0.0,
        cooldown_seconds=30.0,
        cooldown_penalty=0.6,
    )
    engine.note_reply("test:g1", 100.0)
    right_after = engine.evaluate(group_event(), now=100.0)
    assert not right_after.should_reply
    assert right_after.factors.cooldown_penalty == pytest.approx(0.6)
    # 冷却过半，减分减半
    halfway = engine.evaluate(group_event(), now=115.0)
    assert halfway.factors.cooldown_penalty == pytest.approx(0.3)
    assert halfway.should_reply  # 0.9 - 0.3 = 0.6 > 0.5
    # 冷却结束后不再惩罚
    after = engine.evaluate(group_event(), now=131.0)
    assert after.factors.cooldown_penalty == 0.0
    assert after.should_reply


# ----------------------------------------------------------------------
# 阈值随机与日志因子
# ----------------------------------------------------------------------


def test_noise_only_near_threshold():
    rng = FixedRandom(0.08)
    engine = DecisionEngine(
        DecisionConfig(
            base=0.85,
            threshold=0.85,
            threshold_noise=0.1,
            threshold_noise_band=0.2,
            momentum_bonus=0.0,
            cooldown_seconds=0.0,
        ),
        rng=rng,
    )
    positive = engine.evaluate(group_event(), now=100.0)
    assert positive.should_reply
    assert rng.uniform_calls == 1
    # 分数离阈值很远时不摇骰子
    far = engine.evaluate(
        group_event(),
        now=100.0,
        recent_messages=history(5, 5),  # 占比减分把分数压到阈值band之外
    )
    assert rng.uniform_calls == 1
    assert far.factors.noise == 0.0


def test_noise_can_flip_near_threshold():
    def evaluate_with(noise: float):
        engine = DecisionEngine(
            DecisionConfig(
                base=0.85,
                threshold=0.85,
                threshold_noise=0.1,
                threshold_noise_band=0.2,
                momentum_bonus=0.0,
                cooldown_seconds=0.0,
            ),
            rng=FixedRandom(noise),
        )
        return engine.evaluate(group_event(), now=100.0)

    assert evaluate_with(-0.08).should_reply is False
    assert evaluate_with(+0.08).should_reply is True


def test_decision_log_text_contains_factors():
    engine = make_engine(threshold=0.85, threshold_noise=0.0, cooldown_seconds=0.0, momentum_bonus=1.0)
    engine.note_reply("test:g1", 100.0)
    decision = engine.evaluate(group_event(), now=110.0)
    text = decision.log_text()
    assert text.startswith("回")
    assert "动量" in text and "占比" in text and "冷却" in text


# ----------------------------------------------------------------------
# 每群配置与静默时段
# ----------------------------------------------------------------------


def test_per_group_multiplier_and_base_override():
    cfg = DecisionConfig(
        base=0.1,
        threshold=1.0,
        momentum_bonus=0.0,
        threshold_noise=0.0,
        groups={"g1": GroupDecisionConfig(activity_multiplier=2.0, base=0.6)},
    )
    engine = DecisionEngine(cfg)
    active = engine.evaluate(group_event(chat_id="g1"), now=100.0)
    quiet = engine.evaluate(group_event(chat_id="g2"), now=100.0)
    assert active.factors.base == 0.6
    assert active.factors.multiplier == 2.0
    assert active.score == pytest.approx(1.2)
    assert active.should_reply
    assert not quiet.should_reply
    assert quiet.score == pytest.approx(0.1)


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


def test_quiet_hours_silence_normal_group_message():
    engine = make_engine(groups={"g1": GroupDecisionConfig(quiet_hours=["00:00-23:59"])})
    decision = engine.evaluate(group_event(), now=_local_ts(12, 0))
    assert not decision.should_reply
    assert decision.reason == "quiet_hours"


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
