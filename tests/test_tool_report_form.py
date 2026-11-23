"""电脑任务汇报形态：交付成功静默，失败 / 部分完成只发一句人话。"""

import asyncio
import io
import json
from collections import deque

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.computer import failure_detail, fallback_sentence, one_sentence
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.persona import Persona


def event(text, sender="console:me", **kwargs):
    return MessageEvent("console", "group", "room", sender, "同一个昵称", text, **kwargs)


def call(name, **args):
    return {"tool_calls": [{"id": name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}


class Model:
    """策划步都判成操作委托；工具步按脚本走；说明步由 ``outcome`` 决定。"""

    def __init__(self, steps, *, deliver=False, outcome=None):
        self.steps = deque(steps)
        self.deliver = deliver
        self.outcome = outcome
        self.outcome_prompts = []

    async def complete(self, messages, **kwargs):
        task = kwargs.get("task")
        if task == "gen_outcome":
            self.outcome_prompts.append(messages)
            if self.outcome is None:
                raise RuntimeError("说明生成不可用")
            return self.outcome
        assert task == "gen_stage1", task
        prompt = messages[-1]["content"]
        if "【操作委托】" not in prompt:
            return '{"intent":"沉默","speak":false}'
        data = {"intent": "操作电脑", "basis": "guess", "speak": True, "deliver_file": self.deliver}
        return "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"

    async def chat_with_tools(self, messages, tools, *, tool_choice, **kwargs):
        if not self.steps:
            return {"content": ""}
        return self.steps.popleft()


@pytest.fixture
def make(config, store, tmp_path):
    config.computer.enabled = True
    config.database_path = str(tmp_path / "bot.db")
    config.attention.talk_value = 0

    def build(steps, **kwargs):
        llm = Model(steps, **kwargs)
        bot = RubenBot(config, store, llm, Persona("Ruben", "一个群友"))
        adapter = ConsoleAdapter(stdout=io.StringIO())
        bot.register_adapter(adapter)
        adapter.bind(bot.handle_event)
        return bot, adapter, llm

    return build


def texts(console):
    return ["".join(segment.render() for segment in segments) for _, _, segments in console.sent]


def files(console):
    return [segment.data for _, _, segments in console.sent for segment in segments
            if segment.type == "file"]


def assert_single_sentence(text, *, mentions=()):
    """恰好一句：无列表、无换行、无邀约尾巴。"""
    assert text.endswith("。")
    assert text.count("。") == 1
    assert "\n" not in text and "- " not in text and "1." not in text
    for phrase in ("如果你需要", "需要的话", "需要我", "要不要", "随时", *mentions):
        assert phrase not in text


def run(console, text):
    asyncio.run(console.dispatch(event(text, mentions_bot=True)))


def test_delivered_file_ends_silently(make, tmp_path):
    """回放 txt 场景：写文件后 send_file，群里只收到附件，没有任何汇报消息。"""
    path = tmp_path / "北京时间.txt"
    content = "北京时间 10:07"
    bot, console, llm = make([
        call("list_dir", path=str(tmp_path)),
        call("write_file", path=str(path), content=content),
        call("send_file", path=str(path)),
        # 模型仍会写一段总结：成功路径必须丢弃，不发给任何人。
        {"content": "文件已生成并发送到当前会话 📄\n- 文件名：北京时间.txt\n如果你需要其他格式，随时告诉我。"},
    ], deliver=True)

    run(console, "写个内容是现在的北京时间的 txt 文件发出来")

    assert path.read_text() == content
    assert [item["content"] for item in files(console)] == [path.read_bytes()]
    # 出站只有附件那一条，没有任何文字条。
    assert [segment.data.get("text") for _, _, segments in console.sent for segment in segments
            if segment.type == "text"] == []
    assert not llm.outcome_prompts
    # 文件已送达算完成，不进入失败说明；也没有留下「发回」之类的文字流水。
    assert not [m for m in bot.store.recent_messages("room", sender_id=BOT_SENDER_ID) if m.text.strip()]


def test_failure_speaks_exactly_one_sentence(make, tmp_path):
    """目标路径不可写：只收到一句人话说明，无列表、无邀约尾巴。"""
    bot, console, llm = make(
        [call("write_file", path=str(tmp_path / "不可写.txt"), content="x")],
        deliver=False,
        outcome="写文件那步没走通，现在什么都没改成。\n如果你需要，我可以换个路径再试。",
    )
    (tmp_path / "不可写.txt").mkdir()  # 目标是目录：写入必然失败

    run(console, "把结果写进 txt 发出来")

    assert len(console.sent) == 1
    assert_single_sentence(texts(console)[0])
    assert "写文件" in texts(console)[0]
    assert llm.outcome_prompts, "失败时应请模型用自己的口吻说一句"
    prompt = llm.outcome_prompts[0]
    assert "执行记录" in prompt[-1]["content"] and "本轮原话" in prompt[-1]["content"]
    # 说明生成只给执行记录 + 原话，不带群聊历史或记忆。
    assert len(prompt) == 2
    log = next(r for r in bot.store.list_decision_logs() if r["reason"] == "computer_task_failed")
    assert log["should_reply"]


def test_partial_completion_speaks_exactly_one_sentence(make, tmp_path):
    """读取成功、交付失败：一句说明里说清哪部分成了、哪部分没成。"""
    existing = tmp_path / "已存在.txt"
    existing.write_text("旧内容")
    bot, console, llm = make([
        call("read_file", path=str(existing)),
        call("send_file", path=str(tmp_path / "不存在.txt")),
    ], deliver=True)

    run(console, "把这个文件发出来")

    assert len(console.sent) == 1
    text = texts(console)[0]
    assert_single_sentence(text)
    assert "发文件" in text and "读文件" in text
    log = next(r for r in bot.store.list_decision_logs() if r["reason"] == "computer_task_partial")
    assert log["should_reply"]


def test_partial_without_deliverable_gets_one_sentence(make, tmp_path):
    """要求附件却没发出：模型不写说明时，兜底句也只一条，且不冒充完成。"""
    path = tmp_path / "没发.txt"
    bot, console, llm = make([
        call("write_file", path=str(path), content="x"),
        {"content": "文件写好了，已经发到群里。"},  # 无据宣称已发
    ], deliver=True)

    run(console, "写个 txt 发出来")

    assert path.read_text() == "x"
    assert not files(console)
    assert len(console.sent) == 1
    assert_single_sentence(texts(console)[0])
    assert "发文件" in texts(console)[0] and "写文件" in texts(console)[0]
    assert llm.outcome_prompts, "没交出附件时要请模型用自己的口吻说一句"


def test_outcome_generation_failure_uses_one_fallback_sentence(make, tmp_path):
    """说明生成失败时用兜底句，仍然只发一条，且不冒充完成。"""
    bot, console, _ = make([call("read_file", path=str(tmp_path / "缺.txt"))], outcome=None)

    run(console, "读一下那个文件")

    assert len(console.sent) == 1
    assert_single_sentence(texts(console)[0])


def test_query_task_answers_with_result_without_summary(make, tmp_path):
    """查询类委托（不要求附件）只用结果本身作答。"""
    path = tmp_path / "数据.txt"
    path.write_text("42")
    bot, console, llm = make([call("read_file", path=str(path)), {"content": "42"}], deliver=False)

    run(console, "看看 数据.txt 里是多少")
    assert texts(console) == ["42"]
    assert not llm.outcome_prompts


def test_query_with_side_effect_stays_silent(make, tmp_path):
    """夹带过写文件等副作用就不算纯查询：成功即静默，模型追加的总结不发。"""
    path = tmp_path / "记录.txt"
    bot, console, llm = make([
        call("read_file", path=str(path)),
        call("write_file", path=str(path), content="x"),
        {"content": "都办好了。"},
    ], deliver=False)
    path.write_text("旧")

    run(console, "把 记录.txt 处理一下")
    assert not console.sent
    assert not llm.outcome_prompts


def test_reminder_style_tool_loop_keeps_final_answer(make, tmp_path):
    """提醒、MCP 之类的普通工具循环仍照常收尾：这次改动只动操作委托。"""
    bot, console, llm = make([{"content": "好，稍后提醒你。"}], deliver=False)

    async def go():
        return await bot.generate([{"role": "user", "content": "半小时后提醒我"}], event=event("半小时后提醒我"),
                                  task="gen_stage2")

    assert asyncio.run(go()) == "好，稍后提醒你。"
    assert not console.sent


def test_command_failure_does_not_leak_output(make, tmp_path):
    """命令失败原因带 stdout/stderr：对外说明只留退出码，不转发命令输出正文。"""
    bot, console, llm = make(
        [call("run_command", command="sh -c 'echo secret-token; exit 3'")],
        deliver=False, outcome=None,
    )

    run(console, "跑一下这个命令")
    text = texts(console)[0]
    assert_single_sentence(text)
    assert "secret-token" not in text and "退出码" in text


@pytest.mark.parametrize("raw,expected", [
    ("文件已生成并发送到当前会话 📄\n- 文件名：a.txt\n- 内容：10:07\n如果你需要其他格式，随时告诉我。",
     "文件已生成并发送到当前会话 📄，文件名：a.txt，内容：10:07。"),
    ("写文件那步没走通，不过前面看目录成了。\n需要我再试试吗？", "写文件那步没走通，不过前面看目录成了。"),
    ("**没搞定**：\n1. 读文件失败\n2. 没写成\n需要的话我可以换个路径。", "没搞定：读文件失败，没写成。"),
    ("脚本写好了，\n已经跑通并发回群里。", "脚本写好了，已经跑通并发回群里。"),
    ("没成，随时喊我", "没成。"),
    ("随时可以找我。", ""),
])
def test_one_sentence_strips_lists_newlines_and_invite_tails(raw, expected):
    result = one_sentence(raw)
    assert result == expected
    if result:
        assert result.count("。") == 1 and "\n" not in result


def test_one_sentence_keeps_plain_sentence_and_length_cap():
    assert one_sentence("没发出去，因为平台不支持附件。") == "没发出去，因为平台不支持附件。"
    assert one_sentence("") == ""
    long = "第一句没成。" + "后面很多解释，" * 40
    result = one_sentence(long)
    assert result.startswith("第一句没成。") and len(result) <= 161


@pytest.mark.parametrize("done,file_sent,expected", [
    ((), False, "写文件那步没走通（权限不足），现在什么都没改成。"),
    (("list_dir",), False, "写文件那步没走通（权限不足），前面看目录成了。"),
    (("list_dir", "write_file"), True, "发文件那步没走通（平台未能发送附件），文件已经发出去了。"),
])
def test_fallback_sentence_states_step_and_state(done, file_sent, expected):
    assert fallback_sentence(
        failed_tool="write_file" if not file_sent else "send_file",
        error="操作失败：" + ("权限不足" if not file_sent else "平台未能发送附件"),
        done=done, file_sent=file_sent,
    ) == expected


@pytest.mark.parametrize("tool,error,expected", [
    ("run_command", "操作失败：命令执行失败（退出码 7）：secret-token\nstack", "命令执行失败（退出码 7）"),
    ("read_file", "操作失败：No such file or directory", "文件或目录不存在"),
    ("read_file", "操作失败：Operation not permitted", "系统不允许"),
    # 认不出来的英文错误不带进中文说明。
    ("fetch_url", "操作失败：SSL handshake failed", ""),
    ("read_file", "操作失败：权限不足", "权限不足"),
])
def test_failure_detail_sanitizes_tool_error(tool, error, expected):
    assert failure_detail(tool, error) == expected
