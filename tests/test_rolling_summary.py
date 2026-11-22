"""会话滚动摘要：触发/游标推进/增量折叠、上下文组装、开关与容错。"""

import asyncio
import json

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.memory.summary import (
    SUMMARY_BLOCK_TITLE,
    RollingSummarizer,
    format_summary_block,
    trim_summary,
)
from rubenbot.persona import Persona

from conftest import RecordingAdapter, ScriptedLLM


# ----------------------------------------------------------------------
# 夹具与工具
# ----------------------------------------------------------------------


def make_config(*, enabled=True, trigger_after=5, keep_recent=2, max_chars=800) -> Config:
    cfg = Config()
    cfg.reply.segment_delay_enabled = False
    cfg.decision.merge_window_seconds = 0
    cfg.decision.hard_merge_window_seconds = 0
    cfg.memory.rolling_summary.enabled = enabled
    cfg.memory.rolling_summary.trigger_after = trigger_after
    cfg.memory.rolling_summary.keep_recent = keep_recent
    cfg.memory.rolling_summary.max_chars = max_chars
    return cfg


def make_bot(config, store, llm) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm, Persona(name="Ruben", text="你是 Ruben。"))
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def add_messages(store, count, *, chat_id="g1", platform="recording", start_ts=100.0):
    """按顺序往某个会话落 count 条消息，返回落库的 id 列表。"""
    ids = []
    for index in range(count):
        ids.append(
            store.add_raw_message(
                platform=platform,
                chat_id=chat_id,
                sender_id="u1",
                sender_name="阿花",
                text=f"第{index}条消息",
                ts=start_ts + index,
            )
        )
    return ids


def event(text="现在聊到哪了", *, chat_type="group", chat_id="g1") -> MessageEvent:
    return MessageEvent(
        platform="recording",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id="u1",
        sender_name="阿花",
        text=text,
    )


SUMMARY_TEXT = (
    "一、当前/近期话题\n在聊周末爬山。\n"
    "二、关键事实\n阿花住杭州。\n"
    "三、人物动态\n阿草最近在找工作。\n"
    "四、未完的话题与梗\n约好下次一起看日出。\n"
    "五、氛围与注意事项\n气氛轻松。"
)


# ----------------------------------------------------------------------
# 触发、游标推进与增量折叠
# ----------------------------------------------------------------------


def test_below_threshold_does_not_summarize(store):
    config = make_config(trigger_after=5, keep_recent=2)
    add_messages(store, 5)  # 恰好 5 条，没有「超过」阈值
    llm = ScriptedLLM({"summary": [SUMMARY_TEXT]})
    summarizer = RollingSummarizer(llm, store, config.memory)

    result = asyncio.run(summarizer.summarize_chat("recording", "g1"))

    assert result is None
    assert store.get_conversation_summary("recording", "g1") is None
    assert llm.calls == []


def test_trigger_creates_summary_and_advances_cursor(store):
    config = make_config(trigger_after=5, keep_recent=2)
    ids = add_messages(store, 8)
    llm = ScriptedLLM({"summary": [SUMMARY_TEXT]})
    summarizer = RollingSummarizer(llm, store, config.memory)

    result = asyncio.run(summarizer.summarize_chat("recording", "g1"))

    assert result is not None and result.updated
    stored = store.get_conversation_summary("recording", "g1")
    assert stored.summary == SUMMARY_TEXT
    # 8 条里保留最近 2 条，压掉前 6 条
    assert stored.covered_until_msg_id == ids[5]
    assert result.compressed_messages == 6
    # 用 summary 角色、task 打标
    assert llm.calls[0]["role"] == "summary"
    assert llm.calls[0]["task"] == "rolling_summary"
    prompt = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "第0条消息" in prompt and "第5条消息" in prompt
    assert "第6条消息" not in prompt  # 最近 keep_recent 条不压


def test_second_trigger_folds_into_existing_summary(store):
    config = make_config(trigger_after=5, keep_recent=2)
    add_messages(store, 8)
    llm = ScriptedLLM({"summary": ["旧摘要：" + SUMMARY_TEXT, "新摘要：" + SUMMARY_TEXT]})
    summarizer = RollingSummarizer(llm, store, config.memory)
    asyncio.run(summarizer.summarize_chat("recording", "g1"))

    second_ids = add_messages(store, 7, start_ts=200.0)
    result = asyncio.run(summarizer.summarize_chat("recording", "g1"))

    assert result is not None and result.updated
    stored = store.get_conversation_summary("recording", "g1")
    assert stored.summary == "新摘要：" + SUMMARY_TEXT
    assert stored.covered_until_msg_id == second_ids[4]
    # 第二次请求必须带上旧摘要（增量折叠，而不是从零重写）
    second_prompt = "\n".join(m["content"] for m in llm.calls[1]["messages"])
    assert "旧摘要：" + SUMMARY_TEXT in second_prompt
    # 只喂水位之后的新内容
    first_prompt = "\n".join(m["content"] for m in llm.calls[0]["messages"])
    assert "第7条消息" not in first_prompt


def test_failed_summary_keeps_old_row_and_cursor(store):
    class BoomLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("模型挂了")

    config = make_config(trigger_after=1, keep_recent=1)
    add_messages(store, 4)
    store.set_conversation_summary("recording", "g1", "旧摘要", 2, source_ids=[1, 2], now=100.0)
    summarizer = RollingSummarizer(BoomLLM(), store, config.memory)

    result = asyncio.run(summarizer.summarize_chat("recording", "g1"))

    assert result is not None and result.error
    stored = store.get_conversation_summary("recording", "g1")
    assert stored.summary == "旧摘要"
    assert stored.covered_until_msg_id == 2


def test_empty_model_output_does_not_overwrite(store):
    config = make_config(trigger_after=2, keep_recent=1)
    add_messages(store, 4)
    summarizer = RollingSummarizer(ScriptedLLM({"summary": ["   "]}), store, config.memory)

    result = asyncio.run(summarizer.summarize_chat("recording", "g1"))

    assert result is not None and result.error == "empty summary"
    assert store.get_conversation_summary("recording", "g1") is None


def test_maybe_summarize_skips_when_disabled(store):
    config = make_config(enabled=False, trigger_after=1, keep_recent=0)
    add_messages(store, 10)
    llm = ScriptedLLM({"summary": [SUMMARY_TEXT]})
    summarizer = RollingSummarizer(llm, store, config.memory)

    assert asyncio.run(summarizer.maybe_summarize()) == []
    assert llm.calls == []
    assert store.get_conversation_summary("recording", "g1") is None


def test_conversations_are_isolated(store):
    config = make_config(trigger_after=2, keep_recent=1)
    add_messages(store, 4, chat_id="g1")
    add_messages(store, 4, chat_id="g2")
    llm = ScriptedLLM({"summary": ["摘要A", "摘要B"]})
    summarizer = RollingSummarizer(llm, store, config.memory)

    asyncio.run(summarizer.summarize_chat("recording", "g1"))
    asyncio.run(summarizer.summarize_chat("recording", "g2"))

    assert store.get_conversation_summary("recording", "g1").summary == "摘要A"
    assert store.get_conversation_summary("recording", "g2").summary == "摘要B"


def test_same_id_group_prompt_has_only_group_summary_history_and_members(store):
    config = make_config(trigger_after=1, keep_recent=1)
    config.privacy.enabled = True

    class Summaries(ScriptedLLM):
        async def complete(self, messages, **kwargs):
            if kwargs.get('task') == 'rolling_summary':
                text = messages[-1]['content']
                assert ('私下花名' in text) != ('群内记录' in text)
                self.responses['summary'] = ['私人备忘' if '私下花名' in text else '群里登山约定']
            return await super().complete(messages, **kwargs)

    bot, _ = make_bot(config, store, Summaries())
    ids = {'group': [], 'private': []}
    for i in range(3):
        for kind, name, sender, text in [('group', '群内阿青', 'u1', '群内记录'),
                                        ('private', '私下花名', 'g1', '专属私聊记录')]:
            ids[kind].append(store.add_raw_message(platform='recording', chat_type=kind, chat_id='g1',
                                                   sender_id=sender, sender_name=name, text=f'{text}{i}', ts=100+i))
    results = asyncio.run(bot.summarizer.maybe_summarize(now=200))
    assert len(results) == 2 and all(r.updated for r in results)
    for kind, summary, sender in [('group', '群里登山约定', 'u1'), ('private', '私人备忘', 'g1')]:
        row = bot.summarizer.active_summary('recording', 'g1', chat_type=kind)
        assert (row.summary, row.covered_until_msg_id, row.subjects) == (summary, ids[kind][1], ['recording:' + sender])
    prompt = '\n'.join(m['content'] for m in asyncio.run(bot.build_context(event(), now=201)))
    assert all(part in prompt for part in ('群内阿青', '群里登山约定', '群内记录2'))
    assert all(part not in prompt for part in ('私下花名', '专属私聊记录', '私人备忘', '群内记录1'))


def test_no_record_lines_are_short_history_not_summarization_input(store):
    from rubenbot.group_profile import GroupProfileSummarizer

    config = make_config(trigger_after=1, keep_recent=0)
    config.memory.group_profile.min_messages = 2
    config.privacy.enabled = True
    llm = ScriptedLLM()
    bot, _ = make_bot(config, store, llm)
    group = GroupProfileSummarizer(llm, store, config.memory.group_profile)
    store.set_privacy_mode('recording:u1', 'norecord')
    for i in range(3):
        store.add_raw_message(platform='recording', chat_id='g1', sender_id='u1', text=f'只作片刻上下文{i}', ts=101+i)
    store.set_privacy_mode('recording:u1', 'normal')  # 恢复档位不追溯生成长期记忆。
    pending = asyncio.run(bot.build_context(event(), now=110))
    assert any('只作片刻上下文2' in m['content'] for m in pending)
    assert asyncio.run(bot.summarizer.maybe_summarize(now=110)) == []
    assert asyncio.run(group.maybe_update(now=110)) == []
    add_messages(store, 2, start_ts=120)
    assert asyncio.run(bot.summarizer.maybe_summarize(now=130))[0].updated
    assert asyncio.run(group.maybe_update(now=130))[0].updated
    prompts = {c['task']: c['messages'][-1]['content'] for c in llm.calls if c['role'] == 'summary'}
    assert set(prompts) == {'rolling_summary', 'group_profile'}
    assert all('第1条消息' in text and '只作片刻上下文' not in text for text in prompts.values())
    # 新摘要水位超过不记消息的 id，不代表它已被摘要覆盖；只能自然退出当前短期窗口。
    active = asyncio.run(bot.build_context(event(), now=131))
    assert any('只作片刻上下文2' in m['content'] for m in active)
    config.chat.history_size = 2
    expired = asyncio.run(bot.build_context(event(), now=132))
    assert all('只作片刻上下文' not in m['content'] for m in expired)


def test_summary_keeps_earlier_subjects_after_trim(store):
    summarizer = RollingSummarizer(ScriptedLLM(), store, make_config(trigger_after=1, keep_recent=0).memory)
    for sender in ('u1', 'u2'):
        for uid in (sender, '__bot__'):
            store.add_raw_message(platform='recording', chat_id='g1', sender_id=uid, text='新进展', processed=True)
        assert asyncio.run(summarizer.summarize_chat('recording', 'g1')).updated
        store.trim_raw(0)
    assert store.get_conversation_summary('recording', 'g1').subjects == ['recording:u1', 'recording:u2']


@pytest.mark.parametrize('invalidate', ['batch', 'previous'])
def test_withdrawn_evidence_while_summarizing_is_not_written_back(store, invalidate):
    add_messages(store, 2)
    assert store.set_conversation_summary('recording', 'g1', '已有摘要', 2, source_ids=[1, 2])
    add_messages(store, 2)

    class Withdrawn(ScriptedLLM):
        async def complete(self, *args, **kwargs):
            with store._conn:
                if invalidate == 'batch':
                    store._conn.execute('DELETE FROM raw_messages WHERE id=3')
                else:
                    store._conn.execute('DELETE FROM conversation_summaries')
            return '不可写回的结果'

    summarizer = RollingSummarizer(Withdrawn(), store, make_config(trigger_after=1, keep_recent=0).memory)
    result = asyncio.run(summarizer.summarize_chat('recording', 'g1'))
    assert result.error and not result.updated
    old = store.get_conversation_summary('recording', 'g1')
    assert (old.summary if old else None) == ('已有摘要' if invalidate == 'batch' else None)


def test_summary_length_is_capped(store):
    config = make_config(trigger_after=1, keep_recent=0, max_chars=100)
    add_messages(store, 3)
    long_text = "一、当前/近期话题\n" + "很长的内容" * 100
    summarizer = RollingSummarizer(ScriptedLLM({"summary": [long_text]}), store, config.memory)

    asyncio.run(summarizer.summarize_chat("recording", "g1"))

    stored = store.get_conversation_summary("recording", "g1")
    assert len(stored.summary) <= 100


# ----------------------------------------------------------------------
# 上下文组装
# ----------------------------------------------------------------------


def test_build_context_injects_summary_and_only_messages_after_cursor(store):
    config = make_config(trigger_after=2, keep_recent=1)
    ids = add_messages(store, 6)
    store.set_conversation_summary("recording", "g1", "早期在聊爬山和找工作的阿草", ids[3], source_ids=ids[:4], now=100.0)
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))

    messages = asyncio.run(bot.build_context(event(), now=200.0))

    system = messages[0]["content"]
    assert SUMMARY_BLOCK_TITLE in system
    assert "早期在聊爬山和找工作的阿草" in system
    contents = [m["content"] for m in messages[1:]]
    joined = "\n".join(contents)
    # 水位之前的原文不再出现，水位之后的照常带入
    assert "第0条消息" not in joined
    assert "第2条消息" not in joined and "第3条消息" not in joined
    assert "第4条消息" in joined and "第5条消息" in joined


def test_build_context_unchanged_when_disabled(store):
    config = make_config(enabled=False, trigger_after=2, keep_recent=1)
    ids = add_messages(store, 6)
    # 即使库里已有摘要，开关关闭时也不注入、不裁剪原文
    store.set_conversation_summary("recording", "g1", "不该出现的摘要", ids[3], source_ids=ids[:4], now=100.0)
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))

    messages = asyncio.run(bot.build_context(event(), now=200.0))

    system = messages[0]["content"]
    assert SUMMARY_BLOCK_TITLE not in system
    assert "不该出现的摘要" not in system
    joined = "\n".join(m["content"] for m in messages[1:])
    assert "第0条消息" in joined and "第5条消息" in joined


def test_build_context_without_summary_matches_old_behavior(store):
    config = make_config()
    add_messages(store, 3)
    bot, _ = make_bot(config, store, ScriptedLLM({"chat": ["嗯"]}))

    messages = asyncio.run(bot.build_context(event(), now=200.0))

    assert SUMMARY_BLOCK_TITLE not in messages[0]["content"]
    joined = "\n".join(m["content"] for m in messages[1:])
    assert "第0条消息" in joined and "第2条消息" in joined


# ----------------------------------------------------------------------
# 与主循环联动：失败不阻塞回复；后台维护会更新摘要
# ----------------------------------------------------------------------


def test_reply_still_works_when_summary_model_fails(config, store):
    config.memory.rolling_summary.trigger_after = 2
    config.memory.rolling_summary.keep_recent = 1

    class BoomOnSummary(ScriptedLLM):
        async def complete(self, messages, *, role="chat", task=None, **kwargs):
            if role == "summary":
                raise RuntimeError("摘要模型挂了")
            return await super().complete(messages, role=role, task=task, **kwargs)

    bot, adapter = make_bot(config, store, BoomOnSummary({"chat": ["在的"]}))
    add_messages(store, 5)
    asyncio.run(bot.run_maintenance(now=1000.0))
    assert store.get_conversation_summary("recording", "g1") is None

    asyncio.run(bot.handle_event(event("在吗", chat_type="private", chat_id="u1")))
    assert adapter.texts == ["在的"]


def test_maintenance_updates_summary_via_core(config, store):
    config.memory.rolling_summary.trigger_after = 3
    config.memory.rolling_summary.keep_recent = 1
    llm = ScriptedLLM({"summary": [SUMMARY_TEXT]})
    bot, _ = make_bot(config, store, llm)
    ids = add_messages(store, 6)

    asyncio.run(bot.run_maintenance(now=1000.0))

    stored = store.get_conversation_summary("recording", "g1")
    assert stored is not None
    assert stored.covered_until_msg_id == ids[4]
    # 维护任务里也走 summary 角色
    assert any(call["role"] == "summary" for call in llm.calls)


# ----------------------------------------------------------------------
# 配置与工具函数
# ----------------------------------------------------------------------

def test_large_backlog_is_compressed_in_batches(store):
    """积压很多时分批压：水位一次只推进一批，剩下的下一轮继续。"""
    from rubenbot.memory.summary import MAX_BATCH_MESSAGES

    config = make_config(trigger_after=5, keep_recent=1)
    total = MAX_BATCH_MESSAGES + 50
    ids = add_messages(store, total)
    summarizer = RollingSummarizer(ScriptedLLM({"summary": ["摘要"]}), store, config.memory)

    first = asyncio.run(summarizer.summarize_chat("recording", "g1"))
    assert first.compressed_messages == MAX_BATCH_MESSAGES
    assert store.get_conversation_summary("recording", "g1").covered_until_msg_id == ids[MAX_BATCH_MESSAGES - 1]

    second = asyncio.run(summarizer.summarize_chat("recording", "g1"))
    assert second is not None and second.updated
    # 第二批是剩下 49 条（总数 - 一批 - 保留的 1 条）
    assert second.compressed_messages == total - MAX_BATCH_MESSAGES - 1


def test_structured_config_validates_rolling_summary():
    from rubenbot.web.config_schema import ConfigValidationError, validate_structured

    update = validate_structured(
        {
            "memory": {
                "rolling_summary": {
                    "enabled": False,
                    "trigger_after": "12",
                    "keep_recent": 4,
                    "max_chars": 500,
                }
            }
        }
    )
    assert update["memory"]["rolling_summary"] == {
        "enabled": False,
        "trigger_after": 12,
        "keep_recent": 4,
        "max_chars": 500,
    }
    with pytest.raises(ConfigValidationError):
        validate_structured({"memory": {"rolling_summary": {"trigger_after": 0}}})


def test_summary_call_recorded_in_llm_usage():
    """摘要调用走 LLMClient 时记入 llm_usage，task 为 rolling_summary。"""
    import httpx

    from rubenbot.config import LLMConfig
    from rubenbot.llm import LLMClient, make_usage_recorder

    store = MemoryStore(":memory:")
    try:
        config = make_config(trigger_after=1, keep_recent=0)
        add_messages(store, 3)
        client = LLMClient(
            LLMConfig(api_key="k", chat_model="m"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "摘要"}}]})
            ),
            usage_recorder=make_usage_recorder(store),
            retry_delays=(0.0,),
        )
        summarizer = RollingSummarizer(client, store, config.memory)
        asyncio.run(summarizer.summarize_chat("recording", "g1"))
        asyncio.run(client.close())
        by_task = {item["task"]: item for item in store.usage_by_task()}
        assert by_task["rolling_summary"]["calls"] == 1
    finally:
        store.close()


def test_config_rolling_summary_defaults_and_parsing(tmp_path):
    config = Config()
    assert config.memory.rolling_summary.enabled is True
    assert config.memory.rolling_summary.trigger_after == 60
    assert config.memory.rolling_summary.keep_recent == 30

    path = tmp_path / "config.yaml"
    path.write_text(
        "memory:\n  rolling_summary:\n    enabled: false\n    trigger_after: 12\n"
        "    keep_recent: 4\n    max_chars: 500\n",
        encoding="utf-8",
    )
    loaded = Config.load(path)
    assert loaded.memory.rolling_summary.enabled is False
    assert loaded.memory.rolling_summary.trigger_after == 12
    assert loaded.memory.rolling_summary.keep_recent == 4
    assert loaded.memory.rolling_summary.max_chars == 500


def test_config_bad_values_fall_back_to_defaults():
    loaded = Config.from_dict(
        {"memory": {"rolling_summary": {"enabled": True, "trigger_after": "坏", "keep_recent": -5}}}
    )
    assert loaded.memory.rolling_summary.enabled is True
    assert loaded.memory.rolling_summary.trigger_after == 60
    assert loaded.memory.rolling_summary.keep_recent == 0


def test_format_summary_block_marks_early_history():
    block = format_summary_block("第一段内容")
    assert block.startswith(SUMMARY_BLOCK_TITLE)
    assert "更早的对话" in block
    assert "第一段内容" in block
    assert format_summary_block("  ") == ""


def test_trim_summary_respects_paragraph_boundary():
    text = "第一段很长的内容。\n第二段。"
    assert trim_summary(text, 9) == "第一段很长的内容。"


def test_very_long_message_is_truncated_in_summary_input(store):
    """个别超长消息只取开头，不会把摘要请求撑爆。"""
    from rubenbot.memory.summary import MAX_MESSAGE_CHARS, format_messages_for_summary

    msg_id = store.add_raw_message(
        platform="recording",
        chat_id="g1",
        sender_id="u1",
        sender_name="阿花",
        text="长" * (MAX_MESSAGE_CHARS * 3),
        ts=100.0,
    )
    rendered = format_messages_for_summary(store.raw_messages_after("g1", platform="recording"))
    assert len(rendered) < MAX_MESSAGE_CHARS + 100
    assert rendered.endswith("…")
    assert store.raw_messages_after("g1", platform="recording")[0].id == msg_id


def test_legacy_unscoped_summary_and_cursor_are_not_adopted(tmp_path):
    import sqlite3

    path = tmp_path / 'mixed.sqlite'
    old = MemoryStore(path)
    add_messages(old, 3)
    old.set_group_manual_note('recording', 'g1', '站长手写')
    old.set_group_auto_summary('recording', 'g1', '旧版归纳：或含私聊', 3)
    old.close()
    with sqlite3.connect(path) as conn:
        conn.executescript("""DROP TABLE conversation_summaries;
            CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY, summary TEXT,
                covered_until_msg_id INTEGER, updated_at REAL);
            INSERT INTO conversation_summaries VALUES('recording:g1','无出处的旧摘要',77,100);""")
    migrated = MemoryStore(path)
    try:
        assert all(migrated.get_conversation_summary('recording', 'g1', chat_type=kind) is None
                   for kind in ('group', 'private'))
        profile = migrated.get_group_profile('recording', 'g1')
        assert (profile.auto_summary, profile.covered_until_msg_id, profile.manual_note) == ('', 0, '站长手写')
        assert migrated.set_conversation_summary('recording', 'g1', '新摘要', 3, source_ids=[1, 2, 3])
    finally:
        migrated.close()
    reopened = MemoryStore(path)
    try:
        assert reopened.get_conversation_summary('recording', 'g1').summary == '新摘要'
        assert reopened.get_conversation_summary('recording', 'g1', chat_type='private') is None
    finally:
        reopened.close()


def test_old_db_gets_conversation_summaries_table(tmp_path):
    """旧库（没有摘要表）启动时自动补表，现有数据不受影响。"""
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE raw_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,"
        " chat_id TEXT NOT NULL, chat_type TEXT NOT NULL DEFAULT 'group', sender_id TEXT NOT NULL,"
        " sender_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, ts REAL NOT NULL,"
        " processed INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO raw_messages(platform, chat_id, sender_id, text, ts)"
        " VALUES('recording','g1','u1','旧消息',10.0);"
    )
    conn.commit()
    conn.close()

    store = MemoryStore(path)
    try:
        assert store.count_raw_messages_after("g1", platform="recording") == 1
        assert store.get_conversation_summary("recording", "g1") is None
        # 新表可读写
        store.set_conversation_summary("recording", "g1", "摘要", 1, source_ids=[1], now=20.0)
        assert store.get_conversation_summary("recording", "g1").summary == "摘要"
    finally:
        store.close()
