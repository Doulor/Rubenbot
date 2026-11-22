"""适配器网关：目录披露、风险分级、四维冷却、审计、工具注册与降级。"""

from __future__ import annotations

import asyncio
import time

import pytest

from rubenbot.adapters.base import BaseAdapter
from rubenbot.config import Config, GatewayConfig
from rubenbot.core import RubenBot
from rubenbot.gateway.catalog import (
    RISK_DENIED,
    RISK_SAFE,
    RISK_SENSITIVE,
    ActionCatalog,
    ActionEntry,
    catalog_for,
    clear_cache,
)
from rubenbot.gateway.gatekeeper import Gatekeeper, object_id_from_params
from rubenbot.gateway.service import (
    ADAPTER_ACTIONS_TOOL,
    ADAPTER_CALL_TOOL,
    GatewayService,
    validate_params,
)
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import ScriptedLLM


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 目录
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def snowluma_catalog() -> ActionCatalog:
    clear_cache()
    catalog = catalog_for("snowluma")
    assert catalog is not None
    return catalog


def test_catalog_loads_standard_and_extension_actions(snowluma_catalog):
    names = set(snowluma_catalog.actions)
    assert {"send_group_msg", "send_private_msg", "get_group_member_list"} <= names
    assert {"set_msg_emoji_like", "fetch_ptt_text", "send_qzone_msg"} <= names


def test_catalog_denied_actions_invisible(snowluma_catalog):
    for name in ("delete_friend", "set_group_dismiss", "set_restart", "clean_cache"):
        entry = snowluma_catalog.get(name)
        assert entry is not None and entry.risk == RISK_DENIED
        assert entry not in snowluma_catalog.visible()
        assert snowluma_catalog.describe(name) is None
        # 关键词也搜不到
        assert all(item.name != name for item in snowluma_catalog.search(name))
    assert all(item.risk != RISK_DENIED for item in snowluma_catalog.search(""))


def test_catalog_search_and_describe(snowluma_catalog):
    results = snowluma_catalog.search("表情")
    assert [item.name for item in results] == ["set_msg_emoji_like"]
    assert snowluma_catalog.search("群成员")  # 说明里带这个词
    text = snowluma_catalog.describe("send_group_msg")
    assert "group_id" in text and "必填" in text
    # free-form 动作给自由对象提示
    free = snowluma_catalog.describe("get_group_list")
    assert "无" in free or "自由" in free


def test_catalog_sensitive_actions_listed_with_risk(snowluma_catalog):
    sensitive = [item.name for item in snowluma_catalog.visible() if item.risk == RISK_SENSITIVE]
    assert set(sensitive) >= {
        "set_group_kick",
        "set_group_ban",
        "set_group_admin",
        "set_group_leave",
        "delete_msg",
        "set_friend_add_request",
        "set_group_add_request",
    }


def test_catalog_for_unknown_adapter_is_none():
    clear_cache()
    assert catalog_for("telegram") is None
    assert catalog_for(None) is None


def test_catalog_rejects_bad_risk(tmp_path):
    path = tmp_path / "weird.json"
    path.write_text('{"actions": [{"name": "x", "summary": "y", "risk": "danger"}]}', encoding="utf-8")
    catalog = ActionCatalog.load("weird", tmp_path)
    assert catalog.get("x").risk == RISK_SAFE  # 不认识的按 safe，不按 denied 藏起来


# ----------------------------------------------------------------------
# 闸门：四维冷却与拒绝原因
# ----------------------------------------------------------------------


def make_gatekeeper(store=None, **gateway) -> tuple[Gatekeeper, Config]:
    store = store or MemoryStore(":memory:")
    cfg = Config()
    cfg.gateway = GatewayConfig(**gateway)
    return Gatekeeper(store, cfg), cfg


def entry(**kwargs) -> ActionEntry:
    defaults = dict(name="set_msg_emoji_like", summary="贴表情", risk=RISK_SAFE, cooldown_seconds=60)
    defaults.update(kwargs)
    return ActionEntry(**defaults)


def test_conversation_cooldown_returns_remaining_seconds():
    gate, _ = make_gatekeeper()
    emoji = entry()
    now = 1000.0
    assert gate.check(emoji, chat_key="p:g1", now=now).allowed
    gate.note_success(emoji, chat_key="p:g1", now=now)
    refused = gate.check(emoji, chat_key="p:g1", now=now + 15)
    assert not refused.allowed
    assert "45 秒" in refused.reason
    assert refused.retry_after == pytest.approx(45)
    # 冷却到期后放行；另一个会话不受影响
    assert gate.check(emoji, chat_key="p:g1", now=now + 61).allowed
    assert gate.check(emoji, chat_key="p:g2", now=now + 1).allowed


def test_object_limit_three_per_hour():
    gate, _ = make_gatekeeper()
    emoji = entry(cooldown_seconds=0)
    now = 5000.0
    for index in range(3):
        assert gate.check(emoji, chat_key="p:g1", object_id="u1", now=now + index).allowed
        gate.note_success(emoji, chat_key="p:g1", object_id="u1", now=now + index)
    refused = gate.check(emoji, chat_key="p:g1", object_id="u1", now=now + 10)
    assert not refused.allowed
    assert "同一个人" in refused.reason
    # 对别人仍然可以
    assert gate.check(emoji, chat_key="p:g1", object_id="u2", now=now + 10).allowed
    # 一小时窗口滑过后恢复
    assert gate.check(emoji, chat_key="p:g1", object_id="u1", now=now + 3601).allowed


def test_conversation_daily_budget():
    gate, _ = make_gatekeeper(daily_budget_per_conversation=2)
    emoji = entry(cooldown_seconds=0)
    now = 100.0
    gate.note_success(emoji, chat_key="p:g1", now=now)
    gate.note_success(emoji, chat_key="p:g1", now=now)
    refused = gate.check(emoji, chat_key="p:g1", now=now + 1)
    assert not refused.allowed and "额度" in refused.reason
    # 别的会话还有自己的额度
    assert gate.check(emoji, chat_key="p:g2", now=now + 1).allowed


def test_daily_budget_resets_next_day():
    gate, _ = make_gatekeeper(daily_budget_per_conversation=1)
    emoji = entry(cooldown_seconds=0)
    day1 = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
    gate.note_success(emoji, chat_key="p:g1", now=day1)
    assert not gate.check(emoji, chat_key="p:g1", now=day1 + 60).allowed
    day2 = time.mktime((2026, 11, 21, 12, 0, 0, 0, 0, -1))
    assert gate.check(emoji, chat_key="p:g1", now=day2).allowed


def test_global_daily_cap():
    gate, _ = make_gatekeeper(global_daily_cap=2)
    emoji = entry(cooldown_seconds=0)
    now = 100.0
    gate.note_success(emoji, chat_key="p:g1", now=now)
    gate.note_success(emoji, chat_key="p:g2", now=now)
    refused = gate.check(emoji, chat_key="p:g3", now=now + 1)
    assert not refused.allowed and "全局" in refused.reason


def test_readonly_actions_skip_budget():
    gate, _ = make_gatekeeper(daily_budget_per_conversation=0, global_daily_cap=0)
    readonly = entry(name="get_group_member_list", cooldown_seconds=0, proactive=False)
    assert gate.check(readonly, chat_key="p:g1", now=100.0).allowed


def test_failure_backoff_after_three_failures():
    gate, _ = make_gatekeeper()
    emoji = entry(cooldown_seconds=0)
    now = 100.0
    for index in range(3):
        gate.note_failure(emoji, now=now + index)
    refused = gate.check(emoji, chat_key="p:g1", now=now + 5)
    assert not refused.allowed and "暂停" in refused.reason
    assert gate.check(emoji, chat_key="p:g1", now=now + 3660).allowed
    # 成功一次清零
    gate.note_success(emoji, chat_key="p:g1", now=now + 3700)
    gate.note_failure(emoji, now=now + 3701)
    assert gate.check(emoji, chat_key="p:g1", now=now + 3705).allowed


def test_sensitive_gate_and_denied():
    gate, cfg = make_gatekeeper(allow_sensitive=False)
    kick = entry(name="set_group_kick", cooldown_seconds=0, risk=RISK_SENSITIVE)
    assert not gate.check(kick, chat_key="p:g1", now=1.0).allowed
    cfg.gateway.allow_sensitive = True
    assert gate.check(kick, chat_key="p:g1", now=1.0).allowed
    banned = entry(name="delete_friend", risk=RISK_DENIED)
    assert not gate.check(banned, chat_key="p:g1", now=1.0).allowed


def test_state_survives_new_gatekeeper(store):
    """状态落库：换一个闸门实例（模拟重启）后冷却仍然生效。"""
    gate, _ = make_gatekeeper(store)
    emoji = entry()
    gate.note_success(emoji, chat_key="p:g1", object_id="u1", now=100.0)
    reborn = Gatekeeper(store, gate.config)
    assert not reborn.check(emoji, chat_key="p:g1", now=110.0).allowed


def test_object_id_from_params():
    assert object_id_from_params({"user_id": 123}) == "123"
    assert object_id_from_params({"target_id": ""}) == ""
    assert object_id_from_params({}) == ""


# ----------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------


def test_validate_params_required_and_types(snowluma_catalog):
    send = snowluma_catalog.get("send_group_msg")
    assert validate_params(send, {"group_id": 1, "message": "hi"}) is None
    assert "缺少必填参数" in validate_params(send, {"group_id": 1})
    assert "integer" in validate_params(send, {"group_id": "abc", "message": "x"})
    assert validate_params(send, {"group_id": "123", "message": "x"}) is None  # 数字字符串可接受


def test_validate_params_freeform_requires_serializable(snowluma_catalog):
    free = snowluma_catalog.get("get_group_list")
    assert validate_params(free, {}) is None
    assert "JSON" in validate_params(free, {"bad": object()})


# ----------------------------------------------------------------------
# 服务：执行、拒绝、审计、适配器降级
# ----------------------------------------------------------------------


class ActionAdapter(BaseAdapter):
    """实现通用动作通道的假适配器。"""

    name = "snowluma"

    def __init__(self, *, fail=None):
        self.calls = []
        self.fail = fail
        self.sent = []

    async def start(self, on_event):
        return None

    async def send(self, chat_id, segments, *, chat_type="group"):
        self.sent.append((chat_id, segments))
        return "m1"

    async def stop(self):
        return None

    async def call_action(self, name, params=None):
        self.calls.append((name, dict(params or {})))
        if self.fail is not None:
            raise self.fail
        return {"message_id": 42}


class PlainAdapter(ActionAdapter):
    """没有实现 call_action 的适配器（Telegram/QQ 官方那种）。"""

    name = "telegram"

    call_action = BaseAdapter.call_action


def make_service(store=None, **gateway) -> tuple[GatewayService, MemoryStore]:
    store = store or MemoryStore(":memory:")
    cfg = Config()
    # 群聊默认是拒绝的（白名单为空不放行），测试默认把 g1 加进白名单
    gateway.setdefault("enabled_conversations", ["snowluma:g1"])
    cfg.gateway = GatewayConfig(**gateway)
    return GatewayService(store, cfg), store


def test_service_call_success_and_audit():
    service, store = make_service(enabled=True)
    adapter = ActionAdapter()
    text = run(
        service.call(adapter, "set_msg_emoji_like", {"message_id": 7, "emoji_id": "66"}, chat_id="g1")
    )
    assert "成功" in text and "42" in text
    assert adapter.calls == [("set_msg_emoji_like", {"message_id": 7, "emoji_id": "66"})]
    audit = store.list_gateway_audit()
    assert audit[0]["action"] == "set_msg_emoji_like"
    assert audit[0]["ok"] == 1
    assert audit[0]["chat_id"] == "g1"


@pytest.mark.parametrize("action", ["delete_friend", "set_group_dismiss", "set_restart", "clean_cache", "set_online_status"])
def test_service_denied_action_rejected_and_not_listed(action):
    service, store = make_service(enabled=True, allow_sensitive=True)
    adapter = ActionAdapter()
    assert not service.gatekeeper.check(service.catalog_for(adapter).get(action), chat_key="snowluma:g1").allowed
    text = run(service.call(adapter, action, {}, chat_id="g1"))
    assert "禁止" in text
    assert adapter.calls == []
    assert service.list_actions(adapter, keyword=action).startswith("没有匹配")
    assert "没有名为" in service.describe_action(adapter, action)
    row, = store.list_gateway_audit()
    assert row["action"] == action and row["ok"] == 0 and row["sensitive"] == 1
    assert row["reject_reason"] == text


@pytest.mark.parametrize("action,target", [("send_msg", "user_id"), ("send_private_msg", "user_id"), ("send_group_msg", "group_id")])
def test_message_schema_accepts_segments(snowluma_catalog, action, target):
    entry = snowluma_catalog.get(action)
    segment = {"type": "text", "data": {"text": "验收"}}
    for message in ("验收", segment, [segment]):
        assert validate_params(entry, {target: 1, "message": message}) is None
    assert validate_params(entry, {target: 1, "message": 123})
    assert "string / object / array" in entry.describe()


@pytest.mark.parametrize("action,params", [
    ("set_friend_add_request", {"flag": "test-flag", "approve": False}),
    ("set_group_add_request", {"flag": "test-flag", "approve": False, "sub_type": "add"}),
])
def test_request_manual_tool_dispatch_and_audit(store, action, params):
    # 仅用假适配器核验人工处理通道，不向真机提交虚构的申请。
    bot, adapter = make_bot(store, ScriptedLLM(), enabled=True)
    args = {"action": action, "params": params}
    assert "敏感" in run(bot._gateway_tool("adapter_call", args, group_event()))
    assert not adapter.calls
    bot.config.gateway.allow_sensitive = True
    assert "成功" in run(bot._gateway_tool("adapter_call", args, group_event()))
    assert adapter.calls == [(action, params)]
    assert [(row["action"], row["ok"], row["sensitive"]) for row in store.list_gateway_audit()] == [
        (action, 1, 1), (action, 0, 1),
    ]


def test_service_unknown_action_rejected():
    service, _ = make_service(enabled=True)
    adapter = ActionAdapter()
    text = run(service.call(adapter, "totally_unknown", {}, chat_id="g1"))
    assert "没有名为" in text
    assert adapter.calls == []


def test_service_sensitive_gate():
    service, store = make_service(enabled=True, allow_sensitive=False)
    adapter = ActionAdapter()
    text = run(service.call(adapter, "set_group_kick", {"group_id": 1, "user_id": 2}, chat_id="g1"))
    assert "敏感" in text
    assert adapter.calls == []
    audit = store.list_gateway_audit()
    assert audit[0]["sensitive"] == 1 and audit[0]["ok"] == 0

    service.config.gateway.allow_sensitive = True
    text = run(service.call(adapter, "set_group_kick", {"group_id": 1, "user_id": 2}, chat_id="g1"))
    assert "成功" in text
    assert adapter.calls


def test_service_cooldown_message_includes_seconds():
    service, _ = make_service(enabled=True)
    adapter = ActionAdapter()
    now = 1000.0
    run(service.call(adapter, "set_msg_emoji_like", {"message_id": 1, "emoji_id": "1"}, chat_id="g1", now=now))
    text = run(
        service.call(adapter, "set_msg_emoji_like", {"message_id": 2, "emoji_id": "1"}, chat_id="g1", now=now + 20)
    )
    assert "40 秒" in text
    assert len(adapter.calls) == 1


def test_service_failure_returns_truncated_error_and_backoff():
    service, store = make_service(enabled=True)
    long_error = "x" * 900
    adapter = ActionAdapter(fail=RuntimeError(long_error))
    text = run(service.call(adapter, "send_group_msg", {"group_id": 1, "message": "hi"}, chat_id="g1"))
    assert "失败" in text
    assert len(text) < 900
    for _ in range(2):
        run(service.call(adapter, "send_group_msg", {"group_id": 1, "message": "hi"}, chat_id="g1"))
    text = run(service.call(adapter, "send_group_msg", {"group_id": 1, "message": "hi"}, chat_id="g1"))
    assert "暂停" in text
    assert store.list_gateway_audit()[0]["ok"] == 0


def test_service_action_not_supported_degrades():
    service, _ = make_service(enabled=True)
    adapter = PlainAdapter()
    assert adapter.supports_actions() is False
    assert service.tools(adapter, chat_id="g1") == []
    text = run(service.call(adapter, "send_group_msg", {"group_id": 1, "message": "x"}, chat_id="g1"))
    assert "不支持" in text


def test_service_disabled_gateway_returns_text():
    service, _ = make_service(enabled=False)
    adapter = ActionAdapter()
    assert service.tools(adapter, chat_id="g1") == []
    assert "未开启" in run(service.call(adapter, "send_group_msg", {"group_id": 1, "message": "x"}, chat_id="g1"))


# ----------------------------------------------------------------------
# 工具注册门控
# ----------------------------------------------------------------------


def test_tool_registration_gate():
    service, _ = make_service(enabled=True, enabled_conversations=["snowluma:g_allowed"])
    adapter = ActionAdapter()
    # 私聊恒可（依据见 is_owner_chat 注释）
    assert [tool["function"]["name"] for tool in service.tools(adapter, chat_id="owner", chat_type="private")] == [
        "adapter_actions",
        "adapter_call",
    ]
    # 群聊不在白名单：不给
    assert service.tools(adapter, chat_id="g_other") == []
    # 在白名单：给
    assert service.tools(adapter, chat_id="g_allowed")
    # 适配器没有动作通道：不给
    assert service.tools(PlainAdapter(), chat_id="g_allowed") == []
    # 关掉总开关：全面不给
    service.config.gateway.enabled = False
    assert service.tools(adapter, chat_id="g_allowed") == []


def test_tools_are_openai_shape():
    assert ADAPTER_ACTIONS_TOOL["function"]["name"] == "adapter_actions"
    assert ADAPTER_CALL_TOOL["function"]["name"] == "adapter_call"
    assert ADAPTER_CALL_TOOL["function"]["parameters"]["required"] == ["action"]


def test_guide_is_single_line_and_only_when_available():
    service, _ = make_service(enabled=True, enabled_conversations=[])
    adapter = ActionAdapter()
    guide = service.guide(adapter, chat_id="owner", chat_type="private")
    assert guide.count("\n") == 0
    assert "adapter_actions" in guide and "set_msg_emoji_like" in guide
    assert service.guide(adapter, chat_id="g1") == ""  # 群聊未开


# ----------------------------------------------------------------------
# core 接线
# ----------------------------------------------------------------------


def make_bot(store: MemoryStore, llm, **gateway) -> tuple[RubenBot, ActionAdapter]:
    cfg = Config()
    cfg.reply.segment_delay_enabled = False
    cfg.decision.merge_window_seconds = 0
    gateway.setdefault("enabled_conversations", ["snowluma:g1"])
    cfg.gateway = GatewayConfig(**gateway)
    bot = RubenBot(cfg, store, llm, Persona("Ruben", "你是 Ruben。"))
    adapter = ActionAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def group_event():
    from rubenbot.adapters.base import MessageEvent

    return MessageEvent(
        platform="snowluma",
        chat_type="group",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text="贴个表情",
    )


def test_core_tool_definitions_include_gateway_when_enabled():
    bot, _ = make_bot(MemoryStore(":memory:"), ScriptedLLM(), enabled=True)
    names = [tool["function"]["name"] for tool in bot.tool_definitions(group_event())]
    assert "adapter_actions" in names and "adapter_call" in names
    # 没有事件上下文（拿不到会话）时不给
    assert "adapter_call" not in [t["function"]["name"] for t in bot.tool_definitions()]


def test_core_gateway_tool_dispatch():
    bot, adapter = make_bot(MemoryStore(":memory:"), ScriptedLLM(), enabled=True)
    event = group_event()
    listed = run(bot._gateway_tool("adapter_actions", {"keyword": "表情"}, event))
    assert "set_msg_emoji_like" in listed
    detail = run(bot._gateway_tool("adapter_actions", {"action": "set_msg_emoji_like"}, event))
    assert "message_id" in detail
    result = run(
        bot._gateway_tool(
            "adapter_call",
            {"action": "set_msg_emoji_like", "params": {"message_id": 3, "emoji_id": "66"}},
            event,
        )
    )
    assert "成功" in result
    assert adapter.calls == [("set_msg_emoji_like", {"message_id": 3, "emoji_id": "66"})]


def test_core_system_prompt_has_gateway_pointer():
    store = MemoryStore(":memory:")
    bot, _ = make_bot(store, ScriptedLLM(), enabled=True)
    messages = run(bot.build_context(group_event()))
    assert "adapter_actions" in messages[0]["content"]
    # 关掉后没有指针
    bot.config.gateway.enabled = False
    bot.gateway.config = bot.config
    messages = run(bot.build_context(group_event()))
    assert "adapter_actions" not in messages[0]["content"]


def test_chat_tools_still_importable():
    from rubenbot.core import chat_tools

    assert isinstance(chat_tools(Config()), list)
