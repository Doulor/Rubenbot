"""召回查询的轻量语义归一：动作词归一 + 最近同类事件指代解析。

复刻 2026-11-24 13:55 的 magnolia 案：账本里是「点了 magnolia」，问句是
「你记得我刚刚给你推荐了什么歌吗」。字面检索对不上动作词，归一器要能把
「推荐」和「点了」归成同一类歌相关事件，并把指代解析到最近那条。
"""

import asyncio

import pytest

from rubenbot.memory.normalize import (
    FOOD,
    IMAGE,
    LINK,
    SONG,
    classify_event,
    expand_query,
    latest_matching,
    normalize_query,
    resolve_recent_event,
)
from rubenbot.memory.timeline import search_timeline

NOW = 1_800_000_000.0
PERSON = "console:u1"
MAGNOLIA_QUERY = "你记得我刚刚给你推荐了什么歌吗"


def add(store, text, *, ts=NOW - 600, person_id=PERSON, chat="console:g1", **overrides):
    return store.add_event(
        person_id,
        text,
        ts=ts,
        source_chat=chat,
        audience="group:g1",
        source_chat_type="group",
        now=ts,
        **overrides,
    )


# ----------------------------------------------------------------------
# magnolia 复刻
# ----------------------------------------------------------------------


def test_magnolia_query_resolves_to_clicked_song(store):
    add(store, "点了 magnolia", ts=NOW - 600)
    hit = asyncio.run(resolve_recent_event(store, MAGNOLIA_QUERY, now=NOW))
    assert hit is not None
    assert hit.text == "点了 magnolia"
    assert hit.ts == NOW - 600


def test_magnolia_query_finds_song_through_search_timeline(store):
    """归一后的查询直接交给现有检索路径就能命中，不依赖向量库。"""
    add(store, "点了 magnolia", ts=NOW - 600)
    rows = asyncio.run(search_timeline(store, expand_query(MAGNOLIA_QUERY), now=NOW))
    assert [row.text for row in rows] == ["点了 magnolia"]


def test_magnolia_query_with_speaker_scope(store):
    add(store, "点了 magnolia", ts=NOW - 600)
    add(store, "点了 晴天", ts=NOW - 300, person_id="console:u2")
    hit = asyncio.run(resolve_recent_event(store, MAGNOLIA_QUERY, person_id=PERSON, now=NOW))
    assert hit is not None and hit.text == "点了 magnolia"


# ----------------------------------------------------------------------
# 动作词归一
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ledger_text", "query"),
    [
        ("点了 magnolia", "你记得我刚刚给你推荐了什么歌吗"),
        ("推荐了一首 magnolia", "我刚刚点了什么歌来着"),
        ("推了一首 magnolia", "刚才那首歌叫什么"),
        ("安利了 magnolia", "你记得刚刚给我安利了什么歌吗"),
        ("分享了 magnolia", "刚刚那首是什么歌"),
    ],
)
def test_song_action_words_share_one_kind(store, ledger_text, query):
    add(store, ledger_text)
    hit = asyncio.run(resolve_recent_event(store, query, now=NOW))
    assert hit is not None, ledger_text
    assert hit.text == ledger_text


def test_other_kinds(store):
    add(store, "吃了鸭腿饭", ts=NOW - 900)
    add(store, "分享了一篇文章", ts=NOW - 800)
    add(store, "发了一张猫图", ts=NOW - 700)
    cases = {
        "我刚刚吃了什么": "吃了鸭腿饭",
        "刚才发的那个链接呢": "分享了一篇文章",
        "那张图片发我看看": "发了一张猫图",
    }
    for query, expected in cases.items():
        hit = asyncio.run(resolve_recent_event(store, query, now=NOW))
        assert hit is not None, query
        assert hit.text == expected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("点了 magnolia", SONG),
        ("推荐了《晴天》", SONG),
        ("推了一首老歌", SONG),
        ("安利了一个播客", SONG),
        ("刚刚吃了鸭腿", FOOD),
        ("点了外卖", FOOD),
        ("发了个链接", LINK),
        ("发了一张截图", IMAGE),
        ("今天天气不错", None),
        ("在吗", None),
    ],
)
def test_classify_event(text, kind):
    assert classify_event(text) == kind


def test_normalize_query_plan_fields(store):
    plan = normalize_query(MAGNOLIA_QUERY)
    assert plan.kind == SONG
    assert plan.action == "推荐"
    assert plan.recent and plan.past and plan.referential
    assert plan.expanded_query.startswith(plan.text)
    assert "点歌" in plan.expanded_query


def test_expand_query_leaves_unrelated_query_alone():
    assert expand_query("今天天气不错") == "今天天气不错"
    assert expand_query("在吗") == "在吗"


def test_expand_query_does_not_duplicate_existing_words():
    plan = normalize_query("刚刚点歌的那首歌叫什么")
    extra = dict(plan.terms)
    assert "点歌" not in extra  # 原查询里已有的整词不重复追加


# ----------------------------------------------------------------------
# 最近同类事件解析
# ----------------------------------------------------------------------


def test_resolution_picks_nearest_of_same_kind(store):
    add(store, "点了 magnolia", ts=NOW - 3000)
    add(store, "吃了夜宵", ts=NOW - 1200)
    add(store, "点了 晴天", ts=NOW - 300)
    hit = asyncio.run(resolve_recent_event(store, "刚刚那首歌叫什么", now=NOW))
    assert hit is not None and hit.text == "点了 晴天"


def test_resolution_prefers_event_over_raw_of_same_ts(store):
    add(store, "点了 magnolia", ts=NOW - 600)
    add(store, "点了 magnolia", ts=NOW - 600, kind="digest")
    hit = asyncio.run(resolve_recent_event(store, "刚刚那首歌叫什么", now=NOW))
    assert hit is not None and hit.kind == "event"


def test_no_recent_same_kind_returns_none(store):
    add(store, "吃了夜宵", ts=NOW - 600)
    assert asyncio.run(resolve_recent_event(store, MAGNOLIA_QUERY, now=NOW)) is None


def test_plain_question_is_not_treated_as_reference(store):
    add(store, "点了 magnolia")
    assert asyncio.run(resolve_recent_event(store, "magnolia 是什么", now=NOW)) is None


def test_recent_window_excludes_old_events(store):
    add(store, "点了 magnolia", ts=NOW - 3 * 86400)
    assert asyncio.run(resolve_recent_event(store, MAGNOLIA_QUERY, now=NOW)) is None
    # 显式给 since 时按调用方窗口查，而不是走默认 24 小时。
    hit = asyncio.run(
        resolve_recent_event(store, MAGNOLIA_QUERY, now=NOW, since=NOW - 7 * 86400)
    )
    assert hit is not None and hit.text == "点了 magnolia"


def test_future_events_are_ignored(store):
    add(store, "点了 magnolia", ts=NOW + 100)
    assert asyncio.run(resolve_recent_event(store, MAGNOLIA_QUERY, now=NOW)) is None


def test_latest_matching_accepts_plain_entries():
    class Entry:
        def __init__(self, text, ts, kind="event"):
            self.text, self.ts, self.kind = text, ts, kind

    plan = normalize_query(MAGNOLIA_QUERY)
    entries = [Entry("点了 magnolia", NOW - 600), Entry("吃了饭", NOW - 100)]
    hit = latest_matching(entries, plan, now=NOW)
    assert hit is not None and hit.text == "点了 magnolia"
