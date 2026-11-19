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


@dataclass
class LLMConfig:
    """OpenAI 兼容接口配置。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    extract_model: str | None = None
    consolidate_model: str | None = None
    temperature: float = 0.8
    timeout: float = 60.0
    max_retries: int = 2

    def model_for(self, role: str = "chat") -> str:
        """按用途取模型，抽取/归纳未配置时跟随 chat。"""
        if role == "extract":
            return self.extract_model or self.chat_model
        if role == "consolidate":
            return self.consolidate_model or self.chat_model
        return self.chat_model


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


@dataclass
class ChatConfig:
    """对话与发送参数。"""

    history_size: int = 30
    split_max_parts: int = 3
    split_part_chars: int = 60
    split_delay_seconds: float = 0.6
    bot_name: str = "Ruben"
    history_ttl_hours: float = 12.0


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
class PluginsConfig:
    """插件目录与启停。

    ``enabled`` 为 None 表示启用全部；给列表则只启用列表中的插件。
    """

    dir: str = "plugins"
    enabled: list[str] | None = None
    command_prefix: str = "/"


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

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        """从 YAML 加载配置；文件不存在时返回全默认值。"""
        file = Path(path)
        if not file.exists():
            return cls()
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"配置文件格式错误：{file}")
        return cls.from_dict(_expand_env(data))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        llm = LLMConfig(**{k: v for k, v in (data.get("llm") or {}).items() if _has(LLMConfig, k)})
        memory = MemoryConfig(**{k: v for k, v in (data.get("memory") or {}).items() if _has(MemoryConfig, k)})
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
            chat=chat,
            decision=decision,
            plugins=plugins,
            webui=webui,
            **basics,
        )


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
