"""消息处理主循环。

流程：统一事件 → 落 raw_messages / 更新 people → 插件 on_message 钩子 → 合并窗口
→ 回复决策（硬触发 / 意愿评分）→ 拼上下文 → LLM 生成 → 否决标记检查
→ 拆分 → 拆条发送 → 插件 on_after_reply。

稳健性：每个环节都包了异常处理，单个事件/插件/LLM 失败只记录日志，不停循环。
后台任务负责：抽取记忆、归纳记忆、到时放出合并窗口里的消息。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapters.base import (
    BaseAdapter,
    CapabilityError,
    MessageEvent,
    Segment,
    as_segment,
    render_segments,
)
from .config import Config
from .decision import (
    BOT_SENDER_ID,
    DecisionEngine,
    MessageMerger,
    MergedRound,
    ReplyDecision,
    parse_generation_output,
)
from .emotion import EmotionEngine
from .llm import LLMClient, LLMProtocol, ToolUnsupported, make_usage_recorder
from .memory.consolidate import Consolidator
from .memory.extract import MemoryExtractor, audience_for_chat
from .memory.gate import PrivacyGate
from .memory.retrieve import MemoryReader, Retriever
from .memory.store import MemoryStore, make_person_id
from .memory.summary import RollingSummarizer, format_summary_block
from .persona import Persona
from .plugins.loader import PluginContext, PluginManager

logger = logging.getLogger(__name__)

# 适配器异常后的重启退避（秒）
ADAPTER_RESTART_MIN = 1.0
ADAPTER_RESTART_MAX = 30.0

# ---- 两段式生成（默认关闭）----
# 第一段用快速模型；没配 fast 时由 LLMConfig.model_for 沿回退链落到 chat
GUIDANCE_ROLE = "fast"
# 第一段只产出「怎么回」的简短指导，不做正文草稿；远超约定长度就截断
GUIDANCE_MAX_CHARS = 200
# 第一段最多等这么久（秒）；再长就直接回退单段，不让指导拖住回复
GUIDANCE_TIMEOUT_CAP = 15.0
# 第一段的任务说明：附在正文上下文末尾，只让模型想、不让它说
GUIDANCE_TASK_PROMPT = (
    "这条消息已经确定要回了，不用再判断该不该接话。"
    "现在先不要写回复正文，只替自己想清楚该怎么接：\n"
    "- 回应什么：这句话真正在说什么、需要接住哪一点\n"
    "- 态度语气：这轮该用什么样的情绪和分寸\n"
    "- 要提到的点：必须带上的信息；没有就写「无」\n"
    "- 要避开的：容易说错、说重或显得敷衍的地方\n"
    "只写几行要点，总长不超过 150 字；不要写成正文草稿，不要复述聊天记录。"
)
# 第二段末尾的生成指令：指导是内部材料，不能被说出来
GENERATION_INSTRUCTION = (
    "现在按上面的内部指导，直接写出这一轮要发出的回复正文；"
    "如果仍然判断不该接话，照前面的规则处理。"
    "不要输出思考过程，不要复述或提及这段指导。"
)

# recall 工具的定义（T2）。只开放这一个工具，模型不用/失败就退化为单次生成。
RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "recall",
        "description": (
            "回忆关于某人或某个话题的记忆（画像、旧事、事实）。"
            "背景记忆不够用时才调用，不要为了闲聊反复调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_query": {
                    "type": "string",
                    "description": "要回忆的人：昵称或你在上下文里看到的名字",
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
            "required": ["person_query"],
        },
    },
}


# ----------------------------------------------------------------------
# 发送节奏与文本后处理（借鉴 MaiBot 研读 #2 的最小版）
# ----------------------------------------------------------------------

# 打字时间估算：中文约 0.3s/字，其他字符约 0.15s/字，每条另加回车时间；
# 单字消息乘 3；纯图片/表情条固定 1s。倍率与单条上限在调用处应用。
_CJK_SECONDS = 0.3
_OTHER_SECONDS = 0.15
_ENTER_SECONDS = 0.3
_SINGLE_CHAR_FACTOR = 3.0
_STICKER_SECONDS = 1.0

# 句子切分点；切分时必须避开括号、引号、URL、行内代码
_SENTENCE_ENDS = frozenset("。！？!?…~～")
# 成对符号：开 -> 关（ASCII 引号开闭同型）
_BRACKET_PAIRS = {
    "（": "）",
    "(": ")",
    "「": "」",
    "『": "』",
    "【": "】",
    "《": "》",
    "〈": "〉",
    "[": "]",
    "{": "}",
    "“": "”",
    "‘": "’",
    '"': '"',
}
_URL_RE = re.compile(
    r"https?://[^\s<>\"'（）()【】「」『』，。！？；：、]+",
    re.IGNORECASE,
)

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


def estimate_typing_seconds(segments: Sequence[Any]) -> float:
    """估算一条消息的原始打字时间（秒），不含全局倍率和单条上限。

    只统计文本段的字符：中文 0.3s/字、其他非空白字符 0.15s/字，再加
    0.3s 回车时间；整条只有一个字时乘以 3。没有文本（纯图片/表情）固定 1s。
    """
    normalized = [as_segment(segment) for segment in segments]
    text = "".join(seg.render() for seg in normalized if seg.type == "text")
    if not text.strip():
        return _STICKER_SECONDS
    chars = [ch for ch in text if not ch.isspace()]
    cjk = sum(1 for ch in chars if _is_cjk(ch))
    others = len(chars) - cjk
    seconds = cjk * _CJK_SECONDS + others * _OTHER_SECONDS
    if len(chars) == 1:
        seconds *= _SINGLE_CHAR_FACTOR
    return seconds + _ENTER_SECONDS


def _find_closing(text: str, start: int, open_char: str, close_char: str) -> int:
    """从 ``start`` 起找配对的结束符号下标；找不到返回 -1（按普通字符处理）。"""
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if open_char != close_char and char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def split_sentences(text: str) -> list[str]:
    """按句末标点和换行切句，但不在括号、引号、URL、行内代码中间断开。

    颜文字通常整体落在括号里（如 ``(≧▽≦)``）；``QAQ``、``OTZ`` 这类不含
    句末标点的符号/字母串本身不会被切断。
    """
    parts: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)

    def flush() -> None:
        piece = "".join(buffer).strip()
        if piece:
            parts.append(piece)
        buffer.clear()

    while index < length:
        char = text[index]
        # URL：整段跳过，避免在 ? ! 等处切断
        if char in "hH":
            match = _URL_RE.match(text, index)
            if match:
                buffer.append(match.group(0))
                index = match.end()
                continue
        # 行内代码：`...`
        if char == "`":
            end = text.find("`", index + 1)
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        # 括号/引号：整段保留到配对的结束符号
        if char in _BRACKET_PAIRS:
            end = _find_closing(text, index + 1, char, _BRACKET_PAIRS[char])
            if end != -1:
                buffer.append(text[index : end + 1])
                index = end + 1
                continue
        buffer.append(char)
        index += 1
        if char in _SENTENCE_ENDS or char == "\n":
            # 吃掉切分点后的空白（含连续换行），拼接回来时不丢内容
            while index < length and text[index].isspace():
                index += 1
            flush()
    flush()
    return parts


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


def strip_transcript_prefix(text: str) -> str:
    """剥掉回复开头的转录行前缀，如 ``[10:00] [我] ``。

    模型偶尔会模仿历史里的 ``[HH:MM] [昵称] 正文`` 格式，把前缀也生成出来。
    只剥开头、只剥一次；正文中间出现的同样样式不动。
    """
    if not text:
        return text
    return _TRANSCRIPT_PREFIX_RE.sub("", text, count=1)


def _strip_segments_actions(segments: list[Segment]) -> list[Segment]:
    """对消息段列表做动作括号清理：只改文本段，清空后的文本段直接去掉。"""
    cleaned: list[Segment] = []
    for segment in segments:
        if segment.type != "text":
            cleaned.append(segment)
            continue
        text = strip_action_parens(str(segment.data.get("text", "")))
        if text:
            cleaned.append(Segment.text(text))
    return cleaned


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

    @property
    def memory(self) -> Any | None:
        """读取侧（索引 / 预拉取 / recall）；privacy 插件等可从这拿。"""
        return self.extras.get("memory_reader")

    @property
    def gate(self) -> PrivacyGate | None:
        """唯一的分寸审查口；插件判断「能不能说」一律走它。"""
        return self.extras.get("privacy_gate")


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
        self.gate = PrivacyGate(store, llm, config.memory)
        self.memory_reader = MemoryReader(
            store, llm, config.memory, gate=self.gate, retriever=self.retriever
        )
        self.extractor = MemoryExtractor(llm, store, config.memory)
        self.consolidator = Consolidator(llm, store, config.memory)
        # 会话滚动摘要：只压缩本会话的历史，与跨会话记忆各管各的
        self.summarizer = RollingSummarizer(llm, store, config.memory)
        # 情绪与好感度：确定性规则，状态落库，只做背景注入与阈值微调
        self.emotion = EmotionEngine(store, config.emotion)
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
            extras={
                "plugins": plugins,
                "bot": self,
                "memory_reader": self.memory_reader,
                "privacy_gate": self.gate,
                "emotion": self.emotion,
            },
        )
        # 每个会话最近一次收到消息的时间，用于日志/调试
        self._last_activity: dict[str, float] = {}
        # 合并窗口与意愿评分引擎；状态都在内存里
        self.merger = MessageMerger(
            config.decision.merge_window_seconds,
            config.decision.hard_merge_window_seconds,
            batch_max=config.decision.batch_max_seconds,
            urgent=config.decision.urgent_seconds,
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

    def apply_config(self, config: Config, *, rebuild_llm: bool = True) -> None:
        """热应用新配置：后续消息立即使用新的 LLM、意愿、记忆与聊天参数。

        WebUI 保存设置后调用。只换引用与参数，不动意愿评分/记忆算法本身；
        已经建立的连接（适配器、数据库、WebUI 端口）不在这里处理，那些
        字段只能重启生效。``rebuild_llm=False`` 时保留现有客户端（只改了
        意愿/记忆等参数时不必重建连接）。
        """
        old_llm = self.llm
        self.config = config
        if rebuild_llm:
            # 重建 LLM 客户端（改了 base_url / api_key / 超时等都需要新客户端）
            self.llm = LLMClient(config.llm, usage_recorder=make_usage_recorder(self.store))
        # 各模块持有的是配置对象引用，换成新对象即可让参数立即生效
        self.extractor.config = config.memory
        self.extractor.llm = self.llm
        self.consolidator.config = config.memory
        self.consolidator.llm = self.llm
        self.summarizer.config = config.memory
        self.summarizer.llm = self.llm
        # 情绪规则：参数换新，已落库的好感度/心情不受影响
        self.emotion.config = config.emotion
        # 读取侧与审查口：配置/模型一起热换，索引缓存保留（配置里 TTL 会立即生效）
        self.memory_reader.config = config.memory
        self.memory_reader.llm = self.llm
        self.memory_reader.gate.llm = self.llm
        self.memory_reader.gate.config = config.memory
        self.context.config = config
        if self.plugins is not None:
            self.plugins.config = config
        # 意愿引擎：配置与别名一起换，内部状态（冷却/动量）保留
        self.decision_engine.config = config.decision
        aliases = config.decision.aliases or [config.chat.bot_name]
        self.decision_engine.set_aliases(aliases)
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
            "配置已热应用：模型=%s 阈值=%.2f 注入上限=%s",
            config.llm.chat_model,
            config.decision.threshold,
            config.memory.inject_limit,
        )

    def reload_persona(self, persona: Persona) -> None:
        """人格文件重读后替换（路径不变时 WebUI 保存设置会顺带调用）。"""
        self.persona = persona
        self.context.persona = persona

    async def run(self) -> None:
        """启动全部适配器与后台任务，直到 stop。"""
        self._stop.clear()
        # 情绪系统冷启动：补一个「有人理」的基准时刻（只在从没记录时写）
        try:
            self.emotion.ensure_tracking()
        except Exception as exc:  # noqa: BLE001 - 情绪状态失败不能拖住启动
            logger.warning("初始化情绪状态失败：%s", exc)
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
        # 滚动摘要：开关关闭或没有会话需要压缩时开销很小；失败不影响其他维护
        try:
            await self.summarizer.maybe_summarize(now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话摘要异常：%s", exc)
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
        if not await self._record(event, now):
            # 重复事件：直接跳过，不再进插件/合并窗口
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
        ready = self.merger.add(
            event,
            now,
            hard=hard is not None,
            urgent=hard in ("mentioned", "reply_to_bot"),
        )
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
        self.observe_emotion(event, events=round_.events, now=now)
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

        two_stage = bool(self.config.generation.two_stage.enabled)
        task = "gen_stage2" if two_stage else "chat"
        guidance = ""
        if two_stage:
            # 第一段只「想」：失败/空/超时都退化为单段直出，绝不影响回复
            guidance = await self.guide_reply(messages)
            if guidance:
                messages = self.with_guidance(messages, guidance)

        raw_reply = await self.generate(messages, event=event, task=task)
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
                    "mood_adjust": factors.mood_adjust,
                }
                if factors is not None
                else {},
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 观测数据写不进去也不能影响回复
            logger.warning("写入决策日志失败：%s", exc)

    async def _record(self, event: MessageEvent, now: float) -> bool:
        """落流水 + 更新人物；返回 False 表示这是重复事件（已被丢掉）。

        去重：同一 (platform, platform_msg_id) 在 ``chat.dedupe_ttl_seconds``
        时间窗内重复到达时只记 debug 日志并跳过；平台消息 id 为空的平台不去重。
        """
        if not event.text.strip():
            return True
        if self._is_duplicate(event, now):
            logger.debug(
                "重复消息，已跳过：%s %s %s", event.platform, event.platform_msg_id, event.text[:40]
            )
            return False
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
            platform_msg_id=event.platform_msg_id or "",
            reply_to_id=event.reply_to_id or "",
        )
        self._last_activity[f"{event.platform}:{event.chat_id}"] = now
        self.last_message_at = now
        logger.debug("记录消息 %s: %s", person_id, event.text[:60])
        return True

    def _is_duplicate(self, event: MessageEvent, now: float) -> bool:
        """入站去重检查；去掉重或没有平台消息 id 时返回 False。"""
        if not event.platform_msg_id:
            return False
        ttl = self.config.chat.dedupe_ttl_seconds
        if ttl <= 0:
            return False
        try:
            return self.store.platform_message_exists(
                event.platform, event.platform_msg_id, since=now - ttl
            )
        except Exception as exc:  # noqa: BLE001 - 去重失败宁可放行，不能丢消息
            logger.warning("入站去重检查失败：%s", exc)
            return False

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
            # 心情只微调普通群消息的开口阈值；硬触发本来就不看分数
            mood_adjust=self.emotion.threshold_adjust(now=now),
        )

    def should_reply(self, event: MessageEvent, *, now: float | None = None) -> ReplyDecision:
        """兼容旧调用名，等价于 :meth:`decide`。"""
        return self.decide(event, now=now)

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
        now = time.time() if now is None else now
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
        now = time.time() if now is None else now
        if not self.config.emotion.enabled:
            return
        try:
            self.emotion.note_engaged(now=now)
            if event.is_group:
                self.emotion.maybe_excited(event.platform, event.chat_id, now=now)
        except Exception as exc:  # noqa: BLE001 - 情绪状态失败不能影响回复
            logger.warning("更新发言后情绪失败：%s", exc)

    def emotion_text(self, event: MessageEvent, *, now: float | None = None) -> str:
        """状态注入行（平静且普通档位时为空串）；失败退化为空。"""
        now = time.time() if now is None else now
        try:
            person_id = make_person_id(event.platform, event.sender_id)
            return self.emotion.injection_line(person_id, now=now)
        except Exception as exc:  # noqa: BLE001 - 注入失败只少一行背景
            logger.warning("组装情绪状态失败：%s", exc)
            return ""

    # ------------------------------------------------------------------
    # 上下文拼装
    # ------------------------------------------------------------------

    # 引用消息正文的截断长度（渲染历史时）
    QUOTE_SNIPPET_CHARS = 50
    # 回复目标（本轮正在回的那条）正文的截断长度
    REPLAY_TARGET_CHARS = 300

    @staticmethod
    def _format_clock(ts: float) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    def _quote_suffix(self, msg, index: dict[str, object]) -> str:
        """被引用消息的附注：查得到就带昵称+截断正文，查不到只说明引用了消息。"""
        if not msg.reply_to_id:
            return ""
        target = index.get(msg.reply_to_id)
        if target is None:
            return "（引用了一条消息）"
        name = "我" if target.sender_id == BOT_SENDER_ID else (target.sender_name or target.sender_id)
        snippet = self._truncate(target.text, self.QUOTE_SNIPPET_CHARS)
        return f"（引用 [{name}]：{snippet}）"

    def render_history(self, messages: list) -> list[dict[str, str]]:
        """把流水渲染成对话消息：别人的是 ``[HH:MM] [昵称] 正文``，bot 自己的只要正文。

        bot 的历史就是它说过的话，必须是干净正文：带上 ``[HH:MM] [我]`` 前缀后，
        模型会模仿这个格式，把自己的回复也生成成转录行（线上出过这种串台）。
        别人的消息保留前缀，模型需要知道群里谁在说话。

        被引用的消息只在**本批历史**里按 ``reply_to_id`` 查；查不到就只标
        「（引用了一条消息）」。附注放在正文后的独立括号里，不属于正文。
        """
        index: dict[str, object] = {}
        for msg in messages:
            if msg.platform_msg_id:
                index.setdefault(msg.platform_msg_id, msg)

        rendered: list[dict[str, str]] = []
        for msg in messages:
            is_bot = msg.sender_id == BOT_SENDER_ID
            if is_bot:
                content = msg.text
            else:
                name = msg.sender_name or msg.sender_id
                content = f"[{self._format_clock(msg.ts)}] [{name}] {msg.text}"
            content += self._quote_suffix(msg, index)
            rendered.append({"role": "assistant" if is_bot else "user", "content": content})
        return rendered

    def _reply_target_section(self, event: MessageEvent) -> str:
        """本轮正在回的那条消息：单独截断放进上下文要求区，不整段重复。"""
        if not event.reply_to_id:
            return ""
        try:
            target = self.store.message_by_platform_id(
                event.platform, event.reply_to_id, chat_id=event.chat_id
            )
        except Exception as exc:  # noqa: BLE001 - 查引用失败不影响回复
            logger.warning("查询被引用消息失败：%s", exc)
            target = None
        if target is None:
            # 历史里已经标了「（引用了一条消息）」，这里不重复占位
            return ""
        is_bot = target.sender_id == BOT_SENDER_ID
        name = "我" if is_bot else (target.sender_name or target.sender_id)
        body = self._truncate(target.text, self.REPLAY_TARGET_CHARS)
        return f"对方引用的那条消息（{name}）：{body}"

    @staticmethod
    def _requirements_section(reply_target: str) -> str:
        """上下文要求区：本轮回复目标（被引用那条）单独截断呈现，不整段重复。"""
        if not reply_target:
            return ""
        return "上下文要求：\n" + "\n".join(
            [
                f"- {reply_target}",
                "- 被引用的内容只作理解背景，不必复述；直接接当前这轮的话。",
            ]
        )

    async def build_context(self, event: MessageEvent, *, now: float | None = None) -> list[dict[str, str]]:
        """人格 + 记忆 + 本会话早期摘要 + 场景 + 上下文要求 + 最近历史。

        会话有滚动摘要时，历史部分换成「摘要块 + 摘要水位之后的原文」；
        没有摘要（或开关关闭）时和以前完全一致，直接带最近原文。
        """
        now = time.time() if now is None else now
        persona_prompt = self.persona.system_prompt()
        person_id = make_person_id(event.platform, event.sender_id)
        person = self.store.get_person(person_id)
        display = (person.display_name if person else "") or event.sender_name

        sections: list[str] = [persona_prompt]
        memory_text = await self.memory_text(event, now=now)
        if memory_text:
            sections.append(memory_text)
        # 情绪与好感度：一行状态描述，放在记忆之后、场景之前；平静且普通时整行省掉
        emotion_text = self.emotion_text(event, now=now)
        if emotion_text:
            sections.append(emotion_text)
        # 本会话的早期对话压缩：只补对话历史，不涉及跨会话记忆
        summary = self.summarizer.active_summary(event.platform, event.chat_id)
        covered_until = 0
        if summary is not None:
            block = format_summary_block(summary.summary)
            if block:
                sections.append(block)
                covered_until = int(summary.covered_until_msg_id or 0)
        # 场景说明带行为约束，压住人格文件里可能残留的场景想象
        sections.append(format_scene(event, display))
        requirements = self._requirements_section(self._reply_target_section(event))
        if requirements:
            sections.append(requirements)
        system_prompt = "\n\n".join(sections)

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        since = now - self.config.chat.history_ttl_hours * 3600
        history = self.store.recent_messages(
            event.chat_id,
            limit=self.config.chat.history_size,
            since=since,
            platform=event.platform,
            real_only=True,
            after_id=covered_until,
        )
        messages.extend(self.render_history(history))
        return messages

    def recent_window_texts(self, event: MessageEvent, *, now: float | None = None) -> list[str]:
        """取本轮消息窗口的文本（被提及者识别用），失败返回空列表。"""
        now = time.time() if now is None else now
        try:
            history = self.store.recent_messages(
                event.chat_id,
                limit=self.config.chat.history_size,
                since=now - self.config.chat.history_ttl_hours * 3600,
                platform=event.platform,
                real_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - 窗口取不到只是少一点上下文
            logger.warning("读取消息窗口失败：%s", exc)
            return []
        return [msg.text for msg in history if msg.sender_id != BOT_SENDER_ID]

    async def memory_text(self, event: MessageEvent, *, now: float | None = None) -> str:
        """读取侧组装好的记忆区块；任何失败都退化为空（不影响回复）。"""
        now = time.time() if now is None else now
        person_id = make_person_id(event.platform, event.sender_id)
        try:
            block = await self.memory_reader.assemble(
                text=event.text,
                person_id=person_id,
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                window_texts=self.recent_window_texts(event, now=now),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 记忆组装失败不能拖垮回复
            logger.warning("组装记忆区块失败：%s", exc)
            return ""
        return block.render()

    async def recall(
        self,
        person_query: str,
        topic: str = "",
        time_range: str = "",
        *,
        event: MessageEvent | None = None,
        now: float | None = None,
    ) -> str:
        """T2：按人/话题/时段深查记忆（结果内部过分寸审查口）。

        ``event`` 给了就按当前会话的受众审查；不给则按「未知受众」审查
        （跨场合项会被扣下）。任何异常都返回一句「查不到」，不向外抛。
        """
        now = time.time() if now is None else now
        if event is not None:
            audience_now = audience_for_chat(event.chat_type, event.chat_id)
            speaker = make_person_id(event.platform, event.sender_id)
        else:
            audience_now, speaker = "unknown", ""
        try:
            return await self.memory_reader.recall(
                person_query,
                topic,
                time_range,
                audience_now=audience_now,
                speaker=speaker,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 回忆失败只当没查到
            logger.warning("recall 失败：%s", exc)
            return "回忆失败，暂时想不起来。"

    # ------------------------------------------------------------------
    # 生成与发送
    # ------------------------------------------------------------------

    async def guide_reply(self, messages: list[dict[str, str]]) -> str:
        """两段式的第一段：按与正文相同的上下文，产出一段简短回复指导。

        任何失败（调用异常、超时、空内容）都返回空串，由调用方回退单段直出；
        这里只记日志，绝不向上抛。用量经由 LLM 客户端记 ``gen_stage1``。
        """
        request = list(messages) + [{"role": "user", "content": GUIDANCE_TASK_PROMPT}]
        try:
            text = await asyncio.wait_for(
                self.llm.complete(request, role=GUIDANCE_ROLE, task="gen_stage1"),
                timeout=self.guidance_timeout(),
            )
        except asyncio.TimeoutError:
            logger.warning("两段式第一段超时（%.1fs），回退单段直出", self.guidance_timeout())
            return ""
        except Exception as exc:  # noqa: BLE001 - 指导失败不能拖垮回复
            logger.warning("两段式第一段失败，回退单段直出：%s", exc)
            return ""
        guidance = (text or "").strip()
        if not guidance:
            logger.info("两段式第一段返回空内容，回退单段直出")
            return ""
        marker = self.config.decision.veto_marker
        if marker and marker in guidance:
            # 第一段顺着上下文里的「可以不接话」出口回了否决标记：这不是指导
            logger.info("两段式第一段只返回了否决标记，回退单段直出")
            return ""
        if len(guidance) > GUIDANCE_MAX_CHARS:
            # 明显跑偏成了草稿：宁可截断也不要让第二段照抄
            guidance = guidance[:GUIDANCE_MAX_CHARS].rstrip()
        logger.debug("两段式第一段指导：%s", guidance)
        return guidance

    def guidance_timeout(self) -> float:
        """第一段的等待上限：不超过客户端超时，也不超过固定上限。"""
        configured = float(self.config.llm.timeout or 0.0)
        if configured <= 0:
            return GUIDANCE_TIMEOUT_CAP
        return max(0.1, min(configured, GUIDANCE_TIMEOUT_CAP))

    @staticmethod
    def with_guidance(
        messages: list[dict[str, str]], guidance: str
    ) -> list[dict[str, str]]:
        """把指导插到历史之后、生成指令之前，标注为内部材料、不要复述。"""
        block = (
            "下面是给你自己的内部回复指导，只用来决定怎么说，不要复述、不要提及：\n"
            f"{guidance}\n\n"
            f"{GENERATION_INSTRUCTION}"
        )
        return list(messages) + [{"role": "system", "content": block}]

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        event: MessageEvent | None = None,
        task: str = "chat",
    ) -> str:
        """生成回复；模型支持工具调用时先给最多两轮 recall 工具循环。

        工具不可用、模型不用工具、或任何一步出问题，都退化为普通单次生成。
        ``task`` 只用于用量打标：单段直出是 ``chat``，两段式的第二段是
        ``gen_stage2``。
        """
        if event is not None and self.config.memory.recall_tool_enabled:
            try:
                reply = await self._generate_with_tools(messages, event, task=task)
            except ToolUnsupported as exc:
                logger.debug("当前模型不支持工具调用，退化为单次生成：%s", exc)
                reply = None
            except Exception as exc:  # noqa: BLE001 - 工具循环失败不能拖死回复
                logger.warning("recall 工具循环失败，退化为单次生成：%s", exc)
                reply = None
            if reply is not None:
                return reply.strip()
        try:
            reply = await self.llm.complete(messages, role="chat", task=task)
        except Exception as exc:  # noqa: BLE001 - LLM 失败不能带死循环
            logger.warning("生成回复失败：%s", exc)
            return ""
        return (reply or "").strip()

    async def _generate_with_tools(
        self,
        messages: list[dict[str, str]],
        event: MessageEvent,
        *,
        task: str = "chat",
    ) -> str | None:
        """最多两轮 recall 工具循环；返回 None 表示应当退化为单次生成。"""
        if not getattr(self.llm, "supports_tools", True):
            raise ToolUnsupported("当前模型已确认不支持工具调用")
        chat_with_tools = getattr(self.llm, "chat_with_tools", None)
        if not callable(chat_with_tools):
            raise ToolUnsupported("当前 LLM 客户端不支持工具调用")
        max_rounds = max(0, int(self.config.memory.recall_tool_max_rounds))
        if max_rounds == 0:
            raise ToolUnsupported("recall 工具循环已关闭")
        working: list[dict[str, Any]] = list(messages)
        for _ in range(max_rounds):
            message = await self._call_with_tools(working, [RECALL_TOOL], role="chat", task=task)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = (message.get("content") or "").strip()
                if content:
                    # 模型没要工具、直接给了回复：这就是一次正常生成，不必再花一次请求
                    return content
                return None
            working.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                working.append(await self._tool_result_message(call, event))
        # 两轮后模型还在要工具：不再给，直接让模型收尾一次
        message = await self._call_with_tools(
            working, [RECALL_TOOL], role="chat", task=task, tool_choice="none"
        )
        return message.get("content") or ""

    @staticmethod
    def _accepts_kwarg(callable_: Any, name: str) -> bool:
        """可调用对象是否接受 ``name`` 这个关键字参数（有 ``**kwargs`` 也算）。"""
        try:
            parameters = inspect.signature(callable_).parameters
        except (TypeError, ValueError):  # 内建/无法取签名：按支持处理，交给调用方兜底
            return True
        return name in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )

    async def _call_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        role: str,
        task: str,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """调用客户端的工具接口；只给不认 ``task`` 参数的实现传它认识的参数。"""
        kwargs: dict[str, Any] = {"role": role, "tool_choice": tool_choice}
        if self._accepts_kwarg(self.llm.chat_with_tools, "task"):
            kwargs["task"] = task
        return await self.llm.chat_with_tools(messages, tools, **kwargs)

    async def _tool_result_message(self, call: dict[str, Any], event: MessageEvent) -> dict[str, Any]:
        """执行一次 recall 工具调用，把结果包装成 tool 消息。"""
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        if name != "recall":
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"没有名为 {name or '（空）'} 的工具。",
            }
        arguments = function.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                args = {}
        else:
            args = arguments if isinstance(arguments, dict) else {}
        try:
            content = await self.recall(
                str(args.get("person_query") or ""),
                str(args.get("topic") or ""),
                str(args.get("time_range") or ""),
                event=event,
            )
        except Exception as exc:  # noqa: BLE001 - 工具失败只把原因告诉模型
            logger.warning("recall 工具执行失败：%s", exc)
            content = f"回忆失败：{exc}"
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def split_reply(self, reply: str) -> list[str]:
        """按句/长度拆成 2~3 条短消息。

        短回复（不超过一条上限）不拆；切句时避开括号、引号、颜文字、URL
        与行内代码，不会从它们中间断开。贪心合并到接近 ``split_part_chars``。
        """
        reply = reply.strip()
        if not reply:
            return []
        max_parts = max(1, self.config.chat.split_max_parts)
        limit = max(8, self.config.chat.split_part_chars)
        if len(reply) <= limit or max_parts == 1:
            return [reply]
        sentences = split_sentences(reply) or [reply]
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

    def typing_delay(self, segments: list[Segment]) -> float:
        """一条消息发出前的等待时间：按字符估算 × 全局倍率，再截到单条上限。

        倍率或上限配成 0 都表示不等待；第一条消息由调用方直接发出，不经过这里。
        """
        raw = estimate_typing_seconds(segments)
        speed = max(0.0, float(self.config.chat.type_speed))
        cap = max(0.0, float(self.config.chat.type_delay_max_seconds))
        return min(raw * speed, cap) if speed > 0 else 0.0

    async def send_reply(
        self,
        event: MessageEvent,
        reply: str | list[Segment] | list[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> None:
        """拆条发送，并把 bot 自己的话写进流水（``processed=True``，不参与记忆抽取）。

        ``reply`` 可以是纯文本（按句拆条），也可以是一组消息段：段列表原样
        交给适配器，整条消息只发一次，不做二次拆分。多条之间有按字数估算
        的打字停顿（第一条立即发）。
        """
        adapter = self.adapters.get(event.platform)
        if adapter is None:
            logger.warning("没有平台 %s 的适配器，无法回复", event.platform)
            return
        strip_actions = bool(getattr(self.config.chat, "strip_action_parens", False))
        if isinstance(reply, str):
            text_reply = strip_action_parens(reply) if strip_actions else reply
            # 模型可能模仿历史格式把转录行前缀一起生成出来，发送前剥掉兜底
            text_reply = strip_transcript_prefix(text_reply)
            parts: list[list[Segment]] = [[Segment.text(part)] for part in self.split_reply(text_reply)]
        else:
            segments = [as_segment(segment) for segment in reply]
            if strip_actions:
                segments = _strip_segments_actions(segments)
            parts = [segments] if segments else []
        if not parts:
            # 拆条后什么都不剩（例如动作括号清理后为空）：不发，也不记发言
            return
        for index, segments in enumerate(parts):
            if index > 0:
                delay = self.typing_delay(segments)
                if delay > 0:
                    await asyncio.sleep(delay)
            text = render_segments(segments)
            try:
                message_id = await adapter.send(event.chat_id, segments, chat_type=event.chat_type)
            except Exception as exc:  # noqa: BLE001 - 一条发失败不影响后续
                logger.warning("发送失败（%s 第 %s 条）：%s", event.platform, index + 1, exc)
                continue
            # 记入流水，供后续上下文使用；标记已处理，不参与记忆抽取。
            # 平台回执的消息 id 一并落库，后续引用解析与自我时间线依赖它。
            self.store.add_raw_message(
                platform=event.platform,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                sender_id=BOT_SENDER_ID,
                sender_name=self.config.chat.bot_name,
                text=text,
                processed=True,
                platform_msg_id="" if message_id is None else str(message_id),
            )
            # 自我时间线：发送成功即记一条 self 事件（记忆 v2 写入侧唯一接线点）
            try:
                self.store.add_self_event(
                    text[: max(1, int(self.config.memory.self_event_max_chars))],
                    source_chat=f"{event.platform}:{event.chat_id}",
                    audience=audience_for_chat(event.chat_type, event.chat_id),
                )
            except Exception as exc:  # noqa: BLE001 - 自我事件失败不能影响发送
                logger.warning("记录自我事件失败：%s", exc)
        # 发言时间交给决策引擎：动量加成与发言后冷却都依赖它
        self.decision_engine.note_reply(self.decision_engine.chat_key(event), time.time() if now is None else now)
        # 情绪侧：刚说过话 = 有人理；群里热闹就兴奋一下
        self.note_emotion_after_reply(event, now=now)

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

