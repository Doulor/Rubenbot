"""qzone_post 插件：命令触发、定时触发、能力不可用时的回复。

用假 capability 入口（replace bot.call_capability）与假 LLM，不碰网络。
"""

import asyncio
import time
from pathlib import Path

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona
from rubenbot.plugins.loader import PluginManager

from conftest import RecordingAdapter, ScriptedLLM

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def write_plugin(tmp_path: Path, config_yaml: str = "") -> Path:
    """把真实插件目录拷到 tmp（带上可选的 config.yaml）。"""
    root = tmp_path / "plugins"
    plugin_dir = root / "qzone_post"
    plugin_dir.mkdir(parents=True)
    src = PLUGIN_DIR / "qzone_post" / "plugin.py"
    (plugin_dir / "plugin.py").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if config_yaml:
        (plugin_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")
    return root


class FakeCapability:
    """替换 bot.call_capability：记录调用，可指定成功/失败。"""

    def __init__(self, ok: bool = True, reason: str = "ok: 1"):
        self.ok = ok
        self.reason = reason
        self.calls: list[tuple[str | None, str, dict]] = []

    async def __call__(self, adapter_name, name, **kwargs):
        self.calls.append((adapter_name, name, kwargs))
        return self.ok, self.reason


def make_bot(tmp_path, plugin_yaml="", *, llm_replies=None, capability=None):
    root = write_plugin(tmp_path, plugin_yaml)
    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(root)
    config.chat.split_delay_seconds = 0
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, store)
    manager.load_all()
    llm = ScriptedLLM({"chat": llm_replies or []}, default="今天有点困")
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben，一个普通上班族"), plugins=manager)
    manager.extras["bot"] = bot  # 宿主启动时注入；这里提前注入，定时路径不必等第一条消息
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    cap = capability or FakeCapability()
    bot.call_capability = cap
    return bot, adapter, manager, llm, cap, store


def private(text: str, sender_id: str = "u1") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type="private",
        chat_id=sender_id,
        sender_id=sender_id,
        sender_name="阿花",
        text=text,
    )


# ----------------------------------------------------------------------
# 命令路径
# ----------------------------------------------------------------------


def test_command_with_content_publishes_directly(tmp_path):
    bot, adapter, _, llm, cap, _ = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("/说说 今天风好大")))

    assert cap.calls == [(None, "qzone.publish", {"content": "今天风好大"})]
    assert llm.calls == []  # 有内容就不打扰模型
    assert adapter.texts == ["说说已经发了：今天风好大"]


def test_command_without_content_generates_then_publishes(tmp_path):
    bot, adapter, _, llm, cap, _ = make_bot(tmp_path, llm_replies=["「 下雨了，正好不用出门 」"])
    asyncio.run(bot.handle_event(private("/说说")))

    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "Ruben" in system_prompt  # 带人格
    assert "60" in system_prompt  # 有字数要求
    # 引号被剥掉，发布的是干净正文
    assert cap.calls == [(None, "qzone.publish", {"content": "下雨了，正好不用出门"})]
    assert adapter.texts == ["说说已经发了：下雨了，正好不用出门"]


def test_command_capability_unavailable_replies_reason(tmp_path):
    cap = FakeCapability(ok=False, reason="适配器 snowluma 的能力 qzone.publish 已在配置中关闭")
    bot, adapter, _, _, _, _ = make_bot(tmp_path, capability=cap)
    asyncio.run(bot.handle_event(private("/说说 测试")))

    assert len(cap.calls) == 1
    assert adapter.texts == ["发不了说说：适配器 snowluma 的能力 qzone.publish 已在配置中关闭"]


def test_command_disabled_capability_reason_from_real_gate(tmp_path):
    """走核心真实能力入口：没有适配器声明该能力时给出「不支持」原因。"""
    root = write_plugin(tmp_path)
    store = MemoryStore(":memory:")
    config = Config()
    config.plugins.dir = str(root)
    config.chat.split_delay_seconds = 0
    config.decision.merge_window_seconds = 0
    config.decision.hard_merge_window_seconds = 0
    manager = PluginManager(config, store)
    manager.load_all()
    bot = RubenBot(config, store, ScriptedLLM(), Persona("Ruben", "你是 Ruben"), plugins=manager)
    adapter = RecordingAdapter()  # 不声明任何能力
    bot.register_adapter(adapter)

    asyncio.run(bot.handle_event(private("/说说 测试")))
    assert adapter.texts[0].startswith("发不了说说：")
    assert "不支持" in adapter.texts[0]


def test_command_owner_only_blocks_strangers(tmp_path):
    cfg = "owner_only: true\nowner_ids: ['boss']\n"
    bot, adapter, _, _, cap, _ = make_bot(tmp_path, cfg)
    asyncio.run(bot.handle_event(private("/说说 你好", sender_id="u1")))
    assert cap.calls == []
    assert adapter.texts == ["只有主人能让我发说说"]

    asyncio.run(bot.handle_event(private("/说说 你好", sender_id="boss")))
    assert cap.calls == [(None, "qzone.publish", {"content": "你好"})]


def test_custom_command_name(tmp_path):
    bot, adapter, _, _, cap, _ = make_bot(tmp_path, 'command: "qzone"\n')
    asyncio.run(bot.handle_event(private("/qzone 换个名字")))
    assert cap.calls == [(None, "qzone.publish", {"content": "换个名字"})]


def test_generated_content_is_truncated(tmp_path):
    long = "今天真的特别特别困，早上闹钟响了三次都没起来，最后差点迟到，好在路上没堵车" * 2
    bot, adapter, _, _, cap, _ = make_bot(tmp_path, llm_replies=[long])
    asyncio.run(bot.handle_event(private("/说说")))
    content = cap.calls[0][2]["content"]
    assert 0 < len(content) <= 60


# ----------------------------------------------------------------------
# 定时路径
# ----------------------------------------------------------------------


def test_interval_zero_starts_no_task(tmp_path):
    bot, adapter, manager, llm, cap, _ = make_bot(tmp_path)
    asyncio.run(bot.handle_event(private("随便聊聊")))
    plugin = manager.extras["qzone_post"]
    assert plugin.task is None
    assert cap.calls == []


def test_interval_path_publishes_with_topic(tmp_path):
    bot, adapter, manager, llm, cap, store = make_bot(
        tmp_path, "interval_hours: 1\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=["摸鱼的一下午"]
    )
    now = time.time()
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="1001",
        sender_name="老周",
        text="今天加班到十点",
        ts=now,
    )
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="1002",
        sender_name="阿花",
        text="那你还来水群",
        ts=now + 0.1,
    )
    plugin = manager.extras["qzone_post"]

    ok, _ = asyncio.run(plugin.publish_scheduled(now=now + 1))
    assert ok is True
    assert cap.calls == [(None, "qzone.publish", {"content": "摸鱼的一下午"})]
    # 群聊热点被当作题材喂给模型
    topic_message = llm.calls[0]["messages"][1]["content"]
    assert "今天加班到十点" in topic_message


def test_interval_path_generation_failure_skips_publish(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, "interval_hours: 1\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=[""])
    plugin = manager.extras["qzone_post"]
    ok, reason = asyncio.run(plugin.publish_scheduled())
    assert ok is False
    assert reason == "没能生成内容"
    assert cap.calls == []


def test_interval_path_capability_failure_contained(tmp_path):
    cap = FakeCapability(ok=False, reason="网络炸了")
    bot, adapter, manager, _, _, _ = make_bot(tmp_path, "interval_hours: 1\nquiet_hours: \"\"\ndraft_confirm: false\n", capability=cap)
    plugin = manager.extras["qzone_post"]
    ok, detail = asyncio.run(plugin.publish_scheduled())
    assert ok is False and detail == "网络炸了"


def test_interval_loop_posts_and_exits_on_stop(tmp_path, monkeypatch):
    """把间隔缩到几乎为 0，验证后台任务真的会发；停用后干净退出。"""
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, "interval_hours: 0.0001\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=["夜里的小情绪"]
    )

    async def scenario():
        await bot.handle_event(private("起床"))
        plugin = manager.extras["qzone_post"]
        assert plugin.task is not None
        # 等任务至少跑一轮
        for _ in range(100):
            if cap.calls:
                break
            await asyncio.sleep(0.01)
        assert cap.calls, "定时任务没有发布"
        await plugin.stop()
        assert plugin.task is None

    asyncio.run(scenario())


def test_interval_loop_survives_exceptions(tmp_path):
    """单轮异常不能让任务死掉，下一轮继续。"""
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, "interval_hours: 0.0001\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=["第一条", "第二条", "第三条"]
    )

    async def scenario():
        await bot.handle_event(private("起床"))
        plugin = manager.extras["qzone_post"]

        calls = {"n": 0}
        original = plugin.publish_scheduled

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("第一轮炸了")
            return await original()

        plugin.publish_scheduled = flaky
        for _ in range(200):
            if cap.calls:
                break
            await asyncio.sleep(0.01)
        assert calls["n"] >= 2
        assert cap.calls, "第一轮异常后任务应当继续并成功发布"
        await plugin.stop()

    asyncio.run(scenario())


def test_disabled_plugin_task_exits(tmp_path):
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, "interval_hours: 0.0001\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=["x"]
    )

    async def scenario():
        await bot.handle_event(private("起床"))
        plugin = manager.extras["qzone_post"]
        task = plugin.task
        assert task is not None
        manager.set_enabled("qzone_post", False)
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.done()

    asyncio.run(scenario())


def test_bot_stop_event_exits_task(tmp_path):
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, "interval_hours: 100\nquiet_hours: \"\"\ndraft_confirm: false\n", llm_replies=["x"]
    )

    async def scenario():
        await bot.handle_event(private("起床"))
        plugin = manager.extras["qzone_post"]
        task = plugin.task
        assert task is not None and not task.done()
        bot.stop()
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.done()
        assert cap.calls == []

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 概率触发 / LLM 触发
# ----------------------------------------------------------------------


class FixedRng:
    """固定随机源：random() 返回 value，uniform() 返回 jitter。"""

    def __init__(self, value: float = 0.0, jitter: float = 0.0):
        self.value = value
        self.jitter = jitter
        self.uniform_calls: list[tuple[float, float]] = []

    def random(self) -> float:
        return self.value

    def uniform(self, a: float, b: float) -> float:
        self.uniform_calls.append((a, b))
        return self.jitter


AUTO_BASE = 'quiet_hours: ""\ndraft_confirm: false\ncooldown_minutes: 0\ndaily_cap: 0\n'


def test_probability_gate_blocks_when_chance_not_hit(tmp_path):
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, AUTO_BASE + "prob: true\npost_chance: 0.3\n", llm_replies=["该发的说说"]
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.9)  # 0.9 > 0.3：没过概率门

    assert asyncio.run(plugin.on_opportunity()) is None
    assert asyncio.run(plugin.run_tick()) is None
    assert cap.calls == []
    assert llm.calls == []  # 概率门在生成之前，不浪费模型调用


def test_probability_zero_never_posts(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, AUTO_BASE + "prob: true\npost_chance: 0\n", llm_replies=["不该发"]
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.0)  # 即使随机数最小也不发
    assert asyncio.run(plugin.on_opportunity()) is None
    assert cap.calls == []


def test_probability_gate_posts_when_hit(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, AUTO_BASE + "prob: true\npost_chance: 0.8\n", llm_replies=["今天的云像棉花"]
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.2)
    ok, _ = asyncio.run(plugin.run_tick())
    assert ok is True
    assert cap.calls == [(None, "qzone.publish", {"content": "今天的云像棉花"})]


def test_llm_decide_false_does_not_post(tmp_path):
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path,
        AUTO_BASE + "llm_decide: true\n",
        llm_replies=['{"post": false, "content": ""}'],
    )
    plugin = manager.extras["qzone_post"]
    assert asyncio.run(plugin.run_tick()) is None
    assert cap.calls == []
    # 模型被问了一次，且要求结构化 JSON
    assert llm.calls[0]["json_mode"] is True
    assert "post" in llm.calls[0]["messages"][-1]["content"]


def test_llm_decide_true_posts_model_content(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path,
        AUTO_BASE + "llm_decide: true\nllm_then_prob: false\n",
        llm_replies=['{"post": true, "content": "「楼下的桂花开了」"}'],
    )
    plugin = manager.extras["qzone_post"]
    ok, _ = asyncio.run(plugin.run_tick())
    assert ok is True
    assert cap.calls == [(None, "qzone.publish", {"content": "楼下的桂花开了"})]


def test_llm_then_prob_gate(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path,
        AUTO_BASE + "llm_decide: true\nllm_then_prob: true\npost_chance: 0.3\n",
        llm_replies=[
            '{"post": true, "content": "想发的一条"}',
            '{"post": true, "content": "想发的另一条"}',
        ],
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.9)  # 模型说发，但没过概率门
    assert asyncio.run(plugin.run_tick()) is None
    assert cap.calls == []

    plugin.rng = FixedRng(0.1)  # 这次过了
    ok, _ = asyncio.run(plugin.run_tick())
    assert ok is True
    assert cap.calls == [(None, "qzone.publish", {"content": "想发的另一条"})]


def test_llm_decide_garbage_treated_as_no_post(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, AUTO_BASE + "llm_decide: true\n", llm_replies=["我觉得今天还行"]
    )
    plugin = manager.extras["qzone_post"]
    assert asyncio.run(plugin.run_tick()) is None
    assert cap.calls == []


def test_opportunity_after_bot_reply_posts(tmp_path):
    """机会事件真的挂在 on_after_reply 上：bot 回完一轮后按概率发说说。"""
    cfg = AUTO_BASE + "prob: true\npost_chance: 1.0\n"
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, cfg, llm_replies=["回你的话", "回完想发的一条"]
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.0)
    asyncio.run(bot.handle_event(private("在吗")))

    assert adapter.texts[0] == "回你的话"  # 先正常回消息
    assert cap.calls == [(None, "qzone.publish", {"content": "回完想发的一条"})]


def test_llm_decide_prompt_has_persona_and_topic(tmp_path):
    """LLM 触发把人格和最近群聊热点喂给模型。"""
    bot, adapter, manager, llm, cap, store = make_bot(
        tmp_path, AUTO_BASE + "llm_decide: true\n", llm_replies=['{"post": false, "content": ""}']
    )
    now = time.time()
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="1001",
        sender_name="老周",
        text="周末去爬山",
        ts=now,
    )
    store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="1002",
        sender_name="阿花",
        text="带我一起",
        ts=now + 0.1,
    )
    plugin = manager.extras["qzone_post"]
    asyncio.run(plugin.run_tick(now=now + 1))

    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "Ruben" in system_prompt
    topic = llm.calls[0]["messages"][1]["content"]
    assert "周末去爬山" in topic


# ----------------------------------------------------------------------
# 节制三件套
# ----------------------------------------------------------------------


def test_quiet_hours_block_auto_post(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'quiet_hours: "00:00-23:59"\ndraft_confirm: false\n'
    )
    plugin = manager.extras["qzone_post"]
    ok, reason = asyncio.run(plugin.auto_post(source="times", content="半夜的碎碎念"))
    assert ok is False
    assert "安静时段" in reason
    assert cap.calls == []


def test_cooldown_blocks_auto_post(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path,
        'quiet_hours: ""\ndraft_confirm: false\ncooldown_minutes: 480\ndaily_cap: 0\n',
    )
    plugin = manager.extras["qzone_post"]
    now = time.time()
    plugin._last_post_ts = now - 60  # 1 分钟前刚发过
    ok, reason = asyncio.run(plugin.auto_post(source="prob", content="再来一条", now=now))
    assert ok is False
    assert "冷却" in reason
    assert cap.calls == []

    # 冷却过去后可以发
    ok, _ = asyncio.run(
        plugin.auto_post(source="prob", content="终于过了冷却", now=now + 480 * 60)
    )
    assert ok is True
    assert cap.calls == [(None, "qzone.publish", {"content": "终于过了冷却"})]


def test_daily_cap_blocks_auto_post(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'quiet_hours: ""\ndraft_confirm: false\ncooldown_minutes: 0\ndaily_cap: 2\n'
    )
    plugin = manager.extras["qzone_post"]
    now = time.time()
    plugin._daily_date = time.strftime("%Y-%m-%d", time.localtime(now))
    plugin._daily_count = 2

    ok, reason = asyncio.run(plugin.auto_post(source="interval", content="第三条", now=now))
    assert ok is False
    assert "上限" in reason
    assert cap.calls == []


def test_command_bypasses_limits_and_draft(tmp_path):
    """命令是明确指令：不走草稿，也不受节制三件套限制。"""
    cfg = 'quiet_hours: "00:00-23:59"\ndraft_confirm: true\ncooldown_minutes: 480\ndaily_cap: 0\n'
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, cfg)
    plugin = manager.extras["qzone_post"]
    plugin._last_post_ts = time.time()
    asyncio.run(bot.handle_event(private("/说说 主人亲口要发的")))
    assert cap.calls == [(None, "qzone.publish", {"content": "主人亲口要发的"})]
    assert plugin.pending_draft is None


def test_bad_quiet_hours_disables_quiet_time():
    module = _load_module()
    cfg = module.config_from_dict({"quiet_hours": "25:00-30:00"})
    assert cfg.quiet_hours == ""  # 非法时段整体关闭，不误伤全天


def test_draft_timer_cancelled_on_stop(tmp_path):
    """插件停用时草稿计时器一并收掉，不留悬挂任务。"""
    bot, adapter, manager, _, _, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]

    async def scenario():
        await plugin.auto_post(source="times", content="停了就别再惦记")
        timer = plugin._draft_timer
        assert timer is not None and not timer.done()
        try:
            await plugin.stop()
            assert timer.done()
            assert plugin.pending_draft is None
        finally:
            await manager_settle(plugin)

    asyncio.run(scenario())


def test_daily_cap_resets_next_day():
    module = _load_module()
    plugin = _bare_plugin(
        module, module.QzonePostConfig(daily_cap=2, quiet_hours="", cooldown_minutes=0)
    )
    now = time.time()
    plugin._note_published(now)
    plugin._note_published(now + 1)
    assert plugin._block_reason(now + 2) == "今天的自动说说已经到上限了"
    # 第二天计数清零
    assert plugin._block_reason(now + 86400) is None
    assert plugin._daily_count == 0


# ----------------------------------------------------------------------
# 草稿确认
# ----------------------------------------------------------------------


def test_draft_confirm_defaults_on_and_sends_private(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]
    assert plugin.config.draft_confirm is True
    ok, detail = asyncio.run(plugin.auto_post(source="times", content="今天的草稿"))
    assert ok is True
    assert "已发给主人确认" in detail
    assert cap.calls == []  # 还没真发
    draft = plugin.pending_draft
    assert draft is not None and draft.content == "今天的草稿"
    assert adapter.sent[0][0] == "boss"
    assert adapter.sent[0][1] == "private"
    assert "回「发」" in adapter.texts[0]


def test_draft_approval_publishes(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]
    asyncio.run(plugin.auto_post(source="times", content="主人确认了这条"))
    assert len(adapter.sent) == 1

    asyncio.run(bot.handle_event(private("发", sender_id="boss")))
    assert plugin.pending_draft is None
    assert cap.calls == [(None, "qzone.publish", {"content": "主人确认了这条"})]
    assert "发好了：主人确认了这条" in adapter.texts[-1]


def test_draft_discard_does_not_publish(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]
    asyncio.run(plugin.auto_post(source="times", content="这条不要了"))
    asyncio.run(bot.handle_event(private("算了", sender_id="boss")))

    assert plugin.pending_draft is None
    assert cap.calls == []
    assert adapter.texts[-1] == "好，这条不发了。"


def test_draft_ignores_non_owner_and_group(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]
    asyncio.run(plugin.auto_post(source="times", content="只给主人确认"))
    # 群消息、别人的私聊都不算确认
    group = MessageEvent(
        platform="recording",
        chat_type="group",
        chat_id="g1",
        sender_id="boss",
        sender_name="老板",
        text="发",
    )
    asyncio.run(bot.handle_event(group))
    asyncio.run(bot.handle_event(private("发", sender_id="stranger")))
    assert plugin.pending_draft is not None
    assert cap.calls == []

    asyncio.run(bot.handle_event(private("发", sender_id="boss")))
    assert cap.calls == [(None, "qzone.publish", {"content": "只给主人确认"})]


def test_draft_timeout_discards(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\ndraft_timeout_minutes: 30\n'
    )
    plugin = manager.extras["qzone_post"]
    now = time.time()
    asyncio.run(plugin.auto_post(source="times", content="放太久了", now=now))
    draft = plugin.pending_draft
    assert draft is not None and draft.expires_at == now + 30 * 60

    assert asyncio.run(plugin.check_draft_timeout(now=now + 29 * 60)) is False
    assert plugin.pending_draft is not None
    assert asyncio.run(plugin.check_draft_timeout(now=now + 31 * 60)) is True
    assert plugin.pending_draft is None
    assert cap.calls == []
    assert "作废了" in adapter.texts[-1]


def test_draft_blocks_further_auto_posts_until_resolved(tmp_path):
    bot, adapter, manager, _, cap, _ = make_bot(
        tmp_path, 'owner_ids: ["boss"]\nquiet_hours: ""\n'
    )
    plugin = manager.extras["qzone_post"]
    asyncio.run(plugin.auto_post(source="times", content="第一条草稿"))
    ok, reason = asyncio.run(plugin.auto_post(source="prob", content="第二条"))
    assert ok is False
    assert "草稿" in reason
    assert len(adapter.sent) == 1


def test_draft_falls_back_to_first_private_chat(tmp_path, caplog):
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, 'quiet_hours: ""\n')
    store = manager.store
    store.add_raw_message(
        platform="recording",
        chat_id="someone",
        chat_type="private",
        sender_id="someone",
        sender_name="阿花",
        text="在吗",
    )
    plugin = manager.extras["qzone_post"]
    with caplog.at_level("INFO"):
        ok, _ = asyncio.run(plugin.auto_post(source="times", content="没有主人的草稿"))
    assert ok is True
    assert adapter.sent[0][0] == "someone"
    assert any("owner_ids 未配置" in record.message for record in caplog.records)


# ----------------------------------------------------------------------
# 时间点定时与抖动
# ----------------------------------------------------------------------


def _local_ts(day_offset: int, hour: int, minute: int) -> float:
    import datetime as dt

    day = dt.date.today() + dt.timedelta(days=day_offset)
    return dt.datetime.combine(day, dt.time(hour, minute)).timestamp()


def test_times_next_target_applies_jitter(tmp_path):
    module = _load_module()
    plugin = _bare_plugin(
        module, module.QzonePostConfig(schedule_mode="times", times=["08:30"], jitter_minutes=10)
    )
    plugin.rng = FixedRng(jitter=10.0)  # 抖动拉满：+10 分钟
    now = _local_ts(0, 7, 0)
    target = plugin._next_time_target(now)
    assert target is not None
    fire_at, slot = target
    assert slot == f"{__import__('time').strftime('%Y-%m-%d', __import__('time').localtime(now))} 08:30"
    assert fire_at == _local_ts(0, 8, 30) + 10 * 60

    # 触发过的槽位不再重复
    plugin._fired_slots[slot] = now
    fire_at, slot = plugin._next_time_target(now)
    assert slot.endswith("08:30")
    assert fire_at >= _local_ts(1, 8, 30) - 10 * 60


def test_times_without_jitter(tmp_path):
    module = _load_module()
    plugin = _bare_plugin(
        module, module.QzonePostConfig(schedule_mode="times", times=["21:00"], jitter_minutes=0)
    )
    plugin.rng = FixedRng()
    now = _local_ts(0, 20, 0)
    fire_at, _ = plugin._next_time_target(now)
    assert fire_at == _local_ts(0, 21, 0)


def test_times_fired_slot_not_repeated(tmp_path):
    """触发过的槽位不再排第二次，顺延到明天的同一时间点。"""
    cfg = 'schedule_mode: times\ntimes: ["08:30"]\njitter_minutes: 0\n'
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, cfg)
    plugin = manager.extras["qzone_post"]
    now = _local_ts(0, 7, 0)
    fire_at, slot = plugin._next_time_target(now)
    assert slot.endswith("08:30") and fire_at == _local_ts(0, 8, 30)

    plugin._fired_slots[slot] = time.time()
    fire_at, next_slot = plugin._next_time_target(now)
    assert next_slot != slot
    assert next_slot.endswith("08:30") and fire_at >= _local_ts(1, 8, 30) - 1


def test_times_loop_fires_and_marks_slot(tmp_path):
    """时间点循环到点后走自动流程，并把槽位记下（同一槽位只发一次）。"""
    cfg = (
        'schedule_mode: times\ntimes: ["08:30"]\njitter_minutes: 0\n'
        'quiet_hours: ""\ndraft_confirm: false\ndaily_cap: 0\ncooldown_minutes: 0\n'
    )
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, cfg, llm_replies=["早上的说说"])
    plugin = manager.extras["qzone_post"]

    async def scenario():
        pending = [(time.time() + 0.05, "2026-01-01 08:30")]
        plugin._next_time_target = lambda now: pending.pop(0) if pending else None
        stop_event = asyncio.Event()
        await asyncio.wait_for(plugin._times_loop(stop_event), timeout=3)
        assert cap.calls == [(None, "qzone.publish", {"content": "早上的说说"})]
        assert "2026-01-01 08:30" in plugin._fired_slots

    asyncio.run(scenario())


def test_schedule_modes_of_times_and_interval(tmp_path):
    module = _load_module()
    # times 模式不拉起 interval 任务
    cfg = module.config_from_dict({"schedule_mode": "times", "times": ["08:30"]})
    plugin = _bare_plugin(module, cfg)
    assert plugin._want_times() is True
    assert plugin._want_interval() is False
    # both 模式两者都开
    cfg = module.config_from_dict(
        {"schedule_mode": "both", "times": ["08:30"], "interval_hours": 6}
    )
    plugin.config = cfg
    assert plugin._want_times() is True and plugin._want_interval() is True
    # interval 模式忽略 times
    cfg = module.config_from_dict({"schedule_mode": "interval", "times": ["08:30"]})
    plugin.config = cfg
    assert plugin._want_times() is False


# ----------------------------------------------------------------------
# 配置与工具
# ----------------------------------------------------------------------


def test_config_defaults_and_override(tmp_path):
    module = _load_module()
    assert module.QzonePostConfig().interval_hours == 0
    assert module.QzonePostConfig().command == "说说"
    cfg = module.config_from_dict(
        {"interval_hours": "2.5", "owner_only": "yes", "owner_ids": "1001", "command": "/空间"}
    )
    assert cfg.interval_hours == 2.5
    assert cfg.owner_only is False  # 只认真布尔
    assert cfg.owner_ids == ["1001"]
    assert cfg.command == "空间"  # 前缀被去掉


def test_config_bad_values_fall_back():
    module = _load_module()
    cfg = module.config_from_dict({"interval_hours": "abc", "max_chars": None})
    assert cfg.interval_hours == 0
    assert cfg.max_chars == 60


def test_clean_generated():
    module = _load_module()
    assert module.clean_generated("  「今天好困」  ") == "今天好困"
    assert module.clean_generated("```\n下雨了\n```") == "下雨了"
    assert module.clean_generated("第一句。第二句。" + "长" * 100).startswith("第一句。")
    assert len(module.clean_generated("长" * 200)) == 60


async def manager_settle(plugin) -> None:
    """停用后如果还有自动任务在跑，补一次 stop，避免测试间残留任务。"""
    if plugin.tasks:
        await plugin.stop()


def _bare_plugin(module, config):
    """不进生命周期、只带内存状态的插件实例，供纯逻辑用例使用。"""
    plugin = object.__new__(module.QzonePostPlugin)
    plugin.config = config
    plugin._draft = None
    plugin._draft_timer = None
    plugin._last_post_ts = None
    plugin._daily_date = ""
    plugin._daily_count = 0
    plugin._fired_slots = {}
    plugin._slot_offsets = {}
    plugin.rng = FixedRng()
    return plugin


def _load_module():
    """按 loader 的方式加载插件模块（独立模块名，不污染其它用例）。"""
    import importlib.util
    import sys

    name = "rubenbot_plugin_qzone_post_test"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "qzone_post" / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# 空间互动：巡检 / 点赞 / 评论 / 回复
# ----------------------------------------------------------------------


class InteractCapability:
    """互动用的假能力入口：假说说流、假评论列表，写操作只记录调用。

    ``bot.call_capability`` 被换成它；``feeds`` 按 uin 返回预设列表，
    ``my_comments`` 返回预设评论；其余能力一律成功（除非在 ``fail_for`` 里）。
    """

    def __init__(self):
        self.calls: list[tuple[str | None, str, dict]] = []
        self.feeds: dict[str, list[dict]] = {}
        self.comments: list[dict] = []
        self.fail_for: dict[str, str] = {}

    def add_feed(self, uin: str, tid: str, created: float, content: str = "今天的云很好看"):
        self.feeds.setdefault(uin, []).append(
            {"tid": tid, "uin": uin, "created_time": int(created), "content": content}
        )

    async def __call__(self, adapter_name, name, **kwargs):
        self.calls.append((adapter_name, name, kwargs))
        if name in self.fail_for:
            return False, self.fail_for[name]
        if name == "qzone.feeds":
            uin = str(kwargs.get("uin") or "")
            return True, list(self.feeds.get(uin, []))
        if name == "qzone.my_comments":
            return True, list(self.comments)
        return True, "ok: 1"

    def names(self) -> list[str]:
        return [name for _, name, _ in self.calls]

    def calls_of(self, name: str) -> list[dict]:
        return [kwargs for _, cap, kwargs in self.calls if cap == name]


INTERACT_BASE = (
    'quiet_hours: ""\n'
    'interact_uins: ["20001"]\n'
    'interact_days: 3\n'
    'interact_backoff_minutes: 0\n'
)


def make_interact_bot(tmp_path, extra_yaml="", *, replies=None):
    cap = InteractCapability()
    bot, adapter, manager, llm, _, _ = make_bot(
        tmp_path, INTERACT_BASE + extra_yaml, llm_replies=replies or [], capability=cap
    )
    plugin = manager.extras["qzone_post"]
    plugin.rng = FixedRng(0.0)  # 概率门默认全过（0.0 < 任何 >0 的概率）
    return bot, manager, plugin, llm, cap


LIKE_ONLY = 'like_enabled: true\nlike_chance: 1.0\nlike_cap_per_round: 5\n'
COMMENT_ONLY = (
    'comment_enabled: true\ncomment_chance: 1.0\n'
    'comment_cap_per_round: 5\ncomment_max_chars: 30\n'
)
REPLY_ONLY = 'reply_enabled: true\nreply_cap_per_round: 3\nreply_cap_per_post: 3\n'


def test_interact_only_polls_whitelisted_uins(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    now = time.time()
    cap.add_feed("20001", "t-1", now - 600)
    cap.add_feed("29999", "t-other", now - 600)  # 非白名单

    report = asyncio.run(plugin.run_interact(now=now))

    assert [call["uin"] for call in cap.calls_of("qzone.feeds")] == ["20001"]
    assert [call["tid"] for call in cap.calls_of("qzone.like")] == ["t-1"]
    assert report.liked == 1
    assert "29999" not in str(cap.calls)


def test_interact_without_whitelist_does_nothing(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(
        tmp_path, LIKE_ONLY + 'interact_uins: []\n'
    )
    report = asyncio.run(plugin.run_interact(now=time.time()))
    assert cap.calls == []
    assert "白名单" in report.skipped


def test_interact_skips_feeds_outside_window(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    now = time.time()
    cap.add_feed("20001", "old", now - 4 * 86400)  # 4 天前，窗口外
    cap.add_feed("20001", "fresh", now - 3600)

    report = asyncio.run(plugin.run_interact(now=now))

    assert [call["tid"] for call in cap.calls_of("qzone.like")] == ["fresh"]
    assert report.liked == 1


def test_interact_like_not_repeated_across_rounds(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)

    asyncio.run(plugin.run_interact(now=now))
    asyncio.run(plugin.run_interact(now=now + 3600))

    assert len(cap.calls_of("qzone.like")) == 1
    # 去重状态落了插件数据文件
    state_path = Path(manager.plugins["qzone_post"].path) / "interact_state.json"
    assert state_path.exists()
    assert "t-1" in state_path.read_text(encoding="utf-8")
    assert "t-1" in plugin.interact_state.tids["liked_tids"]


def test_interact_skips_feeds_already_liked_by_us(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    now = time.time()
    cap.feeds["20001"] = [
        {"tid": "t-liked", "uin": "20001", "created_time": int(now - 60), "is_liked": True},
        {"tid": "t-new", "uin": "20001", "created_time": int(now - 50), "is_liked": False},
    ]

    asyncio.run(plugin.run_interact(now=now))

    assert [c["tid"] for c in cap.calls_of("qzone.like")] == ["t-new"]


def test_interact_like_chance_zero_never_likes(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(
        tmp_path, 'like_enabled: true\nlike_chance: 0\n'
    )
    plug_rng = FixedRng(0.0)
    plugin.rng = plug_rng
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)

    asyncio.run(plugin.run_interact(now=now))

    assert cap.calls_of("qzone.like") == []
    assert cap.names() == ["qzone.feeds"]


def test_interact_like_per_round_cap(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(
        tmp_path, 'like_enabled: true\nlike_chance: 1.0\nlike_cap_per_round: 1\n'
    )
    now = time.time()
    for index in range(3):
        cap.add_feed("20001", f"t-{index}", now - 60)

    report = asyncio.run(plugin.run_interact(now=now))

    assert len(cap.calls_of("qzone.like")) == 1
    assert report.liked == 1


def test_interact_comment_generated_and_deduped(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path, COMMENT_ONLY, replies=["这云看着真舒服"]
    )
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60, content="今天的云很好看")

    asyncio.run(plugin.run_interact(now=now))
    asyncio.run(plugin.run_interact(now=now + 3600))

    comments = cap.calls_of("qzone.comment")
    assert comments == [
        {"uin": "20001", "tid": "t-1", "content": "这云看着真舒服"}
    ]
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "今天的云很好看" in prompt
    assert "机器人" in prompt  # 提示词里明确不许自称机器人


def test_interact_comment_chance_zero_never_comments(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path, 'comment_enabled: true\ncomment_chance: 0\n'
    )
    plugin.rng = FixedRng(0.0)
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)

    asyncio.run(plugin.run_interact(now=now))

    assert cap.calls_of("qzone.comment") == []
    assert llm.calls == []


def test_interact_comment_per_round_cap(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path,
        'comment_enabled: true\ncomment_chance: 1.0\ncomment_cap_per_round: 1\n',
        replies=["好看", "不错"],
    )
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)
    cap.add_feed("20001", "t-2", now - 50)

    report = asyncio.run(plugin.run_interact(now=now))

    assert len(cap.calls_of("qzone.comment")) == 1
    assert report.commented == 1
    assert len(llm.calls) == 1


def test_interact_quiet_hours_block_everything(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path,
        LIKE_ONLY + COMMENT_ONLY + REPLY_ONLY + 'quiet_hours: "00:00-23:59"\n',
        replies=["hi"],
    )
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)
    cap.comments = [
        {
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "c-1",
            "comment_uin": "30003",
            "nickname": "小明",
            "content": "羡慕",
            "created_time": int(now - 60),
        }
    ]

    report = asyncio.run(plugin.run_interact(now=now))

    assert cap.calls == []
    assert llm.calls == []
    assert "安静时段" in report.skipped


def test_interact_replies_only_to_others_comments(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path, REPLY_ONLY, replies=["谢谢呀"]
    )
    now = time.time()
    cap.comments = [
        {  # 自己发在自己说说下的评论：不回
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "c-self",
            "comment_uin": "10000",
            "nickname": "我",
            "content": "自己顶一下",
            "created_time": int(now - 30),
        },
        {  # 别人发的：回
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "c-1",
            "comment_uin": "30003",
            "nickname": "小明",
            "content": "照片呢",
            "created_time": int(now - 60),
        },
    ]

    report = asyncio.run(plugin.run_interact(now=now))
    asyncio.run(plugin.run_interact(now=now + 3600))  # 同一评论不重复回

    replies = cap.calls_of("qzone.reply")
    assert len(replies) == 1
    assert replies[0] == {
        "uin": "10000",
        "tid": "own-1",
        "comment_id": "c-1",
        "content": "谢谢呀",
        "comment_uin": "30003",
        "comment_nickname": "小明",
    }
    assert report.replied == 1


def test_interact_reply_caps_per_post_and_round(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path,
        'reply_enabled: true\nreply_cap_per_post: 2\nreply_cap_per_round: 1\n',
        replies=["嗯嗯", "哈哈", "好的"],
    )
    now = time.time()
    cap.comments = [
        {
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": f"c-{index}",
            "comment_uin": f"3000{index}",
            "nickname": f"朋友{index}",
            "content": "哈哈",
            "created_time": int(now - 60),
        }
        for index in range(4)
    ]

    # 每轮最多回 1 条
    asyncio.run(plugin.run_interact(now=now))
    assert len(cap.calls_of("qzone.reply")) == 1

    # 每条说说累计最多回 2 条：第 2 轮回掉第 2 条后，第 3 轮不再回
    asyncio.run(plugin.run_interact(now=now + 60))
    asyncio.run(plugin.run_interact(now=now + 120))
    assert [c["comment_id"] for c in cap.calls_of("qzone.reply")] == ["c-0", "c-1"]


def test_interact_daily_cap_blocks_likes(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(
        tmp_path,
        'like_enabled: true\nlike_chance: 1.0\nlike_cap_per_round: 0\n'
        'like_daily_cap: 1\n',
    )
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)
    cap.add_feed("20001", "t-2", now - 50)

    asyncio.run(plugin.run_interact(now=now))
    assert len(cap.calls_of("qzone.like")) == 1

    # 第二天额度重置
    cap.feeds["20001"].append(
        {"tid": "t-3", "uin": "20001", "created_time": int(now + 86400), "content": "新的一天"}
    )
    asyncio.run(plugin.run_interact(now=now + 86400))
    # 第二天先补上昨天没赞成的 t-2，额度用完后 t-3 留到下一天
    assert [c["tid"] for c in cap.calls_of("qzone.like")] == ["t-1", "t-2"]


def test_interact_failure_backs_off_and_stops_round(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(
        tmp_path, LIKE_ONLY + REPLY_ONLY + 'interact_backoff_minutes: 30\n'
    )
    now = time.time()
    cap.fail_for["qzone.feeds"] = "登录态失效"
    cap.comments = []

    report = asyncio.run(plugin.run_interact(now=now))
    assert report.stopped is True
    assert cap.names() == ["qzone.feeds"]  # 失败后不再继续，也不调回复

    # 退避期内整轮跳过，不再打接口
    report = asyncio.run(plugin.run_interact(now=now + 60))
    assert cap.names() == ["qzone.feeds"]
    assert "退避" in report.skipped

    # 退避结束后恢复
    cap.fail_for.clear()
    cap.add_feed("20001", "t-1", now - 60)
    report = asyncio.run(plugin.run_interact(now=now + 31 * 60))
    assert report.liked == 1


def test_interact_read_only_when_all_switches_off(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(tmp_path)
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)

    report = asyncio.run(plugin.run_interact(now=now))

    assert cap.names() == ["qzone.feeds"]
    assert llm.calls == []
    assert report.acted is False


def test_interact_task_starts_when_whitelist_configured(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)

    async def scenario():
        await bot.handle_event(private("在吗"))
        task = plugin._tasks.get("interact")
        assert task is not None and not task.done()
        assert task.get_name() == "qzone_post_interact"
        manager.set_enabled("qzone_post", False)
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.01)
        assert task.done()

    asyncio.run(scenario())


def test_interact_loop_survives_exceptions(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    plugin.config.interact_interval_minutes = 0.0001
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)
    calls = {"n": 0}
    original = plugin.run_interact

    async def flaky(*, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一轮炸了")
        return await original(now=now)

    plugin.run_interact = flaky

    async def scenario():
        stop_event = asyncio.Event()
        task = asyncio.create_task(plugin._interact_loop(stop_event))
        for _ in range(200):
            if cap.calls_of("qzone.like"):
                break
            await asyncio.sleep(0.01)
        assert calls["n"] >= 2
        assert cap.calls_of("qzone.like"), "第一轮异常后应当继续巡检"
        stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_interact_reply_skipped_when_llm_returns_empty(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path, REPLY_ONLY, replies=[""]
    )
    now = time.time()
    cap.comments = [
        {
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "c-1",
            "comment_uin": "30003",
            "nickname": "小明",
            "content": "照片呢",
            "created_time": int(now - 60),
        }
    ]

    report = asyncio.run(plugin.run_interact(now=now))

    assert len(llm.calls) == 1
    assert cap.calls_of("qzone.reply") == []


def test_interact_reply_skips_old_comments(tmp_path):
    bot, manager, plugin, llm, cap = make_interact_bot(
        tmp_path, REPLY_ONLY + 'interact_days: 1\n', replies=["谢谢"]
    )
    now = time.time()
    cap.comments = [
        {
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "old",
            "comment_uin": "30003",
            "nickname": "小明",
            "content": "太久了",
            "created_time": int(now - 5 * 86400),
        },
        {
            "tid": "own-1",
            "feed_uin": "10000",
            "comment_id": "new",
            "comment_uin": "30004",
            "nickname": "小红",
            "content": "刚发的",
            "created_time": int(now - 600),
        },
    ]

    asyncio.run(plugin.run_interact(now=now))

    assert [c["comment_id"] for c in cap.calls_of("qzone.reply")] == ["new"]


def test_interact_loop_runs_and_exits_cleanly(tmp_path):
    bot, manager, plugin, _, cap = make_interact_bot(tmp_path, LIKE_ONLY)
    plugin.config.interact_interval_minutes = 0.0001  # 一轮间隔压到 0.1 秒
    now = time.time()
    cap.add_feed("20001", "t-1", now - 60)

    async def scenario():
        stop_event = asyncio.Event()
        task = asyncio.create_task(plugin._interact_loop(stop_event))
        for _ in range(100):
            if cap.calls_of("qzone.like"):
                break
            await asyncio.sleep(0.01)
        assert cap.calls_of("qzone.like")
        stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_interact_config_defaults_and_override():
    module = _load_module()
    defaults = module.QzonePostConfig()
    assert defaults.interact_interval_minutes == 60
    assert defaults.interact_days == 3
    assert defaults.interact_uins == []
    # 写操作默认全关，只读巡检默认开
    assert defaults.like_enabled is False
    assert defaults.comment_enabled is False
    assert defaults.reply_enabled is False
    assert defaults.like_chance == 0.8
    assert defaults.comment_chance == 0.3

    cfg = module.config_from_dict(
        {
            "interact_uins": [20001, "o20002", "20001"],
            "interact_days": "5",
            "like_enabled": True,
            "like_chance": 2,
            "comment_chance": -1,
            "reply_cap_per_post": "2",
        }
    )
    assert cfg.interact_uins == ["20001", "20002"]
    assert cfg.interact_days == 5
    assert cfg.like_enabled is True
    assert cfg.like_chance == 1.0
    assert cfg.comment_chance == 0.0
    assert cfg.reply_cap_per_post == 2
    assert cfg.comment_enabled is False
