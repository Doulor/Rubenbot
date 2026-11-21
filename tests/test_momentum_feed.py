"""动量喂入路径：真实发送走 note_bot_reply，只有被点名触发的发言喂动量。

回归背景：活人感批次把发送路径全改走 note_bot_reply(momentum=False) 后，
_last_momentum_at 再无人写，线上动量恒为 0、bot 不被 @ 永不开口。
旧测试直接调 note_reply 喂动量，测不出这条断链，这里专门钉发送路径。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import RecordingAdapter
from test_decision import group_event, make_engine
from test_send_pacing import group_event as bot_group_event
from test_send_pacing import make_bot

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


# ----------------------------------------------------------------------
# 发送失败不记发言：分条全发失败时，动量/不应期/最后发言时间都不应被写
# （Orange 复核发现：旧 send_reply 在循环后无条件调 note_bot_reply，
# 失败的发送同样喂动量，还会把不应期刷掉、让 bot 误以为自己说过话。）
# ----------------------------------------------------------------------


class FailingAdapter(RecordingAdapter):
    """前 ``fail_times`` 次发送抛错，之后正常记录。"""

    def __init__(self, fail_times: int = 999):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("模拟发送失败")
        return await super().send(chat_id, segments, chat_type=chat_type)


def test_all_parts_failed_feeds_no_momentum(config, store):
    config.reply.max_segments = 3
    bot, _ = make_bot(config, store)
    adapter = FailingAdapter()
    bot.register_adapter(adapter)
    event = bot_group_event()
    asyncio.run(bot.send_reply(event, "第一句话。\n第二句话。", now=T0, addressed=True))
    assert adapter.sent == []
    assert adapter.attempts == 2  # 换行分段：两条
    engine = bot.decision_engine
    assert engine.last_reply_at("recording:g1") is None
    assert engine.evaluate(event, now=T0 + 10).factors.momentum == 0.0
    # 不应期也没被刷：同一时刻的评估不应以 refractory 拒绝
    assert engine.evaluate(event, now=T0 + 10, relevance=4.0).reason != "refractory"


def test_partial_send_still_feeds_momentum_once(config, store):
    config.reply.max_segments = 3
    bot, _ = make_bot(config, store)
    adapter = FailingAdapter(fail_times=1)
    bot.register_adapter(adapter)
    event = bot_group_event()
    asyncio.run(bot.send_reply(event, "第一句话。\n第二句话。", now=T0, addressed=True))
    assert len(adapter.sent) == 1  # 第一条失败，第二条发出
    engine = bot.decision_engine
    assert engine.last_reply_at("recording:g1") == T0
    assert engine.evaluate(event, now=T0 + 10).factors.momentum > 0.0
