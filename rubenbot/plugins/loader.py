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
- ``store`` / ``fetch``：读消息、出网；分别需要 ``read_messages`` / ``network`` 授权
- ``bot`` / ``runtime``：宿主 bot 与最近一次分发时的运行时上下文

**装后授权**：manifest 只声明权限，机主在 WebUI 里逐项授权后插件才能真正使用。
``call_capability`` 按 ``capabilities``（``qzone.*`` 按 ``qzone_write``）检查，
``llm`` / ``store`` / ``fetch`` / ``bot`` 与钩子拿到的适配器 ``send`` 分别检查
``llm`` / ``read_messages`` / ``network`` / ``send_messages``；插件通过
``store`` 写数据一律拒绝（写入口不向插件开放）。未授权时抛
:class:`~rubenbot.plugins.permissions.PluginPermissionError` 并记审计日志。
声明了 ``network`` 却未授权的插件不会分发钩子、命令或能力说明；后台任务
（``on_start``）启动前会再核一遍 ``scheduler`` 与声明的 ``network`` / ``llm``，
任何一项未授权都不拉起。

授权检查是**默认拒绝**：没有记录、记录读不出来、权限被撤回，都按未授权处理。
Python 插件本身仍是完全可信代码（市场页已说明），这里收口的是宿主公开给插件
的入口，不构成沙盒。

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
from typing import Any, Callable, Iterable, Mapping

import httpx

from ..adapters.base import BaseAdapter, MessageEvent, Segment
from ..expression import CapabilityNote
from .manifest import Manifest, ManifestError, load_manifest
from .permissions import (
    PluginPermissionError,
    PluginPermissionStore,
    capability_permission,
    path_for_store,
)

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
class CapabilityNoteSpec:
    """一条能力说明，附带它成立所需的授权（默认按插件声明的全部权限）。"""

    describe: Callable[[MessageEvent, BaseAdapter], CapabilityNote | None]
    requires: tuple[str, ...] | None = None


@dataclass
class Plugin:
    """一个已加载的插件。"""

    name: str
    path: Path
    message_hooks: list[MessageHook] = field(default_factory=list)
    before_reply_hooks: list[MessageHook] = field(default_factory=list)
    after_reply_hooks: list[MessageHook] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    # 随行为注册、随插件停用；返回 None 表示当前线路未验通或本轮不可用。
    capability_notes: list[CapabilityNoteSpec] = field(default_factory=list)
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


# 读消息 / 历史 / 记忆的方法；插件经 ctx.store / runtime.store / bot.store 调这些要
# read_messages 授权。不在名单里的可调用方法一律按「写存储」拒绝（默认拒绝）：
# 插件没有写宿主存储的授权项，宁可拦下，不给「顺手写一条」留口子。
_READ_METHOD_PREFIXES = ("recent_", "list_", "search_", "count_", "get_", "raw_")
_READ_METHOD_NAMES = frozenset(
    {
        "message_by_platform_id",
        "platform_message_exists",
        "chat_counts",
        "snapshot",
        "timeline_rows",
        "pending_messages",
        "active_senders",
        "available_raw_ids",
        "conversation_events",
        "profile_evidence_refs",
    }
)


class PermissionedStore:
    """插件侧的存储代理：读方法按 ``read_messages`` 授权，写方法一律拒绝。

    不继承 :class:`MemoryStore`：只转发属性访问，每次调用都重新检查授权，
    撤回权限后插件手里已经拿到的代理也会立即失效。插件不该写宿主存储，
    因此没有对应的授权项——写入口直接拒绝并留审计。
    """

    def __init__(self, store: Any, context: "PluginContext"):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_context", context)

    def _is_read(self, name: str) -> bool:
        return name in _READ_METHOD_NAMES or name.startswith(_READ_METHOD_PREFIXES)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._store, name)
        if not callable(value):
            return value
        if self._is_read(name):
            def guarded(*args: Any, **kwargs: Any) -> Any:
                self._context._require("read_messages", f"读取 {name}")
                return value(*args, **kwargs)

            return guarded

        def denied(*args: Any, **kwargs: Any) -> Any:
            self._context._deny("store_write", f"插件写存储 {name}")

        return denied

    def __setattr__(self, name: str, value: Any) -> None:
        # 属性赋值同样算写操作，一律拒绝（privacy_enabled 等状态只读即可）
        self._context._deny("store_write", f"修改存储属性 {name}")

    def __repr__(self) -> str:
        return f"<PermissionedStore {type(self._store).__name__}>"


class PermissionedLLM:
    """LLM 客户端代理：每次调用模型前检查 ``llm`` 授权，撤回后立即失效。"""

    def __init__(self, client: Any, context: "PluginContext"):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_context", context)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._client, name)
        if callable(value):
            def guarded(*args: Any, **kwargs: Any) -> Any:
                self._context._require("llm", f"调用模型 {name}")
                return value(*args, **kwargs)

            return guarded
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._client, name, value)


class PermissionedAdapter:
    """转发给插件钩子的适配器：发消息 / 平台能力按授权放行。"""

    def __init__(self, adapter: BaseAdapter, context: "PluginContext"):
        object.__setattr__(self, "_adapter", adapter)
        object.__setattr__(self, "_context", context)

    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> Any:
        self._context._require("send_messages", "发送消息")
        return await self._adapter.send(chat_id, segments, chat_type=chat_type)

    async def send_file(self, chat_id: str, filename: str, content: bytes, *, chat_type: str) -> None:
        self._context._require("send_messages", "发送文件")
        return await self._adapter.send_file(chat_id, filename, content, chat_type=chat_type)

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        self._context._require(capability_permission(name), f"能力 {name}")
        return await self._adapter.call_capability(name, **kwargs)

    async def call_action(self, name: str, params: dict[str, Any] | None = None) -> Any:
        self._context._require("capabilities", f"动作 {name}")
        return await self._adapter.call_action(name, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._adapter, name, value)

    def __eq__(self, other: Any) -> bool:
        # 插件常把适配器对象存起来做「同一条线路」的判断；代理每次新建，
        # 相等性必须回到被代理的真实适配器，否则每次比较都不相等。
        if isinstance(other, PermissionedAdapter):
            return self._adapter is other._adapter
        return self._adapter is other

    def __hash__(self) -> int:
        return hash(id(self._adapter))


class PermissionedExtras:
    """``extras`` 的受限视图：敏感键换成收口代理，其余原样转发。

    插件仍然可以读写自己的附加项（宿主和官方插件依赖这个约定），但
    ``extras["bot"]`` 不会再露出裸 bot，``extras["plugins"]`` 对插件不可见。
    """

    def __init__(self, data: dict[str, Any], wrap: Callable[[str, Any], Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_wrap", wrap)

    def _get(self, key: str) -> Any:
        return self._wrap(key, self._data[key])

    def __getitem__(self, key: str) -> Any:
        return self._get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._data:
            return default
        return self._get(key)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._get(key)
        self._data[key] = default
        return default

    def pop(self, key: str, *args: Any) -> Any:
        if key in self._data:
            return self._get(key)
        if args:
            return args[0]
        raise KeyError(key)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._data.update(*args, **kwargs)

    def keys(self):
        return self._data.keys()

    def values(self):
        return [self._get(key) for key in self._data]

    def items(self):
        return [(key, self._get(key)) for key in self._data]

    def copy(self) -> dict[str, Any]:
        return dict(self.items())


class PermissionedBot:
    """给插件看的宿主 bot：store / llm / adapters / call_capability 收口。

    其余属性（``config`` / ``persona`` / ``_stop`` 等）照常转发，避免打断
    现有插件对宿主对象的依赖；危险入口全部换成按授权检查的代理。
    """

    def __init__(self, bot: Any, context: "PluginContext"):
        object.__setattr__(self, "_bot", bot)
        object.__setattr__(self, "_context", context)

    @property
    def store(self) -> Any:
        store = getattr(self._bot, "store", None)
        if store is None:
            return None
        return PermissionedStore(store, self._context)

    @property
    def llm(self) -> Any:
        client = getattr(self._bot, "llm", None)
        if client is None:
            return None
        return PermissionedLLM(client, self._context)

    @property
    def adapters(self) -> dict[str, PermissionedAdapter]:
        adapters = getattr(self._bot, "adapters", None) or {}
        context = self._context
        return {
            name: context.adapter_view(adapter)
            for name, adapter in adapters.items()
            if adapter is not None
        }

    @property
    def context(self) -> Any:
        runtime = getattr(self._bot, "context", None)
        if runtime is None:
            return None
        return PermissionedRuntime(runtime, self._context)

    @property
    def plugins(self) -> None:
        # 插件管理器带 store / 授权记录，不向插件暴露
        return None

    @property
    def memory_reader(self) -> Any:
        reader = getattr(self._bot, "memory_reader", None)
        if reader is None:
            return None
        self._context._require("read_messages", "使用记忆读取")
        return reader

    async def call_capability(self, adapter_name: str | None, name: str, **kwargs: Any) -> Any:
        self._context._require(capability_permission(name), f"能力 {name}")
        return await self._bot.call_capability(adapter_name, name, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bot, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # 修改宿主对象不算插件该做的事，转发但要求调用方自己承担风险
        setattr(self._bot, name, value)


class PermissionedRuntime:
    """转发给钩子的运行时上下文：store / adapters / llm / extras 按授权收口。"""

    def __init__(self, runtime: Any, context: "PluginContext"):
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "_context", context)

    @property
    def store(self) -> Any:
        store = getattr(self._runtime, "store", None)
        if store is None:
            return None
        return PermissionedStore(store, self._context)

    @property
    def adapters(self) -> dict[str, PermissionedAdapter]:
        adapters = getattr(self._runtime, "adapters", None) or {}
        context = self._context
        return {
            name: context.adapter_view(adapter)
            for name, adapter in adapters.items()
            if adapter is not None
        }

    @property
    def llm(self) -> Any:
        client = getattr(self._runtime, "llm", None)
        if client is None:
            return None
        return PermissionedLLM(client, self._context)

    @property
    def extras(self) -> PermissionedExtras:
        raw = getattr(self._runtime, "extras", None)
        if not isinstance(raw, dict):
            raw = {}
        return PermissionedExtras(raw, self._context._wrap_extra)

    @property
    def plugins(self) -> None:
        # 插件管理器带 store 与授权记录，不向插件暴露
        return None

    @property
    def memory(self) -> Any:
        reader = getattr(self._runtime, "memory", None)
        if reader is None:
            return None
        self._context._require("read_messages", "使用记忆读取")
        return reader

    @property
    def retriever(self) -> Any:
        retriever = getattr(self._runtime, "retriever", None)
        if retriever is None:
            return None
        self._context._require("read_messages", "使用检索")
        return retriever

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._runtime, name, value)


class PluginContext:
    """传给插件 ``register(ctx)`` 的注册上下文。"""

    def __init__(self, plugin: Plugin, manager: "PluginManager"):
        self.plugin = plugin
        self.manager = manager
        self.config = manager.config
        # 插件拿到的 extras：自己的附加项照常读写，bot / plugins 等敏感键换成
        # 收口代理。宿主与测试继续用 ``manager.extras`` 操作底层字典。
        self.extras: PermissionedExtras = PermissionedExtras(
            manager.extras, self._wrap_extra
        )

    # ------------------------------------------------------------------
    # 对外视图（授权收口）
    # ------------------------------------------------------------------

    def _wrap_extra(self, key: str, value: Any) -> Any:
        if key == "bot" and value is not None and not isinstance(value, PermissionedBot):
            return PermissionedBot(value, self)
        if key == "plugins":
            return None
        return value

    @property
    def store(self) -> Any:
        """宿主存储（经授权代理）：读消息/历史/记忆需 ``read_messages`` 授权。"""
        store = self.manager.store
        if store is None:
            return None
        return PermissionedStore(store, self)

    # ------------------------------------------------------------------
    # 宿主组件与平台能力
    # ------------------------------------------------------------------

    @property
    def runtime(self) -> Any | None:
        """最近一次分发时的运行时上下文（``BotContext``），可能为 None。"""
        runtime = getattr(self.manager, "current_runtime", None)
        if runtime is None:
            return None
        return PermissionedRuntime(runtime, self)

    @property
    def bot(self) -> Any | None:
        """宿主 bot（经授权代理：store / llm / adapters / 能力入口已收口）。

        优先取构造 ``PluginManager`` 时注入的 ``extras["bot"]``，否则取最近
        一次分发的运行时上下文里的 bot（宿主每处理一条消息都会更新）。
        """
        bot = self.manager.extras.get("bot")
        if bot is None:
            runtime = getattr(self.manager, "current_runtime", None)
            extra = getattr(runtime, "extras", None)
            bot = extra.get("bot") if isinstance(extra, dict) else None
        if bot is None or isinstance(bot, PermissionedBot):
            return bot
        return PermissionedBot(bot, self)

    @property
    def llm(self) -> Any | None:
        """核心的 LLM 客户端（需 ``llm`` 授权）；bot 尚未就绪时为 None。"""
        client = getattr(self.manager.extras.get("bot"), "llm", None)
        if client is None:
            return None
        self._require("llm", "LLM 客户端")
        return PermissionedLLM(client, self)

    def adapter_view(self, adapter: Any) -> Any:
        """适配器的收口代理；同一真实适配器复用同一个代理对象。

        插件会把适配器对象存起来做「还是同一条线路」的判断（点歌插件就是这样），
        每次新建代理会让身份比较永远不成立，所以按真实对象缓存。
        """
        return self.manager.adapter_view(self.plugin, adapter)

    # ------------------------------------------------------------------
    # 授权检查
    # ------------------------------------------------------------------

    @property
    def permissions(self) -> PluginPermissionStore | None:
        """本次分发所用的授权记录。"""
        return getattr(self.manager, "permissions", None)

    def allowed(self, permission: str) -> bool:
        store = self.permissions
        return store is not None and store.allows(self.plugin.name, permission)

    def _deny(self, permission: str, action: str) -> None:
        """记一条审计并抛出未授权异常（默认拒绝）。"""
        store = self.permissions
        if store is not None:
            store.audit_denial(self.plugin.name, permission, action)
        raise PluginPermissionError(self.plugin.name, permission, action)

    def _require(self, permission: str, action: str) -> None:
        """未授权即抛 :class:`PluginPermissionError` 并记一条审计。"""
        store = self.permissions
        if store is not None and store.allows(self.plugin.name, permission):
            return
        self._deny(permission, action)

    async def call_capability(
        self,
        name: str,
        *,
        adapter: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """经核心能力入口调用平台能力，返回 ``(是否成功, 说明/失败原因)``。

        插件不直接碰任何平台协议：未声明能力、未授权、配置关闭、执行失败都
        只会拿到一个失败原因，不会抛异常（授权拒绝同时写审计日志）。
        """
        try:
            self._require(capability_permission(name), f"能力 {name}")
        except PluginPermissionError as exc:
            return False, str(exc)
        bot = self.bot
        call = getattr(bot, "call_capability", None)
        if not callable(call):
            return False, "宿主未就绪，无法调用平台能力"
        try:
            return await call(adapter, name, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 能力失败不能拖垮插件
            logger.warning("插件调用能力 %s 异常：%s", name, exc)
            return False, str(exc) or exc.__class__.__name__

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        """插件出网的唯一入口（需 ``network`` 授权）：返回 ``httpx.Response``。

        未授权时抛 :class:`PluginPermissionError` 并记审计；网络错误照常
        向上抛，由插件自己决定如何降级。
        """
        self._require("network", f"出网请求 {method} {url}")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await client.request(method, url, headers=dict(headers or {}))

    def capability_note(
        self,
        describe: Callable[[MessageEvent, BaseAdapter], CapabilityNote | None],
        *,
        requires: str | Iterable[str] | None = None,
    ) -> None:
        """登记一个无副作用的本轮能力说明，不代替行为钩子的注册或鉴权。

        ``requires`` 指定这条说明成立所需的授权；省略时按插件声明的全部权限
        计——只要有一项没授权，这条说明就不会出现在注入内容里。
        """
        if requires is None:
            items: tuple[str, ...] | None = None
        elif isinstance(requires, str):
            items = (requires,)
        else:
            items = tuple(requires)
        self.plugin.capability_notes.append(CapabilityNoteSpec(describe, items))

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

    def __init__(
        self,
        config,
        store,
        *,
        extras: dict[str, Any] | None = None,
        permissions: PluginPermissionStore | None = None,
    ):
        self.config = config
        self.store = store
        self.extras: dict[str, Any] = extras or {}
        self.plugins: dict[str, Plugin] = {}
        # 最近一次分发时的运行时上下文（BotContext）：插件可从它拿 bot、persona 等
        self.current_runtime: Any = None
        # 装后授权：构造时没给就按数据库同目录的独立文件读；内存库只驻内存
        self.permissions = permissions or PluginPermissionStore(path_for_store(store))
        # 适配器收口代理缓存：(插件名, id(真实适配器)) -> 代理，保证同一真实
        # 适配器每次拿到的是同一个代理对象（插件用它判断同一条线路）。
        self._adapter_proxies: dict[tuple[str, int], PermissionedAdapter] = {}

    def adapter_view(self, plugin: Plugin, adapter: Any) -> PermissionedAdapter | None:
        """返回适配器的收口代理；同一真实对象复用同一个代理。"""
        if adapter is None:
            return None
        key = (plugin.name, id(adapter))
        proxy = self._adapter_proxies.get(key)
        if proxy is None or proxy._adapter is not adapter:
            proxy = PermissionedAdapter(adapter, PluginContext(plugin, self))
            self._adapter_proxies[key] = proxy
        return proxy

    def _migrate_permissions(self, plugins: Iterable[Plugin]) -> None:
        """首次升级：已装插件按当前 manifest 视为已授权，新装的从空授权开始。

        只在授权层从未初始化过时真正写入；之后放进插件目录或从市场新装的
        插件一律保持空授权（默认拒绝）。
        """
        installed = {
            plugin.name: plugin.manifest.permissions
            for plugin in plugins
            if plugin.valid and plugin.manifest is not None
        }
        try:
            self.permissions.migrate(installed)
        except Exception as exc:  # noqa: BLE001 - 授权迁移失败不阻断启动
            logger.warning("插件授权迁移失败：%s", exc)

    def _remember_runtime(self, ctx) -> None:
        if ctx is not None:
            self.current_runtime = ctx

    def register_new_plugin(self, name: str) -> frozenset[str]:
        """市场装好一个插件后调用：登记空授权（新装默认全未授）。

        必须在 ``discover()`` 之前调用，否则首次迁移可能把它当成已装插件全授。
        """
        plugin = self.plugins.get(name)
        declared = plugin.manifest.permissions if plugin is not None and plugin.manifest else ()
        if not declared:
            # 市场装完时管理器还没扫目录：直接读落盘的 manifest 拿声明，
            # 保证空授权记录按真实声明写盘（记录本身不存权限，只写空表）
            try:
                declared = load_manifest(Path(self.config.plugins.dir) / name).permissions
            except ManifestError:
                declared = ()
        self.permissions.ensure_registered(name, declared)
        return self.permissions.grants(name)

    def forget_plugin(self, name: str) -> None:
        """插件卸载后清掉授权记录，同名重装时从空授权开始。"""
        self.permissions.forget(name)

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
        # 首次升级：授权记录还没初始化时，把此刻扫到的已装插件按 manifest 全授；
        # 之后才放进目录的插件（或市场新装的）一律从空授权开始。
        self._migrate_permissions(found)
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
                    self._start_lifecycle(plugin)
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
        plugin.capability_notes.clear()
        plugin.start_hooks.clear()
        plugin.stop_hooks.clear()
        register(PluginContext(plugin, self))
        plugin.loaded = True
        if plugin.enabled:
            self._start_lifecycle(plugin)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def _note_allowed(self, plugin: Plugin, spec: CapabilityNoteSpec) -> bool:
        """能力说明只在该插件所需权限都已授权时注入（默认拒绝）。

        显式 ``requires`` 优先；省略时按 manifest 声明的全部权限计——声明了
        却没授权的插件，一条说明也不出现在注入内容里。授权记录不可用时同样
        不注入。
        """
        store = getattr(self, "permissions", None)
        if store is None:
            return False
        required = spec.requires
        if required is None:
            required = plugin.manifest.permissions if plugin.manifest is not None else ()
        return all(store.allows(plugin.name, permission) for permission in required)

    def capability_notes(self, event: MessageEvent, adapter: BaseAdapter) -> list[CapabilityNote]:
        notes = []
        for plugin in self.enabled_plugins:
            if plugin.loaded and self._hooks_allowed(plugin):
                context = PluginContext(plugin, self)
                for spec in plugin.capability_notes:
                    if not self._note_allowed(plugin, spec):
                        continue
                    try:
                        # 说明里拿到的适配器同样收口：send 需要 send_messages
                        note = spec.describe(
                            event, context.adapter_view(adapter) if adapter is not None else adapter
                        )
                        if isinstance(note, CapabilityNote):
                            notes.append(note)
                    except PluginPermissionError:
                        continue
                    except Exception as exc:
                        logger.warning("插件 %s 的能力说明不可用：%s", plugin.name, exc)
        return notes

    def _dispatch_context(self, plugin: Plugin, ctx: Any) -> Any:
        """钩子/命令拿到的运行时上下文：store / adapters / llm 按授权收口。"""
        if ctx is None:
            return None
        return PermissionedRuntime(ctx, PluginContext(plugin, self))

    def _command_reply_text(self, plugin: Plugin, result: Any) -> str | None:
        """命令返回值要不要由宿主代为发送：声明了 ``send_messages`` 才发。

        宿主代发同样是以 bot 身份发消息，按同一个授权项裁决；未授权时只记
        审计、不回这条命令的结果，默认拒绝。
        """
        if not isinstance(result, str) or not result:
            return None
        store = getattr(self, "permissions", None)
        if store is None or not store.allows(plugin.name, "send_messages"):
            if store is not None:
                store.audit_denial(plugin.name, "send_messages", "发送命令回复")
            return None
        return result

    async def run_message_hooks(self, ctx, event: MessageEvent) -> bool:
        """返回 False 表示某插件拦截了这条消息。"""
        self._remember_runtime(ctx)
        for plugin in self.enabled_plugins:
            if not self._hooks_allowed(plugin):
                continue
            seen = self._dispatch_context(plugin, ctx)
            for handler in plugin.message_hooks:
                try:
                    result = await _call(handler, seen, event)
                except PluginPermissionError as exc:
                    logger.warning("插件 %s 的 on_message 未授权：%s", plugin.name, exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_message 异常：%s", plugin.name, exc)
                    continue
                if result is False:
                    return False
        return True

    async def run_before_reply(self, ctx, event: MessageEvent, messages: list[dict[str, str]]):
        for plugin in self.enabled_plugins:
            if not self._hooks_allowed(plugin):
                continue
            seen = self._dispatch_context(plugin, ctx)
            for handler in plugin.before_reply_hooks:
                try:
                    result = await _call(handler, seen, event, messages)
                except PluginPermissionError as exc:
                    logger.warning("插件 %s 的 on_before_reply 未授权：%s", plugin.name, exc)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_before_reply 异常：%s", plugin.name, exc)
                    continue
                if isinstance(result, list):
                    messages = result
        return messages

    async def run_after_reply(self, ctx, event: MessageEvent, reply: str) -> None:
        for plugin in self.enabled_plugins:
            if not self._hooks_allowed(plugin):
                continue
            seen = self._dispatch_context(plugin, ctx)
            for handler in plugin.after_reply_hooks:
                try:
                    await _call(handler, seen, event, reply)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 %s 的 on_after_reply 异常：%s", plugin.name, exc)

    def _hooks_allowed(self, plugin: Plugin) -> bool:
        """声明了 ``network`` 却没授权的插件不分发钩子。

        钩子内部完全可能直接出网（现有官方插件就是这么做的），在入口拦一次
        比事后审计更可靠；其余权限在具体入口按需检查。默认拒绝：授权记录
        不可用时同样不放行。
        """
        store = getattr(self, "permissions", None)
        if store is None:
            return False
        if plugin.manifest is not None and "network" in plugin.manifest.permissions:
            if not store.allows(plugin.name, "network"):
                return False
        return True

    def _scheduler_allowed(self, plugin: Plugin) -> bool:
        """后台任务启动准入：``scheduler`` 及声明的 ``network`` / ``llm`` 都要授权。

        后台任务往往没人盯着就直接出网或调模型，入口检查只能事后拦截；这里
        在启动前再做一次，避免未授权联网 / 计费的定时任务被拉起来。
        """
        store = getattr(self, "permissions", None)
        if store is None:
            return False
        if plugin.manifest is None:
            return True
        declared = set(plugin.manifest.permissions)
        if "scheduler" in declared and not store.allows(plugin.name, "scheduler"):
            return False
        for permission in ("network", "llm"):
            if permission in declared and not store.allows(plugin.name, permission):
                return False
        return True

    def _start_lifecycle(self, plugin: Plugin) -> None:
        """按授权决定是否跑 ``on_start``：起后台任务前再核一遍所需权限。"""
        if not self._scheduler_allowed(plugin):
            logger.info("插件 %s 的定时任务授权未开启，跳过 on_start", plugin.name)
            return
        self._run_lifecycle(plugin, plugin.start_hooks, "on_start")

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
            if not self._hooks_allowed(plugin):
                # 与钩子同一条准入：声明了 network 却没授权的插件不执行命令
                store = getattr(self, "permissions", None)
                if store is not None:
                    store.audit_denial(plugin.name, "network", f"执行命令 {name}")
                continue
            seen = self._dispatch_context(plugin, ctx)
            try:
                result = await _call(command.handler, seen, event, args.strip())
            except PluginPermissionError as exc:
                logger.warning("插件 %s 的命令 %s 未授权：%s", plugin.name, name, exc)
                result = "这条命令需要的权限还没授权"
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 %s 的命令 %s 异常：%s", plugin.name, name, exc)
                result = "命令执行出错了"
            reply = self._command_reply_text(plugin, result)
            if reply:
                adapter = ctx.adapters.get(event.platform)
                if adapter is not None:
                    try:
                        await adapter.send(event.chat_id, [Segment.text(reply)], chat_type=event.chat_type)
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
                # 声明与授权的逐项对照，供 WebUI 渲染授权开关
                "permissions": self.permissions.summary(
                    p.name, p.manifest.permissions if p.manifest is not None else ()
                ),
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

    def set_permissions(self, name: str, permissions: Iterable[str]) -> frozenset[str]:
        """整表替换某插件的授权（WebUI 开关用），返回最新授权集合。

        只能授出 manifest 声明过的权限；未声明的一律拒绝。授权变化立即生效：
        撤掉 ``scheduler`` / ``network`` / ``llm`` 导致后台任务不再满足启动
        条件时会触发 ``on_stop``，让任务立即停下；重新满足条件时补一次
        ``on_start``。其它权限在调用入口即时生效，已注册的钩子不用重载。
        """
        plugin = self.plugins.get(name)
        if plugin is None:
            raise KeyError(name)
        if not plugin.valid or plugin.manifest is None:
            raise ValueError(plugin.invalid_reason or "manifest 不合规")
        declared = plugin.manifest.permissions
        had_start = self._scheduler_allowed(plugin)
        grants = self.permissions.set_permissions(name, permissions, declared=declared)
        self._sync_background(plugin, had_start, self._scheduler_allowed(plugin))
        return grants

    def _sync_background(self, plugin: Plugin, had: bool, has: bool) -> None:
        """后台任务的启动条件变化时立即启停，不重启 bot。"""
        if had == has or not plugin.loaded:
            return
        if not has:
            self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
        elif plugin.enabled:
            self._start_lifecycle(plugin)

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
                self._start_lifecycle(plugin)
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
