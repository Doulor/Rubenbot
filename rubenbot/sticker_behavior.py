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
import ipaddress
import json
import logging
import math
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .adapters.base import SEND_STICKER
from .http_body import CompressedBodyError, read_compressed_body
from .llm import parse_json_payload
from .memory.retrieve import tokenize
from .stickers import KIND_PHOTO, KIND_STICKER, infer_ref_kind

logger = logging.getLogger(__name__)

COLLECTABLE_SEGMENTS = frozenset({"sticker", "image"})
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
    ) -> None:
        self.config = config
        self._store_getter = store_getter
        self._store = None
        self.rng = rng or random.Random()
        self.clock = clock or time.monotonic
        self._last_sent_at: dict[tuple[str, str], float] = {}
        self._downloader = downloader
        self._embed_fn = embed_fn
        self._embed_client: httpx.AsyncClient | None = None
        # 后台任务与缓存状态；都只在事件循环里访问
        self._bg_tasks: set[asyncio.Task] = set()
        self._pending_descriptions: set[str] = set()
        self._vision_skip_logged = False
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
        """关闭自建的 HTTP 客户端；后台任务失败不影响调用方。"""
        if self._embed_client is not None and not self._embed_client.is_closed:
            await self._embed_client.aclose()
        self._embed_client = None

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------

    async def on_message(self, runtime: Any, event: Any) -> None:
        """在 core 的消息预处理时机收集群聊里的表情/图片段。"""
        try:
            if not self.enabled or not getattr(self.config, "collect", False) or not getattr(event, "is_group", False):
                return
            store = self.store
            if store is None:
                return
            platform = str(getattr(event, "platform", "") or "")
            tags = topic_tags(getattr(event, "text", "") or "")
            segments = list(getattr(event, "segments", None) or [])
            if not segments:
                return
            # 同一事件里的多张图并行取，避免一条多图消息串行等多次下载超时
            results = await asyncio.gather(
                *(self._collect_segment(store, platform, segment, tags) for segment in segments),
                return_exceptions=True,
            )
            for sticker in results:
                if isinstance(sticker, BaseException):
                    # 单个段失败不影响其他段
                    logger.debug("表情包收集异常（跳过该段）：%s", sticker)
                    continue
                if sticker is not None and getattr(sticker, "file", ""):
                    # 普通图片不参与选图，没必要花视觉模型额度给它写描述
                    if getattr(sticker, "kind", KIND_STICKER) == KIND_STICKER:
                        self._schedule_description(runtime, store, sticker)
        except Exception as exc:  # noqa: BLE001
            logger.debug("表情包收集异常：%s", exc)

    async def _collect_segment(self, store: Any, platform: str, segment: Any, tags: list[str]) -> Any:
        kind = str(getattr(segment, "type", "") or (segment.get("type") if isinstance(segment, Mapping) else ""))
        if kind not in COLLECTABLE_SEGMENTS:
            return None
        data = getattr(segment, "data", None)
        if not isinstance(data, Mapping):
            data = segment.get("data") if isinstance(segment, Mapping) else None
        if not isinstance(data, Mapping) or not data:
            return None
        payload = {str(key): value for key, value in data.items() if value not in (None, "")}
        if not payload:
            return None
        if kind == "sticker" and isinstance(payload.get("native_ref"), Mapping):
            # 适配器已经把平台原生段放进 native_ref；存储层直接保留这份
            # 约定结构，发送侧才能按平台还原原生段。
            native_ref = payload["native_ref"]
        else:
            native_ref = {"kind": kind, "data": payload}
        refs = {platform: native_ref} if platform else None

        # 真表情与误收的普通图片分开：photo 默认不入库，只有显式打开
        # stickers.collect_photos 才收（收进来也标记为 photo，永不参选）。
        sticker_kind = infer_ref_kind(native_ref)
        if sticker_kind == KIND_PHOTO and not getattr(self.config, "collect_photos", False):
            logger.debug(
                "跳过普通图片（默认只收表情；stickers.collect_photos 可放开）：%s",
                payload.get("summary") or payload.get("file") or payload.get("url") or kind,
            )
            return None

        raw = _decode_base64(payload.get("base64"))
        if raw is None:
            raw = await self._download_image(_candidate_url(payload, native_ref))
        if raw is None and not refs:
            return None
        return store.add(
            raw,
            tags=tags,
            native_refs=refs,
            file_name=payload.get("filename") or payload.get("name") or None,
            mime_type=payload.get("mime_type") or payload.get("content_type") or None,
            max_file_bytes=self.max_file_bytes,
            kind=sticker_kind,
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
            logger.warning("表情图片下载失败（保留原生引用）：%s", exc)
            return None

    @property
    def max_file_bytes(self) -> int:
        value = getattr(self.config, "max_file_bytes", _DEFAULT_MAX_BYTES)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return _DEFAULT_MAX_BYTES
        return int(value)

    # ------------------------------------------------------------------
    # 描述生成（配置了 vision 角色才跑）
    # ------------------------------------------------------------------

    def _schedule_description(self, runtime: Any, store: Any, sticker: Any) -> None:
        """存本体成功后后台生成描述；没配视觉模型只记一次 info。"""
        if getattr(sticker, "description", ""):
            return
        llm = _resolve_llm(runtime)
        if llm is None or not _vision_configured(runtime, llm):
            if not self._vision_skip_logged:
                logger.info("未配置 vision 角色，跳过表情包描述生成")
                self._vision_skip_logged = True
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
            logger.warning("表情包描述生成失败（%s）：%s", sticker_id, exc)

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
        encoded = base64.b64encode(data).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }]
        raw = await asyncio.wait_for(
            llm.complete(messages, role="vision", task="sticker_description", json_mode=True),
            timeout=DESCRIBE_TIMEOUT,
        )
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
            if sticker is None:
                return False
            # 冷却按尝试计，避免平台故障时每轮重试；它不是发言额度。
            self._last_sent_at[_chat_key(event)] = now
            ok, reason = await bot.call_capability(
                platform, SEND_STICKER, sticker=store.reference(sticker),
                chat_id=event.chat_id, chat_type=event.chat_type,
            )
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
        避免一次性对上百张表情并发打视觉模型；没配 vision 角色时一条都
        不排、也不发请求。
        """
        store = self.store
        if store is None:
            return 0
        llm = _resolve_llm(runtime)
        if llm is None or not _vision_configured(runtime, llm):
            if not self._vision_skip_logged:
                logger.info("未配置 vision 角色，跳过表情包描述生成")
                self._vision_skip_logged = True
            return 0
        targets = [
            item.id for item in store.find_by_tags(None, kind=KIND_STICKER)
            if item.file and not item.description and item.id not in self._pending_descriptions
        ][:DESCRIBE_BACKFILL_BATCH]
        if not targets:
            return 0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return 0
        self._pending_descriptions.update(targets)
        task = loop.create_task(self._describe_batch(store, targets, llm))
        self._bg_tasks.add(task)
        task.add_done_callback(lambda finished: self._finish_batch(finished, targets))
        return len(targets)

    async def _describe_batch(self, store: Any, sticker_ids: list[str], llm: Any) -> None:
        for sticker_id in sticker_ids:
            try:
                item = store.get(sticker_id)
                # 普通图片不参选，也不值得花视觉额度补描述
                if item is not None and getattr(item, "kind", KIND_STICKER) != KIND_STICKER:
                    continue
                await self._build_description(store, sticker_id, llm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败继续下一条
                logger.warning("表情包描述生成失败（%s）：%s", sticker_id, exc)

    def _finish_batch(self, task: asyncio.Task, sticker_ids: list[str]) -> None:
        self._bg_tasks.discard(task)
        self._pending_descriptions.difference_update(sticker_ids)
        try:
            task.result()
        except asyncio.CancelledError:
            pass


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
        target = url
        for _ in range(4):
            if not await _public_url(target):
                raise ValueError("图片地址不是公网地址")
            async with client.stream(
                "GET", target, headers={"Accept-Encoding": "gzip, deflate"},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("图片重定向缺少目标")
                    target = str(httpx.URL(target).join(location))
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


async def _public_url(url: str) -> bool:
    """只允许访问公网地址：字形 IP 直接判断，域名解析后逐个判断。"""
    try:
        parsed = httpx.URL(url)
        host = parsed.host
    except Exception:  # noqa: BLE001
        return False
    if not host or parsed.scheme not in ("http", "https"):
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return False
        addresses = []
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except ValueError:
                return False
    return bool(addresses) and all(
        address.is_global and not address.is_multicast and not address.is_reserved
        for address in addresses
    )


def _vision_configured(runtime: Any, llm: Any) -> bool:
    config = getattr(runtime, "config", None)
    llm_config = getattr(config, "llm", None)
    models = getattr(llm_config, "models", None)
    if isinstance(models, Mapping) and str(models.get("vision") or "").strip():
        return True
    # 兼容自定义 LLM 实现：自带 model_for 时看这个角色是否被单独指定
    model_for = getattr(getattr(llm, "config", None), "models", None)
    return isinstance(model_for, Mapping) and bool(str(model_for.get("vision") or "").strip())


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
