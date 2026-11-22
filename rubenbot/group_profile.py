"""分群定位：每群两份并存——手写群备注 + 后台自归纳的群定位总结。

- **手写群备注**：用户在 WebUI 里写的一段话，描述这是什么群、bot 该怎么说话。
  可为空。
- **自归纳群定位**：后台任务按该群最近的聊天内容归纳出的氛围、常聊话题、
  成员关系与 bot 的角色。只在后台维护流水线里生成，**不在收消息、
  裁定、生成的热路径上调用模型**。

注入规则（见 :func:`resolve_profile_text`）：手写优先；手写空缺时用自归纳；
两份都没有就不注入。注入内容有长度上限，超长由代码兜底截断。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import GroupProfileConfig
from .llm import LLMProtocol
from .memory.store import GroupProfile, MemoryStore, RawMessage
from .memory.summary import format_messages_for_summary, trim_summary

logger = logging.getLogger(__name__)

# 注入块的标题：与记忆块、情绪块风格一致，明确这是背景而不是新消息
GROUP_PROFILE_TITLE = "【这个群】"

# 手写备注在 WebUI 一侧允许的最大长度（保存校验用）；注入时仍按 max_chars 截断
MANUAL_NOTE_MAX_CHARS = 2000

GROUP_PROFILE_SYSTEM_PROMPT = """你是群聊的记录员。你会看到一个群的已有定位总结（可能为空）和一段新增的群聊天原文。
请把新增内容折叠进已有总结，输出一份完整的、更新后的群定位。

输出要求：
- 直接输出总结正文，不要前言、不要解释、不要 JSON、不要代码块。
- 全文不超过 {max_chars} 字；写不下时优先保留对之后聊天有用的氛围、话题、
  人物关系与 bot 的角色，砍掉一次性的闲聊和具体流水账。
- 用简短的分行写，最多四行，分别覆盖（没有依据的可省略，不要硬凑）：
  氛围、常聊话题、成员关系、这个群里的 bot 是什么角色。
- 只依据提供的原文和已有总结，不要补充它们没提到的人、事或细节；
  不要下关于某个人私生活的长期结论。
- 语气客观简洁，避免评价和煽情。
"""


@dataclass
class GroupProfileResult:
    """一次后台归纳的结果（供日志/测试观察）。"""

    platform: str
    chat_id: str
    updated: bool = False
    covered_until_msg_id: int = 0
    compressed_messages: int = 0
    error: str | None = None


def resolve_profile_text(profile: GroupProfile | None, *, max_chars: int) -> tuple[str, str]:
    """按优先级取生效的定位正文：返回 ``(source, text)``。

    ``source`` 取 ``manual`` / ``auto`` / ``""``（没有可注入内容）。
    """
    if profile is None:
        return "", ""
    manual = trim_summary(profile.manual_note, max_chars)
    if manual:
        return "manual", manual
    auto = trim_summary(profile.auto_summary, max_chars)
    if auto:
        return "auto", auto
    return "", ""


def format_group_profile_block(text: str, source: str) -> str:
    """把定位包成 prompt 里的一段背景块；空正文返回空串。"""
    body = (text or "").strip()
    if not body:
        return ""
    origin = "群备注" if source == "manual" else "平时的观察"
    return (
        f"{GROUP_PROFILE_TITLE}（{origin}，只是背景，不必刻意提起）\n"
        f"在这个群里：{body}"
    )


def active_profile_block(
    store: MemoryStore, platform: str, chat_id: str, *, max_chars: int
) -> str:
    """拼出要注入的群定位块；读库失败或两份都空时返回空串。"""
    try:
        profile = store.get_group_profile(platform, chat_id)
    except Exception as exc:  # noqa: BLE001 - 定位读不到只是少一段背景
        logger.warning("读取群定位失败：%s/%s：%s", platform, chat_id, exc)
        return ""
    source, text = resolve_profile_text(profile, max_chars=max_chars)
    return format_group_profile_block(text, source)


class GroupProfileSummarizer:
    """后台归纳每个群的定位。

    只在 :meth:`RubenBot.run_maintenance` 里被调用；触发条件是
    「水位之后的新群消息达到 ``min_messages``」或「已有自归纳、且距上次
    更新超过 ``update_interval_hours`` 且至少有一条新消息」。失败只记日志
    并保留旧总结。
    """

    def __init__(self, llm: LLMProtocol, store: MemoryStore, config: GroupProfileConfig):
        self.llm = llm
        self.store = store
        self.config = config

    async def maybe_update(self, *, now: float | None = None) -> list[GroupProfileResult]:
        """扫描所有群，满足条件的逐个更新；单个群失败不影响其他群。"""
        now = time.time() if now is None else now
        if not self.config.enabled:
            return []
        try:
            chats = self.store.list_chats(limit=1000)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能拖垮主循环
            logger.warning("读取会话列表失败，跳过群定位归纳：%s", exc)
            return []
        results: list[GroupProfileResult] = []
        for chat in chats:
            if chat.get("chat_type") != "group":
                continue
            try:
                result = await self.update_chat(
                    str(chat["platform"]), str(chat["chat_id"]), now=now
                )
            except Exception as exc:  # noqa: BLE001 - 单群失败不影响其他群
                logger.warning(
                    "群 %s/%s 定位归纳失败：%s", chat["platform"], chat["chat_id"], exc,
                    extra={"platform": chat["platform"], "chat_id": chat["chat_id"], "chat_type": "group"},
                )
                results.append(
                    GroupProfileResult(
                        platform=str(chat["platform"]),
                        chat_id=str(chat["chat_id"]),
                        error=str(exc),
                    )
                )
                continue
            if result is not None:
                results.append(result)
        return results

    async def update_chat(
        self, platform: str, chat_id: str, *, now: float | None = None
    ) -> GroupProfileResult | None:
        """对单个群做一次检查；不需要更新时返回 None。"""
        now = time.time() if now is None else now
        if not self.config.enabled:
            return None
        current = self.store.get_group_profile(platform, chat_id)
        covered = current.covered_until_msg_id if current else 0
        pending = self.store.count_raw_messages_after(
            chat_id, platform=platform, chat_type="group", after_id=covered, real_only=True
        )
        if pending <= 0:
            return None
        # 首次归纳必须攒够 min_messages；已有总结时，除攒够新消息外，到了
        # 每日刷新点也顺手更新一次（「每天一次或每 N 条消息」）。只写了手写
        # 备注、自归纳还空着的群不走每日刷新，避免一条新消息就触归纳。
        has_auto = current is not None and bool((current.auto_summary or "").strip())
        interval_due = has_auto and (now - current.updated_at) >= (
            self.config.update_interval_hours * 3600
        )
        if pending < self.config.min_messages and not interval_due:
            return None

        messages = self.store.raw_messages_after(
            chat_id, platform=platform, chat_type="group", after_id=covered, real_only=True
        )
        batch = messages[: max(1, int(self.config.max_batch_messages))]
        if not batch:
            return None

        old_summary = (current.auto_summary if current else "") or ""
        prompt = self._build_prompt(old_summary, batch)
        context = {"platform": platform, "chat_id": chat_id, "chat_type": "group"}
        try:
            raw = await self.llm.complete(
                prompt, role="summary", task="group_profile", temperature=0.3
            )
        except Exception as exc:  # noqa: BLE001 - 失败保留旧总结，下一轮再试
            logger.warning("生成群定位失败（%s/%s）：%s", platform, chat_id, exc, extra=context)
            return GroupProfileResult(
                platform=platform, chat_id=chat_id, covered_until_msg_id=covered, error=str(exc)
            )

        summary = trim_summary(_clean_output(raw), self.config.max_chars)
        if not summary:
            logger.warning("群定位为空，保留旧总结（%s/%s）", platform, chat_id, extra=context)
            return GroupProfileResult(
                platform=platform, chat_id=chat_id, covered_until_msg_id=covered,
                error="empty summary",
            )

        # 等待模型期间可能有人清空了原话或使旧定位失效，不再写回过时的归纳。
        latest = self.store.get_group_profile(platform, chat_id)
        source_ids = {msg.id for msg in batch}
        if (self.store.available_raw_ids(source_ids) != source_ids
                or (latest.covered_until_msg_id if latest else 0) != covered
                or (latest.auto_summary if latest else '') != old_summary):
            return None
        covered_until = int(batch[-1].id)
        self.store.set_group_auto_summary(
            platform, chat_id, summary, covered_until, now=now
        )
        logger.info(
            "群定位已更新：%s/%s，归纳 %s 条，水位 %s",
            platform, chat_id, len(batch), covered_until, extra=context,
        )
        return GroupProfileResult(
            platform=platform,
            chat_id=chat_id,
            updated=True,
            covered_until_msg_id=covered_until,
            compressed_messages=len(batch),
        )

    def _build_prompt(self, old_summary: str, batch: list[RawMessage]) -> list[dict[str, str]]:
        """拼归纳请求：已有定位 + 新增原文，要求增量折叠。"""
        system = GROUP_PROFILE_SYSTEM_PROMPT.format(max_chars=max(50, int(self.config.max_chars)))
        old_block = old_summary.strip() if old_summary.strip() else "（暂无，这是第一份群定位）"
        user = (
            f"已有群定位：\n{old_block}\n\n"
            f"新增群聊原文（按时间顺序）：\n{format_messages_for_summary(batch)}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def _clean_output(raw: str) -> str:
    """清掉模型偶尔包上的代码块围栏与多余空白。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
