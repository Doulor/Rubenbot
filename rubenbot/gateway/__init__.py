"""通用适配器网关：接口目录、风险分级、冷却引擎与审计。

分层（见任务总纲）：核心层只认「归一化后的抽象动作」与「事件」，
不出现任何 OneBot/QQ 专有词；平台相关的东西（动作名、参数 schema、
风险级、默认冷却）全部放在数据目录里，由各适配器自己提供。

本包包含：

- :mod:`catalog`：接口目录的读取与检索（关键词过滤 / 按名取 schema）
- :class:`Gatekeeper`：四维冷却、风险闸、失败退避与审计落库
"""

from .catalog import ActionEntry, ActionCatalog, catalog_for

__all__ = ["ActionCatalog", "ActionEntry", "catalog_for"]
