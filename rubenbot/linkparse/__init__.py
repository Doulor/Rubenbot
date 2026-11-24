"""core 链接理解（感知层）。

与图片描述同性质：任何 http(s) 链接都尝试读取，平台专用提取器只是解析手段，
不设站点白名单。SSRF 校验、单页大小/超时上限与外部内容隔离标注都在 core 内。

模块分工：

- :mod:`~rubenbot.linkparse.urls`：链接抽取、规范化（去片段/跟踪参数）与平台识别；
- :mod:`~rubenbot.linkparse.fetch`：受检的公网抓取（每跳校验、固定地址、限额）；
- :mod:`~rubenbot.linkparse.extract`：结构化数据 → 通用元数据 → 正文提取；
- :mod:`~rubenbot.linkparse.service`：缓存、冷却、每日额度与交给模型的隔离数据块；
- :mod:`~rubenbot.linkparse.settings`：``config.yaml`` 的 ``link`` 段。

``service`` 会经 ``decision`` 间接依赖 ``config``，而 ``config`` 又需要这里的
``LinkConfig``；因此包入口只暴露配置类型，其余名称按需惰性导入。
"""

from .settings import DELIVERIES, MODES, LinkConfig

_EXPORTS = {
    "GUIDE": "service",
    "LINK_CAPABILITY_TITLE": "service",
    "LinkContext": "service",
    "LinkResult": "service",
    "LinkService": "service",
    "asks_about_link": "service",
}

__all__ = ["DELIVERIES", "GUIDE", "LINK_CAPABILITY_TITLE", "LinkConfig", "LinkContext",
           "LinkResult", "LinkService", "MODES", "asks_about_link"]


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)
