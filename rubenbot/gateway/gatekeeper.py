"""网关闸门：风险分级、四维冷却、失败退避与审计。

核心层只认「动作条目」（:class:`~rubenbot.gateway.catalog.ActionEntry`）与
抽象会话键，不出现任何平台专有词。状态全部落在 ``gateway_state`` 表里，
进程重启后冷却与预算不会丢。

四维冷却（见总纲）：

1. ``(动作, 会话)`` 冷却：默认值来自目录，拒绝时回剩余秒数；
2. ``(动作, 对象)`` 上限：对同一人主动动作 1 小时内最多 3 次；
3. 会话主动预算：每会话每天主动类动作不超过 ``daily_budget_per_conversation``；
4. 全局日上限：所有主动类动作合计不超过 ``global_daily_cap``；
   同动作连续失败 3 次停用 1 小时（失败退避）。

只读动作（``proactive=False``）不计预算、不计对象上限，只受会话冷却约束，
避免模型查一次群成员就耗掉当天额度。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..memory.store import BOT_SENDER_ID, SYSTEM_SENDER_PREFIX
from .catalog import RISK_DENIED, RISK_SENSITIVE, ActionEntry

logger = logging.getLogger(__name__)

# 这两个动作返回登录凭据；记录边界按动作名判断，不依赖目录风险等级或字段名。
CREDENTIAL_ACTIONS = frozenset({"get_cookies", "get_csrf_token"})

# 对象上限：同一人 1 小时内最多几次主动动作
OBJECT_WINDOW_SECONDS = 3600.0
OBJECT_MAX_PER_WINDOW = 3
# 失败退避：同动作连续失败几次后停用多久
FAILURE_LIMIT = 3
FAILURE_BACKOFF_SECONDS = 3600.0

# 动作参数里可能代表「对象」的键（平台无关命名优先）
_OBJECT_KEYS = ("target_id", "target_user_id", "user_id", "qq", "target")

# gateway_state 的 scope
SCOPE_CONVERSATION = "conversation"
SCOPE_OBJECT = "object"
SCOPE_BUDGET = "budget"
SCOPE_GLOBAL = "global"
SCOPE_BACKOFF = "backoff"


@dataclass
class GateDecision:
    """一次闸门检查的结论。拒绝时 ``reason`` 直接回给模型（含剩余秒数）。"""

    allowed: bool
    reason: str = ""
    retry_after: float | None = None
    sensitive: bool = False
    object_id: str = ""

    @classmethod
    def allow(cls) -> "GateDecision":
        return cls(True)

    @classmethod
    def refuse(cls, reason: str, retry_after: float | None = None) -> "GateDecision":
        return cls(False, reason=reason, retry_after=retry_after)


def _day_key(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def object_id_from_params(params: dict[str, Any] | None) -> str:
    """从参数里猜「动作对象」；没有就返回空串（不参与对象上限）。"""
    for key in _OBJECT_KEYS:
        value = (params or {}).get(key)
        if value not in (None, ""):
            return str(value)
    return ""


class Gatekeeper:
    """闸门 + 状态 + 审计。``now`` 一律外部传入，测试可以完全确定。"""

    def __init__(self, store: Any, config: Config):
        self.store = store
        self.config = config

    # ------------------------------------------------------------------
    # 配置视图
    # ------------------------------------------------------------------

    @property
    def settings(self):
        return self.config.gateway

    # ------------------------------------------------------------------
    # 检查
    # ------------------------------------------------------------------

    def check(
        self,
        entry: ActionEntry,
        *,
        chat_key: str,
        object_id: str = "",
        chat_type: str = "",
        params: dict[str, Any] | None = None,
        requester: str = "",
        requester_owner: bool = False,
        now: float | None = None,
    ) -> GateDecision:
        """撤回的申请人与机主身份只由固定聊天路由传入，不从动作参数读取。"""
        now = time.time() if now is None else now
        if entry.risk == RISK_DENIED:
            return GateDecision(False, "这个接口被禁止调用（账号破坏性操作）。", sensitive=True)
        sensitive = entry.risk == RISK_SENSITIVE
        recall_authorized = False
        if entry.name == "delete_msg":
            sender = self._recall_sender(chat_key, chat_type, params or {})
            if not sender:
                return GateDecision(False, "无法从当前会话的消息记录确认撤回目标。", sensitive=True)
            sensitive = sender != BOT_SENDER_ID
            object_id = sender
            recall_authorized = requester == sender or (bool(requester) and requester_owner)
        if sensitive and not recall_authorized and not self.settings.allow_sensitive:
            return GateDecision(False, "这是敏感操作，当前没有开启 allow_sensitive。", sensitive=True)
        decision = self._check_limits(entry, chat_key, object_id, now)
        decision.sensitive = sensitive
        decision.object_id = object_id
        return decision

    def _recall_sender(self, chat_key: str, chat_type: str, params: dict[str, Any]) -> str:
        """只认当前会话的平台消息号，不把本地行号、引用作者或调用者自述当证据。"""
        platform, chat_id = _split_chat_key(chat_key)
        if not platform or not chat_id or chat_type not in {"group", "private"} or params.keys() != {"message_id"}:
            return ""
        value = params["message_id"]
        # 协议端用 JS 整数解析；拒绝有歧义的拼法与会被舍入的整数，保证查、撤同号。
        if type(value) not in (int, str) or not re.fullmatch(r"-?[1-9][0-9]{0,15}", str(value)):
            return ""
        if abs(int(value)) > 2**53 - 1:
            return ""
        try:
            row = self.store.message_by_platform_id(platform, str(value), chat_id=chat_id, chat_type=chat_type)
        except Exception as exc:  # 查账失败不能退化为只检查敏感开关
            logger.warning("撤回目标查账失败：%s", type(exc).__name__)
            return ""
        if row and (row.sender_id == BOT_SENDER_ID or not row.sender_id.startswith(SYSTEM_SENDER_PREFIX)):
            return row.sender_id
        return ""

    def _check_limits(self, entry: ActionEntry, chat_key: str, object_id: str, now: float) -> GateDecision:
        backoff = self._backoff(entry.name, now)
        if backoff is not None:
            return GateDecision.refuse(
                f"这个接口连续失败过 {FAILURE_LIMIT} 次，暂停使用中，约 {_seconds_text(backoff)}后可再试。",
                retry_after=backoff,
            )

        remaining = self._conversation_remaining(entry, chat_key, now)
        if remaining is not None and remaining > 0:
            return GateDecision.refuse(
                f"这个接口在这个会话刚用过，还要等 {_seconds_text(remaining)}。",
                retry_after=remaining,
            )

        if entry.proactive and object_id:
            remaining = self._object_remaining(entry.name, object_id, now)
            if remaining is not None and remaining > 0:
                return GateDecision.refuse(
                    f"对同一个人短时间内做得太多了，还要等 {_seconds_text(remaining)}再对这个对象动作。",
                    retry_after=remaining,
                )

        if entry.proactive:
            budget = self._budget_count(chat_key, now)
            limit = int(self.settings.daily_budget_per_conversation)
            if budget >= limit:
                return GateDecision.refuse(f"这个会话今天的主动动作额度（{limit} 次）已用完。")
            global_count = self._global_count(now)
            cap = int(self.settings.global_daily_cap)
            if global_count >= cap:
                return GateDecision.refuse(f"今天全局主动动作已达上限（{cap} 次），先停一停。")
        return GateDecision.allow()

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def note_success(
        self,
        entry: ActionEntry,
        *,
        chat_key: str,
        object_id: str = "",
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self.store.set_gateway_state(
            SCOPE_CONVERSATION, self._conversation_key(entry, chat_key), now, now=now
        )
        if entry.proactive and object_id:
            stamps = self._object_stamps(entry.name, object_id, now)
            stamps.append(now)
            self.store.set_gateway_state(
                SCOPE_OBJECT, f"{entry.name}|{object_id}", stamps, now=now
            )
        if entry.proactive:
            self._bump_budget(chat_key, now)
            self._bump_global(now)
        # 成功一次就清掉连续失败计数
        self.store.delete_gateway_state(SCOPE_BACKOFF, entry.name)

    def note_failure(self, entry: ActionEntry, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        data = self._read(SCOPE_BACKOFF, entry.name) or {}
        fails = int(data.get("fails") or 0) + 1
        until = float(data.get("until") or 0.0)
        if fails >= FAILURE_LIMIT:
            until = now + FAILURE_BACKOFF_SECONDS
            fails = 0
            logger.warning("网关动作 %s 连续失败，停用 %.0f 秒", entry.name, FAILURE_BACKOFF_SECONDS)
        self.store.set_gateway_state(
            SCOPE_BACKOFF, entry.name, {"fails": fails, "until": until}, now=now
        )

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def audit(
        self,
        *,
        action: str,
        source: str,
        chat_key: str = "",
        chat_type: str = "",
        params: dict[str, Any] | None = None,
        ok: bool = False,
        result: str = "",
        reject_reason: str = "",
        sensitive: bool = False,
        now: float | None = None,
    ) -> None:
        """写审计；失败只记日志（审计写不进去不能反过来打断调用）。"""
        credential_action = action in CREDENTIAL_ACTIONS
        if credential_action:
            # 拒绝原因也可能回显参数或上游错误正文；只留判定元数据。
            params, result, reject_reason = None, "", ""
        platform, chat_id = _split_chat_key(chat_key)
        try:
            self.store.add_gateway_audit(
                action=action,
                source=source,
                platform=platform,
                chat_id=chat_id,
                chat_type=chat_type,
                params=params,
                ok=ok,
                result=result,
                reject_reason=reject_reason,
                sensitive=sensitive,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
            logger.warning("写网关审计失败：%s", action if credential_action else exc)

    # ------------------------------------------------------------------
    # 内部：状态读写
    # ------------------------------------------------------------------

    def _read(self, scope: str, key: str) -> Any:
        raw = self.store.get_gateway_state(scope, key)
        if raw is None:
            return None
        value, _updated = raw
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _conversation_key(entry: ActionEntry, chat_key: str) -> str:
        return f"{entry.name}|{chat_key}"

    def _conversation_remaining(
        self, entry: ActionEntry, chat_key: str, now: float
    ) -> float | None:
        if entry.cooldown_seconds <= 0:
            return None
        last = self._read(SCOPE_CONVERSATION, self._conversation_key(entry, chat_key))
        if not isinstance(last, (int, float)):
            return None
        return max(0.0, float(last) + entry.cooldown_seconds - now)

    def _object_stamps(self, action: str, object_id: str, now: float) -> list[float]:
        raw = self._read(SCOPE_OBJECT, f"{action}|{object_id}")
        stamps = [float(item) for item in raw if isinstance(item, (int, float))] if isinstance(raw, list) else []
        return [item for item in stamps if now - item < OBJECT_WINDOW_SECONDS]

    def _object_remaining(self, action: str, object_id: str, now: float) -> float | None:
        stamps = self._object_stamps(action, object_id, now)
        if len(stamps) < OBJECT_MAX_PER_WINDOW:
            return None
        oldest = min(stamps)
        return max(0.0, oldest + OBJECT_WINDOW_SECONDS - now)

    def _budget_count(self, chat_key: str, now: float) -> int:
        data = self._read(SCOPE_BUDGET, chat_key) or {}
        if data.get("day") != _day_key(now):
            return 0
        return int(data.get("count") or 0)

    def _bump_budget(self, chat_key: str, now: float) -> None:
        count = self._budget_count(chat_key, now) + 1
        self.store.set_gateway_state(
            SCOPE_BUDGET, chat_key, {"day": _day_key(now), "count": count}, now=now
        )

    def _global_count(self, now: float) -> int:
        data = self._read(SCOPE_GLOBAL, "actions") or {}
        if data.get("day") != _day_key(now):
            return 0
        return int(data.get("count") or 0)

    def _bump_global(self, now: float) -> None:
        count = self._global_count(now) + 1
        self.store.set_gateway_state(
            SCOPE_GLOBAL, "actions", {"day": _day_key(now), "count": count}, now=now
        )

    def _backoff(self, action: str, now: float) -> float | None:
        data = self._read(SCOPE_BACKOFF, action) or {}
        until = float(data.get("until") or 0.0)
        if until > now:
            return until - now
        return None


def _split_chat_key(chat_key: str) -> tuple[str, str]:
    """``platform:chat_id`` -> (platform, chat_id)；没有冒号时平台为空。"""
    text = str(chat_key or "")
    if ":" in text:
        platform, chat_id = text.split(":", 1)
        return platform, chat_id
    return "", text


def _seconds_text(seconds: float) -> str:
    """剩余秒数的口语化：<60 秒按秒，<1 小时按分钟，再长按小时。"""
    value = max(0, int(round(seconds)))
    if value < 60:
        return f"{value} 秒"
    if value < 3600:
        return f"{max(1, value // 60)} 分钟"
    return f"{value / 3600:.1f} 小时"
