"""私聊时间线注入：bot 自己的跨会话动作与历史时间断层。

回放 2026-11-23 10:15 机主活验失败：
(a) 09:57 bot 在群里发出 beijing_time.txt，10:15 私聊问「刚刚给我发过什么」，
    必须答出那件发文件的事；
(b) 昨晚 00:42 的私聊旧话被问起，不能被当成「刚刚」。
"""

import asyncio
import json
import time
from collections import deque

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.memory.timeline import (
    SELF_ACTION_MAX_CHARS,
    describe_gap,
    read_timeline,
    recent_self_actions,
    recent_timeline,
)
from rubenbot.persona import Persona

NOW = 1_800_000_000


def private(text="刚刚给我发过什么", *, sender="owner", chat_id="owner"):
    return MessageEvent("recording", "private", chat_id, sender, "机主", text)


def group(text, *, sender="owner", chat_id="room"):
    return MessageEvent("recording", "group", chat_id, sender, "机主", text, mentions_bot=True)


class AttachmentAdapter(RecordingAdapter):
    """带附件通道的假适配器：交付走 send_file，和线上群文件一致。"""

    def __init__(self):
        super().__init__()
        self.files = []

    async def send_file(self, chat_id, filename, content, *, chat_type):
        self.files.append((chat_id, chat_type, filename, content))


def make_bot(config, store, llm=None, adapter=None):
    bot = RubenBot(config, store, llm or ScriptedLLM(), Persona("Ruben", "一个群友"))
    adapter = adapter or RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


# ----------------------------------------------------------------------
# 取数范围：自我动作单独成段
# ----------------------------------------------------------------------


def test_store_timeline_hides_self_events_by_default(store):
    store.add_raw_message(platform="recording", chat_type="group", chat_id="room",
                          sender_id="u2", sender_name="阿草", text="群友的话", ts=NOW - 60)
    store.add_self_event("发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=NOW - 30, now=NOW)

    assert [row.text for row in read_timeline(store, now=NOW)] == ["群友的话"]
    rows = read_timeline(store, now=NOW, include_self=True, person_id="self")
    assert [row.text for row in rows] == ["发了文件 beijing_time.txt"]


def test_self_actions_are_private_only_and_skip_private_sources(store):
    store.add_self_event("群里发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=NOW - 600, now=NOW)
    # bot 和别人的私聊内容属于那个人的私聊，不能在别的会话摊开。
    store.add_self_event("私聊里说的话", source_chat="recording:friend",
                         audience="private", ts=NOW - 300, now=NOW)
    store.add_self_event("本会话刚说过的", source_chat="recording:owner",
                         audience="private", ts=NOW - 60, now=NOW)

    lines = asyncio.run(recent_self_actions(store, private(), now=NOW))
    assert len(lines) == 1 and "beijing_time.txt" in lines[0]
    assert "群聊" in lines[0] and "10 分钟前" in lines[0]
    # 群聊里不摊开自己的私聊动作。
    assert asyncio.run(recent_self_actions(store, group("在吗"), now=NOW)) == []


def test_self_actions_have_own_budget_apart_from_recent_timeline(store):
    """「别处近况」塞满字符上限时，自我交付仍单独成段保留。"""
    for i in range(40):
        store.add_raw_message(platform="recording", chat_type="group", chat_id=f"g{i}",
                              sender_id=f"u{i}", sender_name=f"群友{i}", text="很多别处的近况" * 5,
                              ts=NOW - 100 - i)
    store.add_self_event("发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=NOW - 30, now=NOW)
    target = private()
    recent = asyncio.run(recent_timeline(store, target, char_limit=120, now=NOW))
    assert recent and sum(map(len, recent)) <= 120
    actions = asyncio.run(recent_self_actions(store, target, char_limit=SELF_ACTION_MAX_CHARS, now=NOW))
    assert len(actions) == 1 and "beijing_time.txt" in actions[0]
    assert len(actions[0]) <= SELF_ACTION_MAX_CHARS


# ----------------------------------------------------------------------
# 时间感知：间隔说人话
# ----------------------------------------------------------------------


@pytest.mark.parametrize("seconds,expected", [
    (5, "刚刚"),
    (18 * 60, "18 分钟前"),
    (9 * 3600, "9 小时前"),
    (25 * 3600, "昨天"),
    (3 * 86400, "3 天前"),
])
def test_describe_gap(seconds, expected):
    assert describe_gap(seconds) == expected


def test_overnight_private_history_gets_a_gap_marker(config, store):
    """昨晚 00:42 的话与今早 10:15 的提问之间必须有断层标注，不能看着像刚说。"""
    config.chat.history_ttl_hours = 24
    now = NOW
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="昨晚聊了鸭腿和自行车", ts=now - 9.5 * 3600)
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="__bot__",
                          sender_name="Ruben", text="嗯，鹈鹕骑车那段我知道", ts=now - 9.5 * 3600 + 5)
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="你记得你刚刚给我发过什么吗", ts=now)
    bot, _ = make_bot(config, store)
    context = asyncio.run(bot.build_context(private(), now=now))
    contents = [m["content"] for m in context]
    marker = next(c for c in contents if "时间断层" in c)
    assert "9 小时前" in marker and "不是刚刚说的" in marker
    # 断层标注插在昨晚那段之后、当前这句之前。
    assert contents.index(marker) > next(i for i, c in enumerate(contents) if "鹈鹕骑车" in c)
    assert contents.index(marker) < next(i for i, c in enumerate(contents) if "刚刚给我发过什么" in c)


def test_close_messages_have_no_gap_marker(config, store):
    now = NOW
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="在吗", ts=now - 60)
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="在吗现在", ts=now)
    bot, _ = make_bot(config, store)
    context = asyncio.run(bot.build_context(private(), now=now))
    assert not any("时间断层" in m["content"] for m in context)


def test_timeline_lines_carry_relative_time(store):
    store.add_self_event("群里发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=NOW - 9 * 3600, now=NOW)
    line, = asyncio.run(recent_self_actions(store, private(), now=NOW))
    assert "9 小时前" in line


def test_self_actions_obey_privacy_gate_when_enabled(config, store):
    """隐私开启：群里的正常交付在私聊里照常可见（不白调一次模型裁定），
    高敏感与封存的自我动作照旧扣下。"""
    config.privacy.enabled = True
    store.privacy_enabled = True
    store.add_self_event("发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=NOW - 60, now=NOW)
    store.add_self_event("我在群里提到自己住院了", source_chat="recording:room",
                         audience="group:room", ts=NOW - 120, now=NOW)
    store.add_self_event("这是我私下说的秘密", source_chat="recording:room",
                         audience="group:room", ts=NOW - 180, now=NOW)
    bot, _ = make_bot(config, store)
    speaker = "recording:owner"
    review = bot.memory_reader.timeline_reviewer(
        "private", speaker, {speaker}, NOW, platform="recording", chat_id="owner",
    )
    assert review is not None
    lines = asyncio.run(recent_self_actions(store, private(), allow=review, now=NOW))
    assert any("beijing_time.txt" in line for line in lines)
    assert all("住院" not in line and "秘密" not in line for line in lines)
    assert not [c for c in bot.llm.calls if c["role"] == "gate"]  # 正常交付不送模型裁定


# ----------------------------------------------------------------------
# 回放 (a)：群里发文件 → 私聊问「刚刚给我发过什么」
# ----------------------------------------------------------------------


class ToolModel(ScriptedLLM):
    """按步骤吐工具调用；操作委托的收尾由工具循环负责（交付即静默）。"""

    def __init__(self, steps):
        super().__init__()
        self.steps = deque(steps)

    async def chat_with_tools(self, messages, tools, *, tool_choice="auto", **kwargs):
        self.calls.append({"role": "chat_with_tools", "task": kwargs.get("task"),
                           "messages": list(messages), "tool_choice": tool_choice})
        return self.steps.popleft()


def tool_call(name, **args):
    return {"tool_calls": [{"id": name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def test_group_file_delivery_reaches_private_question(config, store, tmp_path):
    config.computer.enabled = True
    config.computer.whitelist = ["recording:owner"]
    config.database_path = str(tmp_path / "bot.db")
    path = tmp_path / "beijing_time.txt"
    llm = ToolModel([
        tool_call("write_file", path=str(path), content="2026-11-23 09:57 北京时间"),
        tool_call("send_file", path=str(path)),
        {"content": ""},
    ])
    bot, adapter = make_bot(config, store, llm, adapter=AttachmentAdapter())
    asyncio.run(bot.generate([{"role": "user", "content": "写个北京时间发到群里"}],
                             event=group("写个北京时间发到群里"), computer_task=True, deliver_file=True))
    assert path.read_text() == "2026-11-23 09:57 北京时间"
    assert adapter.files == [("room", "group", "beijing_time.txt", path.read_bytes())]
    assert adapter.sent == []  # 交付即静默，群里只有附件
    delivery, = store.list_events(person_id="self")
    assert "beijing_time.txt" in delivery.summary and delivery.source_chat == "recording:room"

    # 十分钟后私聊问「刚刚给我发过什么」：上下文必须带上这件交付。
    later = NOW
    delivery.ts = later - 600
    store._conn.execute("UPDATE events SET ts=? WHERE id=?", (delivery.ts, delivery.id))
    store._conn.commit()
    context = asyncio.run(bot.build_context(private(), now=later))
    actions, = [m["content"] for m in context if "【我最近在别处做过的事" in m["content"]]
    assert "beijing_time.txt" in actions
    assert "群聊" in actions and "10 分钟前" in actions


class EvidenceModel(ScriptedLLM):
    """回答只取决于生成请求里实际带到的证据，不预排答案。"""

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        contents = [m.get("content") or "" for m in messages]
        if task == "gen_stage1":
            return json.dumps({"basis": "ledger", "speak": True, "intent": "补充新信息",
                               "increment": "回答最近发过什么"}, ensure_ascii=False)
        if role == "chat":
            return "beijing_time.txt 那份" if any("beijing_time.txt" in c for c in contents) else "想不起来了"
        return await super().complete(messages, role=role, task=task, temperature=temperature,
                                      json_mode=json_mode)


def test_private_question_is_answered_from_own_delivery(config, store, monkeypatch):
    """整条链路（策划 → 正文）都必须拿到交付物，答的是发文件那件事。"""
    now = NOW
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="今晚吃了鸭腿，还聊了鹈鹕骑自行车", ts=now - 9.5 * 3600)
    store.add_self_event("发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=now - 600, now=now)
    bot, adapter = make_bot(config, store, EvidenceModel())
    monkeypatch.setattr(time, "time", lambda: now)
    asyncio.run(bot.handle_event(private("你记得你刚刚给我发过什么吗")))
    assert adapter.texts[-1] == "beijing_time.txt 那份"
    # 策划与正文都看到了自我动作段与时间断层。
    for call in bot.llm.calls:
        contents = [m["content"] for m in call["messages"]]
        assert any("【我最近在别处做过的事" in c and "beijing_time.txt" in c for c in contents)
        assert any("时间断层" in c for c in contents)
        assert any("10 分钟前" in c for c in contents)


# ----------------------------------------------------------------------
# 回放 (b)：隔夜旧话被问起时不得称「刚刚」
# ----------------------------------------------------------------------


def test_overnight_old_words_are_not_at_the_top_of_new_evidence(config, store):
    """昨晚私聊旧话在历史里，但时间断层与自我动作段先说明「那是昨晚」。

    当前会话的问题不能只靠历史里那几句「刚聊过的」作答；上下文要让模型
    看到时间断层，且自我动作段（群里的交付）与别处近况都在。
    """
    config.chat.history_ttl_hours = 24
    now = NOW
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="今晚吃了鸭腿，还聊了鹈鹕骑自行车",
                          ts=now - 9.5 * 3600)
    store.add_self_event("发了文件 beijing_time.txt", source_chat="recording:room",
                         audience="group:room", ts=now - 18 * 60, now=now)
    store.add_raw_message(platform="recording", chat_type="private", chat_id="owner", sender_id="owner",
                          sender_name="机主", text="你记得你刚刚给我发过什么吗", ts=now)
    bot, _ = make_bot(config, store)
    context = asyncio.run(bot.build_context(private(), now=now))
    marker, = [m["content"] for m in context if "时间断层" in m["content"]]
    actions, = [m["content"] for m in context if "【我最近在别处做过的事" in m["content"]]
    assert "9 小时前" in marker
    assert "beijing_time.txt" in actions and "18 分钟前" in actions
