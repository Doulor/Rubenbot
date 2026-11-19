"""记忆 v2 提取分流：facts/events 落对表，audience/sensitivity/稳定性标注，
时间线索推断，以及不记模式跳过。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.extract import (
    audience_for_chat,
    parse_extraction,
    resolve_event_ts,
)
from rubenbot.memory.store import MemoryStore

BASE_TS = 1_795_000_000.0  # 2026-11-18 前后（固定，避免依赖运行日期）


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages, "json_mode": json_mode})
        return self.responses.pop(0) if self.responses else '{"facts": [], "events": []}'


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def seed(store, sender="u1", texts=("我昨天去爬山了，累死了",), chat_id="g1", chat_type="group", ts=BASE_TS):
    for index, text in enumerate(texts):
        store.add_raw_message(
            platform="console",
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender,
            sender_name="阿花",
            text=text,
            ts=ts + index,
        )


def payload(facts=(), events=()) -> str:
    return json.dumps({"facts": list(facts), "events": list(events)}, ensure_ascii=False)


def test_facts_and_events_go_to_their_tables(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store, texts=("我住在杭州", "昨天下班去看了场电影"))
    llm = FakeLLM(
        payload(
            facts=[{"content": "TA 住在杭州", "category": "identity", "scope": "global"}],
            events=[
                {"summary": "TA 昨天下班去看了场电影", "time_hint": "昨天", "salience": 0.5},
            ],
        )
    )
    extractor = MemoryExtractor(llm, store, MemoryConfig())
    result = asyncio.run(extractor.maybe_extract(now=BASE_TS + 10, force=True))[0]

    assert result.added == ["TA 住在杭州"]
    assert result.added_events == ["TA 昨天下班去看了场电影"]
    assert store.count_memories("console:u1") == 1
    events = store.list_events(person_id="console:u1")
    assert [e.summary for e in events] == ["TA 昨天下班去看了场电影"]
    event = events[0]
    # 时间线索「昨天」→ 消息前一天（钟点按线索默认，不精确到秒）
    assert abs(event.ts - (BASE_TS - 86400)) < 86400
    assert event.ts < BASE_TS
    assert event.salience == 0.5
    assert event.compressed is False
    assert event.kind == "event"


def test_audience_and_sensitivity_annotations(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store, texts=("别跟别人说，我最近在吃抗抑郁药",), chat_id="p1", chat_type="private")
    llm = FakeLLM(
        payload(
            facts=[
                {
                    "content": "TA 最近在吃抗抑郁药",
                    "category": "fact",
                    "sensitivity": "sealed",
                }
            ],
            events=[
                {
                    "summary": "TA 说自己最近在吃抗抑郁药",
                    "sensitivity": "sealed",
                    "salience": 0.9,
                }
            ],
        )
    )
    MemoryExtractor(llm, store, MemoryConfig()).extract_sender  # noqa: B018 - 只为可读性
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))

    fact = store.list_memories(person_id="console:u1")[0]
    assert fact.audience == "private"
    assert fact.origin_chat == "console:p1"
    assert fact.sensitivity == "sealed"
    event = store.list_events(person_id="console:u1")[0]
    assert event.audience == "private"
    assert event.source_chat == "console:p1"
    assert event.sensitivity == "sealed"
    assert event.salience == 0.9


def test_group_audience_and_high_sensitivity(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store)
    llm = FakeLLM(
        payload(
            facts=[{"content": "TA 最近在攒钱买房", "sensitivity": "high"}],
            events=[{"summary": "TA 说自己在攒钱买房", "salience": 0.7}],
        )
    )
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))
    fact = store.list_memories(person_id="console:u1")[0]
    assert fact.audience == "group:g1"
    assert fact.sensitivity == "high"
    event = store.list_events(person_id="console:u1")[0]
    assert event.audience == "group:g1"
    assert event.sensitivity == "normal"


def test_stability_and_expiry_for_temporal_facts(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store, texts=("我明天要出差一周",))
    llm = FakeLLM(
        payload(
            facts=[
                {"content": "TA 明天出差", "stability": "temporal", "expires_in_days": 7},
                {"content": "TA 可能养了只猫", "stability": "uncertain"},
            ]
        )
    )
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))
    facts = {m.content: m for m in store.list_memories(person_id="console:u1")}
    temporal = facts["TA 明天出差"]
    assert temporal.stability == "temporal"
    assert temporal.expires_at == pytest.approx(BASE_TS + 7 * 86400)
    uncertain = facts["TA 可能养了只猫"]
    assert uncertain.stability == "uncertain"
    assert uncertain.expires_at is None


def test_temporal_expiry_days_are_clamped(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store, texts=("我下周要搬家",))
    llm = FakeLLM(
        payload(
            facts=[
                {"content": "TA 下周搬家", "stability": "temporal", "expires_in_days": 999},
                {"content": "TA 今天有点忙", "stability": "temporal", "expires_in_days": 0},
            ]
        )
    )
    cfg = MemoryConfig(status_expiry_min_days=1, status_expiry_max_days=30)
    asyncio.run(MemoryExtractor(llm, store, cfg).maybe_extract(now=BASE_TS + 10, force=True))
    facts = {m.content: m for m in store.list_memories(person_id="console:u1")}
    assert facts["TA 下周搬家"].expires_at == pytest.approx(BASE_TS + 30 * 86400)
    assert facts["TA 今天有点忙"].expires_at == pytest.approx(BASE_TS + 1 * 86400)


def test_norecord_skips_facts_and_events(store):
    from rubenbot.memory.extract import MemoryExtractor

    store.set_privacy_mode("console:u1", "norecord")
    seed(store)
    llm = FakeLLM(payload(facts=[{"content": "TA 住在杭州"}], events=[{"summary": "TA 去爬山了"}]))
    result = asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))[0]
    assert result.skipped_norecord is True
    assert store.count_memories("console:u1") == 0
    assert store.list_events(person_id="console:u1") == []
    # 模型根本没被调用，消息也标记完成了，避免反复尝试
    assert llm.calls == []
    assert store.pending_senders() == []


def test_private_mode_still_records(store):
    """隐私模式（private）不等于不记：仍正常落库，由读取侧审查。"""
    from rubenbot.memory.extract import MemoryExtractor

    store.set_privacy_mode("console:u1", "private")
    seed(store)
    llm = FakeLLM(payload(facts=[{"content": "TA 住在杭州"}]))
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))
    assert store.count_memories("console:u1") == 1


def test_event_dedup_within_a_day(store):
    from rubenbot.memory.extract import MemoryExtractor

    seed(store, texts=("我在学吉他",))
    llm = FakeLLM(payload(events=[{"summary": "TA 在学吉他"}]))
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))

    seed(store, texts=("最近还在弹吉他",), ts=BASE_TS + 3600)
    llm2 = FakeLLM(payload(events=[{"summary": "TA 在学吉他"}]))
    result = asyncio.run(
        MemoryExtractor(llm2, store, MemoryConfig()).maybe_extract(now=BASE_TS + 4000, force=True)
    )[0]
    assert result.added_events == []
    assert result.skipped_events == 1
    assert len(store.list_events(person_id="console:u1")) == 1


def test_self_events_are_separate(store):
    event = store.add_self_event("晚上好呀", source_chat="console:g1", audience="group:g1")
    assert event.person_id == "self"
    assert store.list_events(person_id="self")[0].summary == "晚上好呀"
    assert store.list_events(person_id="console:u1") == []


# ----------------------------------------------------------------------
# 解析与时间推断
# ----------------------------------------------------------------------


def test_parse_extraction_robust():
    raw = """好的：
```json
{"facts": [{"content": "TA 养猫", "stability": "temporal", "expires_in_days": "5"},
           {"content": ""}, "TA 讨厌香菜"],
 "events": [{"summary": "TA 去看了演唱会", "salience": 1.5},
            {"content": "TA 昨天加班"}, "TA 今天请假"]}
```"""
    parsed = parse_extraction(raw)
    assert [f.content for f in parsed.facts] == ["TA 养猫", "TA 讨厌香菜"]
    assert parsed.facts[0].stability == "temporal"
    assert parsed.facts[0].expires_in_days == 5
    assert [e.summary for e in parsed.events] == ["TA 去看了演唱会", "TA 昨天加班", "TA 今天请假"]
    assert parsed.events[0].salience == 1.0  # 夹到 0-1
    # 旧格式（纯数组）仍按事实解析
    assert [f.content for f in parse_extraction('[{"content": "TA 会弹琴"}]').facts] == ["TA 会弹琴"]
    assert parse_extraction("不是 JSON").facts == []


def test_resolve_event_ts_uses_message_time_when_unknown():
    assert resolve_event_ts("", BASE_TS) == BASE_TS
    assert resolve_event_ts("某天", BASE_TS) == BASE_TS
    # 编造的远古年份回退到消息时间
    assert resolve_event_ts("1990-01-01", BASE_TS) == BASE_TS


def test_resolve_event_ts_relative_days():
    yesterday = resolve_event_ts("昨天下午", BASE_TS)
    assert abs(yesterday - (BASE_TS - 86400)) < 86400
    two_days = resolve_event_ts("前天", BASE_TS)
    assert abs(two_days - (BASE_TS - 2 * 86400)) < 86400
    three = resolve_event_ts("3天前", BASE_TS)
    assert abs(three - (BASE_TS - 3 * 86400)) < 86400


def test_resolve_event_ts_absolute_date():
    import datetime

    ts = resolve_event_ts("2026-11-18 20:30", BASE_TS)
    dt = datetime.datetime.fromtimestamp(ts)
    assert (dt.year, dt.month, dt.day) == (2026, 11, 18)


def test_audience_for_chat():
    assert audience_for_chat("private", "p1") == "private"
    assert audience_for_chat("group", "g1") == "group:g1"


def test_extraction_runs_followups_automatically(store):
    """抽取结束后自动跑下游：时间线压缩与画像提炼（无需 core 增加调用点）。"""
    from rubenbot.memory.extract import MemoryExtractor

    # 一条很早的事件，抽取触发后应被压成 digest
    store.add_event("console:u1", "TA 很久以前去过海边", ts=BASE_TS - 30 * 86400)
    seed(store, texts=("我在杭州做后端",))
    llm = FakeLLM(
        payload(facts=[{"content": "TA 在杭州做后端", "category": "identity"}]),
        # 第二轮：画像提炼（followups 里的 distill 调用）
        json.dumps(
            {
                "stable": [{"text": "TA 是杭州的后端工程师", "evidence": ["memory:1"]}],
                "status": [],
                "hooks": ["后端", "杭州", "工作"],
            },
            ensure_ascii=False,
        ),
    )
    asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS + 10, force=True))

    assert [e.kind for e in store.list_events(person_id="console:u1")].count("digest") == 1
    profile = store.get_profile("console:u1")
    assert profile is not None
    assert profile.stable_lines == ["TA 是杭州的后端工程师"]
    assert len(profile.hooks) == 3
    assert [call["role"] for call in llm.calls] == ["extract", "distill"]


def test_followups_run_even_without_pending_messages(store):
    from rubenbot.memory.extract import MemoryExtractor

    store.add_event("console:u1", "TA 很久以前去过海边", ts=BASE_TS - 30 * 86400)
    llm = FakeLLM()
    results = asyncio.run(MemoryExtractor(llm, store, MemoryConfig()).maybe_extract(now=BASE_TS))
    assert results == []
    kinds = [e.kind for e in store.list_events(person_id="console:u1")]
    assert kinds.count("digest") == 1
    # 子事件保留原始摘要，只是标记已压缩
    child = next(e for e in store.list_events(person_id="console:u1") if e.kind == "event")
    assert child.compressed is True
    # 没有待抽取消息时不会调用抽取；新产生的 digest 仍会被画像提炼看到
    assert [call["role"] for call in llm.calls] == ["distill"]
    # 假模型没给出画像 → 保持无画像，不清空/不写空行
    assert store.get_profile("console:u1") is None
