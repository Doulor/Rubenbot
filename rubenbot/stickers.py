"""表情库：图片本体 + 一份 JSON 索引。

默认存放在 ``data/stickers/``（目录可经 ``stickers.dir`` 配置）：

- ``files/``：图片本体，文件名取内容哈希前 16 位
- ``index.json``：每条表情的元信息（id / 文件 / 标签 / 各平台原生引用 / 内容哈希）

去重按内容哈希：同一张图不管从哪条消息收集来，都只入库一次；重复收集时
把新标签与新的平台原生引用并进已有记录。拿不到图片字节（例如只有平台侧
消息段引用）时，用引用的稳定序列化结果充当哈希，同样能去重。

原生引用（``native_refs``）对存储层是不透明的 JSON，由各适配器自己解释，
约定形如 ``{"kind": 段类型, "data": {...}}``（如 OneBot 的 mface 段），
Telegram 回填的是 ``{"kind": "sticker", "file_id": ...}``。

对外接口保持小：:meth:`StickerStore.add` / :meth:`StickerStore.get` /
:meth:`StickerStore.find_by_tags` / :meth:`StickerStore.record_native_ref`。
索引是单个 JSON 文件，写入走「临时文件 + ``os.replace``」，与仓库里
其他运行时状态（Telegram offset 等）的落盘方式一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
INDEX_NAME = "index.json"
FILES_DIR = "files"
DEFAULT_STICKER_DIR = "data/stickers"


def _clean_platform(value: Any) -> str:
    return str(value or "").strip()


def _clean_tag(value: Any) -> str:
    """标签归一：去空白、转小写（中文不受影响）。"""
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class Sticker:
    """表情库里的一条记录。"""

    id: str
    file: str  # 相对表情库根目录的路径；只有原生引用、没有图片本体时为空串
    tags: tuple[str, ...] = ()
    native_refs: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "tags": list(self.tags),
            "native_refs": dict(self.native_refs),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Sticker":
        refs = data.get("native_refs")
        return cls(
            id=str(data.get("id") or ""),
            file=str(data.get("file") or ""),
            tags=tuple(_clean_tag(tag) for tag in (data.get("tags") or []) if _clean_tag(tag)),
            native_refs=dict(refs) if isinstance(refs, Mapping) else {},
            content_hash=str(data.get("content_hash") or ""),
            created_at=float(data.get("created_at") or 0.0),
        )


class StickerStore:
    """表情库读写（单进程、同步调用，索引常驻内存）。"""

    def __init__(self, root: str | Path = DEFAULT_STICKER_DIR):
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME
        self._lock = threading.RLock()
        self._stickers: dict[str, Sticker] = {}
        self._load()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def add(
        self,
        data: bytes | None = None,
        *,
        tags: Iterable[Any] = (),
        native_refs: Mapping[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        now: float | None = None,
    ) -> Sticker:
        """入库一张表情，按内容哈希去重；重复时合并标签与原生引用。

        ``data`` 为图片本体；纯引用条目（只有平台侧引用、没有字节）允许
        ``data=None``，此时 ``file`` 为空。两者都没有时抛 ``ValueError``。
        """
        refs = {
            _clean_platform(platform): ref
            for platform, ref in (native_refs or {}).items()
            if _clean_platform(platform)
        }
        if data is None and not refs:
            raise ValueError("表情入库需要图片数据或至少一个平台原生引用")
        if data is not None:
            digest = hashlib.sha256(bytes(data)).hexdigest()
        else:
            digest = hashlib.sha256(
                json.dumps(refs, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        sticker_id = "st_" + digest[:12]

        with self._lock:
            existing = self._stickers.get(sticker_id)
            if existing is not None:
                # 同一张图重复出现：不新增文件，只并进新标签/新引用
                merged_tags = _merge_tags(existing.tags, tags)
                merged_refs = {**refs, **existing.native_refs}
                if merged_tags != existing.tags or merged_refs != existing.native_refs:
                    existing = Sticker(
                        id=existing.id,
                        file=existing.file,
                        tags=merged_tags,
                        native_refs=merged_refs,
                        content_hash=existing.content_hash,
                        created_at=existing.created_at,
                    )
                    self._stickers[sticker_id] = existing
                    self._save()
                return existing

            relative = ""
            if data is not None:
                relative = self._write_file(bytes(data), digest, file_name, mime_type)
            sticker = Sticker(
                id=sticker_id,
                file=relative,
                tags=_merge_tags((), tags),
                native_refs=refs,
                content_hash=digest,
                created_at=time.time() if now is None else now,
            )
            self._stickers[sticker_id] = sticker
            self._save()
            return sticker

    def get(self, sticker_id: str) -> Sticker | None:
        with self._lock:
            return self._stickers.get(str(sticker_id or "").strip())

    def find_by_tags(
        self,
        tags: Iterable[Any] | None = None,
        *,
        limit: int | None = None,
    ) -> list[Sticker]:
        """按标签找表情（命中任意一个即可）。

        ``tags`` 为空或 None 表示全量；``limit`` 限制返回条数（按入库顺序）。
        """
        wanted = {_clean_tag(tag) for tag in (tags or []) if _clean_tag(tag)}
        with self._lock:
            items = list(self._stickers.values())
        if wanted:
            items = [item for item in items if wanted & set(item.tags)]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    def record_native_ref(self, sticker_id: str, platform: str, ref: Any) -> bool:
        """回填平台原生引用（如 Telegram 返回的 file_id）；表情不存在返回 False。"""
        platform = _clean_platform(platform)
        if not platform or ref in (None, ""):
            return False
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None:
                return False
            refs = {**existing.native_refs, platform: ref}
            self._stickers[existing.id] = Sticker(
                id=existing.id,
                file=existing.file,
                tags=existing.tags,
                native_refs=refs,
                content_hash=existing.content_hash,
                created_at=existing.created_at,
            )
            self._save()
            return True

    def delete(self, sticker_id: str) -> bool:
        """删除一条表情及其图片本体；不存在时返回 False。"""
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None:
                return False
            path = self._safe_file_path(existing)
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._stickers.pop(existing.id, None)
            try:
                self._save(raise_errors=True)
            except Exception:
                self._stickers[existing.id] = existing
                if path is not None and not path.exists():
                    # 删除索引失败时尽量恢复图片，避免内存与磁盘分裂。
                    logger.warning("表情索引删除失败，图片已移除：%s", path)
                raise
            return True

    # ------------------------------------------------------------------
    # 文件与索引
    # ------------------------------------------------------------------

    def path_of(self, sticker: Sticker | None) -> Path | None:
        """图片本体的绝对路径；纯引用条目或不安全文件返回 None。"""
        path = self._safe_file_path(sticker)
        return path if path is not None and path.is_file() else None

    def _safe_file_path(self, sticker: Sticker | None) -> Path | None:
        """只接受表情库 files/ 下的普通文件，拒绝越界与符号链接。"""
        if sticker is None or not sticker.file:
            return None
        relative = Path(sticker.file)
        if relative.is_absolute() or relative.parts[:1] != (FILES_DIR,):
            return None
        path = self.root / relative
        try:
            if path.is_symlink() or path.resolve().parent != (self.root / FILES_DIR).resolve():
                return None
        except OSError:
            return None
        return path

    def reference(self, sticker: Sticker) -> dict[str, Any]:
        """表情引用：能力层 ``send_sticker`` 的入参结构。

        含 id / 标签 / 各平台原生引用，以及图片本体的绝对路径（没有本体时
        为 None）。适配器只需按自己的平台名取 ``native_refs``，取不到再读
        ``file``。
        """
        path = self.path_of(sticker)
        return {
            "id": sticker.id,
            "tags": list(sticker.tags),
            "native_refs": dict(sticker.native_refs),
            "file": str(path) if path is not None else None,
        }

    def _write_file(
        self,
        data: bytes,
        digest: str,
        file_name: str | None,
        mime_type: str | None,
    ) -> str:
        suffix = Path(str(file_name or "")).suffix
        if not suffix:
            mime = str(mime_type or "").split(";")[0].strip()
            suffix = mimetypes.guess_extension(mime) if mime else ""
        suffix = (suffix or ".png").lower()
        relative = f"{FILES_DIR}/{digest[:16]}{suffix}"
        path = self.root / relative
        if path.is_file():
            return relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # 写临时文件再替换：中途崩溃不会留下半张图
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return relative

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏索引不阻塞启动
            logger.warning("读取表情库索引失败（按空库处理）：%s", exc)
            return
        entries = data.get("stickers") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            logger.warning("表情库索引格式异常（按空库处理）：%s", self.index_path)
            return
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            sticker = Sticker.from_dict(item)
            if sticker.id:
                self._stickers[sticker.id] = sticker

    def _save(self, *, raise_errors: bool = False) -> None:
        payload = {
            "version": INDEX_VERSION,
            "stickers": [item.to_dict() for item in self._stickers.values()],
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_name(self.index_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except Exception as exc:  # noqa: BLE001 - 普通写入失败只记日志，内存里仍可用
            logger.warning("保存表情库索引失败：%s", exc)
            if raise_errors:
                raise RuntimeError(f"保存表情库索引失败：{exc}") from exc


def _merge_tags(existing: Iterable[str], extra: Iterable[Any]) -> tuple[str, ...]:
    """合并标签：保序、去重、小写归一。"""
    merged: list[str] = []
    for tag in list(existing) + list(extra):
        name = _clean_tag(tag)
        if name and name not in merged:
            merged.append(name)
    return tuple(merged)


__all__ = [
    "DEFAULT_STICKER_DIR",
    "Sticker",
    "StickerStore",
]
