"""回复决策：合并窗口、硬触发、节奏护栏、指向折扣、改写复述核验、生成否决。

所有时间都显式传入，随机源可注入，因此结果完全确定。
"""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from conftest import ScriptedLLM
from test_core import make_bot

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import DecisionConfig, ProactiveConfig
from rubenbot.decision import (
    AddressingFactor,
    DecisionEngine,
    MessageMerger,
    in_quiet_hours,
    is_reply_to_bot,
    paraphrase_verdict,
    parse_generation_output,
    parse_quiet_hours,
    peer_ground,
)


class FixedRandom(random.Random):
    """uniform 结果固定，且记录调用次数。"""

    def __init__(self, value: float = 0.0):
        super().__init__()
        self.value = value
        self.uniform_calls = 0

    def uniform(self, a, b):
        self.uniform_calls += 1
        return self.value


def group_event(
    text="今天好累",
    *,
    sender="u1",
    chat_id="g1",
    mentions=False,
    reply_to=None,
    raw=None,
    mention_map=None,
    reply_sender=None,
) -> MessageEvent:
    return MessageEvent(
        platform="test",
        chat_type="group",
        chat_id=chat_id,
        sender_id=sender,
        sender_name=f"名字{sender}",
        text=text,
        mentions_bot=mentions,
        reply_to_id=reply_to,
        mentions=mention_map or {},
        reply_sender_id=reply_sender or "",
        raw=raw or {},
    )


def private_event(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="test",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def make_engine(**overrides) -> DecisionEngine:
    cfg = DecisionConfig(**overrides) if overrides else DecisionConfig()
    return DecisionEngine(cfg, aliases=["Ruben"], rng=FixedRandom())


# ----------------------------------------------------------------------
# 消息合并窗口
# ----------------------------------------------------------------------


def test_merger_merges_same_sender_within_window():
    merger = MessageMerger(window=4.0, hard_window=1.0)
    assert merger.add(group_event("你好"), 100.0) == []
    assert merger.add(group_event("在吗"), 102.0) == []
    # 第二条把截止时间延到 106；104 时还不该放出来
    assert merger.pop_ready(104.0) == []
    rounds = merger.pop_ready(106.0)
    assert len(rounds) == 1
    assert rounds[0].text == "你好\n在吗"
    assert len(rounds[0].events) == 2
    assert len(merger) == 0


def test_merger_other_sender_extends_conversation_silence():
    merger = MessageMerger()
    merger.add(group_event("我先说", sender="u1"), 100.0)
    assert merger.add(group_event("我也来", sender="u2"), 101.0) == []
    assert merger.pop_ready(103.49) == []
    batch, = merger.pop_ready(103.5)
    assert [e.sender_id for e in batch.events] == ["u1", "u2"]
    assert batch.primary_sender == "u2"


def test_merger_hard_message_has_short_window():
    merger = MessageMerger(window=4.0, hard_window=1.0)
    assert merger.add(group_event("@Ruben", mentions=True), 100.0, hard=True) == []
    # 普通窗口要 4 秒，硬触发 1 秒就到
    assert merger.pop_ready(101.0)[0].hard is True
    assert len(merger) == 0


def test_merger_zero_hard_window_is_immediate():
    merger = MessageMerger(window=4.0, hard_window=0.0)
    ready = merger.add(group_event("@Ruben", mentions=True), 100.0, hard=True)
    assert len(ready) == 1
    assert ready[0].text == "@Ruben"


def test_merger_zero_window_processes_everything_immediately():
    merger = MessageMerger(window=0.0, hard_window=0.0)
    assert len(merger.add(group_event("一"), 10.0)) == 1
    assert len(merger.add(group_event("二"), 10.0)) == 1
    assert len(merger) == 0


def test_merged_event_keeps_mentions_and_reply_target():
    merger = MessageMerger(window=4.0)
    merger.add(group_event("普通"), 100.0)
    merger.add(
        group_event("叫你", mentions=True, reply_to="42", raw={"x": 1}),
        101.0,
    )
    round_ = merger.pop_ready(105.0)[0]
    event = round_.to_event()
    assert event.mentions_bot is True
    assert event.reply_to_id == "42"
    assert event.text == "普通\n叫你"
    assert event.raw["merged_count"] == 2


def test_next_deadline_and_flush_all():
    merger = MessageMerger(window=4.0, hard_window=0.0)
    merger.add(group_event("一条"), 100.0)
    assert merger.next_deadline() == 104.0
    flushed = merger.flush_all()
    assert [r.text for r in flushed] == ["一条"]
    assert merger.next_deadline() is None


def test_merger_batch_cap_defaults_do_not_break_normal_window():
    """默认整批上限 10s 比普通窗口长：正常对话仍按窗口续命。"""
    merger = MessageMerger(window=4.0, hard_window=1.0)
    merger.add(group_event("一"), 100.0)
    merger.add(group_event("二"), 103.0)
    assert merger.next_deadline() == 107.0


# ----------------------------------------------------------------------
# 硬触发
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, expected",
    [
        (private_event(), "private"),
        (group_event(mentions=True), "mentioned"),
        (group_event("回复你", reply_to="7", raw={"bot_message_ids": ["7"]}), "reply_to_bot"),
        (group_event("Ruben 你觉得呢"), "name:Ruben"),
    ],
)
def test_hard_triggers(event, expected):
    engine = make_engine()
    assert engine.is_hard_trigger(event) == expected
    engine.note_bot_reply(engine.chat_key(event), 99.0, proactive=True)
    assert engine.guard(event, now=100.0, proactive=True) is None


def test_name_alias_respects_word_boundary():
    engine = DecisionEngine(DecisionConfig(), aliases=["Ruben"])
    assert engine.is_hard_trigger(group_event("ruben 在吗")) == "name:Ruben"
    # 不能把 rubenbot 之类误判成叫名字
    assert engine.is_hard_trigger(group_event("rubenbot 是啥")) is None


def test_chinese_alias_substring():
    engine = DecisionEngine(DecisionConfig(), aliases=["小R", "鲁本"])
    assert engine.is_hard_trigger(group_event("小R在干嘛")) == "name:小R"
    assert engine.is_hard_trigger(group_event("鲁本帮我看看")) == "name:鲁本"
    assert engine.is_hard_trigger(group_event("这游戏真难")) is None


def test_reply_to_bot_raw_bool():
    event = group_event("回你", reply_to="9", raw={"reply_to_bot": True})
    assert is_reply_to_bot(event)


@pytest.mark.parametrize("opts,elapsed,reason", [
    ({}, 44.99, "refractory"),
    ({}, 45, None),
    ({"refractory_enabled": False, "min_reply_interval_seconds": 10}, 9.99, "min_interval"),
    ({"refractory_seconds": 0, "min_reply_interval_seconds": 10}, 10, None),
    ({"refractory_seconds": 5, "min_reply_interval_seconds": 10}, 4, "refractory"),
    ({"refractory_seconds": 5, "min_reply_interval_seconds": 10}, 6, "min_interval"),
])
def test_guard_timing_and_precedence(opts, elapsed, reason):
    engine = make_engine(**opts)
    engine.note_bot_reply("test:g1", 100)
    guard = engine.guard(group_event(), now=100 + elapsed, proactive=True)
    assert (guard.reason if guard else None) == reason


@pytest.mark.parametrize("enabled,limit,elapsed,blocked", [
    (True, 0, 60, True), (True, 1, 59, True), (True, 1, 60, False),
    (True, 2, 1, False), (False, 0, 1, False),
])
def test_active_window_boundaries(enabled, limit, elapsed, blocked):
    engine = make_engine(refractory_seconds=0, min_reply_interval_seconds=0,
                         proactive_window_enabled=enabled, proactive_window_seconds=60,
                         proactive_window_limit=limit)
    engine.note_bot_reply("test:g1", 100, proactive=True)
    guard = engine.guard(group_event(), now=100 + elapsed, proactive=True)
    assert (guard.reason if guard else None) == ("proactive_window" if blocked else None)
    assert engine.guard(group_event(mentions=True), now=100 + elapsed, proactive=True) is None


@pytest.mark.parametrize("relevance,bonus", [(-1, 0), (0, 0), (4, .3), (8, .6), (100, .6)])
def test_cold_opener_relevance_is_capped(relevance, bonus):
    engine = make_engine()
    config = ProactiveConfig(base=.2, threshold=.5, threshold_noise=0,
                             relevance_scale=8, relevance_bonus=.6)
    result = engine.evaluate_opener(config, relevance=relevance)
    assert result.score == pytest.approx(.2 + bonus)
    assert result.should_reply == (.2 + bonus >= .5)
    assert result.factors.relevance == pytest.approx(bonus)


@pytest.mark.parametrize("base,noise,should,calls", [
    (.5, -.04, False, 1), (.5, .04, True, 1), (.25, .04, False, 0), (.75, -.04, True, 0),
])
def test_cold_opener_only_draws_near_threshold(base, noise, should, calls):
    rng = FixedRandom(noise)
    engine = DecisionEngine(rng=rng)
    result = engine.evaluate_opener(ProactiveConfig(base=base, threshold=.5,
                                    threshold_noise=.05, threshold_noise_band=.1))
    assert result.should_reply is should
    assert rng.uniform_calls == calls
    assert result.reason in result.log_text() and "相关" in result.log_text()


def test_parse_quiet_hours_and_ignore_bad_values(caplog):
    ranges = parse_quiet_hours(["23:00-07:30", "12:00-13:00", "坏值", "25:00-26:00", "10:00-10:00"])
    assert ranges == [(23 * 60, 7 * 60 + 30), (12 * 60, 13 * 60)]
    assert "忽略" in caplog.text


def _local_ts(hour: int, minute: int) -> float:
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))


def test_in_quiet_hours_cross_midnight():
    ranges = parse_quiet_hours(["23:00-07:30"])
    assert in_quiet_hours(_local_ts(23, 30), ranges)
    assert in_quiet_hours(_local_ts(3, 0), ranges)
    assert in_quiet_hours(_local_ts(7, 29), ranges)
    assert not in_quiet_hours(_local_ts(7, 30), ranges)
    assert not in_quiet_hours(_local_ts(12, 0), ranges)


def test_daytime_quiet_range_has_exclusive_end():
    ranges = parse_quiet_hours(["12:30-13:00"])
    assert not in_quiet_hours(_local_ts(12, 29), ranges)
    assert in_quiet_hours(_local_ts(12, 30), ranges)
    assert not in_quiet_hours(_local_ts(13, 0), ranges)


# ----------------------------------------------------------------------
# 生成后否决
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["[不回复]", " [不回复] ", "[不回复]。", "（不回复）", "【[不回复]】", "「[不回复]」"],
)
def test_generation_veto_marker(text):
    result = parse_generation_output(text)
    assert result.vetoed
    assert result.text == ""


@pytest.mark.parametrize("text", ["", "   ", None])
def test_generation_empty_output_is_vetoed(text):
    assert parse_generation_output(text).vetoed


def test_generation_normal_reply_kept():
    result = parse_generation_output("我觉得还行吧")
    assert not result.vetoed
    assert result.text == "我觉得还行吧"


def test_generation_marker_inside_sentence_not_vetoed():
    """句子里顺带提到「不回复」不应被当成标记，避免误杀。"""
    result = parse_generation_output("他刚才说不回复，可能睡着了吧")
    assert not result.vetoed


def test_generation_custom_marker():
    result = parse_generation_output("<silent>", marker="<silent>")
    assert result.vetoed
    result = parse_generation_output("[不回复]", marker="<silent>")
    assert not result.vetoed


# ----------------------------------------------------------------------
# 增量核验：改写复述判定
# ----------------------------------------------------------------------

# 2026-11-23 10:17 主群 1125257581（decision 3135）：宇轩在答多萝，bot 插话
# 把同一条消息换了个说法复述了一遍。问题不在字面重合（附和闸门拦不住改写），
# 而在草稿没有带来任何新事实、新问题或新态度。
_CASE_3135_SOURCE = "没想到是一天一个地区"
_CASE_3135_REWRITES = (
    "一天一个地区，天天有新地方可以开",
    "原来是一天一个地区啊，那每天都能换个地方玩",
    "一天一个地区啊，那天天都有新地方开",
)


@pytest.mark.parametrize("draft", _CASE_3135_REWRITES)
def test_paraphrase_catches_case_3135_rewrites(draft):
    verdict = paraphrase_verdict(_CASE_3135_SOURCE, draft)
    assert verdict.repetitive, verdict.describe()


@pytest.mark.parametrize(("source", "draft"), [
    # 拆断句 + 加口头语，仍然是同一句话。
    ("这个版本，明天才会开放下载", "嗯，确实，这个版本明天才会开放下载啊。"),
    ("也就是说，今晚剧场开门的时间从原定的晚上七点推迟到了晚上八点。", "原定七点开门，现在推迟到八点了"),
    ("剧场明晚开放线上售票，说起来，线下窗口也会同步开售", "剧场明晚开放线上售票，线下窗口也会同步开售"),
    ("我已经把四个角都压紧了，书不会再翘了", "四个角都压紧了，书就不会翘了"),
    ("今天轮到小陈去车站接小李", "今天是小陈去车站接小李啊"),
])
def test_paraphrase_catches_plain_rewrites(source, draft):
    assert paraphrase_verdict(source, draft).repetitive


@pytest.mark.parametrize(("source", "draft"), [
    # 落回具体某一天/某个时间是新事实：「明天」不能当泛词剥掉。
    ("今天食堂的糖醋排骨卖完了", "糖醋排骨卖完了，明天早点去"),
    ("这个版本，明天才会开放下载", "明天才开放下载，今晚先用旧版"),
    # 第一人称打算/邀约是原句没有的新内容。
    ("今晚八点剧场开放线上售票", "八点开放线上售票，我去看看"),
    ("今晚八点剧场开放线上售票", "八点开放线上售票，那我也要看看"),
    # 换个措辞提问，是新的社交信号。
    ("一号线今天停运", "那二号线今天正常吗"),
])
def test_paraphrase_keeps_new_facts_and_intent(source, draft):
    verdict = paraphrase_verdict(source, draft)
    assert not verdict.repetitive, verdict.describe()


@pytest.mark.parametrize(("source", "draft"), [
    # panda 案：知识型提问的直答，草稿与原句几乎没有字面重合。
    ("下载 YouTube 视频用哪个工具？", "用 yt-dlp，后面接视频链接"),
    # 附带新信息：限量、新时间、新数字、否定被去掉。
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票，限量"),
    ("今晚8点剧场开放线上售票", "今晚9点剧场开放线上售票"),
    ("不允许把这里的书和报纸带回家", "允许把这里的书和报纸带回家"),
    # 换问法、加新态度、换主客体，都算有增量。
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票？"),
    ("没想到是一天一个地区", "一天一个地区，这更新速度有点离谱"),
    # 光秃秃的复述配一句评价（「更新挺快」「这也太慢了」）是新态度，不算纯复述。
    ("没想到是一天一个地区", "一天一个地区，更新挺快"),
    ("没想到是一天一个地区", "一天一个地区，这也太慢了"),
    ("没想到是一天一个地区", "你那边现在开放到第几个地区了？"),
    # 原句没提人，草稿突然对「你」说或是自己的打算：新的社交信号。
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票，你去不去"),
    ("今晚八点剧场开放线上售票", "今晚八点剧场开放线上售票，那我提前守着"),
    # 一字之差的新事实：「线上」换「线下」、补一句「量挺大」，都不是复述。
    ("今晚八点剧场开放线上售票", "剧场八点开放线下售票"),
    ("食堂今天有红烧肉", "今天食堂有红烧肉，量挺大"),
    ("今天轮到小陈去车站接小李", "今天轮到小李去车站接小陈"),
    ("我想下班之后去看音乐剧", "我中午就约了摄影师拍照"),
    # 短接话不是「长复述」：交给附和闸门和策划。
    ("今晚八点剧场开放线上售票", "收到，我去设个闹钟"),
    ("今晚八点剧场开放线上售票", "好"),
    # 空输入没有可判定的内容。
    ("", "一天一个地区"),
    ("没想到是一天一个地区", ""),
])
def test_paraphrase_lets_incremental_replies_through(source, draft):
    verdict = paraphrase_verdict(source, draft)
    assert not verdict.repetitive, verdict.describe()


def test_paraphrase_is_pure_and_explains_itself():
    first = paraphrase_verdict(_CASE_3135_SOURCE, _CASE_3135_REWRITES[0])
    second = paraphrase_verdict(_CASE_3135_SOURCE, _CASE_3135_REWRITES[0])
    assert first == second
    assert first.reason == "restated_source" and first.skeleton
    assert "复述" in first.describe()
    assert "有增量" in paraphrase_verdict(_CASE_3135_SOURCE, "那这速度还挺快").describe()


# ----------------------------------------------------------------------
# 指向事实：别人互问时压低开口权重
# ----------------------------------------------------------------------


def _peer_event(text="没想到是一天一个地区", *, sender="宇轩", **kwargs) -> MessageEvent:
    """2026-11-23 3135 现场事件：宇轩在答多萝，没有点 bot。"""
    return group_event(text, sender=sender, **kwargs)


def test_peer_ground_reads_platform_addressing():
    replied = _peer_event(reply_to="m1", reply_sender="duoluo")
    ground = peer_ground(replied)
    assert ground.to_peer and ground.replied_peer and not ground.to_bot
    assert ground.peers == ("duoluo",)

    mentioned = group_event("你们觉得呢", mention_map={"B": "阿乙"})
    assert peer_ground(mentioned).to_peer and not peer_ground(mentioned).replied_peer

    # @所有人 不是指向某个人；点到 bot 也不算「指向别人」。
    assert not peer_ground(group_event("大家好", mention_map={"all": ""})).to_peer
    to_bot = group_event("在吗", mentions=True)
    assert peer_ground(to_bot).to_bot and not peer_ground(to_bot).to_peer

    # 引用自己刚说的话补充说明，仍是说给大家/bot 听，不算「指向别人」。
    self_reply = _peer_event("补充一下", sender="宇轩", reply_to="m2", reply_sender="宇轩")
    assert not peer_ground(self_reply).replied_peer
    assert not peer_ground(self_reply).to_peer


def test_addressing_factor_only_penalizes_peer_to_peer():
    penalty = AddressingFactor(replied_peer=True, to_peer=True, penalty=1.0)
    assert penalty.penalized and penalty.score == -1.0
    # 只 @ 了别人、没回复谁：记录事实，但折扣为 0（可能是在问 bot）。
    mentioned_only = AddressingFactor(replied_peer=False, to_peer=True, penalty=1.0)
    assert not mentioned_only.penalized and mentioned_only.score == 0.0
    # 点到 bot 就不打折，哪怕同时回复了别人。
    to_bot = AddressingFactor(to_bot=True, to_peer=True, replied_peer=True, penalty=1.0)
    assert not to_bot.penalized and to_bot.score == 0.0


def test_engine_addressing_uses_aliases_before_penalizing():
    engine = make_engine()
    # 「Argon 和 Ruben 哪个好用」@ 了阿乙，但正文点了 bot 的名字：不折扣。
    called = group_event("Argon 和 Ruben 哪个更好用？", mention_map={"B": ""})
    assert not engine.addressing(called).penalized
    # 3135 这轮回复的是多萝，正文没有叫 bot：折扣。
    assert engine.addressing(_peer_event(reply_to="m1", reply_sender="duoluo")).penalized


def test_case_3135_silenced_at_decision_layer():
    """回放 3135：别人互答、没点 bot 的普通群消息，在裁定层直接沉默。"""
    engine = make_engine()
    event = _peer_event(reply_to="m1", reply_sender="duoluo")

    guard = engine.guard(event, now=100.0, proactive=True)
    assert guard is not None and not guard.should_reply
    assert guard.reason == "addressing_elsewhere"
    assert guard.addressing is not None and guard.addressing.penalized
    assert guard.factors is not None and guard.factors.addressing == -1.0
    assert "指向-1.00" in guard.factors.describe()
    assert "指向别人" in guard.addressing.describe()


def test_panda_and_direct_address_still_pass():
    """panda 案（知识型提问直答）与明确递到 bot 面前的接话不受指向因子影响。"""
    engine = make_engine()
    panda = group_event("下载 YouTube 视频用哪个工具？", mentions=True)
    assert engine.guard(panda, now=100.0, proactive=True) is None
    replied_to_bot = group_event("那用哪个参数？", reply_to="m9", reply_sender="__bot__")
    assert engine.guard(replied_to_bot, now=100.0, proactive=True) is None
    # 普通无指向的群消息照旧放行，交给策划。
    assert engine.guard(group_event("今天的天气不错"), now=100.0, proactive=True) is None
    # 私聊没有「别人」，永不折扣。
    assert engine.guard(private_event(), now=100.0, proactive=True) is None
    # 指向折扣只作用于普通主动开口路径；显式非主动调用不拦。
    peer = _peer_event(reply_to="m1", reply_sender="duoluo")
    assert engine.guard(peer, now=100.0) is None


def test_mention_peer_without_reply_is_audited_but_not_silenced():
    """@ 了别人但没回复谁：记录事实，交给策划——这类话也可能在问 bot。"""
    engine = make_engine()
    event = group_event("你们觉得哪个好用", mention_map={"B": "阿乙"})
    factor = engine.addressing(event)
    assert factor.to_peer and not factor.penalized
    assert factor.to_dict() == {"to_bot": False, "to_peer": True, "replied_peer": False,
                                "peers": ["B"], "penalized": False}
    assert engine.guard(event, now=100.0, proactive=True) is None


def test_addressing_factor_is_plain_data_for_wiring():
    """接线时可直接调用：结果可序列化，describe 为空话不产生占位文字。"""
    engine = make_engine()
    plain = engine.addressing(group_event("今天的天气不错"))
    assert plain.to_dict() == {"to_bot": False, "to_peer": False, "replied_peer": False,
                               "peers": [], "penalized": False}
    assert plain.describe() == "" and not plain.penalized
    # 主群案例：引用多萝、正文没叫 bot —— 事实与折扣都能直接交给日志。
    case = engine.addressing(_peer_event(reply_to="m1", reply_sender="duoluo"))
    assert case.to_dict() == {"to_bot": False, "to_peer": True, "replied_peer": True,
                              "peers": ["duoluo"], "penalized": True}
    assert case.describe() == "指向别人"


def test_case_3135_silenced_through_core_without_planner(config, store):
    """端到端回放：合并窗口 0 时，3135 这类消息走 core 也在策划前沉默。

    判定本身不在这里验证（发送前复核的接线见 ``tests/test_echo_wiring.py``）：
    指向因子在真实调用链上生效，不产生任何模型调用，decision_log 记下
    `addressing_elsewhere` 与指向折扣明细。
    """
    llm = ScriptedLLM({"fast": ['{"speak":true,"intent":"顺着接一句"}'], "chat": ["一天一个地区，天天有新地方可以开"]})
    bot, adapter = make_bot(config, store, llm)
    event = group_event("没想到是一天一个地区", sender="A", reply_to="m1", reply_sender="C")

    asyncio.run(bot.handle_event(event))

    assert adapter.texts == []
    assert llm.calls == []
    log, = store.list_decision_logs()
    assert log["reason"] == "addressing_elsewhere" and log["should_reply"] is False
    assert log["factors"]["addressing"]["penalized"] is True
