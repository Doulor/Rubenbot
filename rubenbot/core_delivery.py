"""发送航班、分段投递、出站记账与后置钩子。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import (
    SEND_STICKER,
    BaseAdapter,
    CapabilityError,
    MessageEvent,
    Segment,
    as_segment,
    render_segments,
)
from .adapters.platform import PlatformEvent
from .core_shared import (
    SendResult,
    _sticker_emotion_hint,
    _strip_segments_actions,
    logger,
    strip_action_parens,
)
from .decision import BOT_SENDER_ID
from .expression import ExpressionDecision
from .llm import strip_dsml
from .memory.extract import audience_for_chat
from .verification import RECORD_PLACEHOLDER, WebReply

# 「用语音说」的明确要求（tts_mode=on_request 时才看）：只认很像指令的短句，
# 不把「我发了条语音」这种陈述误判成要求。
_VOICE_REQUEST_RE = re.compile(
    r"(用|发|回|来)(一?条|个|段)?语音"
    r"|语音(说|回|答|讲|来)"
    r"|说给我听"
    r"|听听(你的|你)?声音"
)


def _send_error_code(event: MessageEvent, exc: Exception) -> str:
    # 现有适配器没有结构化拒发异常。只识别明确的收件人限制，不落错误原文。
    if event.is_private and re.search(r"\bresult=16\s+err=发送失败[，,]\s*请先添加对方为好友", str(exc)):
        return "private_recipient_refused"
    return "adapter_error"


@dataclass
class ReplyFlight:
    """持锁的首稿在飞期间，接走会使该草稿过时的入站消息。"""

    primary: str
    updates: list[MessageEvent] = field(default_factory=list)
    collecting: bool = True
    used_action: bool = False
    isolated_sender: bool = False
    computer_failed: bool = False

    def take(self, event: MessageEvent, direct: bool) -> bool:
        if not self.collecting or (not self.updates and event.sender_id != self.primary and not direct):
            return False
        self.updates.append(event)
        return True

    def begin_send(self) -> bool:
        # 从实际首段送出起不可撤回；此后到达的事件正常排到下一批。
        if self.updates:
            return False
        self.collecting = False
        return True


class DeliveryMixin:
    """发送航班、分段投递、出站记账与后置钩子。"""

    async def send_event_message(self, event: PlatformEvent, text: str, *, now: float | None = None) -> None:
        """事件结论里的「说一句话」：走现有发送通道，当作 bot 自己发的消息。"""
        if not text:
            return
        synthetic = MessageEvent(
            platform=event.platform,
            chat_type=event.conversation_type,
            chat_id=event.conversation_id,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text=text,
        )
        await self.send_reply(synthetic, text, now=now)

    async def notify_owner(self, event: PlatformEvent, text: str, *, now: float | None = None) -> bool:
        """通知主人：私聊第一个活跃会话。

        一期退化为「发给最近一个私聊会话」；找不到可用的私聊会话时只落
        pending + 日志（申请类事件的 pending 已在落库时写入），不假装通知成功。
        """
        now = self._now() if now is None else now
        target = self._owner_private_chat(event.platform)
        if target is None:
            logger.info("没有可用的主人私聊会话，通知只落库：%s", text[:60])
            return False
        synthetic = MessageEvent(
            platform=event.platform,
            chat_type="private",
            chat_id=target,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text=text,
        )
        await self.send_reply(synthetic, text[:200], now=now)
        return True

    def _owner_private_chat(self, platform: str) -> str | None:
        """最近活跃的私聊会话（主人通知的退化目标）；没有返回 None。"""
        try:
            chats = self.store.list_chats(limit=100)
        except Exception:  # noqa: BLE001 - 读不到会话只是通知不了
            return None
        for chat in chats:
            if chat.get("chat_type") != "private":
                continue
            if platform and chat.get("platform") != platform:
                continue
            return str(chat.get("chat_id"))
        return None

    async def send_proactive(
        self,
        platform: str,
        chat_id: str,
        reply: str,
        *,
        now: float | None = None,
    ) -> None:
        """主动开口走正常发送通道：拆条、清洗、落流水与自我时间线都一视同仁。"""
        event = MessageEvent(
            platform=platform,
            chat_type="group",
            chat_id=chat_id,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text=reply,
        )
        await self.send_reply(event, reply, now=now)

    def segment_delay(self, segments: Sequence[Any]) -> float:
        """相邻两段之间的等待时间（秒），按下一段的非空白字数估算。

        每字 ``reply.segment_delay_per_char_ms`` 毫秒，再截到
        ``segment_delay_min_ms`` / ``segment_delay_max_ms``；开关关闭或
        每字毫秒数为 0 时返回 0。第一段由调用方直接发出，不经过这里。
        """
        cfg = self.config.reply
        if not cfg.segment_delay_enabled:
            return 0.0
        per_char = max(0.0, float(cfg.segment_delay_per_char_ms))
        if per_char <= 0:
            return 0.0
        normalized = [as_segment(segment) for segment in segments]
        text = "".join(seg.render() for seg in normalized if seg.type == "text")
        chars = sum(1 for ch in text if not ch.isspace())
        if chars == 0:
            # 纯图片/表情条：按一次点击的固定停顿
            raw = float(cfg.segment_delay_min_ms)
        else:
            raw = chars * per_char
        low = max(0.0, float(cfg.segment_delay_min_ms))
        high = max(low, float(cfg.segment_delay_max_ms))
        return min(max(raw, low), high) / 1000.0

    async def try_sticker_reply(
        self, event: MessageEvent, expression: ExpressionDecision | None, *, proactive: bool, now: float,
        begin_send: Callable[[], bool] | None = None,
    ) -> SendResult | None:
        """执行策划选中的表情意图；普通接梗、短附和仍可按配置尝试非文字回应。"""
        if expression is None or expression.intent not in ("接梗", "短附和", "表情回应"):
            return None
        probability = self.config.stickers.nonverbal_probability
        if expression.intent == '表情回应' and probability > 0:
            probability = 1.0  # 明确选择的表达方式不再掷意愿；零概率仍视为禁用。
        if not await self.sticker_behavior.try_send(
            self.context, event, probability=probability,
            emotion_hint=_sticker_emotion_hint(expression), begin_send=begin_send,
        ):
            return None
        reply = "[表情]"
        raw_id = self._record_bot_message(event, reply, None)
        self.decision_engine.note_bot_reply(
            self.decision_engine.chat_key(event), now, proactive=proactive,
        )
        self.note_emotion_after_reply(event, now=now)
        await self.privacy_commands.after_reply(self.context, event)
        if self.plugins is not None:
            try:
                await self.plugins.run_after_reply(self.context, event, reply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 on_after_reply 异常：%s", exc)
        return SendResult(raw_ids=[raw_id], delivered_parts=[reply])

    async def send_reply(
        self,
        event: MessageEvent,
        reply: str | list[Segment] | list[dict[str, Any]],
        *,
        now: float | None = None,
        proactive: bool = False,
        begin_send: Callable[[], bool] | None = None,
        count_speech: bool = True,
        reminder: bool = False,
    ) -> SendResult:
        """拆条发送，并把 bot 自己的话写进流水（``processed=True``，不参与记忆抽取）。

        ``reply`` 可以是纯文本（换行分段 + 标点处概率合并），也可以是一组
        消息段：段列表原样交给适配器，整条消息只发一次，不做二次拆分。
        多条之间有按下一段字数估算的打字停顿（第一段立即发），可用
        ``reply.segment_delay_enabled`` 关闭。

        ``count_speech=False`` 用于失败 / 部分完成说明：仍落流水与自我时间线，
        但不计正常发言冷却与主动发言窗口（没办成不算接了一次话）。

        结果只计成功出站的流水；至少发出一条才计冷却，拆条连发只算一轮。
        """
        adapter = self.adapters.get(event.platform)
        if adapter is None:
            logger.warning("没有平台 %s 的适配器，无法回复", event.platform)
            return SendResult(reason_code="no_adapter")
        strip_actions = bool(getattr(self.config.chat, "strip_action_parens", False))
        # 语音回复：只对「没被拆开的单条短回复」生效；拆条/长文一律发文字
        voice_audio: bytes | None = None
        voice_mime = "audio/mpeg"
        if isinstance(reply, WebReply):
            # 证据只给当前受众，逐字保留；不拆条、不合成语音。
            parts = [[Segment.text(str(reply))]]
        elif isinstance(reply, str):
            text_reply = strip_action_parens(reply) if strip_actions else reply
            # DSML 兜底：split_reply 里也会清，这里提前清一次，保证语音路径同样干净
            text_reply = strip_dsml(text_reply)
            parts = [[Segment.text(part)] for part in self.split_reply(text_reply)]
            if len(parts) == 1:
                voice_audio, voice_mime = await self.synthesize_reply(event, text_reply)
        else:
            segments = [as_segment(segment) for segment in reply]
            if strip_actions:
                segments = _strip_segments_actions(segments)
            parts = [segments] if segments else []
        if not parts:
            # 拆条后什么都不剩（例如动作括号清理后为空）：不发，也不记发言
            return SendResult(reason_code="empty_reply")
        if begin_send and not begin_send():
            return SendResult(reason_code="new_input")
        if voice_audio:
            # 语音与文字兜底是同一段。明确拒收不换媒介再发。
            text = render_segments(parts[0])
            voice_error, sent_id = await self._send_voice(adapter, event, text, voice_audio, voice_mime)
            if voice_error == "private_recipient_refused":
                return SendResult(failed_parts=1, reason_code=voice_error)
            if not voice_error:
                raw_id = self._record_bot_message(event, text, sent_id,
                                                  self_summary=f"投递了提醒：{text}" if reminder else text)
                if count_speech:
                    self.opener_guard.note_reply((event.platform, event.chat_type, event.chat_id), text)
                    self.decision_engine.note_bot_reply(
                        self.decision_engine.chat_key(event), self._now() if now is None else now,
                        proactive=proactive,
                    )
                return SendResult(raw_ids=[raw_id], delivered_parts=[text])
        result = SendResult()
        for index, segments in enumerate(parts):
            if index > 0:
                delay = self.segment_delay(segments)
                if delay > 0:
                    await asyncio.sleep(delay)
            text = render_segments(segments)
            try:
                message_id = await adapter.send(event.chat_id, segments, chat_type=event.chat_type)
            except Exception as exc:  # noqa: BLE001 - 一条发失败不影响后续
                result.failed_parts += 1
                result.reason_code = _send_error_code(event, exc)
                logger.warning("发送失败（%s 第 %s 条）：%s", event.platform, index + 1, result.reason_code)
                if result.reason_code == "private_recipient_refused":
                    break  # 收件人受限，不重试这段，也不向同一私聊续发。
                continue
            # 流水保留回执、发言计数和回复关系，但摘要、自我时间线与实体账都拿不到网页。
            recorded = RECORD_PLACEHOLDER if isinstance(reply, WebReply) else text
            raw_id = self._record_bot_message(event, recorded, message_id, self_summary="" if reminder else recorded)
            result.raw_ids.append(raw_id)
            result.delivered_parts.append(recorded)
            # 以真正送达的文字条为准，不把网页引文、图片占位或失败的草稿当成自己的措辞。
            if not isinstance(reply, WebReply) and count_speech:
                spoken = "".join(s.render() for s in segments if s.type == "text")
                self.opener_guard.note_reply((event.platform, event.chat_type, event.chat_id), spoken)
        if not result:
            # 全部分条都发失败：当作没说过话——不进不应期、不记情绪
            logger.warning("发送全部失败（%s），不记本次发言", event.platform)
            return result
        if reminder:
            self._record_self_action(event, "投递了提醒：" + "\n".join(result.delivered_parts))
        # 发言时间交给决策引擎：发言后冷却与主动发言窗口都依赖它（拆条只记一次）。
        # count_speech=False 是失败 / 部分完成的一句说明，不算社交发言。
        if count_speech:
            self.decision_engine.note_bot_reply(
                self.decision_engine.chat_key(event), self._now() if now is None else now,
                proactive=proactive,
            )
            # 情绪侧：刚说过话 = 有人理；群里热闹就兴奋一下
            self.note_emotion_after_reply(event, now=now)
        return result

    def _record_bot_message(
        self, event: MessageEvent, text: str, message_id: str | None, *, self_summary: str | None = None
    ) -> int:
        """流水保留原话；提醒拆条时传空摘要，送完再合记一次。"""
        # 记入流水，供后续上下文使用；标记已处理，不参与记忆抽取。
        # 平台回执的消息 id 一并落库，后续引用解析与自我时间线依赖它。
        raw_id = self.store.add_raw_message(
            platform=event.platform,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            sender_id=BOT_SENDER_ID,
            sender_name=self.config.chat.bot_name,
            text=text,
            processed=True,
            platform_msg_id="" if message_id is None else str(message_id),
        )
        if self_summary != "":
            self._record_self_action(event, text if self_summary is None else self_summary)
        return raw_id

    def _record_self_action(self, event: MessageEvent, summary: str) -> None:
        """把 bot 自己发出的话或没有正文的交付（如静默发出的文件）记进自我时间线。

        失败只记日志：自我事件是背景，不能影响发送本身。
        """
        self._write_self_action(summary, f"{event.platform}:{event.chat_id}", audience_for_chat(event.chat_type, event.chat_id))

    def _record_music_delivery(self, platform: str, chat_type: str, chat_id: str, title: str) -> None:
        summary = f"点歌，发了《{title}》" if title else "点歌，发了一首歌"
        self._write_self_action(summary, f"{platform}:{chat_id}", audience_for_chat(chat_type, chat_id))

    def _write_self_action(self, summary: str, source_chat: str, audience: str) -> None:
        try:
            self.store.add_self_event(
                summary[: max(1, int(self.config.memory.self_event_max_chars))],
                source_chat=source_chat, audience=audience,
            )
        except Exception as exc:  # noqa: BLE001 - 自我事件失败不能影响发送
            logger.warning("记录自我事件失败：%s", exc)

    # ------------------------------------------------------------------
    # 语音：出站合成与发送
    # ------------------------------------------------------------------

    def wants_voice_reply(self, event: MessageEvent) -> bool:
        """本轮要不要用语音回：按 ``voice.tts_mode`` 与对方这轮是否发了语音判断。

        - ``off``：从不；
        - ``reply_voice``：对方这轮发了语音条；
        - ``on_request``：在上面基础上，被明确要求「用语音说」时也算。

        这是「想不想发」，是否真发还要看合成是否成功、平台是否支持、正文是否
        短到一条——那些在 :meth:`synthesize_reply` 与 :meth:`_send_voice` 里兜。
        """
        voice = self.config.voice
        if not voice.tts_enabled or not voice.tts_configured:
            return False
        if event.raw.get("has_voice"):
            return True
        if voice.tts_mode == "on_request" and _VOICE_REQUEST_RE.search(event.text or ""):
            return True
        return False

    async def synthesize_reply(self, event: MessageEvent, text: str) -> tuple[bytes | None, str]:
        """给一条未拆分的正文合成语音；不该发/太长/失败都返回 ``(None, "")``。

        返回 ``(音频字节, MIME)``；正文超过 ``voice.tts_max_chars`` 字不合成
        （太长没人听），照常发文字。
        """
        if self.voice is None or not self.wants_voice_reply(event):
            return None, ""
        body = (text or "").strip()
        if not body:
            return None, ""
        if len(body) > self.config.voice.tts_max_chars:
            logger.debug("回复 %s 字，超过语音上限，照常发文字", len(body))
            return None, ""
        audio = await self.voice.synthesize(body)
        if not audio:
            return None, ""
        return audio, "audio/mpeg"

    async def _send_voice(
        self,
        adapter: BaseAdapter,
        event: MessageEvent,
        text: str,
        audio: bytes,
        mime: str,
    ) -> tuple[str, str | None]:
        """经适配器的 ``message.voice`` 能力发语音；返回 ``(安全原因码或空串, 消息 id)``。

        不支持、未启用或普通语音故障可回退文字；收件人明确受限则不再投递。
        """
        name = "message.voice"
        declared = set(getattr(adapter, "capabilities", frozenset()) or ())
        if name not in declared:
            logger.info("适配器 %s 不支持语音回复，照常发文字", adapter.name)
            return "voice_unavailable", None
        config_key = (getattr(adapter, "capability_config_keys", None) or {}).get(name)
        if config_key:
            default = (getattr(adapter, "capability_defaults", None) or {}).get(name, True)
            if not self.config.adapter_capability_enabled(adapter.name, config_key, default=default):
                logger.info("适配器 %s 的语音回复已在配置中关闭，照常发文字", adapter.name)
                return "voice_unavailable", None
        try:
            result = await adapter.call_capability(
                name,
                audio=audio,
                mime=mime,
                text=text,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
            )
        except Exception as exc:  # noqa: BLE001 - 语音失败只降级，不影响主循环
            code = _send_error_code(event, exc)
            logger.warning("发送语音失败（%s）：%s", adapter.name, code)
            return code, None
        message_id: str | None = None
        if isinstance(result, Mapping):
            for key in ("message_id", "id"):
                if result.get(key) is not None:
                    message_id = str(result[key])
                    break
        return "", message_id

    # ------------------------------------------------------------------
    # 平台能力
    # ------------------------------------------------------------------

    async def call_capability(
        self,
        adapter_name: str | None,
        name: str,
        **kwargs: Any,
    ) -> tuple[bool, str]:
        """统一的平台能力入口：``(是否成功, 说明/失败原因)``。

        - ``adapter_name`` 为空时按顺序试所有在线适配器，用第一个**已声明且
          配置开关打开**的能力；都不可用则返回失败。
        - 适配器未声明、配置关闭、执行出错一律返回 ``(False, 原因)`` 并记日志，
          绝不抛异常打断主循环。
        """
        candidates: list[BaseAdapter] = []
        if adapter_name is None:
            candidates = list(self.adapters.values())
            if not candidates:
                logger.warning("调用能力 %s 失败：当前没有在线适配器", name)
                return False, "当前没有在线适配器"
        else:
            adapter = self.adapters.get(adapter_name)
            if adapter is None:
                logger.warning("调用能力 %s 失败：适配器 %s 不在线", name, adapter_name)
                return False, f"适配器 {adapter_name} 不在线"
            candidates = [adapter]

        reasons: list[str] = []
        for adapter in candidates:
            reason = self._capability_block_reason(adapter, name)
            if reason is not None:
                reasons.append(reason)
                continue
            try:
                result = await adapter.call_capability(name, **kwargs)
            except CapabilityError as exc:
                logger.warning("适配器 %s 调用能力 %s 失败：%s", adapter.name, name, exc)
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001 - 能力失败不能打断主循环
                logger.warning("适配器 %s 调用能力 %s 异常：%s", adapter.name, name, exc)
                return False, str(exc) or exc.__class__.__name__
            logger.info("适配器 %s 调用能力 %s 成功", adapter.name, name)
            if name == SEND_STICKER:
                self._record_sticker_ref(adapter, kwargs.get("sticker"), result)
            elif name == "qzone.publish" and kwargs.get("content"):
                # 空间可见范围未随回执返回，不能当成全网公开，也不借用触发命令的群。
                self._write_self_action(f"发了空间说说：{kwargs['content']}", f"{adapter.name}:qzone", "unknown")
            return True, self._capability_success_text(result)

        reason = "；".join(dict.fromkeys(reasons)) or f"没有适配器支持能力 {name}"
        logger.warning("调用能力 %s 失败：%s", name, reason)
        return False, reason

    def _capability_block_reason(self, adapter: BaseAdapter, name: str) -> str | None:
        """能力不可用的原因；可用时返回 None。"""
        declared = set(getattr(adapter, "capabilities", frozenset()) or ())
        if name not in declared:
            return f"适配器 {adapter.name} 不支持能力 {name}"
        config_key = (getattr(adapter, "capability_config_keys", None) or {}).get(name)
        if config_key:
            default = (getattr(adapter, "capability_defaults", None) or {}).get(name, True)
            if not self.config.adapter_capability_enabled(adapter.name, config_key, default=default):
                return f"适配器 {adapter.name} 的能力 {name} 已在配置中关闭（adapters.{adapter.name}.capabilities.{config_key}）"
        return None

    def _record_sticker_ref(self, adapter: BaseAdapter, sticker: Any, result: Any) -> None:
        """send_sticker 成功后回填平台原生引用（如 Telegram 的 file_id）。

        表情引用里带 ``id``（即表情库的记录 id）时才回填；平台没返回原生
        引用就跳过。回填失败只记日志，绝不影响这次成功的发送。
        """
        if not isinstance(result, Mapping) or not isinstance(sticker, Mapping):
            return
        sticker_id = sticker.get("id")
        if sticker_id in (None, ""):
            return
        sent = result.get("file_id")
        if sent in (None, ""):
            return
        platform = str(getattr(adapter, "name", "") or "")
        ref = {"file_id": str(sent)}
        try:
            self.stickers.record_native_ref(str(sticker_id), platform, ref)
        except Exception as exc:  # noqa: BLE001 - 回填失败不影响发送
            logger.warning("回填表情原生引用失败（%s/%s）：%s", platform, sticker_id, exc)

    @staticmethod
    def _capability_success_text(result: Any) -> str:
        if result is None:
            return "ok"
        if isinstance(result, dict):
            for key in ("message_id", "id"):
                if result.get(key) is not None:
                    return f"ok: {result[key]}"
        if isinstance(result, (str, int, float)):
            return f"ok: {result}"
        return "ok"

    async def _after_reply(self, event: MessageEvent, sent: SendResult, visual_only: bool) -> None:
        reply = "\n".join(sent.delivered_parts)
        if not visual_only:
            await self.sticker_behavior.on_after_reply(self.context, event, reply)
        await self.privacy_commands.after_reply(self.context, event)
        if self.plugins is not None:
            try:
                await self.plugins.run_after_reply(self.context, event, reply)
            except Exception as exc:  # noqa: BLE001
                logger.warning("插件 on_after_reply 异常：%s", exc)
