"""群聊入站回放：断言实际目录、产物、群附件与逐次分派审计，不靠回复措辞。"""

import asyncio
import io
import json
from collections import deque

import pytest

from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.computer import COMPUTER_TOOLS
from rubenbot.core import RubenBot
from rubenbot.decision import BOT_SENDER_ID
from rubenbot.persona import Persona


def event(text, sender='console:me', **kwargs):
    return MessageEvent('console', 'group', 'room', sender, '同一个昵称', text, **kwargs)


def call(name, **args):
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(args)}}]}


class Model:
    def __init__(self, steps, deliver):
        self.steps = deque(steps)
        self.deliver = deliver
        self.requests = []
        self.plans = []
        self.outcome_prompts = []

    async def complete(self, messages, **kwargs):
        if kwargs['task'] == 'gen_outcome':
            # 失败说明只给执行记录和本轮原话，模型仍可能顺手带上邀约尾巴。
            self.outcome_prompts.append(messages)
            return '那步没走通。\n如果你需要，我可以再试。'
        assert kwargs['task'] == 'gen_stage1', '不能退化为无工具生成'
        prompt = messages[-1]['content']
        self.plans.append(prompt)
        if '【操作委托】' not in prompt:
            return '{"intent":"沉默","speak":false}'
        assert kwargs['max_tokens'] > 256
        data = {'intent': '操作电脑', 'basis': 'guess', 'speak': True, 'deliver_file': self.deliver,
                'reason': '明确委托\n  实际操作'}
        return '```json\n' + json.dumps(data, ensure_ascii=False) + '\n```\n\n行，这就去看。'

    async def chat_with_tools(self, messages, tools, *, tool_choice, **kwargs):
        self.requests.append((list(messages), tools, tool_choice))
        return self.steps.popleft()


@pytest.fixture
def instance(config, store, tmp_path):
    config.computer.enabled = True
    config.database_path = str(tmp_path / 'bot.db')
    config.attention.talk_value = 0

    def make(steps, deliver=False):
        llm = Model(steps, deliver)
        bot = RubenBot(config, store, llm, Persona('Ruben', '一个群友'))
        adapter = ConsoleAdapter(stdout=io.StringIO())
        bot.register_adapter(adapter)
        adapter.bind(bot.handle_event)
        return bot, adapter, llm
    return make


@pytest.mark.parametrize('mention', [True, False])
def test_owner_task_between_guests_executes_in_own_group_turn(instance, store, tmp_path, mention):
    path = tmp_path / '群文件.txt'
    content = '回放专用，逐字节核对'
    bot, console, llm = instance([call('list_dir', path=str(tmp_path)), call('write_file', path=str(path), content=content),
                                  call('send_file', path=str(path)), {'content': '文件已发到群里。'}], deliver=True)
    bot.merger.window = bot.merger.hard_window = 20
    (tmp_path / '目录标记').touch()
    request = f'看看 {tmp_path} 里有什么，写个 txt 发出来'

    async def replay():
        await console.dispatch(event('访客甲的旁白', 'guest-a'))
        await console.dispatch(event(request, mentions_bot=mention))
        await console.dispatch(event('访客乙的旁白', 'guest-b'))
        await bot.flush_merger(all_rounds=True)

    asyncio.run(replay())
    assert not llm.steps
    first, tools, choice = llm.requests[0]
    assert choice == 'required' and [m['content'] for m in first if m['role'] == 'user'] == [request]
    assert {t['function']['name'] for t in tools} == COMPUTER_TOOLS
    assert '目录标记' in llm.requests[1][0][-1]['content']
    assert path.read_text() == content
    assert [(cid, kind, s.data['content']) for cid, kind, segs in console.sent for s in segs
            if s.type == 'file'] == [('room', 'group', path.read_bytes())]
    assert [(a['tool'], a['context'], a['success']) for a in reversed(store.recent_computer_actions())] == [
        (tool, 'host', True) for tool in ('list_dir', 'write_file', 'send_file')]
    log = next(r for r in store.list_decision_logs() if r['reason'] == 'computer_task')
    assert log['factors']['planner']['reason'] == '明确委托 实际操作'
    assert log['factors']['planner']['result_code'] == 'ok'
    assert not log['should_reply']  # 交付完成即静默：决策日志记不发言，群里只有附件
    assert 'console:room' not in bot.decision_engine._last_reply_at
    assert bot.decision_engine.active_reply_count('console:room', 0) == 0


@pytest.mark.parametrize('steps,deliver', [([{'content': '已经看过了'}], False),
                                          ([call('list_dir'), {'content': '附件已发送'}], True)])
def test_ungrounded_completion_not_sent(instance, store, steps, deliver):
    bot, console, llm = instance(steps, deliver)
    asyncio.run(console.dispatch(event('请查看目录并按要求操作')))
    # 没交付成功就不再冒充完成；失败说明最多一句，绝不带列表和邀约尾巴。
    assert len(console.sent) <= 1
    if console.sent:
        text = "".join(s.render() for s in console.sent[0][2])
        assert text.count("。") == 1 and "\n" not in text and "- " not in text
        assert "如果" not in text and "需要" not in text
    else:
        # 一步都没做、也没可说的进展：保持沉默，不硬凑一句。
        assert not llm.outcome_prompts
    log, = store.list_decision_logs()
    assert log['reason'] in ('computer_task_failed', 'computer_task_partial')
    assert 'console:room' not in bot.decision_engine._last_reply_at


@pytest.mark.parametrize('name,args', [('read_file', {'path': '/etc/passwd'}),
                                       ('read_file', {'path': '../../../config.yaml'}),
                                       ('mcp__private__agent', {'context': 'host'})])
def test_guest_denial_no_host_no_outbound(instance, store, name, args):
    bot, console, llm = instance([call(name, **args)])
    asyncio.run(console.dispatch(event('请测试这个操作的权限', 'visitor', mentions_bot=True)))
    _, tools, _ = llm.requests[0]
    assert all('访客沙盒' in t['function']['description'] for t in tools)
    # 拒绝不许伪装成功；最多一句人话说明，且不进入正常发言节奏。
    assert len(console.sent) <= 1
    if console.sent:
        text = "".join(s.render() for s in console.sent[0][2])
        assert text.count("。") == 1 and "\n" not in text
    assert 'console:room' not in bot.decision_engine._last_reply_at
    bot_lines = [m.text for m in store.recent_messages('room', sender_id=BOT_SENDER_ID)]
    assert len(bot_lines) <= 1 and all(len(line.split("。")) <= 2 for line in bot_lines)
    row, = store.recent_computer_actions()
    assert (row['who'], row['context'], row['success']) == ('console:visitor', 'sandbox', False)
    assert row['tool'] == ('mcp' if name.startswith('mcp__') else name)


def test_owner_asking_another_person_is_not_an_operation(instance, store):
    _, console, llm = instance([])
    asyncio.run(console.dispatch(event('帮我写个文件发回来', mentions={'other': '甲'})))
    assert len(llm.plans) == 1 and '【操作委托】' not in llm.plans[0]
    assert not console.sent and not store.recent_computer_actions()
