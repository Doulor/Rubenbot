"""记忆 v2 迁移：旧库升级后行数不变、默认值正确，新表可读写。"""

import sqlite3

from rubenbot.memory.store import MemoryStore

OLD_SCHEMA = """
CREATE TABLE people (
    person_id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_uid TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    UNIQUE(platform, platform_uid)
);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global', scope_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'fact',
    source TEXT NOT NULL DEFAULT 'extracted', source_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL DEFAULT 'group', sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, ts REAL NOT NULL, processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE memory_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, person_id TEXT,
    before_json TEXT NOT NULL DEFAULT '[]', after_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, reverted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def make_old_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO people VALUES('console:u1','console','u1','阿花',10,20)"
    )
    for index in range(3):
        conn.execute(
            "INSERT INTO memories(person_id, content, created_at, updated_at) VALUES(?, ?, ?, ?)",
            ("console:u1", f"旧事实{index}", 10.0 + index, 10.0 + index),
        )
    conn.commit()
    conn.close()


def test_old_db_upgrades_without_data_loss(tmp_path):
    path = tmp_path / "old.sqlite"
    make_old_db(path)
    store = MemoryStore(path)
    try:
        memories = store.list_memories(person_id="console:u1", newest_first=False)
        assert [m.content for m in memories] == ["旧事实0", "旧事实1", "旧事实2"]
        assert store.count_memories("console:u1") == 3
        # 新列都有默认值
        for mem in memories:
            assert mem.origin_chat == ""
            assert mem.audience == ""
            assert mem.sensitivity == "normal"
            assert mem.stability == "stable"
            assert mem.expires_at is None
        # 旧行为不变：日志/撤销仍可用的基本接口
        assert store.list_logs() == []
        # 新表就绪
        assert store.get_profile("console:u1") is None
        assert store.get_privacy_mode("console:u1") == "normal"
        assert store.list_events(person_id="console:u1") == []
    finally:
        store.close()


def test_upgrade_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    make_old_db(path)
    MemoryStore(path).close()
    store = MemoryStore(path)
    try:
        assert store.count_memories() == 3
        mem = store.add_memory(
            "console:u1", "新事实", origin_chat="console:g1", audience="group:g1", sensitivity="high"
        )
        loaded = store.get_memory(mem.id)
        assert loaded.audience == "group:g1"
        assert loaded.sensitivity == "high"
    finally:
        store.close()
