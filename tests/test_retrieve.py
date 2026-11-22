"""检索打分：token 化、IDF 加权、整句 bonus、优先级与兜底。"""

import pytest

from rubenbot.memory.retrieve import Retriever, jaccard, tokenize
from rubenbot.memory.store import MemoryStore


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def test_tokenize_mixed():
    tokens = tokenize("我喜欢 Python 和 火锅")
    assert "python" in tokens
    assert "火锅" in tokens  # bigram
    assert "我喜" in tokens
    # 纯停用词不成 token
    assert tokenize("的了") == []


def test_jaccard():
    assert jaccard("", "") == 1.0
    assert jaccard("abc", "") == 0.0
    assert jaccard("abc", "abc") == 1.0
    assert 0 < jaccard("TA 喜欢火锅", "TA 喜欢火锅和烧烤") < 1


def test_rare_token_weighs_more(store):
    common = "TA 今天吃了饭"
    rare = "TA 是一名量子物理学家"
    for i in range(6):
        store.add_memory("console:u1", f"{common}{i}")
    rare_mem = store.add_memory("console:u1", rare)
    retriever = Retriever(store)
    scored = retriever.score("量子物理学家", store.list_memories(person_id="console:u1"))
    assert scored
    assert scored[0].memory.id == rare_mem.id
    # 只命中停用词的 query 不应该把常见记忆排到珍贵记忆前面
    assert all(s.memory.content != rare for s in retriever.score("的了", store.list_memories())[:1])


def test_exact_phrase_bonus(store):
    exact = store.add_memory("console:u1", "TA 最喜欢的城市是里斯本")
    store.add_memory("console:u1", "TA 去过里斯本出差")
    retriever = Retriever(store)
    scored = retriever.score("最喜欢的城市是里斯本")
    assert scored[0].memory.id == exact.id
    assert scored[0].score > scored[1].score


def test_retrieve_prioritizes_speaker(store):
    mine = store.add_memory("console:u1", "TA 喜欢弹吉他")
    store.add_memory("console:u2", "TA 也喜欢弹吉他")
    for i in range(30):
        store.add_memory("console:u2", f"TA 的编号是 {i}")
    retriever = Retriever(store)
    result = retriever.retrieve("弹吉他", person_id="console:u1", limit=20)
    assert result
    assert result[0].id == mine.id
    assert len(result) <= 20


def test_retrieve_includes_group_scope(store):
    group_mem = store.add_memory(
        "console:u2", "这个群每周五开黑", scope_type="group", scope_id="g1"
    )
    retriever = Retriever(store)
    result = retriever.retrieve("周五开黑", person_id="console:u1", group_id="g1", limit=10)
    assert group_mem.id in [m.id for m in result]


def test_retrieve_recency_fallback_and_limit(store):
    for i in range(10):
        store.add_memory("console:u1", f"TA 的事实{i}", now=float(i))
    retriever = Retriever(store)
    result = retriever.retrieve("完全不相关的话题", limit=3)
    assert len(result) == 3
    # 最近的三条
    assert [m.content for m in result] == ["TA 的事实7", "TA 的事实8", "TA 的事实9"]
    assert retriever.retrieve("x", limit=0) == []


def test_legacy_retrieve_without_audience_cannot_disclose_when_protected(store):
    fact = store.add_memory('console:u1', '封存的蓝莓项目', audience='private', sensitivity='sealed')
    reader = Retriever(store)
    assert [m.id for m in reader.retrieve('蓝莓')] == [fact.id]
    store.privacy_enabled = True
    assert reader.retrieve('蓝莓', group_id='elsewhere') == []


def test_retrieve_no_duplicates(store):
    store.add_memory("console:u1", "TA 喜欢弹吉他")
    store.add_memory("console:u1", "TA 喜欢弹吉他")
    retriever = Retriever(store)
    result = retriever.retrieve("弹吉他", person_id="console:u1", limit=10)
    ids = [m.id for m in result]
    assert len(ids) == len(set(ids)) == 2
