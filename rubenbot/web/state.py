"""WebUI 与 bot 之间的运行时状态。

WebUI 和 bot 同进程，：class:`WebState` 是两者共享的一个小对象：
bot 把自己的实例、配置路径、启动时间放进来，API 只读它，不反向依赖
``rubenbot.core`` 之外的任何东西（保留 ``bot=None`` 供测试和独立预览）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..memory.store import MemoryStore
from ..persona import Persona, load_persona
from ..plugins.loader import PluginManager
from ..stickers import StickerStore
from .auth import SessionSigner
from .logbuffer import install

logger = logging.getLogger(__name__)


@dataclass
class WebState:
    """一次 WebUI 进程内共享的状态。"""

    config: Config
    store: MemoryStore
    config_path: Path = Path("config.yaml")
    bot: Any = None
    plugins: PluginManager | None = None
    started_at: float = field(default_factory=time.time)
    signer: SessionSigner | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    # 启动参数与可执行文件，供「立即重启」原地 execv 使用；测试里可替换
    argv: list[str] = field(default_factory=lambda: list(sys.argv))
    executable: str = field(default_factory=lambda: sys.executable or "python")
    # 是否处于 --webui-only 模式（重启响应里给前端提示用）
    webui_only: bool = False

    @classmethod
    def from_bot(
        cls,
        bot: Any,
        config_path: str | Path = "config.yaml",
        *,
        argv: list[str] | None = None,
        webui_only: bool = False,
    ) -> "WebState":
        """从运行中的 :class:`~rubenbot.core.RubenBot` 构造状态。"""
        config: Config = bot.config
        return cls(
            config=config,
            store=bot.store,
            config_path=Path(config_path),
            bot=bot,
            plugins=bot.plugins,
            started_at=getattr(bot, "started_at", time.time()),
            signer=SessionSigner(config.webui.password, ttl_hours=config.webui.session_hours),
            argv=list(argv) if argv is not None else list(sys.argv),
            webui_only=webui_only,
        )

    # ------------------------------------------------------------------
    # 便捷读取
    # ------------------------------------------------------------------

    @property
    def bot_name(self) -> str:
        return self.config.chat.bot_name

    @property
    def auth_required(self) -> bool:
        return bool(self.signer and self.signer.auth_required)

    def uptime_seconds(self) -> float:
        started = self.started_at
        if self.bot is not None:
            started = getattr(self.bot, "started_at", started)
        return max(0.0, time.time() - started)

    def is_running(self) -> bool:
        """bot 是否在跑：有 bot 实例且没被 stop。"""
        bot = self.bot
        if bot is None:
            return False
        stop_event = getattr(bot, "_stop", None)
        if stop_event is not None and hasattr(stop_event, "is_set"):
            return not stop_event.is_set()
        return True

    def adapters_status(self) -> list[dict[str, Any]]:
        bot = self.bot
        adapters = getattr(bot, "adapters", {}) if bot is not None else {}
        configured = set(self.config.adapters)
        names = sorted(set(adapters) | configured)
        result = []
        for name in names:
            adapter = adapters.get(name)
            item: dict[str, Any] = {
                "name": name,
                "enabled": name in configured or adapter is not None,
                "running": adapter is not None,
                "detail": "",
            }
            if name == "snowluma" and adapter is not None:
                item["detail"] = getattr(adapter, "ws_url", "")
                item["reconnects"] = getattr(adapter, "reconnect_count", 0)
            result.append(item)
        return result

    def recent_decisions(self, *, limit: int = 8, chat_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_decision_logs(limit=limit, chat_id=chat_id)

    def sticker_store(self) -> StickerStore:
        """返回与运行中 bot 共享的表情库实例。"""
        if self.bot is not None and hasattr(self.bot, "stickers"):
            return self.bot.stickers
        store = self.extras.get("sticker_store")
        if store is None:
            store = StickerStore(self.config.stickers.dir)
            self.extras["sticker_store"] = store
        return store

    def plugin_list(self) -> list[dict[str, Any]]:
        """插件列表：有插件管理器时用它的实时状态，否则只按配置与目录展示。

        WebUI 独立模式（``--webui-only``）没有运行中的插件管理器，这里
        仍然扫描插件目录，让设置页能列出可启用的插件。
        """
        if self.plugins is not None:
            # 顺手重新扫描一次目录：新放进去的插件不重启也能出现在列表里
            try:
                self.plugins.discover()
            except OSError as exc:  # noqa: BLE001 - 目录读不了不影响已有列表
                logger.warning("扫描插件目录失败：%s", exc)
            return self.plugins.list_plugins()
        from ..plugins.loader import discover_plugin_dirs
        from ..plugins.manifest import read_manifest

        allow = set(self.config.plugins.enabled)
        items = []
        for name, path in discover_plugin_dirs(self.config.plugins.dir):
            manifest, reason, core_too_old = read_manifest(path)
            items.append(
                {
                    "name": name,
                    "enabled": name in allow,
                    "loaded": False,
                    "valid": manifest is not None,
                    "invalid_reason": reason,
                    "core_too_old": core_too_old,
                    "manifest": manifest.as_dict() if manifest is not None else None,
                    "commands": [],
                    "hooks": {"on_message": 0, "on_before_reply": 0, "on_after_reply": 0},
                    "path": str(path),
                }
            )
        return items

    def set_plugin_enabled(self, name: str, enabled: bool) -> bool:
        """运行时启用/停用插件，并把启用列表同步到内存里的配置。

        插件框架支持热卸载（停用触发 ``on_stop``），因此这里无需重启；
        写回 config.yaml 由调用方负责。找不到插件或启用失败返回 False。
        """
        manager = self.plugins
        if manager is None:
            return False
        if not manager.set_enabled(name, enabled):
            return False
        names = self.enabled_plugin_names(manager)
        self.config.plugins.enabled = names
        # 管理器持有的配置可能是旧对象（没有运行中 bot 时不会走 bot.apply_config）
        manager.config.plugins.enabled = list(names)
        bot = self.bot
        bot_config = getattr(bot, "config", None)
        if bot_config is not None and bot_config is not self.config:
            bot_config.plugins.enabled = list(names)
        return True

    def enabled_plugin_names(self, manager=None) -> list[str]:
        """按用户原有顺序整理启用列表，只保留真正处于启用状态的插件。

        ``set_enabled`` 只增删单个名字，这里以 ``self.config`` 里的顺序为准
        （尽量不改动用户手写的顺序），再补上运行时启用但列表里漏掉的插件。
        """
        manager = manager or self.plugins
        if manager is None:
            return list(self.config.plugins.enabled)
        actual = {plugin.name for plugin in manager.enabled_plugins}
        names = [name for name in self.config.plugins.enabled if name in actual]
        for plugin in manager.enabled_plugins:
            if plugin.name not in names:
                names.append(plugin.name)
        return names

    def usage_summary(self, *, days: int = 7) -> dict[str, Any]:
        """近 ``days`` 天的 LLM 用量聚合：总量 + 按 task / model 分组。"""
        days = max(1, int(days))
        since = time.time() - days * 86400
        return {
            "days": days,
            "since": since,
            "total": self.store.usage_totals(since=since),
            "by_task": self.store.usage_by_task(since=since),
            "by_model": self.store.usage_by_model(since=since),
        }

    # ------------------------------------------------------------------
    # 人设
    # ------------------------------------------------------------------

    def bot_persona(self) -> Persona | None:
        """运行中 bot 持有的人格对象；独立 WebUI 模式返回 None。"""
        persona = getattr(self.bot, "persona", None) if self.bot is not None else None
        return persona if isinstance(persona, Persona) else None

    def apply_persona(self, persona: Persona) -> bool:
        """把新人格换进运行中的 bot，后续消息立即使用。

        返回是否真的热加载了。``context.persona`` 是插件钩子拿到的那份，
        与 ``bot.persona`` 是同一对象引用，需要一起替换，避免插件看到旧人格。
        """
        bot = self.bot
        if bot is None or not hasattr(bot, "persona"):
            return False
        bot.persona = persona
        context = getattr(bot, "context", None)
        if context is not None and hasattr(context, "persona"):
            context.persona = persona
        return True

    def restart_bot(self) -> bool:
        """请求 bot 停机；同一进程里的主流程会在 run() 返回后走 shutdown。

        注意：这里只请求停机，不负责拉起新进程（v1 由外部进程管理器重启）。
        """
        bot = self.bot
        if bot is None or not hasattr(bot, "stop"):
            return False
        bot.stop()
        return True

    # ------------------------------------------------------------------
    # 配置热应用与重启
    # ------------------------------------------------------------------

    def apply_config(self, new_config: Config) -> list[str]:
        """把新配置热应用到运行中的组件，返回只能重启生效的字段列表。

        热生效范围：llm / memory / chat / decision / plugins 的运行时参数、
        日志级别、WebUI 口令与会话时长、background_check_minutes。
        数据库、适配器、persona 路径、WebUI 绑定与插件目录需要重启。
        """
        old = self.config
        restart = restart_fields(old, new_config)
        self.config = new_config
        # 记住尚未重启的字段：GET /api/config 会提示，重启后自然清空
        if restart:
            pending = self.extras.setdefault("pending_restart", [])
            for field in restart:
                if field not in pending:
                    pending.append(field)

        # 日志级别立即生效
        if old.log_level != new_config.log_level:
            logging.getLogger().setLevel(
                getattr(logging, new_config.log_level.upper(), logging.INFO)
            )

        # 口令/会话时长变了：重签会话，新口令立即生效（旧 Cookie 随之失效）
        if (
            self.signer is not None
            and (
                old.webui.password != new_config.webui.password
                or old.webui.session_hours != new_config.webui.session_hours
            )
        ):
            self.signer = SessionSigner(
                new_config.webui.password, ttl_hours=new_config.webui.session_hours
            )

        # 插件启停列表：显式启用制下，列表就是唯一来源；运行时同步（新增启用会现场加载）
        if self.plugins is not None and old.plugins.enabled != new_config.plugins.enabled:
            try:
                self.plugins.apply_enabled(new_config.plugins.enabled)
            except Exception as exc:  # noqa: BLE001 - 热应用失败不能让保存结果丢失
                logger.warning("插件启用列表热应用失败：%s", exc)

        # bot 在跑：交给它替换 LLM 客户端与各模块的配置引用
        bot = self.bot
        if bot is not None and hasattr(bot, "apply_config"):
            try:
                bot.apply_config(new_config, rebuild_llm=(old.llm != new_config.llm))
            except Exception as exc:  # noqa: BLE001 - 热应用失败不能让保存结果丢失
                logger.warning("热应用配置失败：%s", exc)
            if new_config.persona_path == old.persona_path:
                try:
                    bot.reload_persona(load_persona(new_config.persona_path, **new_config.persona_example_options()))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("重读人格文件失败：%s", exc)
        return restart

    def restart_command(self) -> list[str]:
        """构造原地重启的命令行（带上原启动参数）。"""
        executable = self.executable or "python"
        argv = list(self.argv) if self.argv else ["-m", "rubenbot"]
        first = argv[0]
        # ``python -m rubenbot`` 启动时 argv[0] 是 __main__.py 路径，直接执行会
        # 丢包上下文，改回 -m 形式
        if first.endswith("__main__.py") and Path(first).parent.name == "rubenbot":
            argv = ["-m", "rubenbot"] + argv[1:]
        elif Path(first).resolve() == Path(executable).resolve():
            argv = argv[1:]
        else:
            # 启动器脚本（例如 venv 里的 rubenbot）：优先用绝对路径保证能再找到
            resolved = shutil.which(first) if not Path(first).exists() else first
            if resolved:
                argv = [str(Path(resolved).resolve())] + argv[1:]
            else:
                argv = argv[1:]
        return [executable] + argv

    def request_restart(self, *, delay: float = 0.8) -> list[str]:
        """延迟片刻后以 ``os.execv`` 原地重启进程。

        测试或嵌入场景可以通过 ``extras["restart_hook"]`` 注入回调，
        只记录命令而不真正替换进程。
        """
        command = self.restart_command()
        hook = self.extras.get("restart_hook")
        if callable(hook):
            hook(command)
            return command

        def _exec() -> None:
            logger.info("按 WebUI 请求重启进程：%s", " ".join(command))
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:  # noqa: BLE001 - 刷盘失败也要继续重启
                pass
            os.execv(command[0], command)

        timer = threading.Timer(max(0.0, delay), _exec)
        timer.daemon = True
        timer.start()
        return command

    def shutdown(self) -> None:
        """关闭 WebUI 时清理自己挂的东西（不关 store，store 归 bot 管）。"""
        self.extras.clear()


def write_plugin_enabled(state: "WebState") -> Path | None:
    """把当前启用列表写回 config.yaml 的 ``plugins.enabled``。

    只动这一个键，保留其余内容与顺序；写盘沿用设置页的原子替换 + 备份。
    市场页的启用/停用/卸载也走这里，保证和插件开关是同一条写盘路径。
    """
    import yaml

    path = Path(state.config_path)
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("config.yaml 顶层不是键值对，无法写入")
    plugins = data.get("plugins")
    if plugins is None:
        plugins = {}
        data["plugins"] = plugins
    if not isinstance(plugins, dict):
        raise ValueError("config.yaml 里的 plugins 段格式不正确")
    manager = state.plugins
    if manager is None:
        names = list(state.config.plugins.enabled)
    else:
        names = state.enabled_plugin_names(manager)
        # 磁盘配置与内存里的管理器状态保持一致（管理器可能持有旧配置对象）
        state.config.plugins.enabled = list(names)
        manager.config.plugins.enabled = list(names)
    plugins["enabled"] = list(names)
    if "dir" not in plugins:
        plugins["dir"] = state.config.plugins.dir
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return save_config_text(path, text)


def restart_fields(old: Config, new: Config) -> list[str]:
    """比较两份配置，列出本次只能重启生效的字段。"""
    fields: list[str] = []
    if old.database_path != new.database_path:
        fields.append("database_path")
    if old.persona_path != new.persona_path:
        fields.append("persona.path")
    if old.plugins.dir != new.plugins.dir:
        fields.append("plugins.dir")
    for key in ("enabled", "host", "port"):
        if getattr(old.webui, key) != getattr(new.webui, key):
            fields.append(f"webui.{key}")
    for name in sorted(set(old.adapters) | set(new.adapters)):
        if _adapter_signature(name, old.adapters.get(name)) != _adapter_signature(
            name, new.adapters.get(name)
        ):
            fields.append(f"adapters.{name}")
    return fields


def _adapter_signature(name: str, options: Any) -> Any:
    """把适配器配置归一化后比较，避免「写了默认值」被误判为改动。"""
    enabled = options is not None and options is not False
    if str(name) != "snowluma":
        # 未知适配器只比较原样；console 的 ``{}`` 总是表示启用
        return enabled if str(name) == "console" else (enabled, options)
    opts = options if isinstance(options, dict) else {}
    return (
        enabled,
        opts.get("ws_url") or "ws://127.0.0.1:3001",
        opts.get("http_url") or "http://127.0.0.1:3000",
        opts.get("access_token") or "",
        "" if opts.get("self_id") in (None, "") else str(opts.get("self_id")),
    )


def describe_restart_fields(fields: list[str]) -> str:
    """把 restart_fields 的结果翻成人话，供保存响应展示。"""
    labels = {
        "database_path": "数据库路径",
        "persona.path": "人格文件路径",
        "plugins.dir": "插件目录",
        "plugins.enabled": "插件开关",
        "webui.enabled": "WebUI 启用状态",
        "webui.host": "WebUI 监听地址",
        "webui.port": "WebUI 端口",
    }
    parts = []
    for field in fields:
        if field.startswith("adapters."):
            parts.append(f"适配器 {field.split('.', 1)[1]}")
        else:
            parts.append(labels.get(field, field))
    return "、".join(parts)


def save_config_text(path: str | Path, text: str, *, backup: bool = True) -> Path | None:
    """保存设置页提交的 YAML 文本。

    先做语法与结构校验，通过后才写盘；写之前留一份 ``config.yaml.bak``。
    返回备份文件路径（首次保存没有旧文件时为 None）。
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 语法错误：{exc}") from exc
    if data is not None and not isinstance(data, dict):
        raise ValueError("配置的顶层必须是键值对（YAML mapping）")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and target.exists():
        backup_path = target.with_name(target.name + ".bak")
        backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return backup_path


def save_persona_text(path: str | Path, text: str, *, backup: bool = True) -> Path | None:
    """保存人格文件；写之前留一份 ``SOUL.md.bak``。

    与 :func:`save_config_text` 一样先写临时文件再原子替换，避免写一半
    被 bot 读到半截内容。返回备份文件路径（首次保存没有旧文件时为 None）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and target.exists():
        backup_path = target.with_name(target.name + ".bak")
        backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return backup_path


def install_log_capture() -> None:
    """挂上 WebUI 的日志环形缓冲。"""
    install()


async def wait_forever() -> None:
    """占位：独立运行 WebUI 时阻塞用。"""
    await asyncio.Event().wait()
