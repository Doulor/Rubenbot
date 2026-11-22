"""回复多段化：断句器单元测试、生成契约解析与配置。"""

import asyncio
import random

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.config import Config
from rubenbot.core import BOT_SENDER_ID, REPLY_SEGMENT_CONTRACT, RubenBot
from rubenbot.persona import Persona
from rubenbot.reply_segments import split_segments, split_sentences

from conftest import RecordingAdapter, ScriptedLLM


def make_bot(config, store, llm=None, *, rng=None) -> tuple[RubenBot, RecordingAdapter]:
    bot = RubenBot(config, store, llm or ScriptedLLM(), Persona("Ruben", "你是 Ruben。"), rng=rng)
    adapter = RecordingAdapter()
    bot.register_adapter(adapter)
    return bot, adapter


def private_event(text="在吗") -> MessageEvent:
    return MessageEvent("recording", "private", "u1", "u1", "阿花", text)


# ----------------------------------------------------------------------
# 规则断句：保护引号/括号/颜文字/英文/URL
# ----------------------------------------------------------------------


def test_quoted_sentence_not_cut_inside_quotes():
    text = "他说「快点。等一下」然后走了。"
    assert split_segments(text, rng=random.Random(0)) == [text]


def test_bracketed_kaomoji_never_cut():
    text = "太好了（≧▽≦。笑死）我们走。"
    assert split_segments(text, rng=random.Random(0)) == [text]
    text = "好开心(≧▽≦)！周末一起去玩吧。"
    parts = split_segments(text, rng=random.Random(0))
    assert all(part.count("(") == part.count(")") for part in parts)
    assert any("(≧▽≦)" in part for part in parts)


def test_kaomoji_letters_not_cut():
    parts = split_segments("今天真的绝了QAQ！但是没关系OTZ。", rng=random.Random(0))
    assert any("QAQ" in part for part in parts)
    assert any("OTZ" in part for part in parts)


def test_url_kept_whole():
    url = "https://example.com/a?b=1&c=2!d=3"
    parts = split_segments(f"看这个！{url}。好好玩。", rng=random.Random(0))
    assert any(url in part for part in parts)
    assert all("https://" not in part or url in part for part in parts)


def test_english_words_and_mixed_spaces_not_cut():
    assert split_segments("hello world this is fine", rng=random.Random(0)) == [
        "hello world this is fine"
    ]
    assert split_segments("我卡得 PPT 一样", rng=random.Random(0)) == ["我卡得 PPT 一样"]


def test_unmatched_bracket_falls_back_to_plain_text():
    parts = split_segments("前面（没闭合。后面", rng=random.Random(0))
    assert "后面" in "".join(parts)


# ----------------------------------------------------------------------
# 概率合并：总长越长越倾向多条
# ----------------------------------------------------------------------


def test_short_reply_stays_one_segment():
    assert split_segments("好。走。", rng=random.Random(0)) == ["好。走。"]
    assert split_segments("好累啊，今天加班", rng=random.Random(0)) == ["好累啊，今天加班"]


def test_long_reply_more_likely_split():
    long = "今天累死我了，加班到十点，回家路上还下雨，鞋全湿了，真的绝"
    assert len(split_segments(long, rng=random.Random(1))) >= 2


def test_merge_is_deterministic_with_seed():
    long = "今天累死我了，加班到十点，回家路上还下雨，鞋全湿了，真的绝"
    assert split_segments(long, rng=random.Random(1)) == split_segments(
        long, rng=random.Random(1)
    )


def test_segment_cap_merges_overflow_into_last():
    parts = split_segments("一\n二\n三\n四", max_segments=2, rng=random.Random(0))
    assert parts == ["一", "二三四"]


def test_empty_and_punctuation_only_returns_nothing():
    assert split_segments("") == []
    assert split_segments("   ") == []
    assert split_segments("，。，") == []
    assert split_segments("好\n，\n走。") == ["好", "走。"]


def test_expressive_punctuation_only_kept():
    """「？」「……」这类是刻意的语气，不是需要丢弃的空段。"""
    assert split_segments("？") == ["？"]
    assert split_segments("……") == ["……"]


def test_split_sentences_legacy_helper_keeps_quotes_and_code():
    assert split_sentences("他说「快点。等一下」然后走了。") == ["他说「快点。等一下」然后走了。"]
    assert split_sentences("试一下 `print('a。b')` 这句。好。") == [
        "试一下 `print('a。b')` 这句。",
        "好。",
    ]


# ----------------------------------------------------------------------
# 生成契约：换行分段 → 多条发送
# ----------------------------------------------------------------------


def test_generation_contract_is_in_code_not_persona():
    assert "1~3 段" in REPLY_SEGMENT_CONTRACT
    assert "换行" in REPLY_SEGMENT_CONTRACT
    # 人格文件正文没有被改动（多段要求只由生成指令给出）
    assert "1~3 段" not in Persona("Ruben", "你是 Ruben。").system_prompt()


def test_contract_appended_to_generation_prompt(config, store):
    llm = ScriptedLLM({"chat": ["在的"]})
    bot, _ = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event()))
    last = llm.chat_messages[0][-1]
    assert last["role"] == "system"
    assert REPLY_SEGMENT_CONTRACT in last["content"]


def test_newline_reply_sent_as_multiple_messages(config, store):
    config.reply.max_segments = 3
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["确实\n不过这也太离谱了"]}))
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["确实", "不过这也太离谱了"]
    # 流水逐条记录，两条都在
    bot_texts = [
        m.text
        for m in store.recent_messages("u1", platform="recording")
        if m.sender_id == BOT_SENDER_ID
    ]
    assert bot_texts == ["确实", "不过这也太离谱了"]


def test_continued_thought_keeps_its_pauses_and_order(config, store):
    # 用一个因果相接的三段样本回放发送链；真实模型是否写得连贯另做对照评估。
    parts = ["我还以为赶得上末班车", "结果跑到门口", "刚好看它开走"]
    llm = ScriptedLLM({"chat": ["\n".join(parts)]})
    bot, adapter = make_bot(config, store, llm)
    asyncio.run(bot.handle_event(private_event("昨天怎么回去的")))
    assert REPLY_SEGMENT_CONTRACT in llm.chat_messages[0][-1]["content"]
    assert adapter.texts == parts


def test_max_segments_one_sends_single_message(config, store):
    config.reply.max_segments = 1
    bot, adapter = make_bot(config, store, ScriptedLLM({"chat": ["一\n二"]}))
    asyncio.run(bot.handle_event(private_event()))
    assert adapter.texts == ["一\n二"]


# ----------------------------------------------------------------------
# 配置：默认值、示例文件、WebUI 校验
# ----------------------------------------------------------------------


def test_reply_config_defaults():
    reply = Config().reply
    assert reply.max_segments == 3
    assert reply.segment_delay_enabled is True
    assert reply.segment_delay_per_char_ms == 60
    assert reply.segment_delay_min_ms == 150
    assert reply.segment_delay_max_ms == 1500


def test_reply_config_from_raw():
    reply = Config.from_raw(
        {"reply": {"max_segments": 4, "segment_delay_enabled": False,
                   "segment_delay_per_char_ms": 40, "segment_delay_max_ms": 900}}
    ).reply
    assert reply.max_segments == 4
    assert reply.segment_delay_enabled is False
    assert reply.segment_delay_per_char_ms == 40
    assert reply.segment_delay_max_ms == 900


def test_reply_config_rejects_bad_values_leniently():
    reply = Config.from_raw({"reply": {"max_segments": 0, "segment_delay_per_char_ms": -5}}).reply
    assert reply.max_segments == 1
    assert reply.segment_delay_per_char_ms == 0


def test_shipped_config_example_matches_defaults():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    assert Config.load(example).reply == Config().reply


def test_webui_validate_reply():
    from rubenbot.web.config_schema import ConfigValidationError, validate_structured

    update = validate_structured({"reply": {"max_segments": "2", "segment_delay_per_char_ms": "50"}})
    assert update["reply"]["max_segments"] == 2
    assert update["reply"]["segment_delay_per_char_ms"] == 50.0
    with pytest.raises(ConfigValidationError) as info:
        validate_structured({"reply": {"max_segments": 0}})
    assert any(item["field"] == "reply.max_segments" for item in info.value.fields)
    with pytest.raises(ConfigValidationError) as info:
        validate_structured({"reply": {"segment_delay_min_ms": 900, "segment_delay_max_ms": 500}})
    assert any(item["field"] == "reply.segment_delay_max_ms" for item in info.value.fields)
