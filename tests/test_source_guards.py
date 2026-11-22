"""真实消息入口回放：静音不摄入，疲劳只算已送达轮次，恢复不强迫开口。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.decision import DecisionEngine
from rubenbot.persona import Persona
from rubenbot.web.config_schema import ConfigValidationError, structured_config, validate_structured


def alert(i, sender="source", room="g", **opts):
    return MessageEvent("recording", "group", room, sender, sender,
                        f"中转站返回延迟 {i}.25 ms（采样时间 09:{i:02d}:48）", **opts)


def bot_setup(config, store):
    config.decision.min_reply_interval_seconds = 0
    config.decision.refractory_enabled = config.decision.proactive_window_enabled = False
    plan = '{"basis":"common","speak":true,"intent":"短附和","reason":"测试跟评"}'
    llm = ScriptedLLM({"fast": [plan] * 10, "chat": ["还行"] * 10})
    bot = RubenBot(config, store, llm, Persona("Ruben", "群友"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, llm


def test_seed_mute_precedes_all_state(config, store):
    bot, adapter, llm = bot_setup(config, store)
    bot.plugins = Mock()
    bot.prepare_voice = AsyncMock()
    bot.observe_emotion = Mock()
    for i in range(5):
        asyncio.run(bot.handle_event(alert(i, "4012701880", mentions_bot=True)))
    image = alert(5, "4012701880")
    image.chat_type, image.text, image.segments = "private", "[图片]", [Segment.image(url="unused")]
    asyncio.run(bot.handle_event(image))
    assert not adapter.sent and not llm.calls and not bot.merger.pending
    assert not bot._engaged_at and not bot._last_activity and not store.recent_messages("g")
    bot.prepare_voice.assert_not_called()
    bot.observe_emotion.assert_not_called()
    assert not bot.plugins.mock_calls
    logs = store.list_decision_logs()
    assert [row['reason'] for row in logs] == ['muted_sender'] * 6
    assert any('视觉描述缺失' in row['text'] for row in logs)
    # 解除名单后，从未有过跟评，不应凭空触发疲劳。
    config.decision.muted_senders.clear()
    assert bot.decision_engine.source_guard(alert(6, '4012701880'), 2000) is None


def test_third_template_brakes_then_releases_without_extension(config, store, monkeypatch):
    bot, adapter, llm = bot_setup(config, store)
    now = 1000.
    monkeypatch.setattr('rubenbot.core.time.time', lambda: now)
    config.decision.fatigue_cooldown_seconds = 12
    for i in range(1, 6):
        now += 1
        asyncio.run(bot.handle_event(alert(i)))
    assert len(adapter.sent) == 2
    assert len([c for c in llm.calls if c['task'] == 'gen_stage1']) == 2
    assert [row['reason'] for row in reversed(store.list_decision_logs())] == [
        'planned', 'planned', 'fatigue_triggered', 'fatigue_cooldown', 'fatigue_cooldown',
    ]
    # 冷却只影响本群这个账号，连不同内容也不续期；到点恢复。
    asyncio.run(bot.handle_event(alert(6, 'someone')))
    asyncio.run(bot.handle_event(alert(6, room='other')))
    assert len(adapter.sent) == 4
    now = 1015.
    asyncio.run(bot.handle_event(alert(7)))
    assert len(adapter.sent) == 5
    assert [row['reason'] for row in store.list_decision_logs(limit=2)] == ['planned', 'fatigue_released']


def test_waiting_round_rechecks_after_previous_send(config, store):
    config.decision.fatigue_reply_limit = 1
    bot, adapter, _ = bot_setup(config, store)

    async def run():
        started, resume = asyncio.Event(), asyncio.Event()
        send = adapter.send

        async def blocked(*args, **kwargs):
            started.set()
            await resume.wait()
            return await send(*args, **kwargs)

        adapter.send = blocked
        first = asyncio.create_task(bot.handle_event(alert(1)))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(bot.handle_event(alert(2)))
        await asyncio.sleep(0)
        assert not second.done()
        resume.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert len(adapter.sent) == 1
    assert store.list_decision_logs(limit=1)[0]['reason'] == 'fatigue_triggered'


def test_no_fatigue_for_failed_sends(config, store):
    bot, adapter, llm = bot_setup(config, store)
    adapter.send = AsyncMock(side_effect=RuntimeError('模拟失败'))
    for i in range(4):
        asyncio.run(bot.handle_event(alert(i)))
    assert adapter.send.await_count == 4
    assert len([c for c in llm.calls if c['task'] == 'gen_stage1']) == 4
    assert not any('fatigue' in row['reason'] for row in store.list_decision_logs())


def test_only_count_relevant_delivered_rounds_in_window(config):
    cfg = config.decision
    cfg.fatigue_window_seconds = 20
    engine = DecisionEngine(cfg)
    one, two, three = [alert(i) for i in range(3)]
    other = alert(1, 'other')
    engine.note_source_reply(one, [one, two, three, other], 100)
    assert engine.source_guard(three, 101) is None  # 同一批的三条只算一次
    engine.note_source_reply(two, [two], 120)
    assert engine.source_guard(three, 120) is None  # 第一轮已出窗口
    unrelated = alert(0)
    unrelated.text = '我们今天终于把服务器修好了'
    engine.note_source_reply(unrelated, [unrelated], 121)
    assert engine.source_guard(three, 122) is None
    engine.note_source_reply(two, [two], 122)
    assert engine.source_guard(other, 123) is None
    assert engine.source_guard(three, 123) == 'fatigue_triggered'
    assert engine.source_guard(unrelated, 124) == 'fatigue_cooldown'


def test_source_guard_config_roundtrip_and_validation():
    values = {'muted_senders': [], 'fatigue_window_seconds': 100.0,
              'fatigue_reply_limit': 3, 'fatigue_cooldown_seconds': 600.0}
    cfg = Config.from_dict(validate_structured({'decision': values}))
    out = structured_config(cfg)['decision']
    assert all(out[key] == value for key, value in values.items())
    for key, bad_values in {
        'muted_senders': [{}, '123', [None], None, [True], [1.5], ['   ']],
        'fatigue_reply_limit': [1.5, -1, float('inf'), False],
        'fatigue_window_seconds': [-1, float('nan')],
        'fatigue_cooldown_seconds': [-1, True],
    }.items():
        for bad in bad_values:
            payload = {'decision': {key: bad}}
            with pytest.raises(ConfigValidationError):
                validate_structured(payload)
            assert getattr(Config.from_dict(payload).decision, key) == getattr(Config().decision, key)
    assert Config.from_dict({'decision': {'muted_senders': [123]}}).decision.muted_senders == ['123']
