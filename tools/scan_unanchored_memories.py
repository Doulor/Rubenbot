#!/usr/bin/env python3
"""存量扫描：找出「只有评价、没有主语」的记忆条目，补齐实体或清掉。

病灶（第 23 项，机主 2026-11-23 点名）：一条人物记忆原文
「TA 做过一个『随手做的』东西，硬性限制比较大，认为它用来推单向消息不错」——
评价留下了，所指的实体被蒸馏丢掉，整条记忆不可检索、不可用。

本工具按同一个判定口径（:mod:`rubenbot.memory.anchor`）扫两处账本：

- ``memories``：``entity`` 为空、正文里只有指代/评价的人物事实；
- 画像行（``profiles`` + ``profile_evidence.entity``）：同样只算人物画像里没有实体锚点的行。

对每条候选，用出处原话尝试补回实体：

- **补回**：出处原话（``source_ref`` 指向的流水 / 画像证据的 event、memory 正文）里
  能找到唯一可识别专名 → 正文加上「关于「名字」：」前缀并写入 ``entity``；
- **清掉**：找不到、或找到多个实体（指称不明）→ 删除并写 ``memory_log``。

默认**只读**：只输出扫描报告，不改账本。真正补回 / 清掉必须显式加 ``--apply``。
任何修改都写 ``memory_log``（``merge`` / ``drop`` / ``dropped_unanchored`` /
``profile_update``），可事后查证；``--apply`` 前建议先备份数据库。

用法::

    python -m tools.scan_unanchored_memories --db data/rubenbot.db
    python -m tools.scan_unanchored_memories --db data/rubenbot.db --format json
    python -m tools.scan_unanchored_memories --db data/rubenbot.db --apply
    python -m tools.scan_unanchored_memories --db data/rubenbot.db --apply --out /tmp/report.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rubenbot.memory.anchor import (
    Anchor,
    annotate,
    explicit_names,
    find_deictic,
    needs_anchor,
    resolve_anchor,
)


@dataclass
class Candidate:
    """一条待判定条目。"""

    kind: str  # memory / profile
    ident: str  # memory:<id> 或 profile:<person_id>:<section>:<line_no>
    person_id: str
    content: str
    source_text: str = ""
    known_names: dict[str, str] = field(default_factory=dict)
    origin_chat: str = ""
    raw_id: int | None = None
    section: str = ""
    line_no: int = -1
    anchor: Anchor | None = None

    @property
    def deictic(self) -> list[str]:
        return find_deictic(self.content)

    @property
    def outcome(self) -> str:
        return "recovered" if self.anchor is not None else "cleared"


@dataclass
class ScanReport:
    """扫描报告；计数与清单一一对应。"""

    scanned: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    applied: bool = False
    backup_hint: str = (
        "默认只读；加 --apply 才会改账本，改动都会写 memory_log。"
    )

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def recovered(self) -> list[Candidate]:
        return [item for item in self.candidates if item.anchor is not None]

    @property
    def cleared(self) -> list[Candidate]:
        return [item for item in self.candidates if item.anchor is None]

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "total": self.total,
            "recovered": len(self.recovered),
            "cleared": len(self.cleared),
            "applied": self.applied,
            "items": [
                {
                    "kind": item.kind,
                    "id": item.ident,
                    "person_id": item.person_id,
                    "content": item.content,
                    "deictic": item.deictic,
                    "outcome": item.outcome,
                    "entity": item.anchor.name if item.anchor else "",
                    "source_text": item.source_text,
                    "source_raw_id": item.raw_id,
                }
                for item in self.candidates
            ],
        }

    def format_markdown(self) -> str:
        lines = [
            "# 无实体锚点记忆扫描报告",
            "",
            f"- 扫描条目：{self.scanned}",
            f"- 命中「只有评价、没有主语」：{self.total}",
            f"- 可补回实体：{len(self.recovered)}",
            f"- 需清掉：{len(self.cleared)}",
            f"- 是否已改账本：{'是（--apply）' if self.applied else '否（只读）'}",
            "",
        ]
        if not self.candidates:
            lines.append("没有同模式条目。")
            return "\n".join(lines)
        for title, items in (("补回清单", self.recovered), ("清掉清单", self.cleared)):
            lines.append(f"## {title}（{len(items)} 条）")
            lines.append("")
            if not items:
                lines.append("- （无）")
                lines.append("")
                continue
            for item in items:
                target = f" → 「{item.anchor.name}」" if item.anchor else ""
                lines.append(f"- `{item.ident}` [{item.kind}] {item.content}{target}")
                lines.append(f"  - 指称：{'、'.join(item.deictic) or '（无）'}")
                if item.source_text:
                    lines.append(f"  - 出处：{item.source_text}")
                else:
                    lines.append("  - 出处：（原话已裁剪或没有出处）")
            lines.append("")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# 读取（只读连接，不建表、不迁移）
# ----------------------------------------------------------------------


def _connect_ro(path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _known_entities(conn: sqlite3.Connection, origin_chat: str) -> dict[str, str]:
    """实体账里登记过的名字（{写法: 规范名}）；私聊没有实体账，返回空。"""
    platform, _, chat_id = origin_chat.partition(":")
    if "entities" not in _tables(conn) or not platform or not chat_id:
        return {}
    names: dict[str, str] = {}
    rows = conn.execute(
        "SELECT id, canonical_name FROM entities WHERE platform=? AND chat_id=?",
        (platform, chat_id),
    ).fetchall()
    for row in rows:
        canonical = str(row["canonical_name"] or "")
        if canonical:
            names[canonical] = canonical
        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (row["id"],)
        ).fetchall()
        for alias in aliases:
            names.setdefault(str(alias["alias"]), canonical)
    return names


def _source_text(conn: sqlite3.Connection, origin_chat: str, source_ref: str) -> tuple[str, int | None]:
    """从 memory.source_ref（platform:chat:lo-hi[,lo-hi]）读出出处原话。"""
    if not origin_chat or not source_ref or "raw_messages" not in _tables(conn):
        return "", None
    prefix, sep, value = source_ref.rpartition(":")
    if not sep or prefix != origin_chat:
        return "", None
    ids: list[int] = []
    for part in value.split(","):
        try:
            first, last = part.split("-")
            ids.extend(range(int(first), int(last) + 1))
        except ValueError:
            continue
    if not ids:
        return "", None
    platform, _, chat_id = origin_chat.partition(":")
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, text FROM raw_messages WHERE platform=? AND chat_id=? AND id IN ({marks}) ORDER BY id",
        (platform, chat_id, *ids),
    ).fetchall()
    if not rows:
        return "", None
    return "\n".join(str(row["text"]) for row in rows if row["text"]), int(rows[0]["id"])


def _evidence_text(conn: sqlite3.Connection, refs: Iterable[str]) -> tuple[str, str]:
    """画像行的证据原文与已核实锚点：event 摘要 / memory 正文。

    memory 能追到出处原话时一并带上；这条证据记忆自己已经锚在实体上时，
    把锚点一并返回，画像行不必再猜。
    """
    parts: list[str] = []
    entities: set[str] = set()
    for ref in refs:
        kind, _, value = ref.partition(":")
        try:
            ident = int(value)
        except ValueError:
            continue
        if kind == "event" and "events" in _tables(conn):
            row = conn.execute("SELECT summary FROM events WHERE id=?", (ident,)).fetchone()
            if row:
                parts.append(str(row["summary"]))
        elif kind == "memory" and "memories" in _tables(conn):
            columns = _columns(conn, "memories")
            origin_col = "origin_chat" if "origin_chat" in columns else "'' AS origin_chat"
            entity_col = "entity" if "entity" in columns else "'' AS entity"
            row = conn.execute(
                f"SELECT content, {origin_col}, {entity_col}, source_ref FROM memories WHERE id=?",
                (ident,),
            ).fetchone()
            if not row:
                continue
            parts.append(str(row["content"]))
            if str(row["entity"] or "").strip():
                entities.add(str(row["entity"]).strip())
            # 画像行只有 memory 正文时也能凭这条记忆的出处原话补回实体。
            raw, _ = _source_text(conn, str(row["origin_chat"] or ""), str(row["source_ref"] or ""))
            if raw:
                parts.append(raw)
    # 多条证据指向不同实体时不猜，交给正文/出处再判。
    entity = entities.pop() if len(entities) == 1 else ""
    return "\n".join(parts), entity


def _scan_anchor(
    content: str, source_text: str, known_names: dict[str, str], *, entity: str = ""
) -> Anchor | None:
    """扫描用的判定：先按出处原话核实，再看正文自己是否已经写了唯一专名。

    存量里可能存在「正文已有『关于「飞书」：』前缀、但 entity 列没落上」的行；
    原话早被裁剪时也不该把它们当缺失主语清掉。证据记忆已经锚在实体上时，
    直接沿用那个锚点（``entity``）。
    """
    if entity:
        return Anchor(entity, entity, "stored")
    anchor = resolve_anchor(content, source_text, known_names=known_names)
    if anchor is not None:
        return anchor
    names = explicit_names(content)
    if len(names) == 1:
        return Anchor(names[0], names[0], "content")
    return None


def collect(conn: sqlite3.Connection) -> ScanReport:
    """只读扫描：列出候选并尝试解析锚点；不改任何数据。

    只读连接不会跑迁移：老库可能还没有 ``entity`` / ``origin_chat`` 列，
    这里按现有列拼查询，避免为了扫一遍而去改建表。
    """
    report = ScanReport()
    if "memories" not in _tables(conn):
        return report
    columns = _columns(conn, "memories")
    has_entity = "entity" in columns
    has_origin = "origin_chat" in columns
    where = "WHERE entity=''" if has_entity else ""
    origin_col = "origin_chat" if has_origin else "'' AS origin_chat"
    entity_col = "entity" if has_entity else "'' AS entity"
    rows = conn.execute(
        f"SELECT id, person_id, content, {origin_col}, source_ref, {entity_col} "
        f"FROM memories {where} ORDER BY id"
    ).fetchall()
    report.scanned += len(rows)
    for row in rows:
        content = str(row["content"] or "")
        if not needs_anchor(content):
            continue
        origin = str(row["origin_chat"] or "")
        source_text, raw_id = _source_text(conn, origin, str(row["source_ref"] or ""))
        candidate = Candidate(
            kind="memory",
            ident=f"memory:{row['id']}",
            person_id=str(row["person_id"] or ""),
            content=content,
            source_text=source_text,
            known_names=_known_entities(conn, origin),
            origin_chat=origin,
            raw_id=raw_id,
        )
        anchor = _scan_anchor(content, source_text, candidate.known_names)
        candidate.anchor = anchor
        report.candidates.append(candidate)

    if {"profiles", "profile_evidence"} <= _tables(conn):
        has_line_entity = "entity" in _columns(conn, "profile_evidence")
        entity_select = "COALESCE(entity,'') AS entity" if has_line_entity else "'' AS entity"
        for profile in conn.execute("SELECT person_id, stable_text, status_text FROM profiles"):
            for section, text in (("stable", profile["stable_text"]), ("status", profile["status_text"])):
                for line_no, line in enumerate((text or "").splitlines()):
                    if not line.strip():
                        continue
                    report.scanned += 1
                    if not needs_anchor(line):
                        continue
                    row = conn.execute(
                        f"SELECT evidence_refs, {entity_select} FROM profile_evidence "
                        "WHERE person_id=? AND section=? AND line_no=?",
                        (profile["person_id"], section, line_no),
                    ).fetchone()
                    if row is None or str(row["entity"] or "").strip():
                        continue
                    refs = json.loads(row["evidence_refs"] or "[]")
                    source_text, stored_entity = _evidence_text(conn, refs)
                    candidate = Candidate(
                        kind="profile",
                        ident=f"profile:{profile['person_id']}:{section}:{line_no}",
                        person_id=str(profile["person_id"]),
                        content=line,
                        source_text=source_text,
                        section=section,
                        line_no=line_no,
                    )
                    anchor = _scan_anchor(line, source_text, {}, entity=stored_entity)
                    candidate.anchor = anchor
                    report.candidates.append(candidate)
    return report


# ----------------------------------------------------------------------
# 执行（--apply 才改账本）
# ----------------------------------------------------------------------


def apply_report(path: str | Path, report: ScanReport) -> ScanReport:
    """按 report 的判定补回 / 清掉；全部走 MemoryStore，写 memory_log。"""
    from rubenbot.memory.store import MemoryStore

    store = MemoryStore(path)
    try:
        profiles: dict[str, dict[tuple[str, int], Candidate]] = {}
        for item in report.recovered:
            if item.kind == "memory":
                mem_id = int(item.ident.rpartition(":")[2])
                store.merge_memory_inplace(
                    mem_id,
                    content=annotate(item.content, item.anchor),
                    entity=item.anchor.name,
                    reason=f"存量扫描补回实体「{item.anchor.name}」",
                )
            else:
                profiles.setdefault(item.person_id, {})[(item.section, item.line_no)] = item
        for item in report.cleared:
            if item.kind == "memory":
                mem_id = int(item.ident.rpartition(":")[2])
                store.apply_changes(
                    action="drop",
                    delete_ids=[mem_id],
                    reason="存量扫描：只有评价没有主语，找不到可回指的实体",
                    person_id=item.person_id,
                )
                store.log_dropped_unanchored(
                    person_id=item.person_id,
                    origin_chat=item.origin_chat,
                    kind="memory",
                    content=item.content,
                    reason="存量扫描清掉：无实体锚点",
                )
            else:
                profiles.setdefault(item.person_id, {})[(item.section, item.line_no)] = item
        # 同一个人一次重写画像：逐行按原行号处理，删行不会让后面的行号错位。
        for person_id, changes in profiles.items():
            _rewrite_profile(store, person_id, changes)
        report.applied = True
    finally:
        store.close()
    return report


def _rewrite_profile(store, person_id: str, changes: dict[tuple[str, int], Candidate]) -> None:
    """重写某人的画像：补回行加前缀与实体，清掉行移除；写 profile_update 审计日志。"""
    profile = store.get_profile(person_id)
    if profile is None:
        return
    evidence = store.get_profile_evidence(person_id)
    entities = store.get_profile_line_entities(person_id)
    from rubenbot.memory.store import ProfileLine

    rebuilt: dict[str, list[ProfileLine]] = {"stable": [], "status": []}
    dropped: list[Candidate] = []
    changed = False
    for section, lines in (("stable", profile.stable_lines), ("status", profile.status_lines)):
        for index, line in enumerate(lines):
            key = (section, index)
            change = changes.get(key)
            if change is not None and change.anchor is None:
                dropped.append(change)
                changed = True
                continue
            if change is not None:
                text = annotate(line, change.anchor)
                changed = changed or text != line or (change.anchor.name != entities.get(key, ""))
                rebuilt[section].append(
                    ProfileLine(
                        text=text,
                        evidence_refs=evidence.get(key, []),
                        entity=change.anchor.name,
                    )
                )
                continue
            rebuilt[section].append(
                ProfileLine(
                    text=line,
                    evidence_refs=evidence.get(key, []),
                    entity=entities.get(key, ""),
                )
            )
    if not changed:
        return  # 没有实际改动（例如候选行已经带前缀），不写空日志
    store.save_profile(
        person_id,
        stable_lines=rebuilt["stable"],
        status_lines=rebuilt["status"],
        status_expires_at=profile.status_expires_at,
        hooks=profile.hooks,
        log_reason="存量扫描：" + (
            "补回并清掉无实体锚点画像行" if dropped else "补回画像行实体"
        ),
    )
    for item in dropped:
        store.log_dropped_unanchored(
            person_id=person_id,
            origin_chat="",
            kind="profile",
            content=item.content,
            reason="存量扫描清掉：无实体锚点",
        )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描只有评价、没有主语的人物记忆")
    parser.add_argument("--db", default="data/rubenbot.db", help="SQLite 数据库路径")
    parser.add_argument("--apply", action="store_true", help="真正补回/清掉（默认只读出报告）")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--out", default="", help="报告写到文件；留空打印到标准输出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}", file=sys.stderr)
        return 2
    conn = _connect_ro(path)
    try:
        report = collect(conn)
    finally:
        conn.close()
    if args.apply:
        apply_report(path, report)
    text = (
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
        if args.format == "json"
        else report.format_markdown()
    )
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"报告已写入 {args.out}（补回 {len(report.recovered)}，清掉 {len(report.cleared)}）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
