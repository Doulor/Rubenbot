"""存量扫描工具：只读出报告，显式 --apply 才补回/清掉，且都写 memory_log。"""

import json
import sqlite3

import pytest

from rubenbot.memory.store import MemoryStore, make_person_id
from tools import scan_unanchored_memories as scan

BASE_TS = 1_795_000_000.0
WHO = make_person_id("qq", "u1")


@pytest.fixture()
def db(tmp_path):
    """一份带同模式条目的测试账本。"""
    path = tmp_path / "rubenbot.db"
    store = MemoryStore(str(path))
    store.upsert_person("qq", "u1", "阿花", now=BASE_TS)
    # 出处原话能给回实体 → 可补回
    raw_recover = store.add_raw_message(
        platform="qq", chat_id="g1", sender_id="u1",
        text="我做过一个『随手做的』QQ 官方机器人桥，硬性限制比较大，用来推单向消息不错",
        ts=BASE_TS,
    )
    store.add_memory(
        WHO,
        "TA 认为它用来推单向消息不错",
        origin_chat="qq:g1",
        source_ref=f"qq:g1:{raw_recover}-{raw_recover}",
        now=BASE_TS,
    )
    # 出处原话里根本没有实体，也没有源消息 → 补不回，应清掉
    store.add_memory(WHO, "TA 觉得那个东西不好用", origin_chat="qq:g1", source_ref="", now=BASE_TS + 1)
    # 正文早就写了唯一专名、只差 entity 列没落上：按正文补回，不清掉
    store.add_memory(WHO, "关于「飞书」：TA 觉得这个平台好用", origin_chat="qq:g1", now=BASE_TS + 1.5)
    # 指代不明的多实体原话 → 清掉
    raw_ambiguous = store.add_raw_message(
        platform="qq", chat_id="g1", sender_id="u1",
        text="Claude Code 和 Cursor 都不错，用来推单向消息不错",
        ts=BASE_TS + 2,
    )
    store.add_memory(
        WHO,
        "TA 认为它用来推单向消息不错",
        origin_chat="qq:g1",
        source_ref=f"qq:g1:{raw_ambiguous}-{raw_ambiguous}",
        now=BASE_TS + 2,
    )
    # 自带主语的正常条目，不该进清单
    store.add_memory(WHO, "TA 住在杭州", origin_chat="qq:g1", now=BASE_TS + 3)
    # 已经带实体锚点的条目，不该进清单
    store.add_memory(WHO, "关于「飞书」：TA 觉得这个平台好用", entity="飞书", now=BASE_TS + 4)
    # 画像里一条无锚点评价 + 一条正常行
    from rubenbot.memory.store import ProfileLine

    event = store.add_event(WHO, "TA 见过 QQ 官方机器人桥，觉得限制多", ts=BASE_TS, source_chat="qq:g1")
    store.save_profile(
        WHO,
        stable_lines=[
            ProfileLine("TA 认为它推单向消息不错", [f"event:{event.id}"]),
            ProfileLine("TA 住在杭州", [f"event:{event.id}"]),
        ],
        status_lines=[],
        now=BASE_TS,
    )
    store.close()
    return path


def read(path):
    conn = scan._connect_ro(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_scan_is_read_only_and_classifies(db):
    before = db.read_bytes()
    report = scan.collect(read(db))
    assert db.read_bytes() == before  # 只读：文件字节都没变

    assert report.total == 5  # 4 条记忆 + 1 行画像
    assert len(report.recovered) == 3  # 出处原话补回 2 条记忆 + 画像 1 行（其中 1 条凭正文专名补齐）
    assert len(report.cleared) == 2  # 无出处、指代不明各 1
    recovered = {(item.kind, item.anchor.name) for item in report.recovered}
    assert recovered == {
        ("memory", "QQ 官方机器人桥"),
        ("memory", "飞书"),
        ("profile", "QQ 官方机器人桥"),
    }
    cleared = {item.ident for item in report.cleared}
    assert cleared == {"memory:2", "memory:4"}
    # 出处原话一并列出，方便人工复核
    first = report.recovered[0]
    assert "QQ 官方机器人桥" in first.source_text


def test_report_format_markdown_and_json(db):
    report = scan.collect(read(db))
    text = report.format_markdown()
    assert "扫描条目：" in text and "可补回实体：3" in text and "需清掉：2" in text
    assert "补回清单" in text and "清掉清单" in text
    for item in report.candidates:
        assert item.ident in text
    payload = report.as_dict()
    assert payload["total"] == 5 and payload["recovered"] == 3 and payload["cleared"] == 2
    assert {item["outcome"] for item in payload["items"]} == {"recovered", "cleared"}
    assert json.dumps(payload, ensure_ascii=False)  # 可序列化


def test_apply_flag_recovers_and_clears_with_logs(db):
    report = scan.collect(read(db))
    scan.apply_report(db, report)

    store = MemoryStore(str(db))
    try:
        memories = {mem.id: mem for mem in store.list_memories(person_id=WHO)}
        # 补回：正文加了可回指前缀，entity 落库，仍是同一条记忆
        assert memories[1].entity == "QQ 官方机器人桥"
        assert memories[1].content.startswith("关于「QQ 官方机器人桥」：")
        # 正文本来就有唯一专名、只差 entity 列的条目：补上锚点，正文前缀不重复。
        assert memories[3].entity == "飞书"
        assert memories[3].content == "关于「飞书」：TA 觉得这个平台好用"
        # 清掉：无出处 / 指代不明两条都删了
        assert 2 not in memories and 4 not in memories
        # 正常条目与已有锚点条目不动
        assert memories[5].content == "TA 住在杭州"
        assert memories[6].entity == "飞书"
        # 画像行同样补回，另一行不动
        profile = store.get_profile(WHO)
        assert profile.stable_lines == [
            "关于「QQ 官方机器人桥」：TA 认为它推单向消息不错",
            "TA 住在杭州",
        ]
        assert store.get_profile_line_entities(WHO)[("stable", 0)] == "QQ 官方机器人桥"
        # 每次改动都有 memory_log
        actions = [entry.action for entry in store.list_logs(limit=50)]
        assert "merge" in actions and "drop" in actions and "profile_update" in actions
        dropped = [entry for entry in store.list_logs(limit=50) if entry.action == "dropped_unanchored"]
        assert {entry.before[0]["kind"] for entry in dropped} == {"memory"}
        # 补回的 merge 可撤销，正文能恢复
        merge = next(
            entry for entry in store.list_logs(limit=50)
            if entry.action == "merge"
            and entry.before[0]["content"] == "TA 认为它用来推单向消息不错"
        )
        assert store.restore(merge.id) is True
        assert store.get_memory(1).content == "TA 认为它用来推单向消息不错"
    finally:
        store.close()


def test_apply_handles_all_profile_lines_without_index_drift(db):
    """同一个人画像里既有要清掉的行、又有多行要补回：逐行按原行号处理，不串行。"""
    store = MemoryStore(str(db))
    try:
        who = WHO
        from rubenbot.memory.store import ProfileLine

        event = store.add_event(who, "TA 觉得飞书这个平台限制挺多", ts=BASE_TS + 10, source_chat="qq:g1")
        anonymous = store.add_event(who, "TA 觉得那个东西没用", ts=BASE_TS + 11, source_chat="qq:g1")
        store.save_profile(
            who,
            stable_lines=[
                ProfileLine("TA 觉得那个东西没用", [f"event:{anonymous.id}"]),
                ProfileLine("TA 觉得这个平台限制挺多", [f"event:{event.id}"]),
                ProfileLine("TA 住在杭州", [f"event:{event.id}"]),
            ],
            status_lines=[],
            now=BASE_TS + 10,
        )
    finally:
        store.close()

    report = scan.collect(read(db))
    assert [item.ident for item in report.candidates if item.kind == "profile"] == [
        f"profile:{WHO}:stable:0",
        f"profile:{WHO}:stable:1",
    ]
    scan.apply_report(db, report)

    store = MemoryStore(str(db))
    try:
        profile = store.get_profile(WHO)
        assert profile.stable_lines == ["关于「飞书」：TA 觉得这个平台限制挺多", "TA 住在杭州"]
        assert store.get_profile_line_entities(WHO) == {("stable", 0): "飞书", ("stable", 1): ""}
        evidence = store.get_profile_evidence(WHO)
        assert all(refs for refs in evidence.values())
    finally:
        store.close()


def test_missing_db_exits_nonzero(tmp_path, capsys):
    assert scan.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert "数据库不存在" in capsys.readouterr().err


LEGACY_SCHEMA = """
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global', scope_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'fact',
    source TEXT NOT NULL DEFAULT 'extracted', source_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE profiles (person_id TEXT PRIMARY KEY, stable_text TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '', status_expires_at REAL,
    hooks TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
CREATE TABLE profile_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL,
    section TEXT NOT NULL, line_no INTEGER NOT NULL,
    evidence_refs TEXT NOT NULL DEFAULT '[]', UNIQUE(person_id, section, line_no));
"""


def test_legacy_schema_without_entity_columns_is_scannable(tmp_path):
    """老库还没有 entity / origin_chat 列：只读扫描不能为此报错或改表。"""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO memories(person_id, content, created_at, updated_at) VALUES('qq:u1','TA 觉得那个东西不好用',1,1)"
        )
        conn.execute("INSERT INTO profiles VALUES('qq:u1','TA 觉得它不错','',NULL,'',2)")
        conn.execute(
            "INSERT INTO profile_evidence(person_id, section, line_no, evidence_refs) VALUES('qq:u1','stable',0,'[]')"
        )
    before = path.read_bytes()
    assert scan.main(["--db", str(path)]) == 0
    assert path.read_bytes() == before  # 只读连接不改表
    # --apply 走 MemoryStore 时会先迁移补列，再清掉两条
    assert scan.main(["--db", str(path), "--apply", "--format", "json"]) == 0
    store = MemoryStore(str(path))
    try:
        assert store.list_memories() == []
        assert store.get_profile("qq:u1").stable_lines == []
        assert {entry.action for entry in store.list_logs(limit=20)} >= {"drop", "profile_update"}
    finally:
        store.close()


def test_cli_writes_report_file(db, tmp_path, capsys):
    out = tmp_path / "report.md"
    assert scan.main(["--db", str(db), "--out", str(out)]) == 0
    assert "报告已写入" in capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert "补回清单" in text
    # 默认（不带 --apply）不改账本
    store = MemoryStore(str(db))
    try:
        assert store.get_memory(2) is not None
    finally:
        store.close()
