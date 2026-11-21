"""存量表情去重：把索引里同一内容哈希的多条记录并成一条。

病灶（2026-11-21 线上）：索引 140 条，按 ``content_hash`` 只有 105 个唯一
内容——14 组重复、多 35 条；图片本体按哈希命名本来就只有一份，重复的是
索引记录。

本工具按内容哈希分组，每组调用 :meth:`StickerStore.merge`：

- 保留最早 ``created_at`` 的那条（同时间取 id 较小者），id 与 created_at 不变；
- tags 取并集、``native_refs`` 按平台合并、uses 相加、kind 任一方是表情即算表情；
- 只改 ``index.json``，不动 ``files/`` 里的图片文件（同哈希天然共享一份）。

用法::

    python -m tools.dedupe_stickers --config config.yaml
    python -m tools.dedupe_stickers --dir data/stickers --dry-run

重开索引即可看到合并后的条数；本工具不访问网络。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rubenbot.config import Config
from rubenbot.stickers import Sticker, StickerStore

logger = logging.getLogger("dedupe_stickers")


@dataclass
class Counts:
    """去重结果：``before`` / ``after`` 是索引条数，``removed`` 是删掉的条数。"""

    before: int = 0
    after: int = 0
    groups: int = 0
    removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "before": self.before,
            "after": self.after,
            "groups": self.groups,
            "removed": self.removed,
        }


def duplicate_groups(stickers: list[Sticker]) -> list[list[Sticker]]:
    """按内容哈希分组，只返回有重复的组；哈希为空的条目不参与。"""
    groups: dict[str, list[Sticker]] = defaultdict(list)
    for sticker in stickers:
        if sticker.content_hash:
            groups[sticker.content_hash].append(sticker)
    return [group for group in groups.values() if len(group) > 1]


def dedupe(store: StickerStore, *, dry_run: bool = False) -> Counts:
    """合并同一内容哈希的重复记录；``dry_run`` 只统计不写盘。"""
    counts = Counts(before=len(store.find_by_tags(None)))
    for group in duplicate_groups(store.find_by_tags(None)):
        counts.groups += 1
        counts.removed += len(group) - 1
        if dry_run:
            continue
        keeper = min(group, key=lambda item: (item.created_at, item.id))
        ids = [item.id for item in group]
        logger.info(
            "合并 %s 的 %d 条记录：保留 %s，并入 %s",
            group[0].content_hash[:12], len(group), keeper.id,
            ", ".join(item.id for item in group if item.id != keeper.id),
        )
        store.merge(ids, keep=keeper.id)
    counts.after = counts.before - counts.removed if dry_run else len(store.find_by_tags(None))
    return counts


def run(config_path: str | Path, *, dry_run: bool = False) -> Counts:
    """按配置文件里的 ``stickers.dir`` 载入索引并去重。"""
    config = Config.load(config_path)
    return dedupe(StickerStore(config.stickers.dir), dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并表情库里同内容的重复记录（只改索引，不动图片）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（读 stickers.dir）")
    parser.add_argument("--dir", default="", help="直接指定表情库目录，覆盖配置里的 stickers.dir")
    parser.add_argument("--dry-run", action="store_true", help="只统计重复，不写索引")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = Path(args.dir or Config.load(args.config).stickers.dir)
    if not (root / "index.json").is_file():
        print(f"表情库索引不存在：{root / 'index.json'}", file=sys.stderr)
        return 1
    counts = dedupe(StickerStore(root), dry_run=args.dry_run)
    print(json.dumps(counts.as_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
