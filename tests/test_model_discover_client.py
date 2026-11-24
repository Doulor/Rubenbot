"""模型目录和一次性探测：指定提供商、固定开销与对外脱敏。"""

import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest

from rubenbot.config import LLMConfig, ModelTarget
from rubenbot.llm import LLMBudgetExceeded, LLMClient, LLMError, make_usage_recorder, request_result_code
from rubenbot.llm_budget import LLMBudgetConfig
from rubenbot.memory.store import MemoryStore

KEY = "secret-test-token"
TARGET = ModelTarget("selected", "selected-model", "https://selected.test/prefix/v1/", KEY)


def client_for(handler, **kwargs):
    # 故意与手动所选端点不同：请求不得通过聊天池或在失败后降级。
    return LLMClient(LLMConfig(models={"chat": ["other", "fallback"]}, max_retries=4),
                     transport=httpx.MockTransport(handler), **kwargs)


def test_discover_returns_sorted_ids_only_and_ignores_secret_echo():
    records = []

    def upstream(request):
        assert request.method == "GET"
        assert str(request.url) == "https://selected.test/prefix/v1/models"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.content == b""
        assert set(request.extensions["timeout"].values()) == {15.0}
        return httpx.Response(200, json={"data": [
            {"id": "z"}, {"id": "a", "owner": KEY}, {"id": "z"}, {"id": f"Bearer {KEY}"},
        ]})

    client = client_for(upstream, usage_recorder=records.append)
    assert asyncio.run(client.discover_models(TARGET)) == ["a", "z"]
    assert records == []


def test_empty_directory_is_valid_and_blank_key_is_not_sent():
    def upstream(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": []})

    assert asyncio.run(client_for(upstream).discover_models(replace(TARGET, api_key=""))) == []


@pytest.mark.parametrize("bad_url", [f"http://admin:{KEY}@localhost", f"https://host/v1?k={KEY}",
                                      f"https://host/#{KEY}", "file:///tmp/local", "https://foo:bad"])
@pytest.mark.parametrize("method", ["discover_models", "probe_model"])
def test_legacy_unsafe_address_rejected_locally(bad_url, method, caplog):
    client = client_for(lambda _: pytest.fail("不应发请求"))
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as error:
        asyncio.run(getattr(client, method)(replace(TARGET, base_url=bad_url)))
    assert KEY not in str(error.value) + caplog.text


@pytest.mark.parametrize("method", ["discover_models", "probe_model"])
@pytest.mark.parametrize("status", [302, 401, 404, 429, 503])
def test_status_errors_never_echo_upstream_or_follow_redirects(method, status, caplog):
    calls, records = [], []

    def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(status, text=f"error/secret-body {KEY}",
                              headers={"location": "https://elsewhere.test"},
                              extensions={"reason_phrase": f"secret-reason {KEY}".encode()})

    client = client_for(upstream, usage_recorder=records.append)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as error:
        asyncio.run(getattr(client, method)(TARGET))
    assert request_result_code(error.value) == f"http_{status}"
    assert len(calls) == 1
    assert len(records) == (1 if method == "probe_model" else 0)
    assert all(not record.ok for record in records)
    for private in (KEY, "secret-body", "secret-reason"):
        assert private not in str(error.value) + caplog.text


@pytest.mark.parametrize("method", ["discover_models", "probe_model"])
def test_broken_error_body_does_not_mask_known_status(method):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            assert method == "probe_model", "目录请求不应下载错误页"
            yield b"{"
            raise httpx.ReadTimeout("错误正文读取超时")

    client = client_for(lambda _: httpx.Response(401, stream=BrokenStream()))
    with pytest.raises(LLMError) as error:
        asyncio.run(getattr(client, method)(TARGET))
    assert request_result_code(error.value) == "http_401"


def test_failed_probe_keeps_actual_upstream_usage():
    records = []
    client = client_for(lambda _: httpx.Response(429, json={"error": "quota", "usage": {"prompt_tokens": 5}}),
                        usage_recorder=records.append)
    with pytest.raises(LLMError) as error:
        asyncio.run(client.probe_model(TARGET))
    assert request_result_code(error.value) == "http_429"
    assert len(records) == 1 and not records[0].ok and records[0].prompt_tokens == 5


@pytest.mark.parametrize("bad", [b"not JSON", b"[]", b'{"error": {}}', b'{"data": null}',
                                 b'{"data":[{}]}', b'{"data":[{"id": 55}]}',
                                 br'{"data":[{"id":"bad\ud800"}]}', b'{"error":{},"data":[]}'])
def test_invalid_model_catalog_is_not_empty_success(bad):
    client = client_for(lambda _: httpx.Response(200, content=bad))
    with pytest.raises(LLMError) as error:
        asyncio.run(client.discover_models(TARGET))
    assert request_result_code(error.value) == "invalid_response"


@pytest.mark.parametrize("bad", [b"bad JSON", b"{}", b'{"choices":{}}', b'{"choices":[{}]}',
                                 b'{"choices":[{"message":{}}]}', b'{"error":{"message":"fake 200"}}',
                                 b'{"choices":[{"message":{"content":42}}]}'])
def test_probe_rejects_fake_success_and_records_failure(bad):
    records = []
    client = client_for(lambda _: httpx.Response(200, content=bad), usage_recorder=records.append)
    with pytest.raises(LLMError) as error:
        asyncio.run(client.probe_model(TARGET))
    assert request_result_code(error.value) == "invalid_response"
    assert len(records) == 1 and not records[0].ok


@pytest.mark.parametrize("message,finish", [({"content": "a"}, "stop"), ({"content": ""}, "length")])
def test_probe_one_request_measured_and_budgeted(message, finish):
    store = MemoryStore(":memory:")
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://selected.test/prefix/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert set(request.extensions["timeout"].values()) == {15.0}
        body = json.loads(request.content)
        assert set(body) == {"messages", "max_tokens", "model"}
        assert body["max_tokens"] == 1 and body["model"] == TARGET.model
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 1}})

    try:
        budget = LLMBudgetConfig.parse({"tasks": {"model_probe": {"daily_calls": 1}, "chat": {"daily_calls": 0}}})
        client = LLMClient(LLMConfig(budget=budget), transport=httpx.MockTransport(upstream),
                           usage_recorder=make_usage_recorder(store))
        assert asyncio.run(client.probe_model(TARGET)) >= 0
        with pytest.raises(LLMBudgetExceeded):
            asyncio.run(client.probe_model(TARGET))
        assert len(calls) == 1
        totals = store.usage_by_task()
        assert len(totals) == 1
        assert (totals[0]["task"], totals[0]["calls"], totals[0]["total_tokens"]) == ("model_probe", 1, 8)
        ledger = client.budget.snapshot()["models"]["selected/selected-model"]
        assert (ledger["calls"], ledger["prompt_tokens"], ledger["completion_tokens"]) == (1, 7, 1)
    finally:
        store.close()


@pytest.mark.parametrize("method", ["discover_models", "probe_model"])
@pytest.mark.parametrize("exc,code", [(httpx.ConnectError, "request_failed"), (httpx.ReadTimeout, "timeout")])
def test_network_errors_are_safe(method, exc, code, caplog):
    def upstream(_):
        raise exc(f"network-secret {KEY}")

    records = []
    client = client_for(upstream, usage_recorder=records.append)
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as error:
        asyncio.run(getattr(client, method)(TARGET))
    assert request_result_code(error.value) == code
    assert KEY not in str(error.value) + caplog.text
    assert len(records) == (1 if method == "probe_model" else 0)


@pytest.mark.parametrize("method", ["discover_models", "probe_model"])
def test_wall_clock_timeout_even_if_transport_does_not_time_out(method, monkeypatch):
    monkeypatch.setattr("rubenbot.llm._DIAGNOSTIC_TIMEOUT", 0.025)
    records = []

    async def upstream(_):
        await asyncio.sleep(0.25)
        pytest.fail("总时限应在无响应时取消请求")

    client = client_for(upstream, usage_recorder=records.append)
    with pytest.raises(LLMError) as error:
        asyncio.run(getattr(client, method)(TARGET))
    assert request_result_code(error.value) == "timeout"
    assert len(records) == (1 if method == "probe_model" else 0)
    assert all(not record.ok and record.prompt_tokens is None for record in records)
