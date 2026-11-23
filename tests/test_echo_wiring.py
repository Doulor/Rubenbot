"""发送前复核接线：改写复述压稿与指向事实入 decision_log（第 18 项跟进）。

decision.py 的判定本身已有单测；这里只验证 core 的两处接线：

- 普通群聊发言的草稿被 `paraphrase_verdict` 判为改写复述时，发送前复核先
  重写一次，仍是复述就沉默（零出站、不记发言动量），并在 decision_log
  留下触发消息 id 与判定结果；
- 被 @ / 被回复 / 私聊 / 机主要求 / 操作交付与提醒类草稿不走此压制；
- `DecisionEngine.addressing()` 的指向事实随裁定写进 decision_log 的
  factors，只作核对，不改变是否发送。
"""

from __future__ import annotations

import asyncio

from conftest import ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from test_core import make_bot, private_event
from test_reminder import reminder_message

# 2026-11-23 10:17 主群 1125257581（decision 3135）的原话与插话草稿。
_SOURCE = "没想到是一天一个地区"
_ECHO = "一天一个地区，天天有新地方可以开"
_PLAN = '{"basis":"settled","speak":true,"intent":"接梗","increment":"接一句"}'


def group_event(text=_SOURCE, *, sender="宇轩", mentions=False, reply_to=None,
                reply_sender=None, msg_id=None) -> MessageEvent:
    """recording 平台的群消息；可带平台给出的引用与 @ 事实。"""
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender,
        sender_name=sender,
        text=text,
        mentions_bot=mentions,
        reply_to_id=reply_to,
        reply_sender_id=reply_sender or "",
        platform_msg_id=msg_id,
    )


def _log(store):
    logs = store.list_decision_logs()
    assert len(logs) == 1, logs
    return logs[0]


# ----------------------------------------------------------------------
# 改写复述：发送前复核压稿
# ----------------------------------------------------------------------


def test_plain_group_echo_draft_suppressed_after_one_retry(config, store):
    """普通群聊发言的改写复述草稿：重写一次仍复述就沉默，零出站。"""
    llm = ScriptedLLM({"fast": [_PLAN], "chat": [_ECHO, _ECHO]})
    bot, adapter = make_bot(config, store, llm)
    event = group_event(_SOURCE, sender="宇轩", msg_id="pm3135")

    asyncio.run(bot.handle_event(event))

    assert adapter.texts == []
    # 只生成两次（初稿 + 一次重写），没有把复述稿发出去。
    assert [c["task"] for c in llm.calls if c["role"] == "chat"] == ["gen_stage2", "gen_agreement_retry"]
    retry = next(c for c in llm.calls if c["task"] == "gen_agreement_retry")
    assert "复述" in "\n".join(m["content"] for m in retry["messages"])
    log = _log(store)
    assert log["reason"] == "paraphrase_veto" and log["should_reply"] is False
    note = log["factors"]["paraphrase"]
    assert note["verdict"] == "repetitive" and note["reason"] == "restated_source"
    # 记录里能按平台消息 id 找回是哪条触发的。
    assert note["trigger_msg_ids"] == ["pm3135"] and note["attempts"] == 2
    # 不记发言动量：压下的草稿不算接了一次话。
    assert bot.decision_engine.last_reply_at("recording:g1") is None


def test_echo_retry_with_new_content_is_sent(config, store):
    """重写后带来了原句没有的内容，正常发出。"""
    fresh = "一天一个地区，那收集地图得多花时间"
    llm = ScriptedLLM({"fast": [_PLAN], "chat": [_ECHO, fresh]})
    bot, adapter = make_bot(config, store, llm)
    # 草稿里「多」是原句没有的实词，判定有增量。
    asyncio.run(bot.handle_event(group_event(_SOURCE, sender="宇轩")))

    assert adapter.texts == [fresh]
    log = _log(store)
    assert log["reason"] == "planned" and "paraphrase" not in log["factors"]


def test_addressed_bot_same_content_still_answers(config, store):
    """同一内容 @ bot 直接问：照常回答，发送前复核不压。"""
    llm = ScriptedLLM({"fast": [_PLAN], "chat": [_ECHO]})
    bot, adapter = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event(_SOURCE, sender="宇轩", mentions=True)))

    assert adapter.texts == [_ECHO]
    assert len([c for c in llm.calls if c["role"] == "chat"]) == 1
    assert _log(store)["should_reply"] is True


def test_panda_direct_answer_not_suppressed(config, store):
    """panda 案：知识型提问的直答与触发消息字面重合低，不受复核影响。"""
    llm = ScriptedLLM({"fast": [_PLAN], "chat": ["用 yt-dlp，后面接视频链接"]})
    bot, adapter = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event("下载 YouTube 视频用哪个工具？", sender="甲")))

    assert adapter.texts == ["用 yt-dlp，后面接视频链接"]


def test_private_echo_draft_not_suppressed(config, store):
    """私聊里的同一句话不走普通群聊压制，照常回答。"""
    llm = ScriptedLLM({"chat": [_ECHO]})
    bot, adapter = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(private_event(_SOURCE)))

    assert adapter.texts == [_ECHO]


def test_owner_group_draft_not_suppressed(config, store):
    """机主开口（机主批次）不是普通群聊发言，不复述压制。"""
    config.computer.whitelist = ["recording:老板"]
    llm = ScriptedLLM({"fast": [_PLAN], "chat": [_ECHO]})
    bot, adapter = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event(_SOURCE, sender="老板")))

    assert adapter.texts == [_ECHO]


class _ReminderLLM(ScriptedLLM):
    """普通群聊轮里调一次 set_reminder，收尾正文恰好复述触发消息。"""

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        self.calls.append({"role": role, "task": task, "messages": messages})
        return _PLAN if role == "fast" else "记得十分钟后提醒取快递"

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append({"role": "chat", "task": kwargs.get("task"), "messages": messages})
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "记得十分钟后提醒取快递"}
        return reminder_message({"text": "取快递", "delay_minutes": 10})


def test_tool_delivery_draft_not_suppressed(config, store):
    """本轮用过工具（提醒等）的收尾正文不按改写复述压制。"""
    config.reminder.enabled = True
    llm = _ReminderLLM()
    bot, adapter = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event("十分钟后记得提醒我去取快递", sender="甲")))

    assert len(store.list_scheduled_jobs(status="pending")) == 1
    assert adapter.texts == ["记得十分钟后提醒取快递"]
    assert "paraphrase" not in _log(store)["factors"]


# ----------------------------------------------------------------------
# 指向事实落 decision_log
# ----------------------------------------------------------------------


def test_addressing_elsewhere_written_with_decision(config, store):
    """别人互答、没点 bot：裁定沉默，指向事实与折扣明细一并入档。"""
    llm = ScriptedLLM({"fast": [_PLAN], "chat": [_ECHO]})
    bot, adapter = make_bot(config, store, llm)
    event = group_event(_SOURCE, sender="宇轩", reply_to="m1", reply_sender="duoluo")

    asyncio.run(bot.handle_event(event))

    # 3135 回放：回复别人的普通群消息在策划前就被拦下，零出站、零生成。
    assert adapter.texts == [] and llm.calls == []
    log = _log(store)
    assert log["reason"] == "addressing_elsewhere" and log["should_reply"] is False
    assert log["factors"]["guard"] == "addressing_elsewhere"
    assert log["factors"]["addressing"] == {
        "to_bot": False, "to_peer": True, "replied_peer": True,
        "peers": ["duoluo"], "penalized": True, "discount": -1.0,
    }


def test_direct_bot_addressing_written_without_discount(config, store):
    """点到 bot：事实入档，但不打折。"""
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, _ = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event("在吗", mentions=True)))

    addressing = _log(store)["factors"]["addressing"]
    assert addressing["to_bot"] is True and addressing["penalized"] is False
    assert addressing["discount"] == 0.0


def test_plain_and_private_decisions_have_no_addressing_key(config, store):
    """没有明确指向的发言与私聊不写空壳字段。"""
    llm = ScriptedLLM({"fast": [_PLAN], "chat": ["今天的天气不错啊"]})
    bot, _ = make_bot(config, store, llm)

    asyncio.run(bot.handle_event(group_event("今天的天气不错", sender="甲")))
    asyncio.run(bot.handle_event(private_event("在吗")))

    for log in store.list_decision_logs():
        assert "addressing" not in log["factors"]
