"""归纳的校验逻辑：不许新词、20% 上限、user 来源不可删、跨人隔离、撤销。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.consolidate import (
    CONNECTOR_CHARS,
    Consolidator,
    find_new_tokens,
    parse_proposals,
    removal_budget,
)
from rubenbot.memory.store import MemoryStore


class FakeLLM:
    """按顺序返回预设的 JSON 字符串。"""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append((role, messages))
        return self.responses.pop(0) if self.responses else "[]"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def make_consolidator(store, response: str | dict | list) -> tuple[Consolidator, FakeLLM]:
    if not isinstance(response, str):
        response = json.dumps(response, ensure_ascii=False)
    llm = FakeLLM(response)
    return Consolidator(llm, store, MemoryConfig(consolidate_remove_min=4)), llm


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def test_find_new_tokens_strict():
    from rubenbot.memory.store import Memory

    sources = [Memory(person_id="p", content="TA 住在上海"), Memory(person_id="p", content="TA 喜欢打游戏")]
    assert find_new_tokens("TA 住在上海并且喜欢打游戏", sources) == []
    new = find_new_tokens("TA 住在北京并且喜欢打游戏", sources)
    assert "北" in new and "京" in new
    # 编造英文词/数字也会被抓
    assert "python" in find_new_tokens("TA 喜欢打游戏和 python", sources)


def test_connector_whitelist_is_small():
    # 连接词白名单不应包含有实际语义的字
    for word in "北京上海猫狗工作":
        assert word not in CONNECTOR_CHARS


def test_removal_budget():
    assert removal_budget(10) == 4  # 至少 4
    assert removal_budget(100) == 20
    assert removal_budget(50, ratio=0.2, minimum=2) == 10
    assert removal_budget(1) == 4


def test_parse_proposals_robust():
    raw = """```json
    [
      {"op": "merge", "ids": ["1", 2], "content": "x"},
      {"action": "drop", "id": 3},
      {"op": "unknown", "ids": [4]},
      {"op": "drop", "ids": []}
    ]
    ```"""
    proposals = parse_proposals(raw)
    assert [p.op for p in proposals] == ["merge", "drop"]
    assert proposals[0].ids == [1, 2]
    assert proposals[1].ids == [3]
    assert parse_proposals("不是 JSON") == []


# ----------------------------------------------------------------------
# 校验规则
# ----------------------------------------------------------------------


def test_merge_rejected_when_new_words(store):
    m1 = store.add_memory("console:u1", "TA 住在上海")
    m2 = store.add_memory("console:u1", "TA 喜欢打游戏")
    consolidator, _ = make_consolidator(
        store,
        [{"op": "merge", "ids": [m1.id, m2.id], "content": "TA 住在北京并喜欢打游戏", "reason": "合并"}],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.merged == 0
    assert any("原文没有" in r for r in result.rejected)
    # 原记忆不受影响
    assert len(store.list_memories(person_id="console:u1")) == 2


def test_merge_accepted_when_only_rearranging(store):
    m1 = store.add_memory("console:u1", "TA 住在上海")
    m2 = store.add_memory("console:u1", "TA 喜欢打游戏")
    consolidator, _ = make_consolidator(
        store,
        [{"op": "merge", "ids": [m1.id, m2.id], "content": "TA 住在上海并且喜欢打游戏", "reason": "合并"}],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.merged == 1
    memories = store.list_memories(person_id="console:u1")
    assert [m.content for m in memories] == ["TA 住在上海并且喜欢打游戏"]
    logs = store.list_logs()
    assert logs[0].action == "merge"
    assert len(logs[0].before) == 2
    assert logs[0].reason == "合并"


def test_merge_single_id_rejected(store):
    m1 = store.add_memory("console:u1", "TA 住在上海")
    consolidator, _ = make_consolidator(
        store, [{"op": "merge", "ids": [m1.id], "content": "TA 住在上海"}]
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.merged == 0
    assert any("至少需要 2 条" in r for r in result.rejected)


def test_user_source_cannot_be_dropped(store):
    user_mem = store.add_memory("console:u1", "TA 的生日是三月七号", source="user")
    extracted = store.add_memory("console:u1", "TA 今天心情不错")
    consolidator, _ = make_consolidator(
        store,
        [
            {"op": "drop", "ids": [user_mem.id], "reason": "不重要"},
            {"op": "drop", "ids": [extracted.id], "reason": "一次性"},
        ],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.dropped == 1
    remaining = store.list_memories(person_id="console:u1")
    assert [m.id for m in remaining] == [user_mem.id]
    assert any("user 来源" in r for r in result.rejected)


def test_user_source_cannot_be_merged_away(store):
    user_mem = store.add_memory("console:u1", "TA 住在上海", source="user")
    extracted = store.add_memory("console:u1", "TA 在上海工作")
    consolidator, _ = make_consolidator(
        store,
        [{"op": "merge", "ids": [user_mem.id, extracted.id], "content": "TA 住在上海工作"}],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.merged == 0
    assert len(store.list_memories(person_id="console:u1")) == 2


def test_removal_ratio_limit(store):
    """30 条记忆，本轮最多移除 max(4, 6)=6 条。"""
    memories = [store.add_memory("console:u1", f"TA 的旧事第{i}条") for i in range(30)]
    consolidator, _ = make_consolidator(
        store,
        [
            {"op": "drop", "ids": [m.id for m in memories[:7]], "reason": "太多"},  # 超限
            {"op": "drop", "ids": [m.id for m in memories[7:10]], "reason": "ok"},  # 3 条
            {"op": "merge", "ids": [m.id for m in memories[10:13]], "content": "TA 的旧事第10条第11条第12条", "reason": "合并"},  # 净减 2
            {"op": "drop", "ids": [m.id for m in memories[13:16]], "reason": "再加就超"},  # 3 条 → 共 8 > 6
        ],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.dropped == 3
    assert result.merged == 1
    # 移除总数 = 3 + 2 = 5 <= 6
    remaining = store.count_memories("console:u1")
    assert remaining == 30 - 3 - 2
    assert any("上限" in r for r in result.rejected)


def test_budget_at_least_four_even_for_small_store(store):
    memories = [store.add_memory("console:u1", f"TA 的小事第{i}条") for i in range(5)]
    consolidator, _ = make_consolidator(
        store, [{"op": "drop", "ids": [m.id for m in memories[:4]], "reason": "清理"}]
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.dropped == 4
    assert store.count_memories("console:u1") == 1


def test_invalid_ids_and_duplicate_consumption(store):
    m1 = store.add_memory("console:u1", "TA 住在上海")
    m2 = store.add_memory("console:u1", "TA 喜欢打游戏")
    consolidator, _ = make_consolidator(
        store,
        [
            {"op": "drop", "ids": [9999], "reason": "不存在"},
            {"op": "drop", "ids": [m1.id], "reason": "第一次"},
            {"op": "merge", "ids": [m1.id, m2.id], "content": "TA 住在上海打游戏", "reason": "重复引用"},
        ],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.dropped == 1
    assert result.merged == 0
    assert len(result.rejected) == 2


def test_no_cross_person_merge(store):
    """即使模型给出别的人的 id，也不在本次归纳范围内。"""
    mine = store.add_memory("console:u1", "TA 喜欢弹吉他")
    other = store.add_memory("console:u2", "TA 喜欢弹钢琴")
    consolidator, _ = make_consolidator(
        store,
        [{"op": "merge", "ids": [mine.id, other.id], "content": "TA 喜欢弹吉他"}],
    )
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.merged == 0
    assert store.get_memory(other.id) is not None
    assert any("不存在的记忆 id" in r for r in result.rejected)


def test_drop_is_undoable(store):
    mem = store.add_memory("console:u1", "TA 今天有点累")
    consolidator, _ = make_consolidator(store, [{"op": "drop", "ids": [mem.id], "reason": "临时状态"}])
    result = asyncio.run(consolidator.consolidate_person("console:u1"))
    assert result.dropped == 1
    log_id = result.applied_log_ids[0]
    assert store.restore(log_id)
    assert store.get_memory(mem.id).content == "TA 今天有点累"


def test_llm_failure_is_contained(store):
    store.add_memory("console:u1", "TA 住在上海")
    store.add_memory("console:u1", "TA 喜欢打游戏")

    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("模型挂了")

    consolidator = Consolidator(BoomLLM(), store, MemoryConfig())
    result = asyncio.run(consolidator.maybe_consolidate(force=True))
    assert any(r.error for r in result)
    assert store.count_memories("console:u1") == 2


def test_due_people_thresholds(store):
    store.add_memory("console:u1", "TA 住在上海")
    consolidator = Consolidator(FakeLLM(), store, MemoryConfig(consolidate_new_threshold=2))
    assert consolidator.due_people(now=100.0) == []  # 只有 1 条
    store.add_memory("console:u1", "TA 喜欢打游戏")
    assert consolidator.due_people(now=100.0) == ["console:u1"]
    # 间隔到点后，即使没有新记忆也会纳入（有记忆的人）
    store.set_meta("consolidate:last_at", "100")
    assert consolidator.due_people(now=100 + 25 * 3600) == ["console:u1"]
