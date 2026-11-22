"""M12 度量脚本的固定口径测试。"""

import json
import sqlite3

import pytest

from tools.measure_liveliness import (
    Message,
    build_report,
    load_corpus,
    load_messages,
    main,
    measure,
    render_conclusion,
)


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


def test_new_metrics_use_explicit_common_definitions():
    messages = [
        Message("t", "c", "human", "甲甲乙", 0, 1),
        Message("t", "c", "human", "甲乙", 3, 2),
        Message("t", "c", "__bot__", "一二三四五", 5, 3),
        Message("t", "c", "__bot__", "一二", 8, 4),
        Message("t", "c", "human", "乙", 20, 5),
    ]
    session = measure(messages)["t:c"]
    human = session["human"]
    bot = session["bot"]

    assert human["message_length_percentiles"] == pytest.approx({"p50": 2, "p90": 2.8, "p95": 2.9})
    assert bot["message_length_percentiles"] == pytest.approx({"p50": 3.5, "p90": 4.7, "p95": 4.85})
    assert human["burst"]["burst_rate"] == 2 / 3
    assert bot["burst"]["burst_rate"] == 1.0
    assert human["reply_interval_distribution"]["percentiles"] == {
        "p50": 12.0,
        "p90": 12.0,
        "p95": 12.0,
    }
    assert bot["reply_interval_distribution"]["percentiles"] == {"p50": 2.0, "p90": 2.0, "p95": 2.0}
    assert human["vocabulary_repetition"] == {
        "tokens": 4,
        "repeated_tokens": 1,
        "repetition_rate": 0.25,
    }
    assert bot["vocabulary_repetition"] == {
        "tokens": 5,
        "repeated_tokens": 1,
        "repetition_rate": 0.2,
    }
    assert session["comparison"]["farthest_metric"] == "回复间隔"


def test_report_has_json_conclusion_and_corpus_loader(tmp_path):
    human_path = tmp_path / "human.json"
    bot_path = tmp_path / "bot.json"
    human_path.write_text(
        json.dumps([{"sender_id": "u", "text": "好", "ts": 1}], ensure_ascii=False)
    )
    bot_path.write_text(
        json.dumps(
            [
                {
                    "bot": {
                        "platform": "p",
                        "chat_id": "c",
                        "sender_id": "__bot__",
                        "text": "行",
                        "ts": 2,
                    }
                }
            ],
            ensure_ascii=False,
        )
    )
    messages = load_corpus([human_path, bot_path])
    report = build_report(messages)
    assert report["summary"]["human"]["messages"] == 1
    assert report["summary"]["bot"]["messages"] == 1
    assert "真人常模测量结论" in report["conclusion"]
    assert "最远" in report["conclusion"]
    json.dumps(report, ensure_ascii=False)
    assert render_conclusion(report["summary"]) == report["conclusion"]
    assert report["summary"]["bot"]["burst_rate"] is None  # 离散抽样，邻接未知


def test_summary_never_stitches_chats_people_or_interrupted_runs():
    # 时间相同按 id 排，换人、插话、换群都会切断连发。
    rows = [
        Message("t", "x", "u1", "啊", 0, 1),
        Message("t", "x", "u2", "啊", 0, 2),
        Message("t", "x", "u1", "啊", 1, 3),
        Message("t", "y", "u1", "啊", 2, 4),
        Message("t", "x", "__bot__", "好", 2, 5),
        Message("t", "x", "__tool__", "杂音", 4, 6),
        Message("t", "x", "__bot__", "好", 6, 7),
    ]
    summary = build_report(reversed(rows))["summary"]
    assert summary["human"]["messages"] == 4
    assert summary["human"]["burst_rate"] == 0
    assert summary["bot"]["burst_rate"] == 1
    assert summary["human"]["reply_interval_distribution"]["samples"] == 2
    assert summary["bot"]["reply_interval_distribution"]["samples"] == 1
    assert summary["human"]["vocabulary_repetition"]["repetition_rate"] == 0.25


def test_reply_and_burst_never_jump_over_someone_else():
    rows = [
        Message("p", "g", "__bot__", "哈", 0),
        Message("p", "g", "u", "嘿", 1),
        Message("p", "g", "__bot__", "哈", 2),
        Message("p", "g", "__bot__", "额", 7),
    ]
    bot = build_report(rows)["summary"]["bot"]
    assert bot["burst_rate"] == 0
    assert bot["reply_interval_distribution"]["samples"] == 1


def test_empty_one_sided_and_identical_samples_do_not_invent_farthest():
    for rows in (
        [],
        [Message("t", "c", "u", "", 1)],
        [Message("t", "c", "u", "嗯", 1), Message("t", "c", "__bot__", "嗯", 2)],
    ):
        report = build_report(rows)
        assert report["summary"]["comparison"]["farthest_metric"] is None
        assert report["summary"]["comparison"]["distances"]["回复间隔"] is None
        assert "真人常模测量结论" in report["conclusion"]
        json.dumps(report, allow_nan=False)


def test_vocabulary_repetition_uses_a_bounded_history():
    # 与 50 个以上其它词间隔的旧词，不算近期重复。
    text = "start " + " ".join(str(i) for i in range(50)) + " start START"
    side = build_report([Message("t", "c", "u", text, 0)])["summary"]["human"]
    assert side["vocabulary_repetition"]["repeated_tokens"] == 1


def test_read_only_sqlite_and_both_cli_formats(tmp_path, capsys):
    path = tmp_path / "test #1?.db"
    with pytest.raises(sqlite3.OperationalError):
        load_messages(path)
    assert not path.exists()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE raw_messages(id, platform, chat_id, sender_id, text, ts)")
    db.executemany(
        "INSERT INTO raw_messages VALUES (?, '样本', '群', ?, ?, ?)",
        [(1, "人", "好啊", 0), (2, "__bot__", "行", 3)],
    )
    db.commit()
    db.close()
    before = path.read_bytes()
    assert main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sessions"]["样本:群"]["human"]["tone_word_rate"] == 1
    assert main([str(path), "--text"]) == 0
    assert capsys.readouterr().out.rstrip() == report["conclusion"]
    assert path.read_bytes() == before


def test_sqlite_separates_private_chat_from_same_numbered_group(tmp_path):
    path = tmp_path / 'same-number.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE raw_messages(id, platform, chat_type, chat_id, sender_id, text, ts)')
        conn.executemany("INSERT INTO raw_messages VALUES (?, 'p', ?, '42', ?, '嗯', ?)", [
            (1, 'group', 'u', 1), (2, 'private', 'u', 2), (3, 'private', '__bot__', 5),
        ])
    report = build_report(load_messages(path))
    assert report['summary']['sessions'] == 2
    assert report['summary']['human']['burst_rate'] == 0
    assert report['sessions']['p:42']['human']['messages'] == 1
    assert report['sessions']['p:private:42']['bot']['reply_interval_distribution']['percentiles']['p50'] == 3


def test_export_without_timestamps_is_not_measured_as_if_one_second_apart(tmp_path):
    path = tmp_path / "sample.json"
    path.write_text('[{"sender_id":"a", "text":"hi"}]')
    with pytest.raises(ValueError, match="时间戳"):
        load_corpus(path)
