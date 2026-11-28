"""RSS 订阅存储：独立 SQLite 账本，不写宿主存储。

插件不能写 bot 的 ``store``（写入口不向插件开放），所以订阅表放在
运行数据目录下的 ``plugin_data/rss.sqlite3``，不放源码目录。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rss_subscriptions (
    feed_url TEXT NOT NULL,
    feed_title TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    last_seen_guid TEXT NOT NULL DEFAULT '',
    last_checked_at REAL NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (feed_url, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_rss_subscriptions_feed
    ON rss_subscriptions (feed_url);
"""


class RssStore:
    """订阅表的增删改查。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def add(
        self,
        feed_url: str,
        target_type: str,
        target_id: str,
        *,
        feed_title: str = "",
        last_seen_guid: str = "",
        created_by: str = "",
    ) -> None:
        """新增一条订阅；已存在时只更新标题与去重水位，不重置失败计数。"""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rss_subscriptions
                    (feed_url, feed_title, target_type, target_id,
                     last_seen_guid, last_checked_at, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (feed_url, target_type, target_id) DO UPDATE SET
                    feed_title = excluded.feed_title,
                    last_seen_guid = excluded.last_seen_guid
                """,
                (
                    feed_url,
                    feed_title,
                    target_type,
                    target_id,
                    last_seen_guid,
                    now,
                    created_by,
                    now,
                ),
            )

    def remove(self, feed_url: str, target_type: str, target_id: str) -> bool:
        """删除一条订阅；返回是否真的删掉了行。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM rss_subscriptions"
                " WHERE feed_url = ? AND target_type = ? AND target_id = ?",
                (feed_url, target_type, target_id),
            )
            return cur.rowcount > 0

    def get(self, feed_url: str, target_type: str, target_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM rss_subscriptions"
                " WHERE feed_url = ? AND target_type = ? AND target_id = ?",
                (feed_url, target_type, target_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def list(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        created_by: str | None = None,
    ) -> list[dict]:
        """按条件列订阅；都不给就是全表（轮询用）。"""
        where = []
        params: list[str] = []
        if target_type is not None:
            where.append("target_type = ?")
            params.append(target_type)
        if target_id is not None:
            where.append("target_id = ?")
            params.append(target_id)
        if created_by is not None:
            where.append("created_by = ?")
            params.append(created_by)
        sql = "SELECT * FROM rss_subscriptions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY feed_title, feed_url"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def set_title(self, feed_url: str, title: str) -> None:
        """同一源的所有订阅行一起更新标题。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE rss_subscriptions SET feed_title = ? WHERE feed_url = ?",
                (title, feed_url),
            )

    def update_after_check(
        self,
        feed_url: str,
        target_type: str,
        target_id: str,
        *,
        last_seen_guid: str | None = None,
        failed: bool = False,
    ) -> None:
        """一轮抓取后更新水位与失败计数；成功抓到就清零失败计数。"""
        now = time.time()
        with self._connect() as conn:
            if failed:
                conn.execute(
                    "UPDATE rss_subscriptions"
                    " SET last_checked_at = ?, fail_count = fail_count + 1"
                    " WHERE feed_url = ? AND target_type = ? AND target_id = ?",
                    (now, feed_url, target_type, target_id),
                )
            elif last_seen_guid is not None:
                conn.execute(
                    "UPDATE rss_subscriptions"
                    " SET last_checked_at = ?, fail_count = 0, last_seen_guid = ?"
                    " WHERE feed_url = ? AND target_type = ? AND target_id = ?",
                    (now, last_seen_guid, feed_url, target_type, target_id),
                )
            else:
                conn.execute(
                    "UPDATE rss_subscriptions"
                    " SET last_checked_at = ?, fail_count = 0"
                    " WHERE feed_url = ? AND target_type = ? AND target_id = ?",
                    (now, feed_url, target_type, target_id),
                )
