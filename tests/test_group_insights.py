"""group_insights 插件：触发识别、发言聚合、总结取数、冷却与缓存。"""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import BOT_SENDER_ID, MemoryStore, RawMessage
from rubenbot.persona import Persona
from rubenbot.plugins.loader import PluginManager

from conftest import RecordingAdapter, ScriptedLLM

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "group_insights"
MODULE_NAME = "rubenbot_plugin_group_insights_test"


def load_plugin_module():
    """按 loader 的方式从文件加载 plugin.py，拿到独立的模块对象。"""
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin_module()


# ----------------------------------------------------------------------
# 构造数据
# ----------------------------------------------------------------------


def raw(sender_id="u1", name="阿花", text="你好", *, ts=None, chat_id="g1") -> RawMessage:
    return RawMessage(
        id=0,
        platform="recording",
        chat_id=chat_id,
        chat_type="group",
        sender_id=sender_id,
        sender_name=name,
        text=text,
        ts=time.time() if ts is None else ts,
    )


def group_event(
    text="水群榜", *, sender_id="u1", name="阿花", chat_id="g1", mentions_bot=False, raw_data=None
) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=name,
        text=text,
        mentions_bot=mentions_bot,
        raw=raw_data or {},
    )


def private_event(text="水群榜") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def make_plugin_dir(tmp_path: Path) -> Path:
    """把真实插件拷到 tmp，缓存写进 tmp 下的 data 目录。"""
    root = tmp_path / "plugins"
    target = root / "group_insights"
    target.mkdir(parents=True)
    for name in ("plugin.py", "manifest.json"):
        (target / name).write_text(
            (PLUGIN_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return root


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_insights(tmp_path, store=None, *, clock=None, llm=None, config=None, cache_name="s.json"):
    """直接构造 GroupInsights（绕过 loader），方便控制配置和时间。"""
    ctx = SimpleNamespace(
        store=store or MemoryStore(":memory:"),
        llm=llm,
        config=Config(),
        extras={},
    )
    insights = plugin.GroupInsights(
        ctx,
        config,
        clock=clock or time.time,
        cache_path=tmp_path / cache_name,
    )
    return insights


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------


def test_config_defaults_and_overrides():
    default = plugin.InsightsConfig.from_mapping(None)
    assert default.stats_window_days == 1
    assert default.summary_max_messages == 200
    assert default.summary_cooldown_minutes == 10
    assert "水群榜" in default.stats_triggers
    assert "总结一下" in default.summary_triggers

    custom = plugin.InsightsConfig.from_mapping(
        {
            "stats_window_days": 7,
            "stats_triggers": ["排行榜"],
            "summary_triggers": [],
            "summary_max_messages": 50,
            "summary_cooldown_minutes": 0,
            "require_addressing": False,
        }
    )
    assert custom.stats_window_days == 7
    assert custom.stats_triggers == ("排行榜",)
    assert custom.summary_triggers == ()  # 显式空列表 = 关闭总结
    assert custom.summary_max_messages == 50
    assert custom.summary_cooldown_minutes == 0
    assert custom.require_addressing is False


def test_config_rejects_bad_values():
    config = plugin.InsightsConfig.from_mapping(
        {
            "stats_window_days": 0,
            "summary_max_messages": -3,
            "summary_cooldown_minutes": "很久",
        }
    )
    assert config.stats_window_days == 1
    assert config.summary_max_messages == 200
    assert config.summary_cooldown_minutes == 10


def test_load_config_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("summary_max_messages: 66\nstats_window_days: 7\n", encoding="utf-8")
    config = plugin.load_config(path)
    assert config.summary_max_messages == 66
    assert config.stats_window_days == 7
    # 目录形式：优先 config.yaml，没有则 config.example.yaml，都没有用默认值
    directory = tmp_path / "d"
    directory.mkdir()
    (directory / "config.example.yaml").write_text("summary_max_messages: 88\n", encoding="utf-8")
    assert plugin.load_config(directory).summary_max_messages == 88
    assert plugin.load_config(tmp_path / "empty").summary_max_messages == 200


def test_shipped_example_config_parses():
    """仓库自带的 config.example.yaml 必须是合法配置，且与默认值一致。"""
    config = plugin.load_config(PLUGIN_DIR / "config.example.yaml")
    default = plugin.InsightsConfig()
    assert config.stats_window_days == default.stats_window_days
    assert config.summary_max_messages == default.summary_max_messages
    assert config.summary_cooldown_minutes == default.summary_cooldown_minutes
    assert "水群榜" in config.stats_triggers
    assert "补课" in config.summary_triggers


# ----------------------------------------------------------------------
# 触发识别
# ----------------------------------------------------------------------


def test_detect_intent_stats_and_summary():
    config = plugin.InsightsConfig()
    assert plugin.detect_intent("今天谁话最多？", config) == ("stats", 1)
    assert plugin.detect_intent("来个水群榜", config) == ("stats", 1)
    assert plugin.detect_intent("总结一下", config) == ("summary", 1)
    assert plugin.detect_intent("今天都聊了啥", config) == ("summary", 1)
    assert plugin.detect_intent("我补课", config) == ("summary", 1)
    assert plugin.detect_intent("今天天气不错", config) is None
    assert plugin.detect_intent("", config) is None


def test_detect_intent_window_hints():
    config = plugin.InsightsConfig()
    assert plugin.detect_intent("这周的发言统计", config) == ("stats", 7)
    assert plugin.detect_intent("近7天谁话最多", config) == ("stats", 7)
    assert plugin.detect_intent("今天谁话最多", config) == ("stats", 1)
    # 区分不出来时用配置的默认窗口
    assert plugin.detect_intent("发言统计", plugin.InsightsConfig.from_mapping({"stats_window_days": 3})) == (
        "stats",
        3,
    )


def test_detect_intent_stats_wins_over_summary():
    config = plugin.InsightsConfig()
    assert plugin.detect_intent("总结一下今天的发言统计", config)[0] == "stats"


def test_detect_intent_ignores_disabled_feature():
    config = plugin.InsightsConfig.from_mapping({"summary_triggers": []})
    assert plugin.detect_intent("总结一下", config) is None
    assert plugin.detect_intent("水群榜", config) == ("stats", 1)


# ----------------------------------------------------------------------
# 发言聚合
# ----------------------------------------------------------------------


def test_aggregate_counts_and_shares():
    messages = [
        raw("u1", "阿花"),
        raw("u2", "小北"),
        raw("u1", "阿花"),
        raw("u1", "阿花"),
        raw("u3", "老王"),
    ]
    result = plugin.aggregate_speakers(messages)
    assert result.total == 5
    top = result.top
    assert [(s.name, s.count) for s in top] == [("阿花", 3), ("小北", 1), ("老王", 1)]
    assert top[0].share == pytest.approx(3 / 5)
    # 同票按首次出现顺序
    assert top[1].name == "小北"


def test_aggregate_excludes_bot_and_system():
    messages = [
        raw("u1", "阿花"),
        raw(BOT_SENDER_ID, "Ruben"),
        raw("__system__", "系统", text="[图片]"),
        raw("u2", "小北"),
    ]
    result = plugin.aggregate_speakers(messages)
    assert result.total == 2
    assert {s.name for s in result.speakers} == {"阿花", "小北"}


def test_aggregate_uses_latest_name():
    messages = [raw("u1", "阿花"), raw("u1", "阿花花")]
    result = plugin.aggregate_speakers(messages)
    assert result.top[0].name == "阿花花"


def test_aggregate_empty():
    result = plugin.aggregate_speakers([])
    assert result.total == 0
    assert result.top == []
    assert "还没人说话" in plugin.format_stats(result, window_days=1)


def test_format_stats_top5_and_comment():
    messages = []
    # 6 个人，条数依次递减；第 6 名不出现在 Top5
    for index in range(6):
        for _ in range(6 - index):
            messages.append(raw(f"u{index}", f"人{index}"))
    result = plugin.aggregate_speakers(messages)
    text = plugin.format_stats(result, window_days=1)
    assert text.startswith("📊 今日发言榜")
    assert "共 21 条 / 6 人" in text  # 人数按全部发言者算，不受 Top5 截断影响
    assert "人0 — 6 条" in text
    assert "人4 — 2 条" in text
    assert "人5" not in text.split("点评")[0]
    lines = text.splitlines()
    assert lines[-1].startswith("点评：")
    assert len(lines[-1]) <= 40


def test_comment_line_variants():
    close = plugin.StatsResult(
        total=10,
        speakers=[
            plugin.SpeakerStat("u1", "阿花", 5, 0.5),
            plugin.SpeakerStat("u2", "小北", 5, 0.5),
        ],
    )
    assert "半壁江山" in plugin.comment_line(close)
    tie = plugin.StatsResult(
        total=20,
        speakers=[
            plugin.SpeakerStat("u1", "阿花", 5, 0.25),
            plugin.SpeakerStat("u2", "小北", 4, 0.20),
        ],
    )
    assert "咬得很紧" in plugin.comment_line(tie)
    solo = plugin.StatsResult(
        total=20,
        speakers=[
            plugin.SpeakerStat("u1", "阿花", 4, 0.20),
            plugin.SpeakerStat("u2", "小北", 2, 0.10),
        ],
    )
    assert "话最多" in plugin.comment_line(solo)


# ----------------------------------------------------------------------
# 总结取数与拼装
# ----------------------------------------------------------------------


def test_select_summary_messages_takes_recent_n():
    messages = [raw(f"u{i}", f"人{i}", text=f"消息{i}") for i in range(10)]
    selected = plugin.select_summary_messages(messages, max_messages=4)
    assert [m.text for m in selected] == ["消息6", "消息7", "消息8", "消息9"]


def test_select_summary_messages_keeps_bot_and_drops_system():
    messages = [
        raw("u1", "阿花"),
        raw(BOT_SENDER_ID, "Ruben", text="我来了"),
        raw("__system__", "系统"),
        raw("u2", "小北"),
    ]
    selected = plugin.select_summary_messages(messages, max_messages=10)
    assert [m.sender_id for m in selected] == ["u1", BOT_SENDER_ID, "u2"]


def test_format_material_renders_time_name_and_placeholders():
    clock = time.mktime((2026, 11, 20, 9, 30, 0, 0, 0, -1))
    messages = [
        raw("u1", "阿花", text="早上好", ts=clock),
        raw("u2", "小北", text="[图片]", ts=clock + 60),
        raw(BOT_SENDER_ID, "Ruben", text="我也来了", ts=clock + 120),
    ]
    material = plugin.format_material(messages)
    assert "[09:30] 阿花：早上好" in material
    assert "[09:31] 小北：[图片]" in material
    assert "[09:32] 我：我也来了" in material


def test_format_material_truncates_long_messages():
    long_text = "长" * (plugin.MAX_MESSAGE_CHARS + 50)
    material = plugin.format_material([raw("u1", "阿花", text=long_text)])
    assert material.endswith("…")
    assert len(material) < plugin.MAX_MESSAGE_CHARS + 40


def test_build_summary_prompt_structure():
    messages = [raw("u1", "阿花", text="今天聊了啥")]
    messages_prompt = plugin.build_summary_prompt(messages)
    assert messages_prompt[0]["role"] == "system"
    assert "话题" in messages_prompt[0]["content"]
    assert messages_prompt[1]["role"] == "user"
    assert "阿花：今天聊了啥" in messages_prompt[1]["content"]


def test_format_summary_reply_header():
    start = time.mktime((2026, 11, 20, 9, 0, 0, 0, 0, -1))
    messages = [raw("u1", "阿花", ts=start), raw("u2", "小北", ts=start + 3600)]
    text = plugin.format_summary_reply(messages, "1. 大家在聊天气")
    assert text.startswith("🧾 总结了群里最近 2 条消息（09:00–10:00）：")
    assert text.endswith("1. 大家在聊天气")
    assert plugin.format_summary_reply(messages, "  ") == ""


# ----------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------


def test_cache_roundtrip_and_day_expiry(tmp_path):
    path = tmp_path / "cache.json"
    day1 = time.mktime((2026, 11, 20, 9, 0, 0, 0, 0, -1))
    cache = plugin.SummaryCache(path)
    cache.put("recording:g1", "上次的总结", now=day1)
    assert json.loads(path.read_text(encoding="utf-8"))["recording:g1"]["text"] == "上次的总结"
    # 同一天内可复用（新实例 = 重启后）
    fresh = plugin.SummaryCache(path)
    assert fresh.get("recording:g1", now=day1 + 600)["text"] == "上次的总结"
    assert fresh.last_ts()["recording:g1"] == day1
    # 跨天失效
    assert fresh.get("recording:g1", now=day1 + 2 * 86400) is None


def test_cache_survives_corrupt_file(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{不是 json", encoding="utf-8")
    cache = plugin.SummaryCache(path)
    assert cache.get("k", now=time.time()) is None
    cache.put("k", "新内容", now=time.time())
    assert json.loads(path.read_text(encoding="utf-8"))["k"]["text"] == "新内容"


# ----------------------------------------------------------------------
# 插件钩子：统计 / 总结 / 冷却 / 私聊
# ----------------------------------------------------------------------


def make_store(now: float | None = None) -> MemoryStore:
    store = MemoryStore(":memory:")
    now = time.time() if now is None else now
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="u1", text="今天吃什么", sender_name="阿花", ts=now - 300
    )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="u1", text="吃面吧", sender_name="阿花", ts=now - 290
    )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="u2", text="[图片]", sender_name="小北", ts=now - 280
    )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id="u2", text="我也吃面", sender_name="小北", ts=now - 270
    )
    store.add_raw_message(
        platform="recording", chat_id="g1", sender_id=BOT_SENDER_ID, text="加我一个", sender_name="Ruben", ts=now - 260
    )
    return store


def run(coro):
    return asyncio.run(coro)


def test_stats_reply_aggregates_today(tmp_path):
    # 用固定正午时钟：消息在 now-300，若用真实时间、测试恰在 00:00–00:05 跑，
    # 消息会落到昨天午夜前，今日窗口为空（午夜边界抖动）。
    local = time.localtime()
    noon = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 12, 0, 0, 0, 0, -1))
    store = make_store(now=noon)
    store.add_raw_message(platform='recording', chat_type='private', chat_id='g1',
                          sender_id='u3', sender_name='私聊访客', text='不计入群发言榜')
    insights = make_insights(tmp_path, store=store, clock=lambda: noon)
    text = insights.stats_reply(group_event(), window_days=1)
    assert "今日发言榜" in text
    assert "共 4 条" in text  # bot 自己的不计
    assert "阿花 — 2 条（50.0%）" in text
    assert "小北 — 2 条（50.0%）" in text
    assert "Ruben" not in text


def test_window_start_spans_calendar_days():
    now = time.mktime((2026, 11, 20, 15, 0, 0, 0, 0, -1))
    assert plugin.window_start(now, 1) == time.mktime((2026, 11, 20, 0, 0, 0, 0, 0, -1))
    # 含今天在内往前数 7 天 = 14 号零点
    assert plugin.window_start(now, 7) == time.mktime((2026, 11, 14, 0, 0, 0, 0, 0, -1))


def test_stats_window_days_covers_older_messages(tmp_path):
    store = MemoryStore(":memory:")
    now = time.time()
    # 3 天前的消息不算「今日」，但落在「近 7 天」窗口里
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="u3",
        text="老消息",
        sender_name="老王",
        ts=now - 3 * 86400,
    )
    insights = make_insights(tmp_path, store=store)
    assert "老王" not in insights.stats_reply(group_event(), window_days=1)
    assert "老王" in insights.stats_reply(group_event(), window_days=7)


def test_hook_ignores_private_and_unaddressed(tmp_path):
    store = make_store()
    llm = ScriptedLLM({"summary": ["不该被调用"]})
    insights = make_insights(tmp_path, store=store, llm=llm)
    sent: list[str] = []

    async def record(runtime, event, text):
        sent.append(text)
        return True

    insights._send = record  # type: ignore[assignment]

    assert run(insights.on_message(None, private_event())) is None
    assert sent == []
    # 群里没人叫 bot，随口说「总结一下」不接话
    assert run(insights.on_message(None, group_event("总结一下"))) is None
    assert sent == []
    assert llm.calls == []
    # 点名（@bot）才处理，并拦截消息不让普通回复再答一遍
    assert run(insights.on_message(None, group_event("水群榜", mentions_bot=True))) is False
    assert sent and "今日发言榜" in sent[0]


def test_hook_accepts_bot_name_mention(tmp_path):
    insights = make_insights(tmp_path, store=make_store())
    sent: list[str] = []

    async def record(runtime, event, text):
        sent.append(text)
        return True

    insights._send = record  # type: ignore[assignment]
    assert run(insights.on_message(None, group_event("Ruben 来个水群榜"))) is False
    assert sent and "发言榜" in sent[0]


def test_hook_can_disable_addressing_requirement(tmp_path):
    config = plugin.InsightsConfig.from_mapping({"require_addressing": False})
    insights = make_insights(tmp_path, store=make_store(), config=config)
    sent: list[str] = []

    async def record(runtime, event, text):
        sent.append(text)
        return True

    insights._send = record  # type: ignore[assignment]
    assert run(insights.on_message(None, group_event("今天谁话最多"))) is False
    assert sent


def test_summary_feeds_today_messages_and_formats_output(tmp_path):
    store = make_store()
    llm = ScriptedLLM({"summary": ["1. 阿花和小北在讨论午饭，结论是吃面。"]})
    insights = make_insights(tmp_path, store=store, llm=llm)
    sent: list[str] = []

    async def record(runtime, event, text):
        sent.append(text)
        return True

    insights._send = record  # type: ignore[assignment]
    assert run(insights.on_message(None, group_event("总结一下", mentions_bot=True))) is False

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["role"] == "summary"
    assert call["task"] == "group_summary"
    material = call["messages"][1]["content"]
    # 喂进去的材料：今日 5 条（含 bot 的），图片按占位计入
    assert "阿花：今天吃什么" in material
    assert "小北：[图片]" in material
    assert "我：加我一个" in material
    assert sent and sent[0].startswith("🧾 总结了群里最近 5 条消息")
    assert "吃面" in sent[0]


def test_summary_respects_max_messages(tmp_path):
    store = MemoryStore(":memory:")
    now = time.time()
    for index in range(30):
        store.add_raw_message(
            platform="recording", chat_id="g1", sender_id="u1", text=f"消息{index}", sender_name="阿花", ts=now - 300 + index
        )
    config = plugin.InsightsConfig.from_mapping({"summary_max_messages": 5})
    llm = ScriptedLLM({"summary": ["1. 闲聊"]})
    insights = make_insights(tmp_path, store=store, llm=llm, config=config)
    insights._send = _recorder([])  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    material = llm.calls[0]["messages"][1]["content"]
    assert "消息29" in material
    assert "消息25" in material
    assert "消息24" not in material


def _recorder(sent: list[str]):
    async def record(runtime, event, text):
        sent.append(text)
        return True

    return record


def test_summary_cooldown_returns_cached_text(tmp_path):
    store = make_store()
    llm = ScriptedLLM({"summary": ["1. 大家在聊午饭"]})
    insights = make_insights(tmp_path, store=store, llm=llm)
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]

    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    first = sent[0]
    assert first.startswith("🧾")

    # 冷却内再来一次：不调模型，回「刚总结过」+ 上次结果
    run(insights.on_message(None, group_event("再总结一下", mentions_bot=True)))
    assert len(llm.calls) == 1
    assert sent[1].startswith("刚总结过")
    assert first in sent[1]


def test_summary_cooldown_zero_repeats(tmp_path):
    store = make_store()
    llm = ScriptedLLM({"summary": ["第一次", "第二次"]})
    config = plugin.InsightsConfig.from_mapping({"summary_cooldown_minutes": 0})
    insights = make_insights(tmp_path, store=store, llm=llm, config=config)
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    run(insights.on_message(None, group_event("再总结一下", mentions_bot=True)))
    assert len(llm.calls) == 2
    assert "第二次" in sent[1]


def test_summary_cooldown_restored_from_cache_after_restart(tmp_path):
    """重启后（新插件实例）当日缓存仍在，冷却期内直接回缓存文本。"""
    store = make_store()
    llm = ScriptedLLM({"summary": ["1. 上次的总结"]})
    first = make_insights(tmp_path, store=store, llm=llm)
    sent: list[str] = []
    first._send = _recorder(sent)  # type: ignore[assignment]
    run(first.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert sent and sent[0].startswith("🧾")

    # 新实例读同一个缓存文件
    llm2 = ScriptedLLM({"summary": ["不该被调用"]})
    second = make_insights(tmp_path, store=store, llm=llm2)
    sent2: list[str] = []
    second._send = _recorder(sent2)  # type: ignore[assignment]
    run(second.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert llm2.calls == []
    assert sent2[0].startswith("刚总结过")
    assert "1. 上次的总结" in sent2[0]


def test_summary_empty_group_reply(tmp_path):
    insights = make_insights(tmp_path, store=MemoryStore(":memory:"), llm=ScriptedLLM())
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert "还没什么消息" in sent[0]


def test_summary_single_message_not_enough(tmp_path):
    store = MemoryStore(":memory:")
    store.add_raw_message(platform="recording", chat_id="g1", sender_id="u1", text="在吗", sender_name="阿花")
    llm = ScriptedLLM()
    insights = make_insights(tmp_path, store=store, llm=llm)
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert "还不够总结" in sent[0]
    assert llm.calls == []


def test_summary_llm_failure_degrades(tmp_path):
    class Boom:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("上游挂了")

    insights = make_insights(tmp_path, store=make_store(), llm=Boom())
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert "模型没回上来" in sent[0]
    # 失败不写冷却，下一次可以再试
    assert insights.cooldown_remaining("recording:g1") == 0


def test_summary_without_llm(tmp_path):
    insights = make_insights(tmp_path, store=make_store(), llm=None)
    sent: list[str] = []
    insights._send = _recorder(sent)  # type: ignore[assignment]
    run(insights.on_message(None, group_event("总结一下", mentions_bot=True)))
    assert "模型还没配好" in sent[0]


# ----------------------------------------------------------------------
# 与宿主集成：manifest 合规、钩子拦截、默认不启用
# ----------------------------------------------------------------------


def make_bot(tmp_path: Path, *, llm_replies=None, enabled=True):
    root = make_plugin_dir(tmp_path)
    store = make_store()
    config = Config()
    config.plugins.dir = str(root)
    config.plugins.enabled = ["group_insights"] if enabled else []
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, store)
    manager.extras["group_insights_config"] = {"require_addressing": True}
    manager.load_all()
    llm = ScriptedLLM(llm_replies or {"chat": ["嗯嗯"], "summary": ["1. 聊了午饭"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    manager.extras["bot"] = bot
    return bot, adapter, manager, llm


def test_manifest_is_valid_and_permissions_right():
    from rubenbot.plugins.manifest import load_manifest

    manifest = load_manifest(PLUGIN_DIR)
    assert manifest.id == "group_insights"
    assert set(manifest.permissions) == {"read_messages", "send_messages", "llm"}
    assert manifest.hooks == ("on_message",)
    keys = {item["key"] for item in manifest.config_schema}
    assert {"stats_window_days", "summary_triggers", "summary_max_messages"} <= keys


def test_bot_flow_stats_stops_normal_reply(tmp_path):
    bot, adapter, manager, llm = make_bot(tmp_path)
    event = group_event("水群榜", mentions_bot=True)
    run(bot.handle_event(event))
    assert len(adapter.texts) == 1
    assert "今日发言榜" in adapter.texts[0]
    # 插件拦截了消息：没有进入普通生成
    assert llm.calls == []


def test_bot_flow_plain_chat_untouched(tmp_path):
    bot, adapter, manager, llm = make_bot(tmp_path)
    run(bot.handle_event(group_event("今天天气不错", mentions_bot=True)))
    assert adapter.texts == ["嗯嗯"]  # 普通回复照旧


def test_plugin_not_enabled_by_default(tmp_path):
    bot, adapter, manager, llm = make_bot(tmp_path, enabled=False)
    assert manager.enabled_plugins == []
    run(bot.handle_event(group_event("水群榜", mentions_bot=True)))
    assert all("发言榜" not in text for text in adapter.texts)
