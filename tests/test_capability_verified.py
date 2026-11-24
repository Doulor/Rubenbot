"""能力索引的正反例；不发网络请求，也不为探测而建 job。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from rubenbot.expression import CapabilityNote, capability_index, parse_plan
from rubenbot.gateway.catalog import ActionCatalog
from rubenbot.linkparse.service import LinkContext, LinkResult, capability_note
from rubenbot.verification import verify
from test_plugin_disabled import group_song_request, make_bot as music_bot
from test_reminder import make_bot, private, reminder_message
from test_web_search import ToolLLM, make_searcher, tavily_handler


def test_index_and_selection_require_evidence():
    claim = CapabilityNote('记提醒', '自述', '说明')
    assert capability_index((claim, replace(claim, evidence=(' ',)))) == ''
    off = CapabilityNote('点歌', '还没开', '说明', available=False)
    ok = replace(claim, title='看链接', evidence=('link:read=1/1',))
    notes = (claim, off, ok)
    index = capability_index(notes)
    assert '〔看链接〕' in index and 'link:read=1/1' in index
    assert '点歌还没开' in index and '记提醒' not in index
    plan = parse_plan('{"intent":"短附和","capabilities":["看链接","点歌","记提醒"]}', '短句', [], notes)
    assert plan.capabilities == ('看链接',)


def test_plugin_needs_dispatchable_entry_and_grants(tmp_path):
    _, manager, _, adapter, store = music_bot(tmp_path, enabled=['music'], fast='')
    plugin = manager.plugins['music']
    try:
        def notes():
            return tuple(manager.capability_notes(group_song_request(), adapter))

        assert 'entry:on_message' in notes()[0].evidence
        assert '〔点歌〕' in capability_index(notes())
        manager.permissions.set_permissions('music', [])
        assert '〔点歌〕' not in capability_index(notes())
        manager.permissions.set_permissions('music', plugin.manifest.permissions)
        # 两个行为入口时不猜；显式指到已注册的那个才出说明。
        plugin.before_reply_hooks.append(lambda *_: None)
        assert notes() == ()
        plugin.capability_notes[0].entry = plugin.message_hooks[0]
        assert 'entry:on_message' in notes()[0].evidence
        # 即便有另一个正常入口，也不能给已失效的这项背书。
        plugin.message_hooks[:] = [None]
        assert notes() == ()
        plugin.enabled = False
        assert '点歌还没开' in capability_index(notes())
    finally:
        store.close()


def test_gateway_requires_matching_catalog_and_e2e():
    entry = ActionCatalog.load('snowluma').get('delete_msg')
    assert 'D′5' in entry.capability_note.evidence[0]
    assert '〔撤回消息〕' in capability_index((entry.capability_note,))
    raw = {'name': 'delete_msg', 'capability_note': {'title': '空头承诺', 'summary': '自述', 'detail': '说明'}}
    for adapter, item in [('other', raw), ('snowluma', dict(raw, name='set_msg_emoji_like')),
                          ('snowluma', dict(raw, risk='denied'))]:
        catalog = ActionCatalog.from_data(adapter, [item])
        assert catalog.get(item['name']).capability_note is None


def test_link_counts_only_readable_results():
    failed = LinkResult('https://example.invalid', error='没有正文')
    context = LinkContext([failed], delivery='planner')
    assert capability_index((capability_note(context),)) == ''
    assert context.index_payload()[0]['状态'] == '抓不到'
    context.results.append(LinkResult('https://example.org', fields={'标题': '已读到的标题'}))
    note = capability_note(context)
    assert note.evidence == ('link:read=1/2',)
    assert '读到了 1 个' in capability_index((note,))


@pytest.fixture
def wired_core_notes(monkeypatch):
    """在原有检查的成功分支模拟 SPEC 中待合入的 evidence 参数，不替换任何检查。"""
    import rubenbot.core as core

    evidence = {'记提醒': ('tool:set_reminder', 'pending:below-limit'),
                '搜网': ('web_search:result-page-read',), '读原文': ('web_search:result-page-read',)}

    def checked(title, summary, detail, **kwargs):
        kwargs.setdefault('evidence', evidence.get(title, ()))
        return CapabilityNote(title, summary, detail, **kwargs)

    monkeypatch.setattr(core, 'CapabilityNote', checked)


def test_first_job_and_planner_index_agree_after_wiring(store, wired_core_notes):
    llm = ToolLLM([reminder_message({'text': '吃药', 'delay_minutes': 1}), {'content': '好'}])
    bot, _ = make_bot(store, llm)
    bot.config.reminder.max_pending_per_chat = 1
    event = private()
    assert not store.list_scheduled_jobs(status='pending')
    asyncio.run(bot.handle_event(event))
    assert [j.payload['text'] for j in store.list_scheduled_jobs(status='pending')] == ['吃药']
    assert '〔记提醒〕' not in capability_index(bot.capability_notes(event))
    planner = llm.stages['gen_stage1'][-1]['content']
    assert '〔记提醒〕' in planner, 'core.py:2703 须传递工具注册和会话余量的证据；见 SPEC 接线 diff'
    assert 'pending:below-limit' in planner


def test_search_requires_successful_lookup_and_page_after_wiring(store, wired_core_notes, monkeypatch):
    llm = ToolLLM()
    bot, _ = make_bot(store, llm)
    searcher, _ = make_searcher(tavily_handler(), cooldown=0)
    bot.searcher = searcher
    event = private()

    async def check():
        try:
            for page, expected in [('', False), ('原文。', True), ('', False)]:
                monkeypatch.setattr('rubenbot.verification.fetch_page', AsyncMock(return_value=('https://example.org', page)))
                await verify(searcher, llm, question='测试', query='测试', count=1, chat_key='recording:u1')
                index = capability_index(bot.capability_notes(event))
                assert ('〔搜网〕' in index) == ('〔读原文〕' in index) == expected
        finally:
            await searcher.close()

    asyncio.run(check())
