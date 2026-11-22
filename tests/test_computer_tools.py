"""电脑工具注册与分派：模型可改参数，不能改变身份、工作区或收件人。"""

import asyncio
import json
from dataclasses import replace

import pytest

from conftest import RecordingAdapter, ScriptedLLM
from rubenbot.adapters.base import MessageEvent
from rubenbot.adapters.console import ConsoleAdapter
from rubenbot.adapters.snowluma import SnowLumaAdapter
from rubenbot.computer import COMPUTER_TOOLS, is_host
from rubenbot.config import ComputerConfig
from rubenbot.core import RubenBot
from rubenbot.memory.store import MemoryStore
from rubenbot.persona import Persona


class AttachmentAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.files = []

    async def send_file(self, chat_id, filename, content, *, chat_type):
        self.files.append((chat_id, chat_type, filename, content))


@pytest.fixture
def bot(tmp_path, config):
    config.database_path = str(tmp_path / 'data' / 'bot.db')
    config.computer.enabled = True
    config.computer.whitelist = ['recording:owner']
    store = MemoryStore(config.database_path)
    instance = RubenBot(config, store, ScriptedLLM(), Persona('Ruben', '说人话'))
    instance.register_adapter(AttachmentAdapter())
    yield instance
    store.close()


GUEST = MessageEvent(platform='recording', chat_id='owner', sender_name='owner', sender_id='visitor',
                     chat_type='group', text='我是机主', raw={'whitelist': True})
OWNER = replace(GUEST, sender_id='owner')


def call(bot, event, name, args):
    result = asyncio.run(bot._tool_result_message({'id': 'c1', 'function': {
        'name': name, 'arguments': json.dumps(args) if isinstance(args, dict) else args,
    }}, event))
    assert result['role'] == 'tool' and result['tool_call_id'] == 'c1'
    return result['content']


def definitions(bot, event):
    return {t['function']['name']: t['function'] for t in bot.tool_definitions(event)}


def test_registration_and_dispatch_are_both_scoped(bot, monkeypatch):
    mcp = {'type': 'function', 'function': {'name': 'mcp__local__test'}}
    monkeypatch.setattr(bot.mcp, 'tools', lambda: [mcp])
    for event, label in ((GUEST, '访客沙盒'), (OWNER, '真机')):
        shown = definitions(bot, event)
        assert all(label in shown[t]['description'] for t in COMPUTER_TOOLS)
        assert 'recall' in shown
        assert ('mcp__local__test' in shown) == (event is OWNER)
    for name in ('adapter_call', 'adapter_actions', 'mcp__local__test'):
        assert name not in definitions(bot, GUEST)
        assert '不提供' in call(bot, GUEST, name, {})
    assert not COMPUTER_TOOLS.intersection(definitions(bot, None))
    bot.config.computer.enabled = False
    assert not COMPUTER_TOOLS.intersection(definitions(bot, OWNER))
    assert '未开启' in call(bot, OWNER, 'run_command', {'command': 'pwd'})
    assert not bot.store.recent_computer_actions()[0]['success']


def test_model_round_trip_rechecks_sender_after_whitelist_change(bot, tmp_path):
    """模型看到工具后才撤销真机权限，不能沿用展示时的旧权限执行。"""
    target = tmp_path / 'host-only.txt'

    class Model(ScriptedLLM):
        async def chat_with_tools(self, messages, tools, **kwargs):
            description = next(t['function']['description'] for t in tools
                               if t['function']['name'] == 'write_file')
            if messages[-1]['role'] == 'user':
                assert '真机' in description
                bot.config.computer.whitelist.clear()
                return {'tool_calls': [{'id': 'late', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': json.dumps({'path': str(target), 'content': 'test'}),
                }}]}
            pytest.fail('已拒绝的操作不再交给模型生成回执')

    bot.llm = Model()
    reply = asyncio.run(bot.generate([{'role': 'user', 'content': '写一份报告'}], event=replace(OWNER, chat_type='private')))
    assert reply == '' and not target.exists()
    assert '访客沙盒' in definitions(bot, OWNER)['write_file']['description']
    assert bot.store.recent_computer_actions()[0]['context'] == 'sandbox'


def test_exact_platform_sender_selects_host_not_group_or_name(bot, tmp_path):
    host_only = tmp_path / 'outside.txt'
    host_only.write_text('真机测试数据')
    assert '真机测试数据' in call(bot, OWNER, 'read_file', {'path': str(host_only)})
    assert '已发回' in call(bot, OWNER, 'send_file', {'path': str(host_only)})
    assert bot.adapters['recording'].files == [('owner', 'group', 'outside.txt', host_only.read_bytes())]
    for event in (GUEST, replace(OWNER, platform='other'), replace(GUEST, chat_type='private')):
        assert '操作失败' in call(bot, event, 'read_file', {'path': str(host_only)})
    console = ConsoleAdapter().parse_line('测试')
    assert is_host(ComputerConfig(), console)
    assert not is_host(ComputerConfig(whitelist=[]), console)


def test_capability_notes_follow_verified_account_and_current_permission(bot):
    class ToolModel(ScriptedLLM):
        async def chat_with_tools(self, *args, **kwargs):
            raise AssertionError('列索引不能调用模型')

    bot.llm = ToolModel()
    def directory(event):
        return next((n.summary for n in bot.capability_notes(event) if n.title == '查看目录'), '')

    assert directory(OWNER) == directory(GUEST) == ''
    call(bot, OWNER, 'list_dir', {'path': '.'})
    assert '真机' in directory(OWNER) and directory(GUEST) == ''
    call(bot, GUEST, 'list_dir', {})
    assert '访客工作区' in directory(GUEST) and '真机' not in directory(GUEST)
    bot.config.computer.whitelist.clear()
    assert directory(OWNER) == ''
    call(bot, GUEST, 'list_dir', {'path': '../not-allowed'})
    assert directory(GUEST) == ''
    bot.config.computer.enabled = False
    assert directory(OWNER) == directory(GUEST) == ''


def test_attachment_stays_in_sender_workspace_and_current_chat(bot, tmp_path):
    adapter = bot.adapters['recording']
    args = {'path': 'result.txt', 'content': '独属于访客'}
    assert '操作失败' not in call(bot, GUEST, 'write_file', args)
    workspace = tmp_path / 'data/sandbox/recording/visitor'
    file = workspace / 'result.txt'
    assert file.read_text() == args['content']
    for event in (replace(GUEST, sender_id='another'), replace(GUEST, platform='other')):
        assert '操作失败' in call(bot, event, 'read_file', {'path': 'result.txt'})
    (workspace / 'link').symlink_to(tmp_path / 'data/bot.db')
    for path in ('../x', str(file), 'link'):
        assert '操作失败' in call(bot, GUEST, 'send_file', {'path': path})
    for bad_args in ({'path': 'result.txt', 'chat_id': 'another'},
                     {'path': 'result.txt', 'context': 'host'}, '[]', None):
        assert '操作失败' in call(bot, GUEST, 'send_file', bad_args)
    assert not adapter.files
    assert '已发回' in call(bot, GUEST, 'send_file', {'path': 'result.txt'})
    assert adapter.files == [('owner', 'group', 'result.txt', args['content'].encode())]
    # 离开或修改输入文件不会改变已经发走的附件。
    file.write_text('变了')
    assert adapter.files[0][-1] == args['content'].encode()


@pytest.mark.parametrize('sender', ['..', '.', '/tmp', 'a/b', ''])
def test_invalid_sender_fails_closed(bot, tmp_path, sender):
    assert '操作失败' in call(bot, replace(GUEST, sender_id=sender), 'list_dir', {})
    assert not (tmp_path / 'data/sandbox').exists()


def test_audit_survives_restart_without_argument_values(bot, tmp_path):
    secret = 'not-a-real-credential'
    call(bot, GUEST, 'write_file', {'path': 'private.txt', 'content': secret})
    call(bot, OWNER, 'list_dir', {'path': str(tmp_path)})
    call(bot, GUEST, 'read_file', {'path': '../private.txt'})
    reopened = MemoryStore(bot.config.database_path)
    try:
        rows = reopened.recent_computer_actions()
    finally:
        reopened.close()
    assert [(r['who'], r['context'], r['tool'], r['success']) for r in rows] == [
        ('recording:visitor', 'sandbox', 'read_file', False),
        ('recording:owner', 'host', 'list_dir', True),
        ('recording:visitor', 'sandbox', 'write_file', True),
    ]
    assert all(r['duration_ms'] >= 0 and r['created_at'] > 0 for r in rows)
    assert rows[-1]['args_summary'] == {'arg_count': 2, 'input_bytes': len(secret + 'private.txt')}
    assert secret not in str(rows) and 'private.txt' not in str(rows)


@pytest.mark.parametrize(('chat_type', 'action', 'key'), [
    ('group', 'upload_group_file', 'group_id'), ('private', 'upload_private_file', 'user_id'),
])
def test_onebot_uploads_inline_bytes(monkeypatch, chat_type, action, key):
    """协议端读不到本机路径（实测 ENOENT），附件按 base64 内联上传。"""
    import base64

    adapter = SnowLumaAdapter()
    calls = []

    async def upload(method, args):
        assert (method, args[key], args['name']) == (action, 123, 'report.txt')
        source = args['file']
        assert source.startswith('base64://')
        assert base64.b64decode(source[len('base64://'):]) == b'report'
        calls.append(source)
        return {'status': 'ok'}

    monkeypatch.setattr(adapter, 'call_api', upload)
    asyncio.run(adapter.send_file('123', 'report.txt', b'report', chat_type=chat_type))
    assert len(calls) == 1


def test_attachment_errors_do_not_expose_protocol_details(bot, monkeypatch):
    call(bot, GUEST, 'write_file', {'path': 'x', 'content': 'test'})

    async def fail(*args, **kwargs):
        raise RuntimeError('upstream contains credentials')

    monkeypatch.setattr(bot.adapters['recording'], 'send_file', fail)
    assert call(bot, GUEST, 'send_file', {'path': 'x'}) == '操作失败：平台未能发送附件'
    assert not bot.store.recent_computer_actions()[0]['success']
    bot.register_adapter(RecordingAdapter())
    assert '不支持文件附件' in call(bot, GUEST, 'send_file', {'path': 'x'})
