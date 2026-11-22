"""记忆读取侧：T0 索引（别名/钩子/缓存）、T1 预拉取（优先级/预算/模糊时间）、
跨会话摘要（24h 窗口、私聊来源隔离）、recall（T2）。"""

import asyncio
import json
import time

import httpx
import pytest

from rubenbot.config import LLMConfig, MemoryConfig
from rubenbot.llm import LLMClient, ToolUnsupported
from rubenbot.privacy import PrivacyGate
from rubenbot.memory.retrieve import (
    CROSS_SESSION_HEADER,
    MEMORY_HEADER,
    SELF_HEADER,
    MemberIndex,
    MemberRow,
    MemoryReader,
    estimate_tokens,
    format_relative_time,
    is_self_inquiry,
)
from rubenbot.memory.store import MemoryStore, ProfileLine

NOW = 1_795_000_000.0


class FakeLLM:
    """审查模型：默认一律放行（高置信）。"""

    def __init__(self, verdict="allow", confidence=0.9, raises=None):
        self.verdict = verdict
        self.confidence = confidence
        self.raises = raises
        self.calls = []

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages})
        if self.raises is not None:
            raise self.raises
        return json.dumps(
            {
                "items": [
                    {"id": 0, "verdict": self.verdict, "confidence": self.confidence},
                ]
            },
            ensure_ascii=False,
        )


class EchoGateLLM(FakeLLM):
    """按请求里出现的候选数逐项放行。"""

    async def complete(self, messages, *, role="chat", temperature=None, json_mode=False):
        self.calls.append({"role": role, "messages": messages})
        text = messages[-1]["content"]
        count = text.count("- id=")
        return json.dumps(
            {"items": [{"id": i, "verdict": self.verdict, "confidence": self.confidence} for i in range(count)]},
            ensure_ascii=False,
        )


@pytest.fixture()
def store():
    s = MemoryStore(":memory:", privacy_enabled=True)
    yield s
    s.close()


def seed_person(store, uid="u1", name="阿花", stable=(), status=(), hooks=(), now=NOW):
    person_id = f"console:{uid}"
    store.upsert_person("console", uid, name, now=now)
    if stable or status or hooks:
        stable_lines = [ProfileLine(text=text, evidence_refs=[]) for text in stable]
        status_lines = [ProfileLine(text=text, evidence_refs=[]) for text in status]
        store.save_profile(
            person_id,
            stable_lines=stable_lines,
            status_lines=status_lines,
            status_expires_at=now + 7 * 86400 if status else None,
            hooks=list(hooks),
            now=now,
        )
    return person_id


def reader(store, config=None, llm=None) -> MemoryReader:
    config = config or MemoryConfig()
    gate = PrivacyGate(store, llm or EchoGateLLM(), config)
    return MemoryReader(store, llm, config, gate=gate, now_fn=lambda: NOW)


# ----------------------------------------------------------------------
# T0 索引
# ----------------------------------------------------------------------


def test_index_rows_identity_and_hooks(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师", "TA 住在杭州"], hooks=["爬山", "猫"])
    seed_person(store, "u2", "阿草", stable=["TA 是学生"])
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", sender_name="阿花", text="hi", ts=NOW - 60)
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u2", sender_name="阿草", text="hi", ts=NOW - 120)
    index = MemberIndex(store, MemoryConfig(), now_fn=lambda: NOW)
    rows = index.get("console", "g1")
    assert [row.name for row in rows] == ["阿花", "阿草"]  # 按最近活跃排序
    assert rows[0].identity == "后端工程师"
    assert "爬山" in rows[0].hooks
    rendered = rows[0].render()
    assert rendered.startswith("阿花｜后端工程师｜话题：")
    assert "还不太熟" in MemberRow("console:u9", "路人").render()


def test_index_only_recent_active_and_cap(store, monkeypatch):
    config = MemoryConfig(index_active_days=30, index_max_members=2)
    for i in range(4):
        uid = f"u{i}"
        store.upsert_person("console", uid, f"成员{i}", now=NOW)
        store.add_raw_message(
            platform="console", chat_id="g1", sender_id=uid, sender_name=f"成员{i}", text="x", ts=NOW - i * 60
        )
    # 一个 40 天前才活跃的人不该进索引
    store.upsert_person("console", "old", "老成员", now=NOW)
    store.add_raw_message(platform="console", chat_id="g1", sender_id="old", sender_name="老成员", text="x", ts=NOW - 40 * 86400)
    index = MemberIndex(store, config, now_fn=lambda: NOW)
    rows = index.get("console", "g1")
    assert [row.name for row in rows] == ["成员0", "成员1"]


def test_index_excludes_norecord(store):
    seed_person(store, "u1", "阿花")
    store.set_privacy_mode("console:u1", "norecord")
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", sender_name="阿花", text="x", ts=NOW - 10)
    index = MemberIndex(store, MemoryConfig(), now_fn=lambda: NOW)
    assert index.get("console", "g1") == []


def test_index_cache_ttl_and_profile_invalidation(store):
    seed_person(store, "u1", "阿花", stable=["TA 是老师"])
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", sender_name="阿花", text="x", ts=NOW - 10)
    clock = [NOW]
    config = MemoryConfig(index_ttl_seconds=600)
    index = MemberIndex(store, config, now_fn=lambda: clock[0])
    first = index.get("console", "g1")
    assert first and first[0].identity == "老师"
    # 新人进来但缓存未过期：还是旧结果
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u2", sender_name="阿草", text="x", ts=clock[0])
    assert len(index.get("console", "g1")) == 1
    # 画像更新直接让缓存失效（新活跃的排在前面）
    seed_person(store, "u1", "阿花", stable=["TA 是律师"], now=clock[0] + 1)
    rows = index.get("console", "g1")
    assert len(rows) == 2
    assert {row.name: row.identity for row in rows}["阿花"] == "律师"
    # TTL 到期也会重建
    clock[0] += 601
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u3", sender_name="阿三", text="x", ts=clock[0])
    assert len(index.get("console", "g1")) == 3


# ----------------------------------------------------------------------
# T1 预拉取
# ----------------------------------------------------------------------


def test_speaker_profile_injected(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"], status=["最近在准备考试"])
    block = asyncio.run(
        reader(store).assemble(
            text="最近好累", person_id="console:u1", platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    text = block.render()
    assert MEMORY_HEADER in text
    assert "后端工程师" in text
    assert "近况：最近在准备考试" in text


def test_expired_status_not_injected(store):
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"], status=["最近在准备考试"])
    store.save_profile(
        person_id,
        stable_lines=[ProfileLine(text="TA 是一名后端工程师", evidence_refs=[])],
        status_lines=[ProfileLine(text="最近在准备考试", evidence_refs=[])],
        status_expires_at=NOW - 1,
        now=NOW - 86400,
    )
    block = asyncio.run(
        reader(store).assemble(
            text="hi", person_id=person_id, platform="console", chat_id="p1", chat_type="private", now=NOW
        )
    )
    assert "准备考试" not in block.render()
    assert "后端工程师" in block.render()


def test_mentioned_by_alias_pulls_profile(store):
    seed_person(store, "u2", "阿草", stable=["TA 喜欢弹吉他"], hooks=["吉他"])
    block = asyncio.run(
        reader(store).assemble(
            text="阿草最近怎么样", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    text = block.render()
    assert "阿草" in text
    assert "弹吉他" in text


def test_mentioned_by_hook(store):
    seed_person(store, "u2", "阿草", stable=["TA 喜欢弹吉他"], hooks=["吉他"])
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u2", sender_name="阿草", text="x", ts=NOW - 5)
    block = asyncio.run(
        reader(store).assemble(
            text="最近在学吉他", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "弹吉他" in block.render()


def test_mentioned_by_at(store):
    seed_person(store, "999", "小红", stable=["TA 养了一只猫"])
    block = asyncio.run(
        reader(store).assemble(
            text="@999 你看", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "养了一只猫" in block.render()


def test_timeline_hits_are_vague(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    store.add_event(
        "console:u1", "TA 周末去爬山了，去的是香山", ts=NOW - 2 * 86400,
        source_chat="console:g1", audience="group:g1", now=NOW,
    )
    block = asyncio.run(
        reader(store).assemble(
            text="周末想爬山", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    text = block.render()
    assert "香山" in text
    assert "前天" in text
    # 不给精确时间戳
    assert time.strftime("%Y-%m-%d", time.localtime(NOW - 2 * 86400)) not in text


def test_timeline_mismatch_not_injected(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    store.add_event("console:u1", "TA 周末去爬山了，去的是香山", ts=NOW - 86400, audience="group:g1", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="今天代码写不完了", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "香山" not in block.render()


def test_facts_only_true_hits_and_distilled_skipped(store):
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    hit = store.add_memory(person_id, "TA 喜欢喝美式咖啡", audience="private", now=NOW)
    store.add_memory(person_id, "TA 的表弟在加拿大", audience="private", now=NOW)
    # 把「美式咖啡」这条提炼进了画像 → 不再注入
    store.save_profile(
        person_id,
        stable_lines=[
            ProfileLine(text="TA 是一名后端工程师", evidence_refs=[]),
            ProfileLine(text="TA 爱喝美式", evidence_refs=[f"memory:{hit.id}"]),
        ],
        status_lines=[],
        now=NOW,
    )
    block = asyncio.run(
        reader(store).assemble(
            text="来杯美式", person_id=person_id, platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    text = block.render()
    assert "美式咖啡" not in text  # 已进画像
    assert "加拿大" not in text  # 没命中就不要（不做时间兜底）


def test_fact_hit_injected(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_memory(person_id, "TA 喜欢喝美式咖啡", audience="private", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="来杯美式", person_id=person_id, platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    assert "美式咖啡" in block.render()


def test_budget_cuts_facts_then_timeline_keeps_profile(store):
    config = MemoryConfig(inject_budget_tokens=120)
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    for i in range(10):
        store.add_memory(person_id, f"TA 喜欢喝美式咖啡，第 {i} 家店都去过", audience="group:g1", now=NOW)
        store.add_event(
            person_id, f"TA 又去喝美式咖啡了，第 {i} 次", ts=NOW - i * 3600, audience="group:g1", now=NOW
        )
    block = asyncio.run(
        reader(store, config).assemble(
            text="美式咖啡", person_id=person_id, platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert block.trimmed > 0
    text = block.render()
    assert "后端工程师" in text  # 画像核心不动
    # 事实先被砍，剩下的里事实条数应当少于时间线
    fact_lines = [line for title, lines in block.sections if title.startswith("你记得的事") for line in lines]
    assert len(fact_lines) <= 1


def test_gate_withheld_not_rendered(store):
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    store.add_memory(person_id, "TA 在医院做检查", audience="private", sensitivity="sealed", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="最近去医院做检查", person_id=person_id, platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    text = block.render()
    assert "医院" not in text  # 封存项永远不跨场合
    assert block.withheld >= 1
    assert "后端工程师" in text


def test_gate_high_sensitivity_queued_on_injection(store):
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    store.add_event(
        person_id, "TA 去医院做检查", ts=NOW - 3600, audience="private", sensitivity="high", now=NOW
    )
    block = asyncio.run(
        reader(store).assemble(
            text="最近去医院做检查", person_id=person_id, platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "医院" not in block.render()
    asks = store.list_pending_asks(person_id)
    assert asks and asks[0].item_ref.endswith("@group")


def test_profile_line_sensitivity_from_text(store):
    """画像被抽象后敏感度标注丢了，按文字仍要高敏感处理（防洗白）。"""
    person_id = seed_person(store, "u1", "阿花", stable=["TA 最近在看心理医生"])
    event = store.add_event(
        person_id, "TA 去看心理医生了", ts=NOW - 3600,
        audience="group:g1", sensitivity="normal", now=NOW,
    )
    # 画像这一行的证据指向一个普通敏感度的事件；抽象后的句子本身仍含敏感字眼
    store.save_profile(
        person_id,
        stable_lines=[ProfileLine(text="TA 最近在看心理医生", evidence_refs=[f"event:{event.id}"])],
        status_lines=[],
        now=NOW,
    )
    block = asyncio.run(
        reader(store).assemble(
            text="最近怎么样", person_id=person_id, platform="console", chat_id="g2",
            chat_type="group", now=NOW,
        )
    )
    assert "心理医生" not in block.render()
    assert block.withheld >= 1


def test_build_block_render_header_rules(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    block = asyncio.run(
        reader(store).assemble(
            text="hi", person_id="console:u1", platform="console", chat_id="p1", chat_type="private", now=NOW
        )
    )
    text = block.render()
    assert "背景了解" in text
    assert "不是谈话素材" in text
    assert "cue" in text
    assert "低" in text  # 低频主动关心
    assert "档案" in text


def test_group_has_index_private_does_not(store):
    seed_person(store, "u2", "阿草", stable=["TA 是学生"])
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u2", sender_name="阿草", text="x", ts=NOW - 60)
    group_block = asyncio.run(
        reader(store).assemble(
            text="大家好", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "本群常驻成员" in group_block.render()
    assert "阿草" in group_block.render()
    private_block = asyncio.run(
        reader(store).assemble(
            text="在吗", person_id="console:u1", platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    assert "本群常驻成员" not in private_block.render()


# ----------------------------------------------------------------------
# 跨会话摘要
# ----------------------------------------------------------------------


def test_cross_session_24h_window(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_event(person_id, "TA 在隔壁群说周末去露营", ts=NOW - 3600, source_chat="console:g2", audience="group:g2", now=NOW)
    store.add_event(person_id, "TA 两天前在别的群说要去面试", ts=NOW - 48 * 3600, source_chat="console:g3", audience="group:g3", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="在吗", person_id=person_id, platform="console", chat_id="p1", chat_type="private", now=NOW
        )
    )
    text = block.render()
    assert CROSS_SESSION_HEADER in text
    assert "露营" in text
    assert "面试" not in text  # 超出 24h
    assert "今天" in text  # 时间模糊化
    assert "console:g2" not in text  # 不暴露会话 id


def test_cross_session_excludes_current_chat(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_event(person_id, "TA 刚才在这个私聊里说想喝咖啡", ts=NOW - 600, source_chat="console:p1", audience="private", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="在吗", person_id=person_id, platform="console", chat_id="p1", chat_type="private", now=NOW
        )
    )
    assert "想喝咖啡" not in block.render()


def test_group_never_gets_cross_session(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_event(person_id, "TA 在私聊里说家里的事", ts=NOW - 600, source_chat="console:p1", audience="private", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="在吗", person_id=person_id, platform="console", chat_id="g1", chat_type="group", now=NOW
        )
    )
    assert CROSS_SESSION_HEADER not in block.render()
    assert "家里的事" not in block.render()


# ----------------------------------------------------------------------
# 自我注入与私聊自查
# ----------------------------------------------------------------------

def test_self_events_injected_only_in_current_chat(store):
    """自我时间线只回看本会话里自己做过的事，别的会话的不带进来。"""
    seed_person(store, "u1", "阿花")
    store.add_self_event("我说过周末要整理桌面", source_chat="console:p1", ts=NOW - 3600, now=NOW)
    store.add_self_event("我在别的群答应帮人找歌", source_chat="console:g9", ts=NOW - 3600, now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="在吗", person_id="console:u1", platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    text = block.render()
    assert SELF_HEADER in text
    assert "整理桌面" in text
    assert "找歌" not in text  # 别的会话里的自我行为不进这里


def test_self_events_respect_limit(store):
    config = MemoryConfig(self_inject_limit=1)
    seed_person(store, "u1", "阿花")
    store.add_self_event("第一件旧事", source_chat="console:g1", ts=NOW - 7200, now=NOW)
    store.add_self_event("第二件新事", source_chat="console:g1", ts=NOW - 60, now=NOW)
    block = asyncio.run(
        reader(store, config).assemble(
            text="大家好", person_id="console:u1", platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    text = block.render()
    assert "第二件新事" in text
    assert "第一件旧事" not in text


def test_self_events_take_precedence_in_budget(store):
    """预算先砍事实，最后才动自我注入。"""
    config = MemoryConfig(inject_budget_tokens=60, self_inject_limit=5)
    person_id = seed_person(store, "u1", "阿花")
    store.add_self_event("我说过今天要早点睡", source_chat="console:g1", ts=NOW - 60, now=NOW)
    for i in range(10):
        store.add_memory(person_id, f"TA 喜欢喝美式咖啡，第 {i} 家店都去过", audience="group:g1", now=NOW)
    block = asyncio.run(
        reader(store, config).assemble(
            text="美式咖啡", person_id=person_id, platform="console", chat_id="g1",
            chat_type="group", now=NOW,
        )
    )
    assert "早点睡" in block.render()


def test_recall_self_query_returns_self_report(store):
    """模型或用户用「我」提问：还原成当前说话人，如实摊开本人画像。"""
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"], status=["最近在准备考试"])
    store.add_event("console:u1", "TA 去爬山了", ts=NOW - 3600, audience="private", now=NOW)
    store.add_memory("console:u1", "TA 喜欢美式咖啡", audience="private", now=NOW)
    text = asyncio.run(
        reader(store).recall("我", audience_now="private", speaker="console:u1", now=NOW)
    )
    assert "后端工程师" in text
    assert "准备考试" in text
    assert "1 条关于你的事件" in text
    assert "1 条零散记忆" in text


def test_self_report_ignores_gate_and_audience(store):
    """自查不看别人的审查结果：隐私模式的人也能看到自己的画像。"""
    person_id = seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    store.set_privacy_mode(person_id, "private")
    store.add_event(person_id, "TA 去医院做检查", ts=NOW - 3600,
                    audience="private", sensitivity="high", now=NOW)
    gate = PrivacyGate(store, FakeLLM(raises=AssertionError("自查不该调模型")), MemoryConfig())
    r = MemoryReader(store, None, MemoryConfig(), gate=gate, now_fn=lambda: NOW)
    text = asyncio.run(r.recall("我", audience_now="private", speaker=person_id, now=NOW))
    assert "后端工程师" in text


def test_recall_self_in_group_declines(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    text = asyncio.run(
        reader(store).recall("我", audience_now="group:g1", speaker="console:u1", now=NOW)
    )
    assert "私聊" in text
    assert "后端工程师" not in text


# ----------------------------------------------------------------------
# recall（T2）
# ----------------------------------------------------------------------


def test_recall_by_nickname_and_topic(store):
    seed_person(store, "u2", "阿草", stable=["TA 喜欢弹吉他"])
    store.add_event("console:u2", "TA 去听了吉他演奏会", ts=NOW - 86400,
                    source_chat="console:g1", audience="group:g1", now=NOW)
    text = asyncio.run(reader(store).recall("阿草", "吉他", audience_now="group:g1", speaker="console:u1", now=NOW))
    assert "阿草" in text
    assert "弹吉他" in text
    assert "演奏会" in text
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 86400)) in text


def test_recall_unknown_person(store):
    text = asyncio.run(reader(store).recall("不存在的人", now=NOW))
    assert "没找到" in text


def test_recall_unknown_audience_hides_private(store):
    """没有当前受众时，私聊来源的内容一律扣下（fail closed 的保守选择）。"""
    seed_person(store, "u1", "阿花")
    store.add_event("console:u1", "TA 去看心理医生了", ts=NOW - 3600, audience="private", now=NOW)
    text = asyncio.run(reader(store).recall("阿花", "心理", now=NOW))
    assert "心理医生" not in text


def test_recall_gate_failure_closed(store):
    """审查模型挂了：跨场合项一律扣下，只有同场合的能出来。"""
    person_id = seed_person(store, "u1", "阿花")
    store.add_event(person_id, "TA 在群里说下周去露营", ts=NOW - 3600, audience="group:g1", now=NOW)
    llm = FakeLLM(raises=RuntimeError("模型挂了"))
    text = asyncio.run(reader(store, llm=llm).recall("阿花", now=NOW, audience_now="group:g9"))
    assert "露营" not in text
    # 同场合的项不调模型，照常返回
    text = asyncio.run(reader(store, llm=llm).recall("阿花", now=NOW, audience_now="group:g1"))
    assert "露营" in text


def test_time_range_parse(store):
    r = reader(store)
    assert r._parse_time_range("7d", NOW) == NOW - 7 * 86400
    assert r._parse_time_range("24h", NOW) == NOW - 86400
    assert r._parse_time_range("3天", NOW) == NOW - 3 * 86400
    assert r._parse_time_range("", NOW) is None  # 未指定不限制时间，包括一年以前的记录


# ----------------------------------------------------------------------
# 私聊自查「你记得我什么」与过期事实
# ----------------------------------------------------------------------


def test_self_inquiry_detection():
    assert is_self_inquiry("你还记得我什么吗")
    assert is_self_inquiry("你记得我哪些事")
    assert is_self_inquiry("关于我的记忆都有啥")
    assert not is_self_inquiry("今天中午吃什么")
    assert not is_self_inquiry("你记得带伞吗")


def test_self_inquiry_gets_nudge_in_private(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    block = asyncio.run(
        reader(store).assemble(
            text="你还记得我什么吗", person_id="console:u1", platform="console",
            chat_id="p1", chat_type="private", now=NOW,
        )
    )
    text = block.render()
    assert "问你记得他什么" in text
    assert "后端工程师" in text


def test_self_inquiry_without_any_memory_stays_quiet(store):
    """没有画像也没旧事时，不硬塞一个空的记忆区块。"""
    seed_person(store, "u1", "阿花")
    block = asyncio.run(
        reader(store).assemble(
            text="你还记得我什么吗", person_id="console:u1", platform="console",
            chat_id="p1", chat_type="private", now=NOW,
        )
    )
    assert block.render() == ""


def test_self_inquiry_no_nudge_in_group(store):
    seed_person(store, "u1", "阿花", stable=["TA 是一名后端工程师"])
    block = asyncio.run(
        reader(store).assemble(
            text="你还记得我什么吗", person_id="console:u1", platform="console",
            chat_id="g1", chat_type="group", now=NOW,
        )
    )
    assert "问你记得他什么" not in block.render()


def test_expired_fact_not_injected(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_memory(
        person_id, "TA 明天要去出差", audience="private", stability="temporal",
        expires_at=NOW - 60, now=NOW,
    )
    block = asyncio.run(
        reader(store).assemble(
            text="出差", person_id=person_id, platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    assert "出差" not in block.render()


def test_uncertain_fact_marked(store):
    person_id = seed_person(store, "u1", "阿花")
    store.add_memory(person_id, "TA 可能养了一只猫", audience="private", stability="uncertain", now=NOW)
    block = asyncio.run(
        reader(store).assemble(
            text="TA 可能养了一只猫", person_id=person_id, platform="console", chat_id="p1",
            chat_type="private", now=NOW,
        )
    )
    assert "不太确定" in block.render()


# ----------------------------------------------------------------------
# llm.chat_with_tools：工具调用与不支持时的退化信号
# ----------------------------------------------------------------------


def _tool_client(handler) -> LLMClient:
    return LLMClient(
        LLMConfig(api_key="k", chat_model="m", max_retries=0),
        transport=httpx.MockTransport(handler),
    )


def test_chat_with_tools_returns_message():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "recall", "arguments": '{"person_query":"阿花"}'},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async def scenario():
        client = _tool_client(handler)
        message = await client.chat_with_tools(
            [{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "recall"}}]
        )
        await client.close()
        return message

    message = asyncio.run(scenario())
    assert message["tool_calls"][0]["function"]["name"] == "recall"
    assert captured["body"]["tool_choice"] == "auto"
    assert captured["body"]["tools"]


def test_chat_with_tools_unsupported_raises_tool_unsupported():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text='{"error": "tools are not supported"}')

    async def scenario():
        client = _tool_client(handler)
        with pytest.raises(ToolUnsupported):
            await client.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        # 探测到不支持后不再白试
        assert client._tools_unsupported is True
        with pytest.raises(ToolUnsupported):
            await client.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        await client.close()

    asyncio.run(scenario())
    assert len(calls) == 1


def test_chat_with_tools_other_error_is_llm_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    async def scenario():
        client = _tool_client(handler)
        with pytest.raises(Exception) as info:
            await client.chat_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        await client.close()
        return info

    info = asyncio.run(scenario())
    assert not isinstance(info.value, ToolUnsupported)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def test_format_relative_time_buckets():
    assert format_relative_time(NOW - 60, NOW) == "今天"
    assert format_relative_time(NOW - 86400, NOW) == "昨天"
    assert format_relative_time(NOW - 2 * 86400, NOW) == "前天"
    assert format_relative_time(NOW - 4 * 86400, NOW) == "4 天前"
    assert format_relative_time(NOW - 9 * 86400, NOW) == "上周"
    assert format_relative_time(NOW - 45 * 86400, NOW) == "上个月"
    assert format_relative_time(0, NOW) == ""


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("你好世界") == 4


def test_owner_memory_requires_actual_author_in_turn_not_mention_or_other_owner(store):
    pid = seed_person(store, "o1", "甲机主")
    store.add_memory(pid, "TA 喜欢荞麦面", audience="owner", stability="uncertain", now=NOW)
    store.add_event(pid, "TA 在家做了荞麦面", audience="owner", source_chat="console:else", ts=NOW - 86400)
    r = reader(store)
    args = dict(text="甲机主，荞麦面呢", person_id="console:peer", platform="console", chat_type="group", chat_id="g", now=NOW)
    for ids in [(), {"console:o2"}, {"other:o1"}]:
        assert "荞麦面" not in asyncio.run(r.assemble(**args, owner_person_ids=ids)).render()
    text = asyncio.run(r.assemble(**args, owner_person_ids={pid})).render()
    assert "【机主自述记忆】" in text and "不太确定" in text and "昨天" in text
    store.set_ruling(pid, "fact@group", "deny")
    assert "喜欢荞麦面" not in asyncio.run(r.assemble(**args, owner_person_ids={pid})).render()


def test_private_profile_hooks_do_not_follow_public_first_evidence(store):
    pid = seed_person(store, "u1", "阿花")
    group = store.add_memory(pid, "TA 喜欢喝咖啡", audience="group:g1")
    private = store.add_memory(pid, "TA 喜欢水晶", audience="private")
    store.save_profile(pid, stable_lines=[ProfileLine("喜欢咖啡", [f"memory:{group.id}"]),
                                          ProfileLine("喜欢水晶", [f"memory:{private.id}"])],
                       status_lines=[], hooks=["水晶"], now=NOW)
    store.add_raw_message(platform="console", chat_id="g1", sender_id="u1", text="在呢", ts=NOW)
    text = asyncio.run(reader(store).assemble(text="阿花喜欢什么", person_id="console:u2", platform="console",
                                              chat_type="group", chat_id="g1", now=NOW)).render()
    assert "咖啡" in text and "水晶" not in text
