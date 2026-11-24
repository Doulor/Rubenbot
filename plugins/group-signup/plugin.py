"""群内限额报名插件。

首版只处理明确入口和活动卡回复：开队、本人报名、本人退出、查询余位、发起人收队。
报名账本是插件私有 SQLite；不会写宿主聊天库，也不会从普通群聊的关键词猜报名。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from rubenbot.adapters.base import Segment

logger = logging.getLogger(__name__)

_ACTIONS = {"join", "leave", "status", "close", "noop", "clarify"}
_ACTION_ALIASES = {
    "报名": "join",
    "加入": "join",
    "参加": "join",
    "算我一个": "join",
    "退出": "leave",
    "退报": "leave",
    "不去了": "leave",
    "余位": "status",
    "还有没有位": "status",
    "查询": "status",
    "收队": "close",
    "结束": "close",
}
_ID_RE = re.compile(r"(?:#|活动\s*)?(\d+)")


@dataclass(frozen=True)
class SignupConfig:
    """插件配置的安全子集。"""

    allowed_groups: tuple[str, ...] = ()
    max_capacity: int = 20
    max_open_hours: float = 72.0
    retention_days: int = 30

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SignupConfig":
        raw = data if isinstance(data, Mapping) else {}
        groups = raw.get("allowed_groups", ())
        if not isinstance(groups, (list, tuple)):
            groups = ()
        allowed = tuple(str(item).strip() for item in groups if str(item or "").strip())
        return cls(
            allowed_groups=allowed,
            max_capacity=_positive_int(raw.get("max_capacity"), 20, 1, 500),
            max_open_hours=_positive_float(raw.get("max_open_hours"), 72.0, 0.1, 720.0),
            retention_days=_positive_int(raw.get("retention_days"), 30, 1, 3650),
        )


def _positive_int(value: Any, default: int, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    return max(lower, min(upper, value))


def _positive_float(value: Any, default: float, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(lower, min(upper, float(value)))


def load_config(ctx: Any) -> SignupConfig:
    """读取 WebUI 传入的插件配置；没有配置时使用默认值。"""
    override = ctx.extras.get("group_signup_config")
    if isinstance(override, Mapping):
        return SignupConfig.from_mapping(override)
    path = Path(getattr(getattr(ctx, "plugin", None), "path", Path(__file__).parent))
    config_path = path / "config.yaml"
    if not config_path.is_file():
        return SignupConfig()
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("[group-signup] 读取配置失败：%s", exc)
        return SignupConfig()
    return SignupConfig.from_mapping(data)


def _database_path(ctx: Any) -> Path:
    """把插件账本放到运行数据目录，不放源码目录。"""
    override = ctx.extras.get("group_signup_db_path")
    if override:
        return Path(str(override))
    configured = str(getattr(ctx.config, "database_path", "data/rubenbot.db"))
    base = Path(configured).parent if configured != ":memory:" else Path("data")
    return base / "plugin_data" / "group-signup.sqlite3"


class SignupStore:
    """报名状态存储。

    每次业务操作都使用一个短的 ``BEGIN IMMEDIATE`` 事务；模型调用和消息发送
    永远发生在事务外。这样容量复验、幂等标记和成员写入在同一把 SQLite 写锁内。
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = sqlite3.connect(self.path, check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    creator_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    card_message_id TEXT NOT NULL DEFAULT '',
                    closed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_signup_activity_chat
                    ON activities(platform, chat_type, chat_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_signup_card
                    ON activities(platform, chat_type, chat_id, card_message_id)
                    WHERE card_message_id <> '';
                CREATE TABLE IF NOT EXISTS signup_members (
                    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                    member_id TEXT NOT NULL,
                    member_name TEXT NOT NULL DEFAULT '',
                    joined_at REAL NOT NULL,
                    PRIMARY KEY(activity_id, member_id)
                );
                CREATE INDEX IF NOT EXISTS idx_signup_members_activity
                    ON signup_members(activity_id, joined_at);
                CREATE TABLE IF NOT EXISTS handled_messages (
                    platform TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    platform_msg_id TEXT NOT NULL,
                    handled_at REAL NOT NULL,
                    PRIMARY KEY(platform, chat_type, chat_id, platform_msg_id)
                );
                """
            )
            connection.commit()
            if self._memory_connection is None:
                connection.close()

    def _transaction(self):
        """返回连接；调用方负责 commit/rollback/close。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _scope(event: Any) -> tuple[str, str, str]:
        return (str(event.platform), str(event.chat_type), str(event.chat_id))

    def cleanup(self, now: float, retention_days: int) -> None:
        cutoff = now - retention_days * 86400
        with self._lock:
            connection = self._connect()
            connection.execute(
                "DELETE FROM activities WHERE status IN ('closed', 'expired') "
                "AND COALESCE(closed_at, expires_at) < ?",
                (cutoff,),
            )
            connection.execute("DELETE FROM handled_messages WHERE handled_at < ?", (cutoff,))
            connection.commit()
            if self._memory_connection is None:
                connection.close()

    def _expire(self, connection: sqlite3.Connection, now: float, scope: tuple[str, str, str]) -> None:
        connection.execute(
            "UPDATE activities SET status='expired', closed_at=? "
            "WHERE platform=? AND chat_type=? AND chat_id=? AND status='open' AND expires_at <= ?",
            (now, *scope, now),
        )

    def create_activity(
        self, event: Any, title: str, capacity: int, max_open_hours: float, now: float
    ) -> int:
        scope = self._scope(event)
        with self._lock:
            connection = self._transaction()
            try:
                self._expire(connection, now, scope)
                cursor = connection.execute(
                    """INSERT INTO activities
                    (platform, chat_type, chat_id, creator_id, creator_name, title,
                     capacity, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*scope, str(event.sender_id), str(getattr(event, "sender_name", "") or ""),
                     title, capacity, now, now + max_open_hours * 3600),
                )
                activity_id = int(cursor.lastrowid)
                connection.commit()
                return activity_id
            except Exception:
                connection.rollback()
                raise
            finally:
                if self._memory_connection is None:
                    connection.close()

    def set_card_message(self, activity_id: int, message_id: str | None) -> None:
        if not message_id:
            return
        with self._lock:
            connection = self._connect()
            connection.execute(
                "UPDATE activities SET card_message_id=? WHERE id=?", (str(message_id), activity_id)
            )
            connection.commit()
            if self._memory_connection is None:
                connection.close()

    def _row_activity(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_activity(self, activity_id: int, event: Any, now: float) -> dict[str, Any] | None:
        scope = self._scope(event)
        with self._lock:
            connection = self._transaction()
            try:
                self._expire(connection, now, scope)
                row = connection.execute(
                    "SELECT * FROM activities WHERE id=? AND platform=? AND chat_type=? AND chat_id=?",
                    (activity_id, *scope),
                ).fetchone()
                connection.commit()
                return self._row_activity(row)
            finally:
                if self._memory_connection is None:
                    connection.close()

    def find_by_card(self, event: Any, card_message_id: str, now: float) -> dict[str, Any] | None:
        scope = self._scope(event)
        with self._lock:
            connection = self._transaction()
            try:
                self._expire(connection, now, scope)
                row = connection.execute(
                    "SELECT * FROM activities WHERE platform=? AND chat_type=? AND chat_id=? "
                    "AND card_message_id=?",
                    (*scope, str(card_message_id)),
                ).fetchone()
                connection.commit()
                return self._row_activity(row)
            finally:
                if self._memory_connection is None:
                    connection.close()

    def list_activities(self, event: Any, now: float, include_finished: bool = True) -> list[dict[str, Any]]:
        scope = self._scope(event)
        with self._lock:
            connection = self._transaction()
            try:
                self._expire(connection, now, scope)
                clause = "" if include_finished else "AND status='open'"
                rows = connection.execute(
                    "SELECT * FROM activities WHERE platform=? AND chat_type=? AND chat_id=? "
                    f"{clause} ORDER BY id DESC",
                    scope,
                ).fetchall()
                connection.commit()
                return [dict(row) for row in rows]
            finally:
                if self._memory_connection is None:
                    connection.close()

    def members(self, activity_id: int) -> list[dict[str, Any]]:
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT member_id, member_name, joined_at FROM signup_members "
                "WHERE activity_id=? ORDER BY joined_at, member_id",
                (activity_id,),
            ).fetchall()
            if self._memory_connection is None:
                connection.close()
            return [dict(row) for row in rows]

    def mutate(
        self,
        event: Any,
        activity_id: int,
        action: str,
        now: float,
        *,
        message_id: str = "",
    ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        """执行一项有限动作，返回 ``(结果码, 活动, 成员)``。"""
        scope = self._scope(event)
        with self._lock:
            connection = self._transaction()
            try:
                if message_id:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO handled_messages "
                        "(platform, chat_type, chat_id, platform_msg_id, handled_at) VALUES (?, ?, ?, ?, ?)",
                        (*scope, str(message_id), now),
                    )
                    if cursor.rowcount == 0:
                        connection.rollback()
                        return "duplicate", None, []
                self._expire(connection, now, scope)
                row = connection.execute(
                    "SELECT * FROM activities WHERE id=? AND platform=? AND chat_type=? AND chat_id=?",
                    (activity_id, *scope),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return "not_found", None, []
                activity = dict(row)
                member = connection.execute(
                    "SELECT * FROM signup_members WHERE activity_id=? AND member_id=?",
                    (activity_id, str(event.sender_id)),
                ).fetchone()
                if action == "join":
                    if activity["status"] != "open":
                        result = "closed"
                    elif member is not None:
                        result = "already_joined"
                    else:
                        count = connection.execute(
                            "SELECT COUNT(*) FROM signup_members WHERE activity_id=?", (activity_id,)
                        ).fetchone()[0]
                        if count >= activity["capacity"]:
                            result = "full"
                        else:
                            connection.execute(
                                "INSERT INTO signup_members(activity_id, member_id, member_name, joined_at) "
                                "VALUES (?, ?, ?, ?)",
                                (activity_id, str(event.sender_id), str(getattr(event, "sender_name", "") or ""), now),
                            )
                            result = "joined"
                elif action == "leave":
                    if activity["status"] != "open":
                        result = "closed"
                    elif member is None:
                        result = "not_joined"
                    else:
                        connection.execute(
                            "DELETE FROM signup_members WHERE activity_id=? AND member_id=?",
                            (activity_id, str(event.sender_id)),
                        )
                        result = "left"
                elif action == "close":
                    if str(event.sender_id) != activity["creator_id"]:
                        result = "not_creator"
                    elif activity["status"] != "open":
                        result = "closed"
                    else:
                        connection.execute(
                            "UPDATE activities SET status='closed', closed_at=? WHERE id=?",
                            (now, activity_id),
                        )
                        result = "closed_now"
                else:
                    result = "status"
                updated = connection.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
                connection.commit()
                return result, dict(updated), [dict(item) for item in connection.execute(
                    "SELECT * FROM signup_members WHERE activity_id=? ORDER BY joined_at, member_id",
                    (activity_id,),
                ).fetchall()]
            except Exception:
                connection.rollback()
                raise
            finally:
                if self._memory_connection is None:
                    connection.close()


class GroupSignup:
    """群报名业务和消息入口。"""

    def __init__(self, ctx: Any, config: SignupConfig, store: SignupStore, *, clock=time.time):
        self.ctx = ctx
        self.config = config
        self.store = store
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def _allowed(self, event: Any) -> bool:
        return bool(getattr(event, "is_group", False)) and (
            not self.config.allowed_groups or str(event.chat_id) in self.config.allowed_groups
        )

    def _authorized(self, *, needs_llm: bool = False) -> bool:
        """在读账、判意和出站前核对对应授权，避免无回执却改名单。"""
        allowed = getattr(self.ctx, "allowed", None)
        if not callable(allowed):
            return True
        required = ["read_messages", "send_messages"]
        if needs_llm:
            required.append("llm")
        return all(allowed(permission) for permission in required)

    def _key(self, event: Any) -> str:
        return f"{event.platform}:{event.chat_type}:{event.chat_id}"

    def _lock(self, event: Any) -> asyncio.Lock:
        return self._locks.setdefault(self._key(event), asyncio.Lock())

    async def _send(self, runtime: Any, event: Any, text: str) -> tuple[bool, Any]:
        try:
            adapters = getattr(runtime, "adapters", None) or {}
            adapter = adapters.get(event.platform)
            if adapter is None:
                return False, None
            message_id = await adapter.send(
                event.chat_id, [Segment.text(text)], chat_type=event.chat_type
            )
            return True, message_id
        except Exception as exc:  # noqa: BLE001 - 失败不能冒充已通知
            logger.warning("[group-signup] 回执发送失败：%s", exc)
            return False, None

    def _card(self, activity: Mapping[str, Any]) -> str:
        return (
            f"📝 报名活动 #{activity['id']}：{activity['title']}\n"
            f"当前 0/{activity['capacity']} 人，发起人：{activity['creator_name'] or activity['creator_id']}\n"
            "回复这张活动卡说「算我一个」「我退出」或「还有没有位」；也可用 /报名、/退出、/余位。"
        )

    def _status_text(self, activity: Mapping[str, Any], members: list[Mapping[str, Any]]) -> str:
        names = [str(item["member_name"] or item["member_id"]) for item in members]
        status = {"open": "报名中", "closed": "已收队", "expired": "已截止"}.get(
            str(activity["status"]), "已结束"
        )
        roster = "、".join(names) if names else "暂时没人报名"
        return (
            f"活动 #{activity['id']}「{activity['title']}」{status}："
            f"{len(members)}/{activity['capacity']} 人。\n名单：{roster}"
        )

    def _list_text(self, activities: list[Mapping[str, Any]]) -> str:
        if not activities:
            return "这个群现在没有报名活动。"
        lines = ["这个群有多个报名活动，请带活动编号："]
        for activity in activities:
            state = {"open": "报名中", "closed": "已收队", "expired": "已截止"}.get(
                str(activity["status"]), "已结束"
            )
            lines.append(f"#{activity['id']}「{activity['title']}」— {state}")
        return "\n".join(lines)

    def _parse_id(self, args: str) -> int | None:
        match = _ID_RE.search(args or "")
        return int(match.group(1)) if match else None

    def _resolve_id(self, event: Any, args: str, now: float) -> tuple[int | None, str | None]:
        explicit = self._parse_id(args)
        if explicit is not None:
            return explicit, None
        activities = self.store.list_activities(event, now, include_finished=True)
        if len(activities) == 1:
            return int(activities[0]["id"]), None
        if not activities:
            return None, "这个群现在没有报名活动。"
        return None, self._list_text(activities)

    async def create(self, runtime: Any, event: Any, args: str) -> Any:
        if not self._authorized():
            return "报名插件的权限还没授权，暂时不能开队。"
        parts = (args or "").strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].strip():
            return "用法：/开队 人数 活动名，例如：/开队 4 周五桌游"
        capacity = int(parts[0])
        if capacity < 1 or capacity > self.config.max_capacity:
            return f"人数要在 1 到 {self.config.max_capacity} 人之间。"
        title = parts[1].strip()[:80]
        now = self.clock()
        async with self._lock(event):
            activity_id = self.store.create_activity(
                event, title, capacity, self.config.max_open_hours, now
            )
            activity = self.store.get_activity(activity_id, event, now)
            sent, message_id = await self._send(runtime, event, self._card(activity or {}))
            if sent and message_id:
                self.store.set_card_message(activity_id, str(message_id))
            if not sent:
                return "活动已经建好，但活动卡没发出去；可用 /余位查询，暂时别把它当成已通知群友。"
            if not message_id:
                return (
                    f"活动 #{activity_id} 已建好并发出，但平台没返回消息编号；"
                    "后续请用带编号的 /报名、/退出或 /余位。"
                )
            return None

    async def command(self, runtime: Any, event: Any, args: str, action: str) -> Any:
        if not self._allowed(event):
            return ""
        if not self._authorized():
            return "报名插件的权限还没授权，暂时不能操作。"
        now = self.clock()
        if action == "create":
            return await self.create(runtime, event, args)
        async with self._lock(event):
            activity_id, error = self._resolve_id(event, args, now)
            if error:
                return error
            result, activity, members = self.store.mutate(
                event, int(activity_id), action, now, message_id=str(getattr(event, "platform_msg_id", "") or "")
            )
        return self._result_text(result, activity, members, event, action)

    def _result_text(
        self,
        result: str,
        activity: Mapping[str, Any] | None,
        members: list[Mapping[str, Any]],
        event: Any,
        action: str,
    ) -> str:
        if result in {"duplicate", "not_found"}:
            return "这条消息已经处理过了。" if result == "duplicate" else "没找到这张活动卡。"
        if activity is None:
            return "活动不存在。"
        if result == "joined":
            return f"已报名活动 #{activity['id']}「{activity['title']}」。现在 {len(members)}/{activity['capacity']} 人。"
        if result == "already_joined":
            return f"你已经在活动 #{activity['id']} 的名单里了，不重复占位。"
        if result == "full":
            return f"活动 #{activity['id']}「{activity['title']}」已经满员了。"
        if result == "left":
            return f"已退出活动 #{activity['id']}「{activity['title']}」，空位会留给下一位。"
        if result == "not_joined":
            return "你还不在这场活动的名单里。"
        if result == "not_creator":
            return "只有发起人可以收队。"
        if result == "closed_now":
            return self._status_text(activity, members)
        if result == "closed" and action in {"join", "leave"}:
            return self._status_text(activity, members) + "\n这场已经结束，不能再改名单了。"
        return self._status_text(activity, members)

    async def _intent(self, text: str, activity: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            llm = self.ctx.llm
            if llm is None:
                return None
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "你只负责把回复报名活动卡的当事人原话归类成有限动作。"
                        "只能输出 JSON：{\"action\": \"join|leave|status|close|noop|clarify\"}。"
                        "join/leave/status/close 只表示说话人本人意图；替别人转述、闲聊、"
                        "无法确认时用 noop 或 clarify。不要替说话人推断人数、活动编号或他人身份。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"活动：{activity['title']}；状态：{activity['status']}；"
                        f"原话：{text[:300]}"
                    ),
                },
            ]
            raw = await llm.complete(
                prompt, role="fast", task="group_signup_intent", json_mode=True
            )
            if not isinstance(raw, str):
                return None
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                return None
            action = str(data.get("action", "")).strip().lower()
            action = _ACTION_ALIASES.get(action, action)
            return {"action": action} if action in _ACTIONS else None
        except Exception as exc:  # noqa: BLE001 - 模型不确定时不改名单
            logger.warning("[group-signup] 判定活动回复失败：%s", exc)
            return None

    async def on_message(self, runtime: Any, event: Any) -> Any:
        """只处理回复本插件活动卡的消息，普通闲聊一律放行。"""
        if not self._allowed(event) or not getattr(event, "reply_to_id", None):
            return None
        if not self._authorized(needs_llm=True):
            return None
        now = self.clock()
        card = self.store.find_by_card(event, str(event.reply_to_id), now)
        if card is None:
            return None
        intent = await self._intent((getattr(event, "text", "") or "").strip(), card)
        if intent is None or intent["action"] == "noop":
            return None
        action = intent["action"]
        if action == "clarify":
            ok, _ = await self._send(runtime, event, "我没确认你是要报名、退出还是查余位；请把意思说清楚一点。")
            return False if ok else False
        if action not in {"join", "leave", "status", "close"}:
            return None
        async with self._lock(event):
            result, activity, members = self.store.mutate(
                event,
                int(card["id"]),
                action,
                now,
                message_id=str(getattr(event, "platform_msg_id", "") or ""),
            )
        reply = self._result_text(result, activity, members, event, action)
        # 即使回执失败也拦截原消息，避免宿主普通聊天通道凭空声称操作成功。
        await self._send(runtime, event, reply)
        return False


def _note(event: Any, adapter: Any):
    from rubenbot.expression import CapabilityNote

    if not getattr(event, "is_group", False):
        return None
    return CapabilityNote(
        "限额报名",
        "我能在本群开限额活动、处理本人报名和退出。",
        "需要开队时用 /开队 人数 活动名；后续请回复活动卡。",
    )


def register(ctx: Any) -> None:
    """插件入口。"""
    config = load_config(ctx)
    store = SignupStore(_database_path(ctx))
    signup = GroupSignup(ctx, config, store)
    ctx.extras["group_signup"] = signup

    @ctx.command("开队")
    async def create(runtime: Any, event: Any, args: str):
        return await signup.command(runtime, event, args, "create")

    @ctx.command("报名")
    async def join(runtime: Any, event: Any, args: str):
        return await signup.command(runtime, event, args, "join")

    @ctx.command("退出")
    async def leave(runtime: Any, event: Any, args: str):
        return await signup.command(runtime, event, args, "leave")

    @ctx.command("余位")
    async def status(runtime: Any, event: Any, args: str):
        return await signup.command(runtime, event, args, "status")

    @ctx.command("收队")
    async def close(runtime: Any, event: Any, args: str):
        return await signup.command(runtime, event, args, "close")

    @ctx.on_message
    async def handle(runtime: Any, event: Any):
        return await signup.on_message(runtime, event)

    ctx.capability_note(_note, requires=("read_messages", "send_messages", "llm"), entry=handle)
    logger.debug("[group-signup] 已注册（允许群 %s，人数上限 %s）", config.allowed_groups or "全部", config.max_capacity)
