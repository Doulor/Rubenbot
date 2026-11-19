"""WebUI 与 bot 之间的运行时状态。

WebUI 和 bot 同进程，：class:`WebState` 是两者共享的一个小对象：
bot 把自己的实例、配置路径、启动时间放进来，API 只读它，不反向依赖
``rubenbot.core`` 之外的任何东西（保留 ``bot=None`` 供测试和独立预览）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..memory.store import MemoryStore
from ..plugins.loader import PluginManager
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

    @classmethod
    def from_bot(cls, bot: Any, config_path: str | Path = "config.yaml") -> "WebState":
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

    def plugin_list(self) -> list[dict[str, Any]]:
        if self.plugins is not None:
            return self.plugins.list_plugins()
        return []

    def restart_bot(self) -> bool:
        """请求 bot 停机；同一进程里的主流程会在 run() 返回后走 shutdown。

        注意：这里只请求停机，不负责拉起新进程（v1 由外部进程管理器重启）。
        """
        bot = self.bot
        if bot is None or not hasattr(bot, "stop"):
            return False
        bot.stop()
        return True

    def shutdown(self) -> None:
        """关闭 WebUI 时清理自己挂的东西（不关 store，store 归 bot 管）。"""
        self.extras.clear()


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


def install_log_capture() -> None:
    """挂上 WebUI 的日志环形缓冲。"""
    install()


async def wait_forever() -> None:
    """占位：独立运行 WebUI 时阻塞用。"""
    await asyncio.Event().wait()
