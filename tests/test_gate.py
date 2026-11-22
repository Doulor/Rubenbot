"""分寸审查口：六条判定路径、fail closed、裁定缓存、隐私三档。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.gate import PrivacyGate, ReviewItem, ruling_key
from rubenbot.memory.store import MemoryStore

NOW = 1_795_000_000.0


class FakeGateLLM:
    """按脚本返回审查结论；``raises`` 模拟模型故障。"""

    def __init__(self, *payloads, raises: Exception | None = None):
        self.payloads = list(payloads)
        self.raises = raises
        self.calls = []

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages})
        if self.raises is not None:
            raise self.raises
        if self.payloads:
            return self.payloads.pop(0)
        return json.dumps({"items": []})


def verdicts(*entries) -> str:
    return json.dumps({"items": list(entries)}, ensure_ascii=False)


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def item(**kwargs) -> ReviewItem:
    base = dict(
        person_id="console:u1",
        text="TA 最近在吃抗抑郁药",
        audience="private",
        sensitivity="normal",
        subject="health",
        category="health",
    )
    base.update(kwargs)
    return ReviewItem(**base)


def run(gate, items, audience_now="group:g9"):
    return asyncio.run(gate.review(items, audience_now=audience_now, speaker="console:u2", now=NOW))


# ----------------------------------------------------------------------
# ① 同场合 / public
# ----------------------------------------------------------------------


def test_same_site_allowed_without_model(store):
    llm = FakeGateLLM(raises=AssertionError("同场合不该调模型"))
    gate = PrivacyGate(store, llm, MemoryConfig())
    allowed, withheld = run(
        gate,
        [
            item(audience="group:g9"),
            item(audience="public", subject="preference"),
            item(audience=""),  # 旧数据没有来源信息，按同场合处理
        ],
    )
    assert len(allowed) == 3 and withheld == [] and llm.calls == []


def test_group_to_private_is_seamless(store):
    """不对称原则：群里说过的，私聊里回看不需要审查。"""
    llm = FakeGateLLM(raises=AssertionError("群→私不该调模型"))
    gate = PrivacyGate(store, llm, MemoryConfig())
    allowed, withheld = run(gate, [item(audience="group:g1")], audience_now="private")
    assert len(allowed) == 1 and withheld == [] and llm.calls == []


# ----------------------------------------------------------------------
# ② 隐私档位
# ----------------------------------------------------------------------


def test_private_mode_withholds_cross_site(store):
    store.set_privacy_mode("console:u1", "private")
    llm = FakeGateLLM(raises=AssertionError("隐私模式不该调模型"))
    gate = PrivacyGate(store, llm, MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private")])
    assert allowed == [] and len(withheld) == 1
    assert "隐私模式" in withheld[0].withhold_reason


def test_private_mode_whitelist_allow(store):
    store.set_privacy_mode("console:u1", "private")
    key = ruling_key(item(audience="private"), "group:g9")
    store.set_ruling("console:u1", key, "allow", now=NOW)
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private")])
    assert len(allowed) == 1 and withheld == []


def test_norecord_has_no_data(store):
    store.set_privacy_mode("console:u1", "norecord")
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private")])
    assert allowed == [] and withheld and "不记" in withheld[0].withhold_reason


# ----------------------------------------------------------------------
# ③ 封存 / ④ 高敏感
# ----------------------------------------------------------------------


def test_sealed_never_crosses(store):
    store.set_ruling("console:u1", ruling_key(item(audience="private"), "group:g9"), "allow", now=NOW)
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private", sensitivity="sealed")])
    assert allowed == [] and withheld and "封存" in withheld[0].withhold_reason


def test_high_sensitivity_withheld_and_queued(store):
    gate = PrivacyGate(store, FakeGateLLM(raises=AssertionError("高敏感不该问模型")), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private", sensitivity="high")])
    assert allowed == [] and len(withheld) == 1
    asks = store.list_pending_asks("console:u1")
    assert len(asks) == 1
    assert asks[0].item_ref == ruling_key(item(audience="private"), "group:g9")
    assert "可以" in asks[0].question and "不行" in asks[0].question


def test_high_sensitivity_ask_can_be_disabled(store):
    config = MemoryConfig(gate_ask_high_sensitivity=False)
    gate = PrivacyGate(store, FakeGateLLM(), config)
    run(gate, [item(audience="private", sensitivity="high")])
    assert store.list_pending_asks("console:u1") == []


# ----------------------------------------------------------------------
# ⑤ 裁定缓存
# ----------------------------------------------------------------------


def test_ruling_cache_avoids_second_model_call(store):
    llm = FakeGateLLM(verdicts({"id": 0, "verdict": "allow", "confidence": 0.9}))
    gate = PrivacyGate(store, llm, MemoryConfig())
    first = run(gate, [item(audience="private")])
    assert len(first[0]) == 1
    assert len(llm.calls) == 1
    assert store.get_ruling("console:u1", ruling_key(item(), "group:g9")).verdict == "allow"

    # 同一项同一类受众：直接命中缓存，不再问模型
    llm.raises = AssertionError("命中缓存不该再调模型")
    second = run(gate, [item(audience="private")])
    assert len(second[0]) == 1


def test_ruling_deny_kept(store):
    gate = PrivacyGate(
        store, FakeGateLLM(verdicts({"id": 0, "verdict": "deny", "confidence": 0.95})), MemoryConfig()
    )
    allowed, withheld = run(gate, [item(audience="private")])
    assert allowed == [] and len(withheld) == 1
    assert store.get_ruling("console:u1", ruling_key(item(), "group:g9")).verdict == "deny"
    # 第二次直接命中 deny 缓存，不再问模型
    gate.llm.raises = AssertionError("命中缓存不该再调模型")
    allowed2, withheld2 = run(gate, [item(audience="private")])
    assert allowed2 == [] and len(withheld2) == 1
    assert "已有裁定" in withheld2[0].withhold_reason


# ----------------------------------------------------------------------
# ⑥ 模型判定 / 低置信 / fail closed
# ----------------------------------------------------------------------


def test_low_confidence_withheld_and_queued(store):
    llm = FakeGateLLM(verdicts({"id": 0, "verdict": "allow", "confidence": 0.2}))
    gate = PrivacyGate(store, llm, MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private", sensitivity="high", subject="health")])
    assert allowed == [] and withheld
    # 高敏感会在本地判定阶段就扣下并排队，不调模型
    assert llm.calls == []
    assert len(store.list_pending_asks("console:u1")) == 1


def test_low_confidence_normal_sensitivity_not_queued(store):
    llm = FakeGateLLM(verdicts({"id": 0, "verdict": "allow", "confidence": 0.1}))
    gate = PrivacyGate(store, llm, MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private", subject="preference")])
    assert allowed == [] and withheld
    assert store.list_pending_asks("console:u1") == []


def test_model_failure_fails_closed(store):
    gate = PrivacyGate(store, FakeGateLLM(raises=RuntimeError("模型挂了")), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private"), item(audience="group:g1", subject="x")])
    assert allowed == []
    assert len(withheld) == 2
    assert all("fail closed" in w.withhold_reason for w in withheld)


def test_model_garbage_fails_closed(store):
    gate = PrivacyGate(store, FakeGateLLM("这不是 JSON"), MemoryConfig())
    allowed, withheld = run(gate, [item(audience="private")])
    assert allowed == [] and len(withheld) == 1


def test_model_allow_passes(store):
    gate = PrivacyGate(
        store, FakeGateLLM(verdicts({"id": 0, "verdict": "allow", "confidence": 0.8})), MemoryConfig()
    )
    allowed, withheld = run(gate, [item(audience="private", subject="hobby")])
    assert len(allowed) == 1 and withheld == []


def test_store_failure_fails_closed(store, monkeypatch):
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    monkeypatch.setattr(store, "get_privacy_mode", lambda person_id: (_ for _ in ()).throw(RuntimeError("库坏了")))
    allowed, withheld = run(gate, [item(audience="private")])
    assert allowed == [] and len(withheld) == 1
    assert "fail closed" in withheld[0].withhold_reason


# ----------------------------------------------------------------------
# 待询问与裁定键
# ----------------------------------------------------------------------


def test_ruling_key_shape():
    health = item(audience="private", category="health", subject="health")
    assert ruling_key(health, "group:g9") == "health@group"
    assert ruling_key(health, "private") == "health@private"
    fact = item(audience="private", subject="fact", category="preference")
    assert ruling_key(fact, "group:g1") == "preference@group"
    assert ruling_key(item(audience="group:g1", subject="event:12"), "group:g2") == "event:12@group"


def test_pending_ask_answered_writes_ruling(store):
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    run(gate, [item(audience="private", sensitivity="high")])
    ask = store.list_pending_asks("console:u1")[0]
    store.answer_pending_ask(ask.id, "allow")
    # 答完之后同一项按白名单放行
    allowed, withheld = run(gate, [item(audience="private", sensitivity="high")])
    assert len(allowed) == 1 and withheld == []


def test_pending_ask_not_duplicated(store):
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    run(gate, [item(audience="private", sensitivity="high")])
    run(gate, [item(audience="private", sensitivity="high")])
    assert len(store.list_pending_asks("console:u1")) == 1


def test_unaskable_item_not_queued(store):
    gate = PrivacyGate(store, FakeGateLLM(), MemoryConfig())
    run(gate, [item(audience="private", sensitivity="high", askable=False)])
    assert store.list_pending_asks("console:u1") == []


@pytest.mark.parametrize("mode,sensitivity,ruling,allowed", [
    ("normal", "normal", None, True), ("normal", "normal", "deny", False),
    ("private", "normal", None, False), ("private", "normal", "allow", True),
    ("normal", "high", None, False), ("normal", "sealed", "allow", False),
    ("norecord", "normal", "allow", False),
])
def test_owner_audience_is_not_a_privacy_override(store, mode, sensitivity, ruling, allowed):
    candidate = item(audience="owner", sensitivity=sensitivity)
    llm = FakeGateLLM(raises=AssertionError("机主分寸口不该调用模型"))
    gate = PrivacyGate(store, llm, MemoryConfig())
    store.set_privacy_mode(candidate.person_id, mode)
    if ruling:
        store.set_ruling(candidate.person_id, ruling_key(candidate, "group:g1"), ruling)
    yes, no = asyncio.run(gate.review([candidate], audience_now="group:g1", owner_person_ids={candidate.person_id}))
    assert (candidate in yes) == allowed and (candidate in no) != allowed
    assert not llm.calls
