"""命令行入口：``rubenbot -c config.yaml``。

同时支持 ``python -m rubenbot``。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .adapters.console import ConsoleAdapter
from .adapters.snowluma import SnowLumaAdapter
from .config import Config
from .core import RubenBot
from .llm import LLMClient
from .memory.store import MemoryStore
from .persona import DEFAULT_PERSONA, load_persona
from .plugins.loader import PluginManager


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_adapters(config: Config):
    """按配置实例化启用的适配器。"""
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
        else:
            logging.warning("未知适配器：%s（可用：console / snowluma）", name)
    return adapters


def build_bot(config: Config) -> tuple[RubenBot, LLMClient, MemoryStore]:
    store = MemoryStore(config.database_path)
    llm = LLMClient(config.llm)
    persona = load_persona(config.persona_path)
    plugins = PluginManager(config, store)
    try:
        plugins.load_all()
    except Exception as exc:  # noqa: BLE001 - 插件加载失败不阻塞启动
        logging.warning("插件加载失败：%s", exc)
    bot = RubenBot(config, store, llm, persona, plugins=plugins)
    for adapter in build_adapters(config):
        bot.register_adapter(adapter)
    return bot, llm, store


async def _run(config: Config) -> None:
    bot, llm, store = build_bot(config)
    if not bot.adapters:
        logging.error("没有启用任何适配器，请在配置里设置 adapters.console: {} 或 adapters.snowluma")
        return
    try:
        await bot.run()
    finally:
        await bot.shutdown()
        await llm.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rubenbot", description="RubenBot：以记忆为核心的拟人聊天 bot")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--console", action="store_true", help="强制启用 ConsoleAdapter")
    parser.add_argument("--init-persona", action="store_true", help="把默认人格写到配置的路径后退出")
    parser.add_argument("--log-level", default=None, help="日志级别，覆盖配置")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    setup_logging(args.log_level or config.log_level)

    if args.init_persona:
        from pathlib import Path

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

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        print("\n再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
