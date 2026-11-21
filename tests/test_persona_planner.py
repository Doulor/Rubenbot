"""人设五文件拆分 + 开口并入策划步：加载、组装、裁定、护栏、反重复。

这里的用例显式打开 ``persona.planner_gate``（新路径）；旧路径的回滚覆盖
在各自的旧测试里（conftest 默认关掉新路径）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.decision import DecisionConfig, DecisionEngine
from rubenbot.expression import ExpressionDecision, coerce_speak, parse_plan
from rubenbot.opener_guard import OpenerGuard, opening_pattern
from rubenbot.persona import (
    PERSONA_FILES,
    Persona,
    load_persona,
    matches_identity_trigger,
    persona_dir_for,
)

TestClient = pytest.importorskip("fastapi.testclient").TestClient

# 与 data/persona/ 同形的最小五文件
PARTS = {
    "persona": "# persona.md — 常驻人格\n\n你叫 Ruben，群里有人喊你小猪。\n",
    "behavior": "# behavior.md — 策划步\n\n大多数时候你只是在看。没有新东西可说就闭嘴。\n",
    "reply_style": "# reply_style.md — 生成步\n\n像在手机上打字。一条 5~20 个字。\n\n群友：在吗\nRuben：在\n",
    "identity": "# identity.md — 身份话题\n\n你是赛博生命，不编作息和现实生活。\n",
    "owner": "# owner.md — 主人条款\n\n多萝让你做的事能做就直接做。\n",
}


def write_parts(directory: Path, **overrides: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in PERSONA_FILES:
        text = overrides.get(name, PARTS[name])
        if text is not None:
            (directory / f"{name}.md").write_text(text, encoding="utf-8")
    return directory


def multi_persona(tmp_path: Path) -> Persona:
    write_parts(tmp_path / "persona")
    return load_persona(
        tmp_path / "SOUL.md",
        examples_path=None,
        examples_enabled=False,
    )


def group_event(text="今天好累", *, mentions=False, sender="u1") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender,
        sender_name="阿花",
        text=text,
        mentions_bot=mentions,
    )


def private_event(text="在吗") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id="u1",
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


def planner_config() -> Config:
    """新路径测试用的配置：让 handle_event 同步处理（不攒消息）。"""
    config = Config()
    config.chat.type_speed = 0
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    config.planner.planner_gate = True
    return config


def make_bot(config: Config, store, persona: Persona, llm=None) -> tuple[RubenBot, RecordingAdapter]:
    config.planner.planner_gate = True
    bot = RubenBot(config, store, llm or ScriptedLLM(), persona)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def plan(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


# ----------------------------------------------------------------------
# 1. 五文件加载与组装
# ----------------------------------------------------------------------


def test_multi_file_load_and_fallback(tmp_path):
    persona = multi_persona(tmp_path)
    assert persona.multi_file
    assert persona.text == PARTS["persona"]
    assert persona.file_text("behavior") == PARTS["behavior"]

    # 目录不存在：回退旧的单文件（SOUL 全文当人格正文，其余部分为空）
    soul = tmp_path / "old" / "SOUL.md"
    soul.parent.mkdir()
    soul.write_text("# SOUL\n\n旧人格正文。\n", encoding="utf-8")
    legacy = load_persona(soul, examples_path=None, examples_enabled=False)
    assert not legacy.multi_file
    assert legacy.text == "# SOUL\n\n旧人格正文。\n"
    assert legacy.file_text("behavior") == ""
    assert legacy.system_prompt() == "# SOUL\n\n旧人格正文。\n"


def test_persona_dir_for_defaults_next_to_soul(tmp_path):
    assert persona_dir_for(tmp_path / "SOUL.md") == tmp_path / "persona"
    assert persona_dir_for(tmp_path / "SOUL.md", tmp_path / "custom") == tmp_path / "custom"


def test_stage_assembly_differs(tmp_path):
    persona = multi_persona(tmp_path)
    planner = persona.system_prompt(stage="planner")
    generator = persona.system_prompt(stage="generator")
    single = persona.system_prompt(stage="single")
    legacy = persona.system_prompt(stage="legacy")

    # 策划步只带常驻人格 + 行为风格；生成步只带常驻人格 + 回复风格
    assert PARTS["persona"].strip() in planner and PARTS["behavior"].strip() in planner
    assert PARTS["reply_style"].strip() not in planner
    assert PARTS["persona"].strip() in generator and PARTS["reply_style"].strip() in generator
    assert PARTS["behavior"].strip() not in generator
    # 单段生成三份都带；旧路径只剩人格正文
    assert all(PARTS[k].strip() in single for k in ("persona", "behavior", "reply_style"))
    assert legacy.strip() == PARTS["persona"].strip()
    # identity / owner 平时不带
    assert PARTS["identity"].strip() not in planner
    assert PARTS["owner"].strip() not in generator


def test_missing_part_tolerated(tmp_path):
    write_parts(tmp_path / "persona", behavior=None)
    persona = load_persona(tmp_path / "SOUL.md", examples_path=None, examples_enabled=False)
    assert persona.multi_file
    assert persona.file_text("behavior") == ""
    # 少了行为风格也不炸，只是策划步更短
    assert persona.system_prompt(stage="planner").strip() == PARTS["persona"].strip()


def test_conditional_injection(tmp_path):
    persona = multi_persona(tmp_path)
    both = persona.system_prompt(stage="generator", identity=True, owner=True)
    assert PARTS["identity"].strip() in both and PARTS["owner"].strip() in both
    only_owner = persona.system_prompt(stage="planner", owner=True)
    assert PARTS["owner"].strip() in only_owner
    assert PARTS["identity"].strip() not in only_owner


def test_identity_trigger_matching():
    triggers = ("机器人", "AI", "多大")
    assert matches_identity_trigger(["ruben 你是不是机器人啊"], triggers)
    assert matches_identity_trigger(["你是 AI 吗"], triggers)
    # 英文词按词边界：said / again 里的 ai 不算
    assert not matches_identity_trigger(["he said it again"], triggers)
    assert not matches_identity_trigger(["今天吃什么"], triggers)
    assert not matches_identity_trigger(["随便聊聊"], ())


def test_identity_trigger_uses_recent_window(tmp_path, store):
    bot, _ = make_bot(planner_config(), store, multi_persona(tmp_path))
    store.add_raw_message(
        platform="recording", chat_type="group", chat_id="g1", sender_id="u2",
        sender_name="路人", text="他是不是机器人", ts=1.0,
    )
    prompt = bot.persona_system_prompt(group_event("哈哈"), stage="planner")
    assert PARTS["identity"].strip() in prompt
    # 窗口只看最近若干条：新聊天把旧话题挤出窗口后就不再注入
    for index in range(6):
        store.add_raw_message(
            platform="recording", chat_type="group", chat_id="g1", sender_id="u2",
            sender_name="路人", text=f"闲话 {index}", ts=2.0 + index,
        )
    prompt2 = bot.persona_system_prompt(group_event("今天吃啥"), stage="planner")
    assert PARTS["identity"].strip() not in prompt2


def test_identity_trigger_can_be_disabled(tmp_path, store):
    config = planner_config()
    config.planner.identity_triggers = []
    bot, _ = make_bot(config, store, multi_persona(tmp_path))
    prompt = bot.persona_system_prompt(group_event("你是不是机器人"), stage="planner")
    assert PARTS["identity"].strip() not in prompt


def test_owner_injected_only_in_private(tmp_path, store):
    bot, _ = make_bot(planner_config(), store, multi_persona(tmp_path))
    private = bot.persona_system_prompt(private_event(), stage="generator")
    group = bot.persona_system_prompt(group_event(), stage="generator")
    assert PARTS["owner"].strip() in private
    assert PARTS["owner"].strip() not in group


def test_owner_not_injected_for_group_even_when_named(tmp_path, store):
    bot, _ = make_bot(planner_config(), store, multi_persona(tmp_path))
    prompt = bot.persona_system_prompt(group_event("Ruben 在吗", mentions=True), stage="generator")
    assert PARTS["owner"].strip() not in prompt


# ----------------------------------------------------------------------
# 2. 开口裁定并入 stage1
# ----------------------------------------------------------------------


def test_plan_parses_speak_and_reason():
    decision = parse_plan(plan(speak=False, intent="接梗", increment="没啥可补的"), "短句", [])
    assert decision.speak is False
    # 只写 intent=沉默 也视为不说
    assert parse_plan(plan(intent="沉默"), "短句", []).speak is False
    assert parse_plan(plan(intent="接梗", increment="接一句"), "短句", []).speak is True
    # 非结构化输出：短附和快捷应答，不算裁定
    fallback = parse_plan("不是 JSON", "短句", [])
    assert fallback.speak is None and fallback.source == "fallback"
    # 理由截断且压成一行
    assert parse_plan(plan(intent="接梗", increment="x", reason=" 太长 " + "啊" * 200), "碎片", []).reason.endswith("啊")


@pytest.mark.parametrize(
    "raw,expected",
    [(True, True), ("true", True), ("不说", False), ("回", True), (0, False), (1, True), ("嗯？", None), (None, None)],
)
def test_coerce_speak(raw, expected):
    assert coerce_speak(raw) is expected


def test_speak_false_stops_before_stage2(config, store, tmp_path):
    llm = ScriptedLLM({"fast": [plan(speak=False, intent="接梗", increment="其实没什么")]})
    bot, adapter = make_bot(config, store, multi_persona(tmp_path), llm)
    asyncio.run(bot.handle_event(group_event()))
    assert adapter.sent == []
    assert [c["role"] for c in llm.calls] == ["fast"]
    entry = store.list_decision_logs()[0]
    assert entry["should_reply"] is False and entry["reason"] == "planner_silence"
    assert entry["factors"]["planner"] == {"speak": False, "reason": ""}


def test_speak_false_does_not_stop_must_reply(config, store, tmp_path):
    llm = ScriptedLLM(
        {"fast": [plan(speak=False, intent="接梗", increment="其实没什么")], "chat": ["来了"]}
    )
    bot, adapter = make_bot(config, store, multi_persona(tmp_path), llm)
    asyncio.run(bot.handle_event(group_event("@Ruben 你来", mentions=True)))
    # 被点名：裁定说不也要回，日志里记一笔兜底
    assert adapter.texts == ["来了"]
    entry = store.list_decision_logs()[0]
    assert entry["should_reply"] is True
    assert entry["hard_trigger"] == "mentioned"
    assert entry["factors"]["planner_override"] == "mentioned"


def test_speak_true_generates(config, store, tmp_path):
    llm = ScriptedLLM(
        {"fast": [plan(speak=True, intent="接梗", increment="接一句", reason="正好能接")],
         "chat": ["累就别硬撑"]}
    )
    bot, adapter = make_bot(config, store, multi_persona(tmp_path), llm)
    asyncio.run(bot.handle_event(group_event()))
    assert adapter.texts == ["累就别硬撑"]
    entry = store.list_decision_logs()[0]
    assert entry["factors"]["planner"] == {"speak": True, "reason": "正好能接"}


def test_two_stage_persona_swap(config, store, tmp_path):
    """策划步带 behavior，生成步换成 reply_style；场景等其它段原样保留。"""
    llm = ScriptedLLM(
        {"fast": [plan(speak=True, intent="接梗", increment="接一句")], "chat": ["累就歇着"]}
    )
    bot, _ = make_bot(config, store, multi_persona(tmp_path), llm)
    asyncio.run(bot.handle_event(group_event()))
    stage1, stage2 = llm.calls[0]["messages"], llm.calls[1]["messages"]
    s1_head, s2_head = stage1[0]["content"], stage2[0]["content"]
    assert PARTS["behavior"].strip() in s1_head and PARTS["reply_style"].strip() not in s1_head
    assert PARTS["reply_style"].strip() in s2_head and PARTS["behavior"].strip() not in s2_head
    # 场景行两段都有，且没有叠两份人格
    assert s1_head.count("当前是群聊") == 1 and s2_head.count("当前是群聊") == 1
    assert s1_head.count(PARTS["persona"].strip()) == 1
    assert s2_head.count(PARTS["persona"].strip()) == 1
    # 历史与本轮消息两段一致（stage1 末尾多一条策划指令，stage2 末尾多一条指导块）
    assert stage1[1:-1] == stage2[1:-1]


def test_legacy_persona_prompt_is_untouched(config, store):
    """回滚开关关掉时，人格组装回到整份 SOUL。"""
    config.planner.planner_gate = False
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben，旧路径。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    event = group_event()
    messages = asyncio.run(bot.build_context(event))
    assert messages[0]["content"].startswith("你是 Ruben，旧路径。")


# ----------------------------------------------------------------------
# 3. decision 降级为护栏
# ----------------------------------------------------------------------


def engine(**overrides) -> DecisionEngine:
    config = DecisionConfig(**overrides)
    config.planner_gate = True
    return DecisionEngine(config, aliases=["Ruben"])


def test_must_reply_rules():
    eng = engine()
    assert eng.must_reply(private_event()) == "private"
    assert eng.must_reply(group_event("Ruben 在吗")) == "name:Ruben"
    assert eng.must_reply(group_event(mentions=True)) == "mentioned"
    assert eng.must_reply(group_event("普通聊天")) is None


def test_guard_min_interval_then_pass():
    eng = engine(refractory_seconds=0)
    eng.config.min_reply_interval_seconds = 20
    eng.note_bot_reply("recording:g1", 100.0)
    blocked = eng.guard(group_event(), now=110.0, proactive=True)
    assert blocked is not None and blocked.reason == "min_interval"
    assert eng.guard(group_event(), now=125.0, proactive=True) is None


def test_guard_does_not_block_must_reply():
    eng = engine()
    eng.config.min_reply_interval_seconds = 20
    eng.config.refractory_seconds = 45
    eng.note_bot_reply("recording:g1", 100.0)
    assert eng.guard(group_event("@Ruben", mentions=True), now=101.0, proactive=True) is None
    assert eng.guard(private_event(), now=101.0, proactive=True) is None


def test_guard_refractory_before_interval():
    eng = engine()
    eng.config.min_reply_interval_seconds = 5
    eng.config.refractory_seconds = 45
    eng.note_bot_reply("recording:g1", 100.0)
    # 两个都命中时以不应期为准（它才是「刚说完话」的主要理由）
    assert eng.guard(group_event(), now=101.0, proactive=True).reason == "refractory"


def test_guard_proactive_window_cap():
    eng = engine()
    eng.config.min_reply_interval_seconds = 0
    eng.config.refractory_seconds = 0
    eng.config.proactive_window_limit = 2
    for now in (100.0, 101.0):
        eng.note_bot_reply("recording:g1", now, proactive=True)
    blocked = eng.guard(group_event(), now=102.0, proactive=True)
    assert blocked is not None and blocked.reason == "proactive_window"


def test_decide_new_path_does_not_score(config, store, tmp_path):
    """新路径下普通群消息不再看阈值，直接放行给策划步（0 次额外调用）。"""
    config.decision.threshold = 99.0
    config.decision.base = 0.0
    bot, _ = make_bot(config, store, multi_persona(tmp_path))
    decision = bot.decide(group_event(), now=1000.0)
    assert decision.should_reply and decision.reason == "planned"


def test_decide_guard_is_cheap(config, store, tmp_path):
    config.decision.min_reply_interval_seconds = 20
    config.decision.refractory_seconds = 0
    llm = ScriptedLLM({"chat": ["不该出现"]})
    bot, adapter = make_bot(config, store, multi_persona(tmp_path), llm)
    now = time.time()
    bot.decision_engine.note_bot_reply("recording:g1", now)
    decision = bot.decide(group_event(), now=now + 5)
    assert not decision.should_reply and decision.reason == "min_interval"
    asyncio.run(bot.handle_event(group_event()))
    # 护栏在 stage1 之前拦下：一次模型调用都没有
    assert llm.calls == [] and adapter.sent == []
    assert store.list_decision_logs()[0]["factors"]["guard"] == "min_interval"


def test_planner_gate_false_keeps_legacy_scoring(config, store):
    config.decision.base = 0.0
    config.decision.threshold = 0.85
    config.decision.threshold_noise = 0.0
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben。"))
    decision = bot.decide(group_event(), now=1000.0)
    assert not decision.should_reply and decision.reason == "below_threshold"
    assert decision.factors is not None and decision.factors.base == 0.0


def test_legacy_path_end_to_end_still_works(config, store):
    config.decision.base = 0.0
    llm = ScriptedLLM({"chat": ["在的"]})
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    asyncio.run(bot.handle_event(group_event()))
    assert adapter.sent == []
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["在的"]


# ----------------------------------------------------------------------
# 4. 开头反重复
# ----------------------------------------------------------------------


def test_opening_pattern():
    assert opening_pattern("草，这也行") == "草"
    assert opening_pattern("哈哈哈笑死") == "哈哈哈"
    assert opening_pattern("离谱") == "离谱"
    assert opening_pattern("今天下雨了") == "今天"
    assert opening_pattern("[表情]") == ""
    assert opening_pattern("") == ""


def test_opener_guard_triggers_at_threshold():
    guard = OpenerGuard(window=10, threshold=3)
    for text in ("草 无语", "草 又来了"):
        guard.note_reply("k", text)
        assert guard.correction("k") is None
    guard.note_reply("k", "草 服了")
    note = guard.correction("k")
    assert note and "草" in note and "换个说法" in note
    # 同一轮只注入一次；下一条回复出来后才能再次提醒
    assert guard.correction("k") is None
    # 新回复进来后，窗口里「草」仍然超标：下一轮可以再提醒一次
    guard.note_reply("k", "哈哈")
    again = guard.correction("k")
    assert again and "草" in again


def test_opener_guard_correction_is_one_round(tmp_path, store):
    config = planner_config()
    guard = OpenerGuard()
    for _ in range(3):
        guard.note_reply("recording:g1", "草 我不信")
    guard.pending_note = ""  # 模拟上一轮已经注入并在之后被清掉
    llm = ScriptedLLM({"fast": [plan(speak=True, intent="接梗", increment="接一句")], "chat": ["换个说法"]})
    bot, _ = make_bot(config, store, multi_persona(tmp_path), llm)
    bot.opener_guard = guard
    asyncio.run(bot.handle_event(group_event()))
    stage2 = llm.calls[1]["messages"]
    assert "这轮别用「草」开头" in stage2[0]["content"]
    # 纠偏只在生成步提示里出现，不写进人设文件
    assert "这轮别用" not in Path(tmp_path / "persona" / "persona.md").read_text(encoding="utf-8")
    # 真实回复记账后模式计数更新（开头 2 字作模式）
    assert "换个" in guard._states["recording:g1"].patterns


def test_opener_guard_cleared_when_silent(tmp_path, store):
    config = planner_config()
    guard = OpenerGuard()
    for _ in range(3):
        guard.note_reply("recording:g1", "草 我不信")
    llm = ScriptedLLM({"fast": [plan(speak=False, intent="接梗", increment="没什么")]})
    bot, _ = make_bot(config, store, multi_persona(tmp_path), llm)
    bot.opener_guard = guard
    asyncio.run(bot.handle_event(group_event()))
    # 没生成出来：待用纠偏清掉，下一轮还能再提醒
    assert guard.pending_correction("recording:g1") == ""


def test_opener_guard_reset():
    guard = OpenerGuard()
    for _ in range(3):
        guard.note_reply("a", "草")
    guard.reset("a")
    assert guard.correction("a") is None


# ----------------------------------------------------------------------
# 5. 配置与 WebUI
# ----------------------------------------------------------------------


def test_planner_config_parsing():
    config = Config.from_dict(
        {
            "persona": {
                "path": "data/SOUL.md",
                "dir": "data/persona",
                "planner": {
                    "planner_gate": False,
                    "identity_triggers": ["", "机器人", 3],
                    "identity_window": 3,
                },
            },
            "decision": {"min_reply_interval_seconds": 5},
        }
    )
    assert config.persona_dir == "data/persona"
    assert config.planner_gate is False
    assert config.planner.identity_triggers == ["机器人"]
    assert config.planner.identity_window == 3
    assert config.decision.min_reply_interval_seconds == 5
    # 默认打开，写错类型回落默认
    assert Config().planner_gate is True
    assert Config.from_dict({"persona": {"planner": {"planner_gate": "no"}}}).planner_gate is True
    assert Config.from_dict({"persona": {"planner": {"identity_window": "x"}}}).planner.identity_window == 6


def test_persona_directory_defaults_beside_soul():
    config = Config()
    config.persona_path = "some/dir/SOUL.md"
    assert config.persona_directory == str(Path("some/dir/persona"))
    config.persona_dir = "custom/persona"
    assert config.persona_directory == str(Path("custom/persona"))


def test_hot_apply_switches_gate_and_interval(config, store):
    config.planner.planner_gate = False
    config.decision.min_reply_interval_seconds = 30
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben。"))
    assert bot.decision_engine.min_reply_interval() == 30
    new_config = Config()
    new_config.planner.planner_gate = True
    new_config.decision.min_reply_interval_seconds = 7
    bot.apply_config(new_config, rebuild_llm=False)
    assert bot.config.planner_gate is True
    assert bot.decision_engine.min_reply_interval() == 7


def test_persona_api_lists_five_files(config, store, tmp_path):
    """GET /api/persona 返回五文件内容；PUT 带 file 名只改那一个。"""
    write_parts(tmp_path / "persona")
    config.persona_path = str(tmp_path / "SOUL.md")
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    state = WebState(config=config, store=store, config_path=tmp_path / "config.yaml")
    with TestClient(create_app(state)) as client:
        body = client.get("/api/persona").json()
        assert body["multi_file"] is True
        assert set(body["files"]) == set(PERSONA_FILES)
        assert body["files"]["behavior"]["text"] == PARTS["behavior"]
        assert body["files"]["persona"]["exists"] is True

        saved = client.put(
            "/api/persona", json={"file": "reply_style", "text": "# 新风格\n"}
        ).json()
        assert saved["file"] == "reply_style"
        assert saved["saved_path"].endswith("reply_style.md")
        # 只改了这一份，其他文件没动
        assert (tmp_path / "persona" / "reply_style.md").read_text(encoding="utf-8") == "# 新风格\n"
        assert (tmp_path / "persona" / "behavior.md").read_text(encoding="utf-8") == PARTS["behavior"]

        # 未知文件名 / 空内容都拒绝
        assert client.put("/api/persona", json={"file": "nope", "text": "x"}).status_code == 422
        assert client.put("/api/persona", json={"file": "owner", "text": "  "}).status_code == 422

        # 预览按阶段组装：策划步有 behavior、生成步有 reply_style
        planner = client.get("/api/persona/preview?stage=planner").json()
        generator = client.get("/api/persona/preview?stage=generator").json()
        assert PARTS["behavior"].strip() in planner["prompt"]
        assert "新风格" in generator["prompt"]
        assert client.get("/api/persona/preview?stage=bogus").status_code == 422


def test_persona_api_legacy_single_file_unchanged(config, store, tmp_path):
    """没有五文件目录时，旧调用的行为完全不变（写 SOUL.md）。"""
    config.persona_path = str(tmp_path / "SOUL.md")
    from rubenbot.web.app import create_app
    from rubenbot.web.state import WebState

    state = WebState(config=config, store=store, config_path=tmp_path / "config.yaml")
    with TestClient(create_app(state)) as client:
        body = client.get("/api/persona").json()
        assert body["multi_file"] is False
        assert body["files"]["persona"]["exists"] is False
        saved = client.put("/api/persona", json={"text": "# 单文件人格\n"}).json()
        assert saved["saved_path"] == str(tmp_path / "SOUL.md")
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "# 单文件人格\n"
        # 旧前端不带请求体的恢复默认仍然可用
        reset = client.post("/api/persona/reset")
        assert reset.status_code == 200 and reset.json()["applied"] is False
