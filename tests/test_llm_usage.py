"""LLM 用量记录：落库/聚合、缺失 usage 记 NULL、重试与超时。"""

import asyncio
import json

import httpx
import pytest

from rubenbot.config import LLMConfig
from rubenbot.llm import LLMClient, LLMError, make_usage_recorder, usage_from_response
from rubenbot.memory.store import MemoryStore


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def client_for(handler, store=None, **config) -> LLMClient:
    """构造一个不等待的重试客户端；usage 直接写进 store（生产路径同款回调）。"""
    cfg = LLMConfig(api_key="k", chat_model="m-chat", **config)
    return LLMClient(
        cfg,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store) if store is not None else None,
        retry_delays=(0.0, 0.0),
    )


def run(client, **kwargs):
    async def scenario():
        try:
            return await client.complete([{"role": "user", "content": "hi"}], **kwargs)
        finally:
            await client.close()

    return asyncio.run(scenario())


def ok_response(content="ok", usage=None):
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def usage_rows(store):
    with store._lock:  # 测试里直接读表，验证 NULL 等细节
        rows = store._conn.execute(
            "SELECT task, model, prompt_tokens, completion_tokens, latency_ms, ok FROM llm_usage ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------------
# usage 解析
# ----------------------------------------------------------------------


def test_usage_from_response_variants():
    assert usage_from_response({"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}) == (3, 4, 7)
    # total 缺失时用 prompt+completion 推出来（两项都有才算）
    assert usage_from_response({"usage": {"prompt_tokens": 3, "completion_tokens": 4}}) == (3, 4, 7)
    # 只给一半：不伪造另一半
    p, c, t = usage_from_response({"usage": {"prompt_tokens": 3}})
    assert (p, c, t) == (3, None, None)
    assert usage_from_response({}) == (None, None, None)
    assert usage_from_response(None) == (None, None, None)
    # 非法值不猜
    assert usage_from_response({"usage": {"prompt_tokens": "很多"}}) == (None, None, None)
    assert usage_from_response({"usage": {"prompt_tokens": -1}}) == (None, None, None)


# ----------------------------------------------------------------------
# 落库与聚合
# ----------------------------------------------------------------------


def test_usage_written_for_successful_call(store):
    def handler(request):
        return ok_response("你好", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    assert run(client_for(handler, store), role="extract", task="extract") == "你好"
    rows = usage_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row["task"] == "extract"
    assert row["model"] == "m-chat"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5
    assert row["ok"] == 1
    assert row["latency_ms"] >= 0


def test_usage_missing_is_null_not_fake_zero(store):
    run(client_for(lambda request: ok_response("hi"), store))

    row = usage_rows(store)[0]
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    totals = store.usage_totals()
    assert totals["calls"] == 1
    assert totals["prompt_tokens"] == 0
    assert totals["usage_missing"] == 1  # 明确告诉统计口：这条不是 0 token


def test_usage_recorder_failure_does_not_break_call(store):
    def boom(_usage):
        raise RuntimeError("库挂了")

    client = LLMClient(
        LLMConfig(api_key="k", chat_model="m"),
        transport=httpx.MockTransport(lambda request: ok_response("还是能用")),
        usage_recorder=boom,
        retry_delays=(0.0,),
    )
    assert run(client) == "还是能用"


def test_usage_store_aggregations(store):
    now = 1_700_000_000.0
    store.add_llm_usage(task="chat", model="m-big", prompt_tokens=100, completion_tokens=50, latency_ms=200, ok=True, ts=now)
    store.add_llm_usage(task="chat", model="m-big", prompt_tokens=30, completion_tokens=10, latency_ms=100, ok=True, ts=now + 60)
    store.add_llm_usage(task="extract", model="m-small", prompt_tokens=7, completion_tokens=3, latency_ms=80, ok=True, ts=now + 120)
    store.add_llm_usage(task="extract", model="m-small", latency_ms=40, ok=False, ts=now + 86400 * 2)

    totals = store.usage_totals()
    assert totals["calls"] == 4
    assert totals["ok_calls"] == 3
    assert totals["failed_calls"] == 1
    assert totals["prompt_tokens"] == 137
    assert totals["completion_tokens"] == 63
    assert totals["total_tokens"] == 200
    assert totals["usage_missing"] == 1

    by_task = {item["task"]: item for item in store.usage_by_task()}
    assert by_task["chat"]["calls"] == 2
    assert by_task["chat"]["total_tokens"] == 190
    assert by_task["extract"]["failed_calls"] == 1
    assert by_task["extract"]["total_tokens"] == 10

    by_model = {item["model"]: item for item in store.usage_by_model()}
    assert by_model["m-big"]["total_tokens"] == 190
    assert by_model["m-small"]["calls"] == 2

    days = store.usage_by_day()
    assert len(days) == 2
    assert days[0]["day"] < days[1]["day"]  # 从早到晚
    assert sum(item["calls"] for item in days) == 4

    # since 过滤只算区间内
    recent = store.usage_totals(since=now + 100)
    assert recent["calls"] == 2


def test_usage_trim_keeps_recent(store):
    now = 1_700_000_000.0
    store.add_llm_usage(task="chat", model="m", ts=now - 400 * 86400)
    store.add_llm_usage(task="chat", model="m", ts=now - 10 * 86400)
    removed = store.trim_llm_usage(keep_days=180, now=now)
    assert removed == 1
    assert store.usage_totals()["calls"] == 1


# ----------------------------------------------------------------------
# 重试与超时
# ----------------------------------------------------------------------


def test_retries_5xx_then_succeeds_and_records_every_attempt(store):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="busy")
        return ok_response("终于", {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})

    client = client_for(handler, store, max_retries=2)
    assert run(client, role="chat") == "终于"
    assert len(attempts) == 3
    rows = usage_rows(store)
    assert [row["ok"] for row in rows] == [0, 0, 1]
    assert rows[-1]["prompt_tokens"] == 2


def test_retries_429_with_backoff(store, monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("rubenbot.llm.asyncio.sleep", fake_sleep)
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, text="slow down") if len(attempts) == 1 else ok_response("好")

    client = LLMClient(
        LLMConfig(api_key="k", chat_model="m", max_retries=2),
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
    )
    assert run(client) == "好"
    assert sleeps == [1.0]  # 第一次重试前等 1s
    assert [row["ok"] for row in usage_rows(store)] == [0, 1]


def test_retries_exhausted_raises_and_records_all_failures(store):
    def handler(request):
        return httpx.Response(500, text="boom")

    client = client_for(handler, store, max_retries=2)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}], role="consolidate")
        await client.close()

    asyncio.run(scenario())
    rows = usage_rows(store)
    assert len(rows) == 3  # 1 次 + 最多重试 2 次
    assert all(row["ok"] == 0 for row in rows)
    assert all(row["task"] == "consolidate" for row in rows)


def test_4xx_config_error_not_retried(store):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401, text="unauthorized")

    client = client_for(handler, store, max_retries=2)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())
    assert len(attempts) == 1
    assert len(usage_rows(store)) == 1


def test_network_error_retried_and_timeout_configurable(store):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("timeout")
        return ok_response("网络恢复了")

    client = client_for(handler, store, max_retries=1, timeout=0.5)
    assert run(client) == "网络恢复了"
    assert len(attempts) == 2
    assert client.config.timeout == 0.5
    rows = usage_rows(store)
    assert [row["ok"] for row in rows] == [0, 1]
    assert rows[0]["prompt_tokens"] is None


def test_default_retry_delays_are_one_and_three_seconds():
    from rubenbot.llm import retry_delay

    assert retry_delay(0) == 1.0
    assert retry_delay(1) == 3.0
    assert retry_delay(2) == 6.0


def test_malformed_json_body_is_error(store):
    client = client_for(lambda request: httpx.Response(200, text="<html>"), store, max_retries=0)

    async def scenario():
        with pytest.raises(LLMError):
            await client.complete([{"role": "user", "content": "hi"}])
        await client.close()

    asyncio.run(scenario())
    assert usage_rows(store)[0]["ok"] == 0


def test_recorder_accepts_async_callbacks(store):
    async def recorder(usage):
        store.add_llm_usage(
            task=usage.task,
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=usage.latency_ms,
            ok=usage.ok,
            ts=usage.ts,
        )

    client = LLMClient(
        LLMConfig(api_key="k", chat_model="m"),
        transport=httpx.MockTransport(
            lambda request: ok_response("x", {"prompt_tokens": 1, "completion_tokens": 2})
        ),
        usage_recorder=recorder,
        retry_delays=(0.0,),
    )
    assert run(client, task="summary") == "x"
    totals = store.usage_totals()
    assert totals["calls"] == 1
    assert totals["total_tokens"] == 3
