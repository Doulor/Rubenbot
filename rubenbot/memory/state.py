"""会话状态是裁定用的短期渲染缓存，不写回时间线、画像或生成历史。"""

from __future__ import annotations

import json
import logging

from ..config import MemoryConfig
from ..llm import LLMProtocol, parse_json_payload
from .retrieve import audience_for_chat
from .store import Event, MemoryStore
from .summary import trim_summary

logger = logging.getLogger(__name__)

# 每轮最多看近三天；是否还算「当前」取决于证据年龄，而非最近一次合成时间。
STATE_LIFETIME = 3 * 86400
REFRESH_INTERVAL = 3600

_SYSTEM = """你在整理自己此刻的感觉，材料是这一个会话里真实发生的事和有出处的画像倾向。
用第一人称、现在时，以「我」起句，写我现在的注意力、心情或跟这里的人相处的感觉。
取感觉而非流水账；把人的经历归给本人。离现在越远，影响越淡；旧感觉只在近期材料仍支持时延续。
每段最多 200 字，只输出 JSON。state 是我的当前状态。"""


class StateDistiller:
    def __init__(self, llm: LLMProtocol, store: MemoryStore, config: MemoryConfig):
        self.llm = llm
        self.store = store
        self.config = config
        self._next_scan = 0.0

    def _profile_lines(self, events: list[Event], now: float) -> list[str]:
        # 只选全部证据都来自本场合的行；混合来源画像仍交给已有分寸口，
        # 不可通过状态短文把一次私聊洗成同群来源。
        source_chat, audience = events[0].source_chat, events[0].audience
        lines = []
        for pid in dict.fromkeys(e.person_id for e in events):
            profile = self.store.get_profile(pid)
            if profile is None:
                continue
            evidence = self.store.get_profile_evidence(pid)
            ids = {ref for row in evidence.values() for ref in row}
            def ref_ids(kind: str) -> list[int]:
                return [int(suffix) for ref in ids for name, _, suffix in [ref.partition(':')]
                        if name == kind and suffix.isdecimal() and len(suffix) < 18]
            refs = {f"event:{e.id}" for e in self.store.get_events_by_ids(ref_ids('event'))
                    if e.source_chat == source_chat and e.audience == audience and e.person_id == pid
                    and e.sensitivity == 'normal'}
            refs.update(f"memory:{m.id}" for m in self.store.get_memories_by_ids(ref_ids('memory'))
                        if m.origin_chat == source_chat and m.audience == audience and m.person_id == pid
                        and m.sensitivity == 'normal' and (m.expires_at is None or m.expires_at > now))
            sections = [("stable", profile.stable_lines)]
            if profile.status_expires_at is None or profile.status_expires_at > now:
                sections.append(("status", profile.status_lines))
            for section, entries in sections:
                for i, line in enumerate(entries):
                    sources = evidence.get((section, i), [])
                    if sources and refs.issuperset(sources):
                        lines.append(f"{pid}：{line[:160]}")
                        if len(lines) >= 8:
                            return lines
        return lines

    async def maybe_update(self, *, now: float) -> None:
        # 维护循环每隔数秒都会执行；活跃会话列表与模型合成都不必这么频繁。
        if now < self._next_scan:
            return
        self._next_scan = now + 300
        for chat in self.store.list_chats(limit=1000):
            if chat['last_ts'] < now - STATE_LIFETIME:
                break
            try:
                await self.update_chat(chat['platform'], chat['chat_type'], chat['chat_id'], now=now)
            except Exception as exc:  # 单会话失败时保留旧状态，下一轮重试
                logger.warning("更新会话状态失败（%s/%s）：%s", chat['platform'], chat['chat_id'], exc)

    async def update_chat(self, platform: str, chat_type: str, chat_id: str, *, now: float) -> None:
        key = (platform, chat_type, chat_id)
        old = self.store.get_conversation_state(*key)
        if old and now - old['updated_at'] < REFRESH_INTERVAL:
            return
        source_chat = f"{platform}:{chat_id}"
        audience = audience_for_chat(chat_type, chat_id)
        events = self.store.conversation_events(source_chat, audience, since=now - STATE_LIFETIME, now=now)
        # 相同的旧事件不反复占用后台模型；有新交流或画像更新才值得重合成。
        if not events:
            return
        if old and events[0].ts <= old['evidence_ts']:
            fresh = any(e.created_at > old['updated_at'] for e in events)
            if not fresh and not any(
                (profile := self.store.get_profile(pid)) and profile.updated_at > old['updated_at']
                for pid in {e.person_id for e in events}
            ):
                return
        subjects = sorted({e.person_id for e in events})
        lines = [f"{e.person_id}（{int((now - e.ts) / 3600)} 小时前）：{e.summary[:200]}" for e in events]
        profiles = self._profile_lines(events, now)
        if profiles:
            lines += ["本会话有依据的画像倾向：", *profiles]
        # 不用新生成的时间给老记忆续命；数天前的感觉完全退出后续合成。
        group = chat_type == 'group' and self.config.group_profile.enabled
        if (old and now - old['evidence_ts'] < STATE_LIFETIME
                and set(json.loads(old['subjects'])).issubset(subjects)):
            age = int((now - old['updated_at']) / 3600)
            lines.append(f"上次的感觉（{age} 小时前）：{old['state']}")
            if group and old['atmosphere']:
                lines.append(f"上次的群氛围（{age} 小时前）：{old['atmosphere']}")
        fields = ('群聊还用 atmosphere 单独写我感受到的此刻群氛围，同样以「我」起句。'
                  if group else '只需 state 一个字段。')
        raw = await self.llm.complete(
            [{"role": "system", "content": _SYSTEM + fields},
             {"role": "user", "content": "\n".join(lines)}],
            role="summary", task="conversation_state", temperature=0.3, json_mode=True,
        )
        payload = parse_json_payload(raw)
        if not isinstance(payload, dict):
            return

        def first_person(field: str) -> str:
            text = payload.get(field)
            if not isinstance(text, str) or not text.strip().startswith('我'):
                return ''
            return trim_summary(' '.join(text.split()), 200)

        state = first_person('state')
        atmosphere = first_person('atmosphere') if group else ''
        if not state or (group and not atmosphere):
            return
        # 等待模型时，WebUI 或隐私命令可能清空了来源。此时宁可等下一轮重算。
        # 再取同一份有受众/隐私约束的快照，而非只比较事件 id 是否仍存在。
        current = self.store.conversation_events(source_chat, audience, since=now - STATE_LIFETIME, now=now)
        if (current != events or self.store.get_conversation_state(*key) != old
                or self._profile_lines(current, now) != profiles):
            return
        self.store.set_conversation_state(
            *key, state=state, atmosphere=atmosphere, subjects=subjects,
            evidence_ts=events[0].ts, now=now,
        )

    def planner_text(self, platform: str, chat_type: str, chat_id: str, *, now: float) -> str:
        """热路径只读缓存；手写群备注生效时不叠加自动归纳的氛围。"""
        try:
            row = self.store.get_conversation_state(platform, chat_type, chat_id)
            if not row or now - row['evidence_ts'] >= STATE_LIFETIME:
                return ''
            text = f"此刻的感觉：{row['state']}"
            if chat_type == 'group' and self.config.group_profile.enabled and row['atmosphere']:
                profile = self.store.get_group_profile(platform, chat_id)
                if not profile or not profile.manual_note.strip():
                    text += f"\n群氛围状态：{row['atmosphere']}"
            return text
        except Exception as exc:  # 读不到渲染缓存只少一行背景
            logger.warning("读取会话状态失败：%s", exc)
            return ''
