"""链接理解的配置项（core 感知层）。

链接理解与图片描述同属感知能力，不再走插件，配置落在 ``config.yaml`` 的
``link`` 段。取值都做宽松归一：写错类型或超出范围回落到默认，不因为一个
展示性参数挡住启动。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("on_mention", "auto")
DELIVERIES = ("direct", "planner")


def _number(raw: Any, default: float, low: float, high: float, *, integer: bool = False) -> int | float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = float(raw)
    if not math.isfinite(value) or not low <= value <= high:
        return default
    return int(value) if integer else value


@dataclass
class LinkConfig:
    """``link`` 段：读不读、什么时候读、读多少、结果怎么交给模型。"""

    # 总开关：关掉后 core 完全不发外部请求，链接只当普通文字。
    enabled: bool = True
    # on_mention：被 @ / 叫名字 / 回复 bot，或带着提问时读；
    # auto：收到链接就预取（按会话冷却与每日额度），开口时只取已读到的。
    mode: str = "on_mention"
    # direct：资料直接作为本轮回复的外部参考（策划与生成都能看到）；
    # planner：策划步只看到索引，选中「读链接」才把正文交给生成步。
    delivery: str = "direct"
    cooldown_seconds: float = 60.0
    daily_limit: int = 100
    cache_ttl_seconds: float = 3600.0
    max_bytes: int = 1024 * 1024
    timeout_seconds: float = 6.0
    # 每条消息最多读几个不同链接；同 URL 的并发请求合并。
    max_links: int = 3
    # 正文摘录上限（字符）；元数据字段另有各自的上限。
    max_body_chars: int = 1200
    # 可选兜底：JS 渲染页或强反爬时，主链路读不到才调用指定 MCP 工具
    # （``mcp__<服务器>__<工具>``，参数 ``{"url": ...}``）。留空不兜底。
    fallback_tool: str = ""

    def __post_init__(self) -> None:
        self.enabled = self.enabled is True
        if self.mode not in MODES:
            logger.warning("link.mode=%r 未知，按 on_mention 处理", self.mode)
            self.mode = "on_mention"
        if self.delivery not in DELIVERIES:
            logger.warning("link.delivery=%r 未知，按 direct 处理", self.delivery)
            self.delivery = "direct"
        self.fallback_tool = str(self.fallback_tool or "").strip()
        self.cooldown_seconds = _number(self.cooldown_seconds, 60.0, 0, 86400)
        self.daily_limit = _number(self.daily_limit, 100, 0, 100000, integer=True)
        self.cache_ttl_seconds = _number(self.cache_ttl_seconds, 3600.0, 0, 604800)
        self.max_bytes = _number(self.max_bytes, 1024 * 1024, 1, 4 * 1024 * 1024, integer=True)
        self.timeout_seconds = _number(self.timeout_seconds, 6.0, 0.1, 30)
        self.max_links = _number(self.max_links, 3, 1, 10, integer=True)
        self.max_body_chars = _number(self.max_body_chars, 1200, 200, 5000, integer=True)

    @classmethod
    def parse(cls, raw: Any) -> "LinkConfig":
        """从 YAML 的 ``link`` 段构造；未知键忽略，坏值回落默认。"""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            logger.warning("link 应为配置对象，已按默认值处理")
            return cls()
        fields = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in raw.items() if key in fields})
