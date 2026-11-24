"""模型在途时由另一个连接清空、切档或修改证据，不能再落回旧正文。"""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.distill import ProfileDistiller
from rubenbot.memory.store import MemoryStore, ProfileLine


@pytest.mark.parametrize(
    "action", ["purge", "norecord", "resume", "delete_source", "edit_source", "other_purge"]
)
def test_distill_rechecks_whole_round(tmp_path, action):
    path = tmp_path / "memory.sqlite"
    person = "console:u1"
    with (
        closing(MemoryStore(path, privacy_enabled=True)) as store,
        closing(MemoryStore(path, privacy_enabled=True)) as writer,
    ):
        event = store.add_event(person, "做过一个东西，限制比较大", ts=1, source_chat="console:g1")
        fact = store.add_memory(person, "喜欢爬山")
        ref = f"event:{event.id}"

        class DelayedLLM:
            async def complete(self, messages, **kwargs):
                assert (
                    ref in messages[-1]["content"]
                    and f"memory:{fact.id}" in messages[-1]["content"]
                )
                if action == "purge":
                    writer.purge_person(person)
                    writer.add_memory(person, "清空后新记的事")
                elif action in {"norecord", "resume"}:
                    writer.set_privacy_mode(person, "norecord", now=1)
                    if action == "resume":
                        writer.set_privacy_mode(person, "normal", now=1)
                elif action == "delete_source":
                    writer.delete_memory(fact.id)
                elif action == "edit_source":
                    writer.update_memory(fact.id, content="不再爬山", now=fact.updated_at)
                else:
                    writer.purge_person("console:u2")
                # 即使模型没引用失效的事实，它也看过该事实，不能只核对输出里的引用。
                return json.dumps(
                    {
                        "stable": [
                            {"text": "做过一个东西，限制比较大", "evidence": [ref]},
                            {"text": "TA 认为它用来推单向消息不错", "evidence": [ref]},
                        ],
                        "hooks": ["爬山"],
                    }
                )

        update = asyncio.run(
            ProfileDistiller(DelayedLLM(), store, MemoryConfig()).distill_person(person)
        )
        if action == "other_purge":
            assert update.changed and update.dropped_lines == 1
            assert writer.get_profile(person).stable_lines == ["做过一个东西，限制比较大"]
            assert writer.get_profile_evidence(person) == {("stable", 0): [ref]}
            assert {log.action for log in writer.list_logs(person_id=person)} == {
                "dropped_unanchored",
                "profile_update",
            }
            writer.purge_person(person)
        else:
            assert not update.changed and not update.hooks and not update.dropped_lines
        assert writer.get_profile(person) is None
        assert writer.get_profile_evidence(person) == {}
        assert all(log.action == "purge" for log in writer.list_logs(person_id=person))


@pytest.mark.parametrize("delete_before_request", [True, False])
def test_old_profile_evidence_lifetime(delete_before_request):
    person = "console:u1"
    with closing(MemoryStore()) as store:
        old_fact = store.add_memory(person, "旧住址", now=1)
        old_ref = f"memory:{old_fact.id}"
        store.save_profile(person, stable_lines=[ProfileLine("旧住址", [old_ref])], status_lines=[])
        store.set_meta("distill:last_at", "2")
        event = store.add_event(person, "最近在游泳", ts=3, now=3, source_chat="console:g1")
        if delete_before_request:
            store.delete_memory(old_fact.id)

        class Model:
            async def complete(self, messages, **kwargs):
                prompt = messages[-1]["content"]
                assert (old_ref in prompt) == (not delete_before_request)
                assert ("旧住址" in prompt) == (not delete_before_request)
                if not delete_before_request:
                    store.delete_memory(old_fact.id)
                return json.dumps(
                    {"stable": [{"text": "最近在游泳", "evidence": [f"event:{event.id}"]}]}
                )

        update = asyncio.run(
            ProfileDistiller(Model(), store, MemoryConfig()).distill_person(person)
        )
        # 请求前已失效的旧画像不进提示词，可以从新证据重建；请求后才失效则不能提交。
        assert update.changed == delete_before_request
        assert store.get_profile(person).stable_lines == (
            ["最近在游泳"] if delete_before_request else ["旧住址"]
        )
        assert bool(store.list_logs(person_id=person)) == delete_before_request


def test_distill_audit_rolls_back_with_profile(tmp_path):
    path = tmp_path / "memory.sqlite"
    person = "console:u1"
    with closing(MemoryStore(path)) as store, closing(sqlite3.connect(path)) as observer:
        event = store.add_event(person, "做过一个东西，限制比较大", ts=1, source_chat="console:g1")
        inputs = store.distill_input(person, 10, 10)
        # 在画像与丢弃日志已写、证据还未写时失败；其它连接不应看到任何部分提交。
        observer.execute("""CREATE TRIGGER fail_evidence BEFORE INSERT ON profile_evidence
                            BEGIN SELECT RAISE(ABORT, '模拟证据写入失败'); END""")
        ref = f"event:{event.id}"
        with pytest.raises(sqlite3.IntegrityError, match="模拟证据写入失败"):
            store.commit_distillation(
                inputs,
                stable_lines=[ProfileLine("做过一个东西", [ref])],
                status_lines=[],
                status_expires_at=None,
                hooks=[],
                log_reason="首次提炼画像",
                discarded=[(ref, "认为它不错", "无实体锚点")],
                now=2,
            )
        for table in ("profiles", "profile_evidence", "memory_log"):
            assert observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
