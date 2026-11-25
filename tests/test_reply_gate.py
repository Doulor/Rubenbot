"""回复门：生成前先按四类便宜分类，再决定口吻或沉默。

回放素材取自两段脱敏群聊（2026-11-26 00:24–00:29 电池帮、2026-11-25 14:35
交易闲聊），只保留说话人、原话和指向，账号与群号已替换。
"""

import asyncio
import json
import random
from types import SimpleNamespace

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.core import RubenBot
from rubenbot.persona import Persona
from rubenbot.reply_gate import (
    GATE_ROLE,
    GATE_TASK,
    ReplyGateResult,
    build_gate_messages,
    classify_reply,
    guidance,
    parse_gate,
    resolve_action,
)


def event(text, sender="星殒黎明", **kwargs):
    return MessageEvent("recording", "group", "电池帮", sender, sender, text, **kwargs)


def make_bot(config, store, llm, *, seed=40):
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben。"), rng=random.Random(seed))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def gate_reply(category, reason="回放"):
    return json.dumps({"category": category, "reason": reason}, ensure_ascii=False)


def run(bot, events):
    async def replay():
        for item in events:
            await bot.handle_event(item)

    asyncio.run(replay())


# ----------------------------------------------------------------------
# 解析与分发
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "category", "code"), [
    ('{"category":"调侃","reason":"损我"}', "调侃", "ok"),
    ('```json\n{"category":"灌水","reason":"刷屏"}\n```', "灌水", "ok"),
    ('先说一句 {"category":"真问题","reason":"问词义"} 完', "真问题", "ok"),
    ('{"category":"灌水","reason":', "", "invalid_json"),
    ('{"category":"夸我","reason":"没有这一类"}', "", "invalid_category"),
    ("", "", "invalid_json"),
])
def test_parse_gate_accepts_only_four_categories(raw, category, code):
    parsed, _, result_code = parse_gate(raw)
    assert parsed == category and result_code == code


def test_gate_input_reuses_room_state_and_closed_example_list(config, store):
    llm = ScriptedLLM()
    bot, _ = make_bot(config, store, llm)
    prompt = build_gate_messages(bot, event("因为ruben太笨了"))
    system, user = prompt[0]["content"], prompt[1]["content"]
    # 第 36 项的房间状态与四对范例原样复用，不另起一套上下文。
    assert "房间状态：" in user
    assert system.count(" → ") == 9  # 4 对固定范例 + 5 条分类补充，封闭清单 9 条
    assert "因为ruben太笨了" in user and "调侃" in system


def test_gate_uses_cheap_role_and_records_cost(config, store):
    llm = ScriptedLLM({"reply_gate": [gate_reply("调侃")]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("因为ruben太笨了", mentions_bot=True)])
    gate = llm.gate_calls[0]
    assert gate["task"] == GATE_TASK and gate["role"] == GATE_ROLE
    assert gate["json_mode"] is True
    row, = store.list_decision_logs()
    factors = row["factors"]["reply_gate"]
    assert factors["category"] == "调侃" and factors["result_code"] == "ok"
    assert factors["model"] == config.llm.model_for(GATE_ROLE)
    assert isinstance(factors["latency_ms"], int) and factors["usage_id"] is None


def test_gate_usage_is_recorded_under_own_task(config, store):
    """真客户端：分类请求单独记一条 reply_gate 用量，token 与 latency 都在。"""
    import httpx

    from rubenbot.llm import LLMClient, make_usage_recorder

    def handler(request):
        body = json.loads(request.content)
        content = gate_reply("调侃") if body.get("response_format") else (
            '{"basis":"common","speak":true,"intent":"反驳","increment":"呛回去"}'
        )
        if "gen_stage2" == body.get("model"):
            content = "笨也比你反应快"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 6, "total_tokens": 46},
        })

    client = LLMClient(config.llm, transport=httpx.MockTransport(handler),
                       usage_recorder=make_usage_recorder(store), retry_delays=(0.0,))
    bot, _ = make_bot(config, store, client)
    run(bot, [event("因为ruben太笨了", mentions_bot=True)])
    asyncio.run(client.close())

    with store._lock:
        rows = store._conn.execute(
            "SELECT task, prompt_tokens, completion_tokens, latency_ms FROM llm_usage ORDER BY id"
        ).fetchall()
    # 策划先跑（分类按简报接在 generate 开头），然后分类，再正文。
    assert [row["task"] for row in rows] == ["gen_stage1", "reply_gate", "gen_stage2"]
    gate_row = rows[1]
    assert gate_row["prompt_tokens"] == 40 and gate_row["completion_tokens"] == 6
    assert gate_row["latency_ms"] >= 0
    log, = store.list_decision_logs()
    factors = log["factors"]["reply_gate"]
    assert factors["usage_id"] is not None and factors["completion_tokens"] == 6


# ----------------------------------------------------------------------
# 四类回放：灌水 / 调侃 / 真问题 / 不理
# ----------------------------------------------------------------------


def test_swill_gets_short_reply_or_silence(config, store):
    """交易闲聊刷「坚定」「+1」：按气氛短接或沉默，不讲课。"""
    llm = ScriptedLLM({"reply_gate": [gate_reply("灌水")], "fast": [
        '{"basis":"common","speak":true,"intent":"接梗","increment":"顺着起哄"}'], "chat": ["坚定个屁"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("坚定", sender="喜忧半半开")])
    assert adapter.texts == ["坚定个屁"]
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "【回复门·本批性质】" in stage2 and "最多十个字" in stage2
    assert "爆仓线" not in stage2 and "止盈" not in stage2


def test_tease_is_pushed_back_not_admitted(config, store):
    """被说「因为ruben太笨了」：按调侃接话，不再套「这话我认」。"""
    llm = ScriptedLLM({"reply_gate": [gate_reply("调侃")], "fast": [
        '{"basis":"common","speak":true,"intent":"反驳","increment":"呛回去"}'], "chat": ["笨也比你反应快"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("因为ruben太笨了", mentions_bot=True)])
    assert adapter.texts == ["笨也比你反应快"]
    assert "这话我认" not in "".join(adapter.texts)
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "在拿你开涮" in stage2 and "顶回去" in stage2
    row, = store.list_decision_logs()
    assert row["factors"]["reply_gate"]["category"] == "调侃"


def test_real_question_answers_short(config, store):
    llm = ScriptedLLM({"reply_gate": [gate_reply("真问题")], "fast": [
        '{"basis":"common","speak":true,"intent":"补充新信息","increment":"给词义"}'], "chat": ["褒义，夸人善良"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("贤惠是褒义还是贬义？", mentions_bot=True)])
    assert adapter.texts == ["褒义，夸人善良"]
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "直接给答案" in stage2 and "一两句话说清" in stage2


def test_ignore_stays_silent(config, store):
    """别人互相约事、没叫到 bot：分类「不理」，这一轮不出声。"""
    llm = ScriptedLLM({"reply_gate": [gate_reply("不理")], "fast": [
        '{"basis":"common","speak":true,"intent":"接梗","increment":"接一句"}'], "chat": ["不该发出去"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("我四点来找你，你先吃吧", sender="喜忧半半开")])
    assert adapter.texts == []
    # 策划照常看过（它在生成之前），但没进正文生成。
    assert [c["task"] for c in llm.calls] == ["gen_stage1"]
    row, = store.list_decision_logs()
    assert row["reason"] == "veto_marker" and row["should_reply"] is False
    gate = row["factors"]["reply_gate"]
    assert gate["category"] == "不理" and gate["silenced"] is True


def test_ignore_downgrades_to_short_reply_when_directly_called(config, store):
    """被 @ 的话不会是「不理」；分类犹豫时也退成一两个字，不整轮沉默。"""
    llm = ScriptedLLM({"reply_gate": [gate_reply("不理")], "fast": [
        '{"basis":"common","speak":true,"intent":"短附和","increment":"应一声"}'], "chat": ["嗯"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("在吗", mentions_bot=True)])
    assert adapter.texts == ["嗯"]
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "最多十个字" in stage2  # 按灌水短接，不整轮沉默
    row, = store.list_decision_logs()
    # 审计保留模型的原始判断，「降成灌水处理」不覆盖它。
    assert row["factors"]["reply_gate"]["category"] == "不理"


# ----------------------------------------------------------------------
# 失败与边界
# ----------------------------------------------------------------------


def test_gate_failure_falls_back_to_original_flow(config, store):
    llm = ScriptedLLM({"reply_gate": ["不是 JSON"], "fast": [
        '{"basis":"common","speak":true,"intent":"接梗","increment":"接一句"}'], "chat": ["嗯哼"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("今天好热", mentions_bot=True)])
    assert adapter.texts == ["嗯哼"]
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "回复门" not in stage2
    row, = store.list_decision_logs()
    assert row["factors"]["reply_gate"]["result_code"] == "invalid_json"


def test_gate_error_does_not_block_reply(config, store):
    class BoomGate(ScriptedLLM):
        async def complete(self, messages, *, role="chat", task=None, **kwargs):
            if task == GATE_TASK:
                raise RuntimeError("分类模型挂了")
            return await super().complete(messages, role=role, task=task, **kwargs)

    llm = BoomGate({"fast": ['{"basis":"common","speak":true,"intent":"接梗","increment":"接"}'],
                    "chat": ["还行"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("在吗", mentions_bot=True)])
    assert adapter.texts == ["还行"]
    row, = store.list_decision_logs()
    assert row["factors"]["reply_gate"]["result_code"] == "request_failed"


def test_private_chat_skips_gate(config, store):
    llm = ScriptedLLM({"chat": ["在"]})
    bot, adapter = make_bot(config, store, llm)
    private = MessageEvent("recording", "private", "u1", "u1", "阿花", "在吗")
    run(bot, [private])
    assert adapter.texts == ["在"]
    assert llm.gate_calls == []


@pytest.mark.parametrize("text", ["[图片]", "[表情]", "[图片][表情]"])
def test_pure_visual_message_skips_gate(config, store, text):
    """纯图、纯表情没有可分类的文字，别为它们多花一次调用。"""
    llm = ScriptedLLM({"chat": ["图收到了"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event(text, mentions_bot=True)])
    assert llm.gate_calls == []
    assert adapter.texts == ["图收到了"]


def test_gate_runs_once_per_round(config, store):
    """草稿被判复述后的重写不再花第二次分类。"""
    llm = ScriptedLLM({"reply_gate": [gate_reply("灌水")], "fast": [
        '{"basis":"common","speak":true,"intent":"接梗","increment":"接"}'],
        "chat": ["这 10:1 只能算许愿", "换个说法"]})
    bot, adapter = make_bot(config, store, llm)
    run(bot, [event("坚定", sender="喜忧半半开")])
    assert len(llm.gate_calls) == 1


def test_classify_returns_unusable_when_llm_missing(config, store):
    class NoLLM:
        pass

    holder = SimpleNamespace(config=config, llm=NoLLM(), decision_engine=None)
    result = asyncio.run(classify_reply(holder, event("在吗")))
    assert result.result_code == "no_llm" and not result.usable


def test_resolve_action_keeps_hard_trigger(config, store):
    llm = ScriptedLLM()
    bot, _ = make_bot(config, store, llm)
    direct = event("在吗", mentions_bot=True)
    plain = event("我四点来找你")
    silent = ReplyGateResult(category="不理", reason="别人聊")
    assert resolve_action(silent, plain, bot) == ("silent", "不理")
    assert resolve_action(silent, direct, bot) == ("talk", "灌水")
    # 分类不可用时一律照原流程生成。
    assert resolve_action(ReplyGateResult(result_code="timeout"), plain, bot) == ("talk", "")


def test_guidance_only_for_three_talking_categories():
    assert "顶回去" in guidance("调侃", "[不回复]")
    assert "最多十个字" in guidance("灌水", "[不回复]")
    assert "直接给答案" in guidance("真问题", "[不回复]")
    assert guidance("不理", "[不回复]") == ""
    assert guidance("", "[不回复]") == ""


# ----------------------------------------------------------------------
# 脱敏回放：两段群聊原话（账号与群号已替换，只留说话人与正文）
# ----------------------------------------------------------------------

# 2026-11-26 00:24–00:29 电池帮：星殒黎明说 bot「太笨」，bot 回了「这话我认」。
BATTERY_GANG = [
    ("多萝Doulor", "你怎么变成ruben最讨厌的人了"),
    ("星殒黎明", "因为ruben太笨了"),
    ("星殒黎明", "我说纯爱牛逼它来句现在是大白天"),
    ("星殒黎明", "现在又来句这话我认"),
]

# 2026-11-25 14:35 交易闲聊：一串起哄和立场刷屏。
TRADING = [
    ("喜忧半半开", "12张"),
    ("喜忧半半开", "包起飞的！"),
    ("喜忧半半开", "坚定"),
    ("小栀", "+1"),
]


def replay(config, store, llm, lines, *, mentions=False):
    config.decision.merge_window_seconds = config.decision.hard_merge_window_seconds = 2
    bot, adapter = make_bot(config, store, llm)
    tail = lines[-1][1]
    events = [
        event(text, sender=name, mentions_bot=mentions and text == tail)
        for name, text in lines
    ]

    async def run_replay():
        for item in events:
            await bot.handle_event(item)
        await bot.flush_merger(all_rounds=True)

    asyncio.run(run_replay())
    return bot, adapter, llm.gate_calls[0] if llm.gate_calls else None


def test_replay_tease_does_not_admit(config, store):
    llm = ScriptedLLM({
        "reply_gate": [gate_reply("调侃")],
        "fast": ['{"basis":"common","speak":true,"intent":"反驳","increment":"顶回去"}'],
        "chat": ["笨也比你反应快"],
    })
    _, adapter, gate = replay(config, store, llm, BATTERY_GANG)
    assert gate is not None
    # 分类看到的原话就是本批触发的那句，房间状态与主生成同一口径。
    assert "因为ruben太笨了" in gate["messages"][1]["content"]
    assert "房间状态：" in gate["messages"][1]["content"]
    # 封闭清单里有这一对；生成指导叫它顶回去，不教它认账。
    assert "因为ruben太笨了」 → 调侃" in gate["messages"][0]["content"]
    assert "这话我认" not in gate["messages"][0]["content"]
    stage2 = llm.calls[-1]["messages"][-1]["content"]
    assert "顶回去" in stage2 and "这话我认" not in "".join(adapter.texts)
    log, = store.list_decision_logs()
    assert log["factors"]["reply_gate"]["category"] == "调侃"


def test_replay_trading_swill_is_short_or_silent(config, store):
    llm = ScriptedLLM({
        "reply_gate": [gate_reply("灌水")],
        "fast": ['{"basis":"common","speak":false,"intent":"沉默"}'],
        "chat": ["不该发出去"],
    })
    _, adapter, gate = replay(config, store, llm, TRADING)
    assert gate is None
    # 策划已裁定沉默：生成步根本不会跑，也不会有讲课式长回复。
    assert adapter.texts == []
    log, = store.list_decision_logs()
    assert log["reason"] in ("planner_silence", "attention_skip", "veto_marker")

    # 另一条路：策划同意接下（加一句直接呼叫让它进策划），也只会拿到短接口吻。
    called = [*TRADING[:-1], ("小栀", "坚定，@Ruben 你说呢")]
    llm2 = ScriptedLLM({
        "reply_gate": [gate_reply("灌水")],
        "fast": ['{"basis":"common","speak":true,"intent":"接梗","increment":"顺着起哄"}'],
        "chat": ["坚定个屁"],
    })
    _, adapter2, gate2 = replay(config, store, llm2, called, mentions=True)
    assert "坚定" in gate2["messages"][1]["content"]
    assert adapter2.texts == ["坚定个屁"]
    stage2 = llm2.calls[-1]["messages"][-1]["content"]
    assert "最多十个字" in stage2
    assert not any(word in stage2 for word in ("爆仓线", "止盈", "止损", "杠杆率"))
