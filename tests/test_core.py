"""主循环：触发判断、上下文拼装、拆条发送、异常隔离。"""

import asyncio
import json
import random
import re
import time


from rubenbot.adapters.base import MessageEvent, Segment
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.core import BOT_SENDER_ID, RubenBot
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


def make_bot(config, store, llm, *, plugins=None, rng=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"), plugins=plugins, rng=rng)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def group_event(text="你好呀", sender="u1", name="阿花", mentions=False) -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id=sender,
        sender_name=name,
        text=text,
        mentions_bot=mentions,
    )


def private_event(text="在吗", sender="u1", name="阿花") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender,
        sender_id=sender,
        sender_name=name,
        text=text,
    )


class FixedRng(random.Random):
    def __init__(self, value: float):
        super().__init__()
        self.value = value

    def random(self):
        return self.value


# ----------------------------------------------------------------------
# 触发判断（细节见 test_decision.py）
# ----------------------------------------------------------------------


def test_private_always_replies(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.99))
    assert bot.should_reply(private_event()).should_reply


def test_mention_always_replies(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=FixedRng(0.99))
    assert bot.should_reply(group_event(mentions=True)).should_reply


def test_clean_group_turn_reaches_planner_without_model_call(config, store):
    llm = ScriptedLLM()
    bot, _ = make_bot(config, store, llm)
    decision = bot.should_reply(group_event(), now=1000.0)
    assert decision.should_reply and decision.reason == "planned"
    assert not llm.calls and not store.list_decision_logs()


def test_guard_releases_group_after_refractory_period(config, store):
    config.decision.refractory_seconds = 10
    config.decision.min_reply_interval_seconds = 0
    bot, _ = make_bot(config, store, ScriptedLLM())
    bot.decision_engine.note_bot_reply("recording:g1", 1000.0)
    assert bot.should_reply(group_event(), now=1005.0).reason == "refractory"
    assert bot.should_reply(group_event(), now=1010.0).reason == "planned"


# ----------------------------------------------------------------------
# 事件处理全流程
# ----------------------------------------------------------------------


def test_handle_event_records_and_replies(config, store):
    llm = ScriptedLLM({"chat": ["在呢在呢"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("在吗")))

    assert adapter.texts == ["在呢在呢"]
    # 人物与流水都落了
    assert store.get_person("recording:u1").display_name == "阿花"
    msgs = store.recent_messages("u1", platform="recording")
    assert msgs[0].text == "在吗"
    assert msgs[1].sender_id == BOT_SENDER_ID
    assert msgs[1].text == "在呢在呢"
    # bot 的消息不参与抽取
    assert store.pending_senders()[0].sender_id == "u1"


def test_group_silent_does_not_call_llm(config, store):
    config.attention.talk_value = 0.0
    llm = ScriptedLLM()
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(group_event()))
    assert adapter.sent == []
    assert llm.calls == []
    # 但消息仍然进流水
    assert store.recent_messages("g1")[0].text == "你好呀"


def test_private_scene_prompt_has_no_group_framing(config, store):
    """私聊上下文要明确说明只有两个人，且不能把这里描述成群聊。"""
    store.upsert_person("recording", "u1", "阿花")
    store.add_memory("recording:u1", "TA 最喜欢的歌手是坂本龙一")
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("在吗，最近在听坂本龙一")))

    system = llm.chat_messages[0][0]["content"]
    assert "当前是私聊" in system
    assert "两个人单独说话" in system
    assert "阿花" in system
    # 记忆区块在私聊里也要正常出现，且不能把私聊说成群聊
    assert "你记得的事" in system
    assert "坂本龙一" in system
    assert "这个群" not in system
    assert "当前是群聊" not in system


def test_group_scene_prompt_mentions_group(config, store):
    """群聊上下文要说明有很多人、回复所有人可见。"""
    store.upsert_person("recording", "u1", "阿花")
    llm = ScriptedLLM({"fast": ['{"basis":"settled","intent":"短附和","speak":true}'], "chat": ["嗯"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(group_event("有人吗")))

    system = llm.chat_messages[0][0]["content"]
    assert "当前是群聊" in system
    assert "很多人" in system
    assert "所有人" in system
    assert "阿花" in system
    # chat_id 对模型没有意义，不进提示词
    assert "当前是私聊" not in system


def test_history_and_memory_in_context(config, store):
    """历史以「[时间] [昵称] 正文」渲染，记忆与说话人信息都在系统提示里。"""
    store.upsert_person("recording", "u1", "阿花")
    store.add_memory("recording:u1", "TA 最喜欢的歌手是坂本龙一")
    config.memory.inject_limit = 5
    llm = ScriptedLLM({"chat": ["坂本龙一啊，我也喜欢"]})
    bot, _ = make_bot(config, store, llm)
    # 先放一条历史消息
    store.add_raw_message(platform="recording", chat_type="private", chat_id="u1",
                          sender_id="u1", sender_name="阿草", text="今天听歌吗")
    asyncio.run(bot.handle_event(private_event("最近在听坂本龙一")))

    messages = llm.chat_messages[0]
    system = messages[0]
    assert system["role"] == "system"
    assert "你是 Ruben" in system["content"]
    assert "坂本龙一" in system["content"]  # 注入的记忆
    assert "阿花" in system["content"]  # 说话人信息
    # 历史消息按「[HH:MM] [昵称] 正文」渲染
    assert any(re.match(r"\[\d{2}:\d{2}\] \[阿草\] 〔在场者原话·事实部分已定〕今天听歌吗$", m["content"]) for m in messages)
    assert re.match(r"\[\d{2}:\d{2}\] \[阿花\] 〔在场者原话·事实部分已定〕最近在听坂本龙一$", messages[-2]["content"])


def test_llm_failure_does_not_crash(config, store):
    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("模型崩了")

    bot, adapter = make_bot(config, store, BoomLLM())
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常
    assert adapter.sent == []


def test_adapter_send_failure_does_not_crash(config, store):
    class BrokenAdapter(RecordingAdapter):
        async def send(self, chat_id, segments, *, chat_type="group"):
            raise RuntimeError("发送失败")

    bot = RubenBot(config, store, ScriptedLLM({"chat": ["a"]}), Persona("Ruben", "你是 Ruben"))
    bot.register_adapter(BrokenAdapter())
    asyncio.run(bot.handle_event(private_event()))  # 不抛异常


# ----------------------------------------------------------------------
# 拆条
# ----------------------------------------------------------------------


def test_split_short_reply_kept(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM())
    assert bot.split_reply("好呀") == ["好呀"]


def test_split_newline_segments_into_parts(config, store):
    """生成契约用换行分段：换行处一定拆成多条。"""
    config.reply.max_segments = 3
    bot, _ = make_bot(config, store, ScriptedLLM(), rng=random.Random(0))
    parts = bot.split_reply("我卡得 PPT 一样\n卸载了 眼不见心不烦")
    # 换行一定拆；段内空格处按长度概率合并，用固定种子钉住结果
    assert parts[0] == "我卡得 PPT 一样"
    assert "".join(parts).replace(" ", "") == "我卡得PPT一样卸载了眼不见心不烦"


def test_split_respects_max_segments(config, store):
    config.reply.max_segments = 2
    bot, _ = make_bot(config, store, ScriptedLLM())
    parts = bot.split_reply("一\n二\n三\n四")
    assert len(parts) == 2


def test_send_reply_sends_parts(config, store):
    config.reply.max_segments = 3
    bot, adapter = make_bot(config, store, ScriptedLLM())
    asyncio.run(bot.send_reply(private_event(), "第一句话。\n第二句话。"))
    assert len(adapter.sent) == 2
    assert all(chat_type == "private" for _, chat_type, _ in adapter.sent)


# ----------------------------------------------------------------------
# 后台维护
# ----------------------------------------------------------------------


def test_maintenance_extracts_then_uses_memory(config, store):
    """端到端：发消息 → 落流水 → 抽取 → 再发消息时能引用记忆。"""
    extract_payload = json.dumps(
        [{"content": "TA 最喜欢的歌手是坂本龙一", "category": "preference", "scope": "global"}],
        ensure_ascii=False,
    )
    llm = ScriptedLLM({"chat": ["记得，你最喜欢坂本龙一"], "extract": [extract_payload]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("我最喜欢坂本龙一了")))
    assert store.count_memories("recording:u1") == 0

    asyncio.run(bot.run_maintenance(now=1e9, force_extract=True))
    assert store.count_memories("recording:u1") == 1

    asyncio.run(bot.handle_event(private_event("你记得我喜欢谁吗")))
    system = llm.chat_messages[-1][0]["content"]
    assert "坂本龙一" in system


def test_maintenance_error_contained(config, store):
    class BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("挂")

    bot, _ = make_bot(config, store, BoomLLM())
    store.add_raw_message(platform="recording", chat_id="u1", sender_id="u1", text="x")
    asyncio.run(bot.run_maintenance(force_extract=True))  # 不抛异常


# ----------------------------------------------------------------------
# 合并窗口与生成否决（端到端）
# ----------------------------------------------------------------------


def test_merge_window_combines_burst(config, store):
    """窗口内同一发送者连发多条，只决策/生成一次，上下文里看到合并文本。"""
    config.decision.merge_window_seconds = 4.0
    llm = ScriptedLLM({"fast": ['{"basis":"settled","intent":"短附和","speak":true}'], "chat": ["慢慢说"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(group_event("我跟你讲")))
    asyncio.run(bot.handle_event(group_event("今天遇到件事")))
    assert adapter.sent == []
    assert len(bot.merger) == 1
    asyncio.run(bot.flush_merger(now=time.time() + 10))
    assert adapter.texts == ["慢慢说"]
    assert len(llm.chat_messages) == 1
    # 两条碎片都在上下文里（流水逐条保存），模型看到的是完整的一段
    contents = [m["content"] for m in llm.chat_messages[0]]
    assert any("我跟你讲" in c for c in contents)
    assert any("今天遇到件事" in c for c in contents)


def test_merge_window_other_sender_joins_round(config, store):
    """别人插话时，整批一并裁定，不把两人的话算给同一人。"""
    config.decision.merge_window_seconds = 4.0
    llm = ScriptedLLM({"fast": ['{"basis":"settled","intent":"短附和","speak":true}'], "chat": ["收到"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(group_event("我先说", sender="u1")))
    assert adapter.sent == []
    event = group_event("我也来", sender="u2")
    asyncio.run(bot._handle_event(event))
    assert not llm.calls
    asyncio.run(bot.flush_merger(all_rounds=True))
    assert adapter.texts == ["收到"]
    assert len(llm.chat_messages) == 1
    assert any("我先说" in m["content"] for m in llm.chat_messages[0])


def test_generation_veto_keeps_silent(config, store):
    """进入正文生成后、模型输出否决标记时静默丢弃，不写 bot 流水。"""
    llm = ScriptedLLM({"chat": ["[不回复]"]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("随便说说")))
    assert adapter.sent == []
    assert llm.chat_messages  # 确实调用了模型
    assert not any(m.sender_id == BOT_SENDER_ID for m in store.recent_messages("u1"))


def test_prompt_tells_model_veto_marker(config, store):
    marker = config.decision.veto_marker
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("在吗")))
    system = llm.chat_messages[0][0]["content"]
    assert marker in system
    assert f"如果你只是默默看着，整条回复就是 {marker}" in system


def test_inbound_guard_does_not_retrieve_memories(config, store, monkeypatch):
    bot, _ = make_bot(config, store, ScriptedLLM())
    def unexpected(*args, **kwargs):
        raise AssertionError("护栏不应检索历史或话题相关性")
    monkeypatch.setattr(bot.retriever, "score", unexpected)
    monkeypatch.setattr(store, "recent_messages", unexpected)
    assert bot.decide(group_event()).reason == "planned"
    assert bot.decide(group_event(mentions=True)).reason == "mentioned"


def test_decision_logged(config, store, caplog):
    import logging

    config.attention.talk_value = 0.0
    llm = ScriptedLLM({"chat": ["嗯"]})
    bot, _ = make_bot(config, store, llm)
    with caplog.at_level(logging.INFO, logger="rubenbot.core"):
        asyncio.run(bot.handle_event(group_event()))
    assert "决策" in caplog.text
    assert "不回" in caplog.text
    assert "recording:g1" in bot.last_decision
    assert bot.last_decision["recording:g1"].should_reply is False


# ----------------------------------------------------------------------
# 适配器运行容错
# ----------------------------------------------------------------------


def test_adapter_crash_does_not_kill_run(config, store, monkeypatch):
    import rubenbot.core as core_module

    monkeypatch.setattr(core_module, "ADAPTER_RESTART_MIN", 0.02)

    class CrashAdapter(RecordingAdapter):
        def __init__(self):
            super().__init__()
            self.starts = 0

        async def start(self, on_event):
            self.starts += 1
            raise RuntimeError("连接失败")

    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"))
    adapter = CrashAdapter()
    bot.register_adapter(adapter)
    config.background_check_minutes = 0.01

    async def scenario():
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.1)
        bot.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert adapter.starts >= 2  # 至少重启过一次


def test_console_adapter_parse_and_send():
    import io

    out = io.StringIO()
    adapter = ConsoleAdapter(stdout=out)
    private = adapter.parse_line("你好")
    assert private.chat_type == "private"
    group = adapter.parse_line("[摸鱼群] 大家好")
    assert group.chat_type == "group"
    assert group.chat_id == "摸鱼群"
    assert group.text == "大家好"
    named = adapter.parse_line("[摸鱼群|阿花] 我来啦")
    assert named.sender_name == "阿花"
    # Console 给入站事件与出站回执都发本地递增 id
    assert named.platform_msg_id
    assert named.platform_msg_id != private.platform_msg_id
    asyncio.run(adapter.send("摸鱼群", [Segment.text("收到")]))
    assert "Ruben: 收到" in out.getvalue()


def test_console_adapter_send_returns_id():
    import io

    adapter = ConsoleAdapter(stdout=io.StringIO())
    first = asyncio.run(adapter.send("摸鱼群", [Segment.text("收到")]))
    second = asyncio.run(adapter.send("摸鱼群", [Segment.text("再来")]))
    assert first and second and first != second


def test_decision_written_to_store_log(config, store):
    """每次决策都要落 decision_log，WebUI 的「最近决策」读它。"""
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))
    asyncio.run(bot.handle_event(private_event("在吗")))
    logs = store.list_decision_logs()
    assert len(logs) == 1
    entry = logs[0]
    assert entry["chat_id"] == "u1"
    assert entry["sender_name"] == "阿花"
    assert entry["should_reply"] is True
    assert entry["hard_trigger"] == "private"


def test_group_decision_log_has_factors(config, store):
    bot, _ = make_bot(config, store, ScriptedLLM({"fast": ['{"basis":"guess","intent":"提问","increment":"问地点"}']}))
    asyncio.run(bot.handle_event(group_event()))
    entry = store.list_decision_logs(chat_id="g1")[0]
    assert entry["should_reply"] is False
    assert entry["reason"] == "planner_silence"
    planner = entry["factors"]["planner"]
    assert {key: planner[key] for key in ("basis", "speak", "reason", "result_code")} == {
        "basis": "guess", "speak": False, "reason": "", "result_code": "missing_reason",
    }
    assert entry["score"] == entry["threshold"] == 0


# ----------------------------------------------------------------------
# LLM 调用打标与用量落库（真实客户端 + MockTransport）
# ----------------------------------------------------------------------


def test_llm_client_records_usage_with_task_labels(config, store):
    """主循环、抽取、归纳都走同一个客户端，各自的任务名都要落进用量表。"""
    import httpx

    from rubenbot.llm import LLMClient, make_usage_recorder

    tasks = []

    def handler(request):
        body = json.loads(request.content)
        # 抽取/归纳请求 JSON 模式；按 user 内容区分任务
        text = body["messages"][-1]["content"]
        if "JSON" in text or body.get("response_format"):
            tasks.append("json")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好的" if not body.get("response_format") else "[]"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    client = LLMClient(config.llm, transport=httpx.MockTransport(handler), usage_recorder=make_usage_recorder(store), retry_delays=(0.0,))
    bot, _ = make_bot(config, store, client)

    # 私聊必回 → chat 任务
    asyncio.run(bot.handle_event(private_event("在吗")))
    # 此例只验抽取费用；新入站事件本来就是画像维护的原料，不触发到期的画像作业。
    store.set_meta('distill:last_at', str(time.time()))
    # 同一私聊批次只花一次抽取费用；同号群聊是另一批，不混作同一会话。
    store.add_raw_message(platform="recording", chat_type="private", chat_id="u1", sender_id="u1",
                          sender_name="阿花", text="我住在杭州")
    asyncio.run(bot.extractor.maybe_extract(force=True))
    asyncio.run(client.close())

    by_task = {item["task"]: item for item in store.usage_by_task()}
    assert "gen_stage1" in by_task and "gen_stage2" in by_task
    assert "extract" in by_task
    assert by_task["gen_stage2"]["total_tokens"] == 7
    assert by_task["extract"]["total_tokens"] == 7
    assert store.usage_totals()["calls"] == 3
