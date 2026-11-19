"""抽取：去重（精确 + Jaccard）、触发条件、失败容错、JSON 解析。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.extract import MemoryExtractor, parse_extracted_facts
from rubenbot.memory.store import MemoryStore


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages, "json_mode": json_mode})
        return self.responses.pop(0) if self.responses else "[]"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def seed(store, sender="u1", texts=("我住在杭州", "我是做后端的"), chat_id="g1"):
    for i, text in enumerate(texts):
        store.add_raw_message(
            platform="console",
            chat_id=chat_id,
            sender_id=sender,
            sender_name="阿花",
            text=text,
            ts=100.0 + i,
        )


def facts(*contents):
    return json.dumps(
        [{"content": c, "category": "identity", "scope": "global"} for c in contents],
        ensure_ascii=False,
    )


def test_extract_adds_and_marks_processed(store):
    seed(store)
    llm = FakeLLM(facts("TA 住在杭州", "TA 是一名后端工程师"))
    extractor = MemoryExtractor(llm, store, MemoryConfig())
    results = asyncio.run(extractor.maybe_extract(now=1000.0, force=True))
    assert len(results) == 1
    assert results[0].added == ["TA 住在杭州", "TA 是一名后端工程师"]
    assert store.count_memories("console:u1") == 2
    # 流水已处理，不会重复抽取
    assert store.pending_senders() == []
    assert llm.calls[0]["role"] == "extract"
    assert llm.calls[0]["json_mode"] is True


def test_extract_dedupes_exact_and_near(store):
    store.add_memory("console:u1", "TA 住在杭州")
    store.add_memory("console:u1", "TA 是一名后端工程师")
    seed(store)
    llm = FakeLLM(
        facts(
            "TA 住在杭州",  # 精确重复
            "TA 是一名后端工程师写 Python",  # 近似重复（Jaccard = 0.8）
            "TA 每天早上七点跑步",  # 新的
        )
    )
    extractor = MemoryExtractor(llm, store, MemoryConfig())
    result = asyncio.run(extractor.maybe_extract(now=1000.0, force=True))[0]
    assert result.added == ["TA 每天早上七点跑步"]
    assert result.skipped == 2
    assert store.count_memories("console:u1") == 3


def test_extract_skips_duplicates_within_same_batch(store):
    seed(store)
    llm = FakeLLM(facts("TA 喜欢喝美式咖啡", "TA 喜欢喝美式咖啡"))
    extractor = MemoryExtractor(llm, store, MemoryConfig())
    result = asyncio.run(extractor.maybe_extract(now=1000.0, force=True))[0]
    assert result.added == ["TA 喜欢喝美式咖啡"]
    assert result.skipped == 1


def test_extract_group_scope(store):
    seed(store)
    response = json.dumps(
        [{"content": "TA 是群里的管理员", "category": "relationship", "scope": "group"}],
        ensure_ascii=False,
    )
    extractor = MemoryExtractor(FakeLLM(response), store, MemoryConfig())
    asyncio.run(extractor.maybe_extract(now=1000.0, force=True))
    memories = store.list_memories(person_id="console:u1")
    assert memories[0].scope_type == "group"
    assert memories[0].scope_id == "g1"


def test_trigger_threshold(store):
    cfg = MemoryConfig(extract_every_n=5, extract_min_pending=1, extract_silence_seconds=9999)
    seed(store, texts=("a", "b", "c"))  # 不足 5 条且未静默
    llm = FakeLLM(facts("TA 很爱说话"))
    extractor = MemoryExtractor(llm, store, cfg)
    assert asyncio.run(extractor.maybe_extract(now=101.0)) == []
    # 攒够 5 条后触发
    seed(store, texts=("d", "e"))
    results = asyncio.run(extractor.maybe_extract(now=105.0))
    assert len(results) == 1
    assert llm.calls


def test_trigger_silence(store):
    cfg = MemoryConfig(extract_every_n=100, extract_min_pending=2, extract_silence_seconds=30)
    seed(store, texts=("a", "b"))
    llm = FakeLLM(facts("TA 在准备考试"))
    extractor = MemoryExtractor(llm, store, cfg)
    assert asyncio.run(extractor.maybe_extract(now=101.0)) == []  # 刚说过话
    results = asyncio.run(extractor.maybe_extract(now=200.0))  # 静默足够久
    assert len(results) == 1


def test_llm_error_keeps_messages_pending(store):
    seed(store)

    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("超时")

    extractor = MemoryExtractor(BoomLLM(), store, MemoryConfig())
    result = asyncio.run(extractor.maybe_extract(now=1000.0, force=True))[0]
    assert result.error
    assert result.added == []
    # 失败不标记已处理，下次还能重试
    assert store.pending_senders() and store.pending_senders()[0].count == 2


def test_parse_extracted_facts_robust():
    raw = """好的，结果如下：
```json
{"facts": [
  {"content": "TA 养了一只猫", "category": "experience"},
  {"content": "TA 喜欢下雨天", "category": "unknown"},
  {"content": ""},
  "TA 讨厌香菜",
  {"fact": "TA 会弹钢琴"}
]}
```"""
    parsed = parse_extracted_facts(raw)
    assert [f.content for f in parsed] == ["TA 养了一只猫", "TA 喜欢下雨天", "TA 讨厌香菜", "TA 会弹钢琴"]
    assert parsed[1].category == "fact"
    assert parse_extracted_facts("[]") == []
    assert parse_extracted_facts("完全不是 JSON") == []


def test_extract_writes_nothing_when_no_facts(store):
    seed(store)
    extractor = MemoryExtractor(FakeLLM("[]"), store, MemoryConfig())
    result = asyncio.run(extractor.maybe_extract(now=1000.0, force=True))[0]
    assert result.added == []
    assert store.count_memories("console:u1") == 0
