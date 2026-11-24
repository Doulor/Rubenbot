"""跨场合回放：核对策划和正文实际收到的证据，区分近况、原话与蒸馏结果。"""

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

    answer = "鸭腿"

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        evidence = any(self.answer in msg["content"] for msg in messages)
        if task == "gen_stage1":
            self.responses[role] = [json.dumps({
                "basis": "ledger" if evidence else "guess", "speak": True,
                "intent": "补充新信息", "increment": "回答对方",
            }, ensure_ascii=False)]
        elif role == "chat":
            self.responses[role] = [self.answer if evidence else UNKNOWN]
        elif role == "gate":
            # 反例不能靠「恰好模型拒绝」通过。
            self.responses[role] = [json.dumps({"items": [
                {"id": i, "verdict": "allow", "confidence": 1} for i in range(20)
            ]})]
        return await super().complete(messages, role=role, task=task, temperature=temperature, json_mode=json_mode)


def setup(config, store):
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


@pytest.mark.parametrize("privacy", [False, True])
@pytest.mark.parametrize("route", ["recent", "fact", "event"])
@pytest.mark.parametrize("source,target", [("group", "private"), ("private", "group")])
def test_owner_answers_across_places(config, store, monkeypatch, route, source, target, privacy):
    config.privacy.enabled = privacy

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
            if route == "event":
                assert record.source_chat_type == source  # 清理流水不应让私聊/群聊出处丢失
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
                assert any("【相关原话摘录】" in c and STATEMENT in c for c in contents)
            else:
                # raw 已清理，但直落事件仍有原话；抽取不再凭模型改写覆盖原文。
                expected = '机主今晚吃了鸭腿' if route == 'fact' else STATEMENT
                assert any(expected in c for c in contents)
                assert not any('【相关原话摘录】' in c and STATEMENT in c for c in contents)
        assert adapter.texts[-1] == "鸭腿"

    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start)
    asyncio.run(replay())


def test_sealing_last_statement_after_extraction_blocks_both_routes(config, store):
    config.privacy.enabled = True

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


@pytest.mark.parametrize("privacy", [False, True])
def test_owner_need_not_be_primary_speaker(config, store, privacy):
    config.privacy.enabled = privacy

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
        stages = [c for c in llm.calls if c["task"] in {"gen_stage1", "gen_stage2"}]
        assert [c["task"] for c in stages] == ["gen_stage1", "gen_stage2"]
        for call in stages:
            assert any("【相关原话摘录】" in m["content"] and STATEMENT in m["content"]
                       for m in call["messages"])

    asyncio.run(replay())


@pytest.mark.parametrize("privacy", [False, True])
def test_same_name_guest_cannot_claim_owner_via_raw_sender(config, store, privacy):
    config.privacy.enabled = privacy
    bot, llm, adapter = setup(config, store)
    asyncio.run(bot.handle_event(event("private", "owner", STATEMENT)))
    llm.calls.clear()
    guest = event("group", "guest", QUESTION)
    guest.raw = {"sender_id": "owner"}  # 昵称也与机主相同；真实适配器发送者仍是 guest。
    asyncio.run(bot.handle_event(guest))
    stages = [c for c in llm.calls if c["task"] in {"gen_stage1", "gen_stage2"}]
    assert [c["task"] for c in stages] == ["gen_stage1", "gen_stage2"]
    for call in stages:
        contents = [m["content"] for m in call["messages"]]
        if privacy:
            assert all("鸭腿" not in text for text in contents)
        else:
            assert any("【相关原话摘录】" in text and STATEMENT in text for text in contents)
    assert adapter.texts[-1] == (UNKNOWN if privacy else "鸭腿")


def test_recent_context_prefers_distilled_events_and_keeps_current_history_once(config, store):
    now = time.time()
    bot, _, _ = setup(config, store)
    store.upsert_person('recording', 'u3', '阿织')
    store.add_raw_message(platform='recording', chat_id='group-A', sender_id='u3', sender_name='阿织',
                          text='夜市游记的超长逐字稿', ts=now - 300)
    store.add_event('recording:u3', '夜市游记压缩版', ts=now - 120, source_chat='recording:group-A',
                    audience='group:group-A', now=now)
    store.add_event('recording:u3', '前天的景点', ts=now - 25 * 3600, source_chat='recording:old',
                    audience='group:old', now=now)
    store.add_raw_message(platform='recording', chat_id='group-B', sender_id='u3', sender_name='阿织',
                          text='丽江刚下了一阵雨', ts=now - 240)
    # bot 回复成功就有自我事件，却不代表这个场合已蒸馏过群友的话。
    store.add_self_event('收到啦', source_chat='recording:group-B', audience='group:group-B', ts=now - 230)
    current = event('group', 'peer', '有谁还在线？')
    asyncio.run(bot._record(current, now))
    messages = asyncio.run(bot.build_context(current, now=now))
    recent, = [m['content'] for m in messages if '【最近发生的事】' in m['content']]
    for needle, group, ts in [('夜市游记压缩版', 'group-A', now - 120), ('丽江刚下了一阵雨', 'group-B', now - 240)]:
        line, = [line for line in recent.splitlines() if needle in line]
        assert all(part in line for part in [group, '群', '阿织', time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))])
    assert recent.index('夜市游记压缩版') < recent.index('丽江刚下了一阵雨')
    assert all(part not in recent for part in [current.text, '超长逐字稿', '前天的景点'])
    assert sum(m['content'].count(current.text) for m in messages) == 1


def test_auto_search_finds_old_local_quotes_without_repeating_visible_history(config, store):
    config.chat.history_size = 2
    bot, _, _ = setup(config, store)
    now = time.time()
    old = event('group', 'peer', '给夜市订了两箱橘子')
    visible = event('group', 'peer', '夜市的账本收好了')
    current = event('group', 'owner', '夜市订了什么？')
    for item, age in [(old, 300), (visible, 100), (current, 0)]:
        asyncio.run(bot._record(item, now - age))
    contents = [m['content'] for m in asyncio.run(bot.build_context(current, now=now))]
    excerpt, = [c for c in contents if '【相关原话摘录】' in c]
    assert old.text in excerpt and 'main-group' in excerpt
    assert visible.text not in excerpt and current.text not in excerpt
    assert all(sum(c.count(item.text) for c in contents) == 1 for item in (old, visible, current))


def test_disabled_privacy_bypasses_judgment_on_every_read(config, store, monkeypatch):
    """读已有封存数据时，关闭不是「调用后默认 allow」，而是完全不裁定。"""
    from rubenbot.privacy import PrivacyGate

    calls = []

    async def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError('隐私关闭时不得调用裁定')

    monkeypatch.setattr(PrivacyGate, 'review', unexpected)
    monkeypatch.setattr(PrivacyGate, 'review_timeline', unexpected)
    bot, _, _ = setup(config, store)
    now = time.time()
    store.upsert_person('recording', 'peer', '机主')
    rid = store.add_raw_message(platform='recording', chat_type='private', chat_id='peer', sender_id='peer',
                                text='纸鹤项目的代号是蓝杉', ts=now - 400)
    store.block_raw_recall([rid])
    store.add_memory('recording:peer', '纸鹤项目由阿织管理', origin_chat='recording:peer',
                     audience='private', sensitivity='sealed', now=now - 360)
    store.add_event('recording:peer', '纸鹤项目昨日签约', source_chat='recording:peer', audience='private',
                    sensitivity='sealed', ts=now - 360, now=now - 360)
    current = event('group', 'peer', '纸鹤项目进展怎样？')
    messages = asyncio.run(bot.build_context(current, now=now))
    content = '\n'.join(m['content'] for m in messages)
    assert '纸鹤项目由阿织管理' in content
    assert any('【最近发生的事】' in m['content'] and '纸鹤项目昨日签约' in m['content'] for m in messages)
    assert any('【相关原话摘录】' in m['content'] and '纸鹤项目的代号是蓝杉' in m['content'] for m in messages)
    recalled = asyncio.run(bot.recall('', '纸鹤项目', event=current, now=now))
    assert all(key in recalled for key in ('纸鹤项目的代号是蓝杉', '纸鹤项目昨日签约', '纸鹤项目由阿织管理'))
    assert calls == []


@pytest.mark.parametrize('privacy', [True, False])
def test_high_sensitivity_group_to_group(config, store, privacy):
    """出发地本来就是群，不能靠私聊隔离碰巧挡住高敏原话。"""
    config.privacy.enabled = privacy
    bot, _, _ = setup(config, store)
    now = time.time()
    source = MessageEvent('recording', 'group', 'group-A', 'u3', '阿织', '工资这月降到九千八了。')
    asyncio.run(bot._record(source, now - 60))
    current = event('group', 'peer', '阿织的工资怎么样？')
    messages = asyncio.run(bot.build_context(current, now=now))
    for title in ('【最近发生的事】', '【相关原话摘录】'):
        assert any(title in m['content'] and source.text in m['content'] for m in messages) is not privacy
    recalled = asyncio.run(bot.recall('', '工资', event=current, now=now))
    assert (source.text in recalled) is not privacy


@pytest.mark.parametrize('privacy', [True, False])
def test_private_sessions_with_same_chat_id_on_different_platforms(config, store, privacy):
    config.privacy.enabled = privacy
    bot, _, _ = setup(config, store)
    now = time.time()
    secret = '灯塔航标重新涂成海蓝色了'
    source = MessageEvent('elsewhere', 'private', '42', '42', '小舟', secret)
    current = MessageEvent('recording', 'private', '42', '42', '过客', '灯塔航标是什么颜色？')
    asyncio.run(bot._record(source, now - 30))
    messages = asyncio.run(bot.build_context(current, now=now))
    for title in ('【最近发生的事】', '【相关原话摘录】'):
        assert any(title in m['content'] and secret in m['content'] for m in messages) is not privacy
    assert (secret in asyncio.run(bot.recall('', '航标', event=current, now=now))) is not privacy


def test_hot_switch_changes_visibility_without_erasing_mode(config, store):
    from copy import deepcopy

    config.privacy.enabled = True
    bot, _, _ = setup(config, store)
    now = time.time()
    source = event('private', 'peer', '灯塔方案暂定锈红色。')
    current = event('group', 'other', '灯塔方案定了吗？')
    store.set_privacy_mode('recording:peer', 'private')
    asyncio.run(bot._record(source, now - 90))
    for enabled in (True, False, True):
        changed = deepcopy(bot.config)
        changed.privacy.enabled = enabled
        bot.apply_config(changed, rebuild_llm=False)
        messages = asyncio.run(bot.build_context(current, now=now))
        for title in ('【最近发生的事】', '【相关原话摘录】'):
            assert any(title in m['content'] and source.text in m['content'] for m in messages) is not enabled
    assert store.get_privacy_mode('recording:peer') == 'private'


@pytest.mark.parametrize('privacy', [True, False])
def test_owner_identity_quote_obeys_privacy_in_all_group_reads(config, store, privacy):
    config.privacy.enabled = privacy
    bot, _, _ = setup(config, store)
    now = time.time()
    secret = '我的身份证号是测试编号甲'
    asyncio.run(bot._record(event('private', 'owner', secret), now - 60))
    current = event('group', 'owner', '我的身份证号是什么？')
    messages = asyncio.run(bot.build_context(current, now=now))
    for title in ('【最近发生的事】', '【相关原话摘录】'):
        assert any(title in m['content'] and secret in m['content'] for m in messages) is not privacy
    assert (secret in asyncio.run(bot.recall('', '身份证号', event=current, now=now))) is not privacy


def test_owner_correction_reaches_group_verbatim(config, store):
    """机主实测原句不是「我……」起头；即使拿掉近况层，也要自动查到。"""
    config.memory.timeline_max_chars = 0
    bot, llm, adapter = setup(config, store)
    llm.answer = "意大利火箭炮拌鹈鹕骑自行车"
    correction = "骗你的，其实我吃的意大利火箭炮拌鹈鹕骑自行车"
    asyncio.run(bot.handle_event(event("private", "owner", correction)))
    llm.calls.clear()
    asyncio.run(bot.handle_event(event("group", "owner", QUESTION)))
    assert adapter.texts[-1] == llm.answer
    assert [call['task'] for call in llm.calls] == ['gen_stage1', 'gen_stage2']
    for call in llm.calls:
        assert any("【相关原话摘录】" in msg['content'] and correction in msg['content']
                   for msg in call['messages'])


@pytest.mark.parametrize("target", ["private", "group"])
def test_non_owner_statement_reaches_other_places(config, store, target):
    """说话人不在场、也不是机主；按话题和人名从全域查原话。"""
    config.memory.timeline_max_chars = 0
    bot, llm, adapter = setup(config, store)
    source = MessageEvent('recording', 'group', 'group-A', 'u3', '阿织', '昨晚夜市最后一份鸭腿，被我们分啦。')
    asyncio.run(bot._record(source, time.time() - 61))
    destination = event(target, 'u4', '阿织昨晚在夜市吃啥了？')
    asyncio.run(bot.handle_event(destination))
    assert adapter.texts[-1] == '鸭腿'
    stages = [call for call in llm.calls if call['task'] in {'gen_stage1', 'gen_stage2'}]
    assert len(stages) == 2
    for call in stages:
        excerpt, = [m['content'] for m in call['messages'] if '【相关原话摘录】' in m['content']]
        assert all(value in excerpt for value in [source.text, 'group-A', '阿织'])


@pytest.mark.parametrize("privacy", [True, False])
@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("sender,questioner,sealed", [
    ("peer", "peer", False), ("owner", "owner", True), ("owner", "peer", False),
])
def test_private_evidence_follows_switch(config, store, sender, questioner, sealed, extract, privacy):
    config.privacy.enabled = privacy

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
        for call in stages:
            assert any("鸭腿" in m["content"] for m in call["messages"]) is not privacy
        assert adapter.texts[-1] == (UNKNOWN if privacy else "鸭腿")

    asyncio.run(replay())
