"""M12 度量脚本的固定口径测试。"""

from tools.measure_liveliness import Message, measure


def test_measure_reports_message_shape_and_bot_density():
    messages = [
        Message("test", "g1", "u1", "好", 0, 1),
        Message("test", "g1", "__bot__", "第一句。", 60, 2),
        Message("test", "g1", "__bot__", "第二句，补充", 120, 3),
        Message("test", "g1", "u1", "这是一个超过十个字的真人消息", 180, 4),
        Message("test", "g1", "__bot__", "第三条", 240, 5),
        Message("test", "g1", "u1", "嗯", 700, 6),
    ]

    result = measure(messages)["test:g1"]
    bot = result["bot"]
    human = result["human"]
    density = result["bot_density"]

    assert bot["messages"] == 3
    assert bot["average_chars"] == (4 + 6 + 3) / 3
    assert bot["short_le_10_ratio"] == 1.0
    assert bot["average_commas"] == 1 / 3
    assert bot["tone_word_rate"] == 0.0
    assert bot["sentence_length_burstiness"] > 0
    assert human["messages"] == 3
    assert human["short_le_10_ratio"] == 2 / 3
    assert density["bot_messages"] == 3
    assert density["bot_max_in_10_minute_window"] == 3
    assert density["bot_max_consecutive_messages"] == 2


def test_measure_isolated_sessions_and_tone_rate():
    messages = [
        Message("test", "a", "u", "啊，行", 0),
        Message("test", "a", "u", "普通", 1),
        Message("test", "a", "__bot__", "短", 2),
        Message("test", "b", "__bot__", "只有一条", 0),
    ]
    result = measure(messages)
    assert set(result) == {"test:a", "test:b"}
    assert result["test:a"]["human"]["tone_word_rate"] == 0.5
    assert result["test:b"]["bot_density"]["bot_messages_per_10_minutes"] == 1.0
