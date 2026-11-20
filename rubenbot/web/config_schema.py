"""设置页的结构化配置：表单视图、逐字段校验、写回 YAML。

WebUI 用与 ``config.yaml`` 同构的嵌套字典编辑配置，这个模块负责三件事：

- :func:`structured_config`：生成表单初始值。密钥字段一律留空，另用
  ``secrets`` 里的布尔值告诉前端「已设置，留空不改」。
- :func:`validate_structured`：逐字段校验，把数字输入框传来的字符串转成
  数字，返回只含**已提交字段**的规范字典。不合法时抛
  :class:`ConfigValidationError`，里面带着 ``fields`` 列表，API 直接以 422
  返回，不会写文件。
- :func:`merge_config`：把规范字典合并进现有 YAML，保留未知键；
  ``decision.groups`` 和 ``adapters`` 这类整体语义的段按表单为准替换/更新。

密钥字段（``llm.api_key``、``search.tavily.api_key`` / ``search.deepseek.api_key``、
``adapters.snowluma.access_token``、``webui.password``）提交空值表示保留原值：校验阶段
直接把这些键从更新里剔除，因此脱敏占位符和空字符串都不会写回文件。
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from ..config import SEARCH_PROVIDERS, Config
from ..decision import parse_quiet_hours

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ADAPTER_NAMES = ("console", "snowluma")

SNOWLUMA_DEFAULTS = {
    "ws_url": "ws://127.0.0.1:3001",
    "http_url": "http://127.0.0.1:3000",
    "access_token": "",
    "self_id": "",
}

# 表单视图里永远留空的密钥字段（键是点分路径）
SECRET_PATHS = (
    "llm.api_key",
    "search.tavily.api_key",
    "search.deepseek.api_key",
    "adapters.snowluma.access_token",
    "webui.password",
)

# 合并时整段替换、不与旧值递归合并的路径
_REPLACE_PATHS = {("decision", "groups")}


class ConfigValidationError(ValueError):
    """结构化配置校验失败；``fields`` 是 [{field, message}, ...]。"""

    def __init__(self, fields: list[dict[str, str]]):
        self.fields = fields
        summary = "；".join(f"{item['field']}: {item['message']}" for item in fields[:6])
        super().__init__(summary or "配置校验失败")


# ----------------------------------------------------------------------
# 表单视图
# ----------------------------------------------------------------------


def structured_config(config: Config) -> dict[str, Any]:
    """当前生效配置的表单视图：完整字段、密钥置空、附带 ``secrets`` 标记。"""
    enabled = set(config.enabled_adapters)
    snow = config.adapters.get("snowluma")
    snow = snow if isinstance(snow, dict) else {}
    decision = asdict(config.decision)
    llm = asdict(config.llm)
    api_key = llm.pop("api_key", "")
    # 抽取/归纳模型留空表示跟随对话模型，表单里统一用空串，避免 null 与 ""
    # 在「打开设置 → 原样保存」时被误判成改动
    llm["extract_model"] = llm.get("extract_model") or ""
    llm["consolidate_model"] = llm.get("consolidate_model") or ""
    llm["gate_model"] = llm.get("gate_model") or ""
    # 角色表统一成「角色 → 模型名」，没配的角色不出现
    models = llm.get("models")
    llm["models"] = dict(models) if isinstance(models, dict) else {}
    webui = asdict(config.webui)
    password = webui.pop("password", "")
    search = asdict(config.search)
    # 两个渠道的 key 一律留空（提示前端「已设置，留空不改」）
    search_keys = {}
    for name in SEARCH_PROVIDERS:
        channel = search.get(name) if isinstance(search.get(name), dict) else {}
        search_keys[name] = channel.get("api_key", "")
        if isinstance(search.get(name), dict):
            search[name]["api_key"] = ""
    access_token = snow.get("access_token", "")

    return {
        "llm": {**llm, "api_key": ""},
        "persona": {"path": config.persona_path},
        "database_path": config.database_path,
        "memory": asdict(config.memory),
        "search": search,
        "chat": asdict(config.chat),
        "decision": decision,
        "generation": asdict(config.generation),
        "reminder": asdict(config.reminder),
        "proactive": {
            **asdict(config.proactive),
            "quiet_hours": list(config.proactive.quiet_hours),
        },
        "plugins": {
            "dir": config.plugins.dir,
            "enabled": list(config.plugins.enabled),
            "command_prefix": config.plugins.command_prefix,
        },
        "adapters": {
            "console": {"enabled": "console" in enabled},
            "snowluma": {
                "enabled": "snowluma" in enabled,
                "ws_url": snow.get("ws_url") or SNOWLUMA_DEFAULTS["ws_url"],
                "http_url": snow.get("http_url") or SNOWLUMA_DEFAULTS["http_url"],
                "access_token": "",
                "self_id": "" if snow.get("self_id") is None else str(snow.get("self_id")),
            },
        },
        "webui": {**webui, "password": ""},
        "background_check_minutes": config.background_check_minutes,
        "log_level": config.log_level,
        "secrets": {
            "llm.api_key": bool(api_key),
            "search.tavily.api_key": bool(search_keys.get("tavily")),
            "search.deepseek.api_key": bool(search_keys.get("deepseek")),
            "adapters.snowluma.access_token": bool(access_token),
            "webui.password": bool(password),
        },
    }


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


class _Validator:
    """收集错误的小工具：单个字段失败不打断后续字段的检查。"""

    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def fail(self, field: str, message: str) -> None:
        self.errors.append({"field": field, "message": message})

    def number(
        self,
        value: Any,
        field: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        integer: bool = False,
    ) -> float | int | None:
        if isinstance(value, bool) or value is None or value == "":
            self.fail(field, "必须是数字")
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            self.fail(field, "必须是数字")
            return None
        if number != number or number in (float("inf"), float("-inf")):
            self.fail(field, "必须是有限的数字")
            return None
        if integer:
            if not number.is_integer():
                self.fail(field, "必须是整数")
                return None
            number = int(number)
        if minimum is not None and number < minimum:
            self.fail(field, f"不能小于 {minimum:g}")
            return None
        if maximum is not None and number > maximum:
            self.fail(field, f"不能大于 {maximum:g}")
            return None
        return number

    def boolean(self, value: Any, field: str) -> bool | None:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        self.fail(field, "必须是开关（true/false）")
        return None

    def text(
        self,
        value: Any,
        field: str,
        *,
        allow_empty: bool = False,
        max_len: int = 400,
    ) -> str | None:
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)):
            self.fail(field, "必须是文本")
            return None
        text = str(value).strip()
        if not text and not allow_empty:
            self.fail(field, "不能为空")
            return None
        if len(text) > max_len:
            self.fail(field, f"太长（最多 {max_len} 字）")
            return None
        return text

    def string_list(self, value: Any, field: str) -> list[str] | None:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            self.fail(field, "应为字符串列表")
            return None
        result: list[str] = []
        for item in value:
            if item is None:
                continue
            if not isinstance(item, (str, int, float)):
                self.fail(field, "列表里只能放字符串")
                return None
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result


def _section(payload: dict[str, Any], key: str, v: _Validator) -> dict[str, Any] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        v.fail(key, "应为对象")
        return None
    return value


def _prune_secret(container: dict[str, Any], key: str) -> None:
    """空值/脱敏占位符表示保留原值：直接从更新里去掉这个键。"""
    value = container.get(key)
    if value is None:
        container.pop(key, None)
        return
    text = str(value)
    if not text.strip() or "*" in text:
        container.pop(key, None)


def _validate_models(value: Any, v: _Validator) -> dict[str, str] | None:
    """校验 ``llm.models`` 角色映射：角色名非空，模型名填了就保留、空串表示未配。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        v.fail("llm.models", "应为「角色 → 模型名」的对象")
        return None
    result: dict[str, str] = {}
    for raw_role, raw_model in value.items():
        role = str(raw_role).strip()
        if not role:
            continue  # 空角色名直接忽略，不算错误
        if raw_model is None:
            continue
        if not isinstance(raw_model, str):
            v.fail(f"llm.models.{role}", "必须填模型名或留空")
            continue
        model = raw_model.strip()
        if model:
            result[role] = model
    return result


def _validate_llm(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "base_url" in section:
        value = v.text(section["base_url"], "llm.base_url")
        if value is not None:
            out["base_url"] = value
    if "api_key" in section:
        copied = {"api_key": section["api_key"]}
        _prune_secret(copied, "api_key")
        if "api_key" in copied:
            out["api_key"] = str(copied["api_key"])
    if "chat_model" in section:
        value = v.text(section["chat_model"], "llm.chat_model")
        if value is not None:
            out["chat_model"] = value
    for key in ("extract_model", "consolidate_model", "gate_model"):
        if key in section:
            value = section[key]
            if value is None:
                out[key] = ""
            elif isinstance(value, str):
                out[key] = value.strip()
            else:
                v.fail(f"llm.{key}", "必须填模型名或留空（留空跟随对话模型）")
    if "models" in section:
        models = _validate_models(section["models"], v)
        if models is not None:
            out["models"] = models
    if "temperature" in section:
        number = v.number(section["temperature"], "llm.temperature", minimum=0, maximum=2)
        if number is not None:
            out["temperature"] = float(number)
    if "timeout" in section:
        number = v.number(section["timeout"], "llm.timeout", minimum=0.1)
        if number is not None:
            out["timeout"] = float(number)
    if "max_retries" in section:
        number = v.number(section["max_retries"], "llm.max_retries", minimum=0, maximum=10, integer=True)
        if number is not None:
            out["max_retries"] = int(number)
    return out


def _validate_memory(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    int_fields = {
        "extract_every_n": 1,
        "extract_silence_seconds": 0,
        "extract_min_pending": 1,
        "extract_window": 1,
        "inject_limit": 0,
        "consolidate_new_threshold": 1,
        "consolidate_max_memories": 1,
        "consolidate_remove_min": 1,
        "raw_keep_per_chat": 1,
        # 记忆 v2 读取侧
        "index_max_members": 1,
        "index_identity_chars": 8,
        "mentioned_max_people": 0,
        "profile_inject_max_chars": 120,
        "inject_budget_tokens": 0,
        "timeline_hit_limit": 1,
        "self_inject_limit": 0,
        "recall_tool_max_rounds": 0,
    }
    for key, minimum in int_fields.items():
        if key in section:
            number = v.number(section[key], f"memory.{key}", minimum=minimum, integer=True)
            if number is not None:
                out[key] = int(number)
    float_fields = {
        "dedupe_threshold": (0, 1),
        "consolidate_interval_hours": (0.1, None),
        "consolidate_remove_ratio": (0, 1),
        # 记忆 v2 读取侧
        "index_active_days": (0.1, None),
        "index_ttl_seconds": (0, None),
        "cross_session_hours": (0.1, None),
        "self_inject_hours": (0.1, None),
        "gate_confidence_threshold": (0, 1),
    }
    for key, (minimum, maximum) in float_fields.items():
        if key in section:
            number = v.number(section[key], f"memory.{key}", minimum=minimum, maximum=maximum)
            if number is not None:
                out[key] = float(number)
    for key in ("gate_ask_high_sensitivity", "recall_tool_enabled"):
        if key in section:
            flag = v.boolean(section[key], f"memory.{key}")
            if flag is not None:
                out[key] = flag
    rolling = _section(section, "rolling_summary", v)
    if rolling is not None:
        out["rolling_summary"] = _validate_rolling_summary(rolling, v)
    return out


def _validate_rolling_summary(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """滚动摘要的嵌套配置：开关 + 三个上限。"""
    out: dict[str, Any] = {}
    if "enabled" in section:
        flag = v.boolean(section["enabled"], "memory.rolling_summary.enabled")
        if flag is not None:
            out["enabled"] = flag
    for key, minimum in (("trigger_after", 1), ("keep_recent", 0), ("max_chars", 100)):
        if key in section:
            number = v.number(
                section[key], f"memory.rolling_summary.{key}", minimum=minimum, integer=True
            )
            if number is not None:
                out[key] = int(number)
    return out


def _validate_search(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """联网搜索：order 只认已知渠道；各渠道 key 与 llm 一样，空值表示保留原值。"""
    out: dict[str, Any] = {}
    if "order" in section:
        raw = section["order"]
        if isinstance(raw, str):
            raw = [item for item in raw.split(",")]
        if not isinstance(raw, (list, tuple)):
            v.fail("search.order", "应为渠道列表")
        else:
            order: list[str] = []
            for item in raw:
                name = str(item or "").strip().lower()
                if not name:
                    continue
                if name not in SEARCH_PROVIDERS:
                    v.fail("search.order", f"只能是 {' / '.join(SEARCH_PROVIDERS)} 中的渠道")
                    break
                if name not in order:
                    order.append(name)
            else:
                if not order:
                    v.fail("search.order", "至少放一个渠道")
                else:
                    out["order"] = order
    for name in SEARCH_PROVIDERS:
        channel = section.get(name)
        if channel is None:
            continue
        if not isinstance(channel, dict):
            v.fail(f"search.{name}", "应为对象")
            continue
        if "api_key" in channel:
            copied = {"api_key": channel["api_key"]}
            _prune_secret(copied, "api_key")
            if "api_key" in copied:
                out.setdefault(name, {})["api_key"] = str(copied["api_key"])
    if "timeout_seconds" in section:
        number = v.number(section["timeout_seconds"], "search.timeout_seconds", minimum=0.1)
        if number is not None:
            out["timeout_seconds"] = float(number)
    if "cooldown_seconds" in section:
        number = v.number(section["cooldown_seconds"], "search.cooldown_seconds", minimum=0)
        if number is not None:
            out["cooldown_seconds"] = float(number)
    return out


def _validate_chat(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "bot_name" in section:
        value = v.text(section["bot_name"], "chat.bot_name", max_len=60)
        if value is not None:
            out["bot_name"] = value
    for key, minimum in (("history_size", 1), ("split_max_parts", 1), ("split_part_chars", 8)):
        if key in section:
            number = v.number(section[key], f"chat.{key}", minimum=minimum, integer=True)
            if number is not None:
                out[key] = int(number)
    for key in ("split_delay_seconds", "history_ttl_hours"):
        if key in section:
            number = v.number(section[key], f"chat.{key}", minimum=0)
            if number is not None:
                out[key] = float(number)
    return out


def _validate_generation(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    two_stage = section.get("two_stage")
    if two_stage is None:
        return out
    if not isinstance(two_stage, dict):
        v.fail("generation.two_stage", "应为对象")
        return out
    entry: dict[str, Any] = {}
    if "enabled" in two_stage:
        flag = v.boolean(two_stage["enabled"], "generation.two_stage.enabled")
        if flag is not None:
            entry["enabled"] = flag
    if entry:
        out["two_stage"] = entry
    return out


def _validate_decision(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    number_fields = {
        "merge_window_seconds": (0, None),
        "hard_merge_window_seconds": (0, None),
        "base": (0, 1),
        "threshold": (0, 1),
        "threshold_noise": (0, 1),
        "threshold_noise_band": (0, 1),
        "momentum_bonus": (0, 10),
        "momentum_half_life_seconds": (1, None),
        "relevance_bonus": (0, 10),
        "relevance_scale": (0.1, None),
        "ratio_threshold": (0, 1),
        "ratio_silence": (0, 1),
        "ratio_penalty": (0, 10),
        "cooldown_seconds": (0, None),
        "cooldown_penalty": (0, 10),
    }
    for key, (minimum, maximum) in number_fields.items():
        if key in section:
            number = v.number(section[key], f"decision.{key}", minimum=minimum, maximum=maximum)
            if number is not None:
                out[key] = float(number)
    if "ratio_window" in section:
        number = v.number(section["ratio_window"], "decision.ratio_window", minimum=1, integer=True)
        if number is not None:
            out["ratio_window"] = int(number)
    if "aliases" in section:
        aliases = v.string_list(section["aliases"], "decision.aliases")
        if aliases is not None:
            out["aliases"] = aliases
    if "veto_marker" in section:
        value = v.text(section["veto_marker"], "decision.veto_marker", max_len=40)
        if value is not None:
            out["veto_marker"] = value
    if "groups" in section:
        groups = _validate_groups(section["groups"], v)
        if groups is not None:
            out["groups"] = groups
    return out


def _validate_groups(value: Any, v: _Validator) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        v.fail("decision.groups", "应为「群号 → 配置」的对象")
        return None
    result: dict[str, Any] = {}
    for raw_id, options in value.items():
        chat_id = str(raw_id).strip()
        if not chat_id:
            v.fail("decision.groups", "群号不能为空")
            continue
        field = f"decision.groups.{chat_id}"
        if not isinstance(options, dict):
            v.fail(field, "配置应为对象")
            continue
        entry: dict[str, Any] = {}
        if "activity_multiplier" in options:
            number = v.number(
                options["activity_multiplier"], f"{field}.activity_multiplier", minimum=0, maximum=10
            )
            if number is not None:
                entry["activity_multiplier"] = float(number)
        else:
            entry["activity_multiplier"] = 1.0
        base = options.get("base")
        if base in (None, ""):
            entry["base"] = None
        else:
            number = v.number(base, f"{field}.base", minimum=0, maximum=1)
            if number is not None:
                entry["base"] = float(number)
        quiet = v.string_list(options.get("quiet_hours"), f"{field}.quiet_hours")
        if quiet is not None:
            if quiet and not parse_quiet_hours(quiet):
                v.fail(f"{field}.quiet_hours", "格式应为 HH:MM-HH:MM，例如 23:00-07:30")
            elif len(parse_quiet_hours(quiet)) != len(quiet):
                v.fail(f"{field}.quiet_hours", "有格式不对或起止相同的时段")
            else:
                entry["quiet_hours"] = quiet
        result[chat_id] = entry
    return result


def _validate_reminder(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "enabled" in section:
        flag = v.boolean(section["enabled"], "reminder.enabled")
        if flag is not None:
            out["enabled"] = flag
    for key, minimum, maximum in (
        ("max_pending_per_chat", 0, 50),
        ("max_delay_minutes", 1, 60 * 24 * 365),
        ("max_text_chars", 20, 2000),
    ):
        if key in section:
            number = v.number(
                section[key], f"reminder.{key}", minimum=minimum, maximum=maximum, integer=True
            )
            if number is not None:
                out[key] = int(number)
    return out


def _validate_proactive(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """冷场开口：开关 + 静默时段 + 三条门槛 + 一套低意愿评分参数。"""
    out: dict[str, Any] = {}
    if "enabled" in section:
        flag = v.boolean(section["enabled"], "proactive.enabled")
        if flag is not None:
            out["enabled"] = flag
    if "quiet_hours" in section:
        quiet = v.string_list(section["quiet_hours"], "proactive.quiet_hours")
        if quiet is not None:
            parsed = parse_quiet_hours(quiet)
            if len(parsed) != len(quiet):
                v.fail("proactive.quiet_hours", "格式应为 HH:MM-HH:MM，例如 23:00-08:00")
            else:
                out["quiet_hours"] = quiet
    for key, minimum in (
        ("idle_hours", 0.1),
        ("cooldown_minutes", 0),
    ):
        if key in section:
            number = v.number(section[key], f"proactive.{key}", minimum=minimum)
            if number is not None:
                out[key] = float(number)
    if "daily_limit" in section:
        number = v.number(
            section["daily_limit"], "proactive.daily_limit", minimum=0, maximum=100, integer=True
        )
        if number is not None:
            out["daily_limit"] = int(number)
    for key, minimum, maximum in (
        ("base", 0, 1),
        ("threshold", 0, 1),
        ("threshold_noise", 0, 1),
        ("threshold_noise_band", 0, 1),
        ("relevance_bonus", 0, 10),
        ("relevance_scale", 0.1, None),
    ):
        if key in section:
            number = v.number(section[key], f"proactive.{key}", minimum=minimum, maximum=maximum)
            if number is not None:
                out[key] = float(number)
    return out


def _validate_plugins(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "dir" in section:
        value = v.text(section["dir"], "plugins.dir")
        if value is not None:
            out["dir"] = value
    if "enabled" in section:
        # 显式启用制：留空/null 就是「一个都不启用」，不再表示全部启用
        value = section["enabled"]
        if value is None or value == "":
            out["enabled"] = []
        else:
            enabled = v.string_list(value, "plugins.enabled")
            if enabled is not None:
                out["enabled"] = enabled
    if "command_prefix" in section:
        value = v.text(section["command_prefix"], "plugins.command_prefix", allow_empty=True, max_len=8)
        if value is not None:
            out["command_prefix"] = value
    return out


def _validate_adapters(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ADAPTER_NAMES:
        if name not in section:
            continue
        options = section[name]
        if options is None or options is False:
            out[name] = None
            continue
        if options is True:
            options = {}
        if not isinstance(options, dict):
            v.fail(f"adapters.{name}", "应为对象")
            continue
        enabled = options.get("enabled", True)
        enabled = v.boolean(enabled, f"adapters.{name}.enabled")
        if enabled is False:
            out[name] = None
            continue
        if name == "console":
            out[name] = {}
            continue
        entry: dict[str, Any] = {}
        for key in ("ws_url", "http_url"):
            if key in options:
                value = v.text(options[key], f"adapters.snowluma.{key}")
                if value is not None:
                    entry[key] = value
        if "access_token" in options:
            copied = {"access_token": options["access_token"]}
            _prune_secret(copied, "access_token")
            if "access_token" in copied:
                entry["access_token"] = str(copied["access_token"])
        if "self_id" in options:
            value = options["self_id"]
            if value is None:
                entry["self_id"] = ""
            elif isinstance(value, (str, int)):
                entry["self_id"] = str(value).strip()
            else:
                v.fail("adapters.snowluma.self_id", "必须是 QQ 号（文本或数字）")
        out[name] = entry
    return out


def _validate_webui(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "enabled" in section:
        value = v.boolean(section["enabled"], "webui.enabled")
        if value is not None:
            out["enabled"] = value
    if "host" in section:
        value = v.text(section["host"], "webui.host", max_len=120)
        if value is not None:
            out["host"] = value
    if "port" in section:
        number = v.number(section["port"], "webui.port", minimum=1, maximum=65535, integer=True)
        if number is not None:
            out["port"] = int(number)
    if "password" in section:
        copied = {"password": section["password"]}
        _prune_secret(copied, "password")
        if "password" in copied:
            out["password"] = str(copied["password"])
    if "session_hours" in section:
        number = v.number(section["session_hours"], "webui.session_hours", minimum=0.1)
        if number is not None:
            out["session_hours"] = float(number)
    return out


def validate_structured(payload: dict[str, Any]) -> dict[str, Any]:
    """校验表单提交，返回可合并进 YAML 的规范字典（只含已提交字段）。

    不合法时抛 :class:`ConfigValidationError`，附带全部字段级错误。
    """
    if not isinstance(payload, dict):
        raise ConfigValidationError([{"field": "config", "message": "应为 JSON 对象"}])
    v = _Validator()
    update: dict[str, Any] = {}

    llm = _section(payload, "llm", v)
    if llm is not None:
        update["llm"] = _validate_llm(llm, v)
    memory = _section(payload, "memory", v)
    if memory is not None:
        update["memory"] = _validate_memory(memory, v)
    search = _section(payload, "search", v)
    if search is not None:
        update["search"] = _validate_search(search, v)
    chat = _section(payload, "chat", v)
    if chat is not None:
        update["chat"] = _validate_chat(chat, v)
    decision = _section(payload, "decision", v)
    if decision is not None:
        update["decision"] = _validate_decision(decision, v)
    generation = _section(payload, "generation", v)
    if generation is not None:
        update["generation"] = _validate_generation(generation, v)
    reminder = _section(payload, "reminder", v)
    if reminder is not None:
        update["reminder"] = _validate_reminder(reminder, v)
    proactive = _section(payload, "proactive", v)
    if proactive is not None:
        update["proactive"] = _validate_proactive(proactive, v)
    plugins = _section(payload, "plugins", v)
    if plugins is not None:
        update["plugins"] = _validate_plugins(plugins, v)
    adapters = _section(payload, "adapters", v)
    if adapters is not None:
        update["adapters"] = _validate_adapters(adapters, v)
    webui = _section(payload, "webui", v)
    if webui is not None:
        update["webui"] = _validate_webui(webui, v)

    if "persona" in payload:
        persona = payload["persona"]
        if isinstance(persona, str):
            persona = {"path": persona}
        if not isinstance(persona, dict):
            v.fail("persona", "应为对象")
        elif "path" in persona:
            value = v.text(persona["path"], "persona.path")
            if value is not None:
                update["persona"] = {"path": value}
    if "database_path" in payload:
        value = v.text(payload["database_path"], "database_path")
        if value is not None:
            update["database_path"] = value
    if "background_check_minutes" in payload:
        number = v.number(
            payload["background_check_minutes"], "background_check_minutes", minimum=0.1
        )
        if number is not None:
            update["background_check_minutes"] = float(number)
    if "log_level" in payload:
        value = v.text(payload["log_level"], "log_level")
        if value is not None:
            level = value.upper()
            if level not in LOG_LEVELS:
                v.fail("log_level", f"只能是 {' / '.join(LOG_LEVELS)} 之一")
            else:
                update["log_level"] = level

    if v.errors:
        raise ConfigValidationError(v.errors)
    return update


# ----------------------------------------------------------------------
# 合并与写回
# ----------------------------------------------------------------------


def merge_config(raw: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """把规范字典合并进现有 YAML 数据，保留更新里没提到的键。"""
    base = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    _deep_merge(base, update, ())
    return base


def _deep_merge(dst: dict[str, Any], src: dict[str, Any], prefix: tuple[str, ...]) -> None:
    for key, value in src.items():
        path = prefix + (key,)
        if path in _REPLACE_PATHS:
            dst[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value, path)
        else:
            dst[key] = copy.deepcopy(value)


__all__ = [
    "ConfigValidationError",
    "LOG_LEVELS",
    "SECRET_PATHS",
    "merge_config",
    "structured_config",
    "validate_structured",
]
