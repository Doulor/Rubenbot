"""触发路由与事件合并：把「所有事件」分成三档，并在安静时段挂起。

第二层闸（总纲）：

- **必判**：戳一戳戳到 bot、好友申请、入群申请/邀请；
- **条件判**：``group_increase``——该群有 bot 活动记录，或进群者在画像里存在；
- **只记录**：其余通知（他人撤回/禁言/退群/管理员变动等）。

安静时段（``gateway.quiet_hours``，默认 23:00-08:00）里，非必判事件只挂起
记录；必判事件也只记录 + 挂待办，不当场动作（申请本来就可以等）。

事件合并：同一会话 10 秒内同类事件合并成一次判断（如批量进群）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..adapters.platform import (
    KIND_NOTICE,
    KIND_REQUEST,
    ROUTE_CONDITIONAL,
    ROUTE_JUDGE,
    ROUTE_RECORD,
    PlatformEvent,
)
from ..config import GatewayConfig
from ..decision import parse_quiet_hours

# 合并窗口（秒）：同会话同类事件在这个窗口内只判断一次
MERGE_WINDOW_SECONDS = 10.0


@dataclass
class RouteDecision:
    """一次路由的结论。

    ``route`` 是三档之一；``quiet`` 表示命中了安静时段（只记录/挂待办）；
    ``reason`` 给日志看，不进入模型提示。
    """

    route: str
    quiet: bool = False
    reason: str = ""

    @property
    def judge(self) -> bool:
        """是否应当进入快判（第三层）。"""
        return self.route in (ROUTE_JUDGE, ROUTE_CONDITIONAL) and not self.quiet


@dataclass
class EventBatch:
    """合并窗口里攒着的一批同类事件。"""

    key: tuple[str, str, str]
    events: list[PlatformEvent] = field(default_factory=list)
    route: str = ROUTE_RECORD
    quiet: bool = False
    deadline: float = 0.0

    @property
    def first(self) -> PlatformEvent:
        return self.events[0]

    @property
    def conversation_pair(self) -> tuple[str, str]:
        first = self.first
        return first.platform, first.conversation_id

    @property
    def description(self) -> str:
        """合并后的一句话：单条直接给描述，多条给计数 + 首条描述。"""
        if len(self.events) == 1:
            return self.first.description
        return f"（{len(self.events)} 条同类事件）{self.first.description}；等"


def quiet_now(cfg: GatewayConfig, now: float) -> bool:
    """当前是否处于安静时段；区间格式与 ``proactive.quiet_hours`` 一致。"""
    text = str(cfg.quiet_hours or "").strip()
    if not text:
        return False
    ranges = parse_quiet_hours([text])
    if not ranges:
        return False
    local = time.localtime(now)
    minutes = local.tm_hour * 60 + local.tm_min
    for start, end in ranges:
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # 跨零点
            return True
    return False


class EventRouter:
    """三档路由 + 安静时段判定。

    ``activity_fn`` / ``profile_fn`` 是条件判要用的两个探测函数（注入以便
    测试与解耦）：前者判断「该群有没有 bot 活动记录」，后者判断「这个人在
    画像里存不存在」。
    """

    def __init__(
        self,
        config: GatewayConfig,
        *,
        activity_fn=None,
        profile_fn=None,
    ):
        self.config = config
        self.activity_fn = activity_fn
        self.profile_fn = profile_fn

    def route(self, event: PlatformEvent, *, now: float | None = None) -> RouteDecision:
        now = time.time() if now is None else now
        route = self._base_route(event)
        quiet = quiet_now(self.config, now)
        if route == ROUTE_RECORD:
            return RouteDecision(route, quiet=quiet, reason="只记录")
        if route == ROUTE_CONDITIONAL:
            reason = "条件判：群里有活动或进群者已认识" if not quiet else "条件判（安静时段只记录）"
            return RouteDecision(route, quiet=quiet, reason=reason)
        # 必判：安静时段也只记录 + 挂待办
        return RouteDecision(
            route,
            quiet=quiet,
            reason="必判" if not quiet else "必判（安静时段只记录并挂待办）",
        )

    # ------------------------------------------------------------------

    def _base_route(self, event: PlatformEvent) -> str:
        if event.is_request:
            # 好友申请、入群申请/邀请：必须让模型知道（再由分寸闸决定能不能处理）
            return ROUTE_JUDGE
        if event.kind != KIND_NOTICE:
            return ROUTE_RECORD
        if event.type == "notify" and event.sub_type in ("poke", "poke_self"):
            return ROUTE_JUDGE if event.targets_bot else ROUTE_RECORD
        if event.type == "poke":
            return ROUTE_JUDGE if event.targets_bot else ROUTE_RECORD
        if event.type == "group_increase":
            if self._group_active(event) or self._person_known(event):
                return ROUTE_CONDITIONAL
            return ROUTE_RECORD
        return ROUTE_RECORD

    def _group_active(self, event: PlatformEvent) -> bool:
        if self.activity_fn is None:
            return False
        try:
            return bool(self.activity_fn(event.platform, event.conversation_id))
        except Exception:  # noqa: BLE001 - 探测失败按「不确定」处理，偏保守
            return False

    def _person_known(self, event: PlatformEvent) -> bool:
        if self.profile_fn is None:
            return False
        try:
            return bool(self.profile_fn(event.platform, event.actor_id))
        except Exception:  # noqa: BLE001
            return False


class EventMerger:
    """同会话同类事件的 10 秒合并窗口；纯内存、时钟外部传入。"""

    def __init__(self, window: float = MERGE_WINDOW_SECONDS):
        self.window = max(0.0, float(window))
        self._batches: dict[tuple[str, str, str], EventBatch] = {}

    def __len__(self) -> int:
        return len(self._batches)

    def add(
        self,
        event: PlatformEvent,
        route: RouteDecision,
        now: float,
    ) -> list[EventBatch]:
        """加入一批，返回可以判断的批次（``window=0`` 时立即返回这一批）。

        合并语义：同会话同类事件在窗口内只判一次——已有一批未到期时追加，
        不另起一批；到期的批次由 :meth:`pop_ready` 统一放出。这里返回的是
        「本次加入导致可判断」的批次：``window=0`` 时就是刚加的这一批，否则
        是当时已到期的那几批。
        """
        key = event.dedupe_key()
        batch = self._batches.get(key)
        if batch is None:
            batch = EventBatch(key=key, route=route.route, quiet=route.quiet, deadline=now + self.window)
            self._batches[key] = batch
        batch.events.append(event)
        if self.window == 0:
            self._batches.pop(key, None)
            return [batch]
        return self.pop_ready(now)

    def pop_ready(self, now: float) -> list[EventBatch]:
        """取出已到期的批次（按到期顺序）。"""
        ready: list[EventBatch] = []
        for key, batch in list(self._batches.items()):
            if now >= batch.deadline:
                ready.append(batch)
                del self._batches[key]
        ready.sort(key=lambda item: item.deadline)
        return ready

    def flush_all(self) -> list[EventBatch]:
        batches = list(self._batches.values())
        self._batches.clear()
        return batches

    def next_deadline(self) -> float | None:
        if not self._batches:
            return None
        return min(batch.deadline for batch in self._batches.values())


def request_requires_owner(event: PlatformEvent) -> bool:
    """申请类事件一期只能通知主人或忽略（硬规则，代码层强制）。"""
    return event.kind == KIND_REQUEST
