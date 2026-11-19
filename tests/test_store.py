"""记忆存储：CRUD、变更日志与撤销。"""

import time

import pytest

from rubenbot.memory.store import MemoryStore


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def test_person_upsert_and_identity(store):
    p1 = store.upsert_person("snowluma", "10001", "小明", now=100.0)
    p2 = store.upsert_person("snowluma", "10001", "小明改名", now=200.0)
    assert p1.person_id == p2.person_id == "snowluma:10001"
    assert p2.display_name == "小明改名"
    assert p2.first_seen == 100.0
    assert p2.last_seen == 200.0
    # 不同平台是不同 person
    p3 = store.upsert_person("console", "10001", "小明", now=300.0)
    assert p3.person_id != p1.person_id


def test_memory_crud(store):
    store.upsert_person("snowluma", "1", "阿花")
    pid = "snowluma:1"
    mem = store.add_memory(pid, "TA 喜欢在深夜写代码", category="habit", now=10.0)
    assert mem.id is not None
    assert store.get_memory(mem.id).content == "TA 喜欢在深夜写代码"

    updated = store.update_memory(mem.id, content="TA 喜欢在凌晨写代码", now=20.0)
    assert updated.content == "TA 喜欢在凌晨写代码"
    assert updated.updated_at == 20.0

    assert store.count_memories(pid) == 1
    assert len(store.list_memories(person_id=pid)) == 1

    assert store.delete_memory(mem.id) is True
    assert store.get_memory(mem.id) is None
    assert store.delete_memory(mem.id) is False


def test_list_memories_filters_and_order(store):
    store.upsert_person("console", "a", "A")
    store.upsert_person("console", "b", "B")
    store.add_memory("console:a", "事实一", now=1.0)
    store.add_memory("console:a", "事实二", now=2.0)
    store.add_memory("console:b", "事实三", scope_type="group", scope_id="g1", now=3.0)

    a_memories = store.list_memories(person_id="console:a")
    assert [m.content for m in a_memories] == ["事实二", "事实一"]  # 新→旧
    oldest_first = store.list_memories(person_id="console:a", newest_first=False)
    assert [m.content for m in oldest_first] == ["事实一", "事实二"]

    group_memories = store.list_memories(scope_type="group", scope_id="g1")
    assert [m.content for m in group_memories] == ["事实三"]
    assert store.count_memories(since=2.5) == 1


def test_apply_changes_writes_log_and_restore(store):
    pid = "console:a"
    m1 = store.add_memory(pid, "TA 住在上海", now=10.0)
    m2 = store.add_memory(pid, "TA 喜欢打游戏", now=11.0)
    m3 = store.add_memory(pid, "TA 讨厌早起", now=12.0)

    log = store.apply_changes(
        action="merge",
        delete_ids=[m1.id, m2.id],
        add_specs=[{"person_id": pid, "content": "TA 住在上海，喜欢打游戏"}],
        reason="合并",
        person_id=pid,
        now=100.0,
    )
    assert log.action == "merge"
    assert len(log.before) == 2
    assert len(log.after) == 1
    assert len(store.list_memories(person_id=pid)) == 2  # 合并结果 + m3

    assert store.restore(log.id) is True
    restored = {m.content for m in store.list_memories(person_id=pid)}
    assert restored == {"TA 住在上海", "TA 喜欢打游戏", "TA 讨厌早起"}
    # 合并结果被删除
    assert all(m.id in (m1.id, m2.id, m3.id) for m in store.list_memories(person_id=pid))
    # 日志标记已撤销，重复撤销无效
    assert store.get_log(log.id).reverted is True
    assert store.restore(log.id) is False
    # 撤销本身也留了日志
    logs = store.list_logs()
    assert logs[0].action == "restore"


def test_restore_drop_brings_back_full_text(store):
    pid = "console:a"
    mem = store.add_memory(pid, "TA 养了一只叫团子的猫", category="experience", source="user", now=1.0)
    log = store.apply_changes(action="drop", delete_ids=[mem.id], reason="测试", person_id=pid)
    assert store.list_memories(person_id=pid) == []
    assert store.restore(log.id)
    back = store.list_memories(person_id=pid)
    assert len(back) == 1
    assert back[0].content == "TA 养了一只叫团子的猫"
    assert back[0].source == "user"
    assert back[0].created_at == 1.0


def test_raw_messages_pending_flow(store):
    for i in range(3):
        store.add_raw_message(
            platform="console",
            chat_id="g1",
            sender_id="u1",
            sender_name="阿花",
            text=f"消息{i}",
            ts=float(i),
        )
    store.add_raw_message(
        platform="console", chat_id="g1", sender_id="u2", sender_name="阿二", text="你好", ts=10.0
    )
    pending = store.pending_senders()
    assert len(pending) == 2
    u1 = next(p for p in pending if p.sender_id == "u1")
    assert u1.count == 3

    msgs = store.pending_messages("console", "g1", "u1")
    assert [m.text for m in msgs] == ["消息0", "消息1", "消息2"]
    store.mark_processed([m.id for m in msgs])
    assert store.pending_senders()[0].sender_id == "u2"
    assert store.recent_messages("g1", limit=10)[-1].text == "你好"
    assert store.last_message_ts("g1") == 10.0


def test_trim_raw_keeps_recent_processed(store):
    for i in range(10):
        store.add_raw_message(
            platform="console", chat_id="g1", sender_id="u1", text=f"m{i}", ts=float(i), processed=True
        )
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", text="pending", ts=99.0)
    removed = store.trim_raw(keep_per_chat=3)
    assert removed == 7
    remaining = store.recent_messages("g1", limit=100)
    # 未处理的永远保留
    assert any(m.text == "pending" for m in remaining)
    assert [m.text for m in remaining] == ["m7", "m8", "m9", "pending"]


def test_meta(store):
    assert store.get_meta("x") is None
    store.set_meta("x", "1")
    store.set_meta("x", "2")
    assert store.get_meta("x") == "2"
    assert store.get_meta_float("x") == 2.0
    assert store.get_meta_float("missing", 5.0) == 5.0


def test_messages_have_timestamps(store):
    before = time.time()
    store.add_raw_message(platform="console", chat_id="g", sender_id="u", text="hi")
    msg = store.recent_messages("g")[0]
    assert msg.ts >= before


# 决策日志

def test_decision_log_crud_and_stats(store):
    first = store.add_decision_log(
        platform="console",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text="在吗",
        should_reply=True,
        reason="willing",
        score=0.9,
        threshold=0.85,
        factors={"base": 0.1, "momentum": 0.8},
        now=100.0,
    )
    store.add_decision_log(
        platform="console",
        chat_id="g1",
        text="@Ruben",
        should_reply=True,
        reason="mentioned",
        hard_trigger="mentioned",
        score=1.0,
        threshold=0.85,
        now=200.0,
    )
    store.add_decision_log(platform="console", chat_id="g2", text="路过", now=300.0)

    assert first == 1
    all_items = store.list_decision_logs()
    assert [item["chat_id"] for item in all_items] == ["g2", "g1", "g1"]
    assert store.list_decision_logs(chat_id="g1", limit=1)[0]["text"] == "@Ruben"

    stats = store.decision_stats()
    assert stats["decisions"] == 3
    assert stats["decision_replies"] == 2
    assert stats["hard_triggers"] == 1
    assert stats["reply_rate"] == 2 / 3

    only_recent = store.decision_stats(since=250.0)
    assert only_recent["decisions"] == 1

    store.trim_decision_log(keep=2)
    assert len(store.list_decision_logs()) == 2


def test_decision_log_factors_roundtrip_unicode(store):
    store.add_decision_log(
        platform="console",
        chat_id="群一",
        text="今天好累啊",
        factors={"base": 0.1, "noise": -0.03},
    )
    item = store.list_decision_logs()[0]
    assert item["text"] == "今天好累啊"
    assert item["factors"] == {"base": 0.1, "noise": -0.03}


def test_search_memories_and_counts(store):
    store.add_memory("p:1", "TA 在杭州做后端")
    store.add_memory("p:1", "TA 喜欢爬山")
    store.add_memory("p:2", "TA 养猫", scope_type="group", scope_id="g1", now=50.0)

    items, total = store.search_memories("杭州")
    assert total == 1 and items[0].content == "TA 在杭州做后端"

    items, total = store.search_memories("TA")
    assert total == 3

    items, total = store.search_memories("", person_id="p:1")
    assert total == 2

    items, total = store.search_memories("", scope_type="group")
    assert total == 1 and items[0].scope_id == "g1"

    items, total = store.search_memories("", limit=1, offset=1)
    assert total == 3 and len(items) == 1

    # 通配符按字面匹配
    store.add_memory("p:3", "完成度 80%")
    assert store.search_memories("80%")[1] == 1
    assert store.search_memories("_")[1] == 0


def test_people_with_counts_and_chats(store):
    store.upsert_person("console", "u1", "阿花", now=10.0)
    store.upsert_person("console", "u2", "老周", now=20.0)
    store.add_memory("console:u1", "事实一")
    store.add_memory("console:u1", "事实二")

    rows = store.list_people_with_counts()
    counts = {person.person_id: count for person, count in rows}
    assert counts == {"console:u1": 2, "console:u2": 0}
    assert store.count_people() == 2

    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", text="hi", ts=5.0)
    store.add_raw_message(platform="console", chat_id="g1", sender_id="__bot__", text="yo", ts=8.0, processed=True)
    store.add_raw_message(platform="console", chat_id="u2", sender_id="u2", text="私聊", chat_type="private", ts=9.0)

    chats = store.list_chats()
    assert chats[0]["chat_id"] == "u2" and chats[0]["chat_type"] == "private"
    g1 = next(chat for chat in chats if chat["chat_id"] == "g1")
    assert g1["message_count"] == 2 and g1["last_ts"] == 8.0

    counts = store.chat_counts()
    assert counts == {"messages": 3, "replies": 1, "chats": 2}
