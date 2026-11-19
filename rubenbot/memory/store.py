"""SQLite 存储层：memories / memory_log / people / raw_messages。

设计原则：单文件、无 ORM、同步调用（本地 SQLite 足够快），对外暴露小接口。
所有时间戳统一用 Unix 秒（float）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """CREATE TABLE IF NOT EXISTS people (
    person_id     TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    platform_uid  TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    UNIQUE(platform, platform_uid)
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   TEXT NOT NULL,
    scope_type  TEXT NOT NULL DEFAULT 'global',   -- global / group
    scope_id    TEXT NOT NULL DEFAULT '',         -- group 时为群 id
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'fact',
    source      TEXT NOT NULL DEFAULT 'extracted', -- extracted / user / system
    source_ref  TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_person ON memories(person_id);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

CREATE TABLE IF NOT EXISTS raw_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_type   TEXT NOT NULL DEFAULT 'group',
    sender_id   TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    ts          REAL NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_chat ON raw_messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_raw_pending ON raw_messages(processed, sender_id);

CREATE TABLE IF NOT EXISTS memory_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    person_id   TEXT,
    before_json TEXT NOT NULL DEFAULT '[]',
    after_json  TEXT NOT NULL DEFAULT '[]',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    reverted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 回复决策日志：每次「回 / 不回」的结论与因子，供 WebUI 展示和调参
CREATE TABLE IF NOT EXISTS decision_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_type   TEXT NOT NULL DEFAULT 'group',
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    should_reply INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    hard_trigger TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    threshold   REAL NOT NULL DEFAULT 0,
    factors     TEXT NOT NULL DEFAULT '{}',   -- DecisionFactors 的 JSON
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_chat ON decision_log(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_created ON decision_log(created_at);
"""


@dataclass
class Memory:
    """一条关于某个人的短事实。"""

    id: int | None = None
    person_id: str = ""
    scope_type: str = "global"
    scope_id: str = ""
    content: str = ""
    category: str = "fact"
    source: str = "extracted"
    source_ref: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        return cls(**{key: row[key] for key in row.keys()})


@dataclass
class Person:
    person_id: str
    platform: str
    platform_uid: str
    display_name: str
    first_seen: float
    last_seen: float


@dataclass
class RawMessage:
    id: int
    platform: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    text: str
    ts: float


@dataclass
class PendingSender:
    """有待抽取消息的发送者（按 平台+会话+人 聚合）。"""

    platform: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    count: int


@dataclass
class LogEntry:
    id: int
    action: str
    person_id: str | None
    before: list[dict]
    after: list[dict]
    reason: str
    created_at: float
    reverted: bool


class MemoryStore:
    """所有 SQLite 读写的唯一入口。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # people
    # ------------------------------------------------------------------

    def upsert_person(
        self,
        platform: str,
        platform_uid: str,
        display_name: str = "",
        now: float | None = None,
    ) -> Person:
        """登记/更新一个人；同一平台 uid 视为同一人。"""
        now = time.time() if now is None else now
        person_id = make_person_id(platform, platform_uid)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO people(person_id, platform, platform_uid, display_name, first_seen, last_seen)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, platform_uid) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name <> '' THEN excluded.display_name ELSE people.display_name END,
                    last_seen = excluded.last_seen
                """,
                (person_id, platform, str(platform_uid), display_name, now, now),
            )
            self._conn.commit()
        return self.get_person(person_id)  # type: ignore[return-value]

    def get_person(self, person_id: str) -> Person | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM people WHERE person_id = ?", (person_id,)).fetchone()
        return Person(**dict(row)) if row else None

    def list_people(self, limit: int = 100) -> list[Person]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM people ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Person(**dict(row)) for row in rows]

    def count_people(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()
        return int(row["c"])

    def memory_counts_by_person(self) -> dict[str, int]:
        """一次查出每个人有多少条记忆，供人物列表展示。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT person_id, COUNT(*) AS c FROM memories GROUP BY person_id"
            ).fetchall()
        return {row["person_id"]: int(row["c"]) for row in rows}

    def list_people_with_counts(self, limit: int = 200) -> list[tuple[Person, int]]:
        counts = self.memory_counts_by_person()
        return [(person, counts.get(person.person_id, 0)) for person in self.list_people(limit)]

    def list_chats(self, limit: int = 200) -> list[dict]:
        """会话列表：按最近消息时间排序，带消息数与最后活跃时间。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT platform, chat_id, chat_type,
                       MAX(chat_id) AS _,
                       MAX(ts) AS last_ts, COUNT(*) AS message_count,
                       MAX(sender_name) AS _n
                FROM raw_messages
                GROUP BY platform, chat_id, chat_type
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "platform": row["platform"],
                "chat_id": row["chat_id"],
                "chat_type": row["chat_type"],
                "last_ts": row["last_ts"],
                "message_count": int(row["message_count"]),
            }
            for row in rows
        ]

    def chat_counts(self) -> dict[str, int]:
        """消息总数与机器人自己发出的消息数（按会话无关的全量统计）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN sender_id = '__bot__' THEN 1 ELSE 0 END) AS bot,
                       COUNT(DISTINCT platform || ':' || chat_id) AS chats
                FROM raw_messages
                """
            ).fetchone()
        return {
            "messages": int(row["total"] or 0),
            "replies": int(row["bot"] or 0),
            "chats": int(row["chats"] or 0),
        }

    # ------------------------------------------------------------------
    # memories
    # ------------------------------------------------------------------

    def add_memory(
        self,
        person_id: str,
        content: str,
        *,
        scope_type: str = "global",
        scope_id: str = "",
        category: str = "fact",
        source: str = "extracted",
        source_ref: str = "",
        now: float | None = None,
    ) -> Memory:
        """直接写入一条记忆（不做去重；去重在 extract 层）。"""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO memories(person_id, scope_type, scope_id, content, category, source, source_ref, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (person_id, scope_type, scope_id or "", content.strip(), category, source, source_ref, now, now),
            )
            self._conn.commit()
            mem_id = int(cur.lastrowid)
        return self.get_memory(mem_id)  # type: ignore[return-value]

    def get_memory(self, memory_id: int) -> Memory | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return Memory.from_row(row) if row else None

    def list_memories(
        self,
        *,
        person_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if scope_type is not None:
            sql += " AND scope_type = ?"
            params.append(scope_type)
        if scope_id is not None:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        sql += " ORDER BY created_at DESC, id DESC" if newest_first else " ORDER BY created_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Memory.from_row(row) for row in rows]

    def update_memory(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        now: float | None = None,
    ) -> Memory | None:
        mem = self.get_memory(memory_id)
        if mem is None:
            return None
        now = time.time() if now is None else now
        new_content = mem.content if content is None else content.strip()
        new_category = mem.category if category is None else category
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET content = ?, category = ?, updated_at = ? WHERE id = ?",
                (new_content, new_category, now, memory_id),
            )
            self._conn.commit()
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def count_memories(self, person_id: str | None = None, since: float | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM memories WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if since is not None:
            sql += " AND created_at > ?"
            params.append(since)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"])

    def search_memories(
        self,
        query: str = "",
        *,
        person_id: str | None = None,
        scope_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        """按内容做简单全文搜索（LIKE，``%``/``_`` 已转义），返回 (结果, 总数)。"""
        clauses = ["1=1"]
        params: list = []
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(person_id)
        if scope_type is not None:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        keyword = (query or "").strip()
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        where = " AND ".join(clauses)
        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) AS c FROM memories WHERE {where}", params).fetchone()["c"])
            rows = self._conn.execute(
                f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, max(1, limit), max(0, offset)],
            ).fetchall()
        return [Memory.from_row(row) for row in rows], total

    def get_memories_by_ids(self, ids: Sequence[int]) -> list[Memory]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders})", ids
            ).fetchall()
        return [Memory.from_row(row) for row in rows]

    def apply_changes(
        self,
        *,
        action: str,
        delete_ids: Iterable[int] = (),
        add_specs: Iterable[dict] = (),
        reason: str = "",
        person_id: str | None = None,
        now: float | None = None,
    ) -> LogEntry:
        """原子地执行一次变更并写 memory_log（保留 before/after 全文）。

        add_specs 的字段与 ``add_memory`` 一致，可额外传 ``source`` 等。
        """
        now = time.time() if now is None else now
        delete_ids = [int(i) for i in delete_ids]
        before = self.get_memories_by_ids(delete_ids)
        added: list[Memory] = []
        with self._lock:
            try:
                for mem in before:
                    self._conn.execute("DELETE FROM memories WHERE id = ?", (mem.id,))
                for spec in add_specs:
                    cur = self._conn.execute(
                        """
                        INSERT INTO memories(person_id, scope_type, scope_id, content, category, source, source_ref, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            spec["person_id"],
                            spec.get("scope_type", "global"),
                            spec.get("scope_id", "") or "",
                            spec["content"].strip(),
                            spec.get("category", "fact"),
                            spec.get("source", "extracted"),
                            spec.get("source_ref", ""),
                            spec.get("created_at", now),
                            spec.get("updated_at", now),
                        ),
                    )
                    added.append(self.get_memory(int(cur.lastrowid)))  # type: ignore[arg-type]
                log = self._insert_log(action, person_id, before, added, reason, now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return log

    # ------------------------------------------------------------------
    # memory_log / 撤销
    # ------------------------------------------------------------------

    def _insert_log(
        self,
        action: str,
        person_id: str | None,
        before: Sequence[Memory],
        after: Sequence[Memory],
        reason: str,
        now: float,
    ) -> LogEntry:
        before_json = json.dumps([m.to_dict() for m in before], ensure_ascii=False)
        after_json = json.dumps([m.to_dict() for m in after], ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO memory_log(action, person_id, before_json, after_json, reason, created_at, reverted) VALUES(?, ?, ?, ?, ?, ?, 0)",
            (action, person_id, before_json, after_json, reason, now),
        )
        return LogEntry(
            id=int(cur.lastrowid),
            action=action,
            person_id=person_id,
            before=json.loads(before_json),
            after=json.loads(after_json),
            reason=reason,
            created_at=now,
            reverted=False,
        )

    def list_logs(self, limit: int = 50, person_id: str | None = None) -> list[LogEntry]:
        sql = "SELECT * FROM memory_log"
        params: list = []
        if person_id is not None:
            sql += " WHERE person_id = ?"
            params.append(person_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_log(row) for row in rows]

    def get_log(self, log_id: int) -> LogEntry | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memory_log WHERE id = ?", (log_id,)).fetchone()
        return self._row_to_log(row) if row else None

    @staticmethod
    def _row_to_log(row: sqlite3.Row) -> LogEntry:
        return LogEntry(
            id=row["id"],
            action=row["action"],
            person_id=row["person_id"],
            before=json.loads(row["before_json"] or "[]"),
            after=json.loads(row["after_json"] or "[]"),
            reason=row["reason"],
            created_at=row["created_at"],
            reverted=bool(row["reverted"]),
        )

    def restore(self, log_id: int) -> bool:
        """撤销一条日志：把 before 快照写回，并生成一条可反撤销的 restore 日志。"""
        log = self.get_log(log_id)
        if log is None or log.reverted:
            return False
        before_ids = {int(m["id"]) for m in log.before}
        after_ids = {int(m["id"]) for m in log.after}
        now = time.time()
        with self._lock:
            try:
                for mem_id in after_ids - before_ids:
                    self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
                restored: list[Memory] = []
                for snapshot in log.before:
                    row = dict(snapshot)
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO memories(id, person_id, scope_type, scope_id, content, category, source, source_ref, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"],
                            row["person_id"],
                            row.get("scope_type", "global"),
                            row.get("scope_id", "") or "",
                            row["content"],
                            row.get("category", "fact"),
                            row.get("source", "extracted"),
                            row.get("source_ref", ""),
                            row.get("created_at", now),
                            row.get("updated_at", now),
                        ),
                    )
                    restored.append(Memory(**row))
                current = self.get_memories_by_ids(sorted(after_ids))
                self._insert_log("restore", log.person_id, current, restored, f"撤销日志 #{log.id}", now)
                self._conn.execute("UPDATE memory_log SET reverted = 1 WHERE id = ?", (log_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return True

    # ------------------------------------------------------------------
    # raw_messages
    # ------------------------------------------------------------------

    def add_raw_message(
        self,
        *,
        platform: str,
        chat_id: str,
        sender_id: str,
        text: str,
        chat_type: str = "group",
        sender_name: str = "",
        ts: float | None = None,
        processed: bool = False,
    ) -> int:
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO raw_messages(platform, chat_id, chat_type, sender_id, sender_name, text, ts, processed)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, str(chat_id), chat_type, str(sender_id), sender_name, text, ts, 1 if processed else 0),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def recent_messages(
        self,
        chat_id: str,
        limit: int = 30,
        since: float | None = None,
        platform: str | None = None,
    ) -> list[RawMessage]:
        """按时间正序返回某会话最近的消息。"""
        sql = "SELECT * FROM raw_messages WHERE chat_id = ?"
        params: list = [str(chat_id)]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_raw(row) for row in reversed(rows)]

    def pending_senders(self) -> list[PendingSender]:
        """仍有未抽取消息的发送者，按待处理条数降序。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT platform, chat_id, chat_type, sender_id,
                       MAX(sender_name) AS sender_name, COUNT(*) AS count
                FROM raw_messages
                WHERE processed = 0 AND sender_id <> '' AND text <> ''
                GROUP BY platform, chat_id, sender_id
                ORDER BY count DESC
                """
            ).fetchall()
        return [PendingSender(**dict(row)) for row in rows]

    def pending_messages(
        self,
        platform: str,
        chat_id: str,
        sender_id: str,
        limit: int = 100,
        since: float | None = None,
    ) -> list[RawMessage]:
        sql = """
            SELECT * FROM raw_messages
            WHERE processed = 0 AND platform = ? AND chat_id = ? AND sender_id = ?
        """
        params: list = [platform, str(chat_id), str(sender_id)]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts ASC, id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_raw(row) for row in rows]

    def mark_processed(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute(f"UPDATE raw_messages SET processed = 1 WHERE id IN ({placeholders})", ids)
            self._conn.commit()

    def trim_raw(self, keep_per_chat: int = 500) -> int:
        """每个会话只保留最近 keep_per_chat 条已处理消息。"""
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM raw_messages
                WHERE processed = 1 AND id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY ts DESC, id DESC) AS rn
                        FROM raw_messages WHERE processed = 1
                    ) WHERE rn <= ?
                )
                """,
                (keep_per_chat,),
            )
            self._conn.commit()
        return cur.rowcount

    def last_message_ts(self, chat_id: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM raw_messages WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    @staticmethod
    def _row_to_raw(row: sqlite3.Row) -> RawMessage:
        return RawMessage(
            id=row["id"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            chat_type=row["chat_type"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=row["text"],
            ts=row["ts"],
        )

    # ------------------------------------------------------------------
    # meta（后台任务的游标等）
    # ------------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_meta_float(self, key: str, default: float = 0.0) -> float:
        raw = self.get_meta(key)
        try:
            return float(raw) if raw is not None else default
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # decision_log
    # ------------------------------------------------------------------

    def add_decision_log(
        self,
        *,
        platform: str,
        chat_id: str,
        chat_type: str = "group",
        sender_id: str = "",
        sender_name: str = "",
        text: str = "",
        should_reply: bool = False,
        reason: str = "",
        hard_trigger: str = "",
        score: float = 0.0,
        threshold: float = 0.0,
        factors: dict | None = None,
        now: float | None = None,
    ) -> int:
        """记录一次回复决策；WebUI 的「最近决策」直接读它。"""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO decision_log(platform, chat_id, chat_type, sender_id, sender_name, text,
                                         should_reply, reason, hard_trigger, score, threshold, factors, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    str(chat_id),
                    chat_type,
                    str(sender_id),
                    sender_name,
                    text,
                    int(bool(should_reply)),
                    reason,
                    hard_trigger or "",
                    float(score),
                    float(threshold),
                    json.dumps(factors or {}, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def list_decision_logs(
        self,
        *,
        chat_id: str | None = None,
        platform: str | None = None,
        limit: int = 50,
        since: float | None = None,
    ) -> list[dict]:
        """最近的决策，按时间倒序。"""
        sql = "SELECT * FROM decision_log WHERE 1=1"
        params: list = []
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def decision_stats(self, since: float | None = None) -> dict:
        """决策汇总：总数、回复数、硬触发数。"""
        sql = "SELECT COUNT(*) AS total, SUM(should_reply) AS replies, SUM(hard_trigger <> '') AS hard FROM decision_log"
        params: list = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(since)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        total = int(row["total"] or 0)
        replies = int(row["replies"] or 0)
        return {
            "decisions": total,
            "decision_replies": replies,
            "hard_triggers": int(row["hard"] or 0),
            "reply_rate": (replies / total) if total else 0.0,
        }

    def trim_decision_log(self, keep: int = 3000) -> int:
        """只保留最近 keep 条决策记录，避免库无限增长。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM decision_log WHERE id NOT IN (SELECT id FROM decision_log ORDER BY id DESC LIMIT ?)",
                (max(1, keep),),
            )
            self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "platform": row["platform"],
            "chat_id": row["chat_id"],
            "chat_type": row["chat_type"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "text": row["text"],
            "should_reply": bool(row["should_reply"]),
            "reason": row["reason"],
            "hard_trigger": row["hard_trigger"] or None,
            "score": row["score"],
            "threshold": row["threshold"],
            "factors": json.loads(row["factors"] or "{}"),
            "created_at": row["created_at"],
        }


def make_person_id(platform: str, platform_uid: str) -> str:
    """person_id 形如 ``snowluma:12345``；同一人不同平台暂不合并。"""
    return f"{platform}:{platform_uid}"
