"""事件通道：解析、自身过滤、三档路由、10 秒合并、安静时段与快判分寸。"""

from __future__ import annotations

import asyncio
import json
import time


from rubenbot.adapters.platform import (
    KIND_NOTICE,
    KIND_REQUEST,
    ROUTE_CONDITIONAL,
    ROUTE_JUDGE,
    ROUTE_RECORD,
    PlatformEvent,
)
from rubenbot.adapters.snowluma import SnowLumaAdapter, parse_onebot_platform_event
from rubenbot.config import Config, GatewayConfig
from rubenbot.core import RubenBot
from rubenbot.gateway.events import (
    EventBatch,
    EventMerger,
    EventRouter,
    quiet_now,
)
from rubenbot.gateway.judge import EventJudge
from rubenbot.memory.store import MemoryStore, ProfileLine, make_person_id
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


def notice(**overrides):
    payload = {
        "post_type": "notice",
        "self_id": 10000,
        "notice_type": "notify",
        "sub_type": "poke",
        "group_id": 55555,
        "user_id": 12345,
        "target_id": 10000,
        "sender": {"nickname": "阿花"},
    }
    payload.update(overrides)
    return payload


def friend_request(**overrides):
    payload = {
        "post_type": "request",
        "self_id": 10000,
        "request_type": "friend",
        "user_id": 22222,
        "comment": "加个好友",
        "flag": "flag-1",
    }
    payload.update(overrides)
    return payload


def group_request(**overrides):
    payload = {
        "post_type": "request",
        "self_id": 10000,
        "request_type": "group",
        "sub_type": "add",
        "group_id": 55555,
        "user_id": 22222,
        "comment": "想进群",
        "flag": "flag-2",
    }
    payload.update(overrides)
    return payload


def group_increase(**overrides):
    payload = {
        "post_type": "notice",
        "self_id": 10000,
        "notice_type": "group_increase",
        "sub_type": "approve",
        "group_id": 55555,
        "user_id": 22222,
        "sender": {"nickname": "新人"},
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


def test_parse_poke_targets_bot():
    event = parse_onebot_platform_event(notice(), self_id="10000")
    assert isinstance(event, PlatformEvent)
    assert event.kind == KIND_NOTICE
    assert event.type == "notify"
    assert event.sub_type == "poke"
    assert event.actor_id == "12345"
    assert event.conversation_type == "group"
    assert event.conversation_id == "55555"
    assert event.targets_bot is True
    assert "戳" in event.description


def test_parse_poke_someone_else():
    event = parse_onebot_platform_event(notice(target_id=999), self_id="10000")
    assert event.targets_bot is False


def test_parse_friend_request():
    event = parse_onebot_platform_event(friend_request())
    assert event.kind == KIND_REQUEST
    assert event.type == "friend"
    assert event.actor_id == "22222"
    assert event.conversation_type == "private"
    assert event.conversation_id == "22222"
    assert event.pending is True
    assert event.platform_event_id == "flag-1"
    assert "好友申请" in event.description
    assert event.targets_bot is False


def test_parse_group_request():
    event = parse_onebot_platform_event(group_request())
    assert event.kind == KIND_REQUEST
    assert event.type == "group"
    assert event.sub_type == "add"
    assert event.conversation_type == "group"
    assert "入群申请" in event.description


def test_parse_group_increase_and_ban():
    event = parse_onebot_platform_event(group_increase())
    assert event.type == "group_increase"
    assert "加入了群" in event.description

    ban = parse_onebot_platform_event(
        notice(notice_type="group_ban", sub_type="ban", duration=600, user_id=22222, target_id=None)
    )
    assert "禁言" in ban.description

    recall = parse_onebot_platform_event(
        notice(notice_type="group_recall", sub_type="", user_id=22222, message_id=77)
    )
    assert "撤回" in recall.description
    assert recall.platform_event_id == "77"


def test_parse_ignores_meta_and_message():
    assert parse_onebot_platform_event({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is None
    assert parse_onebot_platform_event({"post_type": "message", "message_type": "group"}) is None
    assert parse_onebot_platform_event({"post_type": "notice"}) is None  # 没有 notice_type


def test_adapter_dispatches_notice_but_ignores_heartbeat():
    received = []

    async def on_platform(event):
        received.append(event)

    async def scenario():
        adapter = SnowLumaAdapter(self_id="10000")
        adapter.set_platform_event_callback(on_platform)
        await adapter._handle_raw(json.dumps(notice()))
        await adapter._handle_raw(json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat"}))
        await adapter._handle_raw(json.dumps(friend_request()))

    asyncio.run(scenario())
    assert len(received) == 2
    assert {event.kind for event in received} == {KIND_NOTICE, KIND_REQUEST}


# ----------------------------------------------------------------------
# 路由三档
# ----------------------------------------------------------------------


def make_router(**kwargs) -> EventRouter:
    return EventRouter(GatewayConfig(quiet_hours=""), **kwargs)


def test_route_judge_for_poke_requests():
    router = make_router()
    poke = parse_onebot_platform_event(notice(), self_id="10000")
    assert router.route(poke, now=time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))).route == ROUTE_JUDGE
    assert router.route(parse_onebot_platform_event(friend_request())).route == ROUTE_JUDGE
    assert router.route(parse_onebot_platform_event(group_request())).route == ROUTE_JUDGE


def test_route_record_for_other_notices():
    router = make_router()
    for payload in (
        notice(notice_type="group_recall", message_id=1, target_id=None),
        notice(notice_type="group_decrease", sub_type="leave", target_id=None),
        notice(notice_type="group_admin", sub_type="set", target_id=None),
        notice(target_id=999),  # 戳别人
    ):
        event = parse_onebot_platform_event(payload)
        assert router.route(event).route == ROUTE_RECORD, payload


def test_route_group_increase_conditional_or_record():
    event = parse_onebot_platform_event(group_increase())
    # 没有任何活动记录 / 画像：只记录
    assert make_router().route(event).route == ROUTE_RECORD
    # 群里有 bot 活动记录：条件判
    router = make_router(activity_fn=lambda platform, chat_id: chat_id == "55555")
    assert router.route(event).route == ROUTE_CONDITIONAL
    # 进群者在画像里存在：条件判
    router = make_router(profile_fn=lambda platform, actor: actor == "22222")
    assert router.route(event).route == ROUTE_CONDITIONAL


def test_route_quiet_hours_hangs_everything():
    quiet = GatewayConfig(quiet_hours="23:00-08:00")
    router = EventRouter(quiet)
    # 2026-11-20 23:30 本地时间
    late = time.mktime((2026, 11, 20, 23, 30, 0, 0, 0, -1))
    assert quiet_now(quiet, late) is True
    poke = parse_onebot_platform_event(notice(), self_id="10000")
    decision = router.route(poke, now=late)
    assert decision.quiet is True
    assert decision.route == ROUTE_JUDGE  # 档位不丢，只是不当场动作
    assert decision.judge is False
    # 白天同一事件：正常判
    noon = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
    assert router.route(poke, now=noon).judge is True


def test_quiet_hours_cross_midnight():
    cfg = GatewayConfig(quiet_hours="23:00-08:00")
    assert quiet_now(cfg, time.mktime((2026, 11, 20, 7, 59, 0, 0, 0, -1))) is True
    assert quiet_now(cfg, time.mktime((2026, 11, 20, 8, 0, 0, 0, 0, -1))) is False
    assert quiet_now(cfg, time.mktime((2026, 11, 20, 22, 59, 0, 0, 0, -1))) is False
    assert quiet_now(GatewayConfig(quiet_hours=""), time.mktime((2026, 11, 20, 2, 0, 0, 0, 0, -1))) is False


# ----------------------------------------------------------------------
# 10 秒合并
# ----------------------------------------------------------------------


def make_event(actor="22222", type_= "notify", sub="poke") -> PlatformEvent:
    return parse_onebot_platform_event(
        notice(actor_id=None, user_id=actor, notice_type=type_, sub_type=sub, target_id=10000),
        self_id="10000",
    )


def test_merger_combines_same_conversation_within_window():
    merger = EventMerger(window=10.0)
    route = EventRouter(GatewayConfig(quiet_hours="")).route(make_event())
    assert merger.add(make_event(), route, now=100.0) == []
    assert merger.add(make_event(), route, now=105.0) == []
    ready = merger.pop_ready(now=111.0)
    assert len(ready) == 1
    assert len(ready[0].events) == 2
    assert "2 条同类事件" in ready[0].description


def test_merger_separates_different_conversations_or_after_window():
    merger = EventMerger(window=10.0)
    route = EventRouter(GatewayConfig(quiet_hours="")).route(make_event())
    merger.add(make_event(), route, now=100.0)
    merger.add(make_event(actor="33333"), route, now=100.0)  # 不同人、同群同类 => 同一批
    merger.add(make_event(type_="group_recall", sub=""), route, now=100.0)  # 不同类 => 新批
    ready = merger.pop_ready(now=111.0)
    assert len(ready) == 2
    assert {len(batch.events) for batch in ready} == {1, 2}
    # 窗口过期后再来的事件另起一批
    merger.add(make_event(), route, now=120.0)
    assert len(merger) == 1
    assert merger.pop_ready(now=131.0)[0].events[0].actor_id == "22222"


def test_merger_zero_window_returns_immediately():
    merger = EventMerger(window=0)
    route = EventRouter(GatewayConfig(quiet_hours="")).route(make_event())
    ready = merger.add(make_event(), route, now=1.0)
    assert len(ready) == 1 and len(ready[0].events) == 1
    assert len(merger) == 0


# ----------------------------------------------------------------------
# core 入口：落库、自身过滤、pending、快判接线
# ----------------------------------------------------------------------


class SnowlumaRecorder(RecordingAdapter):
    """平台名对齐 snowluma 的假适配器：事件从 snowluma 来，回的也得是它。"""

    name = "snowluma"


def make_bot(config: Config, store: MemoryStore, llm) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben。"))
    adapter = SnowlumaRecorder()
    adapter.self_id = "10000"
    bot.register_adapter(adapter)
    return bot, adapter


def gateway_config(*, judge=False, quiet="") -> Config:
    cfg = Config()
    cfg.chat.type_speed = 0
    cfg.decision.merge_window_seconds = 0
    cfg.decision.hard_merge_window_seconds = 0
    cfg.gateway = GatewayConfig(enabled=False, judge_events=judge, quiet_hours=quiet)
    return cfg


def run(coro):
    return asyncio.run(coro)


def test_event_stored_and_pending_counted(store):
    cfg = gateway_config()
    bot, _ = make_bot(cfg, store, ScriptedLLM())
    event = parse_onebot_platform_event(friend_request())
    run(bot._handle_platform_event(event))
    rows = store.list_platform_events()
    assert len(rows) == 1
    assert rows[0]["kind"] == "request"
    assert rows[0]["pending"] == 1
    assert store.count_pending_platform_events() == 1
    # 时间线也有一条
    assert store.list_events(person_id=make_person_id("snowluma", "22222"))


def test_event_self_generated_filtered(store):
    cfg = gateway_config()
    bot, _ = make_bot(cfg, store, ScriptedLLM())
    event = parse_onebot_platform_event(notice(user_id=10000), self_id="10000")
    run(bot._handle_platform_event(event))
    assert store.list_platform_events() == []


def test_event_judging_off_by_default(store):
    cfg = gateway_config(judge=False)
    llm = ScriptedLLM({"fast": ['{"verdict": "ignore"}']})
    bot, _ = make_bot(cfg, store, llm)
    event = parse_onebot_platform_event(notice(), self_id="10000")
    run(bot._handle_platform_event(event))
    assert store.list_platform_events()
    assert [call for call in llm.calls if call["role"] == "fast"] == []


def test_event_judging_runs_when_enabled(store):
    cfg = gateway_config(judge=True)
    llm = ScriptedLLM({"fast": ['{"verdict": "ignore", "reason": "小事"}']})
    bot, _ = make_bot(cfg, store, llm)
    bot.event_merger.window = 0  # 立即判断
    event = parse_onebot_platform_event(notice(), self_id="10000")
    run(bot._handle_platform_event(event))
    assert [call for call in llm.calls if call["role"] == "fast"]


def test_event_quiet_hours_not_judged(store):
    late = time.mktime((2026, 11, 20, 23, 30, 0, 0, 0, -1))
    cfg = gateway_config(judge=True, quiet="23:00-08:00")
    llm = ScriptedLLM({"fast": ['{"verdict": "message", "payload": {"text": "hi"}}']})
    bot, adapter = make_bot(cfg, store, llm)
    bot.event_merger.window = 0
    event = parse_onebot_platform_event(notice(), self_id="10000")
    run(bot._handle_platform_event(event, now=late))
    assert [call for call in llm.calls if call["role"] == "fast"] == []
    assert store.list_platform_events()
    assert adapter.sent == []


def test_event_recorded_but_not_judged_when_route_record(store):
    cfg = gateway_config(judge=True)
    llm = ScriptedLLM({"fast": ['{"verdict": "message", "payload": {"text": "hi"}}']})
    bot, _ = make_bot(cfg, store, llm)
    bot.event_merger.window = 0
    event = parse_onebot_platform_event(notice(target_id=999), self_id="10000")
    run(bot._handle_platform_event(event))
    assert [call for call in llm.calls if call["role"] == "fast"] == []
    assert store.list_platform_events()


def test_event_batch_judged_after_window(store):
    cfg = gateway_config(judge=True)
    llm = ScriptedLLM({"fast": ['{"verdict": "ignore"}']})
    bot, _ = make_bot(cfg, store, llm)
    bot.event_merger.window = 10
    now = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
    event = parse_onebot_platform_event(notice(), self_id="10000")
    run(bot._handle_platform_event(event, now=now))
    assert [call for call in llm.calls if call["role"] == "fast"] == []
    run(bot.flush_event_batches(now=now + 11))
    assert [call for call in llm.calls if call["role"] == "fast"]


def test_message_verdict_sends_short_message(store):
    cfg = gateway_config(judge=True)
    llm = ScriptedLLM({"fast": ['{"verdict": "message", "payload": {"text": "谁戳我"}}']})
    bot, adapter = make_bot(cfg, store, llm)
    bot.event_merger.window = 0
    bot.config.gateway.enabled = True
    event = parse_onebot_platform_event(notice(), self_id="10000")
    run(bot._handle_platform_event(event))
    assert adapter.texts == ["谁戳我"]


def test_notify_owner_falls_back_to_recent_private_chat(store):
    cfg = gateway_config(judge=True)
    llm = ScriptedLLM({"fast": []})
    bot, adapter = make_bot(cfg, store, llm)
    store.add_raw_message(
        platform="snowluma", chat_id="owner", chat_type="private", sender_id="owner", text="在吗"
    )
    event = parse_onebot_platform_event(friend_request())
    ok = run(bot.notify_owner(event, "有人加好友，待你处理"))
    assert ok is True
    assert adapter.sent[-1][0] == "owner"
    assert adapter.sent[-1][1] == "private"


def test_notify_owner_without_private_chat_only_logs(store, caplog):
    cfg = gateway_config()
    bot, adapter = make_bot(cfg, store, ScriptedLLM())
    event = parse_onebot_platform_event(friend_request())
    with caplog.at_level("INFO"):
        assert run(bot.notify_owner(event, "有人加好友")) is False
    assert adapter.sent == []
    assert any("通知只落库" in record.message for record in caplog.records)


# ----------------------------------------------------------------------
# 快判分寸：request 类强制降级
# ----------------------------------------------------------------------


def make_batch(event: PlatformEvent) -> EventBatch:
    return EventBatch(key=event.dedupe_key(), events=[event], route=ROUTE_JUDGE)


def test_request_verdicts_are_downgraded_to_notify_owner():
    judge = EventJudge(ScriptedLLM(), MemoryStore(":memory:"))
    batch = make_batch(parse_onebot_platform_event(friend_request()))
    verdict = judge.parse('{"verdict": "react", "payload": {"message_id": 1, "emoji_id": "66"}}', batch)
    assert verdict.verdict == "notify_owner"
    assert verdict.downgraded is True
    assert "好友申请" in verdict.payload["text"]

    verdict = judge.parse('{"verdict": "gateway", "payload": {"action": "set_friend_add_request"}}', batch)
    assert verdict.verdict == "notify_owner"

    verdict = judge.parse('{"verdict": "message", "payload": {"text": "同意"}}', batch)
    assert verdict.verdict == "notify_owner"


def test_request_verdict_ignore_stays():
    judge = EventJudge(ScriptedLLM(), MemoryStore(":memory:"))
    batch = make_batch(parse_onebot_platform_event(group_request()))
    assert judge.parse('{"verdict": "ignore"}', batch).verdict == "ignore"
    assert judge.parse('{"verdict": "notify_owner", "payload": {"text": "看下"}}', batch).verdict == "notify_owner"
    assert judge.parse('{"verdict": "notify_owner", "payload": {"text": "看下"}}', batch).downgraded is False


def test_non_request_verdicts_untouched():
    judge = EventJudge(ScriptedLLM(), MemoryStore(":memory:"))
    batch = make_batch(parse_onebot_platform_event(notice(), self_id="10000"))
    verdict = judge.parse('{"verdict": "react", "payload": {"message_id": 1, "emoji_id": "66"}}', batch)
    assert verdict.verdict == "react"
    assert verdict.downgraded is False


def test_unknown_verdict_or_bad_json_is_ignore():
    judge = EventJudge(ScriptedLLM(), MemoryStore(":memory:"))
    batch = make_batch(parse_onebot_platform_event(notice(), self_id="10000"))
    assert judge.parse("不是 JSON", batch).verdict == "ignore"
    assert judge.parse('{"verdict": "explode"}', batch).verdict == "ignore"


def test_judge_prompt_includes_profile_summary(store):
    store.upsert_person("snowluma", "12345", "阿花")
    person_id = make_person_id("snowluma", "12345")
    store.save_profile(person_id, stable_lines=[ProfileLine("喜欢猫")], status_lines=[])
    llm = ScriptedLLM({"fast": ['{"verdict": "ignore"}']})
    judge = EventJudge(llm, store, "你是 Ruben。\n爱音乐")
    batch = make_batch(parse_onebot_platform_event(notice(), self_id="10000"))
    run(judge.judge(batch))
    call = llm.calls[-1]
    assert "喜欢猫" in call["messages"][1]["content"]
    assert "你是 Ruben" in call["messages"][0]["content"]


def test_judge_failure_is_ignore(store):
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    judge = EventJudge(Boom(), store, "")
    verdict = run(judge.judge(make_batch(parse_onebot_platform_event(notice(), self_id="10000"))))
    assert verdict.verdict == "ignore"
