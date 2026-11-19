"""插件发现与加载。

插件是一个目录，形如 ``plugins/<name>/plugin.py``，暴露 ``register(ctx)``。
ctx 提供：

- ``on_message(handler)``：消息预处理钩子，``handler(ctx, event)``；
  返回 False 表示拦截（不再继续处理），返回 None/True 表示放行
- ``on_before_reply(handler)``：``handler(ctx, event, messages)``，可返回新的 messages
- ``on_after_reply(handler)``：``handler(ctx, event, reply)``
- ``command(name, handler)``：注册命令，``handler(ctx, event, args)`` 返回字符串作为回复

钩子与命令里的异常都会被捕获并记录，不会拖垮主循环。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..adapters.base import MessageEvent, Segment

logger = logging.getLogger(__name__)

MessageHook = Callable[..., Any]
CommandHandler = Callable[..., Any]

# 各钩子返回值的语义标记：True 放行 / False 拦截
HookResult = bool | None


@dataclass
class Command:
    name: str
    handler: CommandHandler


@dataclass
class Plugin:
    """一个已加载的插件。"""

    name: str
    path: Path
    message_hooks: list[MessageHook] = field(default_factory=list)
    before_reply_hooks: list[MessageHook] = field(default_factory=list)
    after_reply_hooks: list[MessageHook] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    enabled: bool = True


class PluginContext:
    """传给插件 ``register(ctx)`` 的注册上下文。"""

    def __init__(self, plugin: Plugin, manager: "PluginManager"):
        self.plugin = plugin
        self.manager = manager
        self.config = manager.config
        self.store = manager.store
        # 插件开发者可以通过它拿到宿主的其他组件
        self.extras: dict[str, Any] = manager.extras

    def on_message(self, handler: MessageHook) -> MessageHook:
        self.plugin.message_hooks.append(handler)
        return handler

    def on_before_reply(self, handler: MessageHook) -> MessageHook:
        self.plugin.before_reply_hooks.append(handler)
        return handler

    def on_after_reply(self, handler: MessageHook) -> MessageHook:
        self.plugin.after_reply_hooks.append(handler)
        return handler

    def command(self, name: str, handler: CommandHandler | None = None):
        """注册命令。既可直接传 handler，也可作装饰器：``@ctx.command("hi")``。"""
        normalized = name.lstrip("/").lower()

        def register(handler: CommandHandler) -> CommandHandler:
            self.plugin.commands[normalized] = Command(name=normalized, handler=handler)
            return handler

        if handler is None:
            return register
        return register(handler)


async def _call(handler: Callable[..., Any], *args: Any) -> Any:
    """同时支持同步与异步 handler。"""
    result = handler(*args)
    if inspect.isawaitable(result):
        return await result
    return result


class PluginManager:
    """发现、注册并分发插件钩子。"""

    def __init__(self, config, store, *, extras: dict[str, Any] | None = None):
        self.config = config
        self.store = store
        self.extras: dict[str, Any] = extras or {}
        self.plugins: dict[str, Plugin] = {}

    @property
    def enabled_plugins(self) -> list[Plugin]:
        return [p for p in self.plugins.values() if p.enabled]

    def load_all(self, directory: str | Path | None = None) -> list[Plugin]:
        """扫描目录加载全部插件。单个插件失败只跳过它。"""
        root = Path(directory or self.config.plugins.dir)
        if not root.exists():
            logger.info("插件目录不存在，跳过：%s", root)
            return []
        allow = self.config.plugins.enabled
        loaded: list[Plugin] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            plugin_file = entry / "plugin.py"
            if not plugin_file.exists():
                continue
            enabled = True if allow is None else entry.name in allow
            plugin = Plugin(name=entry.name, path=entry, enabled=enabled)
            self.plugins[entry.name] = plugin
            if not enabled:
                # 禁用的插件不调用 register，也就不会注册任何钩子
                logger.info("插件 %s 已禁用", entry.name)
                continue
            try:
                self._load_plugin(plugin)
                loaded.append(plugin)
                logger.info("插件 %s 已加载", entry.name)
            except Exception as exc:  # noqa: BLE001 - 插件失败不能拖垮宿主
                logger.warning("加载插件 %s 失败：%s", entry.name, exc)
                self.plugins.pop(entry.name, None)
        return loaded

    def _load_plugin(self, plugin: Plugin) -> None:
        module_name = f"rubenbot_plugin_{plugin.name}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin.path / "plugin.py",
            submodule_search_locations=[str(plugin.path)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块：{plugin.path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise AttributeError("plugin.py 缺少 register(ctx) 函数")
        register(PluginContext(plugin, self))

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    async def run_message_hooks(self, ctx, event: MessageEvent) -> bool:
        """返回 False 表示某插件拦截了这条消息。"""
        for plugin in self.enabled_plugins:
            for handler in plugin.message_hooks:
                try:
                    result = await _call(handler, ctx, event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_message 异常：%s", plugin.name, exc)
                    continue
                if result is False:
                    return False
        return True

    async def run_before_reply(self, ctx, event: MessageEvent, messages: list[dict[str, str]]):
        for plugin in self.enabled_plugins:
            for handler in plugin.before_reply_hooks:
                try:
                    result = await _call(handler, ctx, event, messages)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_before_reply 异常：%s", plugin.name, exc)
                    continue
                if isinstance(result, list):
                    messages = result
        return messages

    async def run_after_reply(self, ctx, event: MessageEvent, reply: str) -> None:
        for plugin in self.enabled_plugins:
            for handler in plugin.after_reply_hooks:
                try:
                    await _call(handler, ctx, event, reply)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_after_reply 异常：%s", plugin.name, exc)

    async def handle_commands(self, ctx, event: MessageEvent) -> bool:
        """识别 ``/命令 参数`` 并执行；返回是否命中。"""
        text = (event.text or "").strip()
        prefix = self.config.plugins.command_prefix
        if not prefix or not text.startswith(prefix):
            return False
        body = text[len(prefix) :].strip()
        if not body:
            return False
        name, _, args = body.partition(" ")
        name = name.lower()
        for plugin in self.enabled_plugins:
            command = plugin.commands.get(name)
            if command is None:
                continue
            try:
                result = await _call(command.handler, ctx, event, args.strip())
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 %s 的命令 %s 异常：%s", plugin.name, name, exc)
                result = "命令执行出错了"
            if isinstance(result, str) and result:
                adapter = ctx.adapters.get(event.platform)
                if adapter is not None:
                    try:
                        await adapter.send(event.chat_id, [Segment.text(result)], chat_type=event.chat_type)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("命令回复发送失败：%s", exc)
            return True
        return False

    def list_plugins(self) -> list[dict[str, Any]]:
        """供 WebUI/调试使用。"""
        return [
            {
                "name": p.name,
                "enabled": p.enabled,
                "commands": sorted(p.commands),
                "hooks": {
                    "on_message": len(p.message_hooks),
                    "on_before_reply": len(p.before_reply_hooks),
                    "on_after_reply": len(p.after_reply_hooks),
                },
            }
            for p in self.plugins.values()
        ]
