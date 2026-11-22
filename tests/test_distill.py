"""画像提炼：证据链校验、矛盾替换留痕、近况过期夹取、hooks、不记模式。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.distill import (
    ProfileDistiller,
    clamp_expiry_days,
    parse_profile,
)
from rubenbot.memory.store import MemoryStore

NOW = 1_795_000_000.0


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages, "json_mode": json_mode})
        return self.responses.pop(0) if self.responses else "{}"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def seed_events(store, person_id="console:u1", summaries=("TA 昨天去爬山了",), ts=NOW - 3600):
    return [
        store.add_event(
            person_id,
            summary,
            ts=ts + index,
            source_chat="console:g1",
            audience="group:g1",
            now=NOW - 3600,
        )
        for index, summary in enumerate(summaries)
    ]


def proposal(stable=(), status=(), hooks=()) -> str:
    return json.dumps(
        {
            "stable": list(stable),
            "status": list(status),
            "hooks": list(hooks),
        },
        ensure_ascii=False,
    )


def run_distill(store, response, *, cfg=None, person_id="console:u1"):
    llm = FakeLLM(response)
    distiller = ProfileDistiller(llm, store, cfg or MemoryConfig())
    update = asyncio.run(distiller.distill_person(person_id, now=NOW))
    return update, llm


def test_lines_without_evidence_are_dropped(store):
    events = seed_events(store)
    response = proposal(
        stable=[
            {"text": "TA 喜欢户外活动", "evidence": [f"event:{events[0].id}"]},
            {"text": "TA 是一名医生", "evidence": []},  # 无证据 → 丢
            {"text": "TA 有一个弟弟", "evidence": ["event:9999"]},  # 假证据 → 丢
        ]
    )
    update, _ = run_distill(store, response)
    profile = store.get_profile("console:u1")
    assert profile.stable_lines == ["TA 喜欢户外活动"]
    assert update.dropped_lines == 2
    evidence = store.get_profile_evidence("console:u1")
    assert evidence[("stable", 0)] == [f"event:{events[0].id}"]


def test_empty_model_output_keeps_old_profile(store):
    events = seed_events(store)
    run_distill(
        store,
        proposal(stable=[{"text": "TA 喜欢户外活动", "evidence": [f"event:{events[0].id}"]}]),
    )
    before = store.get_profile("console:u1")
    # 模型输出坏 JSON：不能把已有画像清空
    update, _ = run_distill(store, "完全不是 JSON")
    after = store.get_profile("console:u1")
    assert update.changed is False
    assert after.stable_text == before.stable_text


def test_contradiction_replaces_line_and_logs(store):
    events = seed_events(store, summaries=("TA 说下个月搬去北京",))
    # 先放一个旧画像（直接经 store，模拟上一轮提炼结果）
    first_event = events[0].id
    store.save_profile(
        "console:u1",
        stable_lines=[
            __import__("rubenbot.memory.store", fromlist=["ProfileLine"]).ProfileLine(
                "TA 住在杭州", [f"event:{first_event}"]
            )
        ],
        status_lines=[],
        hooks=["旅行"],
        now=NOW - 100,
        log_reason="首次提炼画像",
    )
    old_evidence = f"event:{first_event}"
    # 新提炼：用新行替换旧行（模型顺带引用了旧证据也算有效，这里直接给新证据）
    new_event = store.add_event("console:u1", "TA 说下个月搬去北京", ts=NOW, source_chat="console:g1")
    response = proposal(
        stable=[{"text": "TA 住在北京（原来在杭州）", "evidence": [f"event:{new_event.id}", old_evidence]}],
        hooks=["搬家", "北京"],
    )
    update, _ = run_distill(store, response)
    profile = store.get_profile("console:u1")
    assert update.changed is True
    assert profile.stable_lines == ["TA 住在北京（原来在杭州）"]
    assert "TA 住在杭州" not in profile.stable_text
    # 留痕：最新一条日志是 profile_update，before 里有旧行
    logs = store.list_logs(person_id="console:u1")
    assert logs[0].action == "profile_update"
    assert any(item["content"] == "TA 住在杭州" for item in logs[0].before)
    assert any(item["content"] == "TA 住在北京（原来在杭州）" for item in logs[0].after)
    # 画像日志不可用 restore 回滚（只作审计）
    assert store.restore(logs[0].id) is False


def test_status_expiry_days_are_clamped(store):
    events = seed_events(store)
    event_id = events[0].id
    cfg = MemoryConfig(status_expiry_min_days=2, status_expiry_max_days=30)
    response = proposal(
        status=[
            {"text": "TA 这周在准备考试", "expires_in_days": 999, "evidence": [f"event:{event_id}"]},
            {"text": "TA 最近在学做饭", "expires_in_days": 0, "evidence": [f"event:{event_id}"]},
        ]
    )
    run_distill(store, response, cfg=cfg)
    profile = store.get_profile("console:u1")
    assert len(profile.status_lines) == 2
    # 近况整体过期时间取各行里最长的那条，并夹到 [2, 30] 天
    assert profile.status_expires_at == NOW + 30 * 86400


def test_clamp_expiry_days_helper():
    assert clamp_expiry_days(None) == 7
    assert clamp_expiry_days(0) == 1
    assert clamp_expiry_days(500) == 90
    assert clamp_expiry_days(-5, low=3, high=10) == 3
    assert clamp_expiry_days(20, low=3, high=10) == 10


def test_hooks_limited_and_extracted(store):
    events = seed_events(store)
    event_id = events[0].id
    hooks = [f"话题{i}" for i in range(12)]
    response = proposal(
        stable=[{"text": "TA 喜欢户外活动", "evidence": [f"event:{event_id}"]}],
        hooks=hooks,
    )
    update, _ = run_distill(store, response)
    assert 3 <= len(update.hooks) <= 8
    assert update.hooks == hooks[:8]


def test_evidence_can_come_from_new_facts(store):
    memory = store.add_memory("console:u1", "TA 是杭州的后端工程师", now=NOW - 100)
    response = proposal(
        stable=[{"text": "TA 是后端工程师", "evidence": [f"memory:{memory.id}"]}],
    )
    update, _ = run_distill(store, response)
    assert update.stable_lines == 1
    assert store.get_profile_evidence("console:u1")[("stable", 0)] == [f"memory:{memory.id}"]


def test_norecord_person_is_skipped(store):
    store.privacy_enabled = True
    store.set_privacy_mode("console:u1", "norecord")
    seed_events(store)
    llm = FakeLLM(proposal(stable=[{"text": "TA 喜欢户外活动", "evidence": ["event:1"]}]))
    distiller = ProfileDistiller(llm, store, MemoryConfig())
    update = asyncio.run(distiller.distill_person("console:u1", now=NOW))
    assert update.changed is False
    assert store.get_profile("console:u1") is None
    assert llm.calls == []


def test_due_people_threshold_and_interval(store):
    seed_events(store)
    cfg = MemoryConfig(distill_new_threshold=2, distill_interval_hours=24)
    distiller = ProfileDistiller(FakeLLM(), store, cfg)
    # 刚提炼过：只有 1 条新事件，没到阈值也没到间隔
    store.set_meta("distill:last_at", str(NOW - 7200))
    assert distiller.due_people(now=NOW) == []
    # 攒够 2 条新事件 → 达阈值
    seed_events(store, summaries=("TA 今天加班",), ts=NOW - 100)
    assert "console:u1" in distiller.due_people(now=NOW)
    # 间隔到点后，没有新内容的人也不纳入（没东西可炼）
    store.set_meta("distill:last_at", str(NOW))
    assert distiller.due_people(now=NOW + 25 * 3600) == []


def test_distill_failure_contained(store):
    seed_events(store)

    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("模型挂了")

    distiller = ProfileDistiller(BoomLLM(), store, MemoryConfig())
    results = asyncio.run(distiller.maybe_distill(now=NOW, force=True))
    assert all(r.error for r in results)
    assert store.get_profile("console:u1") is None


def test_parse_profile_robust():
    raw = """```json
    {"stable": [{"text": "TA 是一名工程师", "evidence": ["event:3"]}, {"text": ""}, "TA 养猫"],
     "status": [{"text": "TA 在准备考试", "expires_in_days": "5", "evidence": ["event:4"]}],
     "hooks": ["工作", "猫", 5, "工作"]}
    ```"""
    stable, status, hooks = parse_profile(raw)
    assert [line.text for line in stable] == ["TA 是一名工程师", "TA 养猫"]
    assert stable[1].refs == []
    assert status[0].expires_in_days == 5
    assert hooks == ["工作", "猫"]


def test_all_lines_invalid_keeps_old_profile(store):
    """坏输出或全被证据校验挡下时，不能把已有画像清空。"""
    events = seed_events(store)
    run_distill(
        store,
        proposal(stable=[{"text": "TA 喜欢户外活动", "evidence": [f"event:{events[0].id}"]}]),
    )
    before = store.get_profile("console:u1")
    update, _ = run_distill(
        store,
        proposal(stable=[{"text": "TA 是医生", "evidence": ["event:404"]}]),
    )
    after = store.get_profile("console:u1")
    assert update.changed is False
    assert update.dropped_lines == 1
    assert after.stable_text == before.stable_text


def test_old_profile_lines_can_reuse_their_evidence(store):
    """模型保留仍有效的旧行、沿用旧证据（本轮输入里没有那些事件）也能通过。"""
    events = seed_events(store)
    event_ref = f"event:{events[0].id}"
    run_distill(store, proposal(stable=[{"text": "TA 喜欢户外活动", "evidence": [event_ref]}]))
    # 下一轮输入里只有新事件，模型顺带保留旧行并原样带上旧证据
    new_event = store.add_event("console:u1", "TA 约朋友周末去露营", ts=NOW, source_chat="console:g1")
    update, _ = run_distill(
        store,
        proposal(
            stable=[
                {"text": "TA 喜欢户外活动", "evidence": [event_ref]},
                {"text": "TA 周末去露营", "evidence": [f"event:{new_event.id}"]},
            ]
        ),
    )
    assert update.changed is True
    assert store.get_profile("console:u1").stable_lines == ["TA 喜欢户外活动", "TA 周末去露营"]
