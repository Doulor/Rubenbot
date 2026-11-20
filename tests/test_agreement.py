"""附和闸门只拦长复述；经主循环验证重写次数和工具副作用。"""

import asyncio
import re

import pytest

from conftest import ScriptedLLM
from rubenbot.agreement import is_repetitive_agreement
from test_core import make_bot, private_event
from test_reminder import reminder_message


SOURCE = "十分钟后记得提醒我去取快递"
PLAN = '{"intent":"接梗","increment":"接住取快递的梗"}'


@pytest.mark.parametrize(("source", "reply"), [
    (SOURCE, SOURCE),
    (SOURCE, f"嗯，确实，{SOURCE}啊。"),
    ("这个版本，明天才会开放下载", "这个版本明天才会开放下载，没错。"),
    ("一二三四五六七八九十甲", "一二三四五六七八九十甲"),
    ("剧场明晚开放线上售票，说起来，线下窗口也会同步开售", "剧场明晚开放线上售票，线下窗口也会同步开售"),
])
def test_long_echo(source, reply):
    assert is_repetitive_agreement(source, reply)


@pytest.mark.parametrize(("source", "reply"), [
    ("一二三四五六七八九十", "一二三四五六七八九十"),
    ("嗯", "嗯"), (SOURCE, ""), ("", SOURCE),
    (SOURCE, f"「{SOURCE}」——哪个驿站？"),
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票，限量"),
    ("今晚八点剧场开放线上售票", "今晚八点剧场不开放线上售票"),
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票？"),
    ("今晚8点剧场开放线上售票", "今晚9点剧场开放线上售票"),
    ("今天轮到小陈去车站接小李", "今天轮到小李去车站接小陈"),
    ("不允许把这里的书和报纸带回家", "允许把这里的书和报纸带回家"),
    ("周末郊区除了有雨还有大风，剧场明晚开放线上售票", "剧场明晚开放线上售票，没错"),
    ("我想下班之后去看音乐剧", "我中午就约了摄影师拍照"),
])
def test_short_or_new_information(source, reply):
    assert not is_repetitive_agreement(source, reply)


@pytest.mark.parametrize(("rewrite", "sent"), [("好", ["好"]), (SOURCE, []), ("【不回复】", [])])
def test_core_rewrites_once_or_stays_silent(config, store, rewrite, sent):
    config.generation.two_stage.enabled = True
    llm = ScriptedLLM({"fast": [PLAN], "chat": [SOURCE, rewrite]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event(SOURCE)))

    assert adapter.texts == sent
    assert [m.text for m in store.recent_messages("u1")] == [SOURCE, *sent]
    assert len(llm.chat_messages) == 2
    retry = [call for call in llm.calls if call["task"] == "gen_agreement_retry"]
    assert len(retry) == 1
    prompt = "\n".join(m["content"] for m in retry[0]["messages"])
    assert "角度" in prompt and re.search(r"[三3](?:个)?字(?:以?内)", prompt)


def test_quoting_new_information_needs_no_retry(config, store):
    config.generation.two_stage.enabled = True
    reply = f"「{SOURCE}」——哪个驿站？"
    llm = ScriptedLLM({"fast": [PLAN], "chat": [reply]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event(SOURCE)))

    assert adapter.texts == [reply]
    assert len(llm.chat_messages) == 1


def test_core_retry_cannot_repeat_a_tool_action(config, store):
    class ReminderLLM(ScriptedLLM):
        tool_requests = 0

        async def chat_with_tools(self, messages, tools, **kwargs):
            self.tool_requests += 1
            # 只要模型拿到工具，就会在每次新生成时再设一遍提醒。
            if messages[-1]["role"] == "tool":
                return {"content": SOURCE}
            return reminder_message({"text": "取快递", "delay_minutes": 10})

    config.generation.two_stage.enabled = True
    config.reminder.enabled = True
    llm = ReminderLLM({"fast": [PLAN], "chat": ["记下了"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event(SOURCE)))

    assert llm.tool_requests == 2  # 只含初稿的一次调用和一次收尾
    assert len(store.list_scheduled_jobs(status="pending")) == 1
    assert [c["task"] for c in llm.calls if c["role"] == "chat"] == ["gen_agreement_retry"]
    assert adapter.texts == ["记下了"]
