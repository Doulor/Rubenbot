"""端到端：ConsoleAdapter + 假 LLM，跑通「发消息 → 落流水 → 抽取 → 引用记忆」。"""

import asyncio
import io
import json

from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona

from conftest import ScriptedLLM


def build() -> tuple[RubenBot, ConsoleAdapter, ScriptedLLM]:
    config = Config()
    config.reply.segment_delay_enabled = False
    config.decision.merge_window_seconds = 0  # 测试里不攒消息，逐条处理
    config.decision.hard_merge_window_seconds = 0
    config.decision.base = 0.0  # 群里默认不插话
    # 端到端走旧裁定路径（意愿评分 + 整份 SOUL），与线上灰度前的行为一致
    config.planner.planner_gate = False
    store = MemoryStore(":memory:")
    extract_payload = json.dumps(
        [
            {"content": "TA 是一名住在杭州的后端工程师", "category": "identity", "scope": "global"},
            {"content": "TA 在养一只叫团子的橘猫", "category": "experience", "scope": "global"},
        ],
        ensure_ascii=False,
    )
    llm = ScriptedLLM(
        {
            "extract": [extract_payload],
            "chat": [
                "后端啊，那天天跟服务器打交道",
                "团子这名字好，一定很胖",
            ],
        }
    )
    bot = RubenBot(config, store, llm, Persona("Ruben", "你是 Ruben，一个群友。"))
    out = io.StringIO()
    console = ConsoleAdapter(stdout=out)
    bot.register_adapter(console)
    console.bind(bot.handle_event)
    return bot, console, llm


def test_end_to_end_memory_roundtrip():
    async def scenario():
        bot, console, llm = build()

        # 1. 私聊说两句关于自己的话
        await console.feed_line("我在杭州做后端开发")
        await console.feed_line("对了，我家猫叫团子，是只橘猫")
        assert bot.store.pending_senders()[0].count >= 2

        # 2. 触发抽取
        await bot.run_maintenance(now=1e9, force_extract=True)
        memories = bot.store.list_memories(person_id="console:console:me")
        contents = [m.content for m in memories]
        assert any("杭州" in c for c in contents)
        assert any("团子" in c for c in contents)

        # 3. 再发一条消息，系统提示里应带上记忆
        await console.feed_line("你还记得我是干嘛的吗")
        last_call = llm.chat_messages[-1]
        system_prompt = last_call[0]["content"]
        assert "后端工程师" in system_prompt
        assert "团子" in system_prompt
        # 历史消息也在上下文里
        assert any("我在杭州做后端开发" in m["content"] for m in last_call)

        # 4. 回复真的发出去了
        assert "后端" in console.stdout.getvalue()

    asyncio.run(scenario())


def test_end_to_end_group_mention():
    async def scenario():
        bot, console, llm = build()
        # 群聊里没被 @：低活跃度下不回复
        await console.feed_line("[摸鱼群|阿花] 今天好累")
        assert "Ruben:" not in console.stdout.getvalue()

        # 被 @：必回。ConsoleAdapter 群聊不自动置位，直接构造事件
        event = console.parse_line("[摸鱼群|阿花] 在吗")
        event.mentions_bot = True
        await bot.handle_event(event)
        assert "Ruben:" in console.stdout.getvalue()
        output = console.stdout.getvalue()
        assert "后端" in output

    asyncio.run(scenario())


def test_end_to_end_merge_window_with_merge_loop():
    """走真实的合并循环：碎片消息攒成一轮后只回复一次。"""

    async def scenario():
        bot, console, llm = build()
        bot.config.decision.merge_window_seconds = 0.05
        bot.merger.window = 0.05
        bot.config.decision.base = 10.0  # 群里保证过线
        bot._stop.clear()
        task = asyncio.create_task(bot._merge_loop())
        await console.feed_line("[摸鱼群|阿花] 我跟你讲")
        await console.feed_line("[摸鱼群|阿花] 今天遇到件事")
        await asyncio.sleep(0.01)
        assert console.sent == []  # 还在窗口里，不应逐条回复
        await asyncio.sleep(0.4)
        assert len(console.sent) == 1
        assert len(llm.chat_messages) == 1
        bot.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_streaming_stdin_reader():
    """用 asyncio.StreamReader 模拟 stdin，验证 start() 能把行送给回调。"""

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data("第一行\n第二行\n".encode())
        reader.feed_eof()
        adapter = ConsoleAdapter(reader=reader, stdout=io.StringIO())
        seen = []
        await adapter.start(lambda event: _append(seen, event))
        return seen

    seen = asyncio.run(scenario())
    assert [e.text for e in seen] == ["第一行", "第二行"]


async def _append(bucket, item):
    bucket.append(item)
