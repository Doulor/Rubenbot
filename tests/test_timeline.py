"""时间线压缩：7 天前按人按周压成 digest，子事件标记 compressed；
90 天前只留高显著度事件与 digest。"""

import pytest

from rubenbot.config import MemoryConfig
from rubenbot.memory.store import MemoryStore
from rubenbot.memory.timeline import TimelineCompressor, week_key

NOW = 1_795_000_000.0
DAY = 86400.0


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    s.upsert_person("console", "u1", "阿花")
    s.upsert_person("console", "u2", "阿草")
    yield s
    s.close()


def make_compressor(store, **overrides) -> TimelineCompressor:
    return TimelineCompressor(store, MemoryConfig(**overrides))


def test_recent_events_are_untouched(store):
    store.add_event("console:u1", "TA 今天去爬山了", ts=NOW - 2 * DAY)
    result = make_compressor(store).run(now=NOW)
    assert result.digests == 0
    assert store.list_events(person_id="console:u1")[0].compressed is False


def test_week_digest_created_and_children_marked(store):
    ts_a = NOW - 20 * DAY
    ts_b = ts_a + 3600
    assert week_key(ts_a) == week_key(ts_b)
    a = store.add_event(
        "console:u1", "TA 周一去了图书馆", ts=ts_a, source_chat="console:g1",
        audience="group:g1", salience=0.3,
    )
    b = store.add_event(
        "console:u1", "TA 周三看了电影", ts=ts_b, source_chat="console:g1",
        audience="group:g1", salience=0.8, sensitivity="high",
    )
    result = make_compressor(store).run(now=NOW)
    assert result.digests == 1
    assert result.compressed_children == 2

    events = store.list_events(person_id="console:u1")
    digests = [e for e in events if e.kind == "digest"]
    assert len(digests) == 1
    digest = digests[0]
    assert "TA 周一去了图书馆" in digest.summary
    assert "TA 周三看了电影" in digest.summary
    # salience 取子事件最大；敏感度取最高；时间取子事件里最晚的
    assert digest.salience == pytest.approx(0.8)
    assert digest.sensitivity == "high"
    assert digest.ts == pytest.approx(ts_b)
    assert digest.source_chat == "console:g1"
    assert digest.audience == "group:g1"
    # 子事件还在，只是标记 compressed
    children = [e for e in events if e.kind == "event"]
    assert len(children) == 2
    assert all(child.compressed for child in children)


def test_different_people_and_weeks_grouped_separately(store):
    store.add_event("console:u1", "u1 的事 A", ts=NOW - 30 * DAY)
    store.add_event("console:u1", "u1 的事 B", ts=NOW - 10 * DAY)  # 不同周
    store.add_event("console:u2", "u2 的事", ts=NOW - 30 * DAY)
    result = make_compressor(store).run(now=NOW)
    assert result.digests == 3
    assert len(store.list_events(person_id="console:u1", kinds=["digest"])) == 2
    assert len(store.list_events(person_id="console:u2", kinds=["digest"])) == 1


def test_digest_is_not_recompressed(store):
    store.add_event("console:u1", "旧事", ts=NOW - 40 * DAY)
    make_compressor(store).run(now=NOW)
    second = make_compressor(store).run(now=NOW)
    assert second.digests == 0
    assert len(store.list_events(person_id="console:u1", kinds=["digest"])) == 1


def test_90_day_prune_keeps_salient(store):
    low = store.add_event("console:u1", "很久以前的琐事", ts=NOW - 120 * DAY, salience=0.3)
    high = store.add_event("console:u1", "很久以前的大事", ts=NOW - 120 * DAY, salience=0.9)
    cfg = MemoryConfig(timeline_compress_after_days=9999)  # 先只测裁剪
    TimelineCompressor(store, cfg).run(now=NOW)
    assert store.get_event(low.id).compressed is True
    assert store.get_event(high.id).compressed is False


def test_prune_keeps_digests(store):
    store.add_event("console:u1", "旧事", ts=NOW - 200 * DAY, salience=0.2)
    make_compressor(store).run(now=NOW)
    digest = store.list_events(person_id="console:u1", kinds=["digest"])[0]
    assert digest.compressed is False


def test_compressor_failure_is_contained(store):
    class BoomStore:
        def list_events(self, **kwargs):
            raise RuntimeError("库坏了")

    compressor = TimelineCompressor(BoomStore(), MemoryConfig())
    result = compressor.run(now=NOW)
    assert result.error
    assert result.digests == 0


def test_self_timeline_is_compressed_too(store):
    store.add_self_event("晚上好", source_chat="console:g1", audience="group:g1", ts=NOW - 30 * DAY)
    result = make_compressor(store).run(now=NOW)
    assert result.digests == 1
    digest = store.list_events(person_id="self", kinds=["digest"])[0]
    assert "晚上好" in digest.summary
