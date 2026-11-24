"""提醒触发的人称（第 29 项）：本人提醒本人时不许把自己的名字当第三人数。

现场（2026-11-24 08:31，主群 1125257581）：机主说「5分钟过后提醒我吃饭」，
即时确认「好，5分钟后叫你吃饭」人称正确；到点却发出「多萝让我喊你一声」
「先去吃饭吧。」——把请求者（也是唯一被提醒的人）写成了「多萝」。

根因：``scheduled_jobs.payload`` 里的 ``requester_name`` 被原样拼进触发提示，
渲染成「之前多萝让你提醒一件事」，模型按字面理解成「多萝（第三人）让我提醒你」。
这里回放同一场景，并覆盖三件事：

- 请求者=被提醒者时提示词不出现姓名，直接说事（第二人称）；
- 请求者≠被提醒者时姓名才出现，并写明谁托谁、该提醒谁；
- 关系只按结构化 id（``requester_id`` / ``recipient_id`` / 私聊会话）判定，
  姓名字符串只作台账，不能反过来推断「谁让提醒谁」。
"""

from __future__ import annotations

import asyncio

from conftest import ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.reminder import ReminderParties, parties_from_payload

from test_reminder import make_bot, private


# ----------------------------------------------------------------------
# 现场回放：群里「提醒我吃饭」，到点不出现自己的名字
# ----------------------------------------------------------------------


def group(text="5分钟过后提醒我吃饭", *, sender="3657338908", name="多萝") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="1125257581",
        sender_id=sender,
        sender_name=name,
        text=text,
        mentions_bot=True,
    )


def test_self_reminder_prompt_drops_requester_name(store):
    """本人提醒本人：提示里是第二人称，姓名只留在台账，不进生成上下文。"""
    llm = ScriptedLLM({"chat": ["到饭点啦，先去吃饭吧"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "该吃饭了", "delay_minutes": 5}, event=group()))
    job = store.list_scheduled_jobs(status="pending")[0]

    # 台账纪律：姓名照存，另存结构化 id，到点判定不靠姓名。
    assert job.payload["requester_name"] == "多萝"
    assert job.payload["requester_id"] == "recording:3657338908"
    assert job.payload["recipient_id"] == "recording:3657338908"

    asyncio.run(bot.handle_scheduled_job(job))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "该吃饭了" in prompt
    # 4805 的根本症状：请求者姓名被当成第三人称写进提示。
    assert "多萝" not in prompt
    assert "之前多萝让你提醒" not in prompt
    assert "对方亲口托你提醒" in prompt
    assert adapter.texts == ["到饭点啦，先去吃饭吧"]


def test_private_self_reminder_prompt_drops_requester_name(store):
    llm = ScriptedLLM({"chat": ["该吃药了"]})
    bot, adapter = make_bot(store, llm)
    asyncio.run(bot.set_reminder({"text": "吃药", "delay_minutes": 10}, event=private()))
    job = store.list_scheduled_jobs(status="pending")[0]
    asyncio.run(bot.handle_scheduled_job(job))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "阿花" not in prompt
    assert "私聊" in prompt and "对方亲口托你提醒" in prompt
    assert adapter.texts == ["该吃药了"]


# ----------------------------------------------------------------------
# 关系判定：只看结构化 id
# ----------------------------------------------------------------------


def test_same_person_when_ids_match_even_if_names_differ():
    """同一个人换个昵称（私聊里改名片）：id 相同就是本人提醒本人。"""
    parties = parties_from_payload(
        {"requester_id": "recording:u1", "requester_name": "多萝", "recipient_id": "recording:u1",
         "recipient_name": "多萝二号"},
    )
    assert parties.same_person is True


def test_legacy_payload_without_ids_is_treated_as_self():
    """旧 job（只有 requester_name）按本人提醒本人处理，宁可不提名字。"""
    parties = parties_from_payload({"text": "该吃饭了", "requester_name": "多萝"})
    assert parties.same_person is True


def test_private_recipient_comes_from_chat_when_payload_lacks_it():
    """私聊里接收者就是会话对方；请求者与它不同才写成 A 让提醒 B。"""
    same = parties_from_payload({"requester_id": "recording:u1"},
                                platform="recording", chat_type="private", chat_id="u1")
    assert same.same_person is True
    other = parties_from_payload({"requester_id": "recording:u2", "requester_name": "阿草"},
                                 platform="recording", chat_type="private", chat_id="u1")
    assert other.same_person is False


def test_different_people_render_both_names_with_relation():
    parties = ReminderParties(same_person=False, requester_name="阿草", recipient_name="阿花")
    line = parties.scene_line("吃药")
    assert "阿草" in line and "阿花" in line and "托你提醒" in line and "吃药" in line


def test_different_people_prompt_names_who_is_being_reminded(store):
    """A≠B 时两人都点名，并交代「你」指被提醒者，不把请求者的「我」当自己。"""
    from rubenbot.reminder import build_reminder_prompt

    messages = build_reminder_prompt(
        persona="你是 Ruben", text="开会", is_private=False, late=False,
        parties=ReminderParties(same_person=False, requester_name="阿草", recipient_name="阿花"),
    )
    prompt = messages[0]["content"]
    assert "阿草" in prompt and "阿花" in prompt
    assert "该提醒的人是「阿花」" in prompt
    assert "别把请求者说的「我」当成你" in prompt


# ----------------------------------------------------------------------
# planner 能力事实与工具注册表同源（dec 3571 类矛盾）
# ----------------------------------------------------------------------


def test_reminder_capability_listed_before_any_reminder_saved(store):
    """提醒工具在清单里就报能记，不要求本进程此前记过一条。"""
    from test_web_search import ToolLLM

    bot, _ = make_bot(store, ToolLLM())
    event = group(text="@Ruben 1分钟过后提醒我喝水")
    titles = {note.title for note in bot.capability_notes(event)}
    assert "记提醒" in titles

    bot.config.reminder.enabled = False
    assert "记提醒" not in {note.title for note in bot.capability_notes(event)}


def test_reminder_capability_withdrawn_when_chat_is_full(store):
    from test_web_search import ToolLLM

    bot, _ = make_bot(store, ToolLLM())
    event = group()
    for _ in range(bot.config.reminder.max_pending_per_chat):
        asyncio.run(bot.set_reminder({"text": "喝水", "delay_minutes": 10}, event=event))
    assert "记提醒" not in {note.title for note in bot.capability_notes(event)}


def test_non_owner_reminder_is_created_and_fired(store):
    """提醒对所有人开放：群友托提醒照样建 job，到点走同一条触发链。"""
    llm = ScriptedLLM({"chat": ["一分钟到了，喝口水吧"]})
    bot, adapter = make_bot(store, llm)
    event = group(text="@Ruben 1分钟过后提醒我喝多罗的，矿泉水", sender="1812473936", name="群友")
    answer = asyncio.run(bot.set_reminder({"text": "喝水", "delay_minutes": 1}, event=event))
    assert "已设好提醒" in answer
    job = store.list_scheduled_jobs(status="pending")[0]
    assert job.created_by == "recording:1812473936"
    assert job.payload["requester_id"] == "recording:1812473936"

    asyncio.run(bot.handle_scheduled_job(job))
    prompt = llm.calls[0]["messages"][0]["content"]
    # 群友给自己设的提醒，同样不把自己的昵称写成第三人称。
    assert "群友" not in prompt
    assert adapter.texts == ["一分钟到了，喝口水吧"]


def test_planner_prompt_carries_reminder_capability_for_guest(store):
    """分组回放：群友被 @ 托提醒时，策划步的能力索引里确实有「记提醒」，
    不会再写「定时提醒不在能力里」这类与执行行为相反的 reason。"""
    from test_web_search import ToolLLM

    llm = ToolLLM([{"content": "好，一分钟后就提醒你"}])
    bot, adapter = make_bot(store, llm)
    event = group(text="@Ruben 1分钟过后提醒我喝多罗的，矿泉水", sender="1812473936", name="群友")
    asyncio.run(bot.handle_event(event))
    planner = llm.stages["gen_stage1"][-1]["content"]
    assert "记提醒" in planner and "这轮我能做的事" in planner
