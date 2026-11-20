"""会话、成员的展示资料：缓存名字，不缓存图片；身份不参与授权。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .web.state import WebState

logger = logging.getLogger(__name__)


def avatar_url(platform: str, uid: str, kind: str = "user") -> str:
    """只把数字 QQ 号用于公开头像地址，其他平台不猜 URL。"""
    if platform != "snowluma" or not uid.isascii() or not uid.isdigit():
        return ""
    if kind == "group":
        return f"https://p.qlogo.cn/gh/{uid}/{uid}/640"
    return f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"


class IdentityService:
    def __init__(self, state: WebState, *, ttl: float = 3600):
        self.state = state
        self.store = state.store
        self.ttl = ttl
        self._list_locks: dict[str, asyncio.Lock] = {}
        self._retry_after: dict[str, float] = {}

    def cached(self, platform: str, value: str, *, kind: str, hint: str = "", group_id: str = "") -> dict[str, str]:
        cache_kind = "user" if kind == "private" else kind
        row = self.store.get_identity(platform, value, cache_kind, group_id=group_id)
        name = (row or {}).get("name") or hint.strip()
        if not name or name in (value, f"{platform}:{value}"):
            name = "未知群" if kind == "group" else "未知用户"
        return {"id": value, "platform": platform, "kind": kind, "name": name,
                "avatar_url": avatar_url(platform, value, kind)}

    def person(self, person) -> dict[str, str]:
        return self.cached(person.platform, person.platform_uid, kind="user", hint=person.display_name)

    def person_id(self, person_id: str, *, hint: str = "", group_id: str = "") -> dict[str, str] | None:
        platform, sep, uid = person_id.partition(":")
        if not sep or not uid:
            return None
        person = self.store.get_person(person_id)
        if group_id:
            # 有明确会话时先读本群名片，再用当时的署名；不用其他群的名片顶替。
            local = self.cached(platform, uid, kind="user", group_id=group_id, hint=hint)
            if local["name"] != "未知用户":
                return local
        return self.cached(platform, uid, kind="user", hint=hint or (person.display_name if person else ""))

    def chat(self, platform: str, chat_id: str, *, chat_type: str = "group") -> dict[str, str]:
        if chat_type == "private":
            person = self.store.get_person(f"{platform}:{chat_id}")
            return self.cached(platform, chat_id, kind="private", hint=person.display_name if person else "")
        return self.cached(platform, chat_id, kind="group")

    def chat_source(self, source_chat: str, *, audience: str) -> dict[str, str] | None:
        platform, sep, chat_id = source_chat.partition(":")
        if not sep or not chat_id:
            return None
        if audience.startswith("group:") or audience == "group":
            return self.chat(platform, chat_id)
        if audience == "private" or audience.startswith("private:"):
            return self.chat(platform, chat_id, chat_type="private")
        return {"id": chat_id, "platform": platform, "kind": "chat", "name": "未知会话", "avatar_url": ""}

    async def refresh(self, *, platform: str, chat_id: str = "", person_id: str = "", force: bool = False) -> dict:
        """群列表有独立的刷新时钟。失败短暂退避，旧资料仍可使用。"""
        adapter = getattr(self.state.bot, "adapters", {}).get(platform)
        if adapter is None:
            return {"ok": False, "error": "适配器不在线", "refreshed": 0}

        async def fetch():
            try:
                rows = await adapter.refresh_identities(chat_id=chat_id or None, person_id=person_id or None)
            except Exception:
                logger.warning("无法从 %s 刷新身份资料，保留上次结果", platform)
                return {"ok": False, "error": "未能读取平台资料，已保留缓存", "refreshed": 0}
            count = 0
            for row in rows:
                if row.get("name", "").strip() and row.get("id"):
                    self.store.upsert_identity(platform, row["id"], kind=row["kind"], name=row["name"].strip(),
                                               group_id=row.get("group_id", ""))
                    count += 1
            return {"ok": True, "platform": platform, "refreshed": count}

        if chat_id or person_id:
            return await fetch()
        async with self._list_locks.setdefault(platform, asyncio.Lock()):
            key = f"group_directory:{platform}:updated_at"
            if not force and (time.time() - float(self.store.get_meta(key) or 0) < self.ttl
                              or time.monotonic() < self._retry_after.get(platform, 0)):
                return {"ok": True, "platform": platform, "refreshed": 0, "cached": True}
            result = await fetch()
            if result["ok"]:
                self.store.set_meta(key, str(time.time()))
            else:
                self._retry_after[platform] = time.monotonic() + 30
            return result
