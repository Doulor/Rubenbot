"""情绪与好感度：规则逐项单测（幅度、上限、衰减、档位、注入、阈值微调）。

所有时间与随机源都显式注入，结果完全确定。
"""

from __future__ import annotations

import asyncio

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config, EmotionConfig
from rubenbot.core import RubenBot
from rubenbot.emotion import (
    AFFINITY_BANDS,
    LAST_ENGAGED_KEY,
    MOOD_CALM,
    EmotionEngine,
    affinity_band,
    decay_intensity,
    decay_toward_baseline,
    detect_insult,
    detect_praise,
    intensity_phrase,
    local_day_start,
)
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM

NOW = 1_000_000.0
PID = "recording:u1"
DAY = 86400.0
WEEK = 7 * DAY


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("recording", "u1", "阿花")
    yield s
    s.close()


@pytest.fixture()
def engine(store):
    return EmotionEngine(store, EmotionConfig(), now_fn=lambda: NOW)


def group_event(text="你好呀", *, mentions=False) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
        mentions_bot=mentions,
    )


def private_event(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def make_bot(config, store, llm=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm or ScriptedLLM({"chat": ["在的"]}), Persona("Ruben", "你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


# ----------------------------------------------------------------------
# 关键词规则（保守：宁可漏判，不可误判）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["谢谢你", "感谢你陪我", "Ruben 你真厉害", "你真的很棒", "你太可爱了", "辛苦啦", "有你真好", "你写得好"],
)
def test_praise_detected(text):
    assert detect_praise(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "他真厉害",
        "好厉害啊你",  # 关键词在「你」前面，是感叹不是在夸对方
        "我不觉得你厉害",
        "你看这个好恶心",  # 说别的东西
        "你真是个垃圾袋",  # 修饰具体物品
        "今天好累啊",
        "",
    ],
)
def test_praise_not_detected(text):
    assert detect_praise(text) is False


@pytest.mark.parametrize(
    "text",
    ["你真傻逼", "你这个笨蛋", "你白痴吧", "你就是个废物", "滚开", "闭嘴", "你真讨厌"],
)
def test_insult_detected(text):
    assert detect_insult(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "傻逼",  # 没有第二人称，可能是自嘲/转述
        "他可真蠢货",
        "你这垃圾袋怎么回事",
        "你看这个好恶心",
        "你不蠢啊",
        "滚",  # 单个「滚」歧义太大，宁可漏判
        "今天天气不错",
    ],
)
def test_insult_not_detected(text):
    assert detect_insult(text) is False


# ----------------------------------------------------------------------
# 档位与衰减（纯函数）
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected",
    [(100, "亲近"), (80, "亲近"), (79.9, "友好"), (60, "友好"), (59.9, "普通"),
     (40, "普通"), (39.9, "冷淡"), (20, "冷淡"), (19.9, "疏远"), (0, "疏远")],
)
def test_affinity_band_boundaries(score, expected):
    assert affinity_band(score) == expected


def test_band_thresholds_are_the_documented_order():
    assert [name for _score, name in AFFINITY_BANDS] == ["亲近", "友好", "普通", "冷淡"]


def test_decay_toward_baseline_one_week_is_five_percent():
    assert decay_toward_baseline(100.0, WEEK) == pytest.approx(50.0 + 50.0 * 0.95)
    assert decay_toward_baseline(0.0, WEEK) == pytest.approx(50.0 - 50.0 * 0.95)
    # 基线不动；没经过时间也不动
    assert decay_toward_baseline(50.0, WEEK * 10) == pytest.approx(50.0)
    assert decay_toward_baseline(73.0, 0.0) == pytest.approx(73.0)
    # 负数时间是坏数据，按 0 处理
    assert decay_toward_baseline(73.0, -100.0) == pytest.approx(73.0)


def test_intensity_half_life():
    assert decay_intensity(0.6, 3600, 3600) == pytest.approx(0.3)
    assert decay_intensity(0.6, 2 * 3600, 3600) == pytest.approx(0.15)
    assert decay_intensity(0.6, 0, 3600) == pytest.approx(0.6)
    assert decay_intensity(0.6, 100 * 3600, 3600) < 0.001


def test_intensity_phrases_are_ordered():
    assert intensity_phrase(0.9) == "很强烈"
    assert intensity_phrase(0.6) == "挺明显"
    assert intensity_phrase(0.3) == "淡淡的"
    assert intensity_phrase(0.05) == "若隐若现"


def test_local_day_start_is_midnight():
    import time as _time

    start = local_day_start(NOW)
    assert start <= NOW < start + DAY
    assert _time.localtime(start).tm_hour == 0
    assert _time.localtime(start).tm_min == 0


# ----------------------------------------------------------------------
# 好感度：幅度、流水、上限、衰减
# ----------------------------------------------------------------------


def test_baseline_without_record(engine):
    assert engine.current_affinity(PID, now=NOW) == 50.0
    assert engine.band_for(PID, now=NOW) == "普通"


def test_chat_round_adds_point_two_and_logs(engine, store):
    gained = engine.record_chat_round(PID, place="recording:g1", now=NOW)
    assert gained == pytest.approx(0.2)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(50.2)
    logs = store.list_affinity_logs(person_id=PID)
    assert [(entry.delta, entry.reason) for entry in logs] == [(0.2, "对话")]
    assert logs[0].place == "recording:g1"


def test_chat_daily_cap_two_points(engine):
    for _ in range(10):  # 0.2 × 10 = 2.0
        engine.record_chat_round(PID, now=NOW)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(52.0)
    # 超额不再加
    assert engine.record_chat_round(PID, now=NOW) == 0.0
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(52.0)


def test_chat_cap_resets_next_day(engine):
    for _ in range(10):
        engine.record_chat_round(PID, now=NOW)
    day_end = local_day_start(NOW) + DAY - 1
    # 同一自然日的最后一刻仍然到顶
    assert engine.chat_round_allowance(PID, now=day_end) == pytest.approx(0.0)
    # 次日零点重新计
    assert engine.chat_round_allowance(PID, now=day_end + 2) == pytest.approx(2.0)
    assert engine.record_chat_round(PID, now=day_end + 2) == pytest.approx(0.2)


def test_allowance_caps_partial_add(store):
    """额度只剩 0.1 时只加 0.1，不会超上限。"""
    engine = EmotionEngine(store, EmotionConfig(chat_daily_cap=0.3), now_fn=lambda: NOW)
    assert engine.record_chat_round(PID, now=NOW) == pytest.approx(0.2)
    assert engine.chat_round_allowance(PID, now=NOW) == pytest.approx(0.1)
    assert engine.record_chat_round(PID, now=NOW) == pytest.approx(0.1)
    assert engine.chat_round_allowance(PID, now=NOW) == pytest.approx(0.0)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(50.3)


def test_praise_and_insult_amplitudes(engine, store):
    engine.apply_delta(PID, 1.0, reason="被夸", now=NOW)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(51.0)
    engine.apply_delta(PID, -2.0, reason="被骂", now=NOW)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(49.0)
    reasons = [entry.reason for entry in store.list_affinity_logs(person_id=PID)]
    assert reasons == ["被骂", "被夸"]  # 新的在前


def test_affinity_clamped_to_range(engine):
    engine.apply_delta(PID, 999, reason="被夸", now=NOW)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(100.0)
    engine.apply_delta(PID, -999, reason="被骂", now=NOW)
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(0.0)


def test_lazy_decay_folds_into_log(engine, store):
    engine.apply_delta(PID, 30.0, reason="被夸", now=NOW)  # 80
    # 一周后读：折算 5%
    assert engine.current_affinity(PID, now=NOW + WEEK) == pytest.approx(50 + 30 * 0.95)
    # 读不算写：流水里还没有衰减这一笔
    assert [entry.reason for entry in store.list_affinity_logs(person_id=PID)] == ["被夸"]
    # 下一次真正写入时把衰减折进存值并单独记一笔
    engine.apply_delta(PID, 0.2, reason="对话", now=NOW + WEEK)
    reasons = [entry.reason for entry in store.list_affinity_logs(person_id=PID)]
    assert reasons == ["对话", "衰减", "被夸"]
    decay_entry = [entry for entry in store.list_affinity_logs(person_id=PID) if entry.reason == "衰减"][0]
    assert decay_entry.delta == pytest.approx(-1.5, abs=1e-3)


def test_decay_pulls_low_scores_up(engine):
    engine.apply_delta(PID, -30.0, reason="被骂", now=NOW)  # 20
    assert engine.current_affinity(PID, now=NOW + WEEK) == pytest.approx(50 - 30 * 0.95)


def test_disabled_engine_changes_nothing(store):
    engine = EmotionEngine(store, EmotionConfig(enabled=False), now_fn=lambda: NOW)
    assert engine.record_chat_round(PID, now=NOW) == 0.0
    assert engine.apply_delta(PID, 1.0, reason="被夸", now=NOW) == 50.0
    assert engine.injection_line(PID, now=NOW) == ""
    assert store.get_affinity(PID) is None


def test_norecord_person_is_ignored(store):
    store.privacy_enabled = True
    store.upsert_person("recording", "u2", "阿草")
    store.set_privacy_mode("recording:u2", "norecord", now=NOW)
    engine = EmotionEngine(store, EmotionConfig(), now_fn=lambda: NOW)
    assert engine.record_chat_round("recording:u2", now=NOW) == 0.0
    assert engine.apply_delta("recording:u2", 1.0, reason="被夸", now=NOW) == 50.0
    assert store.get_affinity("recording:u2") is None
    assert engine.injection_line("recording:u2", now=NOW) == ""


# ----------------------------------------------------------------------
# 情绪：事件 → 状态
# ----------------------------------------------------------------------


def test_praise_reacts_happy_at_point_six(engine):
    result = engine.observe_message(person_id=PID, text="你真厉害", addressed=True, now=NOW)
    assert result == {"praise": True, "insult": False}
    state = engine.current_mood(now=NOW)
    assert (state.mood, state.intensity) == ("开心", pytest.approx(0.6))
    # 被夸也加好感
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(51.0)


def test_insult_reacts_wronged_and_skips_praise(engine):
    result = engine.observe_message(person_id=PID, text="你真傻逼", addressed=True, now=NOW)
    assert result == {"praise": False, "insult": True}
    state = engine.current_mood(now=NOW)
    assert (state.mood, state.intensity) == ("委屈", pytest.approx(0.6))
    assert engine.current_affinity(PID, now=NOW) == pytest.approx(48.0)


def test_message_not_addressed_to_bot_is_ignored(engine):
    result = engine.observe_message(person_id=PID, text="你真厉害", addressed=False, now=NOW)
    assert result == {"praise": False, "insult": False}
    assert engine.current_mood(now=NOW).mood == MOOD_CALM
    assert engine.current_affinity(PID, now=NOW) == 50.0


def test_mood_decays_toward_calm(engine):
    engine.react("开心", 0.6, now=NOW)
    assert engine.current_mood(now=NOW + 3600).intensity == pytest.approx(0.3)
    # 两小时后仍高于阈值
    assert engine.current_mood(now=NOW + 2 * 3600).mood == "开心"
    # 五小时后已低于阈值（0.6 / 32 ≈ 0.019）：回到平静
    state = engine.current_mood(now=NOW + 5 * 3600)
    assert state.mood == MOOD_CALM and state.intensity == 0.0


def test_same_mood_stacks_keeps_since_and_max(engine, store):
    engine.react("开心", 0.3, now=NOW)
    engine.react("开心", 0.5, now=NOW + 600)
    row = store.get_mood()
    assert row.intensity == pytest.approx(0.5)
    assert row.since == pytest.approx(NOW)


def test_boredom_after_three_hours(engine):
    engine.note_engaged(now=NOW)
    assert engine.current_mood(now=NOW + 2 * 3600).mood == MOOD_CALM
    state = engine.current_mood(now=NOW + 3 * 3600)
    assert (state.mood, state.intensity) == ("无聊", pytest.approx(0.3))


def test_boredom_waits_for_strong_mood_to_fade(engine):
    engine.note_engaged(now=NOW)
    engine.react("开心", 0.6, now=NOW)
    # 三小时时开心虽然淡了但只到 0.6/8=0.075（低于阈值，算平静）；
    # 这里换一个很强的开心，验证「三小时没聊」+「情绪还很浓」时不会判无聊。
    engine.react("开心", 0.9, now=NOW + 3 * 3600)
    assert engine.current_mood(now=NOW + 3 * 3600).mood == "开心"
    # 每过一小时减半：0.225、0.1125 都还浓
    assert engine.current_mood(now=NOW + 4 * 3600).mood == "开心"
    assert engine.current_mood(now=NOW + 6 * 3600).mood == "开心"
    # 到 0.056 时（低于阈值）才轮到无聊顶上
    assert engine.current_mood(now=NOW + 7 * 3600).mood == "无聊"


def test_no_boredom_without_engagement_baseline(engine):
    assert engine.current_mood(now=NOW + 10 * 3600).mood == MOOD_CALM


def test_ensure_tracking_only_writes_once(engine, store):
    engine.ensure_tracking(now=NOW)
    engine.ensure_tracking(now=NOW + 99)
    assert store.get_meta_float(LAST_ENGAGED_KEY) == pytest.approx(NOW)


def test_excitement_when_group_lively(engine, store):
    for index in range(6):
        store.add_raw_message(
            platform="recording", chat_id="g1", sender_id=f"u{index}", sender_name="某人",
            text=f"消息{index}", ts=NOW - 10 + index,
        )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="__bot__", sender_name="Ruben",
        text="我在", ts=NOW - 5, processed=True,
    )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="__bot__", sender_name="Ruben",
        text="真的", ts=NOW - 4, processed=True,
    )
    assert engine.maybe_excited("recording", "g1", now=NOW) is True
    state = engine.current_mood(now=NOW)
    assert (state.mood, state.intensity) == ("兴奋", pytest.approx(0.4))


def test_excitement_needs_bot_participation(engine, store):
    for index in range(8):
        store.add_raw_message(
            platform="recording", chat_id="g1", sender_id=f"u{index}", sender_name="某人",
            text=f"消息{index}", ts=NOW - 10 + index,
        )
    assert engine.maybe_excited("recording", "g1", now=NOW) is False
    assert engine.current_mood(now=NOW).mood == MOOD_CALM


def test_excitement_does_not_override_strong_mood(engine, store):
    engine.react("委屈", 0.6, now=NOW)
    for index in range(6):
        store.add_raw_message(
            platform="recording", chat_id="g1", sender_id=f"u{index}", sender_name="某人",
            text=f"消息{index}", ts=NOW - 10 + index,
        )
    for index in range(2):
        store.add_raw_message(
            platform="recording", chat_id="g1", sender_id="__bot__", sender_name="Ruben",
            text=f"我说{index}", ts=NOW - 5 + index, processed=True,
        )
    assert engine.maybe_excited("recording", "g1", now=NOW) is False
    assert engine.current_mood(now=NOW).mood == "委屈"


# ----------------------------------------------------------------------
# 心情仍能展示和注入，不参与是否开口的计算。
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mood", ["开心", "兴奋", "无聊", "委屈", "低落"])
def test_displayed_mood_matches_injected_mood(engine, mood):
    engine.react(mood, 0.6, now=NOW)
    displayed = engine.status(now=NOW)["mood"]
    assert displayed["mood"] == mood
    assert f"{mood}（{displayed['phrase']}）" in engine.injection_line(PID, now=NOW)


def test_displayed_intensity_decays_without_writing(engine, store):
    engine.react("开心", .6, now=NOW)
    assert engine.status(now=NOW + 3600)["mood"]["intensity"] == pytest.approx(.3)
    assert store.get_mood().intensity == pytest.approx(.6)


def test_custom_praise_mood_is_used_in_context(store):
    engine = EmotionEngine(store, EmotionConfig(praise_mood="兴奋"), now_fn=lambda: NOW)
    engine.observe_message(person_id=PID, text="你真厉害", addressed=True, now=NOW)
    assert "心情：兴奋" in engine.injection_line(PID, now=NOW)


# ----------------------------------------------------------------------
# 注入行
# ----------------------------------------------------------------------


def test_injection_line_omitted_when_calm_and_neutral(engine):
    assert engine.injection_line(PID, now=NOW) == ""


def test_injection_line_has_mood_and_band(engine):
    engine.react("开心", 0.6, now=NOW)
    engine.apply_delta(PID, 31.0, reason="被夸", now=NOW)  # 81 → 亲近
    line = engine.injection_line(PID, now=NOW)
    assert "心情：开心（挺明显）" in line
    assert "对当前说话人：亲近" in line
    assert line.startswith("【你现在的状态】")


def test_injection_line_band_only(engine):
    engine.apply_delta(PID, -31.0, reason="被骂", now=NOW)  # 19 → 疏远
    line = engine.injection_line(PID, now=NOW)
    assert "心情" not in line
    assert "对当前说话人：疏远" in line


def test_injection_line_mood_only(engine):
    engine.react("无聊", 0.3, now=NOW)
    line = engine.injection_line(PID, now=NOW)
    assert "心情：无聊（淡淡的）" in line
    assert "对当前说话人" not in line


# ----------------------------------------------------------------------
# 存储侧
# ----------------------------------------------------------------------


def test_purge_removes_affinity_and_logs(store):
    engine = EmotionEngine(store, EmotionConfig(), now_fn=lambda: NOW)
    engine.apply_delta(PID, 1.0, reason="被夸", now=NOW)
    counts = store.purge_person(PID, now=NOW)
    assert counts["affinity"] == 1
    assert store.get_affinity(PID) is None
    assert store.list_affinity_logs(person_id=PID) == []


def test_daily_gain_only_counts_positive(store):
    store.add_affinity_log(PID, 1.0, reason="对话", now=NOW)
    store.add_affinity_log(PID, -2.0, reason="被骂", now=NOW)
    store.add_affinity_log(PID, 5.0, reason="被夸", now=NOW)
    assert store.daily_affinity_gain(PID, NOW - 1) == pytest.approx(6.0)
    assert store.daily_affinity_gain(PID, NOW - 1, reason="对话") == pytest.approx(1.0)


def test_mood_single_row_upsert(store):
    store.set_mood("开心", 0.6, now=NOW)
    store.set_mood("委屈", 0.4, now=NOW + 1)
    row = store.get_mood()
    assert (row.mood, row.intensity) == ("委屈", pytest.approx(0.4))


# ----------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------


def test_emotion_config_defaults():
    config = Config()
    assert config.emotion.enabled is True
    assert config.emotion.chat_delta == pytest.approx(0.2)
    assert config.emotion.chat_daily_cap == pytest.approx(2.0)
    assert config.emotion.praise_delta == pytest.approx(1.0)
    assert config.emotion.insult_delta == pytest.approx(-2.0)
    assert config.emotion.decay_weekly == pytest.approx(0.05)


def test_emotion_config_parsed_and_lenient():
    config = Config.from_dict(
        {
            "emotion": {
                "enabled": "false",
                "chat_delta": "0.5",
                "praise_delta": "不是数字",
                "praise_mood": None,
            }
        }
    )
    emotion = config.emotion
    assert emotion.enabled is False
    assert emotion.chat_delta == pytest.approx(0.5)
    assert emotion.praise_delta == pytest.approx(1.0)  # 写错回落默认
    assert emotion.praise_mood == "开心"  # None 回落默认


# ----------------------------------------------------------------------
# 主循环接线
# ----------------------------------------------------------------------


def test_private_message_counts_as_chat_round(config, store):
    bot, adapter = make_bot(config, store)
    asyncio.run(bot.handle_event(private_event("在吗")))
    assert adapter.texts == ["在的"]
    engine = bot.emotion
    assert engine.current_affinity(PID) == pytest.approx(50.2)


def test_group_chatter_not_addressed_does_not_gain(config, store):
    config.attention.talk_value = 0.0
    bot, _ = make_bot(config, store)
    asyncio.run(bot.handle_event(group_event("今天食堂真难吃")))
    assert bot.store.get_affinity(PID) is None


def test_mention_counts_as_chat_round(config, store):
    bot, _ = make_bot(config, store)
    asyncio.run(bot.handle_event(group_event("Ruben 你在吗", mentions=True)))
    assert bot.emotion.current_affinity(PID) == pytest.approx(50.2)


def test_praise_in_private_updates_mood_and_affinity(config, store):
    bot, _ = make_bot(config, store)
    asyncio.run(bot.handle_event(private_event("你真的很厉害")))
    assert bot.emotion.current_mood().mood == "开心"
    # +1 被夸，+0.2 对话（同一轮里各记一笔）
    assert bot.emotion.current_affinity(PID) == pytest.approx(51.2)


def test_merged_round_counts_one_chat_round_and_sees_late_praise(config, store):
    """连发两条 @ 只算一轮对话；第二条的「谢谢」也要认出来。"""
    config.decision.merge_window_seconds = 5
    config.decision.hard_merge_window_seconds = 5
    bot, _ = make_bot(config, store)
    first = group_event("Ruben 你在吗", mentions=True)
    second = group_event("谢谢你", mentions=False)
    now = 1_000_000.0
    bot.merger.add(first, now, hard=True, urgent=True)
    assert bot.merger.add(second, now + 1, hard=False) == []
    # 两条在同一批里；@ 上限让这批在「首条 + urgent_seconds」时放出
    ready = bot.merger.pop_ready(now + 2)
    assert len(ready) == 1  # 同一人在窗口内合并成一轮
    asyncio.run(bot.process_round(ready[0]))
    # 对话轮只记一次
    chat_logs = [
        entry for entry in store.list_affinity_logs(person_id=PID) if entry.reason == "对话"
    ]
    assert len(chat_logs) == 1
    # 第二轮里的「谢谢」被识别为感谢
    praise_logs = [
        entry for entry in store.list_affinity_logs(person_id=PID) if entry.reason == "被夸"
    ]
    assert len(praise_logs) == 1


def test_injection_line_in_system_prompt(config, store):
    llm = ScriptedLLM({"chat": ["谢谢"]})
    bot, _ = make_bot(config, store, llm)
    bot.emotion.react("开心", 0.6)
    asyncio.run(bot.handle_event(private_event("在吗")))
    system = llm.chat_messages[0][0]["content"]
    assert "【你现在的状态】" in system
    assert "心情：开心" in system


def test_injection_omitted_when_neutral(config, store):
    llm = ScriptedLLM({"chat": ["嗯"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("在吗")))
    assert "【你现在的状态】" not in llm.chat_messages[0][0]["content"]


def test_emotion_failure_does_not_break_reply(config, store, monkeypatch):
    bot, adapter = make_bot(config, store)

    def boom(*args, **kwargs):
        raise RuntimeError("情绪模块崩了")

    monkeypatch.setattr(bot.emotion, "observe_message", boom)
    asyncio.run(bot.handle_event(private_event("在吗")))
    assert adapter.texts == ["在的"]


def test_mood_does_not_override_refractory_guard(config, store):
    bot, _ = make_bot(config, store)
    bot.decision_engine.note_bot_reply("recording:g1", NOW)
    bot.emotion.react("开心", .6, now=NOW)
    assert bot.decide(group_event(), now=NOW + 1).reason == "refractory"
    assert bot.decide(group_event(mentions=True), now=NOW + 1).reason == "mentioned"


def test_mood_does_not_override_planner_silence(config, store):
    bot, adapter = make_bot(config, store)
    bot.llm.responses["fast"] = ['{"basis":"settled","speak":false,"reason":"在听","intent":"沉默"}']
    bot.emotion.react("兴奋", 0.6)
    asyncio.run(bot.handle_event(group_event("今天好累")))
    entry, = store.list_decision_logs()
    assert not adapter.sent and entry["reason"] == "planner_silence"
    assert entry["factors"]["planner"]["reason"] == "在听"
    assert "心情：兴奋" in bot.llm.calls[0]["messages"][0]["content"]


def test_run_initializes_engagement_baseline(config, store):
    bot, _ = make_bot(config, store)
    asyncio.run(bot.run())
    assert store.get_meta_float(LAST_ENGAGED_KEY) > 0


# ----------------------------------------------------------------------
# WebUI 只读状态
# ----------------------------------------------------------------------


def test_status_top_bottom_and_recent(engine, store):
    for index in range(8):
        uid = f"p{index}"
        store.upsert_person("recording", uid, f"人{index}", now=NOW)
        store.set_affinity(f"recording:{uid}", 40 + index * 6, now=NOW)  # 40..82
    payload = engine.status(now=NOW)
    top = payload["affinity"]["top"]
    bottom = payload["affinity"]["bottom"]
    assert [item["score"] for item in top] == [82, 76, 70, 64, 58]
    # Bottom3 只从 Top5 之外取，不会与 Top5 重复
    assert [item["score"] for item in bottom] == [40, 46, 52]
    assert {item["person_id"] for item in top}.isdisjoint({item["person_id"] for item in bottom})
    assert payload["affinity"]["tracked"] == 8


def test_status_recent_changes(engine, store):
    engine.apply_delta(PID, 1.0, reason="被夸", now=NOW)
    payload = engine.status(now=NOW)
    assert payload["recent_changes"][0]["delta"] == pytest.approx(1.0)
    assert payload["recent_changes"][0]["display_name"] == "阿花"
    assert payload["recent_changes"][0]["reason"] == "被夸"


def test_status_scores_are_decayed(engine, store):
    store.set_affinity(PID, 80.0, now=NOW)
    payload = engine.status(now=NOW + WEEK)
    assert payload["affinity"]["top"][0]["score"] == pytest.approx(78.5)
    assert payload["affinity"]["top"][0]["band"] == "友好"
