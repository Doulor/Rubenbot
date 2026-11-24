"""核心流水线共用的上下文、投递结果、回复清理与展示函数。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import BaseAdapter, MessageEvent, Segment, render_segments
from .adapters.platform import PlatformEvent
from .config import Config
from .expression import ExpressionDecision
from .llm import strip_dsml
from .memory.consolidate import Consolidator
from .memory.extract import MemoryExtractor
from .memory.retrieve import Retriever
from .memory.store import MemoryStore
from .persona import Persona
from .plugins.loader import PluginManager
from .privacy import PrivacyGate
from .reminder import REMINDER_TOOL
from .sticker_behavior import VISUAL_SEGMENTS

logger = logging.getLogger("rubenbot.core")
# 生成契约（正文生成）：放开条数，允许 1~3 段短句。
# 段与段之间用换行分隔——发送端把换行当「另发一条」的硬边界。
REPLY_SEGMENT_CONTRACT = (
    "【这次的回复怎么写】先想清一个完整的意思。能一口气说完就发一段；"
    "还想顺着说下去，就在自然停顿处换行，一次 1~3 段短句，每段约 5~20 个字。"
    "后段接着前段延续、补充、递进或转折，连起来读仍是同一个念头。直接写聊天正文。"
    "被问到会的做法，就给能用的答案。拿不准的公开事实，有查证工具才去核对；没查实就直说。"
)

# recall 工具的定义（T2）。没配置搜索时只开放这一个工具，模型不用/失败就退化为单次生成。
RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": (
            "按时间线找聊过的原话和事件，也可以按人查画像、事实。"
            "当前对话需要补一段具体旧事时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_query": {
                    "type": "string",
                    "description": "可选的说话人：昵称或账号；不填则跨所有人和会话检索",
                },
                "topic": {
                    "type": "string",
                    "description": "话题关键词；留空表示看这个人最近的事",
                },
                "time_range": {
                    "type": "string",
                    "description": "时间范围，如 7d / 24h / 3d；留空表示不限",
                },
            },
            "required": [],
        },
    },
}

def chat_tools(config: Config) -> list[dict[str, Any]]:
    """当前配置下可用的工具集：recall（可选）+ set_reminder（可选）。

    两个开关都在 ``memory`` / ``reminder`` 段；工具不可用时模型自然只会
    普通生成，不需要别处再判断。
    """
    tools: list[dict[str, Any]] = []
    if config.memory.recall_tool_enabled:
        tools.append(RECALL_TOOL)
    if config.reminder.enabled:
        tools.append(REMINDER_TOOL)
    return tools


# ----------------------------------------------------------------------
# 文本后处理（发送节奏与多段拆分见 reply_segments.py）
# ----------------------------------------------------------------------

# 只认直接揶揄对方「光发图、不说话」的句式，不因正文提到了图就否决。
_IMAGE_SCOLD_RE = re.compile(
    r"(?:[只光就又]?发(?:了)?(?:一?[张个])?(?:图(?:片)?|表情(?:包)?)|(?:^|[，。！？\s])(?:图|表情包))"
    r"[，,、\s]*(?:也|都|又|咋|怎么)?(?:不说(?:话|是吧|一句)|不吱声|一(?:句|声)不[说吭])"
    r"|(?:咋|怎么|为啥)(?:又|老是|光|只)+发(?:图|表情)"
)


def _visual_only(events: list[MessageEvent]) -> bool:
    """只对真的视觉段放开必回覆盖；带文字的图片提问仍是普通提问。"""
    return bool(events) and all(
        any(seg.type in VISUAL_SEGMENTS for seg in item.segments)
        and all(seg.type in VISUAL_SEGMENTS | {"reply", "at"} or
                (seg.type == "text" and not seg.render().strip()) for seg in item.segments)
        and item.text.strip() in {render_segments(item.segments).strip(), ""}
        for item in events
    )


_VISUAL_PLACEHOLDERS = ("[图片]", "[表情]", "[动画表情]")


def _visual_placeholder(text: str) -> bool:
    """正文里带视觉占位（``[图片]`` / ``[表情]``）的流水也值得看一眼描述。"""
    return any(token in (text or "") for token in _VISUAL_PLACEHOLDERS)


# 括号动作清理：只删「短、纯中文、命中动作/语气词」的括号内容。
# 含数字/英文/符号（例如颜文字）、或拿不准的一律保留。
_ACTION_PAREN_RE = re.compile(r"[（(]([^（）()\n]{1,12})[）)]")
_TONE_RE = re.compile(r"^[嗯啊哦噢喔呀哈呵嘿嘻呜嘤喵汪诶欸唉哎呃唔]+$")
_ACTION_WORDS = frozenset(
    {
        # 面部表情
        "笑", "笑了笑", "轻笑", "微笑", "大笑", "坏笑", "苦笑", "干笑", "偷笑",
        "冷笑", "傻笑", "捂脸", "捧脸", "眨眼", "眨眨眼", "脸红", "皱眉",
        "皱了皱眉", "翻白眼", "白眼", "憋笑", "捂嘴", "捂嘴笑", "擦泪", "哭",
        "哭了", "流泪",
        # 肢体动作
        "点头", "点了点头", "点点头", "摇头", "摇了摇头", "摇摇头", "耸肩",
        "耸了耸肩", "摊手", "摊了摊手", "歪头", "偏头", "摆手", "摆了摆手",
        "挥手", "挥挥手", "鼓掌", "拍手", "起身", "坐下", "伸懒腰", "摸摸头",
        "摸了摸头", "挠头", "挠了挠头", "托腮", "低头", "抬头", "打滚", "溜走",
        "比心", "点赞", "握手", "鞠躬", "敬礼", "抱拳", "干杯", "抱头",
        # 声音/神态
        "叹气", "叹了口气", "轻叹", "叹气声", "打了个哈欠", "哈欠", "嘀咕",
        "嘟囔", "碎碎念", "哼唧", "清了清嗓子", "咳咳", "咳", "沉默",
        "沉默片刻", "沉思", "想了想", "思考", "轻声", "小声", "低声",
    }
)
# 纯语气/感叹的短词（不表动作，也不承载语义）
_INTERJECTIONS = frozenset({"啊这", "好家伙", "哎嘿", "嘿嘿", "哈哈", "呜呜"})


def _is_cjk(char: str) -> bool:
    """是否中文汉字（不含中文标点，标点按「其他字符」计）。"""
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def strip_action_parens(text: str) -> str:
    """移除括号包裹的动作描写，如「（笑）」「（点头）」。

    保守规则：只处理括号内 ≤6 字、纯中文、且命中动作词表或纯语气词的内容；
    含数字/英文/符号（例如颜文字）或拿不准的括号原样保留。
    """
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner or len(inner) > 6:
            return match.group(0)
        if not all(_is_cjk(ch) for ch in inner):
            return match.group(0)
        if inner in _ACTION_WORDS or inner in _INTERJECTIONS or _TONE_RE.match(inner):
            return ""
        return match.group(0)

    cleaned = _ACTION_PAREN_RE.sub(replace, text)
    # 删除后可能留下连续空格/行首空格
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


_TRANSCRIPT_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}\]\s*\[[^\]]*\]\s*")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
_MARKDOWN_LIST_RE = re.compile(r"(?m)^\s*\d+[.)、]\s*")
_LEADING_NARRATION_RE = re.compile(r"^\s*（[^（）\n]{1,40}）\s*")


def strip_transcript_prefix(text: str) -> str:
    """剥掉回复开头的转录行前缀，如 ``[10:00] [我] ``。

    模型偶尔会模仿历史里的 ``[HH:MM] [昵称] 正文`` 格式，把前缀也生成出来。
    只剥开头、只剥一次；正文中间出现的同样样式不动。
    """
    if not text:
        return text
    return _TRANSCRIPT_PREFIX_RE.sub("", text, count=1)


def clean_reply_patterns(text: str) -> str:
    """清理高置信的作文/旁白格式残留，不改正常正文标点。

    这里只处理发送链能确定是格式而非语义的模式：加粗标记、行首标题/列表号，
    以及整条开头的旁白括号。每条规则命中一次即记一条日志，便于线上回看误伤。
    """
    if not text:
        return text
    cleaned = text
    hits: list[str] = []
    stripped = strip_dsml(cleaned)
    if stripped != cleaned:
        cleaned = stripped
        hits.append("dsml")
    if "**" in cleaned:
        cleaned = cleaned.replace("**", "")
        hits.append("markdown_bold")
    if "__" in cleaned:
        cleaned = cleaned.replace("__", "")
        hits.append("markdown_underline")
    cleaned, count = _MARKDOWN_HEADING_RE.subn("", cleaned)
    if count:
        hits.append("markdown_heading")
    cleaned, count = _MARKDOWN_LIST_RE.subn("", cleaned)
    if count:
        hits.append("markdown_list")
    cleaned, count = _LEADING_NARRATION_RE.subn("", cleaned, count=1)
    if count:
        hits.append("leading_narration")
    # 单条消息中的空行是作文段落，不保留空行；普通单换行仍交给既有断句。
    if "\n\n" in cleaned:
        cleaned = re.sub(r"\n{2,}", "\n", cleaned)
        hits.append("blank_line")
    for rule in hits:
        logger.info("出站清洗命中规则 %s", rule)
    return cleaned.strip()


def _strip_segments_actions(segments: list[Segment]) -> list[Segment]:
    """对消息段列表做动作括号清理：只改文本段，清空后的文本段直接去掉。"""
    cleaned: list[Segment] = []
    for segment in segments:
        if segment.type != "text":
            cleaned.append(segment)
            continue
        text = strip_action_parens(str(segment.data.get("text", "")))
        # 段列表不经过 split_reply 的清洗，DSML 兜底要在这里再做一次
        text = strip_dsml(text)
        if text:
            cleaned.append(Segment.text(text))
    return cleaned


def _sticker_emotion_hint(expression: ExpressionDecision | None) -> str:
    """选表情用的情感提示：直接取表达决策书已算出的意图与增量点。

    不另做一套情感分析；没有决策书时返回空串，选择逻辑会退回上下文原文。
    """
    if expression is None:
        return ""
    parts = [expression.intent, expression.increment]
    return "：".join(part.strip() for part in parts if part and part.strip())


def _log_identity(event: MessageEvent | PlatformEvent) -> dict[str, str]:
    """把日志关联到实际事件，不从日志文本或模型输出推断身份。"""
    if isinstance(event, PlatformEvent):
        return {"platform": event.platform, "chat_type": event.conversation_type, "chat_id": event.conversation_id,
                "sender_id": event.actor_id, "sender_name": event.actor_name}
    return {"platform": event.platform, "chat_type": event.chat_type, "chat_id": event.chat_id,
            "sender_id": event.sender_id, "sender_name": event.sender_name}


def format_scene(event: MessageEvent, display: str) -> str:
    """拼装场景说明行。

    独立成函数，WebUI 的人设预览要用同一段文案，避免两处漂移。
    群定位（手写备注 / 自归纳）不在这里拼：那段要读库，由
    :meth:`RubenBot.group_profile_text` 单独追加。
    """
    if event.is_private:
        return (
            f"当前是私聊：现在只有你和{display}两个人单独说话。"
        )
    return (
        f"当前是群聊：群里有很多人，这条消息是{display}说的。"
        "你的回复群里所有人都看得见。甲把问题递给乙时，轮到乙来答；"
        "说话对象不清楚时，用群友的名字区分人。"
    )


def _parse_tool_arguments(arguments: Any) -> dict[str, Any] | None:
    """兼容已解析的对象与 JSON 字符串；区分合法空参数和格式错误。"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return None
    return arguments if isinstance(arguments, dict) else None


@dataclass
class SendResult:
    """一次回复的分段投递结果。正文仅供本轮后置钩子使用，不写进决策日志。"""

    raw_ids: list[int] = field(default_factory=list)
    delivered_parts: list[str] = field(default_factory=list)
    failed_parts: int = 0
    reason_code: str = ""

    @property
    def sent_parts(self) -> int:
        return len(self.raw_ids)

    def __bool__(self) -> bool:
        return self.sent_parts > 0

    @property
    def status(self) -> str:
        if self:
            return "partial" if self.failed_parts else "sent"
        return "superseded" if self.reason_code == "new_input" else "send_failed"

    def audit(self) -> dict[str, Any]:
        return {"status": self.status, "sent_parts": self.sent_parts, "failed_parts": self.failed_parts,
                "reason_code": self.reason_code, "raw_ids": self.raw_ids}


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

    @property
    def memory(self) -> Any | None:
        """读取侧（索引 / 预拉取 / recall），供核心命令及插件使用。"""
        return self.extras.get("memory_reader")

    @property
    def gate(self) -> PrivacyGate | None:
        """唯一的分寸审查口；插件判断「能不能说」一律走它。"""
        return self.extras.get("privacy_gate")
