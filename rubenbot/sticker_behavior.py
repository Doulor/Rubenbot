"""核心表情包行为：收集群聊表情，按话题选图用于独立回复或文字后补发。

收集时优先把图片本体落盘（base64 段或可下载的 URL），只有拿不到字节
时才退化成纯原生引用条目。群里的普通图片（照片、截图）默认不收，只有
配置 ``stickers.collect_photos`` 才入库；即便入库也标记为 ``photo``，
永不参与选图。选图分两级：

1. 语义检索（可选）：配置了 embedding 通道时，把每条表情的描述+情绪标签
   做成向量、缓存在 ``data/stickers/embeddings.json``，用上下文与情感提示
   做余弦检索，超过阈值直接命中；
2. 文本选择：把候选编号列表（情绪标签+描述）交给 fast 角色，只接受 JSON
   编号；
3. 两级都没有结果时回落到原有的标签匹配。

情感提示取自表达决策书已经算出的意图/增量点，不另做一套情感分析。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .adapters.base import SEND_STICKER, Segment
from .computer_fetch import pinned_public_url
from .http_body import CompressedBodyError, read_compressed_body
from .llm import parse_json_payload
from .memory.retrieve import tokenize
from .stickers import KIND_PHOTO, KIND_STICKER, infer_ref_kind, sniff_image_suffix
from .vision_image import Encoder as _ImageEncoder, prepare_image

logger = logging.getLogger(__name__)

COLLECTABLE_SEGMENTS = frozenset({"sticker", "image"})
VISUAL_SEGMENTS = COLLECTABLE_SEGMENTS | {"face"}
# 所有入库图片都排队，但不让一批多图同时占满下载连接和视觉通道。
VISION_CONCURRENCY = 4
MAX_TAGS = 8
# 文本选择的候选上限：太长的编号列表会让模型乱选
MAX_SELECT_CANDIDATES = 50
# 语义检索的查询文本上限：情感提示 + 最近对话尾部
MAX_QUERY_CHARS = 300
# 二级文本选择与描述生成的等待上限（秒）
SELECT_TIMEOUT = 15.0
DESCRIBE_TIMEOUT = 30.0
EMBEDDINGS_NAME = "embeddings.json"
EMBEDDINGS_VERSION = 1
# 启动补描述每批条数：串行跑，避免一次性对上百张表情并发打视觉模型
DESCRIBE_BACKFILL_BATCH = 10
# 下载表情图片时的连接/读取上限（秒）
DOWNLOAD_TIMEOUT = 10.0
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
# 识图时序默认值：与 StickerConfig 保持一致，供注入的简易配置（测试/插件）回退
_DEFAULT_VISION_WAIT = 15.0
_DEFAULT_FETCH_RETRIES = 1
_DEFAULT_FETCH_BACKOFF = 0.4
_DEFAULT_IMAGE_MAX_SIDE = 1024
_DEFAULT_IMAGE_QUALITY = 80
# 裁定日志里留多少条近期图片状态；超了从最旧的丢
_VISUAL_STATUS_LIMIT = 512
# 这些阶段说明字节没拿到或模型没给出描述，与「还在途」要分开记。
_FETCH_FAILURE_STAGES = frozenset({
    "image_bytes", "image_source", "invalid_image", "no_model",
    "vision_model", "vision_model_timeout", "invalid_description",
    "writeback", "writeback_skipped", "ingest", "fetch",
})

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DESCRIBE_PROMPT = (
    "这是一张聊天表情包。请用一句中文描述画面内容与常见使用场景，"
    "并给出最多 5 个中文情绪/语气标签。"
    '只返回 JSON，例如 {"description": "猫咪大笑，用来接梗", "emotions": ["开心", "大笑"]}。'
)

IMAGE_PROMPT = (
    '用一句中文记录画面中可直接看到的内容，逐字抄下清晰的专名和说明。'
    '不清晰的文字留空。返回 JSON：{"description":"画面所见","emotions":[]}'
)

SELECT_PROMPT = (
    "你在替聊天机器人挑一张表情包回复当前对话。"
    "下面是候选编号列表，每行含情绪标签与画面描述。"
    "只返回 JSON，例如 {\"index\": 3}；都不合适就返回 {\"index\": 0}。"
    "不要输出解释或其他内容。"
)


class StickerBehavior:
    """表情包收集与发送决策；随机源、时钟和库均可注入测试。"""

    def __init__(
        self,
        config: Any,
        *,
        store_getter: Callable[[], Any] | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
        downloader: Callable[[str, int], Awaitable[bytes | None]] | None = None,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
        image_encoder: _ImageEncoder | None = None,
    ) -> None:
        self.config = config
        self._store_getter = store_getter
        self._store = None
        self.rng = rng or random.Random()
        self.clock = clock or time.monotonic
        self._last_sent_at: dict[tuple[str, str], float] = {}
        self._verified_adapter: dict[tuple[str, str], Any] = {}
        self._downloader = downloader
        self._embed_fn = embed_fn
        # 视觉前压缩可注入（测试不依赖 Pillow/ffmpeg）
        self._image_encoder = image_encoder
        self._embed_client: httpx.AsyncClient | None = None
        # 后台任务与缓存状态；都只在事件循环里访问
        self._bg_tasks: set[asyncio.Task] = set()
        self._visual_ready: dict[int, asyncio.Future[None]] = {}
        # 每张图的描述状态：ready / timeout / 失败阶段；只供裁定日志区分用途。
        self._visual_status: dict[int, str] = {}
        self._visual_started: dict[int, float] = {}
        self._vision_slots = asyncio.Semaphore(VISION_CONCURRENCY)
        # 成功/失败按图片段；等待超时按本轮未就绪的流水计，不取消后台回写。
        self.visual_counts = dict(success=0, failed=0, timeout=0)
        self._pending_descriptions: set[str] = set()
        self._vision_skip_logged = False
        self._vision_broken = False
        self._last_runtime: Any = None
        self._describe_attempted: set[str] = set()
        self._embedding_cache: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return getattr(self.config, "enabled", False) is True

    @property
    def store(self) -> Any:
        if self._store is None and self._store_getter is not None:
            self._store = self._store_getter()
        return self._store

    async def aclose(self) -> None:
        """先收回后台任务，避免关库后再回写；随后关闭自建客户端。"""
        pending = list(self._bg_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._embed_client is not None and not self._embed_client.is_closed:
            await self._embed_client.aclose()
        self._embed_client = None

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------

    def ingest(self, runtime: Any, event: Any, raw_id: int) -> None:
        """同步登记流水的后台任务，任何网络 I/O 都不占用摄入或防抖窗口。"""
        segments = [s for s in event.segments if s.type in VISUAL_SEGMENTS]
        if not segments:
            return
        descriptions = [segment_description(s) for s in segments]
        ready = sum(bool(text) for text in descriptions)
        self.visual_counts["success"] += ready  # 适配器自带描述已经同步写入
        collect = event.is_group and self.enabled and self.config.collect
        self._prune_visual_status()
        if ready == len(segments) and not collect:
            self._visual_status[raw_id] = "ready"
            return
        self._visual_started[raw_id] = self.clock()
        task = asyncio.create_task(self._ingest_images(
            runtime, raw_id, event.platform, event.chat_type, segments, descriptions,
            topic_tags(event.text) if collect else [], collect, self.clock(),
        ))
        self._bg_tasks.add(task)
        if ready < len(segments):
            self._visual_ready[raw_id] = asyncio.get_running_loop().create_future()
        task.add_done_callback(lambda done: self._finish_ingest(raw_id, done))

    def _release_visual(self, raw_id: int) -> None:
        ready = self._visual_ready.pop(raw_id, None)
        # 放行的这一刻至少已有一段可见文字（适配器说明或刚落库的描述）；
        # 已记下的失败阶段保留，不把它翻成成功。
        if self._visual_status.get(raw_id, "") in ("", "timeout"):
            self._visual_status[raw_id] = "ready"
        if ready is not None:
            ready.set_result(None)

    def visual_pending(self, raw_id: int | None) -> bool:
        """这张流水还有值得等的描述请求吗（已等满上限的不再重复等）。"""
        return (raw_id is not None and raw_id in self._visual_ready
                and self._visual_status.get(raw_id) != "timeout")

    def _finish_ingest(self, raw_id: int, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            self._failure(raw_id, "ingest", exc)
        self._release_visual(raw_id)

    async def wait_for_visuals(self, raw_ids: Iterable[int], *, timeout: float) -> dict[str, Any]:
        """等本批触发图落的描述；每个流水从**自己入库时**起算时限。

        只应在触发消息本身含图时调用；历史/引用里的配图不在这里等。
        ``timeout`` 是单张图从入库起最长等多久（配置 ``stickers.vision_wait_seconds``）。
        返回可写进 ``decision_log`` 的等待报告：谁等到了、谁等满超时、
        谁在字节或模型阶段失败、谁压根没有描述请求。

        超时不取消后台任务：迟到的描述仍会回写流水，下一轮能看到。
        """
        ids = list(dict.fromkeys(rid for rid in raw_ids if rid is not None))
        started = self.clock()
        # 已经超时过的流水不再重复等：下一轮不该再被同一张图拖满一遍。
        pending = {rid: self._visual_ready[rid] for rid in ids
                   if rid in self._visual_ready and self._visual_status.get(rid) != "timeout"}
        if pending:
            now = self.clock()
            # 每张图从入库起算自己的截止时间；本轮最多等到最晚的那张，
            # 但整体不超过一个 `timeout`，排队很久的轮次不会又等满一轮。
            remaining = max(
                (self._visual_started.get(rid, started) + timeout - now for rid in pending),
                default=0.0,
            )
            wait_for = min(timeout, max(0.0, remaining))
            _, still_pending = await asyncio.wait(set(pending.values()), timeout=wait_for)
            for rid, ready in pending.items():
                if ready not in still_pending:
                    continue
                self.visual_counts["timeout"] += 1
                logger.warning("图片感知 raw_id=%s type=planner_timeout", rid)
                self._visual_status[rid] = "timeout"
        report: dict[str, Any] = {
            "waited_ms": int((self.clock() - started) * 1000),
            # 调用时仍在途的条数；0 说明这些图本来就有描述，没真的等。
            "pending": len(pending),
            "ready": [], "timed_out": [], "failed": [], "no_request": [],
        }
        for rid in ids:
            status = self._visual_status.get(rid, "")
            if status == "ready":
                report["ready"].append(rid)
            elif status == "timeout":
                report["timed_out"].append(rid)
            elif status.startswith("failed") or status in _FETCH_FAILURE_STAGES:
                report["failed"].append({"raw_id": rid, "stage": status.removeprefix("failed:")})
            else:
                report["no_request"].append(rid)
        return report

    def _failure(self, raw_id: int, stage: str, exc: Exception | None = None) -> None:
        self.visual_counts["failed"] += 1
        # 一段成功、另一段失败时仍以已有描述为准，不把成图报成失败。
        if self._visual_status.get(raw_id) != "ready":
            self._visual_status[raw_id] = stage
        logger.warning("图片感知失败 raw_id=%s type=%s error=%s", raw_id, stage,
                       type(exc).__name__ if exc is not None else "-")

    def _prune_visual_status(self) -> None:
        # 只为近期裁定留状态；超过上限先从最旧的丢，避免无限增长。
        # 以入库时间为序清理，防止卡住的任务让 started 单独膨胀。
        while len(self._visual_started) >= _VISUAL_STATUS_LIMIT:
            oldest = next(iter(self._visual_started))
            self._visual_started.pop(oldest, None)
            self._visual_status.pop(oldest, None)
        if len(self._visual_status) >= _VISUAL_STATUS_LIMIT:
            for key in list(self._visual_status)[: len(self._visual_status) - _VISUAL_STATUS_LIMIT // 2]:
                self._visual_status.pop(key, None)

    async def _fetch_image_bytes(self, segment: Segment, resolve: Any, raw_id: int) -> tuple[bytes | None, str]:
        """取图片字节（直接图源 → 平台补取），失败按配置有限次重试。

        返回 ``(字节, 失败阶段)``：拿到有效图片时阶段为空；重试用尽仍失败时，
        阶段是 ``image_bytes``（字节没拉到）或 ``image_source``（平台补取失败），
        供裁定日志把「永远看不见」与「描述还在途」分开记。
        """
        attempts = self.fetch_retries + 1
        stage = "image_bytes"
        for attempt in range(1, attempts + 1):
            raw: bytes | None = None
            try:
                raw = await self._image_bytes(segment.data)
            except Exception as exc:  # noqa: BLE001 - 取源异常要重试，不打断后台任务
                stage = "image_bytes"
                logger.warning("图片取源失败 raw_id=%s type=%s error=%s attempt=%s", raw_id, stage,
                               type(exc).__name__, attempt)
            if sniff_image_suffix(raw):
                return raw, ""
            if resolve is not None:
                try:
                    source = await resolve(segment)
                    fetched = await self._image_bytes(source) if source else None
                    if sniff_image_suffix(fetched):
                        return fetched, ""
                    if raw and sniff_image_suffix(raw):
                        return raw, ""
                except Exception as exc:  # noqa: BLE001 - 平台补取失败同样重试
                    stage = "image_source"
                    logger.warning("图片补取失败 raw_id=%s type=%s error=%s attempt=%s", raw_id, stage,
                                   type(exc).__name__, attempt)
            elif raw:
                # 不是可识别的图片也不是网络失败：直接按无效图处理，不重试。
                return raw, ""
            if attempt < attempts:
                await asyncio.sleep(self.fetch_backoff_seconds)
        return None, stage

    async def _ingest_images(self, runtime, raw_id, platform, chat_type, segments, descriptions,
                             tags, collect, started) -> None:
        llm = _resolve_llm(runtime)
        adapter = getattr(_resolve_bot(runtime), "adapters", {}).get(platform)
        resolve = getattr(adapter, "resolve_image_source", None)
        # 先处理缺描述的段；不让纯贴纸收集拖延已全部就绪的流水。
        order = sorted(range(len(segments)), key=lambda i: bool(descriptions[i]))
        for index in order:
            segment = segments[index]
            if descriptions[index]:
                self._release_visual(raw_id)
            async with self._vision_slots:
                ref = self._collection_ref(segment) if collect else None
                if descriptions[index] and ref is None:
                    continue
                try:
                    raw, fetch_stage = await self._fetch_image_bytes(segment, resolve, raw_id)
                except Exception as exc:  # noqa: BLE001 - 兜底：取源失败只记警告
                    logger.warning("图片取源失败 raw_id=%s type=image_source error=%s", raw_id, type(exc).__name__)
                    raw, fetch_stage = None, "image_source"
                sticker = None
                if ref is not None:
                    try:
                        sticker = self._store_segment(self.store, platform, segment, tags, raw, ref)
                    except Exception as exc:  # 选图库写失败不妨碍本轮看图
                        logger.warning("表情收集失败 raw_id=%s error=%s", raw_id, type(exc).__name__)
                if descriptions[index]:
                    if sticker and sticker.file and sticker.kind == KIND_STICKER:
                        self._schedule_description(runtime, self.store, sticker)
                    continue
                if not raw:
                    self._failure(raw_id, fetch_stage or "image_bytes")
                    continue
                suffix = sniff_image_suffix(raw)
                if not suffix or len(raw) > self.max_file_bytes:
                    self._failure(raw_id, "invalid_image")
                    continue
                if llm is None:
                    self._failure(raw_id, "no_model")
                    continue
                try:
                    payload_bytes, mime = await prepare_image(
                        raw, suffix,
                        max_side=self.image_max_side, quality=self.image_quality,
                        encoder=self._image_encoder,
                    )
                    output = await vision_request(llm, payload_bytes, mime, IMAGE_PROMPT, 'image_description')
                    payload = parse_json_payload(output)
                except Exception as exc:
                    self._failure(raw_id, "vision_model_timeout" if isinstance(exc, TimeoutError) else "vision_model", exc)
                    continue
                text = payload.get("description") if isinstance(payload, Mapping) else None
                if not isinstance(text, str) or not text.strip():
                    self._failure(raw_id, "invalid_description")
                    continue
                descriptions[index] = text.strip()[:500]
                try:
                    saved = runtime.store.set_raw_visual_text(raw_id, "\n".join(filter(None, descriptions)),
                                                              chat_type=chat_type)
                except Exception as exc:
                    self._failure(raw_id, "writeback", exc)
                    continue
                if not saved:  # 流水被删除或隐私模式变更，不留视觉描述副本。
                    self._failure(raw_id, "writeback_skipped")
                    continue
                self.visual_counts["success"] += 1
                # 至少一段拿到描述就算「等到了」；别的段失败不能把它翻成失败。
                self._visual_status[raw_id] = "ready"
                logger.info("图片感知完成 raw_id=%s part=%s elapsed_ms=%d", raw_id, index + 1,
                            (self.clock() - started) * 1000)
                if sticker and sticker.kind == KIND_STICKER and not sticker.description:
                    try:
                        self.store.update_metadata(sticker.id, description=descriptions[index],
                                                   emotions=payload.get('emotions') or [])
                    except Exception as exc:
                        logger.warning("贴纸描述回填失败 raw_id=%s error=%s", raw_id, type(exc).__name__)

    async def on_message(self, runtime: Any, event: Any) -> None:
        """仅收集表情的入口（没有可回写的消息流水）。"""
        if not event.is_group or not self.enabled or not self.config.collect:
            return
        for segment in event.segments:
            ref = self._collection_ref(segment)
            if ref is None:
                continue
            try:
                raw = await self._image_bytes(segment.data)
                sticker = self._store_segment(self.store, event.platform, segment, topic_tags(event.text), raw, ref)
                if sticker and sticker.file and sticker.kind == KIND_STICKER:
                    self._schedule_description(runtime, self.store, sticker)
            except Exception as exc:
                logger.warning("表情收集失败：%s", type(exc).__name__)

    async def _image_bytes(self, payload: Mapping) -> bytes | None:
        native = payload.get("native_ref")
        encoded = payload.get("base64")
        if not encoded and str(payload.get("file", "")).startswith(("base64://", "data:")):
            encoded = payload["file"]
        if encoded:
            if len(encoded) > 4 * self.max_file_bytes // 3 + 128:
                return None
            return _decode_base64(encoded)
        return await self._download_image(_candidate_url(payload, native))

    def _collection_ref(self, segment: Segment) -> Mapping | None:
        if segment.type not in COLLECTABLE_SEGMENTS or not segment.data:
            return None
        ref = segment.data.get("native_ref") if segment.type == "sticker" else None
        if not isinstance(ref, Mapping):
            ref = {"kind": segment.type, "data": segment.data}
        if infer_ref_kind(ref) == KIND_PHOTO and not getattr(self.config, "collect_photos", False):
            logger.debug("跳过普通图片（默认只收表情；stickers.collect_photos 可放开）")
            return None
        return ref

    def _store_segment(self, store, platform, segment, tags, raw, ref):
        payload = segment.data
        return store.add(
            raw,
            tags=tags,
            native_refs={platform: ref} if platform else None,
            file_name=payload.get("filename") or payload.get("name") or None,
            mime_type=payload.get("mime_type") or payload.get("content_type") or None,
            max_file_bytes=self.max_file_bytes,
            kind=infer_ref_kind(ref),
        )

    async def _download_image(self, url: str) -> bytes | None:
        """按收集上限下载图片字节；失败时只记 warning，退回纯引用条目。"""
        if not url:
            return None
        limit = self.max_file_bytes
        try:
            if self._downloader is not None:
                return await self._downloader(url, limit)
            return await _default_download(url, limit)
        except Exception as exc:  # noqa: BLE001 - 下载失败不影响收集
            logger.warning("表情图片下载失败：%s", type(exc).__name__)
            return None

    @property
    def max_file_bytes(self) -> int:
        value = getattr(self.config, "max_file_bytes", _DEFAULT_MAX_BYTES)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return _DEFAULT_MAX_BYTES
        return int(value)

    @property
    def vision_wait_seconds(self) -> float:
        return _float_setting(self.config, "vision_wait_seconds", _DEFAULT_VISION_WAIT)

    @property
    def fetch_retries(self) -> int:
        return int(_float_setting(self.config, "vision_fetch_retries", _DEFAULT_FETCH_RETRIES))

    @property
    def fetch_backoff_seconds(self) -> float:
        return _float_setting(self.config, "vision_fetch_backoff_seconds", _DEFAULT_FETCH_BACKOFF)

    @property
    def image_max_side(self) -> int:
        return int(_float_setting(self.config, "vision_image_max_side", _DEFAULT_IMAGE_MAX_SIDE))

    @property
    def image_quality(self) -> int:
        return int(_float_setting(self.config, "vision_image_quality", _DEFAULT_IMAGE_QUALITY))

    # ------------------------------------------------------------------
    # 描述生成（vision 角色走框架回退链，默认落到 chat 模型）
    # ------------------------------------------------------------------

    def _schedule_description(self, runtime: Any, store: Any, sticker: Any) -> None:
        """存本体成功后后台生成描述；没配视觉模型只记一次 info。"""
        if getattr(sticker, "description", ""):
            return
        self._last_runtime = runtime
        llm = _resolve_llm(runtime)
        if llm is None:
            if not self._vision_skip_logged:
                logger.info("无 LLM 客户端，跳过表情包描述生成")
                self._vision_skip_logged = True
            return
        if self._vision_broken:
            return
        sticker_id = str(getattr(sticker, "id", "") or "")
        if not sticker_id or sticker_id in self._pending_descriptions:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending_descriptions.add(sticker_id)
        task = loop.create_task(self._build_description(store, sticker_id, llm))
        self._bg_tasks.add(task)
        task.add_done_callback(lambda finished: self._finish_bg(finished, sticker_id))

    def _finish_bg(self, task: asyncio.Task, sticker_id: str) -> None:
        self._bg_tasks.discard(task)
        self._pending_descriptions.discard(sticker_id)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 后台描述失败不影响收集
            self._vision_broken = True
            logger.warning("表情包描述生成失败（%s）：%s，本进程内暂停描述生成", sticker_id, exc)

    async def _build_description(self, store: Any, sticker_id: str, llm: Any) -> None:
        sticker = store.get(sticker_id)
        path = store.path_of(sticker) if sticker is not None else None
        if path is None:
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("读取表情图片失败（%s）：%s", sticker_id, exc)
            return
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
        raw = await vision_request(llm, data, mime, DESCRIBE_PROMPT, "sticker_description")
        parsed = parse_json_payload(raw)
        if isinstance(parsed, Mapping):
            description = str(parsed.get("description") or parsed.get("desc") or "").strip()
            emotions = parsed.get("emotions") or parsed.get("emotion") or []
        else:
            description = str(raw or "").strip()
            emotions = []
        if isinstance(emotions, str):
            emotions = [part for part in emotions.replace("，", ",").split(",") if part.strip()]
        if not description and not emotions:
            logger.warning("表情包描述生成为空（%s）", sticker_id)
            return
        store.update_metadata(sticker_id, description=description, emotions=emotions)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def on_after_reply(self, runtime: Any, event: Any, reply: str) -> None:
        """在 core 发送完回复后按概率/冷却补一张表情。"""
        await self.try_send(runtime, event, probability=self.config.send_probability)

    async def try_send(
        self,
        runtime: Any,
        event: Any,
        *,
        probability: float,
        emotion_hint: str = "",
        begin_send: Callable[[], bool] | None = None,
    ) -> bool:
        """独立回复与文字后补发共用选图、冷却和发送；只有能力调用成功才算已发。"""
        try:
            if not self.enabled or self.rng.random() >= probability:
                return False
            now = self.clock()
            if self._in_cooldown(event, now):
                return False
            bot = _resolve_bot(runtime)
            platform = event.platform
            if bot is None or not _can_send(bot, platform):
                return False
            store = self.store
            if store is None:
                return False
            sticker = await self._pick(runtime, store, event, emotion_hint)
            if sticker is None or (begin_send and not begin_send()):
                return False
            # 冷却按尝试计，避免平台故障时每轮重试；它不是发言额度。
            self._last_sent_at[_chat_key(event)] = now
            ok, reason = await bot.call_capability(
                platform, SEND_STICKER, sticker=store.reference(sticker),
                chat_id=event.chat_id, chat_type=event.chat_type,
            )
            route = (platform, event.chat_type)
            if ok:
                self._verified_adapter[route] = bot.adapters.get(platform)
            else:
                self._verified_adapter.pop(route, None)
            if not ok:
                logger.debug("发表情失败（忽略）：%s", reason)
            elif sticker.id:
                try:
                    store.record_use(sticker.id)
                except Exception as exc:  # noqa: BLE001 - 计数失败不影响发送
                    logger.debug("表情使用计数失败：%s", exc)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.debug("表情包发送异常：%s", exc)
            return False

    def available_for(self, event: Any, adapter: Any) -> bool:
        """只读验通记录、概率、冷却和实际可选图片；控制台占位不是发表情。"""
        return bool(self.enabled and event.platform != 'console'
                    and self._verified_adapter.get((event.platform, event.chat_type)) is adapter
                    and max(self.config.send_probability, self.config.nonverbal_probability) > 0
                    and not self._in_cooldown(event, self.clock()) and self._store is not None
                    and any(_has_local_file(self._store, item) for item in
                            self._store.find_by_tags(None, kind=KIND_STICKER)))

    def _in_cooldown(self, event: Any, now: float) -> bool:
        last = self._last_sent_at.get(_chat_key(event))
        return last is not None and now - last < getattr(self.config, "cooldown_seconds", 300.0)

    async def _pick(self, runtime: Any, store: Any, event: Any, emotion_hint: str) -> Any:
        """两级选择 + 标签回落；候选只取有本地文件的真表情（photo 永不参选）。"""
        text = str(getattr(event, "text", "") or "")
        candidates = [
            item for item in store.find_by_tags(None, kind=KIND_STICKER)
            if _has_local_file(store, item)
        ]
        if not candidates:
            return None
        context = _context_text(runtime, event) or text
        # 情感提示优先，上下文只留尾部（最新）一段：别把整屏历史塞进向量接口
        tail = context[-MAX_QUERY_CHARS:]
        query = " ".join(part for part in (emotion_hint.strip(), tail) if part).strip()

        picked = await self._pick_by_embedding(store, candidates, query)
        if picked is not None:
            return picked
        picked = await self._pick_by_llm(runtime, candidates, query, emotion_hint)
        if picked is not None:
            return picked
        return self._pick_by_tags(candidates, text, context)

    def _pick_by_tags(self, candidates: list[Any], text: str, context: str) -> Any:
        tags = topic_tags(text) or topic_tags(context)
        if tags:
            matched = [item for item in candidates if set(tags) & set(item.tags)]
            if matched:
                return self.rng.choice(matched)
        return self.rng.choice(candidates) if candidates else None

    # ------------------------------------------------------------------
    # 一级：描述 embedding 检索
    # ------------------------------------------------------------------

    def embedding_configured(self) -> bool:
        if self._embed_fn is not None:
            return True
        return bool(
            str(getattr(self.config, "embedding_base_url", "") or "").strip()
            and str(getattr(self.config, "embedding_model", "") or "").strip()
        )

    async def _pick_by_embedding(self, store: Any, candidates: list[Any], query: str) -> Any:
        if not query or not self.embedding_configured():
            return None
        try:
            texts = {item.id: _embedding_text(item) for item in candidates}
            texts = {key: value for key, value in texts.items() if value}
            if not texts:
                return None
            vectors = await self._embedding_vectors(store, texts)
            query_vector = (await self._embed_texts([query]))[0]
            best_id, best_score = "", -1.0
            for sticker_id, vector in vectors.items():
                score = _cosine(query_vector, vector)
                if score > best_score:
                    best_id, best_score = sticker_id, score
            threshold = _float_setting(self.config, "embedding_threshold", 0.3)
            if best_score < threshold:
                return None
            picked = store.get(best_id)
            return picked if picked is not None and _has_local_file(store, picked) else None
        except Exception as exc:  # noqa: BLE001 - 语义检索失败退回文本选择
            logger.warning("表情描述向量检索失败，降级文本选择：%s", exc)
            return None

    async def _embedding_vectors(self, store: Any, texts: dict[str, str]) -> dict[str, list[float]]:
        """读缓存；缺失或过期的描述按批重新向量化并落盘。"""
        cache = self._load_embedding_cache(store)
        model = self._embedding_signature()
        now = time.time()
        interval = _float_setting(self.config, "embedding_refresh_seconds", 21600.0)
        stale = (
            cache.get("model") != model
            or interval <= 0
            or now - float(cache.get("updated_at") or 0.0) > interval
        )
        items = cache.get("items") if isinstance(cache.get("items"), dict) else {}
        result: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        for sticker_id, text in texts.items():
            entry = items.get(sticker_id)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if (not stale and isinstance(entry, Mapping) and entry.get("hash") == digest
                    and isinstance(entry.get("vector"), list) and entry["vector"]):
                result[sticker_id] = [float(value) for value in entry["vector"]]
            else:
                missing[sticker_id] = text
        if missing:
            keys = list(missing)
            vectors = await self._embed_texts([missing[key] for key in keys])
            if len(vectors) != len(keys):
                raise ValueError("embedding 返回数量与请求不符")
            for key, vector in zip(keys, vectors):
                if not vector or not all(math.isfinite(value) for value in vector):
                    continue
                result[key] = vector
                items[key] = {
                    "hash": hashlib.sha256(missing[key].encode("utf-8")).hexdigest(),
                    "vector": vector,
                }
            self._save_embedding_cache(store, {"model": model, "updated_at": now, "items": items})
        return result

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self._embed_fn is not None:
            vectors = await self._embed_fn(texts)
            return [[float(value) for value in vector] for vector in vectors]
        base = str(getattr(self.config, "embedding_base_url", "") or "").strip().rstrip("/")
        model = str(getattr(self.config, "embedding_model", "") or "").strip()
        api_key = str(getattr(self.config, "embedding_api_key", "") or "").strip()
        if self._embed_client is None or self._embed_client.is_closed:
            self._embed_client = httpx.AsyncClient(base_url=base, timeout=20.0)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = await self._embed_client.post(
            "/embeddings", json={"model": model, "input": texts}, headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("embedding 响应格式不符")
        rows = sorted(rows, key=lambda row: row.get("index", 0) if isinstance(row, Mapping) else 0)
        return [[float(value) for value in row.get("embedding") or []] for row in rows]

    def _embedding_signature(self) -> str:
        return "|".join([
            str(getattr(self.config, "embedding_base_url", "") or "").strip(),
            str(getattr(self.config, "embedding_model", "") or "").strip(),
        ])

    def _embedding_path(self, store: Any) -> Path:
        return Path(getattr(store, "root", ".")) / EMBEDDINGS_NAME

    def _load_embedding_cache(self, store: Any) -> dict[str, Any]:
        if self._embedding_cache is not None:
            return self._embedding_cache
        path = self._embedding_path(store)
        data: dict[str, Any] = {}
        try:
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as exc:  # noqa: BLE001 - 坏缓存按空缓存处理
            logger.warning("读取表情向量缓存失败（按空缓存处理）：%s", exc)
        self._embedding_cache = data
        return data

    def _save_embedding_cache(self, store: Any, data: dict[str, Any]) -> None:
        self._embedding_cache = data
        path = self._embedding_path(store)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": EMBEDDINGS_VERSION, **data}
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败下次重算
            logger.warning("保存表情向量缓存失败：%s", exc)

    # ------------------------------------------------------------------
    # 二级：fast 角色文本选择
    # ------------------------------------------------------------------

    async def _pick_by_llm(
        self, runtime: Any, candidates: list[Any], context: str, emotion_hint: str,
    ) -> Any:
        llm = _resolve_llm(runtime)
        if llm is None:
            return None
        # 候选全都没有描述/情绪时，编号列表对模型没有信息量：直接回落标签匹配，
        # 不为一次注定无效的选择多打一轮模型（旧库、没配 vision 时的常态）。
        if not any(getattr(item, "description", "") or getattr(item, "emotions", ()) for item in candidates):
            return None
        pool = candidates[:MAX_SELECT_CANDIDATES]
        lines = []
        for index, item in enumerate(pool, start=1):
            emotions = "、".join(item.emotions) or "无"
            description = getattr(item, "description", "") or "无描述"
            lines.append(f"{index}. 情绪：{emotions}；描述：{description}")
        hint = emotion_hint.strip() or "无"
        messages = [
            {"role": "system", "content": SELECT_PROMPT},
            {"role": "user", "content": (
                f"【本轮情感提示】{hint}\n"
                f"【最近对话】{context[:600] or '无'}\n"
                "【候选表情】\n" + "\n".join(lines)
            )},
        ]
        try:
            raw = await asyncio.wait_for(
                llm.complete(messages, role="fast", task="sticker_select", json_mode=True),
                timeout=SELECT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 选择失败按无选择处理
            logger.debug("表情文本选择失败：%s", exc)
            return None
        parsed = parse_json_payload(raw)
        index: Any = None
        if isinstance(parsed, Mapping):
            for key in ("index", "id", "choice", "number"):
                if key in parsed:
                    index = parsed[key]
                    break
        elif isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
            index = parsed
        if isinstance(index, str):
            index = int(index) if index.strip().isdigit() else None
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(pool):
            return None
        return pool[index - 1]

    async def reschedule_descriptions(self, runtime: Any) -> int:
        """给缺描述的存量条目补后台描述任务，返回排上的条数。

        启动时调用。只排一小批（``DESCRIBE_BACKFILL_BATCH``）并串行执行，
        避免一次性对上百张表情并发打视觉模型；一批补完自动续排下一批。
        vision 角色按框架回退链落到 chat 模型，不要求单独配置；生成连续
        失败会熔断，本进程内不再排。
        """
        store = self.store
        if store is None:
            return 0
        self._last_runtime = runtime
        llm = _resolve_llm(runtime)
        if llm is None:
            if not self._vision_skip_logged:
                logger.info("无 LLM 客户端，跳过表情包描述生成")
                self._vision_skip_logged = True
            return 0
        if self._vision_broken:
            return 0
        targets = self._describe_targets(store)
        if not targets:
            return 0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return 0
        task = loop.create_task(self._backfill_chain(store, targets, llm))
        self._bg_tasks.add(task)
        task.add_done_callback(self._finish_chain)
        return len(targets)

    def _describe_targets(self, store: Any) -> list[str]:
        return [
            item.id for item in store.find_by_tags(None, kind=KIND_STICKER)
            if item.file and not item.description
            and item.id not in self._pending_descriptions
            and item.id not in self._describe_attempted
        ][:DESCRIBE_BACKFILL_BATCH]

    async def _backfill_chain(self, store: Any, targets: list[str], llm: Any) -> None:
        """串行一批批补描述，直到没有新目标或熔断；全程一个任务内完成。"""
        batch = targets
        while batch:
            self._pending_descriptions.update(batch)
            try:
                await self._describe_batch(store, batch, llm)
            finally:
                self._pending_descriptions.difference_update(batch)
            if self._vision_broken:
                break
            batch = self._describe_targets(store)

    def _finish_chain(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 后台补描述失败不影响主流程
            logger.warning("表情包存量补描述异常结束：%s", exc)

    async def _describe_batch(self, store: Any, sticker_ids: list[str], llm: Any) -> None:
        failures = 0
        for sticker_id in sticker_ids:
            self._describe_attempted.add(sticker_id)
            try:
                item = store.get(sticker_id)
                # 普通图片不参选，也不值得花视觉额度补描述
                if item is not None and getattr(item, "kind", KIND_STICKER) != KIND_STICKER:
                    continue
                await self._build_description(store, sticker_id, llm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败继续下一条
                failures += 1
                logger.warning("表情包描述生成失败（%s）：%s", sticker_id, exc)
                if failures >= 2:
                    # 连续失败多半是模型不支持视觉：熔断，别把整批额度烧完
                    self._vision_broken = True
                    logger.warning("描述生成连续失败，本进程内暂停描述生成")
                    break


def segment_description(segment: Any) -> str:
    """适配器带来的可见文字；通用占位符、文件名和地址不是图片说明。"""
    if segment.type in VISUAL_SEGMENTS:
        for key in ("description", "alt", "summary"):
            value = segment.data.get(key)
            if isinstance(value, str) and value.strip() not in {"", "[图片]", "[动画表情]", "[表情]"}:
                return value.strip()[:500]
    return ""


async def vision_request(llm, data: bytes, mime: str, prompt: str, task: str) -> str:
    """表情感知和群图共用同一视觉角色、字节传输和超时。"""
    encoded = base64.b64encode(data).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
    ]}]
    return await asyncio.wait_for(llm.complete(messages, role="vision", task=task, json_mode=True),
                                  timeout=DESCRIBE_TIMEOUT)


def _has_local_file(store: Any, sticker: Any) -> bool:
    try:
        return store.path_of(sticker) is not None
    except Exception:  # noqa: BLE001
        return False


def _embedding_text(sticker: Any) -> str:
    parts = [
        getattr(sticker, "description", "") or "",
        "、".join(getattr(sticker, "emotions", ()) or ()),
        "、".join(getattr(sticker, "tags", ()) or ()),
    ]
    return " ".join(part for part in parts if part).strip()


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return -1.0
    return dot / (left_norm * right_norm)


def _float_setting(config: Any, name: str, default: float) -> float:
    value = getattr(config, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _candidate_url(payload: Mapping[str, Any], native_ref: Any) -> str:
    """从消息段或原生引用里找可下载的图片地址。"""
    urls: list[Any] = [payload.get("url"), payload.get("file")]
    if isinstance(native_ref, Mapping):
        data = native_ref.get("data")
        if isinstance(data, Mapping):
            urls.extend([data.get("url"), data.get("file")])
    for value in urls:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            return text
    return ""


async def _default_download(url: str, limit: int) -> bytes | None:
    """按收集上限下载图片；gzip/deflate 正文走 http_body 的共用解压。

    不复用连接池、不读环境代理、不自动跟随重定向（每一跳都重新校验目标
    地址），避免群消息里的 url 变成访问内网的跳板。失败统一抛异常，由
    调用方记 warning。
    """
    if not url.startswith(("http://", "https://")):
        return None
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT, follow_redirects=False, trust_env=False, limits=limits,
    ) as client:
        target = httpx.URL(url)
        for _ in range(4):
            if target.scheme not in {"http", "https"} or not target.raw_host or target.userinfo:
                raise ValueError("需要不含凭据的公网图片地址")
            pinned = await pinned_public_url(target)
            async with client.stream(
                "GET", pinned, headers={b"Host": target.netloc, b"Accept-Encoding": b"gzip, deflate"},
                extensions={"sni_hostname": target.raw_host.decode("ascii")},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("图片重定向缺少目标")
                    target = target.join(location)
                    continue
                if response.status_code >= 400:
                    raise ValueError(f"图片返回 HTTP {response.status_code}")
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding in ("gzip", "deflate"):
                    try:
                        body = await read_compressed_body(response.aiter_raw(), encoding, limit)
                    except CompressedBodyError as exc:
                        raise ValueError(f"图片正文解压失败：{exc}") from exc
                    return bytes(body)
                body = bytearray()
                async for chunk in response.aiter_raw():
                    left = limit - len(body)
                    if len(chunk) > left:
                        raise ValueError("图片超过收集大小上限")
                    body.extend(chunk)
                return bytes(body) or None
    raise ValueError("图片重定向次数过多")


def _resolve_llm(runtime: Any) -> Any:
    bot = _resolve_bot(runtime)
    llm = getattr(bot, "llm", None) if bot is not None else None
    if llm is not None and hasattr(llm, "complete"):
        return llm
    llm = getattr(runtime, "llm", None)
    return llm if llm is not None and hasattr(llm, "complete") else None


def _context_text(runtime: Any, event: Any) -> str:
    """最近几条对话原文；拿不到存储时退回当前消息。"""
    store = getattr(runtime, "store", None)
    recent = getattr(store, "recent_messages", None)
    if not callable(recent):
        return str(getattr(event, "text", "") or "")
    try:
        rows = recent(
            event.chat_id, platform=event.platform, chat_type=event.chat_type, limit=8,
        )
    except Exception:  # noqa: BLE001
        return str(getattr(event, "text", "") or "")
    texts = [str(getattr(row, "text", "") or "") for row in rows or []]
    return "\n".join(text for text in texts if text)


def topic_tags(text: str) -> list[str]:
    """从正文抽取最多八个话题标签。"""
    try:
        tokens = tokenize(text or "")
    except Exception:  # noqa: BLE001
        return []
    tags: list[str] = []
    for token in tokens:
        token = token.strip().lower()
        if len(token) < 2 or token in tags:
            continue
        tags.append(token)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _decode_base64(value: Any) -> bytes | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("base64://"):
        text = text[len("base64://") :]
    elif text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        data = base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        return None
    return data or None


def _resolve_bot(runtime: Any) -> Any:
    extras = getattr(runtime, "extras", None)
    if isinstance(extras, dict):
        bot = extras.get("bot")
        if bot is not None and hasattr(bot, "call_capability"):
            return bot
    return runtime if hasattr(runtime, "call_capability") else None


def _can_send(bot: Any, platform: str) -> bool:
    # chat_id 只在来源平台上有意义，不能让能力层改走别的在线适配器。
    adapter = bot.adapters.get(platform)
    if adapter is None or SEND_STICKER not in adapter.capabilities:
        return False
    key = adapter.capability_config_keys.get(SEND_STICKER)
    if not key:
        return True
    default = adapter.capability_defaults.get(SEND_STICKER, True)
    return bot.config.adapter_capability_enabled(platform, key, default=default)


def _chat_key(event: Any) -> tuple[str, str]:
    return str(getattr(event, "platform", "")), str(getattr(event, "chat_id", ""))


__all__ = ["StickerBehavior", "topic_tags"]
