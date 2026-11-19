"""消息处理主循环。

流程：统一事件 → 落 raw_messages / 更新 people → 插件 on_message 钩子 → 合并窗口
→ 回复决策（硬触发 / 意愿评分）→ 拼上下文 → LLM 生成 → 否决标记检查
→ 拆分 → 拆条发送 → 插件 on_after_reply。

稳健性：每个环节都包了异常处理，单个事件/插件/LLM 失败只记录日志，不停循环。
后台任务负责：抽取记忆、归纳记忆、到时放出合并窗口里的消息。
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
from .decision import (
    BOT_SENDER_ID,
    DecisionEngine,
    MessageMerger,
    MergedRound,
    ReplyDecision,
    parse_generation_output,
)
from .llm import LLMProtocol
from .memory.consolidate import Consolidator
from .memory.extract import MemoryExtractor
from .memory.retrieve import Retriever
from .memory.store import MemoryStore, make_person_id
from .persona import Persona
from .plugins.loader import PluginContext, PluginManager

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…~～])\s*|\n+")
# 适配器异常后的重启退避（秒）
ADAPTER_RESTART_MIN = 1.0
ADAPTER_RESTART_MAX = 30.0


def format_scene(event: MessageEvent, display: str) -> str:
    """拼装场景说明行。

    独立成函数，WebUI 的人设预览要用同一段文案，避免两处漂移。
    """
    if event.is_private:
        return (
            f"当前是私聊：现在只有你和{display}两个人单独说话，没有别人在场。"
            "不要把这里当成群聊，也不要提「群」「大家」这类词。"
        )
    return (
        f"当前是群聊：群里有很多人，这条消息是{display}说的。"
        "你的回复群里所有人都看得见。"
    )


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
        # 每个会话最近一次收到消息的时间，用于日志/调试
        self._last_activity: dict[str, float] = {}
        # 合并窗口与意愿评分引擎；状态都在内存里
        self.merger = MessageMerger(
            config.decision.merge_window_seconds,
            config.decision.hard_merge_window_seconds,
        )
        aliases = config.decision.aliases or [config.chat.bot_name]
        self.decision_engine = DecisionEngine(config.decision, aliases=aliases, rng=self.rng)
        self.last_decision: dict[str, ReplyDecision] = {}
        # 启动时间与最近一次处理消息的时间，供 WebUI 概览使用
        self.started_at: float = time.time()
        self.last_message_at: float | None = None
        # 每个会话的消息处理锁，保证串行，避免同一会话并发回复混乱
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._bg_task: asyncio.Task | None = None
        self._merge_task: asyncio.Task | None = None
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
        self._merge_task = asyncio.create_task(self._merge_loop())
        try:
            if tasks:
                await asyncio.gather(*tasks)
            else:
                await self._stop.wait()
        finally:
            # 适配器全部结束（例如 stdin EOF）时，把攒着还没到窗口的消息也处理掉
            try:
                await self.flush_merger(all_rounds=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("退出前处理待合并消息失败：%s", exc)
            for task in (self._bg_task, self._merge_task):
                if task is not None:
                    task.cancel()
            self._bg_task = None
            self._merge_task = None
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
        # 停机前把攒着的消息放出来处理掉，避免丢失
        try:
            await self.flush_merger(all_rounds=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理待合并消息失败：%s", exc)
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

    async def _merge_loop(self) -> None:
        """合并窗口到期检查：每轮 sleep 到最近的截止时间，短窗口才有意义。"""
        while not self._stop.is_set():
            wait = self.merger.next_deadline()
            timeout = 0.25 if wait is None else max(0.01, min(0.25, wait - time.time()))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self.flush_merger()
            except Exception as exc:  # noqa: BLE001
                logger.warning("放出合并消息异常：%s", exc)

    async def flush_merger(self, now: float | None = None, *, all_rounds: bool = False) -> None:
        """处理已到期的合并轮次；``all_rounds`` 时连未到期的一起处理（停机用）。"""
        now = time.time() if now is None else now
        rounds = self.merger.flush_all() if all_rounds else self.merger.pop_ready(now)
        for round_ in rounds:
            await self.process_round(round_)

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
        """统一事件入口。任何异常都只记录，不向上抛。

        消息先落流水、过插件钩子/命令，然后进入合并窗口；窗口到期后由
        ``_merge_loop`` 调 :meth:`process_round` 做决策和生成。窗口配成 0 时
        在当前调用里立即处理完。
        """
        try:
            await self._handle_event(event)
        except Exception as exc:  # noqa: BLE001 - 主循环必须活下来
            logger.exception("处理消息异常：%s", exc)

    async def _handle_event(self, event: MessageEvent) -> None:
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
            # 命令处理（命令不参与合并，直接响应）
            try:
                handled = await self.plugins.handle_commands(self.context, event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件命令异常：%s", exc)
                handled = False
            if handled:
                return

        hard = self.decision_engine.is_hard_trigger(event)
        ready = self.merger.add(event, now, hard=hard is not None)
        for round_ in ready:
            await self.process_round(round_)

    async def process_round(self, round_: MergedRound) -> None:
        """对合并后的一轮消息做决策与生成。异常只记录，不向上抛。"""
        event = round_.to_event()
        lock = self._chat_locks.setdefault(f"{event.platform}:{event.chat_id}", asyncio.Lock())
        async with lock:
            try:
                await self._process_round_locked(round_, event)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能带死循环
                logger.exception("处理合并消息异常：%s", exc)

    async def _process_round_locked(self, round_: MergedRound, event: MessageEvent) -> None:
        now = time.time()
        decision = self.decide(event, now=now)
        self.last_decision[self.decision_engine.chat_key(event)] = decision
        self._log_decision(round_, event, decision, now)
        logger.info(
            "决策 %s/%s %s：%s %s",
            event.platform,
            event.chat_id,
            "群聊" if event.is_group else "私聊",
            event.sender_name,
            decision.log_text(),
        )
        if not decision.should_reply:
            return

        messages = await self.build_context(event, now=now)
        # 让模型知道「可以不接话」的出口
        marker = self.config.decision.veto_marker
        messages[0]["content"] += (
            f"\n\n如果你判断这条消息现在没必要接话（比如只是两句家常、话题已结束），"
            f"只输出 {marker}，不要输出任何其他内容。"
        )
        if self.plugins is not None:
            try:
                messages = await self.plugins.run_before_reply(self.context, event, messages) or messages
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 on_before_reply 异常：%s", exc)

        raw_reply = await self.generate(messages)
        parsed = parse_generation_output(raw_reply, marker)
        if parsed.vetoed:
            logger.info(
                "生成后否决(%s)：%s %s", parsed.reason, event.sender_name, event.text[:40]
            )
            return
        reply = parsed.text
        if not reply:
            return

        await self.send_reply(event, reply, now=now)
        if self.plugins is not None:
            try:
                await self.plugins.run_after_reply(self.context, event, reply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 on_after_reply 异常：%s", exc)

    def _log_decision(self, round_: MergedRound, event: MessageEvent, decision: ReplyDecision, now: float) -> None:
        """把决策写入 decision_log，供 WebUI 展示；写库失败不影响主流程。"""
        factors = decision.factors
        try:
            self.store.add_decision_log(
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                sender_id=event.sender_id,
                sender_name=event.sender_name,
                text=event.text,
                should_reply=decision.should_reply,
                reason=decision.reason,
                hard_trigger=decision.hard_trigger or "",
                score=decision.score,
                threshold=decision.threshold,
                factors={
                    "base": factors.base,
                    "momentum": factors.momentum,
                    "relevance": factors.relevance,
                    "ratio_penalty": factors.ratio_penalty,
                    "cooldown_penalty": factors.cooldown_penalty,
                    "multiplier": factors.multiplier,
                    "noise": factors.noise,
                    "share": factors.share,
                }
                if factors is not None
                else {},
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 观测数据写不进去也不能影响回复
            logger.warning("写入决策日志失败：%s", exc)

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
        self.last_message_at = now
        logger.debug("记录消息 %s: %s", person_id, event.text[:60])

    # ------------------------------------------------------------------
    # 回复决策
    # ------------------------------------------------------------------

    def topic_relevance(self, event: MessageEvent) -> float:
        """话题相关性：当前消息与记忆的最高检索分（没有积累就是 0）。"""
        person_id = make_person_id(event.platform, event.sender_id)
        pool = [
            mem
            for mem in self.store.list_memories()
            if mem.person_id == person_id
            or (mem.scope_type == "group" and mem.scope_id == str(event.chat_id))
        ]
        if not pool:
            return 0.0
        scored = self.retriever.score(event.text, pool)
        return max((item.score for item in scored), default=0.0)

    def decide(self, event: MessageEvent, *, now: float | None = None) -> ReplyDecision:
        """回复决策：硬触发直通，普通群消息走意愿评分。"""
        now = time.time() if now is None else now
        hard = self.decision_engine.is_hard_trigger(event)
        relevance = 0.0 if hard else self.topic_relevance(event)
        recent = ()
        if not hard:
            recent = self.store.recent_messages(
                event.chat_id,
                limit=self.config.decision.ratio_window,
                platform=event.platform,
            )
        return self.decision_engine.evaluate(
            event,
            now=now,
            recent_messages=recent,
            relevance=relevance,
            hard_trigger=hard,
        )

    def should_reply(self, event: MessageEvent, *, now: float | None = None) -> ReplyDecision:
        """兼容旧调用名，等价于 :meth:`decide`。"""
        return self.decide(event, now=now)

    # ------------------------------------------------------------------
    # 上下文拼装
    # ------------------------------------------------------------------

    def format_memories(self, memories: list) -> str:
        """把检索到的记忆渲染成一条中性标题；标题不绑定场景，私聊里不会冒出「群」。"""
        if not memories:
            return ""
        lines = [f"- {m.content}" for m in memories]
        return "你记得的事：\n" + "\n".join(lines)

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
        # 场景说明带行为约束，压住人格文件里可能残留的场景想象
        sections.append(format_scene(event, display))
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
        """拆条发送，并把 bot 自己的话写进流水（``processed=True``，不参与记忆抽取）。"""
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
        # 发言时间交给决策引擎：动量加成与发言后冷却都依赖它
        self.decision_engine.note_reply(self.decision_engine.chat_key(event), time.time() if now is None else now)
