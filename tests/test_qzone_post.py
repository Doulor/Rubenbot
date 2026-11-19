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
        tmp_path, "interval_hours: 1\n", llm_replies=["摸鱼的一下午"]
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
    bot, adapter, manager, _, cap, _ = make_bot(tmp_path, "interval_hours: 1\n", llm_replies=[""])
    plugin = manager.extras["qzone_post"]
    ok, reason = asyncio.run(plugin.publish_scheduled())
    assert ok is False
    assert reason == "没能生成内容"
    assert cap.calls == []


def test_interval_path_capability_failure_contained(tmp_path):
    cap = FakeCapability(ok=False, reason="网络炸了")
    bot, adapter, manager, _, _, _ = make_bot(tmp_path, "interval_hours: 1\n", capability=cap)
    plugin = manager.extras["qzone_post"]
    ok, detail = asyncio.run(plugin.publish_scheduled())
    assert ok is False and detail == "网络炸了"


def test_interval_loop_posts_and_exits_on_stop(tmp_path, monkeypatch):
    """把间隔缩到几乎为 0，验证后台任务真的会发；停用后干净退出。"""
    bot, adapter, manager, llm, cap, _ = make_bot(
        tmp_path, "interval_hours: 0.0001\n", llm_replies=["夜里的小情绪"]
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
        tmp_path, "interval_hours: 0.0001\n", llm_replies=["第一条", "第二条", "第三条"]
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
        tmp_path, "interval_hours: 0.0001\n", llm_replies=["x"]
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
        tmp_path, "interval_hours: 100\n", llm_replies=["x"]
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
