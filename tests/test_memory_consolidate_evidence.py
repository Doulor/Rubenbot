"""归纳落库前刚提交的画像证据也必须保留，且不能部分执行提议。"""

import asyncio
import json
from contextlib import closing

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.consolidate import Consolidator
from rubenbot.memory.store import MemoryStore, ProfileLine


@pytest.mark.parametrize("op, section", [("merge", "stable"), ("drop", "status")])
def test_consolidation_preserves_newly_referenced_fact(tmp_path, monkeypatch, op, section):
    db = tmp_path / "test.sqlite"
    pid = "console:u1"
    with closing(MemoryStore(db)) as store, closing(MemoryStore(db)) as writer:
        other = store.add_memory(pid, "今天喝水")
        event = store.add_event(pid, "一起喝茶")
        assert event.id == other.id  # 同号事件引用不能阻止删除未被引用的事实。
        fact = store.add_memory(pid, "喜欢红茶", source_ref="chat:1")
        peer = store.add_memory(pid, "喜欢绿茶", source_ref="chat:2")
        proposals = [
            {"op": op, "ids": [peer.id, fact.id], "content": "喜欢红茶和绿茶"},
            {"op": "drop", "ids": [other.id]},
        ]

        class FakeLLM:
            async def complete(self, *args, **kwargs):
                return json.dumps(proposals)

        refs = [f"event:{event.id}", f"memory:{fact.id}"]
        apply_changes = store.apply_changes

        def apply_after_profile_commit(**kwargs):
            # 归纳器已读完候选并校验；在它进入删除事务前，由另一连接提交画像。
            lines = [ProfileLine("常喝茶", refs)]
            writer.save_profile(pid, stable_lines=lines if section == "stable" else [],
                                status_lines=lines if section == "status" else [])
            monkeypatch.setattr(store, "apply_changes", apply_changes)
            return apply_changes(**kwargs)

        monkeypatch.setattr(store, "apply_changes", apply_after_profile_commit)
        # 首条提议恰好耗尽预算；被拒绝后预算必须留给后一条。
        config = MemoryConfig(consolidate_remove_min=1 if op == "merge" else 2)
        result = asyncio.run(Consolidator(FakeLLM(), store, config).consolidate_person(pid))

        assert store.get_memory(fact.id) == fact
        assert store.get_memory(peer.id) == peer
        assert store.get_profile_evidence(pid) == {(section, 0): refs}
        assert store.count_memories(pid) == 2
        assert store.get_memory(other.id) is None
        assert (result.merged, result.dropped) == (0, 1)
        assert len(result.rejected) == 1 and "画像证据" in result.rejected[0]
        log, = store.list_logs()
        assert result.applied_log_ids == [log.id]
        assert log.action == "drop" and [row["id"] for row in log.before] == [other.id]
