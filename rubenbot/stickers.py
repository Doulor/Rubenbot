"""表情库：图片本体 + 一份 JSON 索引。

默认存放在 ``data/stickers/``（目录可经 ``stickers.dir`` 配置）：

- ``files/``：图片本体，文件名取内容哈希前 16 位 + 文件头嗅探出的扩展名
- ``index.json``：每条表情的元信息（id / 文件 / 标签 / 各平台原生引用 /
  内容哈希 / 种类 / 视觉描述 / 情绪标签 / 使用次数）

去重按内容哈希：同一张图不管从哪条消息收集来，都只入库一次；重复收集时
把新标签、新的平台原生引用与使用次数并进已有记录，保留最早的 id 与
created_at。拿不到图片字节（例如只有平台侧消息段引用）时，用引用的稳定
序列化结果充当哈希，同样能去重；之后若拿到字节（回填工具 / 重新出现），
会挂到同一条记录上补本体，id 不变。

``kind`` 区分真表情与普通图片：``mface`` / ``sticker`` 段、摘要含「表情」
的图片是 ``sticker``；其余普通 image 段（照片、截图）是 ``photo``。旧索引
没有该字段时按原生引用里的段类型与 ``summary`` 现场推导，读出来不会丢。

图片本体只收文件头能嗅探成 jpg/png/gif/webp 的字节，超过
``max_file_bytes`` 的不收——群聊里什么字节都可能混进来。

原生引用（``native_refs``）对存储层是不透明的 JSON，由各适配器自己解释，
约定形如 ``{"kind": 段类型, "data": {...}}``（如 OneBot 的 mface 段），
Telegram 回填的是 ``{"kind": "sticker", "file_id": ...}``。

对外接口保持小：:meth:`StickerStore.add` / :meth:`StickerStore.get` /
:meth:`StickerStore.find_by_tags` / :meth:`StickerStore.record_native_ref`，
以及回填用的 :meth:`StickerStore.attach_file` 与元数据写回
:meth:`StickerStore.update_metadata` / :meth:`StickerStore.record_use`。
索引是单个 JSON 文件，写入走「临时文件 + ``os.replace``」，与仓库里
其他运行时状态（Telegram offset 等）的落盘方式一致。旧索引缺新字段时按
空值读出，向后兼容。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
INDEX_NAME = "index.json"
FILES_DIR = "files"
DEFAULT_STICKER_DIR = "data/stickers"
# 图片本体收集上限；超过就不收，避免把群聊里的大图塞进表情库
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024

# 表情真伪分类：群聊里真正的表情（mface/贴纸/[动画表情]）与误收的
# 普通图片（照片、截图）分开存，后者永不参与选图。
KIND_STICKER = "sticker"
KIND_PHOTO = "photo"
STICKER_KINDS = frozenset({KIND_STICKER, KIND_PHOTO})

# 原生引用里能判定「这是表情」的段类型
_STICKER_REF_KINDS = frozenset({"mface", "sticker"})

# 文件头 -> 扩展名。只认表情包常见的四种格式；识别不了的不入库。
_IMAGE_HEADERS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def sniff_image_suffix(data: bytes | bytearray | None) -> str | None:
    """按文件头识别图片格式，返回扩展名（``.jpg`` / ``.png`` / ``.gif`` /
    ``.webp``）；识别不了返回 None。"""
    if not data:
        return None
    head = bytes(data[:16])
    for prefix, suffix in _IMAGE_HEADERS:
        if head.startswith(prefix):
            return suffix
    # WebP：RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def _clean_platform(value: Any) -> str:
    return str(value or "").strip()


def _clean_tag(value: Any) -> str:
    """标签归一：去空白、转小写（中文不受影响）。"""
    return str(value or "").strip().lower()


def infer_ref_kind(native_ref: Mapping[str, Any] | None) -> str:
    """按单个平台原生引用判定种类：有表情信号为 ``sticker``，否则 ``photo``。"""
    if not isinstance(native_ref, Mapping):
        return KIND_PHOTO
    ref_kind = str(native_ref.get("kind") or "").strip().lower()
    if ref_kind in _STICKER_REF_KINDS:
        return KIND_STICKER
    data = native_ref.get("data")
    if isinstance(data, Mapping) and "表情" in str(data.get("summary") or ""):
        return KIND_STICKER
    return KIND_PHOTO


def infer_kind(native_refs: Mapping[str, Any] | None) -> str:
    """按平台原生引用判定条目种类；拿不到信号时按表情处理。

    - 段类型是 ``mface`` / ``sticker``：一定是表情；
    - 图片段 ``summary`` 里带「表情」（如 ``[动画表情]``）：表情；
    - 其余图片段（``[图片]`` / 普通照片）：``photo``；
    - 完全没有原生引用（旧库直接入库的条目）保持 ``sticker``，不误隔离。
    """
    refs = native_refs if isinstance(native_refs, Mapping) else {}
    if not refs:
        return KIND_STICKER
    kinds = (infer_ref_kind(raw) for raw in refs.values())
    return KIND_STICKER if KIND_STICKER in kinds else KIND_PHOTO


def _merge_kinds(left: str, right: str) -> str:
    """合并两条同内容记录的种类：任一方是表情就按表情算。"""
    return KIND_STICKER if KIND_STICKER in (left, right) else KIND_PHOTO


@dataclass(frozen=True)
class Sticker:
    """表情库里的一条记录。"""

    id: str
    file: str  # 相对表情库根目录的路径；只有原生引用、没有图片本体时为空串
    tags: tuple[str, ...] = ()
    native_refs: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    created_at: float = 0.0
    # 视觉模型生成的一句话描述与情绪标签；旧索引读出来为空
    description: str = ""
    emotions: tuple[str, ...] = ()
    # 成功发出的次数（选择时作为偏好参考，WebUI 只读展示）
    uses: int = 0
    # sticker = 真表情；photo = 误收的普通图片（照片/截图），永不参选
    kind: str = KIND_STICKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "tags": list(self.tags),
            "native_refs": dict(self.native_refs),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "description": self.description,
            "emotions": list(self.emotions),
            "uses": self.uses,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Sticker":
        refs = data.get("native_refs")
        raw_uses = data.get("uses")
        uses = raw_uses if isinstance(raw_uses, int) and not isinstance(raw_uses, bool) else 0
        raw_emotions = data.get("emotions")
        raw_emotions = raw_emotions if isinstance(raw_emotions, (list, tuple)) else ()
        clean_refs = dict(refs) if isinstance(refs, Mapping) else {}
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in STICKER_KINDS:
            # 旧索引没有 kind：按原生引用的段类型与 summary 现场推导
            kind = infer_kind(clean_refs)
        return cls(
            id=str(data.get("id") or ""),
            file=str(data.get("file") or ""),
            tags=tuple(_clean_tag(tag) for tag in (data.get("tags") or []) if _clean_tag(tag)),
            native_refs=clean_refs,
            content_hash=str(data.get("content_hash") or ""),
            created_at=float(data.get("created_at") or 0.0),
            description=str(data.get("description") or "").strip(),
            emotions=tuple(tag for tag in (_clean_tag(item) for item in raw_emotions) if tag),
            uses=max(0, uses),
            kind=kind,
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
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
        kind: str | None = None,
    ) -> Sticker:
        """入库一张表情，按内容哈希去重；重复时合并标签、原生引用与种类。

        ``data`` 为图片本体；纯引用条目（只有平台侧引用、没有字节）允许
        ``data=None``，此时 ``file`` 为空。两者都没有时抛 ``ValueError``。

        图片本体先做格式嗅探与大小校验：不是 jpg/png/gif/webp 或超过
        ``max_file_bytes`` 时不收，有平台引用就退化成纯引用条目（记
        warning），否则抛 ``ValueError``。同一张图若先以纯引用入库、之后
        才拿到字节，会把本体重挂到原记录上，不新建重复条目。
        ``file_name`` / ``mime_type`` 只作兼容保留：扩展名一律以文件头
        嗅探结果为准，不信群聊里这两项自报的格式。

        ``kind`` 不传时按原生引用的段类型与 ``summary`` 推导（见
        :func:`infer_kind`）；重复收集合并时任一方是表情就按表情算。
        """
        refs = {
            _clean_platform(platform): ref
            for platform, ref in (native_refs or {}).items()
            if _clean_platform(platform)
        }
        incoming_kind = kind if kind in STICKER_KINDS else infer_kind(refs)
        payload = _prepare_image(data, max_file_bytes)
        if data is not None and payload is None:
            if not refs:
                raise ValueError("表情图片格式不受支持或超过大小上限")
            logger.warning("表情图片格式不受支持或超过大小上限，只保留原生引用")
        if payload is None and not refs:
            raise ValueError("表情入库需要图片数据或至少一个平台原生引用")
        if payload is not None:
            digest = hashlib.sha256(payload[0]).hexdigest()
        else:
            digest = _refs_digest(refs)
        sticker_id = "st_" + digest[:12]

        with self._lock:
            existing = self._stickers.get(sticker_id)
            if existing is None and payload is not None:
                existing = self._find_by_content_hash(digest)
            if existing is None and payload is not None and refs:
                # 之前只存了引用、现在拿到字节：挂到原记录上，id 保持稳定
                existing = self._find_ref_only(refs)
            if existing is not None:
                # 与 add 的去重语义保持一致：同内容的收集只并进旧条目
                merged = self._combine(
                    existing, tags=tags, refs=refs, kind=incoming_kind,
                    payload=payload, digest=digest,
                )
                if merged != existing:
                    self._stickers[existing.id] = merged
                    self._save()
                return merged

            relative = ""
            if payload is not None:
                relative = self._write_file(payload[0], payload[1], digest)
            sticker = Sticker(
                id=sticker_id,
                file=relative,
                tags=_merge_tags((), tags),
                native_refs=refs,
                content_hash=digest,
                created_at=time.time() if now is None else now,
                kind=incoming_kind,
            )
            self._stickers[sticker_id] = sticker
            self._save()
            return sticker

    def attach_file(
        self,
        sticker_id: str,
        data: bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
    ) -> bool:
        """给已有条目补上图片本体（回填工具与后补下载走这里）。

        格式不受支持、超过上限、条目不存在或已有本体时返回 False。
        """
        payload = _prepare_image(data, max_file_bytes)
        if payload is None:
            return False
        raw, suffix = payload
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None or existing.file:
                return False
            # 同一内容已在别的记录上：仍给本条补本体，但不覆盖那条的哈希
            relative = self._write_file(raw, suffix, digest)
            updated = replace(existing, file=relative, content_hash=digest)
            self._stickers[updated.id] = updated
            self._save()
            return True

    def update_metadata(
        self,
        sticker_id: str,
        *,
        description: str | None = None,
        emotions: Iterable[Any] | None = None,
    ) -> bool:
        """写回视觉模型生成的描述与情绪标签；条目不存在返回 False。"""
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None:
                return False
            changes: dict[str, Any] = {}
            if description is not None:
                text = str(description).strip()
                if text != existing.description:
                    changes["description"] = text
            if emotions is not None:
                merged = _merge_tags(existing.emotions, emotions)
                if merged != existing.emotions:
                    changes["emotions"] = merged
            if not changes:
                return True
            self._stickers[existing.id] = replace(existing, **changes)
            self._save()
            return True

    def record_use(self, sticker_id: str, amount: int = 1) -> bool:
        """发表情成功后累加``uses``；条目不存在返回 False。"""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return False
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None:
                return False
            self._stickers[existing.id] = replace(existing, uses=existing.uses + amount)
            self._save()
            return True

    def get(self, sticker_id: str) -> Sticker | None:
        with self._lock:
            return self._stickers.get(str(sticker_id or "").strip())

    def find_by_tags(
        self,
        tags: Iterable[Any] | None = None,
        *,
        limit: int | None = None,
        kind: str | None = None,
    ) -> list[Sticker]:
        """按标签找表情（命中任意一个即可）。

        ``tags`` 为空或 None 表示全量；``kind`` 传 ``"sticker"`` /
        ``"photo"`` 时只返回该种类（选图只传 ``"sticker"``）。
        ``limit`` 限制返回条数（按入库顺序）。
        """
        wanted = {_clean_tag(tag) for tag in (tags or []) if _clean_tag(tag)}
        with self._lock:
            items = list(self._stickers.values())
        if kind in STICKER_KINDS:
            items = [item for item in items if item.kind == kind]
        if wanted:
            items = [item for item in items if wanted & set(item.tags)]
        if limit is not None and limit >= 0:
            items = items[:limit]
        return items

    def record_native_ref(self, sticker_id: str, platform: str, ref: Any) -> bool:
        """回填平台原生引用（如 Telegram 返回的 file_id）；表情不存在返回 False。

        回填的原生引用若带来更强的种类信号（如 ``mface``），同步升级
        ``kind``，避免误收的图片一直留在隔离区。
        """
        platform = _clean_platform(platform)
        if not platform or ref in (None, ""):
            return False
        with self._lock:
            existing = self._stickers.get(str(sticker_id or "").strip())
            if existing is None:
                return False
            refs = {**existing.native_refs, platform: ref}
            kind = _merge_kinds(existing.kind, infer_kind({platform: ref}))
            self._stickers[existing.id] = replace(existing, native_refs=refs, kind=kind)
            self._save()
            return True

    def merge(self, sticker_ids: Iterable[str], *, keep: str | None = None) -> Sticker | None:
        """把多条同内容记录并成一条；返回保留的记录，没有可合并时返回 None。

        保留条目默认取最早 created_at 的那条（同时间按 id 最小），也可用
        ``keep`` 指定。合并规则与 :meth:`add` 的去重一致：tags 并集、
        ``native_refs`` 按平台合并（保留条目的引用优先）、uses 相加、
        种类任一方是表情即算表情；描述沿用保留条目的，空的才用对方的。
        被并条目的图片文件不删——按内容哈希命名，保留条目指向同一文件；
        万一指向别的文件也留着，不碰磁盘。

        供 ``tools/dedupe_stickers.py`` 修存量重复。
        """
        wanted = {str(item or "").strip() for item in sticker_ids} - {""}
        with self._lock:
            group = [self._stickers[key] for key in wanted if key in self._stickers]
            if len(group) < 2:
                return group[0] if group else None
            keeper = self._stickers.get(str(keep or "").strip())
            if keeper is None or keeper not in group:
                keeper = min(group, key=lambda item: (item.created_at, item.id))
            others = [item for item in group if item.id != keeper.id]
            tags = keeper.tags
            refs = dict(keeper.native_refs)
            kind = keeper.kind
            uses = keeper.uses
            file = keeper.file
            content_hash = keeper.content_hash
            description = keeper.description
            emotions = keeper.emotions
            for item in others:
                tags = _merge_tags(tags, item.tags)
                refs = {**item.native_refs, **refs}
                kind = _merge_kinds(kind, item.kind)
                uses += item.uses
                emotions = _merge_tags(emotions, item.emotions)
                description = description or item.description
                if not file and item.file:
                    file = item.file
                    content_hash = content_hash or item.content_hash
            merged = replace(
                keeper, tags=tags, native_refs=refs, kind=kind,
                uses=uses, file=file, content_hash=content_hash,
                description=description, emotions=emotions,
            )
            self._stickers[merged.id] = merged
            for item in others:
                self._stickers.pop(item.id, None)
            self._save()
            return merged

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

    def _write_file(self, data: bytes, suffix: str, digest: str) -> str:
        """按内容哈希落盘：``files/<哈希前16位><扩展名>``。"""
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

    def _find_by_content_hash(self, digest: str) -> Sticker | None:
        for item in self._stickers.values():
            if item.file and item.content_hash == digest:
                return item
        return None

    def _combine(
        self,
        existing: Sticker,
        *,
        tags: Iterable[Any],
        refs: Mapping[str, Any],
        kind: str,
        payload: tuple[bytes, str] | None,
        digest: str,
    ) -> Sticker:
        """把一次收集的字段并进已有条目（tags 并集、引用按平台、kind 取强、补本体）。"""
        merged_tags = _merge_tags(existing.tags, tags)
        merged_refs = {**refs, **existing.native_refs}
        merged_kind = _merge_kinds(existing.kind, kind)
        file = existing.file
        content_hash = existing.content_hash
        if payload is not None and not file:
            file = self._write_file(payload[0], payload[1], digest)
            content_hash = digest
        if (merged_tags == existing.tags and merged_refs == existing.native_refs
                and file == existing.file and content_hash == existing.content_hash
                and merged_kind == existing.kind):
            return existing
        return replace(
            existing,
            file=file,
            tags=merged_tags,
            native_refs=merged_refs,
            content_hash=content_hash,
            kind=merged_kind,
        )

    def _find_ref_only(self, refs: Mapping[str, Any]) -> Sticker | None:
        """找与这组平台引用匹配的纯引用条目（补本体时沿用原 id）。"""
        for item in self._stickers.values():
            if item.file:
                continue
            if all(item.native_refs.get(platform) == ref for platform, ref in refs.items()):
                return item
        return None

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


def _refs_digest(refs: Mapping[str, Any]) -> str:
    """纯引用条目的稳定哈希（引用序列化结果）。"""
    return hashlib.sha256(
        json.dumps(refs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _prepare_image(data: bytes | None, max_bytes: int | None) -> tuple[bytes, str] | None:
    """校验并归一图片字节：返回 (字节, 嗅探出的扩展名)；不合格返回 None。

    格式只按文件头嗅探，不信文件名或 Content-Type（群聊里这两项都不可靠）。
    """
    if data is None:
        return None
    raw = bytes(data)
    if not raw:
        return None
    if max_bytes is not None and max_bytes > 0 and len(raw) > max_bytes:
        return None
    suffix = sniff_image_suffix(raw)
    if suffix is None:
        return None
    return raw, suffix


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_STICKER_DIR",
    "KIND_PHOTO",
    "KIND_STICKER",
    "STICKER_KINDS",
    "Sticker",
    "StickerStore",
    "infer_kind",
    "infer_ref_kind",
    "sniff_image_suffix",
]
