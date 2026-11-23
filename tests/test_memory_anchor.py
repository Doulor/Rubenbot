"""指称完整性：评价型断言必须有实体锚点，否则拦下、并账或丢弃留痕。"""

import asyncio
import json

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.anchor import (
    Anchor,
    annotate,
    evaluate,
    find_deictic,
    needs_anchor,
    needs_anchor_for_event,
    resolve_anchor,
)
from rubenbot.memory.store import MemoryStore

BASE_TS = 1_795_000_000.0


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, role="chat", task=None, temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages})
        return self.responses.pop(0) if self.responses else "{}"


@pytest.fixture()
def store():
    s = MemoryStore(":memory:", privacy_enabled=True)
    s.upsert_person("console", "u1", "阿花")
    yield s
    s.close()


def seed(store, texts, *, sender="u1", chat_id="g1", chat_type="group", start=BASE_TS):
    for index, text in enumerate(texts):
        store.add_raw_message(
            platform="console",
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender,
            sender_name="阿花",
            text=text,
            ts=start + index,
        )


def payload(facts=(), events=(), entities=()) -> str:
    return json.dumps(
        {"facts": list(facts), "events": list(events), "entities": list(entities)},
        ensure_ascii=False,
    )


def run_extract(store, response, *, is_owner=None):
    from rubenbot.memory.extract import MemoryExtractor

    llm = FakeLLM(response)
    extractor = MemoryExtractor(llm, store, MemoryConfig(extract_min_pending=1), is_owner=is_owner)
    results = asyncio.run(extractor.maybe_extract(now=BASE_TS + 60, force=True))
    return results, llm


# ----------------------------------------------------------------------
# 判定口径
# ----------------------------------------------------------------------


def test_deictic_and_subjectless_phrases_need_anchor():
    bad = [
        "TA 做过一个『随手做的』东西，硬性限制比较大，认为它用来推单向消息不错",
        "TA 认为它用来推单向消息不错",
        "TA 觉得这个平台限制太多",
        "TA 还是觉得那个东西不好用",
        "TA 说这个不错",
        "TA 觉得不错",
        "TA 觉得挺好的",
    ]
    for text in bad:
        assert needs_anchor(text), text
        assert find_deictic(text)
    # 自带对象或自带主语的正常写法不误伤：「觉得压力大」的对象是压力。
    for text in ("TA 住在杭州", "TA 是杭州的后端工程师", "TA 在这个群里负责组织活动",
                 "TA 在创业，觉得压力很大", "TA 觉得飞书挺好用", "TA 会弹吉他"):
        assert not needs_anchor(text), text


def test_quote_modifier_is_not_mistaken_for_the_entity_name():
    """『随手做的』是定语，不是实体名；真正的实体在引号外面。"""
    content = "TA 做过一个『随手做的』QQ 官方机器人桥"
    source = "我做过一个『随手做的』QQ 官方机器人桥，硬性限制比较大，用它来推单向消息不错"
    decision = evaluate(content, source_text=source)
    assert decision.ok and decision.anchor is not None
    assert decision.anchor.name == "QQ 官方机器人桥"


def test_generic_nouns_and_deictic_crumbs_are_not_anchorable():
    """「那个平台」「它插件」不是实体；出处里没有可识别专名时一律不落账。"""
    for source in ("我搞了个内部系统，觉得它挺好", "我在用 AstrBot 框架，觉得它插件生态不错"):
        decision = evaluate("TA 觉得它挺好", source_text=source)
        if "AstrBot" in source:
            assert decision.ok and decision.anchor.name == "AstrBot"
        else:
            assert not decision.ok and decision.anchor is None


def test_event_layer_only_blocks_deictic_evaluation():
    # 事件贴近原文，普通叙事里的「它」不拦；评价 + 指代才拦。
    assert needs_anchor_for_event("TA 认为它推单向消息不错")
    assert not needs_anchor_for_event("TA 用它做单推")
    assert not needs_anchor_for_event("TA 昨天去看了电影")


def test_resolve_anchor_prefers_quoted_source_entity():
    source = "我做过一个『随手做的』QQ 官方机器人桥，硬性限制比较大，用它来推单向消息不错"
    anchor = resolve_anchor("TA 认为它用来推单向消息不错", source)
    assert anchor == Anchor("QQ 官方机器人桥", "QQ 官方机器人桥", "known")


def test_resolve_anchor_rejects_ambiguous_multi_entity_source():
    source = "Claude Code 和 Cursor 都不错，用来推单向消息不错"
    assert resolve_anchor("TA 认为它用来推单向消息不错", source) is None
    # 正文自己写了唯一名字时可以直接锚定。
    assert resolve_anchor("TA 觉得 Claude Code 不错", source).name == "Claude Code"


def test_model_entity_must_appear_in_source():
    assert resolve_anchor("TA 认为它能用", "", model_entity="QQ 官方机器人桥") is None
    anchor = resolve_anchor("TA 认为它能用", "QQ 官方机器人桥 还行", model_entity="QQ 官方机器人桥")
    assert anchor == Anchor("QQ 官方机器人桥", "QQ 官方机器人桥", "model")


def test_annotate_only_when_name_missing():
    anchor = Anchor("飞书", "飞书", "known")
    assert annotate("关于飞书这个平台：TA 觉得限制多", anchor) == "关于飞书这个平台：TA 觉得限制多"
    assert annotate("TA 觉得这个平台限制多", anchor) == "关于「飞书」：TA 觉得这个平台限制多"


def test_evaluate_reports_reason_when_unanchored():
    decision = evaluate("TA 认为它不错", source_text="今天天气不错")
    assert decision.required and not decision.ok and decision.anchor is None
    assert decision.reason


# ----------------------------------------------------------------------
# 抽取管线：拦下、并账、丢弃留痕
# ----------------------------------------------------------------------


def test_unanchored_evaluative_fact_is_not_written_and_is_logged(store):
    seed(store, ["今天聊到那个东西挺好用的"])
    results, _ = run_extract(
        store,
        payload(facts=[{"content": "TA 认为它很好用", "entity": ""}]),
    )
    result = results[0]
    assert result.added == []
    assert store.list_memories() == []
    assert result.dropped == ["TA 认为它很好用"]
    logs = [entry for entry in store.list_logs() if entry.action == "dropped_unanchored"]
    assert len(logs) == 1
    assert logs[0].person_id == "console:u1"
    assert logs[0].before[0]["content"] == "TA 认为它很好用"
    assert logs[0].before[0]["kind"] == "fact"
    assert logs[0].after == []
    # 丢弃记录只作审计，不能撤销成一条记忆。
    assert store.restore(logs[0].id) is False


def test_anchored_fact_is_written_with_entity(store):
    seed(store, ["我做过一个『随手做的』QQ 官方机器人桥，限制比较大，用来推单向消息不错"])
    results, _ = run_extract(
        store,
        payload(facts=[{
            "content": "TA 认为它用来推单向消息不错",
            "entity": "QQ 官方机器人桥",
        }]),
    )
    assert results[0].added
    mem = store.list_memories()[0]
    assert mem.entity == "QQ 官方机器人桥"
    # 正文没写名字时补一个可回指的前缀，保证按实体能检索到。
    assert "QQ 官方机器人桥" in mem.content
    assert mem.source_ref.startswith("console:g1:")


def test_evaluative_fact_attaches_to_existing_entity_entry(store):
    seed(store, ["我做过一个『随手做的』QQ 官方机器人桥，硬性限制比较大，用来推单向消息不错"])
    first, _ = run_extract(
        store,
        payload(facts=[{
            "content": "TA 做过一个『随手做的』QQ 官方机器人桥",
            "entity": "QQ 官方机器人桥",
        }]),
    )
    assert len(first[0].added) == 1
    only = store.list_memories()[0]
    assert only.entity == "QQ 官方机器人桥"

    # 第二轮只留下评价，主语靠出处原话能补回来 → 并进同一实体条目，而不是独立成账。
    seed(store, ["就是那个 QQ 官方机器人桥，推单向消息确实不错"], start=BASE_TS + 100)
    new_raw = store.recent_messages("g1")[-1].id
    second, _ = run_extract(
        store,
        payload(facts=[{
            "content": "TA 认为它用来推单向消息不错",
            "entity": "",
            "source_msg_id": new_raw,
        }]),
    )
    assert second[0].dropped == []
    memories = store.list_memories()
    assert len(memories) == 1
    merged = memories[0]
    assert merged.id == only.id
    assert merged.entity == "QQ 官方机器人桥"
    assert "推单向消息" in merged.content
    assert "QQ 官方机器人桥" in merged.content
    # 并账写一条可撤销的 merge 日志；撤销能把正文恢复成并账前的样子。
    merge_log = next(entry for entry in store.list_logs() if entry.action == "merge")
    assert merge_log.before and merge_log.after
    assert store.restore(merge_log.id) is True
    assert store.get_memory(only.id).content == "TA 做过一个『随手做的』QQ 官方机器人桥"


def test_deictic_event_without_anchor_is_logged(store):
    seed(store, ["那个东西限制太多了吧"])
    results, _ = run_extract(
        store,
        payload(events=[{"summary": "TA 觉得那个东西限制太多"}]),
    )
    assert results[0].added_events == []
    assert store.list_events(person_id="console:u1") == []
    logs = [entry for entry in store.list_logs() if entry.action == "dropped_unanchored"]
    assert logs and logs[0].before[0]["kind"] == "event"
    assert logs[0].before[0]["content"] == "TA 觉得那个东西限制太多"


def test_multi_entity_conversation_each_line_points_back(store):
    """回放含多实体的对话：逐条能回指到实体与源消息。"""
    seed(
        store,
        [
            "飞书这个平台文档协作还行",
            "钉钉审批流程更顺",
            "飞书我主要用来传文档",
            "钉钉的消息提醒太吵了",
        ],
    )
    raw_ids = [row.id for row in store.recent_messages("g1")]
    results, _ = run_extract(
        store,
        payload(
            facts=[
                {"content": "TA 认为飞书文档协作还行", "entity": "飞书", "source_msg_id": raw_ids[0]},
                {"content": "TA 认为钉钉审批流程更顺", "entity": "钉钉", "source_msg_id": raw_ids[1]},
                # 指称不明：两个实体都在原话窗口里，不猜。
                {"content": "TA 觉得它太吵了", "entity": ""},
            ],
            entities=[
                {"name": "飞书", "message_ids": [str(raw_ids[0])]},
                {"name": "钉钉", "message_ids": [str(raw_ids[1])]},
            ],
        ),
    )
    result = results[0]
    assert result.dropped == ["TA 觉得它太吵了"]
    memories = {mem.entity: mem for mem in store.list_memories()}
    assert set(memories) == {"飞书", "钉钉"}
    for name, mem in memories.items():
        assert name in mem.content
        assert mem.source_ref.startswith("console:g1:")
    assert memories["飞书"].source_ref == f"console:g1:{raw_ids[0]}-{raw_ids[0]}"
    assert memories["钉钉"].source_ref == f"console:g1:{raw_ids[1]}-{raw_ids[1]}"


def test_plain_narrative_fact_still_written_without_entity(store):
    seed(store, ["我住在杭州", "我是做后端的"])
    results, _ = run_extract(
        store,
        payload(facts=[
            {"content": "TA 住在杭州", "category": "identity"},
            {"content": "TA 是做后端的", "category": "identity"},
        ]),
    )
    assert len(results[0].added) == 2
    assert [mem.entity for mem in store.list_memories()] == ["", ""]


def test_deictic_but_non_evaluative_narrative_is_not_blocked(store):
    """事件接近原文：普通叙事里的「它」不算缺主语。"""
    seed(store, ["我上周拿它当闹钟用了几天"])
    results, _ = run_extract(
        store,
        payload(events=[{"summary": "TA 拿它当闹钟用了几天", "source_msg_id": 1}]),
    )
    assert results[0].added_events == ["TA 拿它当闹钟用了几天"]
    assert results[0].dropped == []


def test_entity_ledger_never_guesses_for_a_message_that_does_not_name_it(store):
    """实体账里登记过的名字不能替一条没点名它的消息猜锚点。"""
    # 先在群里登记过这个名字，且原话里逐字出现
    store.add_raw_message(platform="console", chat_id="g1", chat_type="group", sender_id="u2",
                          text="飞书的文档协作还行", ts=BASE_TS - 10, processed=True)
    from rubenbot.memory.entities import EntityCandidate, EntityLedger

    ledger = EntityLedger(store)
    for message in store.recent_messages("g1"):
        ledger.observe_message(platform="console", chat_id="g1", message=message,
                               candidates=[EntityCandidate("飞书")])
    seed(store, ["那个平台限制太多了"], start=BASE_TS)
    results, _ = run_extract(
        store,
        payload(facts=[{"content": "TA 觉得那个平台限制太多", "source_msg_id": 3}]),
    )
    # 出处原话（"那个平台限制太多了"）本身没有名字，实体账虽在同群登记了「飞书」，
    # 但这条消息没有逐字提到它 —— 不能凭台账猜，只能丢弃留痕。
    assert results[0].dropped == ["TA 觉得那个平台限制太多"]
    assert store.list_memories() == []

    seed(store, ["飞书那个平台限制太多了"], start=BASE_TS + 10)
    new_id = store.recent_messages("g1")[-1].id
    results, _ = run_extract(
        store,
        payload(facts=[{"content": "TA 觉得那个平台限制太多", "source_msg_id": new_id}]),
    )
    assert results[0].dropped == []
    assert store.list_memories()[0].entity == "飞书"


# ----------------------------------------------------------------------
# 画像提炼：逐条可回指、无锚点不落行
# ----------------------------------------------------------------------


def _distill(store, response):
    from rubenbot.memory.distill import ProfileDistiller

    llm = FakeLLM(response)
    update = asyncio.run(ProfileDistiller(llm, store, MemoryConfig()).distill_person("console:u1", now=BASE_TS + 60))
    return update, llm


def test_profile_line_without_anchor_is_dropped_and_logged(store):
    event = store.add_event("console:u1", "TA 做过一个东西，限制比较大", ts=BASE_TS, source_chat="console:g1")
    response = json.dumps(
        {
            "stable": [
                {"text": "TA 认为它用来推单向消息不错", "evidence": [f"event:{event.id}"]},
                {"text": "TA 做过一个东西，限制比较大", "evidence": [f"event:{event.id}"]},
            ],
            "status": [],
            "hooks": [],
        },
        ensure_ascii=False,
    )
    update, _ = _distill(store, response)
    profile = store.get_profile("console:u1")
    # 出处原话里也没有实体名：评价行被拦下，只剩自带主语的叙事行。
    assert profile.stable_lines == ["TA 做过一个东西，限制比较大"]
    assert update.dropped_lines == 1
    logs = [entry for entry in store.list_logs() if entry.action == "dropped_unanchored"]
    assert logs and logs[0].before[0]["kind"] == "profile"
    assert logs[0].before[0]["content"] == "TA 认为它用来推单向消息不错"


def test_profile_line_anchor_filled_from_source(store):
    event = store.add_event(
        "console:u1",
        "TA 做过一个『随手做的』QQ 官方机器人桥，限制比较大",
        ts=BASE_TS,
        source_chat="console:g1",
    )
    response = json.dumps(
        {
            "stable": [{"text": "TA 认为它用来推单向消息不错", "evidence": [f"event:{event.id}"]}],
            "status": [],
            "hooks": [],
        },
        ensure_ascii=False,
    )
    update, _ = _distill(store, response)
    profile = store.get_profile("console:u1")
    # 出处原话能补回实体：不丢，正文加可回指前缀，实体单独落库。
    assert profile.stable_lines == ["关于「QQ 官方机器人桥」：TA 认为它用来推单向消息不错"]
    assert update.dropped_lines == 0
    assert store.get_profile_line_entities("console:u1") == {("stable", 0): "QQ 官方机器人桥"}


def test_profile_lines_in_multi_entity_replay_point_to_entity_and_source(store):
    """多实体对话的画像产物：每行能回指实体，证据链指向输入 id。"""
    now = BASE_TS
    first = store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", text="飞书文档协作还行", ts=now)
    second = store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", text="钉钉审批更顺", ts=now + 1)
    e1 = store.add_event("console:u1", "TA 说飞书文档协作还行", ts=now, source_chat="console:g1", source_raw_id=first)
    e2 = store.add_event("console:u1", "TA 说钉钉审批更顺", ts=now + 1, source_chat="console:g1", source_raw_id=second)
    response = json.dumps(
        {
            "stable": [
                {"text": "TA 认为飞书文档协作还行", "evidence": [f"event:{e1.id}"], "entity": "飞书"},
                {"text": "TA 认为钉钉审批更顺", "evidence": [f"event:{e2.id}"], "entity": "钉钉"},
                {"text": "TA 觉得它审批更顺", "evidence": [f"event:{e2.id}"]},
            ],
            "status": [],
            "hooks": [],
        },
        ensure_ascii=False,
    )
    update, _ = _distill(store, response)
    profile = store.get_profile("console:u1")
    assert profile.stable_lines == ["TA 认为飞书文档协作还行", "TA 认为钉钉审批更顺"]
    evidence = store.get_profile_evidence("console:u1")
    entities = store.get_profile_line_entities("console:u1")
    assert evidence[("stable", 0)] == [f"event:{e1.id}"]
    assert evidence[("stable", 1)] == [f"event:{e2.id}"]
    assert entities == {("stable", 0): "飞书", ("stable", 1): "钉钉"}
    assert update.dropped_lines == 1


# ----------------------------------------------------------------------
# 归纳：跨实体不许合并，合并结果沿用锚点
# ----------------------------------------------------------------------


def test_consolidate_refuses_cross_entity_merge_and_keeps_anchor(store):
    from rubenbot.memory.consolidate import Consolidator

    a = store.add_memory("console:u1", "TA 认为飞书文档协作还行", entity="飞书")
    b = store.add_memory("console:u1", "TA 认为钉钉审批更顺", entity="钉钉")
    response = json.dumps(
        [{"op": "merge", "ids": [a.id, b.id], "content": "TA 认为飞书文档协作还行钉钉审批更顺"}],
        ensure_ascii=False,
    )
    result = asyncio.run(Consolidator(FakeLLM(response), store, MemoryConfig()).consolidate_person("console:u1"))
    assert result.merged == 0
    assert any("不同实体" in reason for reason in result.rejected)
    assert {mem.entity for mem in store.list_memories()} == {"飞书", "钉钉"}


def test_consolidate_rejects_merge_that_drops_the_subject(store):
    """合并句只剩评价、主语并没了也拒绝；实体锚点不能替代正文里的主语。"""
    from rubenbot.memory.consolidate import Consolidator

    # 存量扫描补回的行：正文仍是指代，实体锚点另存在 entity 列。
    a = store.add_memory("console:u1", "TA 觉得它传文件很方便", entity="飞书")
    b = store.add_memory("console:u1", "TA 觉得它审批也顺", entity="飞书")
    response = json.dumps(
        [{"op": "merge", "ids": [a.id, b.id], "content": "TA 觉得它传文件很方便审批也顺"}],
        ensure_ascii=False,
    )
    result = asyncio.run(Consolidator(FakeLLM(response), store, MemoryConfig()).consolidate_person("console:u1"))
    assert result.merged == 0
    assert any("没有可回指的实体" in reason for reason in result.rejected)
    assert len(store.list_memories()) == 2


def test_plain_fact_with_subject_is_not_folded_into_entity_entry(store):
    """自带主语的完整句子照旧独立成账，不因同名实体被并掉。"""
    seed(store, ["我做过一个『随手做的』QQ 官方机器人桥，硬性限制比较大，用来推单向消息不错"])
    first, _ = run_extract(
        store,
        payload(facts=[{
            "content": "TA 做过一个『随手做的』QQ 官方机器人桥",
            "entity": "QQ 官方机器人桥",
        }]),
    )
    assert len(first[0].added) == 1
    seed(store, ["QQ 官方机器人桥我后来又改了一版，加了日志"], start=BASE_TS + 100)
    new_raw = store.recent_messages("g1")[-1].id
    second, _ = run_extract(
        store,
        payload(facts=[{
            "content": "TA 后来又改了一版 QQ 官方机器人桥，加了日志",
            "entity": "QQ 官方机器人桥",
            "source_msg_id": new_raw,
        }]),
    )
    assert second[0].dropped == []
    assert len(store.list_memories()) == 2


def test_model_entity_from_message_without_original_is_not_trusted(store):
    """说不清这条出自哪条原话时，模型自报的实体不能凭空作数。"""
    seed(store, ["那个东西限制太多了", "今天天气不错"])
    results, _ = run_extract(
        store,
        payload(facts=[{"content": "TA 认为它限制太多", "entity": "飞书"}]),
    )
    assert results[0].dropped == ["TA 认为它限制太多"]
    assert store.list_memories() == []


def test_multi_entity_replay_records_entity_and_source_per_line(store):
    """回放多实体对话：蒸馏产物逐条能回指实体与源消息（画像 + 事实两层）。"""
    seed(
        store,
        [
            "我最近在用飞书传文档，觉得它协作挺顺",
            "钉钉审批流程更顺，就是提醒太吵",
            "飞书的多维表格我也在用",
        ],
    )
    raw = {row.id: row.text for row in store.recent_messages("g1")}
    ids = sorted(raw)
    results, _ = run_extract(
        store,
        payload(facts=[
            {"content": "TA 觉得飞书协作挺顺", "entity": "飞书", "source_msg_id": ids[0]},
            {"content": "TA 觉得钉钉审批更顺，提醒太吵", "entity": "钉钉", "source_msg_id": ids[1]},
            {"content": "TA 还在用飞书多维表格", "entity": "飞书", "source_msg_id": ids[2]},
        ]),
    )
    assert results[0].dropped == []
    for mem in store.list_memories():
        assert mem.entity, mem
        assert mem.entity in mem.content
        # source_ref 精确指回这条断言对应的源消息。
        assert mem.source_ref.split(":")[-1].split("-")[0].isdigit()
        first_id = int(mem.source_ref.split(":")[-1].split("-")[0])
        assert raw[first_id]
    entity_to_source = {mem.entity: mem.source_ref for mem in store.list_memories()}
    assert entity_to_source["飞书"].endswith(f"{ids[0]}-{ids[0]}") or entity_to_source["飞书"].endswith(
        f"{ids[2]}-{ids[2]}"
    )
    assert entity_to_source["钉钉"].endswith(f"{ids[1]}-{ids[1]}")
