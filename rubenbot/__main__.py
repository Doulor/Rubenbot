"""命令行入口：``rubenbot -c config.yaml``。

同时支持 ``python -m rubenbot``。WebUI（第二阶段）与 bot 同进程：
配置里 ``webui.enabled: true``（默认）时随 bot 一起拉起。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .adapters.console import ConsoleAdapter
from .adapters.qq_official import QQOfficialAdapter
from .adapters.snowluma import SnowLumaAdapter
from .adapters.telegram import TelegramAdapter
from .config import Config, QQOfficialConfig, TelegramConfig
from .core import RubenBot
from .llm import LLMClient, make_usage_recorder
from .memory.store import MemoryStore
from .persona import DEFAULT_PERSONA, load_persona
from .plugins.loader import PluginManager
from .web.logbuffer import install as install_log_buffer

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # WebUI 的「日志」页从这个环形缓冲读，挂不上也不影响运行
    try:
        install_log_buffer()
    except Exception:  # noqa: BLE001
        pass


def build_adapters(config: Config):
    """按配置实例化启用的适配器。

    段内字段先用 ``config.py`` 里的强类型视图归一（类型不对回落默认），
    缺省值仍集中在一处；凭证没填的适配器也会装配出来，由适配器自己在
    ``start`` 时记一条明确的错误日志，不在这里静默吞掉。
    """
    adapters = []
    for name, opts in config.adapters.items():
        if opts is None or opts is False:
            continue
        opts = opts if isinstance(opts, dict) else {}
        if name == "console":
            adapters.append(ConsoleAdapter(bot_name=config.chat.bot_name))
        elif name == "snowluma":
            adapters.append(
                SnowLumaAdapter(
                    ws_url=opts.get("ws_url", "ws://127.0.0.1:3001"),
                    http_url=opts.get("http_url", "http://127.0.0.1:3000"),
                    access_token=opts.get("access_token", ""),
                    self_id=opts.get("self_id"),
                )
            )
        elif name == "telegram":
            telegram = TelegramConfig.from_options(opts)
            adapters.append(
                TelegramAdapter(
                    bot_token=telegram.bot_token,
                    poll_timeout=telegram.poll_timeout,
                    allowed_chat_ids=telegram.allowed_chat_ids,
                    offset_file=telegram.offset_file or None,
                )
            )
        elif name == "qq_official":
            qq = QQOfficialConfig.from_options(opts)
            adapters.append(
                QQOfficialAdapter(
                    app_id=qq.app_id,
                    client_secret=qq.client_secret,
                    sandbox=qq.sandbox,
                    download_images=qq.download_images,
                )
            )
        else:
            logging.warning(
                "未知适配器：%s（可用：console / snowluma / telegram / qq_official）", name
            )
    return adapters


def build_bot(config: Config) -> tuple[RubenBot, LLMClient, MemoryStore]:
    store = MemoryStore(config.database_path)
    llm = LLMClient(config.llm, usage_recorder=make_usage_recorder(store))
    persona = load_persona(config.persona_path, **config.persona_example_options())
    plugins = PluginManager(config, store)
    try:
        loaded = plugins.load_all()
        # 插件默认全关：没启用任何插件是正常情况，启动时说清楚
        logging.info(
            "插件：共发现 %s 个，已启用 %s 个%s",
            len(plugins.plugins),
            len(loaded),
            f"（{', '.join(p.name for p in loaded)}）" if loaded else "（默认全部关闭，可在 WebUI 插件页开启）",
        )
    except Exception as exc:  # noqa: BLE001 - 插件加载失败不阻塞启动
        logging.warning("插件加载失败：%s", exc)
    bot = RubenBot(config, store, llm, persona, plugins=plugins)
    for adapter in build_adapters(config):
        bot.register_adapter(adapter)
    return bot, llm, store


async def _run(config: Config, config_path: str = "config.yaml", *, no_webui: bool = False) -> None:
    bot, llm, store = build_bot(config)
    webui = None
    if config.webui.enabled and not no_webui:
        webui = await _start_webui(bot, config_path, webui_only=False)
    if not bot.adapters:
        logging.error(
            "没有启用任何适配器，请在配置里设置 adapters.console: {} 或 adapters.snowluma / "
            "adapters.telegram / adapters.qq_official"
        )
        if webui is not None:
            logging.info("仍然启动 WebUI：http://%s:%s（bot 无适配器）", webui.host, webui.port)
            try:
                await asyncio.Event().wait()
            finally:
                await webui.stop()
                await llm.close()
                store.close()
        return
    try:
        await bot.run()
    finally:
        if webui is not None:
            await webui.stop()
        await bot.shutdown()
        await llm.close()
        store.close()


async def _start_webui(bot: RubenBot, config_path: str, *, webui_only: bool = False):
    """按配置拉起 WebUI；失败只警告，不阻塞 bot 本身。"""
    from .web.auth import BindSecurityError
    from .web.server import start_webui
    from .web.state import WebState

    try:
        state = WebState.from_bot(bot, config_path, webui_only=webui_only)
        return await start_webui(state)
    except BindSecurityError as exc:
        # 安全底线：拒绝启动而不是悄悄改绑
        logging.error("WebUI 拒绝启动：%s", exc)
        raise
    except ImportError as exc:
        logging.warning("WebUI 未启动（缺少可选依赖，可 pip install 'rubenbot[webui]'）：%s", exc)
    except Exception as exc:  # noqa: BLE001
        logging.warning("WebUI 启动失败：%s", exc)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rubenbot", description="RubenBot：以记忆为核心的拟人聊天 bot")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--console", action="store_true", help="强制启用 ConsoleAdapter")
    parser.add_argument("--init-persona", action="store_true", help="把默认人格写到配置的路径后退出")
    parser.add_argument("--no-webui", action="store_true", help="本次不启动 WebUI（覆盖配置）")
    parser.add_argument(
        "--webui-only",
        action="store_true",
        help="只启动 WebUI（不连任何适配器），调试界面用",
    )
    parser.add_argument("--log-level", default=None, help="日志级别，覆盖配置")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    setup_logging(args.log_level or config.log_level)

    if args.init_persona:
        path = Path(config.persona_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            print(f"人格文件已存在：{path}")
        else:
            path.write_text(DEFAULT_PERSONA, encoding="utf-8")
            print(f"已写入默认人格：{path}")
        return 0

    if args.console:
        config.adapters["console"] = config.adapters.get("console") or {}

    if args.webui_only:
        from .web.auth import BindSecurityError
        from .web.server import run_webui_standalone
        from .web.state import WebState

        store = MemoryStore(config.database_path)
        state = WebState(
            config=config,
            store=store,
            config_path=Path(args.config),
            plugins=None,
            signer=None,
            webui_only=True,
        )
        from .web.auth import SessionSigner

        state.signer = SessionSigner(config.webui.password, ttl_hours=config.webui.session_hours)
        try:
            asyncio.run(run_webui_standalone(state))
        except BindSecurityError as exc:
            print(f"WebUI 拒绝启动：{exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n再见。")
        finally:
            store.close()
        return 0

    try:
        asyncio.run(_run(config, args.config, no_webui=args.no_webui))
    except KeyboardInterrupt:
        print("\n再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
