"""LLM 每日预算闸：按任务类型熔断、台账落库、跨日恢复与配置解析。"""

import asyncio
import json
import logging

import httpx
import pytest

from rubenbot.config import Config, LLMConfig
from rubenbot.llm import LLMBudgetExceeded, LLMClient, LLMError, make_usage_recorder
from rubenbot.llm_budget import (
    DEFAULT_DAILY_CALLS,
    DEFAULT_TASK_CALL_LIMITS,
    BudgetDenial,
    LLMBudgetConfig,
    LLMBudgetGate,
    ModelPrice,
    TaskLimit,
)
from rubenbot.memory.store import MemoryStore

T0 = 1_764_000_000.0  # 2025-11-24 左右，任意固定时刻；跨日测试只在它附近加减


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def gate_for(store, **budget):
    config = LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {}, **budget})
    return LLMBudgetGate(config, store, clock=Clock())


def ok_response(content="ok", usage=None):
    body = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def client_for(store, handler, *, budget=None, gate=None):
    cfg = LLMConfig(
        api_key="k",
        chat_model="m-chat",
        budget=LLMBudgetConfig.parse(budget) if budget is not None else LLMBudgetConfig.parse({}),
    )
    return LLMClient(
        cfg,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
        retry_delays=(0.0, 0.0),
        budget_gate=gate,
    )


def run(client, **kwargs):
    async def scenario():
        try:
            return await client.complete([{"role": "user", "content": "hi"}], **kwargs)
        finally:
            await client.close()

    return asyncio.run(scenario())


# ----------------------------------------------------------------------
# 配置解析与默认值
# ----------------------------------------------------------------------


def test_defaults_cover_every_observed_task():
    """线上出现过的任务类型都要有明确的默认上限，不能悄悄落到兜底值。"""
    config = LLMBudgetConfig()
    for task in DEFAULT_TASK_CALL_LIMITS:
        assert config.limit_for(task).daily_calls == DEFAULT_TASK_CALL_LIMITS[task]
    assert config.limit_for("从未见过的任务").daily_calls == DEFAULT_DAILY_CALLS
    assert config.default_daily_cost_usd is None
    assert config.pricing == {}
    assert config.enabled is True


def test_parse_overrides_and_null_means_unlimited():
    config = LLMBudgetConfig.parse(
        {
            "default_daily_calls": 10,
            "default_daily_cost_usd": 1.5,
            "tasks": {
                "chat": {"daily_calls": 3, "daily_cost_usd": 0.2},
                "extract": {"daily_calls": None},
                "vision": None,
                "gen_stage1": None,
            },
        }
    )
    assert config.limit_for("chat") == TaskLimit(3, 0.2)
    assert config.limit_for("extract").daily_calls is None
    assert config.limit_for("extract").daily_cost_usd == 1.5
    # 已知任务写 null 也能关掉内置默认
    assert config.limit_for("gen_stage1") == TaskLimit(None, None)
    # 未写覆盖的任务用默认值
    assert config.limit_for("other") == TaskLimit(10, 1.5)
    # 已知任务仍优先用内置峰值默认，而不是 default_daily_calls
    assert LLMBudgetConfig.parse({}).limit_for("gen_stage1").daily_calls == DEFAULT_TASK_CALL_LIMITS["gen_stage1"]


def test_parse_is_lenient_and_keeps_defaults(caplog):
    with caplog.at_level(logging.WARNING, logger="rubenbot.llm_budget"):
        config = LLMBudgetConfig.parse(
            {
                "enabled": "yes",
                "default_daily_calls": "abc",
                "default_daily_cost_usd": -1,
                "tasks": {"chat": "3"},
                "pricing": {"m": {"input": "x", "output": -2}},
            }
        )
    assert config.enabled is True
    assert config.default_daily_calls == DEFAULT_DAILY_CALLS
    assert config.default_daily_cost_usd is None
    assert "chat" not in config.tasks
    assert config.pricing["m"] == ModelPrice(0.0, 0.0)
    assert any("default_daily_calls" in record.message for record in caplog.records)


def test_config_loads_budget_section_via_llm_config():
    cfg = Config.from_dict(
        {"llm": {"chat_model": "m", "budget": {"tasks": {"chat": {"daily_calls": 7}}, "pricing": {"m": {"input": 0.5}}}}}
    )
    assert cfg.llm.budget.limit_for("chat").daily_calls == 7
    assert cfg.llm.budget.price_for("p", "m") == ModelPrice(0.5, 0.0)
    # 旧配置完全不受影响
    assert Config.from_dict({"llm": {"chat_model": "m"}}).llm.budget.default_daily_calls == DEFAULT_DAILY_CALLS


def test_pricing_lookup_order():
    config = LLMBudgetConfig.parse(
        {"pricing": {"*": {"input": 1, "output": 1}, "m": {"input": 2, "output": 3}, "p/m": {"input": 4, "output": 5}}}
    )
    assert config.price_for("p", "m") == ModelPrice(4, 5)
    assert config.price_for("other", "m") == ModelPrice(2, 3)
    assert config.price_for("other", "unknown") == ModelPrice(1, 1)
    assert LLMBudgetConfig.parse({}).price_for("p", "m") is None


# ----------------------------------------------------------------------
# 闸门本身
# ----------------------------------------------------------------------


def test_gate_denies_after_limit_and_logs_once(store, caplog):
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 2}}}),
        store,
        clock=Clock(),
    )
    with caplog.at_level(logging.WARNING, logger="rubenbot.llm_budget"):
        assert gate.denial("chat") is None
        gate.record("chat", provider="p", model="m", prompt_tokens=10, completion_tokens=5)
        assert gate.denial("chat") is None
        gate.record("chat", provider="p", model="m", prompt_tokens=10, completion_tokens=5)
        denial = gate.denial("chat")
        assert denial == BudgetDenial("chat", "calls", 2, 2)
        assert gate.denial("chat") is not None
    trips = [r for r in caplog.records if "LLM 预算熔断" in r.getMessage()]
    assert len(trips) == 1, "连续命中只应打一条熔断日志"
    assert "chat" in trips[0].getMessage()
    assert "上限 2" in trips[0].getMessage()


def test_gate_records_models_separately(store):
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None}), store, clock=Clock()
    )
    gate.record("chat", provider="a", model="m1", prompt_tokens=100, completion_tokens=20)
    gate.record("chat", provider="b", model="m1", prompt_tokens=50, completion_tokens=5)
    snap = gate.snapshot()
    assert snap["tasks"]["chat"]["calls"] == 2
    assert snap["tasks"]["chat"]["prompt_tokens"] == 150
    assert snap["models"]["a/m1"]["calls"] == 1
    assert snap["models"]["b/m1"]["calls"] == 1


def test_ledger_persists_across_gate_restart(store):
    """进程重启后从 meta 恢复当天台账，不会因为重启重置配额。"""
    clock = Clock()
    first = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 2}}}),
        store,
        clock=clock,
    )
    first.record("chat", provider="p", model="m", prompt_tokens=1, completion_tokens=1)
    first.record("chat", provider="p", model="m", prompt_tokens=1, completion_tokens=1)

    second = LLMBudgetGate(first.config, store, clock=clock)
    assert second.denial("chat") is not None
    assert second.snapshot()["tasks"]["chat"]["calls"] == 2


def test_cross_day_resets_and_new_day_starts_clean(store):
    clock = Clock()
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 1}}}),
        store,
        clock=clock,
    )
    gate.record("chat", provider="p", model="m")
    assert gate.denial("chat") is not None

    clock.now += 86400
    assert gate.denial("chat") is None, "跨日后应恢复放行"
    assert gate.snapshot()["tasks"] == {}


def test_rollover_logs_reset_once(store, caplog):
    clock = Clock()
    gate = LLMBudgetGate(LLMBudgetConfig.parse({}), store, clock=clock)
    gate.record("chat", provider="p", model="m")
    with caplog.at_level(logging.INFO, logger="rubenbot.llm_budget"):
        clock.now += 86400
        gate.denial("chat")
        gate.denial("chat")
    resets = [r for r in caplog.records if "跨日重置" in r.getMessage()]
    assert len(resets) == 1


def test_disabled_gate_never_denies_or_records(store):
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"enabled": False, "tasks": {"chat": {"daily_calls": 0}}}),
        store,
        clock=Clock(),
    )
    gate.record("chat", provider="p", model="m")
    assert gate.denial("chat") is None
    assert store.get_meta("llm_budget:ledger") is None


def test_gate_without_store_is_inert():
    config = LLMBudgetConfig.parse({"tasks": {"chat": {"daily_calls": 0}}})
    gate = LLMBudgetGate(config, None)
    assert gate.active is False
    assert gate.denial("chat") is None
    gate.record("chat", provider="p", model="m")  # 不抛


def test_cost_limit_uses_configured_prices(store):
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse(
            {
                "default_daily_calls": None,
                "tasks": {"vision": {"daily_cost_usd": 0.001}},
                "pricing": {"p/m": {"input": 1.0, "output": 2.0}},
            }
        ),
        store,
        clock=Clock(),
    )
    # 1000 prompt + 500 completion = 0.001 + 0.001 = 0.002 美元
    gate.record("vision", provider="p", model="m", prompt_tokens=1000, completion_tokens=500)
    denial = gate.denial("vision")
    assert denial is not None and denial.kind == "cost"
    assert "vision" in denial.message()


def test_no_pricing_means_cost_stays_zero(store):
    """没配单价时不能凭 token 编出费用：费用上限永远不触发。"""
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_cost_usd": 0.5}}}),
        store,
        clock=Clock(),
    )
    gate.record("chat", provider="p", model="m", prompt_tokens=10**6, completion_tokens=10**6)
    assert gate.snapshot()["tasks"]["chat"]["cost_usd"] == 0
    assert gate.denial("chat") is None


def test_corrupt_ledger_falls_back_to_empty(store, caplog):
    store.set_meta("llm_budget:ledger", "{not json")
    gate = LLMBudgetGate(
        LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 1}}}),
        store,
        clock=Clock(),
    )
    with caplog.at_level(logging.WARNING, logger="rubenbot.llm_budget"):
        assert gate.denial("chat") is None
    assert any("不是有效 JSON" in r.getMessage() for r in caplog.records)
    gate.record("chat", provider="p", model="m")
    assert json.loads(store.get_meta("llm_budget:ledger"))["tasks"]["chat"]["calls"] == 1


# ----------------------------------------------------------------------
# 客户端接线：超限回放 / 未超放行 / 隔离 / 跨日恢复
# ----------------------------------------------------------------------


def test_low_quota_stops_calls_but_keeps_other_tasks_and_bot_alive(store, caplog):
    """回放超限：某任务连续调用被截停、日志有熔断行、其它任务照常。"""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return ok_response("ok", {"prompt_tokens": 10, "completion_tokens": 5})

    budget = {"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 2}}}
    client = client_for(store, handler, budget=budget)

    with caplog.at_level(logging.WARNING, logger="rubenbot.llm_budget"):
        assert run(client, task="chat") == "ok"
        assert run(client, task="chat") == "ok"
        with pytest.raises(LLMBudgetExceeded) as excinfo:
            run(client, task="chat")
        # 熔断后同一类任务再调还是被截停，且没有发往上游
        with pytest.raises(LLMBudgetExceeded):
            run(client, task="chat")
        # 其它任务类型不受影响
        assert run(client, task="extract") == "ok"

    assert len(calls) == 3, "超限后不应再有上游请求"
    assert excinfo.value.task == "chat"
    assert isinstance(excinfo.value, LLMError)
    trips = [r for r in caplog.records if "LLM 预算熔断" in r.getMessage()]
    assert len(trips) == 1
    assert "chat" in trips[0].getMessage()
    # 台账仍记账：chat 2 次、extract 1 次；熔断后的尝试不计
    snap = json.loads(store.get_meta("llm_budget:ledger"))
    assert snap["tasks"]["chat"]["calls"] == 2
    assert snap["tasks"]["extract"]["calls"] == 1
    assert snap["models"]["default/m-chat"]["calls"] == 3


def test_usage_table_still_written_while_gate_active(store):
    client = client_for(
        store,
        lambda request: ok_response("ok", {"prompt_tokens": 3, "completion_tokens": 4}),
        budget={"default_daily_calls": 100},
    )
    assert run(client, task="extract") == "ok"
    assert store.usage_totals()["calls"] == 1
    assert store.usage_by_task()[0]["task"] == "extract"


def test_denial_is_not_retried(store):
    """熔断不是网络故障：不按 max_retries 继续尝试，也不触发退避。"""
    attempts = []

    def handler(request):
        attempts.append(1)
        return ok_response("ok")

    client = client_for(
        store,
        handler,
        budget={"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 1}}},
    )
    client.config.max_retries = 5
    assert run(client, task="chat") == "ok"
    with pytest.raises(LLMBudgetExceeded):
        run(client, task="chat")
    assert len(attempts) == 1


def test_budget_trip_does_not_failover_to_next_model(store):
    """模型池里切下一个模型只会多烧一次，预算熔断必须直接停。"""
    attempts = []

    def handler(request):
        attempts.append(json.loads(request.content)["model"])
        return ok_response("ok")

    cfg = LLMConfig(
        api_key="k",
        chat_model="m-chat",
        models={"chat": ["m-a", "m-b"]},
        budget=LLMBudgetConfig.parse({"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 1}}}),
    )
    client = LLMClient(
        cfg,
        transport=httpx.MockTransport(handler),
        usage_recorder=make_usage_recorder(store),
        retry_delays=(0.0,),
    )
    assert run(client, task="chat") == "ok"
    with pytest.raises(LLMBudgetExceeded):
        run(client, task="chat")
    assert attempts == ["m-a"]


def test_new_client_after_restart_still_trips(store):
    """模拟重启：新建客户端仍从同一张台账看到当天已超限。"""
    budget = {"default_daily_calls": None, "tasks": {"chat": {"daily_calls": 1}}}
    handler = lambda request: ok_response("ok")  # noqa: E731

    first = client_for(store, handler, budget=budget)
    first.budget._clock = Clock(T0)
    assert run(first, task="chat") == "ok"

    second = client_for(store, handler, budget=budget)
    second.budget._clock = Clock(T0 + 3600)
    with pytest.raises(LLMBudgetExceeded):
        run(second, task="chat")

    # 次日恢复：同一张台账跨日后放行
    third = client_for(store, handler, budget=budget)
    third.budget._clock = Clock(T0 + 86400)
    assert run(third, task="chat") == "ok"


def test_cost_limit_stops_calls_end_to_end(store):
    """费用档：按单价估算超限即停，调用不再发往上游。"""
    calls = []

    def handler(request):
        calls.append(1)
        return ok_response("ok", {"prompt_tokens": 1000, "completion_tokens": 1000})

    client = client_for(
        store,
        handler,
        budget={
            "default_daily_calls": None,
            "tasks": {"chat": {"daily_cost_usd": 0.002}},
            "pricing": {"m-chat": {"input": 1.0, "output": 0.0}},
        },
    )
    assert run(client, task="chat") == "ok"
    assert run(client, task="chat") == "ok"
    with pytest.raises(LLMBudgetExceeded) as excinfo:
        run(client, task="chat")
    assert excinfo.value.kind == "cost"
    assert len(calls) == 2


def test_budget_config_roundtrip_through_yaml(tmp_path):
    from rubenbot.config import Config

    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n"
        "  chat_model: m\n"
        "  budget:\n"
        "    enabled: true\n"
        "    default_daily_calls: 50\n"
        "    tasks:\n"
        "      chat:\n"
        "        daily_calls: 5\n"
        "        daily_cost_usd: 0.5\n"
        "    pricing:\n"
        "      deepseek/deepseek-chat: {input: 0.27, output: 1.1}\n",
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.llm.budget.limit_for("chat") == TaskLimit(5, 0.5)
    assert cfg.llm.budget.limit_for("extract").daily_calls == DEFAULT_TASK_CALL_LIMITS["extract"]
    assert cfg.llm.budget.price_for("deepseek", "deepseek-chat") == ModelPrice(0.27, 1.1)
