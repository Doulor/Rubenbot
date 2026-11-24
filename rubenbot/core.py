"""消息处理主循环。

流程：统一事件 → 落 raw_messages / 更新 people → 插件 on_message 钩子 → 合并窗口
→ 回复决策（硬触发 / 节奏护栏 / 策划裁定）→ 拼上下文 → LLM 生成 → 否决标记检查
→ 拆分 → 拆条发送 → 插件 on_after_reply。

稳健性：每个环节都包了异常处理，单个事件/插件/LLM 失败只记录日志，不停循环。
后台任务负责：抽取记忆、归纳记忆、到时放出合并窗口里的消息。
"""

from __future__ import annotations

import asyncio
import random
import time

from . import computer
from .adapters.base import BaseAdapter, MessageEvent
from .config import Config
from .core_context import ContextMixin
from .core_delivery import DeliveryMixin, ReplyFlight as ReplyFlight
from .core_generation import WEB_SEARCH_TOOL as WEB_SEARCH_TOOL, GenerationMixin
from .core_ingress import (
    ADAPTER_RESTART_MAX as ADAPTER_RESTART_MAX,
    ADAPTER_RESTART_MIN as ADAPTER_RESTART_MIN,
    IngressMixin,
)
from .core_shared import (
    RECALL_TOOL as RECALL_TOOL,
    REPLY_SEGMENT_CONTRACT as REPLY_SEGMENT_CONTRACT,
    BotContext as BotContext,
    SendResult as SendResult,
    _parse_tool_arguments as _parse_tool_arguments,
    chat_tools as chat_tools,
    clean_reply_patterns as clean_reply_patterns,
    format_scene as format_scene,
    logger as logger,
    strip_action_parens as strip_action_parens,
    strip_transcript_prefix as strip_transcript_prefix,
)
from .core_verdict import (
    GUIDANCE_ROLE as GUIDANCE_ROLE,
    GUIDANCE_TASK_PROMPT as GUIDANCE_TASK_PROMPT,
    GUIDANCE_TIMEOUT_CAP as GUIDANCE_TIMEOUT_CAP,
    TOPIC_GAP_SECONDS as TOPIC_GAP_SECONDS,
    VerdictMixin,
)
from .decision import BOT_SENDER_ID as BOT_SENDER_ID, DecisionEngine, MessageMerger, ReplyDecision
from .emotion import EmotionEngine
from .gateway.events import EventMerger, EventRouter
from .gateway.judge import EventJudge
from .gateway.service import GatewayService
from .group_profile import GroupProfileSummarizer
from .linkparse.service import LinkService
from .llm import LLMClient, LLMProtocol, make_usage_recorder
from .mcp_client import MCPManager
from .memory.consolidate import Consolidator
from .memory.entities import EntityLedger
from .memory.extract import MemoryExtractor
from .memory.retrieve import MemoryReader, Retriever
from .memory.state import StateDistiller
from .memory.store import MemoryStore
from .memory.summary import RollingSummarizer, format_summary_block
from .memory.timeline import bind_inbound_timeline
from .opener_guard import OpenerGuard
from .persona import Persona
from .plugins.loader import PluginManager
from .privacy import PrivacyCommands, PrivacyGate
from .proactive import ProactiveOpener, is_acceptable_opener
from .reminder import build_reminder_prompt, parties_from_payload
from .scheduler import Scheduler
from .search import WebSearcher, build_searcher
from .sticker_behavior import StickerBehavior
from .stickers import StickerStore
from .voice import VoiceService, build_voice


class RubenBot(IngressMixin, VerdictMixin, ContextMixin, GenerationMixin, DeliveryMixin):
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
        stickers: StickerStore | None = None,
    ):
        self.config = config
        self.store = store
        bind_inbound_timeline(store)
        self.llm = llm
        self.persona = persona
        self.rng = rng or random.Random()
        self.retriever = Retriever(store)
        self.entities = EntityLedger(store)
        self._ledger_read: set[tuple[str, str]] = set()
        self._recall_checked = False
        self.store.privacy_enabled = config.privacy.enabled
        self.gate = PrivacyGate(store, llm, config.memory) if config.privacy.enabled else None
        self.privacy_commands = PrivacyCommands()
        self.memory_reader = MemoryReader(
            store, llm, config.memory, gate=self.gate, retriever=self.retriever
        )
        self.extractor = MemoryExtractor(llm, store, config.memory,
                                         is_owner=lambda row: computer.is_host(self.config.computer, row))
        self.consolidator = Consolidator(llm, store, config.memory)
        # 会话滚动摘要：只压缩本会话的历史，与跨会话记忆各管各的
        self.summarizer = RollingSummarizer(llm, store, config.memory)
        # 群定位：手写备注即时生效；自归纳只在后台维护任务里更新
        self.group_profiles = GroupProfileSummarizer(llm, store, config.memory.group_profile)
        self.states = StateDistiller(llm, store, config.memory)
        # 联网搜索（LLM 工具）：没配 api_key 时为 None，工具不开放
        self.searcher: WebSearcher | None = build_searcher(config.search)
        # 链接理解（感知层）：任何 http(s) 链接都尝试读取；默认 on_mention。
        # 抓取受 SSRF 校验、字节与总耗时上限约束，结果只作外部资料注入。
        self.link_service = self._build_link_service(config.link)
        # MCP 连接与适配器一样，按启动时的配置建连，重启后才切换。
        self.mcp = MCPManager(config.mcp)
        # 语音双向（STT/TTS）：转写与合成都没配时为 None，全部降级为文字
        self.voice: VoiceService | None = build_voice(config.voice)
        # 情绪与好感度：确定性规则，状态落库，只作生成背景
        self.emotion = EmotionEngine(store, config.emotion)
        # 通用适配器网关（感知面的事件通道 + 行动面的接口网关）；默认关
        self.gateway = GatewayService(store, config)
        self.event_merger = EventMerger()
        self.event_router = EventRouter(
            config.gateway,
            activity_fn=self._group_has_activity,
            profile_fn=self._person_has_profile,
        )
        self.event_judge = EventJudge(llm, store, persona.text)
        # 表情库：图片本体 + JSON 索引；首次使用时按配置目录惰性创建
        self._stickers: StickerStore | None = stickers
        self.sticker_behavior = StickerBehavior(
            config.stickers,
            store_getter=lambda: self.stickers,
            rng=random.Random(),
        )
        self.adapters: dict[str, BaseAdapter] = {}
        self.plugins = plugins
        if plugins is not None:
            plugins.on_music_delivery = self._record_music_delivery
        self.context = BotContext(
            store=store,
            persona=persona,
            config=config,
            adapters=self.adapters,
            retriever=self.retriever,
            extractor=self.extractor,
            consolidator=self.consolidator,
            extras={
                "plugins": plugins,
                "bot": self,
                "memory_reader": self.memory_reader,
                "privacy_gate": self.gate,
                "emotion": self.emotion,
                # 可能为 None：插件用 bot.stickers 属性拿（会按需创建）
                "stickers": self._stickers,
            },
        )
        # 每个会话最近一次收到消息的时间，用于日志/调试
        self._last_activity: dict[str, float] = {}
        # 合并窗口与节奏护栏；状态都在内存里
        self.merger = MessageMerger(
            config.decision.merge_window_seconds,
            config.decision.hard_merge_window_seconds,
            batch_max=config.decision.batch_max_seconds,
            urgent=config.decision.urgent_seconds,
        )
        aliases = config.decision.aliases or [config.chat.bot_name]
        self.decision_engine = DecisionEngine(config.decision, aliases=aliases, rng=self.rng)
        self.last_decision: dict[str, ReplyDecision] = {}
        # 按会话跟踪最近成功发出的文字；重复开头、收尾或短语只提醒生成端。
        self.opener_guard = OpenerGuard()
        # 启动时间与最近一次处理消息的时间，供 WebUI 概览使用
        self.started_at: float = time.time()
        self.last_message_at: float | None = None
        # 每个会话的消息处理锁，保证串行，避免同一会话并发回复混乱
        self._chat_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._flights: dict[tuple[str, str, str], ReplyFlight] = {}
        self._round_tasks: set[asyncio.Task] = set()
        self._engaged_at: dict[tuple[str, str, str], float] = {}
        self._bg_task: asyncio.Task | None = None
        self._merge_task: asyncio.Task | None = None
        # 定时任务与冷场开口：跟随主程序生命周期启停
        self.scheduler = Scheduler(store, self.handle_scheduled_job)
        self.opener = ProactiveOpener(config.proactive, rng=self.rng)
        self._scheduler_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------

    def _build_link_service(self, config) -> LinkService:
        """构造链接读取服务；MCP 兜底按调用时的配置判断是否需要。

        兜底只在主链路读不到、且 ``link.fallback_tool`` 指向已发布工具时调用一次，
        属于可选路径，不参与主链路，也不把工具故障当成读到了内容。
        """
        async def fallback(name: str, url: str) -> str:
            return await self.mcp.call(name, {"url": url})

        return LinkService(config, fallback=fallback)

    @property
    def stickers(self) -> StickerStore:
        """表情库（惰性创建：测试里不碰磁盘，真正用到时才建目录）。"""
        if self._stickers is None:
            self._stickers = StickerStore(self.config.stickers.dir)
            self.context.extras["stickers"] = self._stickers
        return self._stickers

    def apply_config(self, config: Config, *, rebuild_llm: bool = True) -> None:
        """热应用新配置：后续消息立即使用新的 LLM、意愿、记忆与聊天参数。

        WebUI 保存设置后调用。只换引用与参数，不动决策/记忆算法本身；
        已经建立的连接（适配器、数据库、WebUI 端口）不在这里处理，那些
        字段只能重启生效。``rebuild_llm=False`` 时保留现有客户端（只改了
        意愿/记忆等参数时不必重建连接）。
        """
        old_llm = self.llm
        old_search_config = self.config.search
        old_voice_config = self.config.voice
        self.config = config
        self.sticker_behavior.config = config.stickers
        if rebuild_llm:
            self._recall_checked = False
            # 重建 LLM 客户端（改了 base_url / api_key / 超时等都需要新客户端）
            self.llm = LLMClient(config.llm, usage_recorder=make_usage_recorder(self.store))
        # 各模块持有的是配置对象引用，换成新对象即可让参数立即生效
        self.extractor.config = config.memory
        self.extractor.llm = self.llm
        self.consolidator.config = config.memory
        self.consolidator.llm = self.llm
        self.summarizer.config = config.memory
        self.summarizer.llm = self.llm
        self.group_profiles.config = config.memory.group_profile
        self.group_profiles.llm = self.llm
        self.states.config = config.memory
        self.states.llm = self.llm
        # 搜索：配置变了就换成新的搜索器（冷却状态在内存里，换配置时重置；
        # 旧客户端的连接交给事件循环在后台关掉）
        if config.search != old_search_config:
            old_searcher = self.searcher
            self.searcher = build_searcher(config.search)
            if isinstance(old_searcher, WebSearcher):
                try:
                    asyncio.get_running_loop().create_task(old_searcher.close())
                except RuntimeError:  # 没有事件循环（测试/同步调用）
                    pass
        # 语音：配置变了就换成新服务（旧 HTTP 客户端的连接同理后台关掉）
        if config.voice != old_voice_config:
            old_voice = self.voice
            self.voice = build_voice(config.voice)
            if isinstance(old_voice, VoiceService):
                try:
                    asyncio.get_running_loop().create_task(old_voice.close())
                except RuntimeError:  # 没有事件循环（测试/同步调用）
                    pass
        # 情绪规则：参数换新，已落库的好感度/心情不受影响
        self.emotion.config = config.emotion
        # 链接理解：参数换新；缓存、冷却与当日额度按新参数继续用，不重置。
        if getattr(self, "link_service", None) is not None:
            self.link_service.config = config.link
        # 网关与事件路由：配置对象换新；冷却/预算状态落库，不受影响
        self.gateway.config = config
        self.gateway.gatekeeper.config = config
        self.event_router.config = config.gateway
        self.event_judge.persona_text = self.persona.text
        # 读取侧与审查口一起切换；不让上个档位下生成的成员索引继续生效
        self.memory_reader.config = config.memory
        self.memory_reader.llm = self.llm
        self.store.privacy_enabled = config.privacy.enabled
        self.gate = PrivacyGate(self.store, self.llm, config.memory) if config.privacy.enabled else None
        self.memory_reader.gate = self.gate
        self.memory_reader.index.config = config.memory
        self.memory_reader.index.invalidate()
        self.context.extras["privacy_gate"] = self.gate
        self.context.config = config
        if self.plugins is not None:
            self.plugins.config = config
        # 节奏护栏：配置与别名一起换，内部状态（冷却/主动窗口）保留
        self.decision_engine.config = config.decision
        aliases = config.decision.aliases or [config.chat.bot_name]
        self.decision_engine.set_aliases(aliases)
        # 提醒与冷场开口：配置对象替换即可生效；冷却/当日计数保留
        self.opener.config = config.proactive
        # 合并窗口：新值对之后到达的消息生效
        self.merger.window = max(0.0, float(config.decision.merge_window_seconds))
        self.merger.hard_window = max(0.0, float(config.decision.hard_merge_window_seconds))
        self.merger.batch_max = max(0.0, float(config.decision.batch_max_seconds))
        self.merger.urgent = max(0.0, float(config.decision.urgent_seconds))
        # 旧客户端在后台关掉，不阻塞保存请求
        if rebuild_llm and isinstance(old_llm, LLMClient):
            try:
                asyncio.get_running_loop().create_task(old_llm.close())
            except RuntimeError:  # 没有事件循环（测试/同步调用）
                pass
        logger.info(
            "配置已热应用：模型=%s 查看概率=%.2f 注入上限=%s",
            config.llm.chat_model,
            config.attention.talk_value,
            config.memory.inject_limit,
        )

    def reload_persona(self, persona: Persona) -> None:
        """人格文件重读后替换（路径不变时 WebUI 保存设置会顺带调用）。"""
        self.persona = persona
        self.context.persona = persona
        self.event_judge.persona_text = persona.text

    async def run(self) -> None:
        """启动全部适配器与后台任务，直到 stop。"""
        self._stop.clear()
        # 情绪系统冷启动：补一个「有人理」的基准时刻（只在从没记录时写）
        try:
            self.emotion.ensure_tracking()
        except Exception as exc:  # noqa: BLE001 - 情绪状态失败不能拖住启动
            logger.warning("初始化情绪状态失败：%s", exc)
        tasks = []
        try:
            # 首条消息也使用发现完毕的工具目录；离线服务仅等待首次尝试，不等待重连。
            await self.mcp.start()
            # 只补选图库里的真表情，不扫描原始消息的历史空缺。
            if self.config.stickers.enabled:
                try:
                    await self.sticker_behavior.reschedule_descriptions(self.context)
                except Exception as exc:  # noqa: BLE001 - 描述补建失败不能拖住启动
                    logger.warning("补建表情描述失败：%s", exc)
            tasks = [asyncio.create_task(self._run_adapter(adapter)) for adapter in self.adapters.values()]
            self._bg_task = asyncio.create_task(self._background_loop())
            self._merge_task = asyncio.create_task(self._merge_loop())
            self._scheduler_task = asyncio.create_task(self.scheduler.run(self._stop))
            self._proactive_task = asyncio.create_task(self._proactive_loop())
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
            # 事件通道的合并窗口也放出来：已到期的照常判断，未到期的只清掉
            try:
                await self.flush_event_batches()
            except Exception as exc:  # noqa: BLE001
                logger.warning("退出前处理待合并事件失败：%s", exc)
            for task in (self._bg_task, self._merge_task, self._scheduler_task, self._proactive_task):
                if task is not None:
                    task.cancel()
            self._bg_task = None
            self._merge_task = None
            self._scheduler_task = None
            self._proactive_task = None
            for task in tasks:
                task.cancel()
            await self.mcp.stop()

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
        if self.searcher is not None:
            try:
                await self.searcher.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭搜索客户端失败：%s", exc)
        if self.voice is not None:
            try:
                await self.voice.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭语音客户端失败：%s", exc)
        if self.plugins is not None:
            try:
                self.plugins.shutdown()
            except Exception as exc:  # noqa: BLE001 - 插件收尾失败不能挡住停机
                logger.warning("关闭插件失败：%s", exc)
        # 表情行为自建的 embedding 客户端；后台描述任务随事件循环结束
        try:
            await self.sticker_behavior.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭表情客户端失败：%s", exc)

    async def _background_loop(self) -> None:
        """定期检查抽取与归纳；间隔每轮从配置现读，改设置后无需重启。"""
        while not self._stop.is_set():
            try:
                await self.run_maintenance()
            except Exception as exc:  # noqa: BLE001
                logger.warning("后台维护任务异常：%s", exc)
            interval = max(5.0, self.config.background_check_minutes * 60)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # 定时任务与冷场开口
    # ------------------------------------------------------------------

    async def handle_scheduled_job(self, job, *, late: bool = False) -> None:
        """执行一条到期任务（目前只有提醒）：让模型按提醒文本现写一句话发出。

        发送走正常通道（拆条、清洗、落流水）；失败只记日志，不重试轰炸。
        """
        if job.kind != "reminder":
            logger.warning("未知的定时任务类型 %s（#%s），已跳过", job.kind, job.id)
            return
        text = str(job.payload.get("text") or "").strip()
        if not text:
            logger.warning("提醒 #%s 没有正文，已跳过", job.id)
            return
        adapter = self.adapters.get(job.platform)
        if adapter is None:
            logger.warning("提醒 #%s 的平台 %s 不在线，无法发送", job.id, job.platform)
            return
        is_private = job.chat_type == "private"
        parties = parties_from_payload(
            job.payload, platform=job.platform, chat_type=job.chat_type, chat_id=job.chat_id,
        )
        messages = build_reminder_prompt(
            persona=self.persona.system_prompt(),
            text=text,
            parties=parties,
            is_private=is_private,
            late=late,
        )
        reply = await self.generate(messages)
        if not reply:
            # 模型一句话都写不出：退回提醒原文，别把事吞掉
            reply = f"{text}" if not late else f"来晚了——{text}"
        event = MessageEvent(
            platform=job.platform,
            chat_type=job.chat_type,
            chat_id=job.chat_id,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text=reply,
        )
        if await self.send_reply(event, reply, reminder=True):
            logger.info("提醒 #%s 已发送%s：%s", job.id, "（迟到）" if late else "", text[:40])

    async def _proactive_loop(self) -> None:
        """冷场开口的后台循环：默认关，开着时每轮只在「可能该开口」的群里花一次生成。"""
        while not self._stop.is_set():
            try:
                await self._proactive_check()
            except Exception as exc:  # noqa: BLE001 - 主动开口失败不能带死循环
                logger.warning("冷场开口检查异常：%s", exc)
            # 评估冷却的 1/10 作为轮询间隔（至少 60 秒）；每轮现算，改配置后不必重启
            interval = max(60.0, self.config.proactive.cooldown_minutes * 6.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _proactive_check(self, *, now: float | None = None) -> bool:
        """扫一遍群会话，找出满足全部条件的群；返回是否开口了。"""
        now = time.time() if now is None else now
        cfg = self.config.proactive
        if not cfg.enabled:
            return False
        for chat in self.store.list_chats(limit=100):
            if chat.get("chat_type") != "group":
                continue
            platform, chat_id = str(chat["platform"]), str(chat["chat_id"])
            # 每轮对每个群只评估一次；评估口径（含冷却）由 opener 统一控制。
            # 相关性检索是惰性的：条件矩阵没过就根本不花这次计算。
            decision = self.opener.evaluate(
                self.store,
                platform,
                chat_id,
                now=now,
                relevance_fn=lambda platform=platform, chat_id=chat_id: self._proactive_relevance(
                    chat_id, platform
                ),
            )
            if decision.decision is not None:
                logger.debug(
                    "冷场开口评估 %s/%s：%s", platform, chat_id, decision.decision.log_text(),
                    extra={"platform": platform, "chat_id": chat_id, "chat_type": "group"},
                )
            if not decision.should_open:
                continue
            if await self._proactive_open(platform, chat_id, now=now):
                return True
        return False

    def _proactive_relevance(self, chat_id: str, platform: str) -> float:
        """本群近期话题与记忆的最高检索分；失败当 0（没积累就不容易开口）。"""
        try:
            recent = self.store.recent_messages(chat_id, limit=8, platform=platform, chat_type="group", real_only=True)
            text = " ".join(msg.text for msg in recent if msg.sender_id != BOT_SENDER_ID)
            if not text.strip():
                return 0.0
            scored = self.retriever.score(text)
            return max((item.score for item in scored), default=0.0)
        except Exception as exc:  # noqa: BLE001 - 相关性算不出来就当 0
            logger.debug("计算主动开口相关性失败：%s", exc)
            return 0.0

    async def _proactive_open(self, platform: str, chat_id: str, *, now: float) -> bool:
        """让模型现写一句并发送；写不出就放弃。返回是否真的开口。"""
        if platform not in self.adapters:
            return False
        recent = self.store.recent_messages(chat_id, limit=12, platform=platform, chat_type="group", real_only=True)
        summary = self.summarizer.active_summary(platform, chat_id, chat_type="group")
        block = format_summary_block(summary.summary) if summary is not None else ""
        # 冷场开口也是群聊生成，同样带上这个群的定位（只读库，不调模型）
        group_event = MessageEvent(
            platform=platform,
            chat_type="group",
            chat_id=chat_id,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text="",
        )
        profile_block = self.group_profile_text(group_event)
        if profile_block:
            block = "\n\n".join(part for part in (block, profile_block) if part)
        messages = self.opener.build_prompt(
            persona=self.persona.system_prompt(),
            bot_name=self.config.chat.bot_name,
            recent=recent,
            summary_block=block,
        )
        # 不传 event：主动开口不需要工具循环（也不该在没触发消息时调 recall）
        reply = await self.generate(messages)
        if not is_acceptable_opener(reply):
            logger.info("冷场开口尝试放弃：%s/%s（%s）", platform, chat_id, (reply or "")[:30],
                        extra={"platform": platform, "chat_id": chat_id, "chat_type": "group"})
            return False
        self.opener.note_opened(platform, chat_id, now)
        await self.send_proactive(platform, chat_id, reply, now=now)
        self.decision_engine.note_bot_reply(f"{platform}:{chat_id}", now, proactive=True)
        try:
            self.store.add_decision_log(
                platform=platform,
                chat_id=chat_id,
                chat_type="group",
                sender_id=BOT_SENDER_ID,
                sender_name=self.config.chat.bot_name,
                text=reply,
                should_reply=True,
                reason="proactive",
                score=0.0,
                threshold=self.config.proactive.threshold,
                factors={"proactive": True},
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 观测数据写不进去不影响发送
            logger.debug("写主动开口决策日志失败：%s", exc)
        logger.info("冷场开口 %s/%s：%s", platform, chat_id, reply[:40],
                    extra={"platform": platform, "chat_id": chat_id, "chat_type": "group"})
        return True

    async def run_maintenance(self, *, now: float | None = None, force_extract: bool = False) -> None:
        """抽取 + 归纳 + 摘要 + 群定位的一次检查；各环节互不影响。"""
        now = time.time() if now is None else now
        try:
            await self.extractor.maybe_extract(now=now, force=force_extract)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆抽取异常：%s", exc)
        try:
            await self.consolidator.maybe_consolidate(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆归纳异常：%s", exc)
        # 滚动摘要：开关关闭或没有会话需要压缩时开销很小；失败不影响其他维护
        try:
            await self.summarizer.maybe_summarize(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话摘要异常：%s", exc)
        # 群定位：只在后台归纳，收消息/裁定/生成三步的模型调用次数不受影响
        try:
            await self.group_profiles.maybe_update(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("群定位归纳异常：%s", exc)
        try:
            await self.states.maybe_update(now=now)
        except Exception as exc:  # 后台蒸馏失败不影响其余维护
            logger.warning("会话状态蒸馏异常：%s", exc)
        self._trim_usage(now)

    def _trim_usage(self, now: float) -> None:
        """用量表最多每天清理一次；失败只记 debug，不影响维护任务。"""
        if now - self.store.get_meta_float("llm_usage:trimmed_at", 0.0) < 86400:
            return
        try:
            self.store.trim_llm_usage(now=now)
        except Exception as exc:  # noqa: BLE001 - 清理失败不值得打断维护
            logger.debug("清理 LLM 用量记录失败：%s", exc)
            return
        self.store.set_meta("llm_usage:trimmed_at", str(now))

    @staticmethod
    def _now() -> float:
        """核心流水线的墙钟入口；离线回放可替换它而不改进程时钟。"""
        return time.time()
