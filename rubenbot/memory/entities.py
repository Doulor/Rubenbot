"""群实体账：模型提出专名、本地核实逐字来源；摄入端只读既有证据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .store import MemoryStore, RawMessage


@dataclass(frozen=True)
class EntityCandidate:
    name: str
    gloss: str = ""
    gloss_msg_id: str = ""
    message_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityRecord:
    id: int
    platform: str
    chat_id: str
    canonical_name: str
    occurrence_count: int
    first_seen: float
    last_seen: float
    gloss: str
    gloss_msg_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FamiliarityTag:
    """摄入时的事实快照，不含 gloss 正文或模型判断。"""

    entity: str
    occurrence_count: int
    has_gloss: bool


class EntityLedger:
    def __init__(self, store: MemoryStore):
        self.store = store

    def observe_message(self, *, platform: str, chat_id: str, message: RawMessage,
                        candidates: Iterable[EntityCandidate], now: float | None = None) -> list[EntityRecord]:
        """只采纳能在本条流水验证的提及；说明也只认同一实体所在的那条消息。

        抽取按整个窗口遍历每条消息，因此说明可来自同窗口的另一条提及。
        模型不能借用窗口外的某个消息 id 把无关群友、机器人或私聊的话入账。
        """
        if ((platform, chat_id) != (message.platform, message.chat_id) or message.chat_type != 'group'
                or not self.store.entity_source_allowed(message)):
            return []
        parts = (message.text, message.visual_text)
        keys = {str(message.id), message.platform_msg_id} - {''}
        records = []
        for item in candidates:
            name = item.name.strip()
            if not name or not any(name in part for part in parts):
                continue
            if item.message_ids and not keys.intersection(item.message_ids):
                continue
            verified = item.gloss and (not item.gloss_msg_id or item.gloss_msg_id in keys) and any(
                item.gloss in part and name in part for part in parts
            )
            aliases = tuple(a for a in item.aliases if a and a != name and any(
                a in part and name in part for part in parts
            ))
            self.store.put_entity_mention(message, name, aliases, item.gloss if verified else '',
                                          (message.platform_msg_id or str(message.id)) if verified else '')
            record = self.lookup(platform, chat_id, name)
            if record:
                records.append(record)
        return records

    def list_entities(self, platform: str, chat_id: str, *, limit: int = 200) -> list[EntityRecord]:
        return [EntityRecord(**row) for row in self.store.entity_rows(platform, chat_id, limit=limit)]

    def lookup(self, platform: str, chat_id: str, name: str) -> EntityRecord | None:
        rows = self.store.entity_rows(platform, chat_id, name=name, limit=1)
        return EntityRecord(**rows[0]) if rows else None

    def record_known_mentions(self, *, platform: str, chat_id: str, message: RawMessage,
                              now: float | None = None) -> None:
        """摄入事实快照后才计本条，通过同一唯一键与后台重放去重。"""
        if (platform, chat_id) != (message.platform, message.chat_id) or message.chat_type != 'group':
            return
        text = message.text + '\n' + message.visual_text
        for row in self.store.entity_rows(platform, chat_id, text=text):
            surface = next(n for n in (row['canonical_name'], *row['aliases']) if n in text)
            self.store.put_entity_mention(message, surface, (), '', '')

    def familiarity(self, platform: str, chat_id: str, text: str) -> list[FamiliarityTag]:
        return [FamiliarityTag(row['canonical_name'], row['occurrence_count'], bool(row['gloss']))
                for row in self.store.entity_rows(platform, chat_id, text=text)]
