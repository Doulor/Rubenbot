"""清空个人数据后，已生成的摘要和旧撤销日志都不能复活原话。"""

import pytest

from rubenbot.memory.store import make_person_id


@pytest.mark.parametrize('privacy', [False, True])
def test_purge_removes_snapshots_and_derived_conversation_material(store, privacy):
    store.privacy_enabled = privacy
    who = make_person_id('qq', 'old')
    secret = '旧站台的铜铃是紫色的'
    rid = store.add_raw_message(platform='qq', chat_id='g', sender_id='old', text=secret)
    fact = store.add_memory(who, secret)
    entry = store.apply_changes(action='drop', delete_ids=[fact.id], reason=secret)
    assert entry.before and entry.person_id is None  # 老快照不一定有顶层人物标签
    store.set_conversation_summary('qq', 'g', secret, rid, chat_type='group', source_ids=[rid])
    store.set_group_auto_summary('qq', 'g', secret, rid)
    store.set_group_manual_note('qq', 'g', '管理员的群备注')
    store.add_decision_log(platform='qq', chat_id='g', sender_id='old', text=secret)
    other = store.add_memory('qq:other', '无关的话')
    other_entry = store.apply_changes(action='drop', delete_ids=[other.id], person_id='qq:other')

    counts = store.purge_person(who)
    assert counts['summaries'] == counts['raw_messages'] == counts['snapshots'] == counts['decisions'] == 1
    assert store.get_log(entry.id) is None and not store.restore(entry.id)
    assert store.restore(other_entry.id)
    assert store.get_conversation_summary('qq', 'g', chat_type='group') is None
    profile = store.get_group_profile('qq', 'g')
    assert not profile.auto_summary and profile.manual_note == '管理员的群备注'
    assert not store.list_decision_logs() and not store.list_memories(person_id=who)
