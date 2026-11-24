"""本群办事卡插件的配置校验与群作用域回放。"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import sys
from pathlib import Path

from rubenbot.adapters.base import MessageEvent
from rubenbot.plugins.loader import PluginManager


PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "group-faq" / "plugin.py"
spec = importlib.util.spec_from_file_location("group_faq_plugin", PLUGIN)
assert spec and spec.loader
faq = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = faq
spec.loader.exec_module(faq)


def card(group: str, title: str = "报销", body: str = "交 A 表", expiry: str = "") -> str:
    return "｜".join((group, title, body, expiry))


def test_expired_cards_are_excluded_and_group_isolation_is_strict():
    now = dt.datetime(2026, 11, 25, 12, tzinfo=dt.timezone.utc)
    values = [
        card("甲群", "现行", "交 A 表", "2026-11-26"),
        card("甲群", "过期", "交旧表", "2026-11-24"),
        card("乙群", "别群", "交 B 表", ""),
    ]

    visible = faq.active_cards(values, "甲群", now=now)
    assert [item.title for item in visible] == ["现行"]
    assert faq.active_cards(values, "乙群", now=now)[0].body == "交 B 表"
    assert not faq.active_cards(values, "丙群", now=now)


def test_empty_title_is_rejected():
    cards, errors = faq.validate_cards([card("甲群", "", "正文")])
    assert cards == []
    assert any("标题不能为空" in error for error in errors)


def test_per_group_card_limit_is_enforced():
    values = [card("甲群", f"卡{i}", "正文") for i in range(faq.MAX_CARDS_PER_GROUP + 1)]
    cards, errors = faq.validate_cards(values)
    assert len(cards) == faq.MAX_CARDS_PER_GROUP
    assert any("最多配置" in error for error in errors)


def test_injection_contains_only_current_group_and_expired_cards_are_missing():
    values = [
        card("甲群", "现行", "交 A 表", "2099-01-01"),
        card("甲群", "过期", "不能看见", "2020-01-01"),
        card("乙群", "别群", "不能串群", "2099-01-01"),
    ]
    cards = faq.active_cards(values, "甲群")
    injected = faq._injection(cards)["content"]
    assert "现行" in injected and "交 A 表" in injected
    assert "过期" not in injected and "不能看见" not in injected
    assert "别群" not in injected and "不能串群" not in injected


def test_register_before_reply_keeps_private_messages_untouched(tmp_path):
    # 通过真实 loader 验证钩子约定，同时覆盖群/私聊边界。
    from rubenbot.config import Config
    from rubenbot.memory.store import MemoryStore

    config = Config()
    config.plugins.dir = str(PLUGIN.parent.parent)
    config.plugins.enabled = ["group-faq"]
    manager = PluginManager(config, MemoryStore(":memory:"))
    assert [item.name for item in manager.load_all()] == ["group-faq"]
    manager.set_permissions("group-faq", ["read_messages", "send_messages"])
    manager.extras["group_faq_config"] = {"cards": [card("甲群", "办事", "去一号窗口")]}

    group = MessageEvent("recording", "group", "甲群", "u", "群友", "去哪办")
    private = MessageEvent("recording", "private", "甲群", "u", "群友", "去哪办")
    runtime = type("Runtime", (), {"extras": {}})()
    messages = [{"role": "system", "content": "原提示"}]
    group_result = asyncio.run(manager.run_before_reply(runtime, group, messages))
    private_result = asyncio.run(manager.run_before_reply(runtime, private, messages))
    assert "办事" in group_result[-1]["content"]
    assert private_result == messages