"""WebUI 服务器的启动与关闭（uvicorn，可选依赖）。

由主程序在 bot 启动时按配置拉起：``start_webui(state)`` 返回一个可以
``stop()`` 的服务对象；WebUI 与 bot 共用同一个事件循环、同一进程。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..config import WebUIConfig
from .auth import BindSecurityError, check_bind_security
from .state import WebState

logger = logging.getLogger(__name__)


@dataclass
class WebUIServer:
    """已经跑起来的 WebUI 服务句柄。"""

    host: str
    port: int
    server: object  # uvicorn.Server
    task: asyncio.Task

    async def stop(self) -> None:
        server = self.server
        server.should_exit = True  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭 WebUI 异常：%s", exc)


async def start_webui(state: WebState) -> WebUIServer:
    """在后台任务里启动 uvicorn，返回句柄。

    依赖缺失时抛出 ImportError，调用方决定是否降级；绑定安全校验失败时
    抛出 :class:`BindSecurityError`，**不会**悄悄改绑本地地址。
    """
    config: WebUIConfig = state.config.webui
    check_bind_security(config)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise ImportError("WebUI 需要安装可选依赖：pip install 'rubenbot[webui]'") from exc

    from .app import create_app
    from .logbuffer import install

    install()
    app = create_app(state)
    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=int(config.port),
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(uv_config)
    task = asyncio.create_task(server.serve(), name="rubenbot-webui")
    # 等 server 真正开始监听（或启动失败），避免调用方以为成功了
    for _ in range(200):
        if getattr(server, "started", False):
            break
        if task.done():
            exc = task.exception()
            raise RuntimeError(f"WebUI 启动失败：{exc}") from exc
        await asyncio.sleep(0.01)
    logger.info("WebUI 已启动：http://%s:%s", config.host, config.port)
    return WebUIServer(host=config.host, port=int(config.port), server=server, task=task)


async def run_webui_standalone(state: WebState) -> None:
    """只跑 WebUI（调试用），直到 Ctrl+C。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError("WebUI 需要安装可选依赖：pip install 'rubenbot[webui]'") from exc
    from .app import create_app
    from .logbuffer import install

    check_bind_security(state.config.webui)
    install()
    app = create_app(state)
    config = uvicorn.Config(
        app,
        host=state.config.webui.host,
        port=int(state.config.webui.port),
        log_level="info",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


__all__ = ["WebUIServer", "start_webui", "run_webui_standalone", "BindSecurityError"]
