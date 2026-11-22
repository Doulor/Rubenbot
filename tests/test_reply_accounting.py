"""真实出站才记冷却；主动发言占窗口，硬触发回复不占。"""

import asyncio

import pytest

from conftest import RecordingAdapter
from test_send_pacing import group_event, make_bot

T0 = 1_000_000.0


@pytest.mark.parametrize("proactive", [False, True])
def test_successful_multipart_records_one_turn(config, store, proactive):
    bot, adapter = make_bot(config, store)
    event = group_event()
    asyncio.run(bot.send_reply(event, "第一句话。\n第二句话。", now=T0, proactive=proactive))
    assert len(adapter.sent) == 2
    assert bot.decision_engine.active_reply_count("recording:g1", T0) == int(proactive)
    assert bot.decide(event, now=T0 + 1).reason == "refractory"
    event.mentions_bot = True
    assert bot.decide(event, now=T0 + 1).hard_trigger == "mentioned"


@pytest.mark.parametrize("reply", ["", "（挥手）"])
def test_empty_after_cleanup_does_not_count(config, store, reply):
    config.chat.strip_action_parens = True
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.send_reply(group_event(), reply, now=T0, proactive=True))
    assert not adapter.sent
    assert bot.decision_engine.last_reply_at("recording:g1") is None
    assert bot.decision_engine.active_reply_count("recording:g1", T0) == 0


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


def test_all_parts_failed_does_not_enter_cooldown(config, store):
    config.reply.max_segments = 3
    bot, _ = make_bot(config, store)
    adapter = FailingAdapter()
    bot.register_adapter(adapter)
    event = group_event()
    asyncio.run(bot.send_reply(event, "第一句话。\n第二句话。", now=T0, proactive=True))
    assert adapter.sent == []
    assert adapter.attempts == 2  # 换行分段：两条
    engine = bot.decision_engine
    assert engine.last_reply_at("recording:g1") is None
    assert engine.active_reply_count("recording:g1", T0) == 0
    assert bot.decide(event, now=T0 + 10).reason == "planned"


def test_partial_send_still_counts_once(config, store):
    config.reply.max_segments = 3
    bot, _ = make_bot(config, store)
    adapter = FailingAdapter(fail_times=1)
    bot.register_adapter(adapter)
    event = group_event()
    asyncio.run(bot.send_reply(event, "第一句话。\n第二句话。", now=T0, proactive=True))
    assert len(adapter.sent) == 1  # 第一条失败，第二条发出
    engine = bot.decision_engine
    assert engine.last_reply_at("recording:g1") == T0
    assert engine.active_reply_count("recording:g1", T0) == 1
    assert bot.decide(event, now=T0 + 10).reason == "refractory"
