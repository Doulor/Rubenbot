"""适配器接入、入站流水与媒体感知、按会话合批。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from . import computer
from .adapters.base import BaseAdapter, MessageEvent, Segment
from .adapters.platform import KIND_REQUEST, PlatformEvent
from .core_shared import _log_identity, logger
from .decision import BOT_SENDER_ID, MergedRound, ReplyDecision
from .gateway.events import EventBatch, RouteDecision
from .gateway.recall import route_recall
from .memory.extract import audience_for_chat
from .memory.store import make_person_id
from .memory.timeline import finish_voice, record_inbound
from .privacy import register_message
from .sticker_behavior import VISUAL_SEGMENTS, segment_description
from .voice import VOICE_PLACEHOLDER, VOICE_PREFIX

# 适配器异常后的重启退避（秒）
ADAPTER_RESTART_MIN = 1.0
ADAPTER_RESTART_MAX = 30.0


class IngressMixin:
    """适配器接入、入站流水与媒体感知、按会话合批。"""

    def register_adapter(self, adapter: BaseAdapter) -> None:
        self.adapters[adapter.name] = adapter
        # 事件通道：能解析 notice/request 的适配器把事件交给核心入口
        adapter.set_platform_event_callback(self.handle_platform_event)

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

    async def _merge_loop(self) -> None:
        """合并窗口到期检查：每轮 sleep 到最近的截止时间，短窗口才有意义。"""
        while not self._stop.is_set():
            wait = self.merger.next_deadline()
            event_wait = self.event_merger.next_deadline()
            candidates = [value for value in (wait, event_wait) if value is not None]
            nearest = min(candidates) if candidates else None
            timeout = 0.25 if nearest is None else max(0.01, min(0.25, nearest - self._now()))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                for batch in self.merger.pop_ready(self._now()):
                    self._start_round(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("放出合并消息异常：%s", exc)
            try:
                await self.flush_event_batches()
            except Exception as exc:  # noqa: BLE001
                logger.warning("放出合并事件异常：%s", exc)

    def _start_round(self, batch: MergedRound) -> None:
        """运行中，慢生成不能挡住适配器收下一条消息或其他群的截止时间。"""
        task = asyncio.create_task(self.process_round(batch))
        self._round_tasks.add(task)
        task.add_done_callback(self._round_tasks.discard)

    async def flush_merger(self, now: float | None = None, *, all_rounds: bool = False) -> None:
        """处理已到期的合并轮次；``all_rounds`` 时连未到期的一起处理（停机用）。"""
        now = self._now() if now is None else now
        rounds = self.merger.flush_all() if all_rounds else self.merger.pop_ready(now)
        for round_ in rounds:
            await self.process_round(round_)
        if all_rounds and self._round_tasks:
            await asyncio.gather(*self._round_tasks)

    # ------------------------------------------------------------------
    # 事件通道（感知面第二、三层）
    # ------------------------------------------------------------------

    async def handle_platform_event(self, event: PlatformEvent) -> None:
        """非消息事件入口：过滤自身 → 落库 → 路由 → 合并 → 快判。异常只记录。"""
        try:
            await self._handle_platform_event(event)
        except Exception as exc:  # noqa: BLE001 - 事件处理失败不能带死主循环
            logger.exception("处理平台事件异常：%s", exc)

    async def _handle_platform_event(self, event: PlatformEvent, *, now: float | None = None) -> EventBatch | None:
        """同步路径化的处理（测试直接调用）；``now`` 可注入。"""
        now = self._now() if now is None else now
        adapter = self.adapters.get(event.platform)
        self_id = getattr(adapter, "self_id", None) if adapter is not None else None
        # 自身引发的事件在入口过滤：自己戳自己、自己的操作回执等不自言自语
        if event.self_generated(self_id):
            logger.debug("忽略自身引发的平台事件：%s", event.description, extra=_log_identity(event))
            return None
        route = self.event_router.route(event, now=now)
        # 不记档不留发起人的事件摘要或时间线，但不改变实时事件的路由与响应。
        if not (self.store.privacy_enabled and event.actor_id
                and self.store.is_norecord(make_person_id(event.platform, event.actor_id))):
            self._record_platform_event(event, route, now=now)
            self._record_event_timeline(event, route, now=now)
            if event.kind == KIND_REQUEST:
                pending = self.store.count_pending_platform_events(kind=KIND_REQUEST)
                logger.info("收到申请（未处理 %s 条）：%s", pending, event.description, extra=_log_identity(event))
        if not self.config.gateway.judge_events:
            return None
        if route.route == "record":
            return None
        batches = self.event_merger.add(event, route, now)
        if batches:
            # 有到期的批次：判断它们（含本次加入后到期的）
            for ready in batches:
                await self.judge_event_batch(ready, now=now)
            return None
        if route.quiet:
            # 安静时段：只挂起记录，不当场动作；批次到期后由 flush 静默丢弃
            logger.info("安静时段事件已记录（挂起）：%s", event.description, extra=_log_identity(event))
            return None
        return None

    async def flush_event_batches(self, now: float | None = None) -> None:
        """放出到期的合并批次并快判；合并循环与停机时调用。"""
        now = self._now() if now is None else now
        for batch in self.event_merger.pop_ready(now):
            if batch.quiet:
                continue
            try:
                await self.judge_event_batch(batch, now=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("快判事件批次异常：%s", exc)

    # ---- 探测函数（条件判用） ----

    def _group_has_activity(self, platform: str, chat_id: str) -> bool:
        """该群有没有 bot 活动记录：bot 在当前群里说过话（流水里有 bot 消息）。"""
        if not chat_id:
            return False
        try:
            history = self.store.recent_messages(chat_id, limit=50, platform=platform, chat_type="group")
        except Exception:  # noqa: BLE001
            return False
        return any(message.sender_id == BOT_SENDER_ID for message in history)

    def _person_has_profile(self, platform: str, actor_id: str) -> bool:
        """这个人在画像里存不存在。"""
        if not actor_id:
            return False
        try:
            profile = self.store.get_profile(make_person_id(platform, actor_id))
        except Exception:  # noqa: BLE001
            return False
        if profile is None:
            return False
        return bool(profile.stable_lines or profile.status_lines)

    def _record_platform_event(self, event: PlatformEvent, route: RouteDecision, *, now: float) -> int:
        """落 platform_events 摘要；失败只记日志。"""
        try:
            return self.store.add_platform_event(event, route=route.route, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("落库平台事件失败：%s", exc)
            return 0

    def _record_event_timeline(self, event: PlatformEvent, route: RouteDecision, *, now: float) -> None:
        """事件进记忆时间线：有 actor 就记在 actor 名下，否则记在 self 名下。"""
        try:
            person_id = (
                make_person_id(event.platform, event.actor_id) if event.actor_id else "self"
            )
            self.store.add_event(
                person_id,
                event.description,
                ts=now,
                source_chat=f"{event.platform}:{event.conversation_id}",
                audience=audience_for_chat(event.conversation_type, event.conversation_id),
                salience=0.4 if route.judge else 0.2,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("事件写入时间线失败：%s", exc)


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
        now = self._now()
        # 重复事件先挡掉，避免为同一条语音条重复请求转写服务；
        # 空正文事件维持旧语义（不在这里去重，仍交给插件处理）
        has_media = any(s.type == "record" or s.type in VISUAL_SEGMENTS for s in event.segments or [])
        if (event.text.strip() or has_media) and self._is_duplicate(event, now):
            logger.debug(
                "重复消息，已跳过：%s %s %s", event.platform, event.platform_msg_id, event.text[:40]
            )
            return
        # 来源逐条过滤：不能将名单消息混进其他人的批次、唤起插件或刷新互动状态。
        if self._source_blocked(event, now):
            self.privacy_commands.purge_if_requested(self.context, event)
            return
        # 先留原话或语音占位；转写中的消息已能去重，完成后更新同一流水与事件。
        has_voice = any(s.type == 'record' for s in event.segments or [])
        if has_voice and VOICE_PLACEHOLDER not in event.text:
            event.text = self._replace_voice_placeholder(event.text, VOICE_PLACEHOLDER)
        await self._record(event, now)
        if has_voice:
            original = event.text
            await self.prepare_voice(event)
            if event.text != original:
                if not finish_voice(self.store, event, owner=computer.is_host(self.config.computer, event)):
                    return  # 等转写期间已清空，不能再把旧消息送去生成回复。
                register_message(self.store, event, event.raw_message_id)

        if event.is_group and event.sender_id != BOT_SENDER_ID and self.decision_engine.is_hard_trigger(event):
            self._engaged_at[(event.platform, event.chat_type, event.chat_id)] = now
        if await self.privacy_commands.handle(self.context, event):
            return
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
        # 自动模式：收到链接就预取，本轮的裁定/回复只取已抓到的资料。
        # 一条消息里的多个链接并发抓取，整批最坏等一个总超时；单个链接失败
        # 不影响其它链接，也不向上抛。on_mention 模式不在这里抓，等确认要
        # 回复后再按触发口径读。
        if self.link_service.active and self.link_service.config.mode == "auto":
            try:
                await self.link_service.prefetch(event)
            except Exception as exc:  # noqa: BLE001 - 预取失败按普通消息处理
                logger.warning("链接预取异常：%s", type(exc).__name__)
        # 主人按已配置的平台账号白名单识别，只提高查看优先级，不改必回规则。
        owner = computer.is_host(self.config.computer, event)
        # 逐条处理明确的平台委托，避免混入电脑任务或借用合批中他人的引用与权限。
        receipt = await route_recall(self.gateway, self.adapters.get(event.platform), event,
                                     hard_trigger=hard, owner=owner)
        if receipt is not None:
            sent = await self.send_reply(event, [Segment.text(receipt)], now=now)
            decision = ReplyDecision(bool(sent), reason='gateway_recall', hard_trigger=hard)
            self.last_decision[self.decision_engine.chat_key(event)] = decision
            self._log_decision(event, decision, now, delivery=sent)
            return
        priority = hard is not None or owner
        key = (event.platform, event.chat_type, event.chat_id)
        flight = self._flights.get(key)
        isolate = event.is_group and self.config.computer.enabled and computer.is_host(self.config.computer, event)
        if flight and not ((isolate or flight.isolated_sender) and event.sender_id != flight.primary):
            if flight.take(event, direct=hard is not None and not event.is_private):
                return
        ready = self.merger.add(event, now, hard=priority, urgent=priority, isolate_sender=isolate)
        for round_ in ready:
            if self._merge_task is not None:
                self._start_round(round_)
            else:
                await self.process_round(round_)

    def _observe_batch(self, events: list[MessageEvent]) -> None:
        # 同一人的补充仍算一个对话轮，不能把甲受到的称呼或赞骂记到乙名下。
        by_sender: dict[str, list[MessageEvent]] = {}
        for item in events:
            by_sender.setdefault(item.sender_id, []).append(item)
        for items in by_sender.values():
            self.observe_emotion(items[-1], events=items)

    async def _record(self, event: MessageEvent, now: float) -> None:
        """落流水时启动图片感知；实体事实快照仍限于可记账的群消息。"""
        if not event.text.strip():
            if not any(s.type in VISUAL_SEGMENTS for s in event.segments):
                return
            event.text = "".join(s.render() for s in event.segments)
        person_id = make_person_id(event.platform, event.sender_id)
        self.store.upsert_person(event.platform, event.sender_id, event.sender_name, now=now)
        if event.is_group and event.sender_name and event.sender_name != event.sender_id:
            self.store.upsert_identity(event.platform, event.sender_id, kind="user", group_id=event.chat_id,
                                       name=event.sender_name, now=now)
        if event.is_group and event.chat_name:
            self.store.upsert_identity(event.platform, event.chat_id, kind="group", name=event.chat_name, now=now)
        recordable = (bool(event.sender_id) and not event.sender_id.startswith("__")
                      and self.store.get_privacy_mode(person_id) == "normal")
        visual = "\n".join(filter(None, map(segment_description, event.segments))) if recordable else ""
        if event.reply_to_id and not event.reply_sender_id:
            target = self.store.message_by_platform_id(
                event.platform, event.reply_to_id, chat_id=event.chat_id, chat_type=event.chat_type,
            )
            if target:
                event.reply_sender_id = target.sender_id
        # 只有当前群缓存过的群名片可以补足无显示名的 @；仍以原账号为键。
        for uid, name in event.mentions.items():
            if event.is_group and not name and uid not in {BOT_SENDER_ID, "all"}:
                member = self.store.get_identity(event.platform, uid, group_id=event.chat_id)
                if member:
                    event.mentions[uid] = member["name"]
        raw_id = self.store.add_raw_message(
            platform=event.platform,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            visual_text=visual,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            text=event.text,
            ts=now,
            platform_msg_id=event.platform_msg_id or "",
            reply_to_id=event.reply_to_id or "",
            reply_sender_id=event.reply_sender_id,
            mentions=event.mentions,
        )
        # 隐私控制命令不是聊天见闻；自查/清空不应把命令本身计入报告。
        prefix = self.config.plugins.command_prefix
        command = event.text.strip().removeprefix(prefix).split() if prefix and event.text.strip().startswith(prefix) else []
        privacy_control = command == ['清空我的数据'] or (self.config.privacy.enabled and command[:1] == ['隐私'])
        if recordable and not privacy_control:
            record_inbound(self.store, event, raw_id, visual, owner=computer.is_host(self.config.computer, event), now=now)
        register_message(self.store, event, raw_id)
        event.raw_message_id = raw_id
        if recordable:
            self.sticker_behavior.ingest(self.context, event, raw_id)
        self._last_activity[f"{event.platform}:{event.chat_id}"] = now
        self.last_message_at = now
        logger.debug("记录消息 %s: %s", person_id, event.text[:60], extra=_log_identity(event))
        if event.is_group and recordable:
            try:
                event.familiarity = tuple(self.entities.familiarity(event.platform, event.chat_id, event.text + "\n" + visual))
                self._ledger_read.add((event.platform, event.chat_id))
                if event.familiarity:
                    row = self.store.raw_message_by_id(raw_id)
                    if row:
                        self.entities.record_known_mentions(platform=event.platform, chat_id=event.chat_id, message=row, now=now)
            except Exception as exc:  # 账本不可用仍照常处理这条消息
                self._ledger_read.discard((event.platform, event.chat_id))
                logger.warning("群实体查账失败：%s", exc)

    def _is_duplicate(self, event: MessageEvent, now: float) -> bool:
        """入站去重检查；去掉重或没有平台消息 id 时返回 False。"""
        if not event.platform_msg_id:
            return False
        ttl = self.config.chat.dedupe_ttl_seconds
        if ttl <= 0:
            return False
        try:
            return self.store.platform_message_exists(
                event.platform, event.platform_msg_id, since=now - ttl,
                chat_id=event.chat_id, chat_type=event.chat_type,
            )
        except Exception as exc:  # noqa: BLE001 - 去重失败宁可放行，不能丢消息
            logger.warning("入站去重检查失败：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 语音：入站转写
    # ------------------------------------------------------------------

    async def prepare_voice(self, event: MessageEvent) -> None:
        """把入站语音条转写成正文；任何失败都保留 ``[语音]`` 占位，不抛异常。

        有语音段时给 ``event.raw["has_voice"]`` 打标（合并后的轮次与语音回复
        判断都读它）；转写成功就把 ``[语音] 文本`` 拼进正文与流水，
        没有可转写的内容就至少让正文保留一个 ``[语音]``——纯语音条原本是
        空正文，会在落流水时被当成空消息丢掉。

        来源适配器支持原生转写（SnowLuma 的 ``fetch_ptt_text``）时只传消息
        id，音频按需下载：``qq_native`` 打头时一次下载请求都不发。
        """
        records = [segment for segment in event.segments or [] if segment.type == "record"]
        if not records:
            return
        event.raw["has_voice"] = True
        transcripts: list[str] = []
        if self.voice is not None:
            adapter = self.adapters.get(event.platform)
            native = self._native_transcriber(adapter)
            for index, segment in enumerate(records):
                if self._voice_too_long(segment):
                    logger.info(
                        "语音时长超过上限（%.0f 秒），跳过转写",
                        self.config.voice.stt_max_seconds,
                    )
                    continue

                # 原生转写按消息取文本，只对第一段生效；同一条消息里还有别的
                # 语音段时走音频下载，避免把整条转写重复拼进正文
                segment_native = native if index == 0 else None

                async def loader(data: Mapping[str, Any] = segment.data) -> Any:
                    return await self.voice.fetch_audio(data)

                try:
                    text = await self.voice.transcribe_voice(
                        None,
                        message_id=event.platform_msg_id,
                        native=segment_native,
                        audio_loader=loader,
                    )
                except Exception as exc:  # noqa: BLE001 - 转写失败只降级
                    logger.warning("语音转写异常：%s", exc)
                    text = None
                if text:
                    transcripts.append(text)
        body = " ".join(part.strip() for part in transcripts if part.strip())
        content = f"{VOICE_PREFIX}{body}" if body else VOICE_PLACEHOLDER
        event.text = self._replace_voice_placeholder(event.text, content)
        logger.debug("语音条转写：%s", content[:60])

    def _native_transcriber(self, adapter: BaseAdapter | None) -> Any:
        """来源平台的原生转写口；不支持时返回 None（交给 HTTP 后端）。

        返回 ``async (message_id) -> text``：经适配器声明的
        ``message.transcribe`` 能力调用，失败抛 ``CapabilityError``，由语音层
        顺延到下一个后端。
        """
        if adapter is None:
            return None
        declared = set(getattr(adapter, "capabilities", frozenset()) or ())
        if "message.transcribe" not in declared:
            return None
        config_key = (getattr(adapter, "capability_config_keys", None) or {}).get(
            "message.transcribe"
        )
        if config_key:
            default = (getattr(adapter, "capability_defaults", None) or {}).get(
                "message.transcribe", True
            )
            if not self.config.adapter_capability_enabled(
                adapter.name, config_key, default=default
            ):
                logger.info(
                    "适配器 %s 的原生转写已在配置中关闭，改走 HTTP 转写", adapter.name
                )
                return None

        async def transcribe(message_id: str) -> str | None:
            result = await adapter.call_capability(
                "message.transcribe", message_id=message_id
            )
            if isinstance(result, Mapping):
                for key in ("text", "transcript", "content"):
                    if result.get(key) is not None:
                        return str(result[key])
                return ""
            return None if result is None else str(result)

        return transcribe

    def _voice_too_long(self, segment: Segment) -> bool:
        """语音段自带时长（秒）时按上限判断；没有元数据就交给字节上限兜底。"""
        limit = float(self.config.voice.stt_max_seconds or 0.0)
        if limit <= 0:
            return False
        try:
            duration = float(segment.data.get("duration"))
        except (TypeError, ValueError):
            return False
        return duration > limit

    @staticmethod
    def _replace_voice_placeholder(current: str, content: str) -> str:
        """把正文投影里的 ``[语音]`` 占位换成转写结果（只换第一处）。

        转写覆盖的是这一条消息里的语音内容；同一事件里若有多段语音，后面的
        占位保持原样，避免把不相干的文字拼进同一处。
        """
        text = current or ""
        if VOICE_PLACEHOLDER in text:
            return text.replace(VOICE_PLACEHOLDER, content, 1)
        return f"{text}{content}" if text else content

    # ------------------------------------------------------------------
    # 情绪与好感度
    # ------------------------------------------------------------------

    def observe_emotion(
        self,
        event: MessageEvent,
        *,
        events: list[MessageEvent] | None = None,
        now: float | None = None,
    ) -> None:
        """这一轮消息对情绪的影响：记互动、识别被夸/被骂、算一轮对话。

        - 只有**对着 bot 说**的消息才算（私聊、@、回复、叫名字）；
        - 与 bot 对话一轮 +0.2（每人每日上限 +2），被夸 +1、被骂 −2；
        - 合并轮逐条看夸/骂和是否对着 bot 说，但「对话轮」一轮只记一次；
        - 任何异常都只记日志：情绪系统坏了也不能影响回复。
        """
        now = self._now() if now is None else now
        if not self.config.emotion.enabled:
            return
        try:
            raw_events = list(events or [event])
            # 一轮里只要有任意一条对着 bot 说（私聊/@/回复/叫名字），整轮就算
            # 对着 bot 的一口气：紧接着补一句「谢谢你」也属于这一轮。
            addressed = any(
                self.decision_engine.is_hard_trigger(item) is not None for item in raw_events
            )
            for item in raw_events:
                self._observe_one_emotion(item, addressed=addressed, now=now)
            if addressed:
                person_id = make_person_id(event.platform, event.sender_id)
                place = f"{event.platform}:{event.chat_id}"
                self.emotion.record_chat_round(person_id, place=place, now=now)
        except Exception as exc:  # noqa: BLE001 - 情绪状态失败不能影响回复
            logger.warning("更新情绪状态失败：%s", exc)

    def _observe_one_emotion(self, item: MessageEvent, *, addressed: bool, now: float) -> None:
        """合并轮里的一条原始消息：识别被夸/被骂。"""
        person_id = make_person_id(item.platform, item.sender_id)
        place = f"{item.platform}:{item.chat_id}"
        self.emotion.observe_message(
            person_id=person_id,
            text=item.text,
            addressed=addressed,
            place=place,
            now=now,
        )

    def note_emotion_after_reply(self, event: MessageEvent, *, now: float | None = None) -> None:
        """bot 发言后：记「有人理」，群里热闹且它参与多就兴奋一下。"""
        now = self._now() if now is None else now
        if not self.config.emotion.enabled:
            return
        try:
            self.emotion.note_engaged(now=now)
            if event.is_group:
                self.emotion.maybe_excited(event.platform, event.chat_id, now=now)
        except Exception as exc:  # noqa: BLE001 - 情绪状态失败不能影响回复
            logger.warning("更新发言后情绪失败：%s", exc)
