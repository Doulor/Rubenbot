"""LLM 每日预算闸：按任务类型累计调用与费用估算，超限只停该类任务。

背景：``llm_usage`` 只记流水，不拦调用。模型池（Q14/Q21）之后一次角色调用可能
经过多次尝试，死循环或配置错误会在后台任务里反复烧额度。这里在客户端发起上游
请求**之前**按任务类型检查当日累计，命中上限就抛错并记一条 WARNING；其余任务
类型和 bot 主循环都不受影响（各调用方本来就按异常降级）。

统计口径（与 ``llm_usage`` 一致，标签来自 ``LLMClient.complete(task=...)``）：

- **调用次数**：每次上游尝试记 1 次，重试的失败尝试同样计入；拿不到响应的超时
  也算（它也确实发起过请求）。
- **token / 费用**：只累计上游实际给出的 usage；缺字段按 0 算，不猜。费用按
  ``budget.pricing`` 里配置的单价（美元 / 百万 token）估算，单价可按键
  ``"提供商/模型"``、``"模型"`` 或 ``"*"`` 回退。**没有配置单价时费用恒为 0**，
  费用上限不会触发——中转站没有公布单价时宁可不算，也不编一个数字。

台账与重置：台账落在数据库 ``meta`` 表的 ``llm_budget:ledger`` 键（JSON），按
本地自然日分桶；进程重启从表里恢复，跨日自动清零。只读/只写 ``meta``，不改
``llm_usage`` 表结构。台账里的 ``models`` 按 ``提供商/模型`` 分桶，便于按新三层
结构核对各提供商的消耗。

设计取舍：检查与记录都在进程内完成，多协程并发时允许少量超额（上限是护栏，
不是计费闸），实现保持轻量。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

META_KEY = "llm_budget:ledger"

# 默认每日调用上限（口径见 CHANGELOG / SPEC）：
# 2026-11-20～23 线上单任务日峰值 × 约 3–5 倍后取整。峰值示例：
# gen_stage1 365、gen_stage2 299、extract 180、image_description 100、
# sticker_description 81、sticker_select 57、conversation_state 52、chat 41。
# 未列出的任务类型用 ``default_daily_calls``。
DEFAULT_TASK_CALL_LIMITS: dict[str, int] = {
    "gen_stage1": 1500,
    "gen_stage2": 1200,
    "extract": 600,
    "image_description": 400,
    "sticker_description": 300,
    "sticker_select": 300,
    "conversation_state": 300,
    "chat": 300,
    "rolling_summary": 200,
    "group_profile": 150,
    "consolidate": 150,
    "distill": 100,
    "gate": 100,
    "event_judge": 50,
}
DEFAULT_DAILY_CALLS = 500

_UNSET = object()


@dataclass(frozen=True)
class ModelPrice:
    """单个模型的参考单价（美元 / 百万 token）。"""

    input_per_million: float = 0.0
    output_per_million: float = 0.0


@dataclass(frozen=True)
class TaskLimit:
    """一个任务类型的每日上限；``None`` 表示不限。"""

    daily_calls: int | None = None
    daily_cost_usd: float | None = None


@dataclass
class LLMBudgetConfig:
    """``llm.budget`` 配置段。

    - ``enabled``：总开关，默认开；关掉后只记账不拦（本实现里也不记）。
    - ``default_daily_calls``：未单列任务类型的每日调用上限；``null`` 不限。
    - ``default_daily_cost_usd``：未单列任务类型的每日费用上限；``null`` 不限。
    - ``tasks``：按任务类型覆盖，每项可写 ``daily_calls`` / ``daily_cost_usd``；
      写 ``null`` 表示该类不限，写 0 表示全部拦下。
    - ``pricing``：模型单价，键可写 ``"提供商/模型"``、``"模型"`` 或 ``"*"``。
    """

    enabled: bool = True
    default_daily_calls: int | None = DEFAULT_DAILY_CALLS
    default_daily_cost_usd: float | None = None
    tasks: dict[str, TaskLimit] = field(default_factory=dict)
    pricing: dict[str, ModelPrice] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> "LLMBudgetConfig":
        """宽松解析配置：写错类型回落默认并记 warning，不挡住启动。"""
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            logger.warning("llm.budget 应为配置对象，已忽略：%r", type(raw).__name__)
            return cls()

        enabled = raw.get("enabled", True) is not False
        default_calls = _parse_calls(
            raw.get("default_daily_calls", _UNSET), DEFAULT_DAILY_CALLS, "llm.budget.default_daily_calls"
        )
        default_cost = _parse_money(
            raw.get("default_daily_cost_usd", _UNSET),
            None,
            "llm.budget.default_daily_cost_usd",
        )

        tasks: dict[str, TaskLimit] = {}
        raw_tasks = raw.get("tasks")
        if isinstance(raw_tasks, dict):
            for raw_name, spec in raw_tasks.items():
                name = str(raw_name).strip()
                if not name:
                    continue
                if spec is None:
                    # 任务级写 null：这个任务类型不限，覆盖内置默认
                    tasks[name] = TaskLimit(None, None)
                    continue
                task_calls = DEFAULT_TASK_CALL_LIMITS.get(name, default_calls)
                task_cost = default_cost
                if isinstance(spec, dict):
                    task_calls = _parse_calls(
                        spec.get("daily_calls", _UNSET), task_calls, f"llm.budget.tasks.{name}.daily_calls"
                    )
                    task_cost = _parse_money(
                        spec.get("daily_cost_usd", _UNSET),
                        task_cost,
                        f"llm.budget.tasks.{name}.daily_cost_usd",
                    )
                else:
                    logger.warning("llm.budget.tasks.%s 应为对象或 null，已忽略", name)
                    continue
                tasks[name] = TaskLimit(task_calls, task_cost)
        elif raw_tasks is not None:
            logger.warning("llm.budget.tasks 应为任务类型到上限的对象，已忽略")

        pricing: dict[str, ModelPrice] = {}
        raw_pricing = raw.get("pricing")
        if isinstance(raw_pricing, dict):
            for raw_key, price in raw_pricing.items():
                key = str(raw_key).strip()
                if not key or not isinstance(price, dict):
                    if key:
                        logger.warning("llm.budget.pricing.%s 应为 {input, output} 对象，已忽略", key)
                    continue
                pricing[key] = ModelPrice(
                    input_per_million=_price_value(price.get("input"), key),
                    output_per_million=_price_value(price.get("output"), key),
                )
        elif raw_pricing is not None:
            logger.warning("llm.budget.pricing 应为模型到单价的对象，已忽略")

        return cls(
            enabled=enabled,
            default_daily_calls=default_calls,
            default_daily_cost_usd=default_cost,
            tasks=tasks,
            pricing=pricing,
        )

    def limit_for(self, task: str) -> TaskLimit:
        """任务类型的生效上限：显式覆盖（含 ``null`` 表示不限）优先，否则内置默认。"""
        if task in self.tasks:
            return self.tasks[task]
        calls = DEFAULT_TASK_CALL_LIMITS.get(task, self.default_daily_calls)
        return TaskLimit(calls, self.default_daily_cost_usd)

    def has_cost_limits(self) -> bool:
        if self.default_daily_cost_usd is not None:
            return True
        return any(limit.daily_cost_usd is not None for limit in self.tasks.values())

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        """按「提供商/模型 → 模型 → *」顺序找单价；都没有返回 ``None``。"""
        keys = []
        if provider and model:
            keys.append(f"{provider}/{model}")
        if model:
            keys.append(model)
        keys.append("*")
        for key in keys:
            price = self.pricing.get(key)
            if price is not None:
                return price
        return None


def _parse_calls(value: Any, default: int | None, field_name: str) -> int | None:
    if value is _UNSET:
        return default
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning("%s 应为非负整数或 null，已用默认值 %s", field_name, default)
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning("%s 应为非负整数或 null，已用默认值 %s", field_name, default)
        return default
    if number < 0:
        logger.warning("%s 不能为负数，已用默认值 %s", field_name, default)
        return default
    return number


def _parse_money(value: Any, default: float | None, field_name: str) -> float | None:
    if value is _UNSET:
        return default
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning("%s 应为非负数或 null，已用默认值 %s", field_name, default)
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("%s 应为非负数或 null，已用默认值 %s", field_name, default)
        return default
    if number < 0:
        logger.warning("%s 不能为负数，已用默认值 %s", field_name, default)
        return default
    return number


def _price_value(value: Any, key: str) -> float:
    try:
        number = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        logger.warning("llm.budget.pricing.%s 的单价不是数字，按 0 处理", key)
        return 0.0
    if number < 0:
        logger.warning("llm.budget.pricing.%s 的单价为负数，按 0 处理", key)
        return 0.0
    return number


@dataclass(frozen=True)
class BudgetDenial:
    """一次被拦下的调用；由客户端包装成异常抛出。"""

    task: str
    kind: str  # calls / cost
    limit: float
    current: float

    def message(self) -> str:
        if self.kind == "calls":
            return f"任务 {self.task} 今日调用 {int(self.current)} 次已达上限 {int(self.limit)}"
        return (
            f"任务 {self.task} 今日估算费用 ${self.current:.4f} 已达上限 ${self.limit:.4f}"
        )


class LLMBudgetGate:
    """每日调用/费用台账与熔断判断。

    ``store`` 只要求实现 ``get_meta`` / ``set_meta``；为 ``None`` 时闸门关闭
    （测试里的假回调用不到库，自然也不拦）。
    """

    def __init__(
        self,
        config: LLMBudgetConfig,
        store: Any = None,
        *,
        clock: Any = None,
    ):
        self.config = config
        self.store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._date = ""
        self._tasks: dict[str, dict[str, float]] = {}
        self._models: dict[str, dict[str, float]] = {}
        # (日期, 任务, 原因) → 熔断日志只在该类第一次命中时打 WARNING
        self._tripped: set[tuple[str, str, str]] = set()
        if self.active and config.has_cost_limits() and not config.pricing:
            logger.warning(
                "llm.budget 配置了费用上限但没有 pricing 单价，费用估算恒为 0，"
                "费用上限不会触发（次数上限照常生效）"
            )

    @property
    def active(self) -> bool:
        return bool(self.config.enabled and self.store is not None)

    def denial(self, task: str) -> BudgetDenial | None:
        """该类任务今日是否已超限；超限时返回原因并（首次）记一条 WARNING。"""
        if not self.active:
            return None
        task = str(task or "chat")
        with self._lock:
            self._roll(self._now())
            entry = self._tasks.get(task) or {}
            calls = int(entry.get("calls", 0))
            cost = float(entry.get("cost_usd", 0.0))
            limits = self.config.limit_for(task)
            denial: BudgetDenial | None = None
            if limits.daily_calls is not None and calls >= limits.daily_calls:
                denial = BudgetDenial(task, "calls", limits.daily_calls, calls)
            elif (
                limits.daily_cost_usd is not None
                and self.config.pricing
                and cost >= limits.daily_cost_usd
            ):
                # 没配任何单价时费用估算没有意义，费用档不参与判断（只记 warning）
                denial = BudgetDenial(task, "cost", limits.daily_cost_usd, cost)
            if denial is not None:
                key = (self._date, denial.task, denial.kind)
                if key not in self._tripped:
                    self._tripped.add(key)
                    logger.warning(
                        "LLM 预算熔断：%s，该类任务暂停发起新调用（其他任务类型不受影响）",
                        denial.message(),
                    )
                else:
                    logger.debug("LLM 预算熔断持续：%s", denial.message())
            return denial

    def record(
        self,
        task: str,
        *,
        provider: str = "",
        model: str = "",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """记一次上游尝试（成功失败都记）；台账写库失败只记 debug。"""
        if not self.active:
            return
        task = str(task or "chat")
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        model_key = f"{provider}/{model}" if provider and model else (model or "unknown")
        cost = self._cost(provider, model, prompt, completion)
        with self._lock:
            self._roll(self._now())
            entry = self._tasks.setdefault(task, _empty_entry())
            entry["calls"] += 1
            entry["prompt_tokens"] += prompt
            entry["completion_tokens"] += completion
            entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)
            per_model = self._models.setdefault(model_key, _empty_entry())
            per_model["calls"] += 1
            per_model["prompt_tokens"] += prompt
            per_model["completion_tokens"] += completion
            per_model["cost_usd"] = round(per_model["cost_usd"] + cost, 6)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        """当前台账快照（便于排查与测试），不含凭据。"""
        with self._lock:
            self._roll(self._now())
            return {
                "date": self._date,
                "tasks": {key: dict(value) for key, value in self._tasks.items()},
                "models": {key: dict(value) for key, value in self._models.items()},
            }

    # ---- 内部 ----

    def _now(self) -> float:
        return float(self._clock()) if self._clock is not None else time.time()

    def _roll(self, now: float) -> None:
        """确保台账对应当前本地日期；跨日清空并从 meta 恢复当天数据。"""
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today == self._date:
            return
        ledger = self._read_ledger()
        if ledger.get("date") == today:
            self._tasks = {
                str(key): _entry_from(value)
                for key, value in (ledger.get("tasks") or {}).items()
                if isinstance(value, dict)
            }
            self._models = {
                str(key): _entry_from(value)
                for key, value in (ledger.get("models") or {}).items()
                if isinstance(value, dict)
            }
        else:
            if ledger.get("date"):
                logger.info("LLM 预算台账跨日重置：%s → %s", ledger.get("date"), today)
            self._tasks = {}
            self._models = {}
        self._date = today
        self._tripped = {key for key in self._tripped if key[0] == today}

    def _read_ledger(self) -> dict[str, Any]:
        get_meta = getattr(self.store, "get_meta", None)
        if not callable(get_meta):
            return {}
        try:
            raw = get_meta(META_KEY)
        except Exception as exc:  # noqa: BLE001 - 台账读不到按空处理
            logger.debug("LLM 预算台账读取失败：%s", exc)
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("LLM 预算台账不是有效 JSON，按空台账处理")
            return {}
        return data if isinstance(data, dict) else {}

    def _persist(self) -> None:
        set_meta = getattr(self.store, "set_meta", None)
        if not callable(set_meta):
            return
        payload = json.dumps(
            {"date": self._date, "tasks": self._tasks, "models": self._models},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            set_meta(META_KEY, payload)
        except Exception as exc:  # noqa: BLE001 - 台账写失败不影响本次调用
            logger.debug("LLM 预算台账写入失败：%s", exc)

    def _cost(self, provider: str, model: str, prompt: int, completion: int) -> float:
        price = self.config.price_for(provider, model)
        if price is None:
            return 0.0
        return (
            prompt / 1_000_000 * price.input_per_million
            + completion / 1_000_000 * price.output_per_million
        )


def _empty_entry() -> dict[str, float]:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def _entry_from(value: dict[str, Any]) -> dict[str, float]:
    entry = _empty_entry()
    for key in entry:
        raw = value.get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            entry[key] = number if key == "cost_usd" else int(number)
    return entry


__all__ = [
    "DEFAULT_DAILY_CALLS",
    "DEFAULT_TASK_CALL_LIMITS",
    "BudgetDenial",
    "LLMBudgetConfig",
    "LLMBudgetGate",
    "ModelPrice",
    "TaskLimit",
]
