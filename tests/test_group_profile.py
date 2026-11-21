"""分群提示词（Q13）：手写群备注 + 后台自归纳群定位。

覆盖：
- 注入三档优先级：手写优先 / 只有自归纳 / 两份都空；私聊不注入。
- 存储：新群两份都空、手写读写清空、自归纳写水位、删群清理。
- 后台任务：按新消息数或每日间隔触发、限长截断、失败保留旧总结、
  开关关闭不发请求、不新增串行调用。
- WebUI：群详情并排接口 + 手写备注编辑 API + 设置页开关。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.group_profile import (
    GROUP_PROFILE_TITLE,
    GroupProfileSummarizer,
    active_profile_block,
    format_group_profile_block,
    resolve_profile_text,
)
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ----------------------------------------------------------------------
# 夹具与工具
# ----------------------------------------------------------------------


def make_config(**overrides) -> Config:
    cfg = Config()
    cfg.reply.segment_delay_enabled = False
    cfg.decision.merge_window_seconds = 0
    cfg.decision.hard_merge_window_seconds = 0
    cfg.planner.planner_gate = False
    cfg.memory.group_profile.min_messages = overrides.pop("min_messages", 3)
    cfg.memory.group_profile.update_interval_hours = overrides.pop("interval_hours", 24.0)
    cfg.memory.group_profile.max_chars = overrides.pop("max_chars", 300)
    for key, value in overrides.items():
        setattr(cfg.memory.group_profile, key, value)
    return cfg


def make_bot(config: Config, store) -> tuple[RubenBot, RecordingAdapter, ScriptedLLM]:
    llm = ScriptedLLM({"summary": ["氛围：安静\n常聊话题：工作与晚饭"]})
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter, llm


def add_messages(store, count, *, chat_id="g1", platform="recording", start=100.0) -> list[int]:
    return [
        store.add_raw_message(
            platform=platform,
            chat_id=chat_id,
            sender_id="u1",
            sender_name="阿花",
            text=f"第{index}条消息",
            ts=start + index,
        )
        for index in range(count)
    ]


def group_event(text="今天好累", *, chat_id="g1", platform="recording") -> MessageEvent:
    return MessageEvent(
        platform=platform,
        chat_type="group",
        chat_id=chat_id,
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


# ----------------------------------------------------------------------
# 1. 注入三档优先级
# ----------------------------------------------------------------------


def test_manual_note_wins_over_auto_summary():
    manual = "工作群，别开玩笑，正经点回答。"
    auto = "氛围：轻松\n常聊话题：游戏"
    profile = type("P", (), {"manual_note": manual, "auto_summary": auto})()
    source, text = resolve_profile_text(profile, max_chars=300)
    assert source == "manual"
    assert text == manual
    block = format_group_profile_block(text, source)
    assert block.startswith(GROUP_PROFILE_TITLE)
    assert "群备注" in block
    assert manual in block
    assert auto not in block


def test_auto_summary_used_when_manual_empty():
    profile = type("P", (), {"manual_note": "  ", "auto_summary": "氛围：热闹"})()
    source, text = resolve_profile_text(profile, max_chars=300)
    assert source == "auto"
    block = format_group_profile_block(text, source)
    assert "平时的观察" in block
    assert "氛围：热闹" in block


def test_no_profile_no_injection(store):
    assert resolve_profile_text(None, max_chars=300) == ("", "")
    assert active_profile_block(store, "recording", "g1", max_chars=300) == ""


def test_profile_injection_is_truncated():
    long_text = "很长的群定位。" * 100
    profile = type("P", (), {"manual_note": "", "auto_summary": long_text})()
    source, text = resolve_profile_text(profile, max_chars=40)
    assert source == "auto"
    assert len(text) <= 40
    assert text != long_text


def test_core_injects_manual_note_into_prompt(config, store):
    config.memory.group_profile.enabled = False  # 注入与自归纳开关无关
    store.set_group_manual_note("recording", "g1", "熟人水群，可以放开聊")
    bot, _, _ = make_bot(config, store)
    prompt = bot.persona_system_prompt(group_event(), stage="single")
    messages = asyncio.run(bot.build_context(group_event()))
    system = messages[0]["content"]
    assert "熟人水群，可以放开聊" in system
    # 人格段本身不被改写
    assert prompt in system


def test_core_uses_auto_when_manual_blank(store, config):
    store.set_group_auto_summary("recording", "g1", "氛围：讲冷笑话", 5, now=1000.0)
    bot, _, _ = make_bot(config, store)
    system = asyncio.run(bot.build_context(group_event()))[0]["content"]
    assert "氛围：讲冷笑话" in system


def test_core_no_group_profile_when_both_empty(store, config):
    bot, _, _ = make_bot(config, store)
    system = asyncio.run(bot.build_context(group_event()))[0]["content"]
    assert GROUP_PROFILE_TITLE not in system


def test_private_chat_never_injects_group_profile(store, config):
    store.set_group_manual_note("recording", "u1", "私聊不该看到这段")
    bot, _, _ = make_bot(config, store)
    event = MessageEvent(
        platform="recording", chat_type="private", chat_id="u1",
        sender_id="u1", sender_name="阿花", text="在吗",
    )
    assert bot.group_profile_text(event) == ""
    system = asyncio.run(bot.build_context(event))[0]["content"]
    assert "私聊不该看到这段" not in system


def test_core_injects_profile_in_planner_stage(store):
    """策划步与生成步共用同一份系统提示，群定位段不被阶段切换丢掉。"""
    config = make_config()
    config.planner.planner_gate = True
    store.set_group_manual_note("recording", "g1", "工作群，别开玩笑")
    bot, _, _ = make_bot(config, store)
    planner = asyncio.run(bot.build_context(group_event(), stage="planner"))[0]["content"]
    generator = asyncio.run(bot.build_context(group_event(), stage="generator"))[0]["content"]
    assert "工作群，别开玩笑" in planner
    assert "工作群，别开玩笑" in generator
    # 群定位在人格前缀之后：阶段切换只换人格开头，不会把这段一起换掉
    assert planner.startswith(bot.persona_system_prompt(group_event(), stage="planner"))
    assert generator.startswith(bot.persona_system_prompt(group_event(), stage="generator"))


def test_reading_injection_does_not_call_llm(store, config):
    """收消息路径只读库：build_context 不花任何模型调用。"""
    store.set_group_manual_note("recording", "g1", "工作群")
    bot, _, llm = make_bot(config, store)
    asyncio.run(bot.build_context(group_event()))
    assert llm.calls == []


# ----------------------------------------------------------------------
# 2. 存储：读写、清空、删群、新群不写脏数据
# ----------------------------------------------------------------------


def test_new_group_has_no_profile_row(store):
    assert store.get_group_profile("recording", "new-group") is None
    assert store.list_group_profiles() == []
    # 读取不会顺手建行
    assert store.get_group_profile("recording", "new-group") is None


def test_manual_note_set_read_clear(store):
    saved = store.set_group_manual_note("recording", "g1", "熟人水群", now=10.0)
    assert saved.manual_note == "熟人水群"
    assert saved.manual_updated_at == 10.0
    got = store.get_group_profile("recording", "g1")
    assert got is not None and got.manual_note == "熟人水群"

    store.set_group_manual_note("recording", "g1", "", now=20.0)
    cleared = store.get_group_profile("recording", "g1")
    assert cleared is not None and cleared.manual_note == ""
    assert cleared.manual_updated_at == 20.0


def test_manual_and_auto_do_not_overwrite_each_other(store):
    store.set_group_manual_note("recording", "g1", "手写备注", now=10.0)
    store.set_group_auto_summary("recording", "g1", "自归纳", 7, now=20.0)
    store.set_group_manual_note("recording", "g1", "改过的手写", now=30.0)
    profile = store.get_group_profile("recording", "g1")
    assert profile.manual_note == "改过的手写"
    assert profile.auto_summary == "自归纳"
    assert profile.covered_until_msg_id == 7
    assert profile.updated_at == 20.0


def test_delete_group_profile_removes_row(store):
    store.set_group_manual_note("recording", "g1", "x")
    assert store.delete_group_profile("recording", "g1") is True
    assert store.get_group_profile("recording", "g1") is None
    assert store.delete_group_profile("recording", "g1") is False


def test_profile_persists_across_reopen(tmp_path):
    path = tmp_path / "rubenbot.db"
    first = MemoryStore(path)
    first.set_group_manual_note("recording", "g1", "落盘内容")
    first.close()
    second = MemoryStore(path)
    try:
        assert second.get_group_profile("recording", "g1").manual_note == "落盘内容"
    finally:
        second.close()


# ----------------------------------------------------------------------
# 3. 后台自归纳任务
# ----------------------------------------------------------------------


def test_below_threshold_does_not_summarize(store):
    config = make_config(min_messages=5)
    add_messages(store, 3)
    summarizer = GroupProfileSummarizer(ScriptedLLM(), store, config.memory.group_profile)
    assert asyncio.run(summarizer.update_chat("recording", "g1", now=100.0)) is None
    assert store.get_group_profile("recording", "g1") is None


def test_enough_new_messages_updates_summary_and_cursor(store):
    config = make_config(min_messages=3)
    ids = add_messages(store, 5)
    llm = ScriptedLLM({"summary": ["氛围：安静\n常聊话题：工作"]})
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)

    result = asyncio.run(summarizer.update_chat("recording", "g1", now=100.0))

    assert result is not None and result.updated
    profile = store.get_group_profile("recording", "g1")
    assert profile.auto_summary == "氛围：安静\n常聊话题：工作"
    assert profile.covered_until_msg_id == ids[-1]
    assert profile.updated_at == 100.0
    # 走 summary 角色、task 打标，进用量统计
    assert llm.calls[0]["role"] == "summary"
    assert llm.calls[0]["task"] == "group_profile"
    # 手写列仍然为空，两份互不污染
    assert profile.manual_note == ""


def test_interval_triggers_with_few_new_messages(store):
    config = make_config(min_messages=50, interval_hours=1.0)
    ids = add_messages(store, 2)
    store.set_group_auto_summary("recording", "g1", "旧总结", ids[-1] - 1, now=100.0)
    add_messages(store, 1, start=200.0)  # 水位之后只有 1 条
    llm = ScriptedLLM({"summary": ["新总结"]})
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)

    # 距上次更新不足 1 小时且新消息不够：跳过
    assert asyncio.run(summarizer.update_chat("recording", "g1", now=200.0)) is None
    # 超过间隔：即便只有 1 条新消息也刷新
    result = asyncio.run(summarizer.update_chat("recording", "g1", now=100.0 + 3601))
    assert result is not None and result.updated
    assert store.get_group_profile("recording", "g1").auto_summary == "新总结"


def test_manual_only_row_does_not_skip_message_threshold(store):
    """只写了手写备注的群，自归纳首次仍要攒够 min_messages。"""
    config = make_config(min_messages=5)
    store.set_group_manual_note("recording", "g1", "手写备注", now=1.0)
    add_messages(store, 2)
    llm = ScriptedLLM()
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)
    assert asyncio.run(summarizer.update_chat("recording", "g1", now=10 * 86400)) is None
    assert llm.calls == []
    profile = store.get_group_profile("recording", "g1")
    assert profile.manual_note == "手写备注"
    assert profile.auto_summary == ""


def test_summary_is_trimmed_to_max_chars(store):
    config = make_config(min_messages=1, max_chars=60)
    add_messages(store, 2)
    long = "氛围很热闹，大家一直在聊。" * 40
    summarizer = GroupProfileSummarizer(ScriptedLLM({"summary": [long]}), store, config.memory.group_profile)
    asyncio.run(summarizer.update_chat("recording", "g1", now=100.0))
    stored = store.get_group_profile("recording", "g1").auto_summary
    assert len(stored) <= config.memory.group_profile.max_chars


def test_empty_model_output_keeps_old_summary(store):
    config = make_config(min_messages=1)
    ids = add_messages(store, 2)
    store.set_group_auto_summary("recording", "g1", "旧总结", ids[0], now=1.0)
    summarizer = GroupProfileSummarizer(ScriptedLLM({"summary": ["   "]}), store, config.memory.group_profile)
    result = asyncio.run(summarizer.update_chat("recording", "g1", now=100.0))
    assert result is not None and not result.updated
    assert store.get_group_profile("recording", "g1").auto_summary == "旧总结"


def test_llm_failure_keeps_old_summary(store):
    config = make_config(min_messages=1)
    add_messages(store, 2)

    class BoomLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("模型挂了")

    summarizer = GroupProfileSummarizer(BoomLLM(), store, config.memory.group_profile)
    result = asyncio.run(summarizer.update_chat("recording", "g1", now=100.0))
    assert result is not None and result.error
    assert store.get_group_profile("recording", "g1") is None


def test_disabled_switch_skips_llm(store):
    config = make_config(min_messages=1, enabled=False)
    add_messages(store, 5)
    llm = ScriptedLLM()
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)
    assert asyncio.run(summarizer.maybe_update(now=100.0)) == []
    assert llm.calls == []


def test_private_chats_are_not_summarized(store):
    config = make_config(min_messages=1)
    for index in range(3):
        store.add_raw_message(
            platform="recording", chat_id="u1", chat_type="private",
            sender_id="u1", sender_name="阿花", text=f"私聊{index}", ts=100.0 + index,
        )
    llm = ScriptedLLM()
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)
    assert asyncio.run(summarizer.maybe_update(now=100.0)) == []
    assert llm.calls == []


def test_maintenance_runs_group_profile_without_extra_chat_calls(store):
    """后台维护顺带更新群定位；收消息路径不因此多一次模型调用。"""
    config = make_config(min_messages=1)
    config.memory.extract_every_n = 10_000  # 让抽取不掺和断言
    add_messages(store, 3)
    bot, _, llm = make_bot(config, store)
    asyncio.run(bot.run_maintenance(now=100.0))
    tasks = [call["task"] for call in llm.calls]
    assert "group_profile" in tasks
    assert store.get_group_profile("recording", "g1").auto_summary


def test_batch_limit_caps_prompt_size(store):
    config = make_config(min_messages=1)
    config.memory.group_profile.max_batch_messages = 3
    add_messages(store, 8)
    llm = ScriptedLLM({"summary": ["总结"]})
    summarizer = GroupProfileSummarizer(llm, store, config.memory.group_profile)
    asyncio.run(summarizer.update_chat("recording", "g1", now=100.0))
    prompt = "\n".join(item["content"] for item in llm.calls[0]["messages"])
    assert "第0条消息" in prompt
    assert "第3条消息" not in prompt
    # 水位只推进到本批最后一条，剩余留到下一轮
    assert store.get_group_profile("recording", "g1").covered_until_msg_id == 3


def test_proactive_opener_includes_group_profile(store, config):
    """冷场开口同样按群说话：只在群里有定位时带上这一段。"""
    store.set_group_manual_note("recording", "g1", "这是熟人水群，可以放开聊")
    bot, _, llm = make_bot(config, store)
    asyncio.run(bot._proactive_open("recording", "g1", now=time.time()))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "熟人水群，可以放开聊" in prompt
    # 群定位只读库：这次主动开口只花一次生成调用
    assert len(llm.calls) == 1


# ----------------------------------------------------------------------
# 4. WebUI：并排接口、手写编辑、清空、设置开关
# ----------------------------------------------------------------------


def make_client(config, store, tmp_path):
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    state = WebState(config=config, store=store, config_path=tmp_path / "config.yaml")
    return TestClient(create_app(state)), state


def test_profile_api_side_by_side(config, store, tmp_path):
    store.add_raw_message(
        platform="recording", chat_id="g1", chat_type="group",
        sender_id="u1", sender_name="阿花", text="hi", ts=100.0,
    )
    store.set_group_auto_summary("recording", "g1", "氛围：安静", 3, now=200.0)
    client, _ = make_client(config, store, tmp_path)

    body = client.get("/api/chats/g1/profile?platform=recording").json()
    assert body["ok"] is True
    assert body["manual_note"] == ""
    assert body["auto_summary"] == "氛围：安静"
    assert body["auto_updated_at"] == 200.0
    assert body["source"] == "auto"
    assert body["auto_enabled"] is True


def test_profile_api_put_read_clear(config, store, tmp_path):
    store.add_raw_message(
        platform="recording", chat_id="g1", chat_type="group",
        sender_id="u1", sender_name="阿花", text="hi", ts=100.0,
    )
    client, _ = make_client(config, store, tmp_path)

    saved = client.put("/api/chats/g1/profile?platform=recording", json={"manual_note": "工作群，正经点"}).json()
    assert saved["manual_note"] == "工作群，正经点"
    assert saved["source"] == "manual"
    assert saved["manual_updated_at"] > 0
    assert client.get("/api/chats/g1/profile?platform=recording").json()["source"] == "manual"

    # 清空：空串立即生效，回落自归纳（这里没有自归纳，所以不注入）
    cleared = client.put("/api/chats/g1/profile?platform=recording", json={"manual_note": ""}).json()
    assert cleared["manual_note"] == ""
    assert cleared["source"] == ""

    # 非法请求被拒
    assert client.put("/api/chats/g1/profile?platform=recording", json={}).status_code == 422
    assert client.put("/api/chats/g1/profile?platform=recording", json={"manual_note": 3}).status_code == 422
    too_long = "字" * 2001
    assert client.put(
        "/api/chats/g1/profile?platform=recording", json={"manual_note": too_long}
    ).status_code == 422


def test_profile_write_requires_platform_when_ambiguous(config, store, tmp_path):
    """同一个群号出现在两个平台时，写操作必须显式给 platform，不能猜。"""
    for platform in ("snowluma", "telegram"):
        store.add_raw_message(
            platform=platform, chat_id="610", chat_type="group",
            sender_id="u1", sender_name="阿花", text="hi", ts=100.0,
        )
    client, _ = make_client(config, store, tmp_path)
    response = client.put("/api/chats/610/profile", json={"manual_note": "x"})
    assert response.status_code == 422
    assert store.get_group_profile("snowluma", "610") is None
    assert store.get_group_profile("telegram", "610") is None
    # 显式指定平台就能写
    ok = client.put("/api/chats/610/profile?platform=telegram", json={"manual_note": "x"})
    assert ok.status_code == 200
    assert store.get_group_profile("telegram", "610").manual_note == "x"


def test_profile_api_delete(config, store, tmp_path):
    store.set_group_manual_note("recording", "g1", "要删掉的")
    client, _ = make_client(config, store, tmp_path)
    assert client.delete("/api/chats/g1/profile?platform=recording").json()["deleted"] is True
    assert store.get_group_profile("recording", "g1") is None


def test_profile_api_missing_platform_and_profile(config, store, tmp_path):
    """没消息、没记录的新群：GET 返回两份空，不建行；写操作无法确定平台时拒绝。"""
    client, _ = make_client(config, store, tmp_path)
    body = client.get("/api/chats/999/profile").json()
    assert body["manual_note"] == "" and body["auto_summary"] == ""
    assert body["source"] == ""
    assert store.get_group_profile("", "999") is None
    assert store.list_group_profiles() == []
    response = client.put("/api/chats/999/profile", json={"manual_note": "x"})
    assert response.status_code == 422


def test_profile_api_clear_flag_alias(config, store, tmp_path):
    """``{"clear": true}`` 与空串等价，供旧前端/脚本使用。"""
    store.set_group_manual_note("recording", "g1", "旧备注")
    client, _ = make_client(config, store, tmp_path)
    body = client.put("/api/chats/g1/profile?platform=recording", json={"clear": True}).json()
    assert body["manual_note"] == ""
    assert store.get_group_profile("recording", "g1").manual_note == ""


def test_profile_api_infers_platform_when_unique(config, store, tmp_path):
    store.add_raw_message(
        platform="snowluma", chat_id="610", chat_type="group",
        sender_id="u1", sender_name="阿花", text="hi", ts=100.0,
    )
    client, _ = make_client(config, store, tmp_path)
    saved = client.put("/api/chats/610/profile", json={"manual_note": "书友群"}).json()
    assert saved["platform"] == "snowluma"
    assert store.get_group_profile("snowluma", "610").manual_note == "书友群"


def test_profile_save_takes_effect_immediately(config, store, tmp_path):
    config.planner.planner_gate = False
    store.add_raw_message(
        platform="recording", chat_id="g1", chat_type="group",
        sender_id="u1", sender_name="阿花", text="hi", ts=100.0,
    )
    bot, _, _ = make_bot(config, store)
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    state = WebState.from_bot(bot, tmp_path / "config.yaml")
    client = TestClient(create_app(state))
    client.put("/api/chats/g1/profile?platform=recording", json={"manual_note": "新写的群备注"})
    # 不重启：下一次拼上下文就能读到
    system = asyncio.run(bot.build_context(group_event()))[0]["content"]
    assert "新写的群备注" in system


def test_settings_switch_visible_in_structured_config():
    from rubenbot.web.config_schema import resolve_page, structured_config

    view = structured_config(Config())
    assert view["memory"]["group_profile"]["enabled"] is True
    assert resolve_page("memory.group_profile.enabled") == "memory"


def test_structured_config_validates_group_profile(tmp_path):
    from rubenbot.web.config_schema import ConfigValidationError, validate_structured

    update = validate_structured(
        {"memory": {"group_profile": {
            "enabled": False, "update_interval_hours": 6,
            "min_messages": 20, "max_chars": 200, "max_batch_messages": 50,
        }}}
    )
    assert update["memory"]["group_profile"] == {
        "enabled": False,
        "update_interval_hours": 6.0,
        "min_messages": 20,
        "max_chars": 200,
        "max_batch_messages": 50,
    }
    with pytest.raises(ConfigValidationError):
        validate_structured({"memory": {"group_profile": {"max_chars": 10}}})
    with pytest.raises(ConfigValidationError):
        validate_structured({"memory": {"group_profile": {"min_messages": 0}}})


def test_config_roundtrip_group_profile():
    cfg = Config.from_dict({
        "memory": {"group_profile": {
            "enabled": False, "update_interval_hours": 12,
            "min_messages": 5, "max_chars": 120, "max_batch_messages": 20,
        }}
    })
    assert cfg.memory.group_profile.enabled is False
    assert cfg.memory.group_profile.update_interval_hours == 12.0
    assert cfg.memory.group_profile.min_messages == 5
    assert cfg.memory.group_profile.max_chars == 120
    assert cfg.memory.group_profile.max_batch_messages == 20


def test_reload_config_updates_summarizer(config, store):
    bot, _, _ = make_bot(config, store)
    new_config = Config()
    new_config.memory.group_profile.min_messages = 7
    bot.apply_config(new_config, rebuild_llm=False)
    assert bot.group_profiles.config.min_messages == 7
    assert bot.group_profiles.config is new_config.memory.group_profile


def test_untouched_group_has_no_rows_after_maintenance(store):
    """没有消息的新群不会因为后台扫描被写出脏数据。"""
    config = make_config(min_messages=1)
    bot, _, _ = make_bot(config, store)
    asyncio.run(bot.run_maintenance(now=time.time()))
    assert store.list_group_profiles() == []
