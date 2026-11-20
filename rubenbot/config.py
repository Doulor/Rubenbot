"""配置加载：读取单个 YAML，并给出合理默认值。

配置里支持 ``${ENV_VAR}`` 形式的环境变量展开，方便把 api_key 放在环境变量里。
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """递归展开字符串里的 ${VAR}；未定义时替换为空串。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


DEFAULT_CHAT_MODEL = "gpt-4o-mini"

# 预置角色及其回退链：自身没配模型时沿链找，最终都落到 chat。
# 未知角色（插件/新代码自定义的 task）同样回退 chat，不报错。
ROLE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "chat": (),
    "extract": ("chat",),
    "consolidate": ("chat",),
    "fast": ("chat",),
    "summary": ("chat",),
    "vision": ("chat",),
    # 记忆 v2：分寸审查没配就跟随 chat；画像提炼沿用归纳模型
    "gate": ("chat",),
    "distill": ("consolidate", "chat"),
}

# 预置角色表；`models` 里写别的角色名也允许，只是回退链只有 chat
MODEL_ROLES: tuple[str, ...] = tuple(ROLE_FALLBACKS)


@dataclass
class LLMConfig:
    """OpenAI 兼容接口配置。

    角色表有两代写法，同时兼容：

    - ``models``：可扩展映射 ``{角色: 模型名}``，例如 ``fast: 小模型``；
    - ``chat_model`` / ``extract_model`` / ``consolidate_model``：旧字段。

    解析顺序是「精确角色 → 回退链 → chat → 默认模型」；``models`` 优先于
    旧字段。旧配置不写 ``models`` 时行为与以前完全一致。
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    chat_model: str = DEFAULT_CHAT_MODEL
    extract_model: str | None = None
    consolidate_model: str | None = None
    # 分寸审查（gate）用的模型；留空跟随 chat
    gate_model: str | None = None
    temperature: float = 0.8
    timeout: float = 60.0
    max_retries: int = 2
    # 角色 → 模型名。预置角色见 MODEL_ROLES，也可写自定义角色。
    # 放在末尾是为了不改变旧字段的位置参数顺序。
    models: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把 ``models`` 归一化成「非空字符串 → 字符串」的字典。"""
        raw = self.models if isinstance(self.models, dict) else {}
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            name = str(key).strip()
            if not name or value is None:
                continue
            model = str(value).strip()
            if model:
                cleaned[name] = model
        self.models = cleaned

    def _explicit_model(self, role: str) -> str | None:
        """只看这个角色自己配没配，不沿着回退链走。"""
        mapped = self.models.get(role)
        if mapped:
            return mapped
        # 旧字段：保持完全兼容
        if role == "extract":
            return self.extract_model or None
        if role == "consolidate":
            return self.consolidate_model or None
        if role == "gate":
            return self.gate_model or None
        if role == "chat":
            return self.chat_model or None
        return None

    def _resolve_model(self, role: str, seen: set[str]) -> str | None:
        if role in seen:
            return None
        seen.add(role)
        explicit = self._explicit_model(role)
        if explicit:
            return explicit
        # 预置角色按表回退；未知角色一律回退 chat
        for fallback in ROLE_FALLBACKS.get(role, ("chat",)):
            resolved = self._resolve_model(fallback, seen)
            if resolved:
                return resolved
        return None

    def model_for(self, role: str = "chat") -> str:
        """按用途取模型：精确角色 → 回退链 → chat → 默认模型。

        记忆 v2 的 ``gate`` 走 ``gate_model``；``distill`` 未配置时沿用
        ``consolidate`` 的回退链（归纳模型 → chat）。
        """
        name = str(role or "chat").strip() or "chat"
        return self._resolve_model(name, set()) or self.chat_model or DEFAULT_CHAT_MODEL


@dataclass
class RollingSummaryConfig:
    """会话滚动摘要参数（按会话压缩早期对话历史）。

    - ``enabled``：总开关；关闭时既不生成摘要，也不用摘要拼上下文。
    - ``trigger_after``：covered_until 之后未摘要的原文消息达到多少条触发一次压缩。
    - ``keep_recent``：始终保留在上下文里的最新原文条数，不参与压缩。
    - ``max_chars``：摘要总长上限；更新时要求模型在上限内取舍，代码兜底截断。
    """

    enabled: bool = True
    trigger_after: int = 60
    keep_recent: int = 30
    max_chars: int = 800


@dataclass
class MemoryConfig:
    """记忆层参数。"""

    extract_every_n: int = 30
    extract_silence_seconds: int = 300
    extract_min_pending: int = 4
    extract_window: int = 80
    inject_limit: int = 20
    dedupe_threshold: float = 0.8
    consolidate_new_threshold: int = 50
    consolidate_interval_hours: float = 24.0
    consolidate_max_memories: int = 150
    # 每轮归纳最多移除的比例，以及至少允许的条数
    consolidate_remove_ratio: float = 0.2
    consolidate_remove_min: int = 4
    raw_keep_per_chat: int = 500

    # ---- 记忆 v2（画像 / 时间线 / 自我）----
    # 画像提炼：新增事件+事实达到阈值、或距上次提炼超过间隔时触发
    distill_new_threshold: int = 20
    distill_interval_hours: float = 24.0
    distill_max_events: int = 120
    distill_max_facts: int = 80
    # 近况（status）过期时间由模型估计天数，代码夹到这个区间
    status_expiry_min_days: int = 1
    status_expiry_max_days: int = 90
    # 时间线压缩：7 天前按人按周压成 digest；90 天前只留高显著度
    timeline_compress_after_days: float = 7.0
    timeline_prune_after_days: float = 90.0
    timeline_prune_keep_salience: float = 0.7
    # bot 自己发的每条消息记进自我时间线的截断长度
    self_event_max_chars: int = 120

    # ---- 记忆 v2 读取侧（索引 / 预拉取 / 跨会话 / 分寸审查）----
    # 群成员索引：只收近 N 天在本群活跃过的人，最多多少人，缓存多久失效
    index_active_days: float = 30.0
    index_max_members: int = 40
    index_ttl_seconds: float = 600.0
    # 索引里「一句话身份」的截断长度
    index_identity_chars: int = 40
    # 一轮里最多拉几个被提及者的核心画像
    mentioned_max_people: int = 3
    # 说话人核心画像的最大字符数（画像核心不参与预算裁剪）
    profile_inject_max_chars: int = 600
    # 本轮记忆注入的总 token 预算；超了先砍事实、再砍时间线，画像核心不动
    inject_budget_tokens: int = 1200
    # 每个被提及者最多带几条相关时间线片段
    timeline_hit_limit: int = 6
    # 私聊跨会话摘要回看的小时数
    cross_session_hours: float = 24.0
    # 自我注入：回看本会话里 bot 自己最近说过/做过的事
    self_inject_hours: float = 24.0
    self_inject_limit: int = 3
    # 分寸审查：模型裁定的置信度阈值，低于它不放行
    gate_confidence_threshold: float = 0.6
    # 高敏感跨场合项是否顺带进待问队列
    gate_ask_high_sensitivity: bool = True
    # recall 工具循环（模型支持 OpenAI 工具调用时生效）
    recall_tool_enabled: bool = True
    recall_tool_max_rounds: int = 2

    # ---- 会话滚动摘要（只压缩本会话的对话历史，与跨会话记忆无关）----
    rolling_summary: RollingSummaryConfig = field(default_factory=RollingSummaryConfig)


@dataclass
class ChatConfig:
    """对话与发送参数。"""

    history_size: int = 30
    split_max_parts: int = 3
    split_part_chars: int = 60
    # 打字节奏：中文字 0.3s/字、其他字符 0.15s/字，每条另加 0.3s 回车时间；
    # type_speed 是全局倍率（0 表示两条之间不停顿），单条上限防止长文等太久
    type_speed: float = 1.0
    type_delay_max_seconds: float = 4.0
    # 发送前是否移除括号里的动作描写（如「（笑）」）；默认关闭
    strip_action_parens: bool = False
    bot_name: str = "Ruben"
    history_ttl_hours: float = 12.0
    # 入站去重：同一平台消息 id 在这个时间窗内重复到达则丢弃；0 表示不去重
    dedupe_ttl_seconds: float = 300.0


@dataclass
class GroupDecisionConfig:
    """单个群的决策覆盖项；未写的字段沿用全局默认。"""

    activity_multiplier: float = 1.0
    base: float | None = None
    quiet_hours: list[str] = field(default_factory=list)


@dataclass
class DecisionConfig:
    """回复意愿系统参数（见 SPEC「回复决策」）。"""

    # 消息合并窗口：普通消息攒 4 秒；硬触发走短窗口，0 表示立即处理
    merge_window_seconds: float = 4.0
    hard_merge_window_seconds: float = 1.0
    # 整批上限：从首条消息算起最多等这么久就放出；0 表示不设上限
    batch_max_seconds: float = 10.0
    # @/回复 bot 的批次截止时间不晚于「首条 + urgent_seconds」；0 表示不启用
    urgent_seconds: float = 2.0
    # 叫到这些名字/别名视为硬触发；为空时使用 chat.bot_name
    aliases: list[str] = field(default_factory=list)

    # 意愿评分：基础值 + 动量 + 相关性 - 占比 - 冷却
    base: float = 0.10
    threshold: float = 0.85
    threshold_noise: float = 0.10
    threshold_noise_band: float = 0.20
    momentum_bonus: float = 1.00
    momentum_half_life_seconds: float = 120.0
    relevance_bonus: float = 0.60
    relevance_scale: float = 8.0
    # 发言占比：看最近 ratio_window 条消息（含 bot 自己的）
    ratio_window: int = 30
    ratio_threshold: float = 0.20
    ratio_silence: float = 0.60
    ratio_penalty: float = 1.50
    # 发言后冷却
    cooldown_seconds: float = 30.0
    cooldown_penalty: float = 0.60
    # 模型输出该标记时静默丢弃
    veto_marker: str = "[不回复]"
    # 每群覆盖：群 id -> 配置
    groups: dict[str, GroupDecisionConfig] = field(default_factory=dict)


@dataclass
class SearchConfig:
    """联网搜索（LLM 工具 ``web_search``）的配置。

    ``provider`` 目前支持 ``bocha``（博查）与 ``tavily``，写错回落 ``bocha``；
    ``api_key`` 与 ``llm.api_key`` 一样支持 ``${ENV_VAR}``（加载时已统一展开）。
    没配 ``api_key`` 时工具不注册，模型看不见它。
    """

    provider: str = "bocha"
    api_key: str = ""
    timeout_seconds: float = 10.0
    # 每个会话两次真实搜索之间的冷却；冷却内直接返回提示文本，不发请求
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        provider = str(self.provider or "").strip().lower()
        self.provider = provider if provider in SEARCH_PROVIDERS else "bocha"
        self.api_key = str(self.api_key or "").strip()
        self.timeout_seconds = max(0.1, _as_float(self.timeout_seconds, 10.0))
        self.cooldown_seconds = max(0.0, _as_float(self.cooldown_seconds, 30.0))

    @property
    def configured(self) -> bool:
        """填了 api_key 才算可用（provider 不需要额外校验）。"""
        return bool(self.api_key)


SEARCH_PROVIDERS: tuple[str, ...] = ("bocha", "tavily")


@dataclass
class PluginsConfig:
    """插件目录与启停。

    插件采用**显式启用**制：``enabled`` 里没列出的插件都不会加载，
    未配置或空列表表示一个插件都不启用（默认全关）。WebUI 的插件开关
    会把选择写回这个列表。
    """

    dir: str = "plugins"
    enabled: list[str] = field(default_factory=list)
    command_prefix: str = "/"

    def __post_init__(self) -> None:
        """把 ``enabled`` 归一化成去重、去空白的字符串列表。

        兼容旧配置里写成 ``null`` 或单个字符串的写法：``None`` 按空列表
        处理（即不启用任何插件），不会意外变成「全部启用」。
        """
        raw = self.enabled
        if raw is None or raw == "":
            raw = []
        elif isinstance(raw, str):
            raw = [raw]
        cleaned: list[str] = []
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if item is None:
                    continue
                name = str(item).strip()
                if name and name not in cleaned:
                    cleaned.append(name)
        self.enabled = cleaned


@dataclass
class WebUIConfig:
    """WebUI 服务配置（第二阶段）。

    默认只绑 127.0.0.1；绑到非本地地址时必须设置 ``password``，否则拒绝启动。
    口令可以用 ``${ENV_VAR}`` 从环境变量读，避免写进配置文件。
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""
    # 登录会话有效期（小时）
    session_hours: float = 72.0


@dataclass
class TelegramConfig:
    """``adapters.telegram`` 配置段（Telegram Bot API 长轮询）。

    ``bot_token`` 与 llm 的 api_key 一样支持 ``${ENV_VAR}``（加载时已统一展开）。
    ``allowed_chat_ids`` 为空表示不限制会话；填了则只处理这些 chat_id 的消息。
    ``offset_file`` 落盘保存 getUpdates 的 offset，重启后不重复消费；留空表示不落盘。
    """

    bot_token: str = ""
    poll_timeout: float = 30.0
    allowed_chat_ids: list[str] = field(default_factory=list)
    offset_file: str = "data/telegram_offset.json"

    @classmethod
    def from_options(cls, options: Any) -> "TelegramConfig":
        if not isinstance(options, dict):
            return cls()
        token = options.get("bot_token")
        poll_timeout = _as_float(options.get("poll_timeout"), 30.0)
        allowed = options.get("allowed_chat_ids")
        if allowed is None or allowed == "":
            allowed_ids: list[str] = []
        elif isinstance(allowed, (str, int)):
            allowed_ids = [str(allowed)]
        elif isinstance(allowed, (list, tuple, set)):
            allowed_ids = [str(item) for item in allowed if str(item) != ""]
        else:
            allowed_ids = []
        offset_file = options.get("offset_file", "data/telegram_offset.json")
        return cls(
            bot_token=str(token or "").strip(),
            poll_timeout=max(0.0, poll_timeout),
            allowed_chat_ids=allowed_ids,
            offset_file="" if offset_file is None else str(offset_file),
        )


@dataclass
class QQOfficialConfig:
    """QQ 官方机器人（q.qq.com 开放平台）配置。

    ``client_secret`` 建议写 ``${ENV_VAR}`` 从环境变量读，避免明文进仓库。
    ``sandbox`` 打开后走沙箱域名 ``sandbox.api.sgroup.qq.com``，用于提审前联调。
    """

    app_id: str = ""
    client_secret: str = ""
    sandbox: bool = False
    # 入站图片附件是否下载（转 base64 放进内部 image 段）
    download_images: bool = True

    @property
    def configured(self) -> bool:
        """app_id 与 client_secret 都填了才算可用。"""
        return bool(self.app_id and self.client_secret)

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "QQOfficialConfig":
        """从 ``adapters.qq_official`` 配置段解析；缺省/类型不对的字段回落默认值。

        ``${ENV_VAR}`` 展开在 :meth:`Config.load` 阶段已经做过，这里不用重复。
        """
        if not isinstance(options, dict):
            return cls()
        return cls(
            app_id=str(options.get("app_id") or ""),
            client_secret=str(options.get("client_secret") or ""),
            sandbox=options.get("sandbox") is True,
            download_images=options.get("download_images") is not False,
        )


def is_loopback_host(host: str) -> bool:
    """判断绑定地址是否只对本机可见。

    ``localhost``、``::1``、以及整个 127.0.0.0/8 都算本地；空字符串按本地处理。
    """
    value = (host or "").strip()
    if value in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    persona_path: str = "data/SOUL.md"
    database_path: str = "data/rubenbot.db"
    adapters: dict[str, Any] = field(default_factory=dict)
    # 后台归纳检查间隔（分钟）
    background_check_minutes: float = 5.0
    log_level: str = "INFO"

    # ---- 导出 ----

    @property
    def enabled_adapters(self) -> list[str]:
        """返回配置中启用（值不为 null/false）的适配器名；``{}`` 也算启用。"""
        return [name for name, opts in self.adapters.items() if opts is not None and opts is not False]

    def adapter_options(self, name: str) -> dict[str, Any]:
        """某个适配器的配置段；未配置或不是 dict 时返回空 dict。"""
        options = self.adapters.get(name)
        return options if isinstance(options, dict) else {}

    def qq_official_options(self) -> QQOfficialConfig:
        """``adapters.qq_official`` 段的强类型视图。"""
        return QQOfficialConfig.from_options(self.adapter_options("qq_official"))

    def adapter_capability_enabled(self, adapter_name: str, config_key: str, default: bool = True) -> bool:
        """读取 ``adapters.<adapter>.capabilities.<config_key>`` 开关。

        未写时用 ``default``；只接受布尔值，写成字符串等一律视为关闭，避免
        ``"false"`` 这种意外真值。
        """
        caps = self.adapter_options(adapter_name).get("capabilities")
        if not isinstance(caps, dict) or config_key not in caps:
            return default
        return caps[config_key] is True

    def adapter_capabilities(self, adapter_name: str) -> dict[str, bool]:
        """某个适配器配置里的能力开关表（原样返回布尔化后的副本）。"""
        caps = self.adapter_options(adapter_name).get("capabilities")
        if not isinstance(caps, dict):
            return {}
        return {str(key): value is True for key, value in caps.items()}

    def telegram_config(self) -> TelegramConfig:
        """解析 ``adapters.telegram`` 段；未配置时返回默认值。"""
        return TelegramConfig.from_options(self.adapter_options("telegram"))

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        """从 YAML 加载配置；文件不存在时返回全默认值。"""
        file = Path(path)
        if not file.exists():
            return cls()
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"配置文件格式错误：{file}")
        return cls.from_raw(data)

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "Config":
        """从已解析的 YAML 数据构造配置（展开 ``${ENV}``）。

        WebUI 结构化保存时先用它校验合并后的字典，通过了才写盘。
        """
        if not isinstance(data, dict):
            raise ValueError("配置的顶层必须是键值对（YAML mapping）")
        return cls.from_dict(_expand_env(data))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        llm = LLMConfig(**{k: v for k, v in (data.get("llm") or {}).items() if _has(LLMConfig, k)})
        memory = _load_memory(data.get("memory") or {})
        search = SearchConfig(
            **{k: v for k, v in (data.get("search") or {}).items() if _has(SearchConfig, k)}
        )
        chat = ChatConfig(**{k: v for k, v in (data.get("chat") or {}).items() if _has(ChatConfig, k)})
        decision = _load_decision(data.get("decision") or {})
        plugins = PluginsConfig(**{k: v for k, v in (data.get("plugins") or {}).items() if _has(PluginsConfig, k)})
        webui = WebUIConfig(**{k: v for k, v in (data.get("webui") or {}).items() if _has(WebUIConfig, k)})
        persona = data.get("persona") or {}
        if isinstance(persona, str):
            persona = {"path": persona}
        basics = {
            "persona_path": persona.get("path", "data/SOUL.md"),
            "database_path": data.get("database_path", "data/rubenbot.db"),
            "adapters": data.get("adapters") or {},
            "background_check_minutes": data.get("background_check_minutes", 5.0),
            "log_level": data.get("log_level", "INFO"),
        }
        return cls(
            llm=llm,
            memory=memory,
            search=search,
            chat=chat,
            decision=decision,
            plugins=plugins,
            webui=webui,
            **basics,
        )


def _load_memory(data: dict[str, Any]) -> MemoryConfig:
    """解析 memory 段；``rolling_summary`` 单独解析成嵌套配置。

    滚动摘要缺省开启（``enabled: true``），旧配置不写这个段时行为与新版
    默认一致；写错类型的字段回落默认值，不让配置问题挡住启动。
    """
    if not isinstance(data, dict):
        return MemoryConfig()
    plain = {k: v for k, v in data.items() if _has(MemoryConfig, k) and k != "rolling_summary"}
    config = MemoryConfig(**plain)
    raw = data.get("rolling_summary")
    if isinstance(raw, dict):
        default = RollingSummaryConfig()
        config.rolling_summary = RollingSummaryConfig(
            enabled=raw.get("enabled") is not False,
            trigger_after=_as_int(raw.get("trigger_after"), default.trigger_after, minimum=1),
            keep_recent=_as_int(raw.get("keep_recent"), default.keep_recent, minimum=0),
            max_chars=_as_int(raw.get("max_chars"), default.max_chars, minimum=100),
        )
    return config


def _load_decision(data: dict[str, Any]) -> DecisionConfig:
    """解析 decision 段；``groups`` 单独转成 GroupDecisionConfig。"""
    if not isinstance(data, dict):
        return DecisionConfig()
    plain = {k: v for k, v in data.items() if _has(DecisionConfig, k) and k != "groups"}
    config = DecisionConfig(**plain)
    groups = data.get("groups") or {}
    if isinstance(groups, dict):
        for chat_id, options in groups.items():
            if not isinstance(options, dict):
                continue
            quiet = options.get("quiet_hours") or []
            if isinstance(quiet, str):
                quiet = [quiet]
            config.groups[str(chat_id)] = GroupDecisionConfig(
                activity_multiplier=float(options.get("activity_multiplier", 1.0)),
                base=None if options.get("base") is None else float(options["base"]),
                quiet_hours=[str(item) for item in quiet],
            )
    return config


def _has(dataclass_type: type, key: str) -> bool:
    return key in getattr(dataclass_type, "__dataclass_fields__", {})


def _as_float(value: Any, default: float) -> float:
    """宽松转 float：空值/非法值回落到默认，避免配置写错就启动失败。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    """宽松转 int（配置里可能写成字符串），下限兜底到 ``minimum``。"""
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)
