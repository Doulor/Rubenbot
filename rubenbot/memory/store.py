"""SQLite 存储层：memories / memory_log / people / raw_messages，
以及记忆 v2 的 profiles / profile_evidence / events / rulings /
privacy_settings / pending_asks。

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
from typing import Any, Iterable, Sequence

# 系统/工具条目（不是用户真实发言）的 sender_id 前缀；bot 自己的消息
# ``__bot__`` 算真实消息，不在此列。窗口按真实消息计时会用到。
SYSTEM_SENDER_PREFIX = "__"
BOT_SENDER_ID = "__bot__"  # 与 decision.BOT_SENDER_ID 保持一致

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
    -- 记忆 v2：来源会话、原始受众、敏感度、事实稳定性与过期时间
    origin_chat TEXT NOT NULL DEFAULT '',          -- platform:chat_id
    audience    TEXT NOT NULL DEFAULT '',          -- private / group:<id> / public
    sensitivity TEXT NOT NULL DEFAULT 'normal',    -- normal / high / sealed
    stability   TEXT NOT NULL DEFAULT 'stable',    -- stable / temporal / uncertain
    expires_at  REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_person ON memories(person_id);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

-- 画像层（语义记忆）：每个 person 一行，内容按行存储，每行挂证据链
CREATE TABLE IF NOT EXISTS profiles (
    person_id         TEXT PRIMARY KEY,
    stable_text       TEXT NOT NULL DEFAULT '',   -- 稳定特性，一行一条
    status_text       TEXT NOT NULL DEFAULT '',   -- 当前近况，一行一条
    status_expires_at REAL,                       -- 近况整体过期时间
    hooks             TEXT NOT NULL DEFAULT '',   -- 逗号分隔的话题关键词
    updated_at        REAL NOT NULL
);

-- 画像每行 → 证据（event:<id> / memory:<id>）的映射
CREATE TABLE IF NOT EXISTS profile_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     TEXT NOT NULL,
    section       TEXT NOT NULL,                  -- stable / status
    line_no       INTEGER NOT NULL,               -- 该节第几行（0 起）
    evidence_refs TEXT NOT NULL DEFAULT '[]',     -- JSON 数组
    UNIQUE(person_id, section, line_no)
);
CREATE INDEX IF NOT EXISTS idx_profile_evidence_person ON profile_evidence(person_id);

-- 时间线层（情景记忆）：发生过的具体事；bot 自己用保留 person_id 'self'
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   TEXT NOT NULL,
    ts          REAL NOT NULL,                    -- 事件发生时间
    summary     TEXT NOT NULL,
    source_chat TEXT NOT NULL DEFAULT '',         -- platform:chat_id
    audience    TEXT NOT NULL DEFAULT '',         -- private / group:<id> / public
    sensitivity TEXT NOT NULL DEFAULT 'normal',   -- normal / high / sealed
    salience    REAL NOT NULL DEFAULT 0.5,        -- 显著度 0-1
    compressed  INTEGER NOT NULL DEFAULT 0,       -- 1 = 已压进 digest
    kind        TEXT NOT NULL DEFAULT 'event',    -- event / digest
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_person_ts ON events(person_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_compressed ON events(compressed, ts);

-- 分寸裁定：本人对「某类目/某件事」的可说与否；判断逻辑在读取侧，这张表只存结论
CREATE TABLE IF NOT EXISTS rulings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT NOT NULL,
    key        TEXT NOT NULL,                     -- 类目（health/finance/...）或具体事件 id
    verdict    TEXT NOT NULL DEFAULT 'deny',      -- allow / deny
    created_at REAL NOT NULL,
    UNIQUE(person_id, key)
);

-- 按人三档隐私：normal / private / norecord
CREATE TABLE IF NOT EXISTS privacy_settings (
    person_id  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL DEFAULT 'normal',
    updated_at REAL NOT NULL
);

-- 灰区异步询问：判断没把握的敏感事项，攒到私聊里自然时机问本人
CREATE TABLE IF NOT EXISTS pending_asks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT NOT NULL,
    item_ref   TEXT NOT NULL,                     -- 相关记忆/事件引用
    question   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / answered / expired
    verdict    TEXT NOT NULL DEFAULT '',          -- allow / deny
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_asks_person ON pending_asks(person_id, status);

CREATE TABLE IF NOT EXISTS raw_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_type   TEXT NOT NULL DEFAULT 'group',
    sender_id   TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    ts          REAL NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0,
    -- 平台消息 id 与它引用的消息 id（入站/出站都记，用于引用解析与自我时间线）
    platform_msg_id TEXT NOT NULL DEFAULT '',
    reply_to_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raw_chat ON raw_messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_raw_pending ON raw_messages(processed, sender_id);
CREATE INDEX IF NOT EXISTS idx_raw_platform_msg ON raw_messages(platform, platform_msg_id);

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

-- LLM 用量：每次请求尝试一行（重试的失败尝试也记，ok 区分）
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    task              TEXT NOT NULL DEFAULT 'chat',
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER,                  -- 上游没给 usage 时为 NULL
    completion_tokens INTEGER,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_llm_usage_task ON llm_usage(task);

-- 会话滚动摘要：每个会话一行，早期对话压成结构化摘要；covered_until_msg_id
-- 之前的原文已被摘要覆盖，拼上下文时不再重复带入。
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id      TEXT PRIMARY KEY,        -- platform:chat_id
    summary              TEXT NOT NULL DEFAULT '',
    covered_until_msg_id INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL
);

-- 定时任务：一期只支持一次性时间点（提醒）；进程重启后 pending 任务继续生效
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'reminder',  -- reminder / 未来的其他类型，别混用
    platform   TEXT NOT NULL DEFAULT '',
    chat_id    TEXT NOT NULL DEFAULT '',
    chat_type  TEXT NOT NULL DEFAULT 'group',
    fire_at    REAL NOT NULL,                     -- 到点时间（epoch 秒）
    payload    TEXT NOT NULL DEFAULT '{}',        -- JSON，提醒内容等
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / done / cancelled
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    fired_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_jobs(status, fire_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_chat ON scheduled_jobs(platform, chat_id, status);

-- 好感度：每人一行（惰性折算衰减，读时按 updated_at 算，不落衰减值）
CREATE TABLE IF NOT EXISTS person_affinity (
    person_id  TEXT PRIMARY KEY,
    score      REAL NOT NULL DEFAULT 50,
    updated_at REAL NOT NULL
);

-- 好感度变化流水：每笔加减（含衰减）都记下来，WebUI 可查
CREATE TABLE IF NOT EXISTS affinity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT NOT NULL,
    delta      REAL NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    place      TEXT NOT NULL DEFAULT '',          -- platform:chat_id，便于按场合翻
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_affinity_log_person ON affinity_log(person_id, created_at);

-- 心情（一期全局一份）：单行，id 固定为 1
CREATE TABLE IF NOT EXISTS bot_mood (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    mood       TEXT NOT NULL DEFAULT '平静',
    intensity  REAL NOT NULL DEFAULT 0,
    since      REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

-- 平台事件（感知面）：notice / request 归一后的摘要；消息事件不进这里。
-- pending=1 的申请类事件在未处理前可计数（WebUI 的「红点」）。
CREATE TABLE IF NOT EXISTS platform_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    platform       TEXT NOT NULL,
    kind           TEXT NOT NULL,                  -- notice / request
    type           TEXT NOT NULL,
    sub_type       TEXT NOT NULL DEFAULT '',
    actor_id       TEXT NOT NULL DEFAULT '',
    actor_name     TEXT NOT NULL DEFAULT '',
    target_id      TEXT NOT NULL DEFAULT '',
    conversation_type TEXT NOT NULL DEFAULT 'private',
    conversation_id   TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    raw            TEXT NOT NULL DEFAULT '{}',     -- 原始载荷摘要（JSON）
    platform_event_id TEXT NOT NULL DEFAULT '',
    pending        INTEGER NOT NULL DEFAULT 0,
    route          TEXT NOT NULL DEFAULT 'record',  -- judge / conditional / record
    verdict        TEXT NOT NULL DEFAULT '',        -- 快判结论（ignore/react/...）
    created_at     REAL NOT NULL,
    ts             REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_platform_events_created ON platform_events(created_at);
CREATE INDEX IF NOT EXISTS idx_platform_events_pending ON platform_events(pending, kind);

-- 网关审计：每次调用（含被拒绝）一行；sensitive 动作标红展示
CREATE TABLE IF NOT EXISTS gateway_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    source       TEXT NOT NULL DEFAULT 'model',     -- model / event / system
    platform     TEXT NOT NULL DEFAULT '',
    chat_id      TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',        -- 参数摘要（JSON，已截断）
    ok           INTEGER NOT NULL DEFAULT 0,
    result       TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    sensitive    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gateway_audit_ts ON gateway_audit(ts);
CREATE INDEX IF NOT EXISTS idx_gateway_audit_action ON gateway_audit(action, ts);

-- 网关冷却状态：四维冷却里需要跨重启保留的部分
-- scope: conversation（动作×会话）/ object（动作×对象）/ budget（会话日预算）
--        / global（全局日上限）/ backoff（动作连续失败退避）
-- value 是 JSON：数字或时间戳数组，按 scope 的语义解析（见 gateway/gatekeeper.py）
CREATE TABLE IF NOT EXISTS gateway_state (
    scope     TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL DEFAULT '0',
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
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
    # 记忆 v2 元数据
    origin_chat: str = ""
    audience: str = ""
    sensitivity: str = "normal"
    stability: str = "stable"
    expires_at: float | None = None
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


# bot 自己在时间线里的保留 person_id（见 SPEC「内容结构：两层 + 自我」）
SELF_PERSON_ID = "self"

VALID_SENSITIVITY = {"normal", "high", "sealed"}
VALID_STABILITY = {"stable", "temporal", "uncertain"}
VALID_PRIVACY_MODES = {"normal", "private", "norecord"}


@dataclass
class Event:
    """时间线层的一个事件：谁、何时、在哪个会话、发生了什么事。"""

    id: int | None = None
    person_id: str = ""
    ts: float = field(default_factory=time.time)
    summary: str = ""
    source_chat: str = ""
    audience: str = ""
    sensitivity: str = "normal"
    salience: float = 0.5
    compressed: bool = False
    kind: str = "event"  # event / digest
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        data = dict(row)
        data["compressed"] = bool(data.get("compressed", 0))
        data["salience"] = float(data.get("salience") or 0.0)
        return cls(**data)


@dataclass
class ProfileLine:
    """画像里的一行结论，附带证据链（event/memory 引用）。"""

    text: str
    evidence_refs: list[str] = field(default_factory=list)
    line_no: int = 0
    # 近况类的估计有效天数（只用于计算 status_expires_at，不落库到证据表）
    expires_in_days: int | None = None


@dataclass
class Profile:
    person_id: str
    stable_text: str = ""
    status_text: str = ""
    status_expires_at: float | None = None
    hooks: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    @property
    def stable_lines(self) -> list[str]:
        return [line for line in self.stable_text.splitlines() if line.strip()]

    @property
    def status_lines(self) -> list[str]:
        return [line for line in self.status_text.splitlines() if line.strip()]


@dataclass
class Ruling:
    id: int
    person_id: str
    key: str
    verdict: str  # allow / deny
    created_at: float


@dataclass
class PendingAsk:
    id: int
    person_id: str
    item_ref: str
    question: str
    status: str  # pending / answered / expired
    verdict: str
    created_at: float


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
    platform_msg_id: str = ""
    reply_to_id: str = ""


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
class ConversationSummary:
    """某个会话的滚动摘要（每个会话一行；covered_until 之前的原文已被覆盖）。"""

    conversation_id: str
    summary: str = ""
    covered_until_msg_id: int = 0
    updated_at: float = 0.0


@dataclass
class ScheduledJob:
    """一条定时任务（一期只有提醒）。

    ``payload`` 是 JSON 字符串：提醒的正文、创建会话等随任务走的附加信息。
    状态：``pending`` 等待执行，``done`` 已执行（含迟到标记），
    ``cancelled`` 被取消（一期没有取消工具，留列给以后）。
    """

    id: int | None = None
    kind: str = "reminder"
    platform: str = ""
    chat_id: str = ""
    chat_type: str = "group"
    fire_at: float = 0.0
    payload: dict = field(default_factory=dict)
    status: str = "pending"
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    fired_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AffinityEntry:
    """好感度变化流水里的一笔。"""

    id: int
    person_id: str
    delta: float
    reason: str
    place: str
    created_at: float


@dataclass
class BotMood:
    """bot 当前的心情（一期全局一份）。"""

    mood: str = "平静"
    intensity: float = 0.0
    since: float = 0.0
    updated_at: float = 0.0


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
            # 先给旧库补列，再执行建表/建索引脚本（索引引用了新列）
            _migrate_raw_messages(self._conn)
            _migrate_memories(self._conn)
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
        origin_chat: str = "",
        audience: str = "",
        sensitivity: str = "normal",
        stability: str = "stable",
        expires_at: float | None = None,
        now: float | None = None,
    ) -> Memory:
        """直接写入一条记忆（不做去重；去重在 extract 层）。"""
        now = time.time() if now is None else now
        if sensitivity not in VALID_SENSITIVITY:
            sensitivity = "normal"
        if stability not in VALID_STABILITY:
            stability = "stable"
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO memories(person_id, scope_type, scope_id, content, category, source, source_ref,
                                     origin_chat, audience, sensitivity, stability, expires_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    scope_type,
                    scope_id or "",
                    content.strip(),
                    category,
                    source,
                    source_ref,
                    origin_chat,
                    audience,
                    sensitivity,
                    stability,
                    expires_at,
                    now,
                    now,
                ),
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
                        INSERT INTO memories(person_id, scope_type, scope_id, content, category, source, source_ref,
                                             origin_chat, audience, sensitivity, stability, expires_at, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            spec["person_id"],
                            spec.get("scope_type", "global"),
                            spec.get("scope_id", "") or "",
                            spec["content"].strip(),
                            spec.get("category", "fact"),
                            spec.get("source", "extracted"),
                            spec.get("source_ref", ""),
                            spec.get("origin_chat", ""),
                            spec.get("audience", ""),
                            spec.get("sensitivity", "normal"),
                            spec.get("stability", "stable"),
                            spec.get("expires_at"),
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
        before,
        after,
        reason: str,
        now: float,
    ) -> LogEntry:
        before_json = json.dumps(
            [m.to_dict() if hasattr(m, "to_dict") else m for m in before], ensure_ascii=False
        )
        after_json = json.dumps(
            [m.to_dict() if hasattr(m, "to_dict") else m for m in after], ensure_ascii=False
        )
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

    def latest_log_id(self, person_id: str, *, action: str | None = None) -> int | None:
        """某人的最新一条日志 id（画像提炼写日志后取回用）。"""
        sql = "SELECT id FROM memory_log WHERE person_id = ?"
        params: list = [person_id]
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["id"]) if row else None

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
        """撤销一条日志：把 before 快照写回，并生成一条可反撤销的 restore 日志。

        只有记忆变更日志能撤销；画像变更（``profile_update``）只作审计留痕，
        本人要求清空（``purge``）是真删，绝不能从日志里撤销回来。
        """
        log = self.get_log(log_id)
        if log is None or log.reverted or log.action in ("profile_update", "purge"):
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
                        INSERT OR REPLACE INTO memories(id, person_id, scope_type, scope_id, content, category, source, source_ref,
                                                        origin_chat, audience, sensitivity, stability, expires_at, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            row.get("origin_chat", ""),
                            row.get("audience", ""),
                            row.get("sensitivity", "normal"),
                            row.get("stability", "stable"),
                            row.get("expires_at"),
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
    # events（时间线层）与自我时间线
    # ------------------------------------------------------------------

    def add_event(
        self,
        person_id: str,
        summary: str,
        *,
        ts: float | None = None,
        source_chat: str = "",
        audience: str = "",
        sensitivity: str = "normal",
        salience: float = 0.5,
        kind: str = "event",
        compressed: bool = False,
        now: float | None = None,
    ) -> Event:
        """记一条时间线事件。``person_id='self'`` 表示 bot 自己的行为。"""
        now = time.time() if now is None else now
        ts = now if ts is None else float(ts)
        if sensitivity not in VALID_SENSITIVITY:
            sensitivity = "normal"
        salience = min(1.0, max(0.0, float(salience)))
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events(person_id, ts, summary, source_chat, audience, sensitivity,
                                   salience, compressed, kind, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    ts,
                    summary.strip(),
                    source_chat,
                    audience,
                    sensitivity,
                    salience,
                    1 if compressed else 0,
                    kind,
                    now,
                ),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)
        return self.get_event(event_id)  # type: ignore[return-value]

    def add_self_event(
        self,
        summary: str,
        *,
        source_chat: str = "",
        audience: str = "",
        ts: float | None = None,
        now: float | None = None,
    ) -> Event:
        """bot 自己发出的消息/行为进自我时间线（保留 person_id ``self``）。"""
        return self.add_event(
            SELF_PERSON_ID,
            summary,
            ts=ts,
            source_chat=source_chat,
            audience=audience,
            sensitivity="normal",
            salience=0.4,
            now=now,
        )

    def get_event(self, event_id: int) -> Event | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def list_events(
        self,
        *,
        person_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        created_since: float | None = None,
        include_compressed: bool = True,
        kinds: Sequence[str] | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[Event]:
        """``since``/``until`` 按事件时间（ts）过滤；``created_since`` 按入库时间过滤。

        画像提炼要的是「上次提炼之后新入库的」事件，用 ``created_since``。
        """
        sql = "SELECT * FROM events WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(float(since))
        if until is not None:
            sql += " AND ts < ?"
            params.append(float(until))
        if created_since is not None:
            sql += " AND created_at > ?"
            params.append(float(created_since))
        if not include_compressed:
            sql += " AND compressed = 0"
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY ts DESC, id DESC" if newest_first else " ORDER BY ts ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Event.from_row(row) for row in rows]

    def count_events(self, person_id: str | None = None, since: float | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM events WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if since is not None:
            sql += " AND created_at > ?"
            params.append(float(since))
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"])

    def get_events_by_ids(self, ids: Sequence[int]) -> list[Event]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})", ids
            ).fetchall()
        return [Event.from_row(row) for row in rows]

    def mark_events_compressed(self, ids: Sequence[int]) -> int:
        """把若干子事件标记为已压缩（只改 compressed 标志，不删数据）。"""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE events SET compressed = 1 WHERE id IN ({placeholders})", ids
            )
            self._conn.commit()
        return cur.rowcount

    def prune_events(self, cutoff: float, keep_salience: float = 0.7) -> int:
        """90 天前只保留高显著度事件与 digest；其余标记 compressed（不删数据）。"""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE events SET compressed = 1
                WHERE compressed = 0 AND ts < ? AND kind = 'event' AND salience < ?
                """,
                (float(cutoff), float(keep_salience)),
            )
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # profiles / profile_evidence（画像层）
    # ------------------------------------------------------------------

    def get_profile(self, person_id: str) -> Profile | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM profiles WHERE person_id = ?", (person_id,)).fetchone()
        if row is None:
            return None
        hooks = [item.strip() for item in (row["hooks"] or "").split(",") if item.strip()]
        return Profile(
            person_id=row["person_id"],
            stable_text=row["stable_text"],
            status_text=row["status_text"],
            status_expires_at=row["status_expires_at"],
            hooks=hooks,
            updated_at=row["updated_at"],
        )

    def list_profiles(self, limit: int = 500) -> list[Profile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM profiles ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        profiles: list[Profile] = []
        for row in rows:
            hooks = [item.strip() for item in (row["hooks"] or "").split(",") if item.strip()]
            profiles.append(
                Profile(
                    person_id=row["person_id"],
                    stable_text=row["stable_text"],
                    status_text=row["status_text"],
                    status_expires_at=row["status_expires_at"],
                    hooks=hooks,
                    updated_at=row["updated_at"],
                )
            )
        return profiles

    def save_profile(
        self,
        person_id: str,
        *,
        stable_lines: Sequence[ProfileLine],
        status_lines: Sequence[ProfileLine],
        status_expires_at: float | None = None,
        hooks: Sequence[str] = (),
        now: float | None = None,
        log_reason: str = "",
    ) -> Profile:
        """整体替换一个人的画像（含证据链），并可选写一条 ``profile_update`` 日志。

        每行结论的证据链存进 ``profile_evidence``，行号与文本行一一对应。
        """
        now = time.time() if now is None else now
        stable_text = "\n".join(line.text.strip() for line in stable_lines if line.text.strip())
        status_text = "\n".join(line.text.strip() for line in status_lines if line.text.strip())
        hooks_text = ",".join(dict.fromkeys(h.strip() for h in hooks if h and h.strip()))
        with self._lock:
            try:
                old = self.get_profile(person_id)
                self._conn.execute(
                    """
                    INSERT INTO profiles(person_id, stable_text, status_text, status_expires_at, hooks, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(person_id) DO UPDATE SET
                        stable_text = excluded.stable_text,
                        status_text = excluded.status_text,
                        status_expires_at = excluded.status_expires_at,
                        hooks = excluded.hooks,
                        updated_at = excluded.updated_at
                    """,
                    (person_id, stable_text, status_text, status_expires_at, hooks_text, now),
                )
                self._conn.execute("DELETE FROM profile_evidence WHERE person_id = ?", (person_id,))
                for section, lines in (("stable", stable_lines), ("status", status_lines)):
                    line_no = 0
                    for line in lines:
                        if not line.text.strip():
                            continue
                        self._conn.execute(
                            """
                            INSERT INTO profile_evidence(person_id, section, line_no, evidence_refs)
                            VALUES(?, ?, ?, ?)
                            """,
                            (
                                person_id,
                                section,
                                line_no,
                                json.dumps(list(line.evidence_refs), ensure_ascii=False),
                            ),
                        )
                        line_no += 1
                if log_reason:
                    log = self._insert_profile_log(person_id, old, stable_text, status_text, log_reason, now)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_profile(person_id)  # type: ignore[return-value]

    def _insert_profile_log(
        self,
        person_id: str,
        old: Profile | None,
        stable_text: str,
        status_text: str,
        reason: str,
        now: float,
    ) -> LogEntry:
        """画像变更也写进 memory_log 留痕（仅审计，不能用 restore 回滚）。"""

        def snapshot(profile: Profile | None) -> list[dict]:
            if profile is None:
                return []
            items: list[dict] = []
            for line in profile.stable_lines:
                items.append({"content": line, "section": "stable"})
            for line in profile.status_lines:
                items.append({"content": line, "section": "status"})
            return items

        new_items = [{"content": line, "section": "stable"} for line in stable_text.splitlines() if line.strip()]
        new_items += [{"content": line, "section": "status"} for line in status_text.splitlines() if line.strip()]
        return self._insert_log("profile_update", person_id, snapshot(old), new_items, reason, now)

    def get_profile_evidence(self, person_id: str) -> dict[tuple[str, int], list[str]]:
        """返回 {(section, line_no): [evidence_ref, ...]}。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT section, line_no, evidence_refs FROM profile_evidence WHERE person_id = ? ORDER BY section, line_no",
                (person_id,),
            ).fetchall()
        result: dict[tuple[str, int], list[str]] = {}
        for row in rows:
            try:
                refs = json.loads(row["evidence_refs"] or "[]")
            except (TypeError, ValueError):
                refs = []
            result[(row["section"], int(row["line_no"]))] = [str(r) for r in refs]
        return result

    # ------------------------------------------------------------------
    # privacy_settings（按人三档隐私）
    # ------------------------------------------------------------------

    def get_privacy_mode(self, person_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT mode FROM privacy_settings WHERE person_id = ?", (person_id,)
            ).fetchone()
        mode = row["mode"] if row else "normal"
        return mode if mode in VALID_PRIVACY_MODES else "normal"

    def set_privacy_mode(self, person_id: str, mode: str, *, now: float | None = None) -> str:
        if mode not in VALID_PRIVACY_MODES:
            raise ValueError(f"未知隐私档位：{mode}")
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO privacy_settings(person_id, mode, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at
                """,
                (person_id, mode, now),
            )
            self._conn.commit()
        return mode

    def is_norecord(self, person_id: str) -> bool:
        return self.get_privacy_mode(person_id) == "norecord"

    # ------------------------------------------------------------------
    # rulings（分寸裁定存储；判断逻辑在读取侧）
    # ------------------------------------------------------------------

    def set_ruling(
        self, person_id: str, key: str, verdict: str, *, now: float | None = None
    ) -> Ruling:
        if verdict not in ("allow", "deny"):
            raise ValueError(f"未知裁定：{verdict}")
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rulings(person_id, key, verdict, created_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(person_id, key) DO UPDATE SET verdict = excluded.verdict, created_at = excluded.created_at
                """,
                (person_id, key, verdict, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM rulings WHERE person_id = ? AND key = ?", (person_id, key)
            ).fetchone()
        return Ruling(**dict(row))  # type: ignore[arg-type]

    def get_ruling(self, person_id: str, key: str) -> Ruling | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rulings WHERE person_id = ? AND key = ?", (person_id, key)
            ).fetchone()
        return Ruling(**dict(row)) if row else None

    def list_rulings(self, person_id: str) -> list[Ruling]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rulings WHERE person_id = ? ORDER BY created_at DESC, id DESC",
                (person_id,),
            ).fetchall()
        return [Ruling(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # pending_asks（灰区异步询问）
    # ------------------------------------------------------------------

    def add_pending_ask(
        self,
        person_id: str,
        item_ref: str,
        question: str,
        *,
        now: float | None = None,
    ) -> PendingAsk:
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO pending_asks(person_id, item_ref, question, status, verdict, created_at)
                VALUES(?, ?, ?, 'pending', '', ?)
                """,
                (person_id, item_ref, question.strip(), now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM pending_asks WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
        return PendingAsk(**dict(row))  # type: ignore[arg-type]

    def list_pending_asks(
        self, person_id: str | None = None, status: str | None = "pending", limit: int = 100
    ) -> list[PendingAsk]:
        sql = "SELECT * FROM pending_asks WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [PendingAsk(**dict(row)) for row in rows]

    def answer_pending_ask(
        self, ask_id: int, verdict: str, *, now: float | None = None
    ) -> PendingAsk | None:
        """本人回答了询问：写状态，并顺手落一条裁定。"""
        if verdict not in ("allow", "deny"):
            raise ValueError(f"未知裁定：{verdict}")
        with self._lock:
            row = self._conn.execute("SELECT * FROM pending_asks WHERE id = ?", (ask_id,)).fetchone()
            if row is None:
                return None
            ask = PendingAsk(**dict(row))
            if ask.status != "pending":
                return ask
            self._conn.execute(
                "UPDATE pending_asks SET status = 'answered', verdict = ? WHERE id = ?", (verdict, ask_id)
            )
            updated = self._conn.execute("SELECT * FROM pending_asks WHERE id = ?", (ask_id,)).fetchone()
            self._conn.commit()
        if ask.item_ref:
            self.set_ruling(ask.person_id, ask.item_ref, verdict, now=now)
        return PendingAsk(**dict(updated))

    def expire_pending_asks(self, older_than: float) -> int:
        """把 created_at 早于 ``older_than`` 的待询问标记为已过期。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_asks SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
                (float(older_than),),
            )
            self._conn.commit()
        return cur.rowcount

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
        platform_msg_id: str = "",
        reply_to_id: str = "",
    ) -> int:
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO raw_messages(platform, chat_id, chat_type, sender_id, sender_name, text, ts, processed,
                                         platform_msg_id, reply_to_id)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    str(chat_id),
                    chat_type,
                    str(sender_id),
                    sender_name,
                    text,
                    ts,
                    1 if processed else 0,
                    str(platform_msg_id or ""),
                    str(reply_to_id or ""),
                ),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def platform_message_exists(
        self,
        platform: str,
        platform_msg_id: str,
        *,
        since: float | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """平台消息 id 是否已在流水里（入站去重用）；``since`` 限定时间窗。"""
        if not platform_msg_id:
            return False
        sql = "SELECT 1 FROM raw_messages WHERE platform = ? AND platform_msg_id = ? AND platform_msg_id <> ''"
        params: list = [platform, str(platform_msg_id)]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row is not None

    def message_by_platform_id(
        self,
        platform: str,
        platform_msg_id: str,
        *,
        chat_id: str | None = None,
    ) -> RawMessage | None:
        """按平台消息 id 反查流水里的一条消息（引用解析用）。"""
        if not platform_msg_id:
            return None
        sql = "SELECT * FROM raw_messages WHERE platform = ? AND platform_msg_id = ?"
        params: list = [platform, str(platform_msg_id)]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return self._row_to_raw(row) if row else None

    def recent_messages(
        self,
        chat_id: str,
        limit: int = 30,
        since: float | None = None,
        platform: str | None = None,
        *,
        real_only: bool = False,
        after_id: int = 0,
        sender_id: str | None = None,
        chat_type: str | None = None,
    ) -> list[RawMessage]:
        """按时间正序返回某会话最近的消息。

        ``real_only=True`` 时排除系统/工具条目（sender_id 以 ``__`` 开头，
        bot 自己的 ``__bot__`` 不算），窗口只按真实消息计。``after_id`` 只取
        流水中 id 更大的消息（滚动摘要已覆盖的部分不再带入）。
        """
        sql = "SELECT * FROM raw_messages WHERE chat_id = ?"
        params: list = [str(chat_id)]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if sender_id is not None:
            sql += " AND sender_id = ?"
            params.append(sender_id)
        if chat_type is not None:
            sql += " AND chat_type = ?"
            params.append(chat_type)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if after_id:
            sql += " AND id > ?"
            params.append(int(after_id))
        if real_only:
            # 不用 LIKE：``_`` 是 LIKE 的通配符，这里按前缀切片精确比较
            sql += " AND (substr(sender_id, 1, ?) <> ? OR sender_id = ?)"
            params.extend([len(SYSTEM_SENDER_PREFIX), SYSTEM_SENDER_PREFIX, BOT_SENDER_ID])
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

    # ------------------------------------------------------------------
    # conversation_summaries（会话滚动摘要）
    # ------------------------------------------------------------------

    def get_conversation_summary(self, conversation_id: str) -> ConversationSummary | None:
        """取某个会话的摘要；没有摘要返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
        return ConversationSummary(**dict(row)) if row else None

    def set_conversation_summary(
        self,
        conversation_id: str,
        summary: str,
        covered_until_msg_id: int,
        *,
        now: float | None = None,
    ) -> None:
        """写入/更新某个会话的摘要与覆盖游标（只增不减由调用方保证）。"""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversation_summaries(conversation_id, summary, covered_until_msg_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary = excluded.summary,
                    covered_until_msg_id = excluded.covered_until_msg_id,
                    updated_at = excluded.updated_at
                """,
                (str(conversation_id), summary, int(covered_until_msg_id), now),
            )
            self._conn.commit()

    def raw_messages_after(
        self,
        chat_id: str,
        *,
        platform: str | None = None,
        after_id: int = 0,
        limit: int | None = None,
        real_only: bool = False,
    ) -> list[RawMessage]:
        """按时间正序取 raw_messages.id > ``after_id`` 的消息（摘要增量用）。"""
        sql = "SELECT * FROM raw_messages WHERE chat_id = ? AND id > ?"
        params: list = [str(chat_id), int(after_id)]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if real_only:
            sql += " AND (substr(sender_id, 1, ?) <> ? OR sender_id = ?)"
            params.extend([len(SYSTEM_SENDER_PREFIX), SYSTEM_SENDER_PREFIX, BOT_SENDER_ID])
        sql += " ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_raw(row) for row in rows]

    def count_raw_messages_after(
        self,
        chat_id: str,
        *,
        platform: str | None = None,
        after_id: int = 0,
        real_only: bool = False,
    ) -> int:
        """统计 ``after_id`` 之后的原文条数（摘要触发判断用）。"""
        sql = "SELECT COUNT(*) AS c FROM raw_messages WHERE chat_id = ? AND id > ?"
        params: list = [str(chat_id), int(after_id)]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if real_only:
            sql += " AND (substr(sender_id, 1, ?) <> ? OR sender_id = ?)"
            params.extend([len(SYSTEM_SENDER_PREFIX), SYSTEM_SENDER_PREFIX, BOT_SENDER_ID])
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"])

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
            platform_msg_id=row["platform_msg_id"] or "",
            reply_to_id=row["reply_to_id"] or "",
        )

    def active_senders(
        self,
        *,
        platform: str | None = None,
        chat_id: str | None = None,
        since: float,
        limit: int | None = None,
    ) -> list[dict]:
        """近期活跃的发送者：``[{sender_id, sender_name, last_ts, count}, ...]``。

        ``chat_id`` 给定则只看该会话（群成员索引用）；不给则跨会话汇总，
        每个 sender_id 一行（跨会话摘要用）。系统条目与 bot 自己都排除
        （``__bot__`` 也以 ``__`` 开头，不能像 ``recent_messages`` 那样单独放行）。
        """
        sql = """
            SELECT sender_id, MAX(sender_name) AS sender_name,
                   MAX(ts) AS last_ts, COUNT(*) AS count
            FROM raw_messages
            WHERE ts >= ? AND sender_id <> ''
              AND substr(sender_id, 1, ?) <> ?
        """
        params: list = [float(since), len(SYSTEM_SENDER_PREFIX), SYSTEM_SENDER_PREFIX]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " GROUP BY sender_id ORDER BY last_ts DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"] or "",
                "last_ts": float(row["last_ts"] or 0.0),
                "count": int(row["count"] or 0),
            }
            for row in rows
        ]

    def profile_evidence_refs(self, person_id: str) -> set[str]:
        """某人画像行挂着的全部证据引用（``event:12`` / ``memory:3``）。"""
        return {ref for refs in self.get_profile_evidence(person_id).values() for ref in refs}

    def purge_person(self, person_id: str, *, reason: str = "", now: float | None = None) -> dict:
        """彻底删除某人的记忆、事件、画像、裁定与待询问，并在 memory_log 留痕。

        返回各类被删条数。``memory_log`` 只记数量与原因，不把被删正文再留存
        一份（否则「清空」就不算真删了）；这条日志标记为不可撤销。
        """
        now = time.time() if now is None else now
        with self._lock:
            try:
                counts = {
                    "memories": int(
                        self._conn.execute(
                            "SELECT COUNT(*) AS c FROM memories WHERE person_id = ?", (person_id,)
                        ).fetchone()["c"]
                    ),
                    "events": int(
                        self._conn.execute(
                            "SELECT COUNT(*) AS c FROM events WHERE person_id = ?", (person_id,)
                        ).fetchone()["c"]
                    ),
                    "profile": 1 if self.get_profile(person_id) is not None else 0,
                    "rulings": int(
                        self._conn.execute(
                            "SELECT COUNT(*) AS c FROM rulings WHERE person_id = ?", (person_id,)
                        ).fetchone()["c"]
                    ),
                    "pending_asks": int(
                        self._conn.execute(
                            "SELECT COUNT(*) AS c FROM pending_asks WHERE person_id = ?", (person_id,)
                        ).fetchone()["c"]
                    ),
                    # 好感度流水条数；person_affinity 本身只算一行
                    "affinity_logs": int(
                        self._conn.execute(
                            "SELECT COUNT(*) AS c FROM affinity_log WHERE person_id = ?", (person_id,)
                        ).fetchone()["c"]
                    ),
                    "affinity": 1 if self.get_affinity(person_id) is not None else 0,
                }
                self._conn.execute("DELETE FROM memories WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM events WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM profile_evidence WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM profiles WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM rulings WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM pending_asks WHERE person_id = ?", (person_id,))
                # 好感度与流水也属于「关于这个人的记录」，一起清掉
                self._conn.execute("DELETE FROM affinity_log WHERE person_id = ?", (person_id,))
                self._conn.execute("DELETE FROM person_affinity WHERE person_id = ?", (person_id,))
                self._conn.execute(
                    "INSERT INTO memory_log(action, person_id, before_json, after_json, reason, created_at, reverted)"
                    " VALUES('purge', ?, '[]', '[]', ?, ?, 1)",
                    (
                        person_id,
                        (
                            f"{reason or '本人要求清空记忆'}；删除计数="
                            f"{json.dumps(counts, ensure_ascii=False)}"
                            "；正文不保留，不可撤销"
                        ),
                        now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return counts

    # ------------------------------------------------------------------
    # scheduled_jobs（定时任务；一期只有提醒）
    # ------------------------------------------------------------------

    def add_scheduled_job(
        self,
        *,
        kind: str,
        fire_at: float,
        platform: str,
        chat_id: str,
        chat_type: str = "group",
        payload: dict | None = None,
        created_by: str = "",
        now: float | None = None,
    ) -> ScheduledJob:
        """新增一条待执行的定时任务。"""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO scheduled_jobs(kind, platform, chat_id, chat_type, fire_at, payload,
                                           status, created_by, created_at, fired_at)
                VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
                """,
                (
                    str(kind or "reminder"),
                    str(platform),
                    str(chat_id),
                    str(chat_type or "group"),
                    float(fire_at),
                    json.dumps(payload or {}, ensure_ascii=False),
                    str(created_by or ""),
                    now,
                ),
            )
            self._conn.commit()
            job_id = int(cur.lastrowid)
        return self.get_scheduled_job(job_id)  # type: ignore[return-value]

    def get_scheduled_job(self, job_id: int) -> ScheduledJob | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?", (int(job_id),)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def due_scheduled_jobs(self, now: float, *, limit: int = 50) -> list[ScheduledJob]:
        """到期且仍待执行的任务，按到期时间先后返回。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM scheduled_jobs
                WHERE status = 'pending' AND fire_at <= ?
                ORDER BY fire_at ASC, id ASC LIMIT ?
                """,
                (float(now), max(1, int(limit))),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def list_scheduled_jobs(
        self,
        *,
        status: str | None = "pending",
        kind: str | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
        limit: int = 100,
    ) -> list[ScheduledJob]:
        sql = "SELECT * FROM scheduled_jobs WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " ORDER BY fire_at ASC, id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def count_pending_jobs(
        self,
        *,
        kind: str | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
    ) -> int:
        """统计待执行任务数（提醒上限检查用）。"""
        sql = "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE status = 'pending'"
        params: list = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def finish_scheduled_job(
        self, job_id: int, *, status: str = "done", now: float | None = None
    ) -> bool:
        """把任务置为 done / cancelled；只有 pending 的会被改动。"""
        if status not in ("done", "cancelled"):
            raise ValueError(f"未知任务状态：{status}")
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs SET status = ?, fired_at = ? WHERE id = ? AND status = 'pending'",
                (status, now, int(job_id)),
            )
            self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return ScheduledJob(
            id=row["id"],
            kind=row["kind"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            chat_type=row["chat_type"],
            fire_at=row["fire_at"],
            payload=payload if isinstance(payload, dict) else {},
            status=row["status"],
            created_by=row["created_by"] or "",
            created_at=row["created_at"],
            fired_at=row["fired_at"],
        )

    # ------------------------------------------------------------------
    # 好感度与心情（情绪系统的存储侧；规则计算在 rubenbot.emotion）
    # ------------------------------------------------------------------

    def get_affinity(self, person_id: str) -> float | None:
        """读某人存下来的好感度原始值；没有记录返回 None（由规则层用基线）。

        注意这里不做衰减折算：衰减是规则层按 ``updated_at`` 惰性算的，
        存储侧只负责原样存取。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT score FROM person_affinity WHERE person_id = ?", (person_id,)
            ).fetchone()
        return float(row["score"]) if row else None

    def affinity_updated_at(self, person_id: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM person_affinity WHERE person_id = ?", (person_id,)
            ).fetchone()
        return float(row["updated_at"]) if row else None

    def set_affinity(self, person_id: str, score: float, *, now: float | None = None) -> None:
        """写入好感度绝对值与更新时间（不做日志；日志按变化笔数写）。"""
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO person_affinity(person_id, score, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET
                    score = excluded.score,
                    updated_at = excluded.updated_at
                """,
                (person_id, float(score), float(now)),
            )
            self._conn.commit()

    def add_affinity_log(
        self,
        person_id: str,
        delta: float,
        *,
        reason: str = "",
        place: str = "",
        now: float | None = None,
    ) -> int:
        """记一笔好感度变化（正负都记；调用方已过滤掉四舍五入后为 0 的变化）。"""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO affinity_log(person_id, delta, reason, place, created_at) VALUES(?, ?, ?, ?, ?)",
                (person_id, float(delta), reason, place, float(now)),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def list_affinity_logs(
        self,
        *,
        person_id: str | None = None,
        limit: int = 50,
        since: float | None = None,
    ) -> list[AffinityEntry]:
        """好感度流水，按时间倒序。"""
        sql = "SELECT * FROM affinity_log WHERE 1=1"
        params: list = []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(float(since))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [AffinityEntry(**dict(row)) for row in rows]

    def daily_affinity_gain(self, person_id: str, since: float, *, reason: str | None = None) -> float:
        """某人在 ``since`` 之后累计的**正向**变化（每日上限用；可按原因过滤）。"""
        sql = (
            "SELECT COALESCE(SUM(delta), 0) AS total FROM affinity_log"
            " WHERE person_id = ? AND created_at >= ? AND delta > 0"
        )
        params: list = [person_id, float(since)]
        if reason is not None:
            sql += " AND reason = ?"
            params.append(reason)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return float(row["total"] or 0.0)

    def list_affinities(self) -> list[tuple[str, float, float]]:
        """全部有记录的人：``(person_id, score, updated_at)``（阈值判定在规则层）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT person_id, score, updated_at FROM person_affinity ORDER BY person_id"
            ).fetchall()
        return [(row["person_id"], float(row["score"]), float(row["updated_at"])) for row in rows]

    def get_mood(self) -> BotMood | None:
        """当前心情；从没写过时返回 None。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM bot_mood WHERE id = 1").fetchone()
        if row is None:
            return None
        return BotMood(
            mood=row["mood"],
            intensity=float(row["intensity"]),
            since=float(row["since"]),
            updated_at=float(row["updated_at"]),
        )

    def set_mood(
        self,
        mood: str,
        intensity: float,
        *,
        since: float | None = None,
        now: float | None = None,
    ) -> None:
        """写入当前心情（单行 upsert）。``since`` 不变时由调用方传原值。"""
        now = time.time() if now is None else now
        since = now if since is None else float(since)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bot_mood(id, mood, intensity, since, updated_at) VALUES(1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mood = excluded.mood,
                    intensity = excluded.intensity,
                    since = excluded.since,
                    updated_at = excluded.updated_at
                """,
                (mood, float(intensity), since, float(now)),
            )
            self._conn.commit()

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

    # ------------------------------------------------------------------
    # llm_usage（每次上游请求尝试一条；token 拿不到就是 NULL）
    # ------------------------------------------------------------------

    def add_llm_usage(
        self,
        *,
        task: str = "chat",
        model: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int = 0,
        ok: bool = True,
        ts: float | None = None,
    ) -> int:
        """记录一次 LLM 请求尝试；只存上游实际给的数据，不补齐 token。"""
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO llm_usage(ts, task, model, prompt_tokens, completion_tokens, latency_ms, ok)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    str(task or "chat"),
                    str(model or ""),
                    _none_or_int(prompt_tokens),
                    _none_or_int(completion_tokens),
                    int(latency_ms or 0),
                    int(bool(ok)),
                ),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def usage_totals(self, since: float | None = None) -> dict:
        """用量总量：调用次数、成败、token 合计、平均延迟。"""
        sql = """
            SELECT COUNT(*) AS calls,
                   SUM(ok) AS ok_calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
                   SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                   SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                   SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS usage_missing,
                   AVG(latency_ms) AS avg_latency_ms
            FROM llm_usage
        """
        params: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        calls = int(row["calls"] or 0)
        prompt = int(row["prompt_tokens"] or 0)
        completion = int(row["completion_tokens"] or 0)
        return {
            "calls": calls,
            "ok_calls": int(row["ok_calls"] or 0),
            "failed_calls": int(row["failed_calls"] or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            # 上游没给 usage 的调用数：这些行的 token 是 NULL，不是 0
            "usage_missing": int(row["usage_missing"] or 0),
            "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 1),
        }

    def usage_by_task(self, since: float | None = None, limit: int = 50) -> list[dict]:
        """按 task 聚合，token 多的排前面。"""
        sql = """
            SELECT task, COUNT(*) AS calls,
                   SUM(ok) AS ok_calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
                   SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                   SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                   AVG(latency_ms) AS avg_latency_ms
            FROM llm_usage
        """
        params: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " GROUP BY task ORDER BY prompt_tokens + completion_tokens DESC, calls DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_usage_group(row, "task") for row in rows]

    def usage_by_model(self, since: float | None = None, limit: int = 50) -> list[dict]:
        """按模型聚合，token 多的排前面。"""
        sql = """
            SELECT model, COUNT(*) AS calls,
                   SUM(ok) AS ok_calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
                   SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                   SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                   AVG(latency_ms) AS avg_latency_ms
            FROM llm_usage
        """
        params: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " GROUP BY model ORDER BY prompt_tokens + completion_tokens DESC, calls DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_usage_group(row, "model") for row in rows]

    def usage_by_day(self, since: float | None = None, limit: int = 31) -> list[dict]:
        """按天聚合（本地时区），返回从早到晚的每日用量。"""
        sql = """
            SELECT date(ts, 'unixepoch', 'localtime') AS day,
                   COUNT(*) AS calls,
                   SUM(ok) AS ok_calls,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
                   SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
                   SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
                   AVG(latency_ms) AS avg_latency_ms
            FROM llm_usage
        """
        params: list = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " GROUP BY day ORDER BY day DESC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_usage_group(row, "day") for row in reversed(rows)]

    def trim_llm_usage(self, keep_days: float = 180.0, *, now: float | None = None) -> int:
        """只保留最近 keep_days 天的用量记录，避免库无限增长。"""
        now = time.time() if now is None else now
        cutoff = now - max(0.0, float(keep_days)) * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM llm_usage WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # 平台事件（感知面）
    # ------------------------------------------------------------------

    def add_platform_event(
        self,
        event: Any,
        *,
        route: str = "record",
        verdict: str = "",
        now: float | None = None,
    ) -> int:
        """落一条平台事件摘要；``event`` 是 :class:`PlatformEvent`（鸭子类型即可）。"""
        now = time.time() if now is None else now
        ts = float(getattr(event, "ts", 0.0) or 0.0) or now
        raw = getattr(event, "raw", {}) or {}
        try:
            raw_text = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_text = "{}"
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO platform_events(platform, kind, type, sub_type, actor_id, actor_name,
                                            target_id, conversation_type, conversation_id, description,
                                            raw, platform_event_id, pending, route, verdict, created_at, ts)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(getattr(event, "platform", "")),
                    str(getattr(event, "kind", "")),
                    str(getattr(event, "type", "")),
                    str(getattr(event, "sub_type", "")),
                    str(getattr(event, "actor_id", "")),
                    str(getattr(event, "actor_name", "")),
                    str(getattr(event, "target_id", "")),
                    str(getattr(event, "conversation_type", "private")),
                    str(getattr(event, "conversation_id", "")),
                    str(getattr(event, "description", "")),
                    raw_text,
                    str(getattr(event, "platform_event_id", "")),
                    1 if getattr(event, "pending", False) else 0,
                    str(route or "record"),
                    str(verdict or ""),
                    now,
                    ts,
                ),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def set_platform_event_verdict(self, event_id: int, verdict: str) -> None:
        """回填快判结论（审计与排查用）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE platform_events SET verdict = ? WHERE id = ?", (str(verdict or ""), int(event_id))
            )
            self._conn.commit()

    def count_pending_platform_events(self, *, kind: str | None = None) -> int:
        """未处理申请数（「红点」）。"""
        sql = "SELECT COUNT(*) AS c FROM platform_events WHERE pending = 1"
        params: list = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def list_platform_events(
        self,
        *,
        kind: str | None = None,
        platform: str | None = None,
        conversation_id: str | None = None,
        pending: bool | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """最近的平台事件，按入库时间倒序。"""
        sql = "SELECT * FROM platform_events WHERE 1=1"
        params: list = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        if platform is not None:
            sql += " AND platform = ?"
            params.append(str(platform))
        if conversation_id is not None:
            sql += " AND conversation_id = ?"
            params.append(str(conversation_id))
        if pending is not None:
            sql += " AND pending = ?"
            params.append(1 if pending else 0)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 网关审计与状态（行动面）
    # ------------------------------------------------------------------

    def add_gateway_audit(
        self,
        *,
        action: str,
        source: str = "model",
        platform: str = "",
        chat_id: str = "",
        params: dict | None = None,
        ok: bool = False,
        result: str = "",
        reject_reason: str = "",
        sensitive: bool = False,
        now: float | None = None,
    ) -> int:
        """写一条网关审计；参数与结果都截断，避免库被大对象撑爆。"""
        now = time.time() if now is None else now
        try:
            params_text = json.dumps(params or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            params_text = "{}"
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO gateway_audit(ts, source, platform, chat_id, action, params, ok,
                                          result, reject_reason, sensitive)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    str(source or "model"),
                    str(platform or ""),
                    str(chat_id or ""),
                    str(action or ""),
                    params_text[:1000],
                    1 if ok else 0,
                    str(result or "")[:1000],
                    str(reject_reason or "")[:500],
                    1 if sensitive else 0,
                ),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def list_gateway_audit(
        self,
        *,
        action: str | None = None,
        chat_id: str | None = None,
        sensitive: bool | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """最近的网关调用记录（含拒绝），按时间倒序。"""
        sql = "SELECT * FROM gateway_audit WHERE 1=1"
        params: list = []
        if action is not None:
            sql += " AND action = ?"
            params.append(str(action))
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        if sensitive is not None:
            sql += " AND sensitive = ?"
            params.append(1 if sensitive else 0)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_gateway_audit(self, *, since: float, ok_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS c FROM gateway_audit WHERE ts >= ?"
        params: list = [float(since)]
        if ok_only:
            sql += " AND ok = 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["c"] or 0)

    def get_gateway_state(self, scope: str, key: str) -> tuple[str, float] | None:
        """读网关状态：``(value_json, updated_at)``；没有返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, updated_at FROM gateway_state WHERE scope = ? AND key = ?",
                (str(scope), str(key)),
            ).fetchone()
        if row is None:
            return None
        return str(row["value"]), float(row["updated_at"])

    def set_gateway_state(
        self, scope: str, key: str, value: Any, *, now: float | None = None
    ) -> None:
        """写网关状态；``value`` 可以是数字/列表/字典，序列化成 JSON 存。"""
        now = time.time() if now is None else now
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = "0"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO gateway_state(scope, key, value, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(scope), str(key), text, now),
            )
            self._conn.commit()

    def delete_gateway_state(self, scope: str, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM gateway_state WHERE scope = ? AND key = ?", (str(scope), str(key))
            )
            self._conn.commit()


def make_person_id(platform: str, platform_uid: str) -> str:
    """person_id 形如 ``snowluma:12345``；同一人不同平台暂不合并。"""
    return f"{platform}:{platform_uid}"


# 旧库升级：raw_messages 后加的列（列名 -> 列定义）
_RAW_MESSAGE_MIGRATIONS: dict[str, str] = {
    "platform_msg_id": "TEXT NOT NULL DEFAULT ''",
    "reply_to_id": "TEXT NOT NULL DEFAULT ''",
}


# 旧库升级：memories 后加的列（列名 -> 列定义）
_MEMORIES_MIGRATIONS: dict[str, str] = {
    "origin_chat": "TEXT NOT NULL DEFAULT ''",
    "audience": "TEXT NOT NULL DEFAULT ''",
    "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
    "stability": "TEXT NOT NULL DEFAULT 'stable'",
    "expires_at": "REAL",
}


def _migrate_memories(conn: sqlite3.Connection) -> None:
    """给旧库的 memories 补上记忆 v2 的列；旧行按列定义里的默认值填充。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
    ).fetchone()
    if exists is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    changed = False
    for name, definition in _MEMORIES_MIGRATIONS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            changed = True
    if changed:
        conn.commit()


def _migrate_raw_messages(conn: sqlite3.Connection) -> None:
    """给旧库的 raw_messages 补上新列；新库（表不存在）直接跳过。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'raw_messages'"
    ).fetchone()
    if exists is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(raw_messages)").fetchall()}
    for name, definition in _RAW_MESSAGE_MIGRATIONS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE raw_messages ADD COLUMN {name} {definition}")


def _none_or_int(value: Any) -> int | None:
    """把 token 数转成 int；None/非法值保持 None（SQLite 里就是 NULL）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _usage_group(row: sqlite3.Row, key: str) -> dict:
    """把一行分组聚合结果转成字典，token 合计仍是整数。"""
    prompt = int(row["prompt_tokens"] or 0)
    completion = int(row["completion_tokens"] or 0)
    return {
        key: row[key],
        "calls": int(row["calls"] or 0),
        "ok_calls": int(row["ok_calls"] or 0),
        "failed_calls": int(row["failed_calls"] or 0),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 1),
    }
