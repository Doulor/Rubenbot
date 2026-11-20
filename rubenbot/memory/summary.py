"""会话滚动摘要：把本会话的早期对话压成结构化摘要，prompt 里只放「摘要 + 近期原文」。

设计要点（只压缩**本会话**的对话历史，不碰跨会话的人物记忆）：

- 每个会话一行（``conversation_summaries``），用 ``covered_until_msg_id`` 记录
  已经压进摘要的原文水位；水位之前的原文不再拼进上下文。
- 触发：水位之后未摘要的原文超过 ``trigger_after`` 条时，把除最近
  ``keep_recent`` 条之外的那批消息交给模型，**增量折叠**进已有摘要。
- 五段式结构，总长有上限；代码兜底截断，防止模型越写越长。
- 任何失败都只记录日志并保留旧摘要/旧水位，不影响回复，下一轮维护再试。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from ..config import MemoryConfig
from ..llm import LLMProtocol
from ..memory.store import BOT_SENDER_ID, ConversationSummary, MemoryStore, RawMessage

logger = logging.getLogger(__name__)

# 单次最多压多少条原文：会话长期离线后积压可能很多，分成几轮压完，
# 避免一次请求把 prompt 撑爆；水位只推进到本轮实际压完的那条。
MAX_BATCH_MESSAGES = 200

# 摘要块在 prompt 里的标题；组装上下文与测试共用，避免两处漂移
SUMMARY_BLOCK_TITLE = "【早期对话摘要】"

SUMMARY_SYSTEM_PROMPT = """你是聊天会话的记录员。你会看到一个会话的已有摘要（可能为空）和一段新增的对话原文。
请把新增内容折叠进已有摘要，输出一份完整的、更新后的摘要。

输出要求：
- 直接输出摘要正文，不要前言、不要解释、不要 JSON、不要代码块。
- 固定五段，每段 1~4 行，全文不超过 {max_chars} 字；写不下时优先保留
  仍在继续的话题、人物关系和之后聊天用得上的细节，砍掉一次性的闲聊。
- 五段标题固定为：
  一、当前/近期话题
  二、关键事实
  三、人物动态
  四、未完的话题与梗
  五、氛围与注意事项
- 把新内容**合并**进已有摘要：仍然有效的内容保留，过时或被新内容修正的
  改写或删掉；不要另起一份、也不要逐条复述原文。
- 只依据提供的原文和已有摘要，不要补充它们没提到的人、事或细节。
- 这是一份本会话的对话压缩，不是对某个人的长期画像：不要下跨会话的长期结论。
- 原话里的梗、昵称、约定、没聊完的事尽量保住；语气保持客观简洁。
"""


@dataclass
class SummaryResult:
    """一次摘要检查的结果（供日志/测试观察）。"""

    conversation_id: str
    updated: bool = False
    covered_until_msg_id: int = 0
    compressed_messages: int = 0
    error: str | None = None


def format_summary_block(summary: str) -> str:
    """把摘要正文包成 prompt 里的摘要块，明确标注这是早期对话的压缩。"""
    body = (summary or "").strip()
    if not body:
        return ""
    return (
        f"{SUMMARY_BLOCK_TITLE}（本会话更早的对话已压缩成下面这份摘要，"
        "不是新消息；理解背景即可，不要逐句复述）\n"
        f"{body}"
    )


def trim_summary(text: str, max_chars: int) -> str:
    """把摘要截到上限；优先在段落边界收尾，实在不行硬截并加省略号。"""
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    # 尽量在最后一个段落/行边界断开，读起来自然一点
    cut = max(clipped.rfind("\n"), clipped.rfind("。"))
    if cut >= max_chars // 2:
        return clipped[: cut + 1].rstrip()
    # 硬截：给省略号留出位置，保证总长不超上限
    return clipped[: max(0, max_chars - 1)].rstrip() + "…"


# 喂给摘要模型的单条原文最多保留多少字：超长消息（贴的日志、长文）
# 只取开头，避免一次请求被个别消息撑爆。
MAX_MESSAGE_CHARS = 300


def format_messages_for_summary(messages: list[RawMessage]) -> str:
    """把待压缩的原文渲染成带时间和昵称的行，供摘要模型阅读。"""
    lines: list[str] = []
    for msg in messages:
        name = "我" if msg.sender_id == BOT_SENDER_ID else (msg.sender_name or msg.sender_id)
        clock = time.strftime("%m-%d %H:%M", time.localtime(msg.ts))
        text = " ".join((msg.text or "").split())
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS].rstrip() + "…"
        lines.append(f"[{clock}] [{name}] {text}")
    return "\n".join(lines)


class RollingSummarizer:
    """按会话维护滚动摘要；配置与 LLM 都可在热更新时替换。"""

    def __init__(self, llm: LLMProtocol, store: MemoryStore, config: MemoryConfig):
        self.llm = llm
        self.store = store
        self.config = config

    @property
    def settings(self):
        return self.config.rolling_summary

    # ------------------------------------------------------------------
    # 后台维护：检查所有会话，满足条件就增量更新
    # ------------------------------------------------------------------

    async def maybe_summarize(self, *, now: float | None = None) -> list[SummaryResult]:
        """检查所有活跃会话；满足触发条件就更新摘要。

        每个会话独立处理，单个会话失败只记日志、保留旧摘要与旧水位。
        开关关闭或表不可用时直接返回空列表。
        """
        now = time.time() if now is None else now
        settings = self.settings
        if not settings.enabled:
            return []
        results: list[SummaryResult] = []
        try:
            chats = self.store.list_chats(limit=1000)
        except Exception as exc:  # noqa: BLE001 - 摘要检查不能拖垮后台任务
            logger.warning("读取会话列表失败，跳过摘要：%s", exc)
            return []
        for chat in chats:
            try:
                result = await self.summarize_chat(
                    chat["platform"], chat["chat_id"], now=now
                )
            except Exception as exc:  # noqa: BLE001 - 单会话失败不影响其他会话
                logger.warning(
                    "会话 %s:%s 摘要失败：%s", chat["platform"], chat["chat_id"], exc,
                    extra={key: chat[key] for key in ("platform", "chat_id", "chat_type")},
                )
                results.append(
                    SummaryResult(
                        conversation_id=f"{chat['platform']}:{chat['chat_id']}",
                        error=str(exc),
                    )
                )
                continue
            if result is not None:
                results.append(result)
        return results

    async def summarize_chat(
        self, platform: str, chat_id: str, *, now: float | None = None
    ) -> SummaryResult | None:
        """对单个会话做一次检查；不需要更新时返回 None。"""
        now = time.time() if now is None else now
        settings = self.settings
        if not settings.enabled:
            return None
        conversation_id = f"{platform}:{chat_id}"
        current = self.store.get_conversation_summary(conversation_id)
        covered = current.covered_until_msg_id if current else 0
        pending_count = self.store.count_raw_messages_after(
            chat_id, platform=platform, after_id=covered, real_only=True
        )
        if pending_count <= settings.trigger_after:
            return None

        messages = self.store.raw_messages_after(
            chat_id, platform=platform, after_id=covered, real_only=True
        )
        keep = max(0, int(settings.keep_recent))
        batch = messages[:-keep] if keep else messages
        # 积压太多时分批：只压最前面这一批，剩下的下一轮继续
        if len(batch) > MAX_BATCH_MESSAGES:
            batch = batch[:MAX_BATCH_MESSAGES]
        if not batch:
            return None

        context = {"platform": platform, "chat_id": chat_id, "chat_type": batch[-1].chat_type}
        old_summary = (current.summary if current else "") or ""
        prompt = self._build_prompt(old_summary, batch)
        try:
            raw = await self.llm.complete(
                prompt, role="summary", task="rolling_summary", temperature=0.3
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败不阻塞，下一轮再试
            logger.warning("生成会话摘要失败（%s）：%s", conversation_id, exc, extra=context)
            return SummaryResult(
                conversation_id=conversation_id,
                covered_until_msg_id=covered,
                error=str(exc),
            )

        summary = trim_summary(_clean_output(raw), settings.max_chars)
        if not summary:
            logger.warning("会话摘要为空，保留旧摘要（%s）", conversation_id, extra=context)
            return SummaryResult(
                conversation_id=conversation_id,
                covered_until_msg_id=covered,
                error="empty summary",
            )

        covered_until = int(batch[-1].id)
        self.store.set_conversation_summary(
            conversation_id, summary, covered_until, now=now
        )
        logger.info(
            "会话摘要已更新：%s，压缩 %s 条，水位 %s",
            conversation_id,
            len(batch),
            covered_until,
            extra=context,
        )
        return SummaryResult(
            conversation_id=conversation_id,
            updated=True,
            covered_until_msg_id=covered_until,
            compressed_messages=len(batch),
        )

    def _build_prompt(self, old_summary: str, batch: list[RawMessage]) -> list[dict[str, str]]:
        """拼摘要请求：已有摘要 + 新增原文，要求增量折叠。"""
        settings = self.settings
        system = SUMMARY_SYSTEM_PROMPT.format(max_chars=max(100, int(settings.max_chars)))
        old_block = old_summary.strip() if old_summary.strip() else "（暂无，这是第一份摘要）"
        user = (
            f"已有摘要：\n{old_block}\n\n"
            f"新增对话原文（按时间顺序）：\n{format_messages_for_summary(batch)}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ------------------------------------------------------------------
    # 读取侧：拼上下文时取摘要
    # ------------------------------------------------------------------

    def active_summary(self, platform: str, chat_id: str) -> ConversationSummary | None:
        """当前生效的摘要；开关关闭、没有摘要或读库失败都返回 None。

        调用方据此决定「摘要块 + 水位之后原文」还是「照旧全量原文」。
        """
        if not self.settings.enabled:
            return None
        try:
            current = self.store.get_conversation_summary(f"{platform}:{chat_id}")
        except Exception as exc:  # noqa: BLE001 - 摘要读不到就退回纯原文
            logger.warning("读取会话摘要失败：%s", exc)
            return None
        if current is None or not (current.summary or "").strip():
            return None
        return current


_CODE_FENCE = re.compile(r"^```(?:[a-zA-Z]+)?\s*|\s*```$")


def _clean_output(raw: str) -> str:
    """清掉模型偶尔包上的代码块围栏与多余空白。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _CODE_FENCE.sub("", text).strip()
    return text
