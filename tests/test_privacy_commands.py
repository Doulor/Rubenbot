"""核心隐私模块：/隐私 命令、待询问投递、答复解析与落裁定、清空。"""

import asyncio
from pathlib import Path

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore, ProfileLine
from rubenbot.persona import Persona
from rubenbot.privacy.commands import parse_answer

from conftest import RecordingAdapter, ScriptedLLM

NOW = 1_795_000_000.0


def make_bot(tmp_path: Path, *, enabled=True):
    store = MemoryStore(":memory:", privacy_enabled=enabled)
    config = Config()
    config.privacy.enabled = enabled
    config.plugins.dir = str(tmp_path / "plugins")
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    llm = ScriptedLLM({"chat": ["嗯嗯"]}, default="嗯嗯")
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, store


def private(text: str, sender_id: str = "u1", name: str = "阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender_id,
        sender_id=sender_id,
        sender_name=name,
        text=text,
    )


def group(text: str, sender_id: str = "u1", name: str = "阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender_id,
        sender_name=name,
        text=text,
    )


def seed_profile(store, person_id="recording:u1"):
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.save_profile(
        person_id,
        stable_lines=[ProfileLine(text="TA 是一名后端工程师", evidence_refs=[])],
        status_lines=[ProfileLine(text="最近在准备考试", evidence_refs=[])],
        status_expires_at=NOW + 7 * 86400,
        now=NOW,
    )
    store.add_event(person_id, "TA 去爬山了", ts=NOW, audience="group:g1", now=NOW)
    store.add_memory(person_id, "TA 喜欢美式咖啡", now=NOW)


# ----------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------


def test_privacy_report_shows_profile(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    seed_profile(store)
    asyncio.run(bot.handle_event(private("/隐私")))
    reply = adapter.texts[-1]
    assert "正常" in reply
    assert "后端工程师" in reply
    assert "最近在准备考试" in reply
    assert "1 条关于你的事件" in reply and "1 条零散记忆" in reply


def test_privacy_report_empty(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("/隐私")))
    assert "还没有关于你的画像" in adapter.texts[-1]


def test_privacy_mode_switch(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("/隐私 模式 隐私")))
    assert store.get_privacy_mode("recording:u1") == "private"
    assert "隐私" in adapter.texts[-1]

    asyncio.run(bot.handle_event(private("/隐私 模式 不记")))
    assert store.get_privacy_mode("recording:u1") == "norecord"
    reply = adapter.texts[-1]
    assert "存量" in reply or "之前留下的记录还保留着" in reply
    assert "/隐私 清空" in reply

    asyncio.run(bot.handle_event(private("/隐私 模式 正常")))
    assert store.get_privacy_mode("recording:u1") == "normal"
    assert "正常" in adapter.texts[-1]


def test_privacy_mode_invalid(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("/隐私 模式 随便")))
    assert store.get_privacy_mode("recording:u1") == "normal"
    assert "正常 / 隐私 / 不记" in adapter.texts[-1]


def test_privacy_purge_removes_everything(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    seed_profile(store)
    store.set_ruling("recording:u1", "health@group", "deny", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "能说吗？", now=NOW)
    asyncio.run(bot.handle_event(private("/隐私 清空")))

    assert store.get_profile("recording:u1") is None
    assert store.count_events("recording:u1") == 0
    assert store.count_memories("recording:u1") == 0
    assert store.list_rulings("recording:u1") == []
    assert store.list_pending_asks("recording:u1", status=None) == []
    reply = adapter.texts[-1]
    assert "已经清空" in reply and "1 条事件" in reply
    # memory_log 留痕，且不保留被删正文
    logs = [entry for entry in store.list_logs(20) if entry.action == "purge"]
    assert logs and logs[0].person_id == "recording:u1"
    snapshot = str(logs[0].before) + str(logs[0].after)
    assert "后端工程师" not in snapshot and "美式咖啡" not in snapshot
    # 清空是真删：不能从日志撤销回来
    assert logs[0].reverted is True
    assert store.restore(logs[0].id) is False
    assert store.get_profile("recording:u1") is None


def test_privacy_help_for_unknown_args(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("/隐私 啥")))
    assert "用法" in adapter.texts[-1]


def test_privacy_report_in_group_does_not_leak_profile(tmp_path):
    """群里发 /隐私 只报档位：画像只在私聊里摊开。"""
    bot, adapter, store = make_bot(tmp_path)
    seed_profile(store)
    asyncio.run(bot.handle_event(group("/隐私")))
    reply = adapter.texts[-1]
    assert "正常" in reply
    assert "私聊" in reply
    assert "后端工程师" not in reply
    assert "准备考试" not in reply


# ----------------------------------------------------------------------
# 待询问投递与答复
# ----------------------------------------------------------------------


def test_pending_ask_delivered_after_reply(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "你之前说过的事，我可以在群里提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    # 先正常回复，再附一条确认问句
    assert adapter.texts[0] == "嗯嗯"
    assert "可以在群里提到吗" in adapter.texts[-1]


def test_pending_ask_not_delivered_in_group(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "你之前说过的事，我可以在群里提到吗？")
    asyncio.run(bot.handle_event(group("Ruben 大家好")))
    assert not any("可以" in text and "提到吗" in text for text in adapter.texts)


def test_pending_ask_only_one_per_private_turn(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "第一件：可以吗？")
    store.add_pending_ask("recording:u1", "finance@group", "第二件：可以吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    asks = [text for text in adapter.texts if "可以吗" in text]
    assert len(asks) == 1


def test_pending_ask_answer_allow(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    asyncio.run(bot.handle_event(private("可以")))
    assert adapter.texts[-1].startswith("好，记住了")
    assert store.list_pending_asks("recording:u1", status=None)[0].status == "answered"
    assert store.get_ruling("recording:u1", "health@group").verdict == "allow"


def test_pending_ask_answer_deny(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    asyncio.run(bot.handle_event(private("不行")))
    assert "不会跟别人提" in adapter.texts[-1]
    assert store.get_ruling("recording:u1", "health@group").verdict == "deny"


def test_pending_ask_unclear_reply_keeps_pending(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    asyncio.run(bot.handle_event(private("唔…")))
    assert "没太看明白" in adapter.texts[-1]
    asks = store.list_pending_asks("recording:u1")
    assert len(asks) == 1 and asks[0].status == "pending"
    assert store.get_ruling("recording:u1", "health@group") is None


def test_pending_ask_long_reply_treated_as_chat(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    count_before = len(adapter.texts)
    asyncio.run(bot.handle_event(private("对了，你吃饭了没有，我今天加班到很晚")))
    # 正常聊天：走 bot 的回复，不写裁定
    assert len(adapter.texts) > count_before
    assert store.list_pending_asks("recording:u1")[0].status == "pending"
    assert store.get_ruling("recording:u1", "health@group") is None


def test_pending_ask_answer_via_command(tmp_path):
    bot, adapter, store = make_bot(tmp_path)
    store.upsert_person("recording", "u1", "阿花", now=NOW)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    asyncio.run(bot.handle_event(private("/隐私 可以")))
    assert store.get_ruling("recording:u1", "health@group").verdict == "allow"


@pytest.mark.parametrize("delivered,reply", [
    (False, private("/隐私 可以")), (True, group("/隐私 可以")),
    (True, private("/隐私 可以 吗")), (True, private("可以吗？")),
])
def test_only_explicit_private_reply_to_delivered_question_grants_consent(tmp_path, delivered, reply):
    bot, _, store = make_bot(tmp_path)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    if delivered:
        asyncio.run(bot.handle_event(private("在吗")))
    asyncio.run(bot.handle_event(reply))
    assert store.get_ruling("recording:u1", "health@group") is None
    assert store.list_pending_asks("recording:u1")[0].status == "pending"


def test_expired_delivery_cannot_be_answered_by_command(tmp_path, monkeypatch):
    import time
    from rubenbot.privacy.commands import ASK_ANSWER_WINDOW_SECONDS

    bot, _, store = make_bot(tmp_path)
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private("在吗")))
    later = time.time() + ASK_ANSWER_WINDOW_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    asyncio.run(bot.handle_event(private("/隐私 可以")))
    assert store.get_ruling("recording:u1", "health@group") is None


def test_parse_answer():
    assert parse_answer("可以") == "allow"
    assert parse_answer("可以的！") == "allow"
    assert parse_answer("不行") == "deny"
    assert parse_answer("不可以") == "deny"
    assert parse_answer("不可以啊") == "deny"
    assert parse_answer("不可以说") == "deny"
    assert parse_answer("可以吗？") is None
    assert parse_answer("可以的话") is None
    assert parse_answer("那就算了吧") is None
    assert parse_answer("我去银行") is None
    assert parse_answer("") is None
    # 否定优先，避免「可以不说」被误判成允许
    assert parse_answer("不行，别说") == "deny"


@pytest.mark.parametrize("prefix", ["/", "!"])
def test_disabled_module_only_keeps_standalone_purge(tmp_path, prefix):
    bot, adapter, store = make_bot(tmp_path, enabled=False)
    bot.config.plugins.command_prefix = prefix
    seed_profile(store)
    row = store.add_raw_message(platform="recording", chat_id="elsewhere", sender_id="u1", text="鸭腿")
    other = store.add_raw_message(platform="recording", chat_id="elsewhere", sender_id="u2", text="羊排")
    store.upsert_person("recording", "u2", "阿川")
    store.save_profile("recording:u2", stable_lines=[ProfileLine(text="TA 做木工", evidence_refs=[])], status_lines=[], now=NOW + 1)
    assert "后端工程师" in "".join(r.render() for r in bot.memory_reader.index.get("recording", "elsewhere"))
    store.add_pending_ask("recording:u1", "health@group", "可以提到吗？")
    asyncio.run(bot.handle_event(private(prefix + "隐私 模式 不记")))
    assert adapter.texts == ["嗯嗯"]
    assert store.get_privacy_mode("recording:u1") == "normal"
    asyncio.run(bot.handle_event(private(prefix + "清空我的数据")))
    assert "已经清空" in adapter.texts[-1]
    assert store.get_profile("recording:u1") is None
    assert store.raw_message_by_id(row) is None
    assert store.raw_message_by_id(other) is not None
    assert "后端工程师" not in "".join(r.render() for r in bot.memory_reader.index.get("recording", "elsewhere"))
