"""配置加载：读取单个 YAML，并给出合理默认值。

配置里支持 ``${ENV_VAR}`` 形式的环境变量展开，方便把 api_key 放在环境变量里。
"""

from __future__ import annotations

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

    group_talkativeness: float = 0.08
    group_talkativeness_boost: float = 0.25
    boost_window_seconds: float = 90.0
    history_size: int = 30
    split_max_parts: int = 3
    split_part_chars: int = 60
    split_delay_seconds: float = 0.6
    bot_name: str = "Ruben"
    history_ttl_hours: float = 12.0


@dataclass
class PluginsConfig:
    """插件目录与启停。

    ``enabled`` 为 None 表示启用全部；给列表则只启用列表中的插件。
    """

    dir: str = "plugins"
    enabled: list[str] | None = None
    command_prefix: str = "/"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
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
        plugins = PluginsConfig(**{k: v for k, v in (data.get("plugins") or {}).items() if _has(PluginsConfig, k)})
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
        return cls(llm=llm, memory=memory, chat=chat, plugins=plugins, **basics)


def _has(dataclass_type: type, key: str) -> bool:
    return key in getattr(dataclass_type, "__dataclass_fields__", {})
