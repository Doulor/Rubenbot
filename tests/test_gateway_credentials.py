"""凭据接口只向获准调用方返回内容，不把内容带进审计和日志。"""

import asyncio
import logging
from unittest.mock import Mock

import pytest

from rubenbot.adapters.base import ActionNotSupported, BaseAdapter
from rubenbot.adapters.platform import PlatformEvent
from rubenbot.core import RubenBot
from rubenbot.gateway.events import EventBatch
from rubenbot.gateway.judge import EventVerdict
from rubenbot.gateway.service import GatewayService
from rubenbot.persona import Persona

from conftest import ScriptedLLM


PARAM = "PLACEHOLDER-PARAM"
VALUE = "PLACEHOLDER-RESULT"
ACTIONS = ("get_cookies", "get_csrf_token")


@pytest.fixture
def service(store, config, caplog):
    config.gateway.enabled = True
    config.gateway.allow_sensitive = True
    caplog.set_level(logging.DEBUG)
    return GatewayService(store, config)


@pytest.fixture
def adapter():
    stub = Mock(spec=BaseAdapter)
    stub.name = "snowluma"
    stub.supports_actions.return_value = True
    return stub


def assert_no_details(rows, caplog):
    for marker in (PARAM, VALUE):
        assert marker not in repr(rows)
        assert marker not in caplog.text
        assert marker not in repr([vars(record) for record in caplog.records])


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("outcome", ["success", "failure", "unsupported", "disabled", "invalid"])
def test_credential_audit(service, adapter, caplog, action, outcome):
    params = {"domain": PARAM}
    adapter.call_action.return_value = {"data": [VALUE]}
    if outcome == "failure":
        adapter.call_action.side_effect = RuntimeError(f"{VALUE} {PARAM}")
    elif outcome == "unsupported":
        adapter.call_action.side_effect = ActionNotSupported(f"{VALUE} {PARAM}")
    elif outcome == "disabled":
        service.config.gateway.allow_sensitive = False
    elif outcome == "invalid":
        # JSON 校验的异常消息会引用不可序列化值的类型名。
        params["extra"] = type(PARAM, (), {})()

    reply = asyncio.run(service.call(adapter, action, params, chat_id="owner", chat_type="private"))
    if outcome == "success":
        assert reply == f'{action} 调用成功：{{"data": ["{VALUE}"]}}'
    else:
        assert "成功" not in reply
    if outcome in ("disabled", "invalid"):
        adapter.call_action.assert_not_awaited()
    else:
        adapter.call_action.assert_awaited_once_with(action, params)
    row, = service.store.list_gateway_audit()
    assert (row["action"], row["ok"], row["sensitive"]) == (action, int(outcome == "success"), 1)
    assert (row["source"], row["chat_id"]) == ("model", "owner")
    assert_no_details([row], caplog)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("outcome", ["success", "failure", "invalid"])
def test_credential_event_log(service, adapter, caplog, action, outcome):
    bot = RubenBot(service.config, service.store, ScriptedLLM([]), Persona("测试", "测试"))
    bot.register_adapter(adapter)
    event = PlatformEvent("snowluma", "notice", "test", conversation_id="owner")
    batch = EventBatch(("snowluma", "owner", "test"), [event])
    params = {"domain": PARAM}
    if outcome == "invalid":
        params["extra"] = type(PARAM, (), {})()
    elif outcome == "failure":
        adapter.call_action.side_effect = RuntimeError(VALUE)
    adapter.call_action.return_value = VALUE
    verdict = EventVerdict("gateway", {"action": action, "params": params})
    asyncio.run(bot.apply_event_verdict(batch, verdict))
    row, = service.store.list_gateway_audit()
    assert (row["source"], row["ok"]) == ("event", int(outcome == "success"))
    assert "事件网关调用" in caplog.text
    assert_no_details([row], caplog)


def test_credential_audit_write_error(service, adapter, caplog, monkeypatch):
    monkeypatch.setattr(service.store, "add_gateway_audit", Mock(side_effect=RuntimeError(VALUE)))
    adapter.call_action.return_value = VALUE
    reply = asyncio.run(service.call(adapter, "get_cookies", {"domain": PARAM}, chat_type="private"))
    assert VALUE in reply and "写网关审计失败" in caplog.text
    assert_no_details([], caplog)
