"""消息处理主循环。

流程：统一事件 → 落 raw_messages / 更新 people → 插件 on_message 钩子 → 触发判断
→ 拼上下文 → LLM 生成 → 拆分 → 拆条发送 → 插件 on_after_reply。

稳健性：每个环节都包了异常处理，单个事件/插件/LLM 失败只记录日志，不停循环。
后台任务负责：抽取记忆、归纳记忆。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import BaseAdapter, MessageEvent, Segment
from .config import Config
from .llm import LLMProtocol
from .memory.consolidate import Consolidator
from .memory.extract import MemoryExtractor
from .memory.retrieve import Retriever
from .memory.store import MemoryStore, make_person_id
from .persona import Persona
from .plugins.loader import PluginContext, PluginManager

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…~～])\s*|\n+")
# bot 自己发出的消息在流水里的 sender_id，避免被当成用户、也不参与抽取
BOT_SENDER_ID = "__bot__"
# 适配器异常后的重启退避（秒）
ADAPTER_RESTART_MIN = 1.0
ADAPTER_RESTART_MAX = 30.0


@dataclass
class ReplyDecision:
    should_reply: bool
    reason: str = ""


@dataclass
class BotContext:
    """插件钩子与命令处理拿到的运行时上下文。"""

    store: MemoryStore
    persona: Persona
    config: Config
    adapters: dict[str, BaseAdapter]
    retriever: Retriever
    extractor: MemoryExtractor
    consolidator: Consolidator
    event: MessageEvent | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def plugins(self) -> PluginManager | None:
        return self.extras.get("plugins")


class RubenBot:
    """把各模块串起来的主循环。"""

    def __init__(
        self,
        config: Config,
        store: MemoryStore,
        llm: LLMProtocol,
        persona: Persona,
        *,
        plugins: PluginManager | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.store = store
        self.llm = llm
        self.persona = persona
        self.rng = rng or random.Random()
        self.retriever = Retriever(store)
        self.extractor = MemoryExtractor(llm, store, config.memory)
        self.consolidator = Consolidator(llm, store, config.memory)
        self.adapters: dict[str, BaseAdapter] = {}
        self.plugins = plugins
        self.context = BotContext(
            store=store,
            persona=persona,
            config=config,
            adapters=self.adapters,
            retriever=self.retriever,
            extractor=self.extractor,
            consolidator=self.consolidator,
            extras={"plugins": plugins, "bot": self},
        )
        # 每个会话最近一次收到消息的时间，用于“连续对话提高插话率”
        self._last_activity: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        # 每个会话的消息处理锁，保证串行，避免同一会话并发回复混乱
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._bg_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------

    def register_adapter(self, adapter: BaseAdapter) -> None:
        self.adapters[adapter.name] = adapter

    async def run(self) -> None:
        """启动全部适配器与后台任务，直到 stop。"""
        self._stop.clear()
        tasks = [asyncio.create_task(self._run_adapter(adapter)) for adapter in self.adapters.values()]
        self._bg_task = asyncio.create_task(self._background_loop())
        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                await self._stop.wait()
        finally:
            self._bg_task.cancel()
            self._bg_task = None
            for task in tasks:
                task.cancel()

    async def _run_adapter(self, adapter: BaseAdapter) -> None:
        """运行单个适配器；异常后重启（带退避），不让整个 bot 停摆。"""
        delay = ADAPTER_RESTART_MIN
        while not self._stop.is_set():
            try:
                await adapter.start(self.handle_event)
                if self._stop.is_set():
                    return
                # 适配器正常返回（例如 Console 读到 EOF）：视为结束，不再重启
                logger.info("适配器 %s 已结束", adapter.name)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 适配器异常不能拖垮主循环
                logger.warning("适配器 %s 异常：%s", adapter.name, exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                delay = min(delay * 2, ADAPTER_RESTART_MAX)

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        self.stop()
        for adapter in self.adapters.values():
            try:
                await adapter.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭适配器 %s 失败：%s", adapter.name, exc)

    async def _background_loop(self) -> None:
        """定期检查抽取与归纳。"""
        interval = max(5.0, self.config.background_check_minutes * 60)
        while not self._stop.is_set():
            try:
                await self.run_maintenance()
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台维护任务异常：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run_maintenance(self, *, now: float | None = None, force_extract: bool = False) -> None:
        """抽取 + 归纳的一次检查；两个环节互不影响。"""
        now = time.time() if now is None else now
        try:
            await self.extractor.maybe_extract(now=now, force=force_extract)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆抽取异常：%s", exc)
        try:
            await self.consolidator.maybe_consolidate(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆归纳异常：%s", exc)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    async def handle_event(self, event: MessageEvent) -> None:
        """统一事件入口。任何异常都只记录，不向上抛。"""
        try:
            await self._handle_event(event)
        except Exception as exc:  # noqa: BLE001 - 主循环必须活下来
            logger.exception("处理消息异常：%s", exc)

    async def _handle_event(self, event: MessageEvent) -> None:
        lock = self._chat_locks.setdefault(f"{event.platform}:{event.chat_id}", asyncio.Lock())
        async with lock:
            now = time.time()
            await self._record(event, now)

            # 插件 on_message：可修改事件或直接拦截（返回 False）
            if self.plugins is not None:
                self.context.event = event
                try:
                    proceed = await self.plugins.run_message_hooks(self.context, event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 on_message 异常：%s", exc)
                    proceed = True
                if not proceed:
                    logger.debug("消息被插件拦截：%s", event.text[:40])
                    return
                # 命令处理
                try:
                    handled = await self.plugins.handle_commands(self.context, event)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件命令异常：%s", exc)
                    handled = False
                if handled:
                    return

            decision = self.should_reply(event, now=now)
            if not decision.should_reply:
                logger.debug("不回复(%s)：%s %s", decision.reason, event.sender_name, event.text[:40])
                return

            messages = await self.build_context(event, now=now)
            if self.plugins is not None:
                try:
                    messages = await self.plugins.run_before_reply(self.context, event, messages) or messages
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 on_before_reply 异常：%s", exc)

            reply = await self.generate(messages)
            if not reply:
                return
            await self.send_reply(event, reply, now=now)
            if self.plugins is not None:
                try:
                    await self.plugins.run_after_reply(self.context, event, reply)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("插件 on_after_reply 异常：%s", exc)

    async def _record(self, event: MessageEvent, now: float) -> None:
        """落流水 + 更新人物；bot 自己发的消息不进流水。"""
        if not event.text.strip():
            return
        person_id = make_person_id(event.platform, event.sender_id)
        self.store.upsert_person(event.platform, event.sender_id, event.sender_name, now=now)
        self.store.add_raw_message(
            platform=event.platform,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            text=event.text,
            ts=now,
        )
        self._last_activity[f"{event.platform}:{event.chat_id}"] = now
        logger.debug("记录消息 %s: %s", person_id, event.text[:60])

    # ------------------------------------------------------------------
    # 触发判断
    # ------------------------------------------------------------------

    def should_reply(self, event: MessageEvent, *, now: float | None = None) -> ReplyDecision:
        """私聊都回；群聊被 @/被回复必回，否则按活跃度概率。"""
        now = time.time() if now is None else now
        if event.is_private:
            return ReplyDecision(True, "private")
        if event.mentions_bot:
            return ReplyDecision(True, "mentioned")
        if self._is_reply_to_bot(event):
            return ReplyDecision(True, "reply_to_bot")
        # 连续对话：刚聊过则提高插话概率
        chat_key = f"{event.platform}:{event.chat_id}"
        probability = self.config.chat.group_talkativeness
        last = self._last_reply_at.get(chat_key)
        if last is not None and now - last <= self.config.chat.boost_window_seconds:
            probability = min(0.9, self.config.chat.group_talkativeness_boost)
        if self.rng.random() < probability:
            return ReplyDecision(True, f"random({probability:.2f})")
        return ReplyDecision(False, "silent")

    def _is_reply_to_bot(self, event: MessageEvent) -> bool:
        """被回复到 bot：适配器通常已映射为 mentions_bot；这里兜底看 raw。"""
        reply_id = event.reply_to_id
        if not reply_id:
            return False
        if isinstance(event.raw, dict):
            bot_ids = event.raw.get("bot_message_ids")
            if bot_ids and reply_id in bot_ids:
                return True
            # OneBot 的 reply 段指向 bot 自己发的消息时，适配器会直接置位 mentions_bot
        return False

    # ------------------------------------------------------------------
    # 上下文拼装
    # ------------------------------------------------------------------

    def format_memories(self, memories: list) -> str:
        if not memories:
            return ""
        lines = [f"- {m.content}" for m in memories]
        return "你记得关于对方和这个群的事：\n" + "\n".join(lines)

    async def build_context(self, event: MessageEvent, *, now: float | None = None) -> list[dict[str, str]]:
        """人格 + 记忆 + 最近历史 + 群/说话人信息。"""
        now = time.time() if now is None else now
        persona_prompt = self.persona.system_prompt()
        person_id = make_person_id(event.platform, event.sender_id)
        person = self.store.get_person(person_id)
        display = (person.display_name if person else "") or event.sender_name

        memories = self.retriever.retrieve(
            event.text,
            person_id=person_id,
            group_id=event.chat_id if event.is_group else None,
            limit=self.config.memory.inject_limit,
        )
        sections: list[str] = [persona_prompt]
        memory_text = self.format_memories(memories)
        if memory_text:
            sections.append(memory_text)
        scene = "私聊" if event.is_private else f"群聊 {event.chat_id}"
        sections.append(f"当前场景：{scene}；正在和你说话的人：{display}。")
        system_prompt = "\n\n".join(sections)

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        since = now - self.config.chat.history_ttl_hours * 3600
        history = self.store.recent_messages(
            event.chat_id,
            limit=self.config.chat.history_size,
            since=since,
            platform=event.platform,
        )
        for msg in history:
            is_bot = msg.sender_id == BOT_SENDER_ID
            role = "assistant" if is_bot else "user"
            content = msg.text if is_bot else f"{msg.sender_name or msg.sender_id}：{msg.text}"
            messages.append({"role": role, "content": content})
        return messages

    # ------------------------------------------------------------------
    # 生成与发送
    # ------------------------------------------------------------------

    async def generate(self, messages: list[dict[str, str]]) -> str:
        try:
            reply = await self.llm.complete(messages, role="chat")
        except Exception as exc:  # noqa: BLE001 - LLM 失败不能带死循环
            logger.warning("生成回复失败：%s", exc)
            return ""
        return (reply or "").strip()

    def split_reply(self, reply: str) -> list[str]:
        """按句/长度拆成 2~3 条短消息。"""
        reply = reply.strip()
        if not reply:
            return []
        max_parts = max(1, self.config.chat.split_max_parts)
        limit = max(8, self.config.chat.split_part_chars)
        if len(reply) <= limit or max_parts == 1:
            return [reply]
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(reply) if s and s.strip()]
        if not sentences:
            sentences = [reply]
        # 贪心合并到接近 limit，且不超过 max_parts
        parts: list[str] = []
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) > limit:
                parts.append(current)
                current = sentence
            else:
                current += sentence
        if current:
            parts.append(current)
        if len(parts) > max_parts:
            # 多余的并入最后一条，避免刷屏
            parts = parts[: max_parts - 1] + ["".join(parts[max_parts - 1 :])]
        return parts

    async def send_reply(self, event: MessageEvent, reply: str, *, now: float | None = None) -> None:
        adapter = self.adapters.get(event.platform)
        if adapter is None:
            logger.warning("没有平台 %s 的适配器，无法回复", event.platform)
            return
        parts = self.split_reply(reply)
        for index, part in enumerate(parts):
            try:
                await adapter.send(event.chat_id, [Segment.text(part)], chat_type=event.chat_type)
            except Exception as exc:  # noqa: BLE001 - 一条发失败不影响后续
                logger.warning("发送失败（%s 第 %s 条）：%s", event.platform, index + 1, exc)
                continue
            # 记入流水，供后续上下文使用；标记已处理，不参与记忆抽取
            self.store.add_raw_message(
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                sender_id=BOT_SENDER_ID,
                sender_name=self.config.chat.bot_name,
                text=part,
                processed=True,
            )
            if index < len(parts) - 1 and self.config.chat.split_delay_seconds > 0:
                await asyncio.sleep(self.config.chat.split_delay_seconds)
        self._last_reply_at[f"{event.platform}:{event.chat_id}"] = time.time() if now is None else now
