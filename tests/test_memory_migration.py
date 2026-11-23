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
            # 指称完整性：旧行没有实体锚点，扫描工具会把它们当作候选
            assert mem.entity == ""
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
        assert loaded.entity == ""
    finally:
        store.close()


def test_old_profile_evidence_gets_empty_entity_column(tmp_path):
    """旧库的 profile_evidence 没有 entity 列；升级后补空值，画像行仍能读写。"""
    from rubenbot.memory.store import ProfileLine

    path = tmp_path / "old-profile.sqlite"
    store = MemoryStore(path)
    try:
        store.save_profile(
            "console:u1",
            stable_lines=[ProfileLine("TA 住在杭州", ["event:1"])],
            status_lines=[],
        )
    finally:
        store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE profile_evidence DROP COLUMN entity")
    store = MemoryStore(path)
    try:
        assert store.get_profile_line_entities("console:u1") == {("stable", 0): ""}
        assert store.get_profile_evidence("console:u1") == {("stable", 0): ["event:1"]}
    finally:
        store.close()


def test_old_allow_is_not_migrated_as_explicit_consent(tmp_path):
    path = tmp_path / "old-rulings.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE rulings (id INTEGER PRIMARY KEY, person_id TEXT, key TEXT,
                verdict TEXT, created_at REAL, UNIQUE(person_id, key));
            INSERT INTO rulings VALUES(1, 'console:u1', 'habit@group', 'allow', 10);
        """)
    store = MemoryStore(path)
    try:
        ruling = store.get_ruling('console:u1', 'habit@group')
        assert (ruling.verdict, ruling.source) == ('allow', 'unknown')
    finally:
        store.close()


def test_old_owner_events_keep_place_after_raw_expires(tmp_path):
    """旧的 owner 是受众，不是场合；只能用证据补群/私类型，不能看号码猜。"""
    from rubenbot.memory import read_timeline

    path = tmp_path / "old-events.sqlite"
    store = MemoryStore(path)
    try:
        for chat_type in ("private", "group"):
            rid = store.add_raw_message(platform="qq", chat_type=chat_type, chat_id="42", sender_id="42",
                                        text=chat_type, ts=20, processed=True)
            store.add_event("qq:42", chat_type, source_chat="qq:42", source_raw_id=rid,
                            audience="owner", ts=20)
        store.add_event("qq:42", "无流水", source_chat="qq:42", audience="owner", ts=12)
    finally:
        store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE events DROP COLUMN source_chat_type")
    store = MemoryStore(path)
    try:
        store.trim_raw(0)
        rows = read_timeline(store, now=30)
        assert [(r.text, r.chat_type) for r in rows] == [
            ("group", "group"), ("private", "private"), ("无流水", "unknown"),
        ]
    finally:
        store.close()
