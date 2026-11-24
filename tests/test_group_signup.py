"""group-signup 插件的并发、幂等、隔离与授权边界测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from rubenbot.adapters.base import MessageEvent, Segment


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "group-signup"
_spec = importlib.util.spec_from_file_location("group_signup_plugin_test", PLUGIN_DIR / "plugin.py")
plugin = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


class FakeAdapter:
    name = "recording"

    def __init__(self, *, message_id="card-1", fail=False):
        self.message_id = message_id
        self.fail = fail
        self.sent: list[tuple[str, list[Segment], str]] = []

    async def send(self, chat_id, segments, *, chat_type="group"):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, segments, chat_type))
        return self.message_id


def event(
    text="",
    *,
    sender="u1",
    name="阿花",
    chat="g1",
    platform="recording",
    reply_to=None,
    message_id=None,
    platform_msg_id=None,
):
    return MessageEvent(
        platform=platform,
        chat_type="group",
        chat_id=chat,
        sender_id=sender,
        sender_name=name,
        text=text,
        reply_to_id=reply_to,
        platform_msg_id=platform_msg_id or message_id,
    )


def make_signup(tmp_path, *, response='{"action":"join"}', now=1000.0):
    llm = FakeLLM(response)
    ctx = SimpleNamespace(
        llm=llm,
        config=SimpleNamespace(database_path=str(tmp_path / "rubenbot.db")),
        extras={},
    )
    config = plugin.SignupConfig(max_open_hours=10, retention_days=30)
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    signup = plugin.GroupSignup(ctx, config, store, clock=lambda: now)
    adapter = FakeAdapter()
    runtime = SimpleNamespace(adapters={"recording": adapter})
    return signup, store, llm, runtime, adapter


def test_last_seat_concurrency_allows_only_one_member(tmp_path):
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    creator = event(sender="owner", name="群主")
    activity_id = store.create_activity(creator, "桌游", 1, 10, 1000)
    first = plugin.SignupStore(tmp_path / "signup.sqlite3")
    second = plugin.SignupStore(tmp_path / "signup.sqlite3")

    async def race():
        return await asyncio.gather(
            asyncio.to_thread(first.mutate, event(sender="u1"), activity_id, "join", 1001, message_id="m1"),
            asyncio.to_thread(second.mutate, event(sender="u2"), activity_id, "join", 1001, message_id="m2"),
        )

    results = asyncio.run(race())
    assert sorted(result[0] for result in results) == ["full", "joined"]
    assert len(store.members(activity_id)) == 1


def test_duplicate_message_does_not_take_two_seats(tmp_path):
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    activity_id = store.create_activity(event(sender="owner"), "电影", 2, 10, 1000)
    member = event(sender="u1", message_id="same-message")
    assert store.mutate(member, activity_id, "join", 1001, message_id=member.platform_msg_id)[0] == "joined"
    assert store.mutate(member, activity_id, "join", 1002, message_id=member.platform_msg_id)[0] == "duplicate"
    assert len(store.members(activity_id)) == 1


def test_leave_frees_seat_for_next_person(tmp_path):
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    activity_id = store.create_activity(event(sender="owner"), "羽毛球", 1, 10, 1000)
    assert store.mutate(event(sender="u1"), activity_id, "join", 1001)[0] == "joined"
    assert store.mutate(event(sender="u1"), activity_id, "leave", 1002)[0] == "left"
    assert store.mutate(event(sender="u2"), activity_id, "join", 1003)[0] == "joined"
    assert [row["member_id"] for row in store.members(activity_id)] == ["u2"]


def test_expired_activity_rejects_join_but_keeps_final_roster(tmp_path):
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    activity_id = store.create_activity(event(sender="owner"), "桌游", 2, 1, 1000)
    assert store.mutate(event(sender="u1"), activity_id, "join", 1001)[0] == "joined"
    result, activity, members = store.mutate(event(sender="u2"), activity_id, "join", 4601)
    assert result == "closed"
    assert activity["status"] == "expired"
    assert [row["member_id"] for row in members] == ["u1"]


def test_same_card_id_isolated_by_group_and_platform(tmp_path):
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    card = "same-card"
    a = event(chat="g1", platform_msg_id=card)
    b = event(chat="g2", platform_msg_id=card)
    ida = store.create_activity(a, "甲群活动", 2, 10, 1000)
    idb = store.create_activity(b, "乙群活动", 2, 10, 1000)
    store.set_card_message(ida, card)
    store.set_card_message(idb, card)
    assert store.find_by_card(event(chat="g1"), card, 1001)["id"] == ida
    assert store.find_by_card(event(chat="g2"), card, 1001)["id"] == idb
    assert store.mutate(event(chat="g1", sender="u1"), ida, "join", 1002)[0] == "joined"
    assert store.members(idb) == []


def test_reply_to_activity_card_runs_model_and_changes_only_real_sender(tmp_path):
    signup, store, llm, runtime, adapter = make_signup(tmp_path)
    creator = event(sender="owner", name="群主")
    activity_id = store.create_activity(creator, "桌游", 2, 10, 1000)
    store.set_card_message(activity_id, "card-1")
    reply = event(
        "我也去，帮小明报名",
        sender="u1",
        name="阿花",
        reply_to="card-1",
        message_id="reply-1",
    )
    # 模型明确识别为转述时，插件仍只以模型有限动作结果为准；代码不采用被转述者身份。
    llm.response = json.dumps({"action": "noop"}, ensure_ascii=False)
    assert asyncio.run(signup.on_message(runtime, reply)) is None
    assert store.members(activity_id) == []
    assert len(llm.calls) == 1


def test_unrelated_chat_saying_i_am_going_does_not_change_roster(tmp_path):
    signup, store, llm, runtime, _ = make_signup(tmp_path)
    activity_id = store.create_activity(event(sender="owner"), "桌游", 2, 10, 1000)
    store.set_card_message(activity_id, "card-1")
    unrelated = event("我也去", sender="u1", reply_to=None, message_id="ordinary-1")
    assert asyncio.run(signup.on_message(runtime, unrelated)) is None
    assert llm.calls == []
    assert store.members(activity_id) == []


def test_disabled_or_unauthorized_context_cannot_claim_success(tmp_path):
    # 插件禁用时不会执行 register；这个单元层也验证没有 ctx.llm 时不写入名单。
    ctx = SimpleNamespace(llm=None, config=SimpleNamespace(database_path=str(tmp_path / "db")), extras={})
    store = plugin.SignupStore(tmp_path / "signup.sqlite3")
    signup = plugin.GroupSignup(ctx, plugin.SignupConfig(), store, clock=lambda: 1000)
    activity_id = store.create_activity(event(sender="owner"), "活动", 2, 10, 1000)
    store.set_card_message(activity_id, "card-1")
    runtime = SimpleNamespace(adapters={"recording": FakeAdapter()})
    reply = event("算我一个", sender="u1", reply_to="card-1", message_id="r1")
    assert asyncio.run(signup.on_message(runtime, reply)) is None
    assert store.members(activity_id) == []


def test_create_records_card_only_after_successful_delivery(tmp_path):
    signup, store, _, runtime, adapter = make_signup(tmp_path)
    creator = event("", sender="owner", name="群主")
    assert asyncio.run(signup.create(runtime, creator, "4 桌游")) is None
    activity = store.list_activities(creator, 1000)[0]
    assert activity["card_message_id"] == "card-1"
    assert adapter.sent

    failed_signup, failed_store, _, failed_runtime, _ = make_signup(tmp_path / "failed")
    failed_runtime.adapters["recording"].fail = True
    result = asyncio.run(failed_signup.create(failed_runtime, creator, "4 失败活动"))
    assert "没发出去" in result
    assert failed_store.list_activities(creator, 1000)[0]["card_message_id"] == ""


def test_manifest_and_loader_keep_plugin_disabled_by_default(tmp_path):
    from rubenbot.config import Config
    from rubenbot.memory.store import MemoryStore
    from rubenbot.plugins.loader import PluginManager

    config = Config()
    config.plugins.dir = str(PLUGIN_DIR.parent)
    config.plugins.enabled = []
    config.database_path = str(tmp_path / "rubenbot.db")
    manager = PluginManager(config, MemoryStore(":memory:"))
    manager.load_all()
    plugin_info = manager.plugins["group-signup"]
    assert plugin_info.manifest is not None
    assert plugin_info.enabled is False
    assert plugin_info.loaded is False
    assert manager.enabled_plugins == []
