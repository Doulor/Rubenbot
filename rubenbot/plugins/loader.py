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
- ``on_load(handler)``：插件加载完成（``register`` 返回后）时调用，
  ``handler(ctx, manifest)``
- ``on_unload(handler)``：插件停用 / 卸载 / 热重载前调用，``handler(ctx)``，
  供插件清理定时任务、后台线程与临时资源
- ``on_config_change(handler)``：插件配置经 WebUI 变更后调用，
  ``handler(ctx, old, new)``；实现了它才算「配置能热生效」

除了 ``store`` / ``config``，插件还可以经 ``PluginContext`` 使用：

- ``call_capability(name, **kwargs)``：走核心能力入口（如 ``qzone.publish``），
  返回 ``(是否成功, 说明/原因)``；适配器不支持、配置关闭或执行失败都只返回原因
- ``llm``：核心的 LLM 客户端（需要 bot 已就绪，否则为 None）
- ``store`` / ``fetch``：读消息、出网；分别需要 ``read_messages`` / ``network`` 授权
- ``fetch_public_page`` / ``fetch_public_cover``：需 ``network`` 授权，受检读取公网链接或封面
- ``bot`` / ``runtime``：宿主 bot 与最近一次分发时的运行时上下文

**启用即信任**：机主显式启用时按本地 manifest 授权，自动恢复和重载不授新权限。
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

**热重载**（WebUI 插件页）：先调 ``on_unload`` / ``on_stop`` 让旧实例收尾，
再清掉旧模块与本次注册，用磁盘上的新代码重新 ``register`` → 校验 manifest →
调 ``on_load``。重载不重置授权记录、不删插件自己的 ``config.yaml``；任何一步
失败都把插件恢复到重载前的旧实例（旧钩子、旧后台任务照常），失败原因回传给
WebUI，插件不会从列表里消失。生命周期钩子本身也走授权准入：声明了
``network`` 却没授权的插件同样收不到 ``on_load`` / ``on_unload`` /
``on_config_change``。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import httpx

from ..adapters.base import BaseAdapter, MessageEvent, Segment
from ..expression import CapabilityNote
from ..linkparse import fetch as public_web
from ..linkparse import sites
from . import public_cover
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


class PluginReloadError(RuntimeError):
    """热重载失败且已回滚到重载前的实例。"""

    def __init__(self, plugin: str, reason: str):
        self.plugin = plugin
        self.reason = reason
        super().__init__(f"插件 {plugin} 重载失败，已回滚到重载前的实例：{reason}")


@dataclass
class Command:
    name: str
    handler: CommandHandler


@dataclass
class CapabilityNoteSpec:
    """一条能力说明，附带它成立所需的授权（默认按插件声明的全部权限）。"""

    describe: Callable[[MessageEvent, BaseAdapter], CapabilityNote | None]
    requires: tuple[str, ...] | None = None
    entry: Callable[..., Any] | None = None


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
    # 加载完成 / 停用（卸载、热重载前）时的生命周期回调，签名见模块文档
    load_hooks: list[Callable[..., Any]] = field(default_factory=list)
    unload_hooks: list[Callable[..., Any]] = field(default_factory=list)
    # 配置变更回调：handler(ctx, old, new)；空列表表示配置不能热生效
    config_change_hooks: list[Callable[..., Any]] = field(default_factory=list)
    enabled: bool = False
    # 是否真的执行过 register；加载时被禁用的插件是 False
    loaded: bool = False
    # 校验通过的 manifest；插件目录没有或不合规时为 None
    manifest: Manifest | None = None
    # manifest 不合规的原因（缺文件/缺字段/权限越界/核心版本过低）
    invalid_reason: str | None = None
    # 不合规是否因为框架版本低于 min_core_version
    core_too_old: bool = False
    # on_load 是否已经跑过且尚未被 on_unload 收尾（用来避免重复回调）
    lifecycle_active: bool = False

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
        result = await self._adapter.send(chat_id, segments, chat_type=chat_type)
        if self._context.plugin.name == "music":
            # 只有音乐卡、语音段能证明发歌；列表、错误提示和附加说明都是文字。
            for seg in segments:
                if seg.get("type") in ("music", "record"):
                    self._note_music_delivery(chat_id, chat_type, (seg.get("data") or {}).get("title", ""))
                    break
        return result

    async def send_file(self, chat_id: str, filename: str, content: bytes, *, chat_type: str) -> None:
        self._context._require("send_messages", "发送文件")
        result = await self._adapter.send_file(chat_id, filename, content, chat_type=chat_type)
        if self._context.plugin.name == "music" and Path(filename).suffix.lower() in (".mp3", ".m4a"):
            self._note_music_delivery(chat_id, chat_type, Path(filename).stem)
        return result

    def _note_music_delivery(self, chat_id: str, chat_type: str, title: str) -> None:
        callback = self._context.manager.on_music_delivery
        if callback is not None:
            try:
                callback(self._adapter.name, chat_type, chat_id, title)
            except Exception as exc:  # noqa: BLE001 - 记账失败不能触发插件重发
                logger.warning("记录点歌投递失败：%s", exc)

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
        """插件通用 HTTP 入口（需 ``network`` 授权），不可信链接请走受检 API。

        未授权时抛 :class:`PluginPermissionError` 并记审计；网络错误照常
        向上抛，由插件自己决定如何降级。
        """
        self._require("network", f"出网请求 {method} {url}")
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await client.request(method, url, headers=dict(headers or {}))

    async def fetch_public_page(
        self, url: str, *, allow_url: Callable[[str], bool] | None = None,
    ) -> public_web.FetchResult:
        """受检读取公网文本（1 MiB、共 6 秒）；可用 allow_url 在每跳限制站点。"""
        self._require("network", "读取公网网页")
        return await public_web.fetch(url, 1 << 20, 6.0, allow_url=allow_url, prepare=sites.prepare)

    async def fetch_public_cover(
        self, url: str, *, allow_url: Callable[[str], bool] | None = None,
    ) -> bytes:
        """受检读取静态封面（4 MiB、共 6 秒、不跟随跳转）；不返回远程地址。"""
        self._require("network", "读取公网封面")
        return await public_cover.fetch(url, allow_url=allow_url)

    def capability_note(
        self,
        describe: Callable[[MessageEvent, BaseAdapter], CapabilityNote | None],
        *,
        requires: str | Iterable[str] | None = None,
        entry: Callable[..., Any] | None = None,
    ) -> None:
        """登记无副作用的说明；多行为入口的插件用 entry 指向这条能力的处理函数。

        ``requires`` 指定这条说明成立所需的授权；省略时按插件声明的全部权限
        计——只要有一项没授权，这条说明就不会出现在注入内容里。
        """
        if requires is None:
            items: tuple[str, ...] | None = None
        elif isinstance(requires, str):
            items = (requires,)
        else:
            items = tuple(requires)
        self.plugin.capability_notes.append(CapabilityNoteSpec(describe, items, entry))

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

    def on_load(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """注册加载完成回调：``handler(ctx, manifest)``。

        在 ``register`` 返回后调用一次（热重载成功时再调用一次）；可以同步
        也可以返回 awaitable。异常只记录，不影响加载结果。
        """
        self.plugin.load_hooks.append(handler)
        return handler

    def on_unload(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """注册卸载清理回调：``handler(ctx)``。

        插件被禁用、卸载或热重载前调用，用来清理定时任务、后台线程与临时
        资源；可以同步也可以返回 awaitable。异常只记录，不阻断停用/重载。
        """
        self.plugin.unload_hooks.append(handler)
        return handler

    def on_config_change(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        """注册配置变更回调：``handler(ctx, old, new)``。

        插件声明这个钩子即表示配置可以热生效：WebUI 保存插件配置后，宿主会
        把变更前后的值传进来；钩子异常只记录。没有声明时 WebUI 会如实提示
        需要重启才生效，不假装热更新。
        """
        self.plugin.config_change_hooks.append(handler)
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


def _diff_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """两份插件配置里真正变了的键（值用相等比较，顺序不影响）。"""
    keys = set(old) | set(new)
    return sorted(key for key in keys if old.get(key) != new.get(key))


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
        self.on_music_delivery: Callable[[str, str, str, str], None] | None = None
        self._trust_migrated = False
        # 实授记录：构造时没给就按数据库同目录的独立文件读；内存库只驻内存
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

    def _write_grants(self, name: str, grants: Iterable[str]) -> None:
        """授权落盘后才允许加载；写入失败时恢复旧实授，沿用原有变更审计。"""
        previous = self.permissions.grants(name)
        self.permissions.set_permissions(name, grants)
        if error := self.permissions.last_error:
            self.permissions.set_permissions(name, previous)
            raise OSError(f"插件 {name} 授权写入失败：{error}")

    def trust_for_enable(self, plugin: Plugin) -> None:
        """仅显式启用用：校验本地声明，不从索引或客户端取权限，不执行插件代码。"""
        self._refresh_manifest(plugin)
        if plugin.manifest is None:
            raise ManifestError(plugin.invalid_reason or "manifest 不合规")
        self._write_grants(plugin.name, plugin.manifest.permissions)

    def migrate_enabled_permissions(self, plugins: Iterable[Plugin]) -> None:
        """首次升级只补启用名单的缺项；用独立版本标记兼容已有的旧授权文件。"""
        key = "plugins:enable_trust:v1"
        if self._trust_migrated or (self.store is not None and self.store.get_meta(key)):
            return
        # 损坏的授权文件仍默认拒绝，不当作待迁移的空记录。
        if not self.permissions.last_error:
            enabled = set(self.config.plugins.enabled)
            for plugin in plugins:
                if plugin.name not in enabled or plugin.manifest is None:
                    continue
                old = self.permissions.grants(plugin.name)
                missing = set(plugin.manifest.permissions) - old
                if missing:
                    try:
                        self._write_grants(plugin.name, old | missing)
                    except OSError as exc:
                        # 保持原授权，不因单个失败阻断其余插件或在后续启动补授新声明。
                        logger.warning("%s；修复写入问题后请手动停用再启用", exc)
        if self.store is not None:
            self.store.set_meta(key, "1")
        self._trust_migrated = True

    def _remember_runtime(self, ctx) -> None:
        if ctx is not None:
            self.current_runtime = ctx

    def register_new_plugin(self, name: str) -> frozenset[str]:
        """市场装好一个插件后登记空授权；仅安装不代表信任。"""
        self.permissions.ensure_registered(name, ())
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
        self.migrate_enabled_permissions(found)
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
                # 但运行中被停用的要补一次 on_start（此前停用时已跑过 on_unload）
                if not plugin.enabled:
                    plugin.enabled = True
                    if not plugin.lifecycle_active:
                        self._run_load(plugin)
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

    def apply_enabled(self, names: list[str] | None, *, trust: bool = True) -> list[str]:
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
        newly_enabled = set(cleaned) - set(self.config.plugins.enabled)
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
            if not self.set_enabled(name, True, trust=trust and name in newly_enabled):
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

    @staticmethod
    def _module_name(plugin: Plugin) -> str:
        """插件在主进程里的模块名（唯一，避免不同插件互相覆盖）。"""
        return f"rubenbot_plugin_{plugin.name}"

    def _purge_modules(self, plugin: Plugin) -> None:
        """清掉上一版插件模块及其子模块，保证热重载真的读到磁盘上的新代码。

        ``importlib`` 会把 ``rubenbot_plugin_<name>`` 及其 ``.fetch`` 等子模块
        留在 ``sys.modules``；只重建 ``plugin.py`` 的话，插件目录里其它被改动的
        文件仍是旧版。旧函数对象还持有旧模块的全局命名空间，回滚时继续可用。

        同时清掉插件目录的 ``__pycache__``：``.pyc`` 头里只有秒级 mtime，
        同一秒内、大小不变的改动会命中旧字节码，热重载就白做了。
        """
        prefix = self._module_name(plugin)
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                sys.modules.pop(name, None)
        shutil.rmtree(plugin.path / "__pycache__", ignore_errors=True)
        importlib.invalidate_caches()

    def _load_plugin(self, plugin: Plugin) -> None:
        # 加载前再校验一次 manifest：目录可能在上一次扫描后被改过
        self._refresh_manifest(plugin)
        if not plugin.valid:
            raise ManifestError(
                plugin.invalid_reason or "manifest 不合规",
                core_too_old=plugin.core_too_old,
            )
        module_name = self._module_name(plugin)
        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin.path / "plugin.py",
            submodule_search_locations=[str(plugin.path)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块：{plugin.path}")
        saved = self._snapshot(plugin)
        self._clear_registrations(plugin)
        self._purge_modules(plugin)
        try:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise AttributeError("plugin.py 缺少 register(ctx) 函数")
            register(PluginContext(plugin, self))
        except BaseException:
            # 注册到一半失败：把上一次的钩子/命令放回去，插件不因一次坏加载而失能
            self._restore(plugin, saved)
            self._purge_modules(plugin)
            raise
        plugin.loaded = True
        # (重新)加载成功且当前启用：先跑 on_load，再按授权拉起后台任务。
        # 已停用的插件只重建注册，不激活生命周期（热重载停用插件时同理）。
        if plugin.enabled:
            self._run_load(plugin)
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
        """本轮的插件能力说明：能分发的按实际可用性注入，做不了的如实标状态。

        启用状态只认 loader 自己的 ``enabled`` / ``loaded``（由
        ``config.plugins.enabled`` 和加载结果决定），不猜、不硬编码。没启用的
        插件不执行 ``register``，也就没有能力说明可调，这里按 manifest 的名字
        给一条不可用条目，让它出现在索引的「现在还做不了的」里而不是凭空消失
        ——否则机主点歌只会看到沉默，分不清是没开还是坏了。

        说明函数不算行为入口：加载和授权通过后，还要在实际分发表找到可调用的
        钩子或命令。只查入口，不为生成索引试发消息。
        """
        notes = []
        for plugin in self.plugins.values():
            if not (plugin.valid and plugin.enabled and plugin.loaded and self._hooks_allowed(plugin)):
                note = self._unavailable_note(plugin)
                if note is not None:
                    notes.append(note)
                continue
            if not plugin.capability_notes:
                continue
            context = PluginContext(plugin, self)
            for spec in plugin.capability_notes:
                entry = self._callable_entry(plugin, spec)
                if not entry or not self._note_allowed(plugin, spec):
                    continue
                try:
                    # 说明里拿到的适配器同样收口：send 需要 send_messages
                    note = spec.describe(
                        event, context.adapter_view(adapter) if adapter is not None else adapter
                    )
                    if (isinstance(note, CapabilityNote) and note.title.strip()
                            and note.summary.strip() and note.detail.strip()):
                        notes.append(replace(note, evidence=(f'plugin:{plugin.name}:loaded',
                                                             f'entry:{entry}', 'permissions:granted')))
                except PluginPermissionError:
                    continue
                except Exception as exc:
                    logger.warning("插件 %s 的能力说明不可用：%s", plugin.name, exc)
        return notes

    @staticmethod
    def _callable_entry(plugin: Plugin, spec: CapabilityNoteSpec) -> str | None:
        entries = [(name, hook) for name, hooks in (('on_message', plugin.message_hooks),
                                                     ('on_before_reply', plugin.before_reply_hooks),
                                                     ('on_after_reply', plugin.after_reply_hooks)) for hook in hooks]
        entries.extend((f'command:{name}', cmd.handler) for name, cmd in plugin.commands.items())
        # 兼容点歌等只有一个行为入口的插件；多个入口不能拿无关命令替另一条能力背书。
        if spec.entry is None:
            return entries[0][0] if len(entries) == 1 and callable(entries[0][1]) else None
        return next((name for name, handler in entries if callable(handler) and handler == spec.entry), None)

    @staticmethod
    def _unavailable_note(plugin: Plugin) -> CapabilityNote | None:
        """做不了的插件，按 manifest 和 loader 记录的真实状态报一句。

        - 没启用：说「还没开」，这是机主能自己去开的开关状态。
        - 启用但没加载起来，或钩子被授权准入拦下（如声明了 network 未授权）：
          说「这会儿用不了」，不把故障或授权缺失说成没开。

        只认 loader 记录的启用/加载结果，不回退去猜配置。manifest 不合规的
        插件连名字都不可信，也可能根本没装对，宁可不提，也不拿目录名冒充
        能力；这类插件在 WebUI 插件页另有「不合规」标记。
        """
        if not plugin.valid:
            return None
        name = plugin.manifest.name
        if not plugin.enabled:
            return CapabilityNote(
                name,
                "还没开",
                f"有人明确要你{name}时，就直说还没开，别装作能做，也别拿别的凑数。",
                available=False,
            )
        return CapabilityNote(
            name,
            "这会儿用不了",
            f"有人明确要你{name}时，就直说这会儿用不了，别装作能做，也别拿别的凑数。",
            available=False,
        )

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

    def _run_load(self, plugin: Plugin) -> None:
        """调 ``on_load(ctx, manifest)``：与钩子同一条准入，未授权不发。

        声明了 ``network`` 却没授权的插件连加载回调也不给——它在回调里同样
        可能直接出网。异常只记日志，不影响插件继续工作。
        """
        if not self._hooks_allowed(plugin):
            self._audit_skip(plugin, "加载回调 on_load")
            return
        context = PluginContext(plugin, self)
        manifest = plugin.manifest
        self._run_lifecycle(plugin, plugin.load_hooks, "on_load", context, manifest)
        # 先置标记再记日志：插件在 on_load 里同步查 list_plugins 也能看到已激活
        plugin.lifecycle_active = True
        logger.info(
            "插件 %s 加载完成（on_load %s）",
            plugin.name,
            f"已执行 {len(plugin.load_hooks)} 个回调" if plugin.load_hooks else "未声明",
        )

    def _run_unload(self, plugin: Plugin, reason: str) -> None:
        """调 ``on_unload(ctx)``：停用、卸载、热重载前各给一次清理机会。

        ``reason`` 只进日志（停用 / 卸载 / 重载）；没有注册回调或没跑过
        ``on_load`` 时不产生任何动作。异常只记日志，不阻断后续流程。
        """
        if not plugin.lifecycle_active:
            return
        if not self._hooks_allowed(plugin):
            self._audit_skip(plugin, "卸载回调 on_unload")
            plugin.lifecycle_active = False
            return
        context = PluginContext(plugin, self)
        plugin.lifecycle_active = False
        self._run_lifecycle(plugin, plugin.unload_hooks, "on_unload", context)
        logger.info("插件 %s 已执行 on_unload（%s）", plugin.name, reason)

    def _audit_skip(self, plugin: Plugin, action: str) -> None:
        """未授权导致的钩子跳过留一条审计，和命令/钩子分发保持同一口径。"""
        store = getattr(self, "permissions", None)
        if store is not None:
            store.audit_denial(plugin.name, "network", action)

    def config_change_reason(self, name: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[bool, bool, str]:
        """预览一次配置变更的处理方式，返回 ``(能否热生效, 是否真的变了, 说明)``。

        WebUI 先用它决定要不要调 ``on_config_change``，以及响应里怎么写：
        声明了钩子的插件算能热生效；没声明的如实说需要重启，不假装更新。
        """
        plugin = self.find_plugin(name)
        changed = _diff_keys(old, new)
        if plugin is None:
            return False, bool(changed), "插件未加载，配置保存后需重启生效"
        if not changed:
            # 值没变：不管插件声不声明钩子都不用重启
            return True, False, "配置没有变化"
        if not self._hooks_allowed(plugin):
            return False, True, "插件未获网络授权，配置未热应用，请确认声明后停用再启用"
        if not plugin.config_change_hooks:
            return False, True, "该插件不支持热更新配置，已写入磁盘，重启 bot 后生效"
        if not plugin.enabled or not plugin.loaded:
            return False, True, "插件当前未启用，配置已保存，启用后生效"
        return True, True, ""

    def apply_config_change(
        self, name: str, old: Mapping[str, Any], new: Mapping[str, Any]
    ) -> tuple[bool, str]:
        """把一次插件配置变更热应用给插件，返回 ``(是否热生效, 说明)``。

        ``old`` / ``new`` 是插件 ``config.yaml`` 的键值快照；宿主只调插件
        自己声明的 ``on_config_change(ctx, old, new)``，不替它猜哪些参数能
        在线改。插件没声明钩子、没启用或未获授权时如实返回「需重启 / 未生效」。
        """
        hot, changed, reason = self.config_change_reason(name, old, new)
        if not changed:
            return True, reason or "配置没有变化"
        if not hot:
            plugin = self.find_plugin(name)
            if plugin is not None and not self._hooks_allowed(plugin):
                # 与钩子分发同一口径：未授权导致的跳过也留审计
                self._audit_skip(plugin, "配置变更 on_config_change")
            logger.info("插件 %s 配置变更未热应用：%s", name, reason)
            return False, reason
        plugin = self.find_plugin(name)
        if plugin is None:  # 理论上不会发生：config_change_reason 已确认找到
            return False, reason or "插件未加载"
        context = PluginContext(plugin, self)
        logger.info("插件 %s 配置变更，调用 on_config_change", name)
        self._run_lifecycle(
            plugin,
            plugin.config_change_hooks,
            "on_config_change",
            context,
            dict(old),
            dict(new),
        )
        return True, "插件配置已热生效"

    def find_plugin(self, name: str) -> Plugin | None:
        """按目录名找插件；找不到再按 manifest id 找（WebUI 传哪个都能定位）。"""
        plugin = self.plugins.get(name)
        if plugin is not None:
            return plugin
        for candidate in self.plugins.values():
            if candidate.manifest is not None and candidate.manifest.id == name:
                return candidate
        return None

    def _snapshot(self, plugin: Plugin) -> dict[str, Any]:
        """热重载前的注册现场：失败时原样放回去，旧实例继续跑。"""
        return {
            "path": plugin.path,
            "manifest": plugin.manifest,
            "invalid_reason": plugin.invalid_reason,
            "core_too_old": plugin.core_too_old,
            "loaded": plugin.loaded,
            "lifecycle_active": plugin.lifecycle_active,
            "message_hooks": list(plugin.message_hooks),
            "before_reply_hooks": list(plugin.before_reply_hooks),
            "after_reply_hooks": list(plugin.after_reply_hooks),
            "commands": dict(plugin.commands),
            "capability_notes": list(plugin.capability_notes),
            "start_hooks": list(plugin.start_hooks),
            "stop_hooks": list(plugin.stop_hooks),
            "load_hooks": list(plugin.load_hooks),
            "unload_hooks": list(plugin.unload_hooks),
            "config_change_hooks": list(plugin.config_change_hooks),
        }

    @staticmethod
    def _restore(plugin: Plugin, saved: Mapping[str, Any]) -> None:
        """把 :meth:`_snapshot` 的现场写回去（热重载回滚用）。"""
        for key, value in saved.items():
            setattr(plugin, key, value)

    @staticmethod
    def _clear_registrations(plugin: Plugin) -> None:
        """清空上一次 ``register`` 留下的注册，避免热重载后钩子翻倍。

        生命周期回调也一并清空：``register`` 重新声明它们；如果新代码不再
        声明某项（比如去掉了 ``on_config_change``），旧回调不能残留。
        """
        plugin.message_hooks.clear()
        plugin.before_reply_hooks.clear()
        plugin.after_reply_hooks.clear()
        plugin.commands.clear()
        plugin.capability_notes.clear()
        plugin.start_hooks.clear()
        plugin.stop_hooks.clear()
        plugin.load_hooks.clear()
        plugin.unload_hooks.clear()
        plugin.config_change_hooks.clear()

    def reload_plugin(self, name: str) -> dict[str, Any]:
        """热重载一个插件：on_unload → 校验 manifest → 重新 register → on_load。

        授权记录与插件自己的 ``config.yaml`` 都不动；任何一步失败都回滚到
        重载前的实例（旧钩子、旧后台任务照常），抛 :class:`PluginReloadError`
        让 WebUI 把原因带回去。``name`` 既可以是插件目录名，也可以是 manifest
        里的 ``id``（WebUI 配置页按 id 定位插件）。未启用的插件只做
        「重新发现 + 校验」，不跑钩子。
        """
        plugin = self.find_plugin(name)
        if plugin is None:
            raise KeyError(name)
        name = plugin.name
        was_enabled = plugin.enabled
        was_loaded = plugin.loaded
        saved = self._snapshot(plugin)
        logger.info("热重载插件 %s 开始（启用=%s，已加载=%s）", name, was_enabled, was_loaded)
        try:
            if was_loaded:
                # 先让旧实例收尾：后台任务停下、生命周期资源释放后再 import 新代码
                if was_enabled:
                    self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
                self._run_unload(plugin, "热重载")
                self._refresh_manifest(plugin)
                if not plugin.valid:
                    raise ManifestError(
                        plugin.invalid_reason or "manifest 不合规",
                        core_too_old=plugin.core_too_old,
                    )
                self._load_plugin(plugin)
            else:
                # 未加载的插件只刷新发现结果与 manifest，不执行插件代码
                self._refresh_manifest(plugin)
                if not plugin.valid:
                    raise ManifestError(
                        plugin.invalid_reason or "manifest 不合规",
                        core_too_old=plugin.core_too_old,
                    )
        except BaseException as exc:
            self._restore(plugin, saved)
            reason = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
            logger.error("热重载插件 %s 失败，已回滚到重载前的实例：%s", name, reason)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # 回滚后旧实例若本来就在跑：先补一次 on_load 重新武装它（卸载
            # 回调可能已经释放了资源），再恢复后台任务。生命周期标记随快照还原。
            if saved["loaded"] and was_enabled:
                if saved["lifecycle_active"]:
                    self._run_load(plugin)
                self._start_lifecycle(plugin)
            raise PluginReloadError(name, reason) from exc
        logger.info("热重载插件 %s 成功", name)
        return {"name": name, "enabled": plugin.enabled, "loaded": plugin.loaded}

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
                # 声明与授权的逐项对照，供 WebUI 只读展示
                "permissions": self.permissions.summary(
                    p.name, p.manifest.permissions if p.manifest is not None else ()
                ),
                "commands": sorted(p.commands),
                "hooks": {
                    "on_message": len(p.message_hooks),
                    "on_before_reply": len(p.before_reply_hooks),
                    "on_after_reply": len(p.after_reply_hooks),
                    "on_start": len(p.start_hooks),
                    "on_stop": len(p.stop_hooks),
                    "on_load": len(p.load_hooks),
                    "on_unload": len(p.unload_hooks),
                    "on_config_change": len(p.config_change_hooks),
                },
                "lifecycle": p.lifecycle_active,
                # 插件是否声明了 on_config_change：WebUI 据此提示配置能否热生效
                "hot_config": bool(p.config_change_hooks),
                "path": str(p.path),
            }
            for p in sorted(self.plugins.values(), key=lambda item: item.name)
        ]

    def set_permissions(self, name: str, permissions: Iterable[str]) -> frozenset[str]:
        """整表替换某插件的授权，返回最新授权集合。

        只能授出 manifest 声明过的权限；未声明的一律拒绝。授权变化立即生效：
        撤掉 ``scheduler`` / ``network`` / ``llm`` 导致后台任务不再满足启动
        条件时会触发 ``on_stop``，让任务立即停下；重新满足条件时补一次
        ``on_start``。撤掉 ``network`` 还会先给插件一次 ``on_unload``，恢复
        时补 ``on_load``（与钩子分发同一口径，插件启用中才补）。其它权限在
        调用入口即时生效，已注册的钩子不用重载。
        """
        plugin = self.plugins.get(name)
        if plugin is None:
            raise KeyError(name)
        if not plugin.valid or plugin.manifest is None:
            raise ValueError(plugin.invalid_reason or "manifest 不合规")
        declared = plugin.manifest.permissions
        had_start = self._scheduler_allowed(plugin)
        had_hooks = self._hooks_allowed(plugin)
        grants = self.permissions.set_permissions(name, permissions, declared=declared)
        has_hooks = self._hooks_allowed(plugin)
        # 钩子准入随 network 授权变化：撤回后先跑卸载回调；恢复后补一次
        # 加载回调，让插件重新拿到「已加载」状态（停用期间不重建资源，真正
        # 启用时会补 on_load）。
        if had_hooks and not has_hooks and plugin.loaded:
            self._run_unload(plugin, "授权撤回")
        elif (
            not had_hooks
            and has_hooks
            and plugin.loaded
            and plugin.enabled
            and not plugin.lifecycle_active
        ):
            self._run_load(plugin)
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

    def shutdown(self) -> None:
        """停机：给所有已加载插件一次 ``on_unload``（宿主进程退出前的收尾）。

        只调卸载回调用不着停用插件：``on_stop`` 管的是后台任务，进程退出时
        由事件循环统一取消。已经跑过 ``on_unload`` 的插件（比如运行中被停用）
        不重复调用：``lifecycle_active`` 为假的插件本来就没有待清理的生命周期
        资源。同步回调当场执行；异步回调交给事件循环，进程随后退出时可能被
        取消，需要保证执行的清理请用同步回调。异常只记日志。
        """
        for plugin in list(self.plugins.values()):
            if plugin.loaded and plugin.lifecycle_active:
                self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
                self._run_unload(plugin, "停机")

    def unload_plugin(self, name: str) -> bool:
        """卸载前调用：给插件一次 ``on_unload`` 并停止后台任务，返回是否找到。

        市场删目录前先走这里，插件才有机会关掉定时任务、后台线程与临时文件。
        ``name`` 支持目录名或 manifest id；未加载的插件不产生动作。清理完顺手
        清空注册，插件对象即使因为删目录失败而残留也不会继续被分发。
        """
        plugin = self.find_plugin(name)
        if plugin is None:
            return False
        if plugin.loaded and plugin.enabled:
            self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
        self._run_unload(plugin, "卸载")
        self._clear_registrations(plugin)
        plugin.loaded = False
        return True

    def set_enabled(self, name: str, enabled: bool, *, trust: bool = True) -> bool:
        """运行时启用/禁用插件，返回是否找到该插件。

        禁用只是让分发时跳过它（已注册的钩子留着，再次启用立即生效）；
        启用一个加载时被跳过的插件时，会现场执行它的 ``register``。
        禁用/启用时会同步调用插件的 ``on_stop`` / ``on_unload`` 与
        ``on_start``，后台任务据此启动和干净退出。manifest 不合规的插件不能启用。

        显式启用先授予本地声明；市场恢复状态时传 ``trust=False`` 保留原实授。
        这个开关会同步更新内存里 ``config.plugins.enabled``；WebUI 调用它
        之后负责把列表写回 config.yaml。
        """
        plugin = self.plugins.get(name)
        if plugin is None:
            return False
        if enabled and trust and not plugin.enabled:
            try:
                self.trust_for_enable(plugin)
            except ManifestError as exc:
                self._reject(plugin, exc.reason)
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
                # 停用那一轮已经收过 on_unload；重新启用时补一次 on_load 让插件
                # 重建生命周期资源（send/register 都没有再跑，钩子仍是旧的）
                if plugin.loaded and not plugin.lifecycle_active:
                    self._run_load(plugin)
                self._start_lifecycle(plugin)
            elif not enabled and was_enabled:
                # 停用：先收后台任务，再给插件 on_unload 清理生命周期资源
                self._run_lifecycle(plugin, plugin.stop_hooks, "on_stop")
                self._run_unload(plugin, "停用")
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
    def _run_lifecycle(
        plugin: Plugin,
        hooks: list[Callable[..., Any]],
        label: str,
        *args: Any,
    ) -> None:
        """同步调用生命周期回调；协程回调有事件循环时调度，否则丢弃并记日志。

        ``args`` 原样传给回调（``on_load(ctx, manifest)`` 等）；同步回调当场
        执行，异步回调交给事件循环。单个回调失败只记日志、继续下一个，失败隔离。
        """
        for handler in list(hooks):
            try:
                result = handler(*args)
            except Exception as exc:  # noqa: BLE001 - 回调失败不能拖垮宿主
                logger.warning("插件 %s 的 %s 回调异常：%s", plugin.name, label, exc)
                logger.debug("插件 %s 的 %s 回调堆栈", plugin.name, label, exc_info=True)
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
