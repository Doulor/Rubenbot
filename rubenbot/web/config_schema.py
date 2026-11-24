"""设置页的结构化配置：表单视图、逐字段校验、写回 YAML。

WebUI 用与 ``config.yaml`` 同构的嵌套字典编辑配置，这个模块负责三件事：

- :func:`structured_config`：生成表单初始值。密钥字段一律留空，另用
  ``secrets`` 里的布尔值告诉前端「已设置，留空不改」。
- :func:`validate_structured`：逐字段校验，把数字输入框传来的字符串转成
  数字，返回只含**已提交字段**的规范字典。不合法时抛
  :class:`ConfigValidationError`，里面带着 ``fields`` 列表，API 直接以 422
  返回，不会写文件。
- :func:`merge_config`：把规范字典合并进现有 YAML，保留未知键；
  ``llm.models`` 和 ``adapters`` 这类整体语义的段按表单为准替换/更新。

密钥字段（``llm.api_key``、``search.tavily.api_key`` / ``search.deepseek.api_key``、
``voice.stt_api_key`` / ``voice.tts_api_key``、``adapters.snowluma.access_token``、
``webui.password``）提交空值表示保留原值：校验阶段直接把这些键从更新里剔除，因此
脱敏占位符和空字符串都不会写回文件。
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from ..config import (
    COMPUTER_LIMITS, DEFAULT_MODEL_STRATEGY, MODEL_STRATEGIES, SEARCH_PROVIDERS,
    STT_PROVIDERS, TTS_MODES, TTS_PROVIDERS,
    ComputerConfig, Config, LLMConfig, MCPConfig, MCPServerConfig, PrivacyConfig,
    _expand_env, normalize_muted_senders,
)
from ..decision import parse_quiet_hours
from ..linkparse.settings import DELIVERIES, MODES

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MODEL_NAME_MAX_LEN = 120
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
    "voice.stt_api_key",
    "voice.tts_api_key",
    "adapters.snowluma.access_token",
    "stickers.embedding_api_key",
    "webui.password",
)

# 合并时整段替换、不与旧值递归合并的路径。
# 模型池与策略是「整张表」语义：表单提交的是完整视图，删掉的角色必须真的
# 从 YAML 里消失（空字典也替换），不能沿用旧值。
_REPLACE_PATHS = {
    ("llm", "models"),
    ("llm", "role_pools"),
    ("llm", "model_strategies"),
}


# ----------------------------------------------------------------------
# 设置页信息架构（纯展示层：不改字段路径、不改校验语义）
# ----------------------------------------------------------------------
#
# 「机器人」与「设置」各有二级分类；「模型」是独立页，沿用相同的字段归属规则。
#
# 归属按「前缀最长优先」解析：``chat`` 默认归聊天页，再给拆条 / 打字节奏
# 等「回复形态」字段单独标到表达页；前端只按这里的规则把表单控件分到页面，
# 字段路径与校验完全不变。人设页是 SOUL 编辑器、表情页暂无结构化字段，
# 两页不参与规则匹配，由分组里的 ``empty`` 说明现状。
SETTINGS_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "bot",
        "route": "bot",
        "title": "机器人",
        "sub": "bot 本体的行为参数：人设、聊天、表达、记忆、情绪、语音与表情",
        "pages": (
            {"id": "persona", "title": "人设", "sub": "编辑人格文件，保存即热生效"},
            {"id": "chat", "title": "聊天", "sub": "上下文、合批与开口护栏"},
            {"id": "expression", "title": "表达", "sub": "群聊查看概率、回复示例与长回复拆条"},
            {"id": "memory", "title": "记忆设置", "sub": "抽取、归纳、检索与滚动摘要"},
            {"id": "emotion", "title": "情绪", "sub": "好感度与心情规则"},
            {"id": "voice", "title": "语音", "sub": "语音转写（STT）与语音回复（TTS）"},
            {
                "id": "emoji",
                "title": "表情",
                "sub": "收集与回复后补发表情",
            },
            {"id": "tools", "title": "外部工具", "sub": "查看正在运行的 MCP 连接与工具数"},
        ),
    },
    {
        "id": "models",
        "route": "models",
        "title": "模型",
        "sub": "接口、模型池与各角色模型",
        "pages": ({"id": "models", "title": "模型"},),
    },
    {
        "id": "settings",
        "route": "settings",
        "title": "设置",
        "sub": "搜索、主动触达、适配器与运行环境",
        "pages": (
            {"id": "search", "title": "搜索", "sub": "web_search 的渠道顺序与密钥"},
            {"id": "computer", "title": "操作电脑", "sub": "真机授权、访客限额与最近操作记录"},
            {"id": "mcp", "title": "MCP 工具", "sub": "外部服务器、连接超时与工具调用限制"},
            {"id": "reminder", "title": "提醒与主动", "sub": "提醒工具与冷场开口"},
            {"id": "adapters", "title": "适配器", "sub": "Console 与 OneBot 连接参数"},
            {"id": "system", "title": "系统", "sub": "WebUI、插件、路径与运行参数"},
        ),
    },
)

# 字段路径前缀 → 二级页面 id；前端按「最长前缀」匹配，匹配不到就不渲染。
#
# 归位原则（展示层）：
# - 按配置段（YAML 顶层键）的语义归属，不按字段在字典里的物理位置切碎；
# - 只有「回复形态」是例外：拆条、打字节奏、动作描写剥离虽然物理上在
#   ``chat`` 段，但描述的是回复长什么样，统一归到「表达」页，其余 ``chat.*``
#   留在「聊天」页；``reply`` 段整段都是回复形态，同样归「表达」。新增字段时
#   先看语义，默认跟随所在段。
_SETTINGS_PAGE_RULES: tuple[tuple[str, str], ...] = (
    # ---- 机器人 ----
    # 表达：生成策略 + 回复形态（拆条 / 打字节奏 / 动作描写）
    ("reply", "expression"),
    ("attention", "expression"),
    ("chat.strip_action_parens", "expression"),
    # 聊天：上下文、入站去重、说话时机（合批与开口护栏）
    ("chat", "chat"),
    ("decision", "chat"),
    ("memory", "memory"),
    ("privacy", "memory"),
    ("emotion", "emotion"),
    ("voice", "voice"),
    ("stickers", "emoji"),
    ("link", "chat"),
    # persona 页是 SOUL 编辑器
    # ---- 模型 ----
    ("llm", "models"),
    # ---- 设置 ----
    ("search", "search"),
    ("mcp", "mcp"),
    ("computer", "computer"),
    ("reminder", "reminder"),
    ("proactive", "reminder"),
    ("adapters", "adapters"),
    # 网关与事件通道是适配器的能力开关，跟连接参数放同一页
    ("gateway", "adapters"),
    # 系统页兜底：运行环境与不属于 bot 本体的零散项
    ("webui", "system"),
    ("plugins", "system"),
    ("market", "system"),
    ("persona.examples_enabled", "expression"),
    ("persona.examples_path", "expression"),
    ("persona.examples_count", "expression"),
    ("persona.examples_position", "expression"),
    ("persona.path", "system"),
    ("database_path", "system"),
    ("background_check_minutes", "system"),
    ("log_level", "system"),
)


def config_layout() -> dict[str, Any]:
    """给前端的信息架构：一级分组、二级页面与字段归属规则。

    只描述「哪个字段显示在哪个页面」，不参与校验；返回的新字典可安全修改。
    """
    return {
        "groups": copy.deepcopy(list(SETTINGS_GROUPS)),
        "rules": [{"prefix": prefix, "page": page} for prefix, page in _SETTINGS_PAGE_RULES],
    }


def resolve_page(path: str) -> str | None:
    """字段路径归属的二级页面 id；没有规则覆盖时返回 ``None``。

    前缀最长优先：``chat.strip_action_parens`` 归表达页，其余 ``chat.*`` 归聊天页；
    ``reply.*`` 整段（拆条与段间停顿）也归表达页。
    """
    best: tuple[str, str] | None = None
    for prefix, page in _SETTINGS_PAGE_RULES:
        if path == prefix or path.startswith(prefix + "."):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, page)
    return best[1] if best else None


def field_paths(view: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    """把表单视图摊平成叶子路径；``secrets`` 只是密钥标记，不算配置字段。"""
    paths: list[str] = []
    for key, value in view.items():
        if not prefix and key == "secrets":
            continue
        path = ".".join(prefix + (key,))
        if isinstance(value, dict) and value:
            paths.extend(field_paths(value, prefix + (key,)))
        else:
            paths.append(path)
    return paths


def assign_pages(view: dict[str, Any]) -> dict[str, list[str]]:
    """按 :func:`resolve_page` 把每个字段分到页面；未归属的放进 ``""``。

    归类完整性测试用它检查「每个字段恰好归入一个页面、无遗漏无重复」。
    """
    result: dict[str, list[str]] = {}
    for path in field_paths(view):
        page = resolve_page(path) or ""
        result.setdefault(page, []).append(path)
    return result


class ConfigValidationError(ValueError):
    """结构化配置校验失败；``fields`` 是 [{field, message}, ...]。"""

    def __init__(self, fields: list[dict[str, str]]):
        self.fields = fields
        summary = "；".join(f"{item['field']}: {item['message']}" for item in fields[:6])
        super().__init__(summary or "配置校验失败")


# ----------------------------------------------------------------------
# 表单视图
# ----------------------------------------------------------------------


def mcp_form(config: MCPConfig) -> dict[str, Any]:
    """连接参数都可能藏有凭据，表单只回显身份；同名条目省略的参数留原值。"""
    return {
        "servers": [{"name": s.name, "transport": s.transport, "enabled": s.enabled} for s in config.servers],
        "connect_timeout": config.connect_timeout,
        "call_timeout": config.call_timeout,
        "max_result_bytes": config.max_result_bytes,
    }


# 新格式只有一份路由事实；不回显、不落盘给旧调用方用的投影。
LEGACY_LLM_ROUTE_KEYS = ("base_url", "api_key", "chat_model", "extract_model", "consolidate_model", "gate_model", "models")


def model_catalog_form(llm: LLMConfig) -> dict[str, Any]:
    """目录视图永远不带密钥，旧端点在此显示为 default。"""
    providers, pools = llm.catalog_view()
    for row in providers:
        row["has_api_key"] = bool(row.pop("api_key", ""))
    return {"providers": providers, "role_pools": pools}


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
    # 角色表统一成「角色 → 模型名或模型名列表」，没配的角色不出现
    models = llm.get("models")
    llm["models"] = dict(models) if isinstance(models, dict) else {}
    strategies = llm.get("model_strategies")
    llm["model_strategies"] = dict(strategies) if isinstance(strategies, dict) else {}
    if config.llm.providers is None:
        llm["api_key"] = ""
    else:
        for key in LEGACY_LLM_ROUTE_KEYS:
            llm.pop(key, None)
        api_key = ""
    llm.update(model_catalog_form(config.llm))
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
    voice = asdict(config.voice)
    stt_key = voice.pop("stt_api_key", "")
    tts_key = voice.pop("tts_api_key", "")
    access_token = snow.get("access_token", "")

    return {
        "llm": llm,
        "persona": {"path": config.persona_path, **config.persona_example_options()},
        "database_path": config.database_path,
        "memory": asdict(config.memory),
        "privacy": asdict(config.privacy),
        "search": search,
        "mcp": mcp_form(config.mcp),
        "computer": asdict(config.computer),
        "voice": {**voice, "stt_api_key": "", "tts_api_key": ""},
        "chat": asdict(config.chat),
        "reply": asdict(config.reply),
        "attention": asdict(config.attention),
        "decision": decision,
        "reminder": asdict(config.reminder),
        "proactive": {
            **asdict(config.proactive),
            "quiet_hours": list(config.proactive.quiet_hours),
        },
        "gateway": {
            **asdict(config.gateway),
            "enabled_conversations": list(config.gateway.enabled_conversations),
        },
        "emotion": asdict(config.emotion),
        "stickers": {
            "enabled": config.stickers.enabled,
            "collect": config.stickers.collect,
            "collect_photos": config.stickers.collect_photos,
            "send_probability": config.stickers.send_probability,
            "nonverbal_probability": config.stickers.nonverbal_probability,
            "cooldown_seconds": config.stickers.cooldown_seconds,
            "embedding_base_url": config.stickers.embedding_base_url,
            "embedding_model": config.stickers.embedding_model,
            "embedding_api_key": "",
            "embedding_threshold": config.stickers.embedding_threshold,
            "embedding_refresh_seconds": config.stickers.embedding_refresh_seconds,
            # 识图时序：触发图等描述的时限、字节重试与送视觉前的压缩
            "vision_wait_seconds": config.stickers.vision_wait_seconds,
            "vision_fetch_retries": config.stickers.vision_fetch_retries,
            "vision_fetch_backoff_seconds": config.stickers.vision_fetch_backoff_seconds,
            "vision_image_max_side": config.stickers.vision_image_max_side,
            "vision_image_quality": config.stickers.vision_image_quality,
        },
        "link": asdict(config.link),
        "plugins": {
            "dir": config.plugins.dir,
            "enabled": list(config.plugins.enabled),
            "command_prefix": config.plugins.command_prefix,
        },
        "market": {"index_url": config.market.index_url},
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
            "voice.stt_api_key": bool(stt_key),
            "voice.tts_api_key": bool(tts_key),
            "adapters.snowluma.access_token": bool(access_token),
            "stickers.embedding_api_key": bool(config.stickers.embedding_api_key),
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
        except (TypeError, ValueError, OverflowError):
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


def _validate_models(value: Any, v: _Validator) -> dict[str, Any] | None:
    """校验 ``llm.models`` 角色映射：值可以是模型名或模型池列表。

    - 角色名非空；空角色名直接忽略，不算错误；
    - 单个字符串照旧表示「这个角色用这个模型」；
    - 列表表示按优先级排的模型池，空列表、空模型名一律拒绝（报清楚哪个角色）；
    - 空串/None 表示这个角色未配（回退链照旧），不写进结果。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        v.fail("llm.models", "应为「角色 → 模型名或模型名列表」的对象")
        return None
    result: dict[str, Any] = {}
    for raw_role, raw_model in value.items():
        role = str(raw_role).strip()
        if not role:
            continue  # 空角色名直接忽略，不算错误
        if raw_model is None:
            continue
        field = f"llm.models.{role}"
        if isinstance(raw_model, str):
            # 单个字符串照旧：空串表示这个角色未配，不算错误
            model = raw_model.strip()
            if len(model) > MODEL_NAME_MAX_LEN:
                v.fail(field, f"模型名太长（最多 {MODEL_NAME_MAX_LEN} 字）")
                continue
            if model:
                result[role] = model
            continue
        if not isinstance(raw_model, (list, tuple)):
            v.fail(field, "必须填模型名、模型名列表或留空")
            continue
        models: list[str] = []
        for index, item in enumerate(raw_model):
            if not isinstance(item, str):
                v.fail(f"{field}.{index}", "模型名必须是文本")
                continue
            model = _model_name(item, f"{field}.{index}", v)
            if model and model not in models:
                models.append(model)
        if not models:
            # 空列表（或全是空串）会让这个角色「配了却没模型」，明确拒绝
            if not raw_model:
                v.fail(field, "模型池不能为空；不需要这个角色就直接删掉整项")
            continue
        result[role] = models[0] if len(models) == 1 else models
    return result


def _model_name(value: str, field: str, v: _Validator) -> str:
    """校验单个模型名：非空、不是纯空白、不长到离谱。"""
    model = str(value).strip()
    if not model:
        v.fail(field, "模型名不能为空；不需要这个角色就直接删掉整项")
        return ""
    if len(model) > MODEL_NAME_MAX_LEN:
        v.fail(field, f"模型名太长（最多 {MODEL_NAME_MAX_LEN} 字）")
        return ""
    return model


def _validate_strategy(value: Any, v: _Validator, field: str) -> str | None:
    """校验模型池策略名：只认 :data:`MODEL_STRATEGIES`。"""
    if not isinstance(value, str):
        v.fail(field, "策略必须是文本")
        return None
    strategy = value.strip().lower()
    if strategy not in MODEL_STRATEGIES:
        v.fail(field, f"未知策略 {value!r}；只能是 {' / '.join(MODEL_STRATEGIES)} 之一")
        return None
    return strategy


def _validate_llm(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "providers" in section:
        rows = section["providers"]
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            v.fail("llm.providers", "应为提供商对象列表")
        else:
            # has_api_key 只是表单提示，不是磁盘字段。缺省 api_key 与显式清空不同。
            out["providers"] = [{k: value for k, value in row.items() if k != "has_api_key"} for row in rows]
    if "role_pools" in section:
        if isinstance(section["role_pools"], dict):
            out["role_pools"] = section["role_pools"]
        else:
            v.fail("llm.role_pools", "应为「角色 → 模型引用列表」的对象")
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
    if "model_strategy" in section:
        value = section["model_strategy"]
        if value is None or (isinstance(value, str) and not value.strip()):
            # 清空表示回默认策略
            out["model_strategy"] = DEFAULT_MODEL_STRATEGY
        else:
            strategy = _validate_strategy(value, v, "llm.model_strategy")
            if strategy is not None:
                out["model_strategy"] = strategy
    if "model_strategies" in section:
        value = section["model_strategies"]
        if value is None:
            out["model_strategies"] = {}
        elif not isinstance(value, dict):
            v.fail("llm.model_strategies", "应为「角色 → 策略」的对象")
        else:
            strategies: dict[str, str] = {}
            for raw_role, raw_strategy in value.items():
                role = str(raw_role).strip()
                if not role:
                    continue
                if raw_strategy is None or (
                    isinstance(raw_strategy, str) and not raw_strategy.strip()
                ):
                    # 空值表示「跟随全局策略」，不写覆盖
                    continue
                strategy = _validate_strategy(
                    raw_strategy, v, f"llm.model_strategies.{role}"
                )
                if strategy is not None:
                    strategies[role] = strategy
            out["model_strategies"] = strategies
    if "temperature" in section:
        number = v.number(section["temperature"], "llm.temperature", minimum=0, maximum=2)
        if number is not None:
            out["temperature"] = float(number)
    if "chat_temperature" in section:
        value = section["chat_temperature"]
        if value in (None, ""):
            out["chat_temperature"] = None
        else:
            number = v.number(value, "llm.chat_temperature", minimum=0, maximum=2)
            if number is not None:
                out["chat_temperature"] = float(number)
    if "chat_frequency_penalty" in section:
        number = v.number(section["chat_frequency_penalty"], "llm.chat_frequency_penalty", minimum=-2, maximum=2)
        if number is not None:
            out["chat_frequency_penalty"] = float(number)
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
        "distill_new_threshold": 1,
        "distill_max_events": 1,
        "distill_max_facts": 1,
        "status_expiry_min_days": 1,
        "status_expiry_max_days": 1,
        "self_event_max_chars": 1,
        # 记忆 v2 读取侧
        "index_max_members": 1,
        "index_identity_chars": 8,
        "mentioned_max_people": 0,
        "profile_inject_max_chars": 120,
        "inject_budget_tokens": 0,
        "timeline_hit_limit": 1,
        "timeline_max_chars": 0,
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
        "distill_interval_hours": (0.1, None),
        "timeline_compress_after_days": (0.1, None),
        "timeline_prune_after_days": (0.1, None),
        "timeline_prune_keep_salience": (0, 1),
        # 记忆 v2 读取侧
        "index_active_days": (0.1, None),
        "index_ttl_seconds": (0, None),
        "cross_session_hours": (0.1, None),
        "timeline_recent_hours": (0, None),
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
    group_profile = _section(section, "group_profile", v)
    if group_profile is not None:
        out["group_profile"] = _validate_group_profile(group_profile, v)
    return out


def _validate_group_profile(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """群定位的嵌套配置：自归纳开关、刷新频率与长度上限。"""
    out: dict[str, Any] = {}
    if "enabled" in section:
        flag = v.boolean(section["enabled"], "memory.group_profile.enabled")
        if flag is not None:
            out["enabled"] = flag
    for key, minimum in (
        ("min_messages", 1),
        ("max_chars", 50),
        ("max_batch_messages", 1),
    ):
        if key in section:
            number = v.number(
                section[key], f"memory.group_profile.{key}", minimum=minimum, integer=True
            )
            if number is not None:
                out[key] = int(number)
    if "update_interval_hours" in section:
        number = v.number(
            section["update_interval_hours"],
            "memory.group_profile.update_interval_hours",
            minimum=0.1,
        )
        if number is not None:
            out["update_interval_hours"] = float(number)
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


def _validate_mcp(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out = {}
    for key in ("connect_timeout", "call_timeout", "max_result_bytes"):
        if key in section:
            size = key == "max_result_bytes"
            out[key] = v.number(section[key], f"mcp.{key}", integer=size, minimum=32 if size else 0)
            if out[key] == 0:
                v.fail(f"mcp.{key}", "必须大于零")
    if "servers" in section:
        servers = section["servers"]
        if not isinstance(servers, list):
            v.fail("mcp.servers", "应为服务器对象列表")
            return out
        names = set()
        for index, row in enumerate(servers):
            try:
                # 仅校验提交的字段；缺省连接参数可能需要从磁盘保留，合并后再查必填项。
                item = MCPServerConfig.parse(row, partial=True)
                if set(row) - MCPServerConfig.__dataclass_fields__.keys():
                    raise ValueError("服务器配置包含未知字段")
                if item.name in names:
                    raise ValueError("name 重复")
                names.add(item.name)
            except ValueError as exc:
                v.fail(f"mcp.servers[{index}]", str(exc))
        out["servers"] = servers
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


def _validate_voice(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """语音双向：STT / TTS 的后端、密钥与上限；密钥留空表示保留原值。"""
    out: dict[str, Any] = {}
    provider_fields = (
        ("stt_provider", STT_PROVIDERS, "stt_provider"),
        ("tts_provider", TTS_PROVIDERS, "tts_provider"),
    )
    for key, allowed, field in provider_fields:
        if key in section:
            value = v.text(section[key], f"voice.{field}", max_len=40)
            if value is not None:
                provider = value.lower()
                if provider not in allowed:
                    v.fail(f"voice.{field}", f"只能是 {' / '.join(allowed)} 之一")
                else:
                    out[key] = provider
    if "tts_mode" in section:
        value = v.text(section["tts_mode"], "voice.tts_mode", max_len=20)
        if value is not None:
            mode = value.lower()
            if mode not in TTS_MODES:
                v.fail("voice.tts_mode", f"只能是 {' / '.join(TTS_MODES)} 之一")
            else:
                out["tts_mode"] = mode
    for key in ("stt_api_key", "tts_api_key"):
        if key in section:
            copied = {key: section[key]}
            _prune_secret(copied, key)
            if key in copied:
                out[key] = str(copied[key])
    text_fields = {
        "stt_base_url": 400,
        "stt_model": 120,
        "tts_base_url": 400,
        "tts_model": 120,
        "tts_voice": 120,
        "tts_siliconflow_voice": 120,
    }
    for key, max_len in text_fields.items():
        if key in section:
            value = v.text(section[key], f"voice.{key}", allow_empty=True, max_len=max_len)
            if value is not None:
                out[key] = value
    float_fields = {
        "stt_timeout_seconds": 0.1,
        "tts_timeout_seconds": 0.1,
        "stt_max_seconds": 0,
    }
    for key, minimum in float_fields.items():
        if key in section:
            number = v.number(section[key], f"voice.{key}", minimum=minimum)
            if number is not None:
                out[key] = float(number)
    for key, minimum in (("stt_max_bytes", 0), ("tts_max_chars", 1)):
        if key in section:
            number = v.number(section[key], f"voice.{key}", minimum=minimum, integer=True)
            if number is not None:
                out[key] = int(number)
    return out


def _validate_chat(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "bot_name" in section:
        value = v.text(section["bot_name"], "chat.bot_name", max_len=60)
        if value is not None:
            out["bot_name"] = value
    for key, minimum in (("history_size", 1),):
        if key in section:
            number = v.number(section[key], f"chat.{key}", minimum=minimum, integer=True)
            if number is not None:
                out[key] = int(number)
    if "strip_action_parens" in section:
        flag = v.boolean(section["strip_action_parens"], "chat.strip_action_parens")
        if flag is not None:
            out["strip_action_parens"] = flag
    for key in ("history_ttl_hours", "dedupe_ttl_seconds"):
        if key in section:
            number = v.number(section[key], f"chat.{key}", minimum=0)
            if number is not None:
                out[key] = float(number)
    return out


def _validate_attention(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out = {}
    for key, maximum in (("talk_value", 1), ("engaged_window_seconds", None)):
        if key in section:
            value = v.number(section[key], f"attention.{key}", minimum=0, maximum=maximum)
            if value is not None:
                out[key] = value
    return out


def _validate_reply(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """回复形态：最多几段、段间延迟开关与每字毫秒数/上下限。"""
    out: dict[str, Any] = {}
    if "max_segments" in section:
        number = v.number(
            section["max_segments"], "reply.max_segments", minimum=1, maximum=10, integer=True
        )
        if number is not None:
            out["max_segments"] = int(number)
    if "segment_delay_enabled" in section:
        flag = v.boolean(section["segment_delay_enabled"], "reply.segment_delay_enabled")
        if flag is not None:
            out["segment_delay_enabled"] = flag
    for key, maximum in (
        ("segment_delay_per_char_ms", 5000),
        ("segment_delay_min_ms", 60000),
        ("segment_delay_max_ms", 60000),
    ):
        if key in section:
            number = v.number(section[key], f"reply.{key}", minimum=0, maximum=maximum)
            if number is not None:
                out[key] = float(number)
    # 上限不能低于下限：表单两个框独立提交，这里统一兜住
    low = out.get("segment_delay_min_ms")
    high = out.get("segment_delay_max_ms")
    if low is not None and high is not None and high < low:
        v.fail("reply.segment_delay_max_ms", "不能小于最小停顿")
    return out


# emotion 段的数值字段 → (下限, 上限)；全都是可调的规则幅度
_EMOTION_NUMBERS: dict[str, tuple[float | None, float | None]] = {
    "affinity_baseline": (0, 100),
    "affinity_min": (0, 100),
    "affinity_max": (0, 100),
    "chat_delta": (0, 10),
    "chat_daily_cap": (0, 100),
    "praise_delta": (-50, 50),
    "insult_delta": (-50, 50),
    "decay_weekly": (0, 1),
    "praise_intensity": (0, 1),
    "insult_intensity": (0, 1),
    "excitement_intensity": (0, 1),
    "excitement_window_seconds": (0, None),
    "boredom_intensity": (0, 1),
    "boredom_after_seconds": (0, None),
    "half_life_seconds": (1, None),
    "min_intensity": (0, 1),
}


def _validate_emotion(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """校验情绪与好感度的开关和规则幅度。"""
    out: dict[str, Any] = {}
    if "enabled" in section:
        flag = v.boolean(section["enabled"], "emotion.enabled")
        if flag is not None:
            out["enabled"] = flag
    for key in ("praise_mood", "insult_mood", "excitement_mood", "boredom_mood"):
        if key in section:
            value = v.text(section[key], f"emotion.{key}", max_len=10)
            if value is not None:
                out[key] = value
    for key, (minimum, maximum) in _EMOTION_NUMBERS.items():
        if key not in section:
            continue
        number = v.number(section[key], f"emotion.{key}", minimum=minimum, maximum=maximum)
        if number is not None:
            out[key] = float(number)
    for key, field in (
        ("excitement_min_messages", "emotion.excitement_min_messages"),
        ("excitement_min_bot_messages", "emotion.excitement_min_bot_messages"),
    ):
        if key in section:
            number = v.number(section[key], field, minimum=0, integer=True)
            if number is not None:
                out[key] = int(number)
    return out


def _validate_decision(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    number_fields = {
        "merge_window_seconds": (0, None),
        "hard_merge_window_seconds": (0, None),
        "batch_max_seconds": (0, None),
        "urgent_seconds": (0, None),
        "min_reply_interval_seconds": (0, None),
        "refractory_seconds": (0, None),
        "proactive_window_seconds": (0, None),
        "fatigue_window_seconds": (0, None),
        "fatigue_cooldown_seconds": (0, None),
    }
    for key, (minimum, maximum) in number_fields.items():
        if key in section:
            number = v.number(section[key], f"decision.{key}", minimum=minimum, maximum=maximum)
            if number is not None:
                out[key] = float(number)
    for key in ("refractory_enabled", "proactive_window_enabled"):
        if key in section:
            value = v.boolean(section[key], f"decision.{key}")
            if value is not None:
                out[key] = value
    for key in ("proactive_window_limit", "fatigue_reply_limit"):
        if key in section:
            number = v.number(section[key], f"decision.{key}", minimum=0, integer=True)
            if number is not None:
                out[key] = int(number)
    if "aliases" in section:
        aliases = v.string_list(section["aliases"], "decision.aliases")
        if aliases is not None:
            out["aliases"] = aliases
    if "muted_senders" in section:
        try:
            out["muted_senders"] = normalize_muted_senders(section["muted_senders"])
        except ValueError as exc:
            v.fail("decision.muted_senders", str(exc))
    if "veto_marker" in section:
        value = v.text(section["veto_marker"], "decision.veto_marker", max_len=40)
        if value is not None:
            out["veto_marker"] = value
    return out


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


def _validate_stickers(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("enabled", "collect", "collect_photos"):
        if key in section:
            value = v.boolean(section[key], f"stickers.{key}")
            if value is not None:
                out[key] = value
    for key in ("send_probability", "nonverbal_probability"):
        if key in section:
            value = v.number(section[key], f"stickers.{key}", minimum=0, maximum=1)
            if value is not None:
                out[key] = float(value)
    if "cooldown_seconds" in section:
        value = v.number(section["cooldown_seconds"], "stickers.cooldown_seconds", minimum=0)
        if value is not None:
            out["cooldown_seconds"] = float(value)
    # 语义检索：留空即不启用（走 fast 角色文本选择 + 标签回落）
    for key in ("embedding_base_url", "embedding_model"):
        if key in section:
            value = v.text(section[key], f"stickers.{key}", allow_empty=True)
            if value is not None:
                out[key] = value
    if "embedding_api_key" in section:
        # 与其他密钥字段一致：空值 / 脱敏占位符表示保留原值
        copied = {"embedding_api_key": section["embedding_api_key"]}
        _prune_secret(copied, "embedding_api_key")
        if "embedding_api_key" in copied:
            out["embedding_api_key"] = str(copied["embedding_api_key"]).strip()
    if "embedding_threshold" in section:
        value = v.number(section["embedding_threshold"], "stickers.embedding_threshold", minimum=-1, maximum=1)
        if value is not None:
            out["embedding_threshold"] = float(value)
    if "embedding_refresh_seconds" in section:
        value = v.number(section["embedding_refresh_seconds"], "stickers.embedding_refresh_seconds", minimum=0)
        if value is not None:
            out["embedding_refresh_seconds"] = float(value)
    # 识图时序：等待上限、拉取重试与压缩参数
    for key, minimum, maximum in (("vision_wait_seconds", 0, 120),
                                  ("vision_fetch_backoff_seconds", 0, 5)):
        if key in section:
            value = v.number(section[key], f"stickers.{key}", minimum=minimum, maximum=maximum)
            if value is not None:
                out[key] = float(value)
    for key, minimum, maximum in (("vision_fetch_retries", 0, 3),
                                  ("vision_image_max_side", 0, 4096),
                                  ("vision_image_quality", 30, 95)):
        if key in section:
            value = v.number(section[key], f"stickers.{key}", integer=True, minimum=minimum, maximum=maximum)
            if value is not None:
                out[key] = int(value)
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


def _validate_market(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "index_url" in section:
        # 留空就是「用内置官方索引」，所以允许空字符串
        value = v.text(
            section["index_url"], "market.index_url", allow_empty=True, max_len=500
        )
        if value is not None:
            if value and not value.lower().startswith(("http://", "https://")):
                v.fail("market.index_url", "索引地址必须是以 http:// 或 https:// 开头的 URL")
            else:
                out["index_url"] = value
    return out


def _validate_gateway(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """网关与事件通道：开关、会话白名单、安静时段与额度。"""
    out: dict[str, Any] = {}
    for key in ("enabled", "judge_events", "allow_sensitive"):
        if key in section:
            value = v.boolean(section[key], f"gateway.{key}")
            if value is not None:
                out[key] = value
    if "enabled_conversations" in section:
        value = v.string_list(section["enabled_conversations"], "gateway.enabled_conversations")
        if value is not None:
            out["enabled_conversations"] = value
    if "quiet_hours" in section:
        value = v.text(section["quiet_hours"], "gateway.quiet_hours", allow_empty=True, max_len=120)
        if value is not None:
            if value and parse_quiet_hours([value]) == []:
                v.fail("gateway.quiet_hours", "格式应为 HH:MM-HH:MM，留空表示不设安静时段")
            else:
                out["quiet_hours"] = value
    for key in ("daily_budget_per_conversation", "global_daily_cap"):
        if key in section:
            number = v.number(section[key], f"gateway.{key}", minimum=0, integer=True)
            if number is not None:
                out[key] = int(number)
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


def _validate_link(section: dict[str, Any], v: _Validator) -> dict[str, Any]:
    """链接理解（core 感知层）：模式 / 投递 / 限额都从表单可改。"""
    out: dict[str, Any] = {}
    if "enabled" in section:
        value = v.boolean(section["enabled"], "link.enabled")
        if value is not None:
            out["enabled"] = value
    for key, allowed in (("mode", MODES), ("delivery", DELIVERIES)):
        if key in section:
            value = str(section[key] or "").strip()
            if value not in allowed:
                v.fail(f"link.{key}", f"只能是 {' / '.join(allowed)} 之一")
            else:
                out[key] = value
    for key, minimum, maximum in (("cooldown_seconds", 0, 86400),
                                  ("cache_ttl_seconds", 0, 604800),
                                  ("timeout_seconds", 0.1, 30)):
        if key in section:
            value = v.number(section[key], f"link.{key}", minimum=minimum, maximum=maximum)
            if value is not None:
                out[key] = float(value)
    for key, minimum, maximum in (("daily_limit", 0, 100000),
                                  ("max_bytes", 1, 4 * 1024 * 1024),
                                  ("max_links", 1, 10),
                                  ("max_body_chars", 200, 5000)):
        if key in section:
            value = v.number(section[key], f"link.{key}", integer=True, minimum=minimum, maximum=maximum)
            if value is not None:
                out[key] = int(value)
    if "fallback_tool" in section:
        value = v.text(section["fallback_tool"], "link.fallback_tool", allow_empty=True, max_len=120)
        if value is not None:
            out["fallback_tool"] = value
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
    privacy = _section(payload, "privacy", v)
    if privacy is not None and "enabled" in privacy:
        try:
            update["privacy"] = asdict(PrivacyConfig.parse(privacy))
        except ValueError:
            v.fail("privacy.enabled", "必须是开关（true/false）")
    search = _section(payload, "search", v)
    if search is not None:
        update["search"] = _validate_search(search, v)
    if "computer" in payload:
        section = copy.copy(payload["computer"])
        # 表单的数字输入是文本；只有数值字段可作此转换，授权字段仍严格校验。
        if isinstance(section, dict):
            for key in COMPUTER_LIMITS:
                if isinstance(section.get(key), str):
                    try:
                        section[key] = int(section[key])
                    except ValueError:
                        pass  # 由下方统一报字段错误
        try:
            ComputerConfig.parse(section)
        except ValueError as exc:
            field, _, message = str(exc).partition(": ")
            v.fail(field, message)
        else:
            update["computer"] = section
    mcp = _section(payload, "mcp", v)
    if mcp is not None:
        update["mcp"] = _validate_mcp(mcp, v)
    voice = _section(payload, "voice", v)
    if voice is not None:
        update["voice"] = _validate_voice(voice, v)
    chat = _section(payload, "chat", v)
    if chat is not None:
        update["chat"] = _validate_chat(chat, v)
    attention = _section(payload, "attention", v)
    if attention is not None:
        update["attention"] = _validate_attention(attention, v)
    reply = _section(payload, "reply", v)
    if reply is not None:
        update["reply"] = _validate_reply(reply, v)
    decision = _section(payload, "decision", v)
    if decision is not None:
        update["decision"] = _validate_decision(decision, v)
    reminder = _section(payload, "reminder", v)
    if reminder is not None:
        update["reminder"] = _validate_reminder(reminder, v)
    proactive = _section(payload, "proactive", v)
    if proactive is not None:
        update["proactive"] = _validate_proactive(proactive, v)
    gateway = _section(payload, "gateway", v)
    if gateway is not None:
        update["gateway"] = _validate_gateway(gateway, v)
    emotion = _section(payload, "emotion", v)
    if emotion is not None:
        update["emotion"] = _validate_emotion(emotion, v)
    stickers = _section(payload, "stickers", v)
    if stickers is not None:
        update["stickers"] = _validate_stickers(stickers, v)
    link = _section(payload, "link", v)
    if link is not None:
        update["link"] = _validate_link(link, v)
    plugins = _section(payload, "plugins", v)
    if plugins is not None:
        update["plugins"] = _validate_plugins(plugins, v)
    market = _section(payload, "market", v)
    if market is not None:
        update["market"] = _validate_market(market, v)
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
        else:
            options = {}
            for key in ("path", "examples_path"):
                if key in persona:
                    options[key] = v.text(persona[key], f"persona.{key}")
            if "examples_enabled" in persona:
                options["examples_enabled"] = v.boolean(persona["examples_enabled"], "persona.examples_enabled")
            if "examples_count" in persona:
                options["examples_count"] = v.number(
                    persona["examples_count"], "persona.examples_count", integer=True, minimum=0
                )
            if "examples_position" in persona:
                position = persona["examples_position"]
                if position not in ("before", "after"):
                    v.fail("persona.examples_position", "只能是 before 或 after")
                else:
                    options["examples_position"] = position
            if options:
                update["persona"] = options
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


def _merge_llm_catalog(previous: dict, update: dict, merged: dict) -> None:
    """迁移或替换目录。仅同一身份、同一端点可保留磁盘上的凭据原文。"""
    new_format = "providers" in previous or "role_pools" in previous
    if not ({"providers", "role_pools"} & update.keys()):
        if new_format and any(key in update for key in LEGACY_LLM_ROUTE_KEYS):
            raise ConfigValidationError([{"field": "llm", "message": "当前使用提供商与角色池，不能写入旧模型字段"}])
        return

    previous_llm = Config.from_raw({"llm": previous}).llm
    previous_view = model_catalog_form(previous_llm)
    if not new_format and "models" in update:
        # 老式全量设置表单也可携带只读转换视图。仅改了旧字段或其它段时，
        # 不让未触碰的目录覆盖那些编辑或悄悄迁移磁盘格式。
        public_rows = [{k: v for k, v in row.items() if k != "has_api_key"} for row in previous_view["providers"]]
        if update.get("providers") == public_rows and update.get("role_pools") == previous_view["role_pools"]:
            merged.pop("providers", None)
            merged.pop("role_pools", None)
            return
    if new_format:
        old_rows = previous.get("providers", [])
    else:
        old_rows = [{"name": "default", "base_url": previous.get("base_url", LLMConfig.base_url), "api_key": previous.get("api_key", "")}]
        merged.setdefault("providers", [{**previous_view["providers"][0], **old_rows[0]}])
        merged.setdefault("role_pools", previous_view["role_pools"])

    # 名称是去除两端空白的字面标识（绝不展开）；只有地址会展开环境变量。
    by_name = {row["name"].strip(): row for row in old_rows}
    for row in merged["providers"]:
        # 格式错误由 Config.from_raw 统一报错；这里不处理畸形标识。
        if not isinstance(row.get("name"), str) or not isinstance(row.get("base_url"), str):
            continue
        old = by_name.get(row["name"].strip())
        # 地址引用本身变了也可能在未来指向别处；不向新变量继承旧凭据。
        url = row["base_url"].strip()
        if old and url in (old["base_url"].strip(), _expand_env(old["base_url"]).strip()):
            row["base_url"] = old["base_url"]
            if "api_key" in old:
                row.setdefault("api_key", old["api_key"])
        row.pop("has_api_key", None)
    for key in LEGACY_LLM_ROUTE_KEYS:
        merged.pop(key, None)


def merge_config(raw: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """把规范字典合并进现有 YAML 数据，保留更新里没提到的键。"""
    base = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if isinstance(base.get("persona"), str) and isinstance(update.get("persona"), dict):
        # 旧格式只写人格路径；仅修改示例设置时也不能丢掉它。
        base["persona"] = {"path": base["persona"]}
    # 数组按本次提交增删，只对同名且同传输的条目保留未回显的连接参数。
    # 从磁盘原文取值，绝不把展开的 ${ENV} 重新写成明文。
    _deep_merge(base, update, ())
    if "llm" in update:
        _merge_llm_catalog(raw.get("llm") or {}, update["llm"], base["llm"])
    if "servers" in update.get("mcp", {}):
        previous = raw.get("mcp")
        rows = previous.get("servers", []) if isinstance(previous, dict) else []
        by_name = {
            row["name"]: row for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        } if isinstance(rows, list) else {}
        for index, row in enumerate(base["mcp"]["servers"]):
            old = by_name.get(row["name"], {})
            if old.get("transport", "stdio") == row.get("transport", "stdio"):
                row = {**old, **row}
            try:
                MCPServerConfig.parse(row, expand_env=True)
            except ValueError as exc:
                raise ConfigValidationError([{"field": f"mcp.servers[{index}]", "message": str(exc)}]) from exc
            base["mcp"]["servers"][index] = row
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
    "assign_pages",
    "config_layout",
    "field_paths",
    "merge_config",
    "resolve_page",
    "structured_config",
    "validate_structured",
]
