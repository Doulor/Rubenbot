"""机主全局记忆：两条独立通道回放同一实测对话，并检查裁定和正文都没看到禁带材料。"""

import asyncio
import json
import time

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.persona import Persona


# 机主在群里 @bot，一分钟后转到私聊。两个输入保留 2026-11-22 的原句。
STATEMENT = "我今晚吃的是鸭腿你知道吗"
QUESTION = "我今晚吃的啥你知道吗"
UNKNOWN = "不知道呢，你吃的啥"


class EvidenceLLM(ScriptedLLM):
    """不预排肯定答案：回复只取决于生成请求里实际收到的证据。"""

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        evidence = any("鸭腿" in msg["content"] for msg in messages)
        if task == "gen_stage1":
            self.responses[role] = [json.dumps({
                "basis": "ledger" if evidence else "guess", "speak": True,
                "intent": "补充新信息", "increment": "回答对方",
            }, ensure_ascii=False)]
        elif role == "chat":
            self.responses[role] = ["鸭腿" if evidence else UNKNOWN]
        elif role == "gate":
            # 反例不能靠「恰好模型拒绝」通过。
            self.responses[role] = [json.dumps({"items": [
                {"id": i, "verdict": "allow", "confidence": 1} for i in range(20)
            ]})]
        return await super().complete(messages, role=role, task=task, temperature=temperature, json_mode=json_mode)


def setup(config, store):
    config.planner.planner_gate = True
    config.computer.whitelist = ["recording:owner"]
    llm = EvidenceLLM()
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是群友。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, llm, adapter


def event(place, sender, text):
    # 昵称故意相同：只有已配置的平台账号是机主。
    return MessageEvent("recording", place, sender if place == "private" else "main-group",
                        sender, "机主", text, mentions_bot=place == "group")


@pytest.mark.parametrize("route", ["recent", "fact", "event"])
@pytest.mark.parametrize("source,target", [("group", "private"), ("private", "group")])
def test_owner_answers_across_places(config, store, monkeypatch, route, source, target):
    async def replay():
        bot, llm, adapter = setup(config, store)
        first = event(source, "owner", STATEMENT)
        await bot.handle_event(first)
        if route != "recent":
            # 一顿晚饭也可能只被提炼为事件，而不是耐久事实。
            key = "content" if route == "fact" else "summary"
            llm.responses["extract"] = [json.dumps({route + "s": [{
                key: "机主今晚吃了鸭腿", "stability": "temporal", "self_reported": True,
                "source_msg_id": first.raw_message_id,
            }]}, ensure_ascii=False)]
            await bot.extractor.maybe_extract(now=start, force=True)
            # 独立验长期通道：仍在一分钟后提问，但模拟已处理的原始流水已被清理。
            store.trim_raw(0)
            assert store.raw_message_by_id(first.raw_message_id) is None
            records = store.list_memories if route == "fact" else store.list_events
            record, = records(person_id="recording:owner")
            assert record.audience == "owner"
        else:
            assert not store.list_memories(person_id="recording:owner")
        llm.calls.clear()
        monkeypatch.setattr(time, "time", lambda: start + 60)
        await bot.handle_event(event(target, "owner", QUESTION))
        assert [c["task"] for c in llm.calls] == ["gen_stage1", "gen_stage2"]
        for call in llm.calls:
            contents = [m["content"] for m in call["messages"]]
            assert any(QUESTION in c for c in contents)
            if route == "recent":
                assert any("【机主最近说过】" in c and STATEMENT in c for c in contents)
            else:
                assert any("机主今晚吃了鸭腿" in c for c in contents)
                assert all(STATEMENT not in c for c in contents)
        assert adapter.texts[-1] == "鸭腿"

    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start)
    asyncio.run(replay())


def test_sealing_last_statement_after_extraction_blocks_both_routes(config, store):
    async def replay():
        bot, llm, adapter = setup(config, store)
        first = event("private", "owner", STATEMENT)
        await bot.handle_event(first)
        evidence = {"self_reported": True, "source_msg_id": first.raw_message_id}
        llm.responses["extract"] = [json.dumps({
            "facts": [{**evidence, "content": "机主今晚吃了鸭腿"}],
            "events": [{**evidence, "summary": "机主今晚吃了鸭腿"}],
        }, ensure_ascii=False)]
        await bot.extractor.maybe_extract(force=True)
        assert store.list_memories(person_id="recording:owner")[0].audience == "owner"
        await bot.handle_event(event("private", "owner", "刚才那句保密"))
        llm.calls.clear()
        await bot.handle_event(event("group", "owner", QUESTION))
        assert adapter.texts[-1] == UNKNOWN
        assert all("鸭腿" not in m["content"] for c in llm.calls for m in c["messages"])

    asyncio.run(replay())


def test_owner_need_not_be_primary_speaker(config, store):
    async def replay():
        bot, llm, adapter = setup(config, store)
        await bot.handle_event(event("private", "owner", STATEMENT))
        llm.calls.clear()
        bot.merger.window = bot.merger.hard_window = 10
        for sender, text in [("owner", QUESTION), ("peer", "我也在听"), ("peer", "好奇")]:
            await bot.handle_event(event("group", sender, text))
        batch, = bot.merger.flush_all()
        assert batch.primary_sender == "peer"
        await bot.process_round(batch)
        assert adapter.texts[-1] == "鸭腿"
        assert any("【机主最近说过】" in m["content"] and STATEMENT in m["content"]
                   for m in llm.calls[0]["messages"])

    asyncio.run(replay())


@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("sender,questioner,sealed", [
    ("peer", "peer", False), ("owner", "owner", True), ("owner", "peer", False),
])
def test_private_evidence_stays_out_of_group(config, store, sender, questioner, sealed, extract):
    async def replay():
        bot, llm, adapter = setup(config, store)
        statement = "请保密，" + STATEMENT if sealed else STATEMENT
        first = event("private", sender, statement)
        await bot.handle_event(first)
        if extract:
            llm.responses["extract"] = [json.dumps({"facts": [{
                "content": "机主今晚吃了鸭腿", "self_reported": True,
                "source_msg_id": first.raw_message_id, "sensitivity": "sealed" if sealed else "normal",
            }]}, ensure_ascii=False)]
            await bot.extractor.maybe_extract(force=True)
            assert store.list_memories(person_id="recording:" + sender)
        llm.calls.clear()
        group_question = event("group", questioner, QUESTION)
        group_question.chat_id = first.chat_id  # 即便群号恰好与私聊号相同，也不是同一场合
        await bot.handle_event(group_question)
        stages = [c for c in llm.calls if c["task"] in {"gen_stage1", "gen_stage2"}]
        assert len(stages) == 2
        assert all("鸭腿" not in m["content"] for c in stages for m in c["messages"])
        assert adapter.texts[-1] == UNKNOWN

    asyncio.run(replay())
