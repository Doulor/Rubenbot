"""插件发现与加载。

插件是一个目录，形如 ``plugins/<name>/plugin.py``，暴露 ``register(ctx)``。
插件采用**显式启用**制：只有 ``config.plugins.enabled`` 里列出的插件才会
加载；未配置或空列表表示一个都不启用。WebUI 的插件开关会把选择写回
这个列表并热生效（新启用的现场 ``register``，停用的触发 ``on_stop``）。
ctx 提供：

- ``on_message(handler)``：消息预处理钩子，``handler(ctx, event)``；
  返回 False 表示拦截（不再继续处理），返回 None/True 表示放行
- ``on_before_reply(handler)``：``handler(ctx, event, messages)``，可返回新的 messages
- ``on_after_reply(handler)``：``handler(ctx, event, reply)``
- ``command(name, handler)``：注册命令，``handler(ctx, event, args)`` 返回字符串作为回复
- ``on_start(handler)`` / ``on_stop(handler)``：插件启用 / 停用时的生命周期回调
  （后台任务据此启动与干净退出；可同步/异步，异常只记录）

除了 ``store`` / ``config``，插件还可以经 ``PluginContext`` 使用：

- ``call_capability(name, **kwargs)``：走核心能力入口（如 ``qzone.publish``），
  返回 ``(是否成功, 说明/原因)``；适配器不支持、配置关闭或执行失败都只返回原因
- ``llm``：核心的 LLM 客户端（需要 bot 已就绪，否则为 None）
- ``bot`` / ``runtime``：宿主 bot 与最近一次分发时的运行时上下文

钩子与命令里的异常都会被捕获并记录，不会拖垮主循环。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..adapters.base import MessageEvent, Segment
from .manifest import Manifest, ManifestError, load_manifest

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
    # 插件停用/卸载时的清理回调（关闭后台任务等）
    stop_hooks: list[Callable[[], Any]] = field(default_factory=list)
    # 插件启用时的启动回调（拉起后台任务等）
    start_hooks: list[Callable[[], Any]] = field(default_factory=list)
    enabled: bool = False
    # 是否真的执行过 register；加载时被禁用的插件是 False
    loaded: bool = False
    # 校验通过的 manifest；插件目录没有或不合规时为 None
    manifest: Manifest | None = None
    # manifest 不合规的原因（缺文件/缺字段/权限越界/核心版本过低）
    invalid_reason: str | None = None
    # 不合规是否因为框架版本低于 min_core_version
    core_too_old: bool = False

    @property
    def valid(self) -> bool:
        """manifest 是否合规（合规的插件才能加载）。"""
        return self.manifest is not None and self.invalid_reason is None


class PluginContext:
    """传给插件 ``register(ctx)`` 的注册上下文。"""

    def __init__(self, plugin: Plugin, manager: "PluginManager"):
        self.plugin = plugin
        self.manager = manager
        self.config = manager.config
        self.store = manager.store
        # 插件开发者可以通过它拿到宿主的其他组件
        self.extras: dict[str, Any] = manager.extras

    # ------------------------------------------------------------------
    # 宿主组件与平台能力
    # ------------------------------------------------------------------

    @property
    def runtime(self) -> Any | None:
        """最近一次分发时的运行时上下文（``BotContext``），可能为 None。"""
        return getattr(self.manager, "current_runtime", None)

    @property
    def bot(self) -> Any | None:
        """宿主 bot。

        优先取构造 ``PluginManager`` 时注入的 ``extras["bot"]``，否则取最近
        一次分发的运行时上下文里的 bot（宿主每处理一条消息都会更新）。
        """
        bot = self.extras.get("bot")
        if bot is not None:
            return bot
        runtime = self.runtime
        extra = getattr(runtime, "extras", None)
        return extra.get("bot") if isinstance(extra, dict) else None

    @property
    def llm(self) -> Any | None:
        """核心的 LLM 客户端；bot 尚未就绪时为 None。"""
        return getattr(self.bot, "llm", None)

    async def call_capability(
        self,
        name: str,
        *,
        adapter: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """经核心能力入口调用平台能力，返回 ``(是否成功, 说明/失败原因)``。

        插件不直接碰任何平台协议：未声明能力、配置关闭、执行失败都只会拿到
        一个失败原因，不会抛异常。
        """
        bot = self.bot
        call = getattr(bot, "call_capability", None)
        if not callable(call):
            return False, "宿主未就绪，无法调用平台能力"
        try:
            return await call(adapter, name, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 能力失败不能拖垮插件
            logger.warning("插件调用能力 %s 异常：%s", name, exc)
            return False, str(exc) or exc.__class__.__name__

    def on_message(self, handler: MessageHook) -> MessageHook:
        self.plugin.message_hooks.append(handler)
        return handler

    def on_before_reply(self, handler: MessageHook) -> MessageHook:
        self.plugin.before_reply_hooks.append(handler)
        return handler

    def on_after_reply(self, handler: MessageHook) -> MessageHook:
        self.plugin.after_reply_hooks.append(handler)
        return handler

    def on_start(self, handler: Callable[[], Any]) -> Callable[[], Any]:
        """注册插件启动回调：插件加载后、及运行时被重新启用时调用。

        回调可以同步也可以返回 awaitable；异常只记录不向外抛。
        """
        self.plugin.start_hooks.append(handler)
        return handler

    def on_stop(self, handler: Callable[[], Any]) -> Callable[[], Any]:
        """注册插件清理回调：插件被禁用时调用（后台任务据此干净退出）。

        回调可以同步也可以返回 awaitable；异常只记录不向外抛。
        """
        self.plugin.stop_hooks.append(handler)
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


def discover_plugin_dirs(directory: str | Path | None = None) -> list[tuple[str, Path]]:
    """扫描插件目录，返回 ``[(插件名, 目录路径), ...]``（按名称排序）。

    只认 ``<目录>/<插件名>/plugin.py`` 的结构；下划线/点开头的目录跳过。
    目录不存在时返回空列表。WebUI 不依赖插件管理器也能列出可用插件。
    """
    root = Path(directory or "plugins")
    if not root.exists():
        return []
    found: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if (entry / "plugin.py").exists():
            found.append((entry.name, entry))
    return found


class PluginManager:
    """发现、注册并分发插件。

    插件采用显式启用制：只有 ``config.plugins.enabled`` 里列出的插件才会
    调用 ``register`` 并分发钩子；未配置或空列表表示一个都不启用。
    目录里发现的所有插件仍会记进 ``self.plugins``（``enabled=False``），
    供 WebUI 列表与运行时开启使用。
    """

    def __init__(self, config, store, *, extras: dict[str, Any] | None = None):
        self.config = config
        self.store = store
        self.extras: dict[str, Any] = extras or {}
        self.plugins: dict[str, Plugin] = {}
        # 最近一次分发时的运行时上下文（BotContext）：插件可从它拿 bot、persona 等
        self.current_runtime: Any = None

    def _remember_runtime(self, ctx) -> None:
        if ctx is not None:
            self.current_runtime = ctx

    @property
    def enabled_plugins(self) -> list[Plugin]:
        return [p for p in self.plugins.values() if p.enabled]

    @property
    def enabled_names(self) -> list[str]:
        """当前启用的插件名（保持发现顺序）。"""
        return [p.name for p in self.plugins.values() if p.enabled]

    def _discover(self, directory: str | Path | None = None) -> list[Plugin]:
        """扫描目录，登记发现的插件但不执行 ``register``。

        已经登记过的插件沿用原对象（保留已注册的钩子、启停状态与生命周期
        回调），这里只更新路径；新发现的插件一律先记为未启用，由
        :meth:`load_all` / :meth:`apply_enabled` / :meth:`set_enabled` 决定
        是否真正加载。每次扫描都重新校验 manifest：装好/换过 manifest 的
        插件不用重启就能拿到最新状态。
        """
        root = Path(directory or self.config.plugins.dir)
        if not root.exists():
            logger.info("插件目录不存在，跳过：%s", root)
            return []
        found: list[Plugin] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            plugin_file = entry / "plugin.py"
            if not plugin_file.exists():
                continue
            plugin = self.plugins.get(entry.name)
            if plugin is None:
                plugin = Plugin(name=entry.name, path=entry)
                self.plugins[entry.name] = plugin
            else:
                plugin.path = entry
            self._refresh_manifest(plugin)
            found.append(plugin)
        return found

    @staticmethod
    def _refresh_manifest(plugin: Plugin) -> None:
        """读取并校验 manifest，把结果记到插件对象上（不合规只记原因）。"""
        try:
            manifest = load_manifest(plugin.path)
        except ManifestError as exc:
            plugin.manifest = None
            plugin.invalid_reason = exc.reason
            plugin.core_too_old = exc.core_too_old
            return
        plugin.manifest = manifest
        plugin.invalid_reason = None
        plugin.core_too_old = False

    def _reject(self, plugin: Plugin, reason: str) -> None:
        """记录拒绝原因并解除启用状态（发现列表里保留它，供 WebUI 展示）。"""
        plugin.invalid_reason = reason
        plugin.enabled = False
        plugin.loaded = False
        logger.warning("拒绝加载插件 %s：%s", plugin.name, reason)

    def discover(self) -> list[Plugin]:
        """重新扫描插件目录（发现新目录），返回当前登记的插件。

        WebUI 列表用它保证新放进 ``plugins/`` 的插件不用重启即可出现在
        页面上；新发现的插件先记为未启用，等真正启用时才 ``register``。
        """
        return self._discover()

    def load_all(self, directory: str | Path | None = None) -> list[Plugin]:
        """扫描目录并加载**启用列表里**的插件。单个插件失败只跳过它。

        ``plugins.enabled`` 未配置或为空时不会加载任何插件（默认全关）。
        """
        found = self._discover(directory)
        allow = set(self.config.plugins.enabled)
        loaded: list[Plugin] = []
        for plugin in found:
            if plugin.name not in allow:
                # 禁用的插件不调用 register，也就不会注册任何钩子；
                # 之前在运行时启用过的，顺手触发 on_stop 干净退出
                if plugin.enabled or plugin.loaded:
                    self.set_enabled(plugin.name, False)
                logger.debug("插件 %s 未在启用列表中，跳过", plugin.name)
                continue
            if not plugin.valid:
                # manifest 不合规（含核心版本过低）：拒绝加载，但保留在发现列表里
                self._reject(plugin, plugin.invalid_reason or "manifest 不合规")
                continue
            if plugin.loaded:
                # 重复调用 load_all 不重复 register（避免钩子翻倍），
                # 但运行中被停用的要补一次 on_start
                if not plugin.enabled:
                    plugin.enabled = True
                    self._run_lifecycle(plugin, plugin.start_hooks, "on_start")
                loaded.append(plugin)
                continue
            plugin.enabled = True  # _load_plugin 按启用状态决定是否跑 on_start
            try:
                self._load_plugin(plugin)
                loaded.append(plugin)
                logger.info("插件 %s 已加载", plugin.name)
            except ManifestError as exc:
                # manifest 在扫描后又被改坏：拒绝加载，但不从列表里移除
                self._reject(plugin, exc.reason)
            except Exception as exc:  # noqa: BLE001 - 插件失败不能拖垮宿主
                plugin.enabled = False
                logger.warning("加载插件 %s 失败：%s", plugin.name, exc)
                self.plugins.pop(plugin.name, None)
        missing = allow - {plugin.name for plugin in found}
        for name in sorted(missing):
            logger.warning("插件 %s 已写入 plugins.enabled，但目录里没有找到", name)
        return loaded

    def apply_enabled(self, names: list[str] | None) -> list[str]:
        """把新的启用列表写进配置并热应用，返回启用失败的插件名。

        会重新扫描插件目录（发现新增的插件目录），然后逐个同步启停：
        新启用的现场 ``register``，停用的触发 ``on_stop`` 并停止分发。
        未找到的插件名只记日志，不影响其它插件。
        """
        cleaned: list[str] = []
        for item in names or []:
            name = str(item).strip()
            if name and name not in cleaned:
                cleaned.append(name)
        self.config.plugins.enabled = cleaned
        allow = set(cleaned)
        self._discover()
        failures: list[str] = []
        for name in cleaned:
            plugin = self.plugins.get(name)
            if plugin is None:
                logger.warning("插件 %s 已写入 plugins.enabled，但目录里没有找到", name)
                failures.append(name)
                continue
            if plugin.enabled and plugin.loaded:
                continue
            if not plugin.valid:
                # 不合规的插件不能启用：如实报失败，但保留在列表里让 WebUI 显示原因
                self._reject(plugin, plugin.invalid_reason or "manifest 不合规")
                failures.append(name)
                continue
            if not self.set_enabled(name, True):
                plugin.enabled = False
                failures.append(name)
                if plugin.invalid_reason:
                    # manifest 校验失败：保留插件对象，不当作导入失败移除
                    continue
                # 与 load_all 一致：加载失败的插件从列表移除，下次扫描再发现
                self.plugins.pop(name, None)
        for name, plugin in list(self.plugins.items()):
            if name not in allow and (plugin.enabled or plugin.loaded):
                self.set_enabled(name, False)
        # set_enabled 会逐条增删内存列表（顺序跟着操作走），最后按提交顺序收敛
        self.config.plugins.enabled = cleaned
        return failures

    def _load_plugin(self, plugin: Plugin) -> None:
        # 加载前再校验一次 manifest：目录可能在上一次扫描后被改过
        self._refresh_manifest(plugin)
        if not plugin.valid:
            raise ManifestError(
                plugin.invalid_reason or "manifest 不合规",
                core_too_old=plugin.core_too_old,
            )
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
        # 运行时重新启用会重新 register：先清掉上次注册的钩子，避免重复分发
        plugin.message_hooks.clear()
        plugin.before_reply_hooks.clear()
        plugin.after_reply_hooks.clear()
        plugin.commands.clear()
        plugin.start_hooks.clear()
        plugin.stop_hooks.clear()
        register(PluginContext(plugin, self))
        plugin.loaded = True
        if plugin.enabled:
            self._run_lifecycle(plugin, plugin.start_hooks, "on_start")

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    async def run_message_hooks(self, ctx, event: MessageEvent) -> bool:
        """返回 False 表示某插件拦截了这条消息。"""
        self._remember_runtime(ctx)
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
        self._remember_runtime(ctx)
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
                    # TODO（动量，2026-11-22 与 Orange 定）：命令是硬触发（/命令），下次动这里时，
                    # 发送成功后补调 decision_engine.note_bot_reply(chat_key, now, addressed=True)，
                    # 发送失败不喂——与 send_reply 全失败不记发言一致（tests/test_momentum_feed.py）。
                    # 现在先不动：ctx 目前拿不到 decision_engine，得先把引擎接进插件上下文。
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
                "loaded": p.loaded,
                "valid": p.valid,
                "invalid_reason": p.invalid_reason,
                "core_too_old": p.core_too_old,
                "manifest": p.manifest.as_dict() if p.manifest is not None else None,
                "commands": sorted(p.commands),
                "hooks": {
                    "on_message": len(p.message_hooks),
                    "on_before_reply": len(p.before_reply_hooks),
                    "on_after_reply": len(p.after_reply_hooks),
                },
                "path": str(p.path),
            }
            for p in sorted(self.plugins.values(), key=lambda item: item.name)
        ]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """运行时启用/禁用插件，返回是否找到该插件。

        禁用只是让分发时跳过它（已注册的钩子留着，再次启用立即生效）；
        启用一个加载时被跳过的插件时，会现场执行它的 ``register``。
        禁用/启用时会同步调用插件的 ``on_stop`` / ``on_start`` 回调，
        后台任务据此启动和干净退出。manifest 不合规的插件不能启用。

        这个开关会同步更新内存里 ``config.plugins.enabled``；WebUI 调用它
        之后负责把列表写回 config.yaml。
        """
        plugin = self.plugins.get(name)
        if plugin is None:
            return False
        if enabled and not plugin.valid:
            # 目录里的 manifest 可能刚被改好/改坏：再校验一次
            self._refresh_manifest(plugin)
            if not plugin.valid:
                self._reject(plugin, plugin.invalid_reason or "manifest 不合规")
                return False
        was_enabled = plugin.enabled
        if enabled and not plugin.loaded:
            plugin.enabled = True  # _load_plugin 按启用状态决定是否跑 on_start
            try:
                self._load_plugin(plugin)
            except Exception as exc:  # noqa: BLE001 - 插件失败不能拖垮宿主
                plugin.enabled = was_enabled
                logger.warning("启用插件 %s 失败：%s", name, exc)
                return False
        else:
            plugin.enabled = enabled
            if enabled and not was_enabled:
                self._run_lifecycle(plugin, plugin.start_hooks, "on_start")
            elif not enabled and was_enabled:
                self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
        self._sync_enabled_config(name, enabled)
        logger.info("插件 %s 已%s", name, "启用" if enabled else "禁用")
        return True

    def _sync_enabled_config(self, name: str, enabled: bool) -> None:
        """把单个插件的开关同步进内存里 ``config.plugins.enabled``。

        保持用户原有顺序，只增删这一个名字；写回 YAML 由 WebUI 负责。
        """
        names = list(self.config.plugins.enabled)
        if enabled:
            if name not in names:
                names.append(name)
        elif name in names:
            names.remove(name)
        self.config.plugins.enabled = names

    @staticmethod
    def _run_lifecycle(plugin: Plugin, hooks: list[Callable[[], Any]], label: str) -> None:
        """同步调用生命周期回调；协程回调有事件循环时调度，否则丢弃并记日志。"""
        for handler in list(hooks):
            try:
                result = handler()
            except Exception as exc:  # noqa: BLE001 - 回调失败不能拖垮宿主
                logger.warning("插件 %s 的 %s 回调异常：%s", plugin.name, label, exc)
                continue
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    logger.debug("插件 %s 的 %s 是协程，当前没有事件循环，已跳过", plugin.name, label)
                    continue
                loop.create_task(_await_close(result, plugin.name, label))


async def _await_close(result: Any, plugin_name: str, label: str) -> None:
    try:
        await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("插件 %s 的 %s 回调异常：%s", plugin_name, label, exc)
