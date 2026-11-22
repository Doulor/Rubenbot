"""冷场开口：条件矩阵逐项、意愿评分、生成内容过滤与接线。

时间全部显式传入（``now``），随机源固定，不依赖真实时钟。
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.proactive import (
    PROACTIVE_VETO,
    ProactiveOpener,
    is_acceptable_opener,
)

from conftest import RecordingAdapter, ScriptedLLM

# 本地时间 2026-11-20 14:00：不在默认安静时段（23:00-08:00）内
NOW = time.mktime((2026, 11, 20, 14, 0, 0, 0, 0, -1))


class FixedRandom(random.Random):
    """uniform 结果固定，便于确定性断言。"""

    def __init__(self, value: float = 0.0):
        super().__init__()
        self.value = value

    def uniform(self, a, b):
        return self.value


def make_config(**overrides) -> Config:
    config = Config()
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    config.proactive.enabled = True
    config.proactive.idle_hours = 6.0
    config.proactive.quiet_hours = ["23:00-08:00"]
    config.proactive.daily_limit = 1
    config.proactive.cooldown_minutes = 0
    config.proactive.threshold_noise = 0.0
    # 默认设成「必开口」的低阈值；不想开口的用例自己调高
    config.proactive.base = 0.5
    config.proactive.threshold = 0.15
    for key, value in overrides.items():
        setattr(config.proactive, key, value)
    return config


def make_opener(config: Config | None = None, *, rng_value: float = 0.0) -> ProactiveOpener:
    return ProactiveOpener((config or make_config()).proactive, rng=FixedRandom(rng_value))


def group_message(store: MemoryStore, *, ts: float, text: str = "今天好累", chat_id: str = "g1") -> None:
    store.add_raw_message(
        platform="recording",
        chat_id=chat_id,
        chat_type="group",
        sender_id="u1",
        sender_name="阿花",
        text=text,
        ts=ts,
    )


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("recording", "u1", "阿花")
    yield s
    s.close()


# ----------------------------------------------------------------------
# 条件矩阵
# ----------------------------------------------------------------------


def test_disabled_blocks(store):
    config = make_config(enabled=False)
    group_message(store, ts=NOW - 7 * 3600)
    opener = make_opener(config)
    assert opener.blocked_reason(store, "recording", "g1", NOW) == "disabled"
    assert not opener.evaluate(store, "recording", "g1", now=NOW).should_open


def test_private_chat_never_opens(store):
    group_message(store, ts=NOW - 7 * 3600)
    opener = make_opener()
    assert (
        opener.blocked_reason(store, "recording", "g1", NOW, chat_type="private")
        == "not_group"
    )


def test_quiet_hours_blocks(store):
    group_message(store, ts=NOW - 7 * 3600)
    opener = make_opener()
    quiet_now = time.mktime((2026, 11, 20, 23, 30, 0, 0, 0, -1))
    assert opener.blocked_reason(store, "recording", "g1", quiet_now) == "quiet_hours"


def test_not_idle_blocks(store):
    group_message(store, ts=NOW - 5 * 3600)
    opener = make_opener()
    assert opener.blocked_reason(store, "recording", "g1", NOW) == "not_idle"


def test_no_history_blocks(store):
    opener = make_opener()
    assert opener.blocked_reason(store, "recording", "g1", NOW) == "not_idle"


def test_daily_limit_blocks(store):
    group_message(store, ts=NOW - 7 * 3600)
    opener = make_opener()
    opener.note_opened("recording", "g1", NOW - 3600)
    assert opener.blocked_reason(store, "recording", "g1", NOW) == "daily_limit"
    # 第二天重新计数
    tomorrow = NOW + 24 * 3600
    assert opener.opened_today("recording", "g1", tomorrow) == 0


def test_evaluation_cooldown(store):
    group_message(store, ts=NOW - 7 * 3600)
    config = make_config(cooldown_minutes=60)
    opener = make_opener(config)
    first = opener.evaluate(store, "recording", "g1", now=NOW)
    assert first.decision is not None
    second = opener.evaluate(store, "recording", "g1", now=NOW + 60)
    assert second.reason == "cooldown" and second.decision is None
    later = opener.evaluate(store, "recording", "g1", now=NOW + 61 * 60)
    assert later.decision is not None


def test_all_conditions_met_then_willingness_scales(store):
    """条件全过之后仍由低分意愿把关：分低就不开口，分够才开口。"""
    group_message(store, ts=NOW - 7 * 3600)
    low = make_opener(make_config(base=0.08, threshold=0.15))
    decision = low.evaluate(store, "recording", "g1", now=NOW)
    assert decision.decision is not None and not decision.should_open

    high = make_opener(make_config(base=0.5, threshold=0.15))
    decision = high.evaluate(store, "recording", "g1", now=NOW)
    assert decision.decision is not None and decision.should_open


def test_relevance_bonus_and_noise(store):
    group_message(store, ts=NOW - 7 * 3600)
    config = make_config(base=0.08, threshold=0.15, relevance_bonus=0.2, relevance_scale=1.0)
    opener = make_opener(config)
    # 相关性满分也只加 0.2，达到阈值
    decision = opener.evaluate(store, "recording", "g1", now=NOW, relevance=1.0)
    assert decision.should_open
    # 没相关性时要摇头
    opener2 = make_opener(make_config(base=0.08, threshold=0.15))
    assert not opener2.evaluate(store, "recording", "g1", now=NOW).should_open


def test_relevance_fn_not_called_when_blocked(store):
    group_message(store, ts=NOW - 5 * 3600)
    opener = make_opener()
    calls: list[int] = []
    opener.evaluate(
        store,
        "recording",
        "g1",
        now=NOW,
        relevance_fn=lambda: calls.append(1) or 1.0,
    )
    assert calls == []


def test_relevance_fn_called_when_unblocked(store):
    group_message(store, ts=NOW - 7 * 3600)
    opener = make_opener()
    calls: list[float] = []

    def relevance() -> float:
        calls.append(1.0)
        return 1.0

    opener.evaluate(store, "recording", "g1", now=NOW, relevance_fn=relevance)
    assert calls == [1.0]


# ----------------------------------------------------------------------
# 生成内容过滤
# ----------------------------------------------------------------------


def test_acceptable_opener_filters():
    assert is_acceptable_opener("后来那事儿怎么着了？")
    assert not is_acceptable_opener("")
    assert not is_acceptable_opener("   ")
    assert not is_acceptable_opener(PROACTIVE_VETO)
    assert not is_acceptable_opener("【[不开口]】")
    assert not is_acceptable_opener("大家都在吗")
    assert not is_acceptable_opener("怎么都不说话呀")


def test_build_prompt_carries_recent_and_summary():
    recent = [
        type("M", (), {"sender_id": "u1", "sender_name": "阿花", "text": "周末去爬山"})
    ]
    messages = ProactiveOpener.build_prompt(
        persona="你是 Ruben",
        bot_name="Ruben",
        recent=recent,  # type: ignore[arg-type]
        summary_block="【早期对话摘要】\n聊过爬山",
    )
    system = messages[0]["content"]
    assert "周末去爬山" in system and "聊过爬山" in system
    assert PROACTIVE_VETO in system


# ----------------------------------------------------------------------
# 接线：默认关；全满足时发一条并走正常通道
# ----------------------------------------------------------------------


def make_bot(store: MemoryStore, llm, config: Config | None = None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config or make_config(), store, llm, Persona("Ruben", "你是 Ruben"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def test_proactive_disabled_by_default():
    assert Config().proactive.enabled is False


def test_check_does_nothing_when_disabled(store):
    group_message(store, ts=NOW - 7 * 3600)
    config = make_config(enabled=False)
    llm = ScriptedLLM({"chat": ["后来呢？"]})
    bot, adapter = make_bot(store, llm, config)
    assert asyncio.run(bot._proactive_check(now=NOW)) is False
    assert adapter.texts == [] and llm.calls == []


def test_same_id_private_activity_does_not_reach_group_opener(store, monkeypatch):
    config = make_config()
    config.privacy.enabled = True
    llm = ScriptedLLM({'chat': ['爬山的路线定了吗？']})
    bot, _ = make_bot(store, llm, config)
    group_message(store, ts=NOW - 7 * 3600, text='群里约了爬山')
    rid = store.add_raw_message(platform='recording', chat_type='private', chat_id='g1', sender_id='g1',
                                text='不对外讲的私聊原话', ts=NOW-1)
    store.set_conversation_summary('recording', 'g1', '私聊早期摘要', rid, chat_type='private', source_ids=[rid])
    queries = []
    monkeypatch.setattr(bot.retriever, 'score', lambda text: queries.append(text) or [])
    assert asyncio.run(bot._proactive_check(now=NOW))  # 刚来的私聊不能重置群的冷场时间。
    assert queries == ['群里约了爬山']
    prompt = '\n'.join(m['content'] for call in llm.calls for m in call['messages'])
    assert '群里约了爬山' in prompt
    assert '私聊原话' not in prompt and '私聊早期摘要' not in prompt


def test_check_opens_once_when_all_conditions_met(store):
    group_message(store, ts=NOW - 7 * 3600, text="周末谁去爬山")
    llm = ScriptedLLM({"chat": ["爬山那事儿最后定了没？"]})
    bot, adapter = make_bot(store, llm)
    opened = asyncio.run(bot._proactive_check(now=NOW))
    assert opened is True
    assert adapter.texts == ["爬山那事儿最后定了没？"]
    # 当天已开口：再扫一次就达到上限，不再开口
    assert asyncio.run(bot._proactive_check(now=NOW + 60)) is False
    assert len(adapter.texts) == 1


@pytest.mark.parametrize(
    "case",
    ["quiet_hours", "not_idle", "daily_limit", "unwilling"],
)
def test_check_sends_nothing_when_any_condition_fails(store, case):
    """条件矩阵逐项：任何一项不满足都不发，且不花生成。"""
    group_message(store, ts=NOW - 7 * 3600)
    config = make_config()
    now = NOW
    if case == "quiet_hours":
        now = time.mktime((2026, 11, 20, 23, 30, 0, 0, 0, -1))
    elif case == "not_idle":
        config.proactive.idle_hours = 8.0
    elif case == "daily_limit":
        config.proactive.daily_limit = 0
    elif case == "unwilling":
        config.proactive.base = 0.08
        config.proactive.threshold = 0.15
    llm = ScriptedLLM({"chat": ["不该发出去"]})
    bot, adapter = make_bot(store, llm, config)
    assert asyncio.run(bot._proactive_check(now=now)) is False
    assert adapter.texts == [] and llm.calls == []


def test_check_skips_when_model_vetoes(store):
    group_message(store, ts=NOW - 7 * 3600)
    llm = ScriptedLLM({"chat": [PROACTIVE_VETO]})
    bot, adapter = make_bot(store, llm)
    assert asyncio.run(bot._proactive_check(now=NOW)) is False
    assert adapter.texts == []
    # 放弃不算「开口」，下次还可以再试
    assert bot.opener.opened_today("recording", "g1", NOW) == 0


def test_check_skips_when_model_canned(store):
    group_message(store, ts=NOW - 7 * 3600)
    llm = ScriptedLLM({"chat": ["大家都在吗"]})
    bot, adapter = make_bot(store, llm)
    assert asyncio.run(bot._proactive_check(now=NOW)) is False
    assert adapter.texts == []


def test_check_skips_private_chats(store):
    store.add_raw_message(
        platform="recording",
        chat_id="u1",
        chat_type="private",
        sender_id="u1",
        sender_name="阿花",
        text="在吗",
        ts=NOW - 7 * 3600,
    )
    llm = ScriptedLLM({"chat": ["在吗？"]})
    bot, adapter = make_bot(store, llm)
    assert asyncio.run(bot._proactive_check(now=NOW)) is False
    assert adapter.texts == []


def test_opening_written_into_history_and_decision_log(store):
    group_message(store, ts=NOW - 7 * 3600)
    llm = ScriptedLLM({"chat": ["爬山那事儿最后定了没？"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot._proactive_check(now=NOW))
    recent = store.recent_messages("g1", limit=5, platform="recording")
    assert recent[-1].text == "爬山那事儿最后定了没？"
    assert recent[-1].sender_id == "__bot__"
    logs = store.list_decision_logs(chat_id="g1", platform="recording")
    assert logs[0]["reason"] == "proactive" and logs[0]["should_reply"] is True
