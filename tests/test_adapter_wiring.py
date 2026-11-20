"""适配器装配线：配置在场时 build_adapters 能装出对应适配器。"""

from __future__ import annotations

import logging

from rubenbot.__main__ import build_adapters
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.adapters.qq_official import QQOfficialAdapter
from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.adapters.telegram import TelegramAdapter
from rubenbot.config import Config


def test_build_console_and_snowluma():
    config = Config.from_dict(
        {
            "adapters": {
                "console": {},
                "snowluma": {"ws_url": "ws://127.0.0.1:3001", "self_id": 10000},
            }
        }
    )
    adapters = build_adapters(config)
    by_name = {adapter.name: adapter for adapter in adapters}
    assert isinstance(by_name["console"], ConsoleAdapter)
    assert isinstance(by_name["snowluma"], SnowLumaAdapter)
    assert by_name["snowluma"].self_id == "10000"


def test_build_telegram_when_configured():
    config = Config.from_dict(
        {
            "adapters": {
                "telegram": {
                    "bot_token": "123:abc",
                    "poll_timeout": 12,
                    "allowed_chat_ids": [-100123, "42"],
                    "offset_file": "data/tg.json",
                }
            }
        }
    )
    adapters = build_adapters(config)
    assert len(adapters) == 1
    telegram = adapters[0]
    assert isinstance(telegram, TelegramAdapter)
    assert telegram.bot_token == "123:abc"
    assert telegram.poll_timeout == 12
    assert telegram.allowed_chat_ids == {"-100123", "42"}
    assert str(telegram.offset_file) == "data/tg.json"


def test_build_telegram_defaults_and_no_offset_file():
    config = Config.from_dict({"adapters": {"telegram": {"bot_token": "x"}}})
    (telegram,) = build_adapters(config)
    assert isinstance(telegram, TelegramAdapter)
    assert telegram.poll_timeout == 30.0
    # 留空 offset_file 表示不落盘
    config = Config.from_dict(
        {"adapters": {"telegram": {"bot_token": "x", "offset_file": ""}}}
    )
    (telegram,) = build_adapters(config)
    assert telegram.offset_file is None


def test_build_qq_official_when_configured():
    config = Config.from_dict(
        {
            "adapters": {
                "qq_official": {
                    "app_id": "APPID",
                    "client_secret": "SECRET",
                    "sandbox": True,
                    "download_images": False,
                }
            }
        }
    )
    adapters = build_adapters(config)
    assert len(adapters) == 1
    qq = adapters[0]
    assert isinstance(qq, QQOfficialAdapter)
    assert qq.app_id == "APPID"
    assert qq.client_secret == "SECRET"
    assert qq.sandbox is True
    assert qq.api_base == "https://sandbox.api.sgroup.qq.com"
    assert qq.download_images is False


def test_unknown_adapter_still_warns(caplog):
    config = Config.from_dict({"adapters": {"mystery": {}}})
    with caplog.at_level(logging.WARNING):
        adapters = build_adapters(config)
    assert adapters == []
    assert any("未知适配器" in record.message for record in caplog.records)


def test_disabled_adapter_is_skipped():
    config = Config.from_dict(
        {"adapters": {"telegram": None, "qq_official": False, "console": {}}}
    )
    adapters = build_adapters(config)
    assert [adapter.name for adapter in adapters] == ["console"]
