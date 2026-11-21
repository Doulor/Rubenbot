"""动量喂入路径：真实发送走 note_bot_reply，只有被点名触发的发言喂动量。

回归背景：活人感批次把发送路径全改走 note_bot_reply(momentum=False) 后，
_last_momentum_at 再无人写，线上动量恒为 0、bot 不被 @ 永不开口。
旧测试直接调 note_reply 喂动量，测不出这条断链，这里专门钉发送路径。
"""

from __future__ import annotations

import pytest

from test_decision import group_event, make_engine

T0 = 1_000_000.0  # 任意时刻（沿用 test_decision 的显式时间约定）


def warm_engine(**overrides):
    # 关掉随机扰动，分数完全由公式决定
    overrides.setdefault("threshold_noise", 0.0)
    return make_engine(**overrides)


def test_addressed_reply_feeds_momentum_and_decays():
    engine = warm_engine(cooldown_seconds=0.0)
    engine.note_bot_reply("test:g1", T0, addressed=True)
    fresh = engine.evaluate(group_event(), now=T0 + 30).factors.momentum
    one_half = engine.evaluate(group_event(), now=T0 + 120).factors.momentum
    later = engine.evaluate(group_event(), now=T0 + 300).factors.momentum
    assert fresh == pytest.approx(0.5 * 0.5 ** (30 / 120))
    assert one_half == pytest.approx(0.25)
    assert later == pytest.approx(0.5 * 0.5 ** (300 / 120))
    assert fresh > one_half > later > 0


def test_proactive_reply_does_not_feed_momentum():
    engine = warm_engine(cooldown_seconds=0.0)
    engine.note_bot_reply("test:g1", T0, proactive=True)
    assert engine.evaluate(group_event(), now=T0 + 10).factors.momentum == 0.0
    # 默认（未标 addressed）的主动路径同样不喂
    engine2 = warm_engine(cooldown_seconds=0.0)
    engine2.note_bot_reply("test:g1", T0)
    assert engine2.evaluate(group_event(), now=T0 + 10).factors.momentum == 0.0


def test_warm_thread_flips_decision_then_cools_off():
    # base 用线上值 0.25（代码默认 0.10）；相关性因子 0.6 * min(1, 4/8) = 0.30
    # 冷会话 0.25+0.30=0.55 不够阈值 0.85
    engine = warm_engine(base=0.25, cooldown_seconds=0.0, refractory_enabled=False)
    cold = engine.evaluate(group_event(), now=T0, relevance=4.0)
    assert not cold.should_reply
    engine.note_bot_reply("test:g1", T0, addressed=True)
    warm = engine.evaluate(group_event(), now=T0 + 30, relevance=4.0)
    assert warm.should_reply  # 0.25 + 0.42 + 0.30 ≈ 0.97
    cooled = engine.evaluate(group_event(), now=T0 + 300, relevance=4.0)
    assert not cooled.should_reply  # 动量衰减到 0.09，合计约 0.64


def test_refractory_still_blocks_right_after_reply():
    engine = warm_engine()  # 默认 refractory 45s、cooldown 30s 都开
    engine.note_bot_reply("test:g1", T0, addressed=True)
    blocked = engine.evaluate(group_event(), now=T0 + 30, relevance=4.0)
    assert not blocked.should_reply
    assert blocked.reason == "refractory"
