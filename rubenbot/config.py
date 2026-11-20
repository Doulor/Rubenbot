"""配置加载：读取单个 YAML，并给出合理默认值。

配置里支持 ``${ENV_VAR}`` 形式的环境变量展开，方便把 api_key 放在环境变量里。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """递归展开字符串里的 ${VAR}；未定义时替换为空串。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


DEFAULT_CHAT_MODEL = "gpt-4o-mini"

# 预置角色及其回退链：自身没配模型时沿链找，最终都落到 chat。
# 未知角色（插件/新代码自定义的 task）同样回退 chat，不报错。
ROLE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "chat": (),
    "extract": ("chat",),
    "consolidate": ("chat",),
    "fast": ("chat",),
    "summary": ("chat",),
    "vision": ("chat",),
    # 记忆 v2：分寸审查没配就跟随 chat；画像提炼沿用归纳模型
    "gate": ("chat",),
    "distill": ("consolidate", "chat"),
}

# 预置角色表；`models` 里写别的角色名也允许，只是回退链只有 chat
MODEL_ROLES: tuple[str, ...] = tuple(ROLE_FALLBACKS)


@dataclass
class LLMConfig:
    """OpenAI 兼容接口配置。

    角色表有两代写法，同时兼容：

    - ``models``：可扩展映射 ``{角色: 模型名}``，例如 ``fast: 小模型``；
    - ``chat_model`` / ``extract_model`` / ``consolidate_model``：旧字段。

    解析顺序是「精确角色 → 回退链 → chat → 默认模型」；``models`` 优先于
    旧字段。旧配置不写 ``models`` 时行为与以前完全一致。
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    chat_model: str = DEFAULT_CHAT_MODEL
    extract_model: str | None = None
    consolidate_model: str | None = None
    # 分寸审查（gate）用的模型；留空跟随 chat
    gate_model: str | None = None
    # 全局温度供 summary/fast 等非聊天角色使用；chat 温度留空时沿用它。
    temperature: float = 0.8
    chat_temperature: float | None = None
    # 聊天单独的轻度重复惩罚；其它角色不默认带此参数。
    chat_frequency_penalty: float = 0.3
    timeout: float = 60.0
    max_retries: int = 2
    # 角色 → 模型名。预置角色见 MODEL_ROLES，也可写自定义角色。
    # 放在末尾是为了不改变旧字段的位置参数顺序。
    models: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把 ``models`` 归一化成「非空字符串 → 字符串」的字典。"""
        raw = self.models if isinstance(self.models, dict) else {}
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            name = str(key).strip()
            if not name or value is None:
                continue
            model = str(value).strip()
            if model:
                cleaned[name] = model
        self.models = cleaned

    def _explicit_model(self, role: str) -> str | None:
        """只看这个角色自己配没配，不沿着回退链走。"""
        mapped = self.models.get(role)
        if mapped:
            return mapped
        # 旧字段：保持完全兼容
        if role == "extract":
            return self.extract_model or None
        if role == "consolidate":
            return self.consolidate_model or None
        if role == "gate":
            return self.gate_model or None
        if role == "chat":
            return self.chat_model or None
        return None

    def _resolve_model(self, role: str, seen: set[str]) -> str | None:
        if role in seen:
            return None
        seen.add(role)
        explicit = self._explicit_model(role)
        if explicit:
            return explicit
        # 预置角色按表回退；未知角色一律回退 chat
        for fallback in ROLE_FALLBACKS.get(role, ("chat",)):
            resolved = self._resolve_model(fallback, seen)
            if resolved:
                return resolved
        return None

    def model_for(self, role: str = "chat") -> str:
        """按用途取模型：精确角色 → 回退链 → chat → 默认模型。

        记忆 v2 的 ``gate`` 走 ``gate_model``；``distill`` 未配置时沿用
        ``consolidate`` 的回退链（归纳模型 → chat）。
        """
        name = str(role or "chat").strip() or "chat"
        return self._resolve_model(name, set()) or self.chat_model or DEFAULT_CHAT_MODEL


@dataclass
class RollingSummaryConfig:
    """会话滚动摘要参数（按会话压缩早期对话历史）。

    - ``enabled``：总开关；关闭时既不生成摘要，也不用摘要拼上下文。
    - ``trigger_after``：covered_until 之后未摘要的原文消息达到多少条触发一次压缩。
    - ``keep_recent``：始终保留在上下文里的最新原文条数，不参与压缩。
    - ``max_chars``：摘要总长上限；更新时要求模型在上限内取舍，代码兜底截断。
    """

    enabled: bool = True
    trigger_after: int = 60
    keep_recent: int = 30
    max_chars: int = 800


@dataclass
class MemoryConfig:
    """记忆层参数。"""

    extract_every_n: int = 30
    extract_silence_seconds: int = 300
    extract_min_pending: int = 4
    extract_window: int = 80
    inject_limit: int = 20
    dedupe_threshold: float = 0.8
    consolidate_new_threshold: int = 50
    consolidate_interval_hours: float = 24.0
    consolidate_max_memories: int = 150
    # 每轮归纳最多移除的比例，以及至少允许的条数
    consolidate_remove_ratio: float = 0.2
    consolidate_remove_min: int = 4
    raw_keep_per_chat: int = 500

    # ---- 记忆 v2（画像 / 时间线 / 自我）----
    # 画像提炼：新增事件+事实达到阈值、或距上次提炼超过间隔时触发
    distill_new_threshold: int = 20
    distill_interval_hours: float = 24.0
    distill_max_events: int = 120
    distill_max_facts: int = 80
    # 近况（status）过期时间由模型估计天数，代码夹到这个区间
    status_expiry_min_days: int = 1
    status_expiry_max_days: int = 90
    # 时间线压缩：7 天前按人按周压成 digest；90 天前只留高显著度
    timeline_compress_after_days: float = 7.0
    timeline_prune_after_days: float = 90.0
    timeline_prune_keep_salience: float = 0.7
    # bot 自己发的每条消息记进自我时间线的截断长度
    self_event_max_chars: int = 120

    # ---- 记忆 v2 读取侧（索引 / 预拉取 / 跨会话 / 分寸审查）----
    # 群成员索引：只收近 N 天在本群活跃过的人，最多多少人，缓存多久失效
    index_active_days: float = 30.0
    index_max_members: int = 40
    index_ttl_seconds: float = 600.0
    # 索引里「一句话身份」的截断长度
    index_identity_chars: int = 40
    # 一轮里最多拉几个被提及者的核心画像
    mentioned_max_people: int = 3
    # 说话人核心画像的最大字符数（画像核心不参与预算裁剪）
    profile_inject_max_chars: int = 600
    # 本轮记忆注入的总 token 预算；超了先砍事实、再砍时间线，画像核心不动
    inject_budget_tokens: int = 1200
    # 每个被提及者最多带几条相关时间线片段
    timeline_hit_limit: int = 6
    # 私聊跨会话摘要回看的小时数
    cross_session_hours: float = 24.0
    # 自我注入：回看本会话里 bot 自己最近说过/做过的事
    self_inject_hours: float = 24.0
    self_inject_limit: int = 3
    # 分寸审查：模型裁定的置信度阈值，低于它不放行
    gate_confidence_threshold: float = 0.6
    # 高敏感跨场合项是否顺带进待问队列
    gate_ask_high_sensitivity: bool = True
    # recall 工具循环（模型支持 OpenAI 工具调用时生效）
    recall_tool_enabled: bool = True
    recall_tool_max_rounds: int = 2

    # ---- 会话滚动摘要（只压缩本会话的对话历史，与跨会话记忆无关）----
    rolling_summary: RollingSummaryConfig = field(default_factory=RollingSummaryConfig)


@dataclass
class ChatConfig:
    """对话与发送参数。"""

    history_size: int = 30
    split_max_parts: int = 3
    split_part_chars: int = 60
    # 打字节奏：中文字 0.3s/字、其他字符 0.15s/字，每条另加 0.3s 回车时间；
    # type_speed 是全局倍率（0 表示两条之间不停顿），单条上限防止长文等太久
    type_speed: float = 1.0
    type_delay_max_seconds: float = 4.0
    # 发送前是否移除括号里的动作描写（如「（笑）」）；默认关闭
    strip_action_parens: bool = False
    bot_name: str = "Ruben"
    history_ttl_hours: float = 12.0
    # 入站去重：同一平台消息 id 在这个时间窗内重复到达则丢弃；0 表示不去重
    dedupe_ttl_seconds: float = 300.0


@dataclass
class ReminderConfig:
    """提醒工具（``set_reminder``）参数。

    提醒只回应用户明确请求：模型在对话里自己决定调工具，核心不开任何
    自动提醒。每会话待执行提醒有上限，防止模型被反复要求时无限堆积。
    """

    enabled: bool = True
    # 每个会话（platform:chat_id）最多同时挂多少个待执行提醒
    max_pending_per_chat: int = 5
    # 单条提醒最多延迟多久（分钟）；超过的请求直接拒绝，避免误设远期任务
    max_delay_minutes: int = 60 * 24 * 30
    # 提醒正文的截断长度
    max_text_chars: int = 200


@dataclass
class ProactiveConfig:
    """冷场开口（主动触达）参数。默认关闭，宁可漏、不可扰。

    ``base`` / ``threshold`` 是「主动开口」场景的意愿评分参数：基础分给得
    很低、阈值附近有随机扰动，所以大多数时候评估不通过就不开口。``relevance``
    加分来自群聊最近消息与记忆的检索命中（有话题积累才更容易开口）。
    """

    enabled: bool = False
    # 群里安静多久才考虑开口（小时）
    idle_hours: float = 6.0
    # 安静时段，默认深夜到早上不开口；格式同 decision.groups 的 quiet_hours
    quiet_hours: list[str] = field(default_factory=lambda: ["23:00-08:00"])
    # 每群每日最多主动开口几次
    daily_limit: int = 1
    # 一次评估不开口后，隔多久再评估（分钟），避免每 10 秒都花一次生成
    cooldown_minutes: float = 60.0
    # 意愿评分（主动开口场景）
    base: float = 0.08
    threshold: float = 0.15
    threshold_noise: float = 0.10
    threshold_noise_band: float = 0.12
    relevance_bonus: float = 0.10
    relevance_scale: float = 8.0


@dataclass
class GroupDecisionConfig:
    """单个群的决策覆盖项；未写的字段沿用全局默认。"""

    activity_multiplier: float = 1.0
    base: float | None = None
    quiet_hours: list[str] = field(default_factory=list)


@dataclass
class DecisionConfig:
    """回复意愿系统参数（见 SPEC「回复决策」）。"""

    # 消息合并窗口：普通消息攒 4 秒；硬触发走短窗口，0 表示立即处理
    merge_window_seconds: float = 4.0
    hard_merge_window_seconds: float = 1.0
    # 整批上限：从首条消息算起最多等这么久就放出；0 表示不设上限
    batch_max_seconds: float = 10.0
    # @/回复 bot 的批次截止时间不晚于「首条 + urgent_seconds」；0 表示不启用
    urgent_seconds: float = 2.0
    # 叫到这些名字/别名视为硬触发；为空时使用 chat.bot_name
    aliases: list[str] = field(default_factory=list)

    # 意愿评分：基础值 + 动量 + 相关性 - 占比 - 冷却
    base: float = 0.10
    threshold: float = 0.85
    threshold_noise: float = 0.10
    threshold_noise_band: float = 0.20
    momentum_bonus: float = 1.00
    momentum_half_life_seconds: float = 120.0
    relevance_bonus: float = 0.60
    relevance_scale: float = 8.0
    # 发言占比：看最近 ratio_window 条消息（含 bot 自己的）
    ratio_window: int = 30
    ratio_threshold: float = 0.20
    ratio_silence: float = 0.60
    ratio_penalty: float = 1.50
    # 发言后冷却（软抑制）
    cooldown_seconds: float = 30.0
    cooldown_penalty: float = 0.60
    # 活人感节奏闸门：普通消息在不应期内直接不触发；窗口内主动发言封顶
    exclude_bot_momentum: bool = True
    refractory_enabled: bool = True
    refractory_seconds: float = 45.0
    proactive_window_enabled: bool = True
    proactive_window_seconds: float = 300.0
    proactive_window_limit: int = 3
    # 模型输出该标记时静默丢弃
    veto_marker: str = "[不回复]"
    # 每群覆盖：群 id -> 配置
    groups: dict[str, GroupDecisionConfig] = field(default_factory=dict)


@dataclass
class EmotionConfig:
    """情绪与好感度参数（确定性规则，全部幅度集中在这里）。

    设计原则见 SPEC「情绪与好感度」：状态变化只由代码按规则计算，模型
    不参与打分；变化写 ``affinity_log`` 可追溯；生成侧只注入一句状态描述，
    另外对意愿阈值做小幅微调。
    """

    # 总开关：关掉后不记录、不注入、不调阈值，行为回到没有这套系统之前
    enabled: bool = True

    # ---- 好感度 ----
    affinity_baseline: float = 50.0
    affinity_min: float = 0.0
    affinity_max: float = 100.0
    # 与 bot 对话一轮的加分，以及每人每日（本地时间）累计上限
    chat_delta: float = 0.2
    chat_daily_cap: float = 2.0
    # 被夸/感谢、被骂/恶语（都要先确认是在对 bot 说）
    praise_delta: float = 1.0
    insult_delta: float = -2.0
    # 每周向基线回归的比例（惰性计算，见 EmotionEngine.current_affinity）
    decay_weekly: float = 0.05

    # ---- 情绪：事件 → 状态 ----
    praise_mood: str = "开心"
    praise_intensity: float = 0.6
    insult_mood: str = "委屈"
    insult_intensity: float = 0.6
    # 群里热闹且 bot 参与多 → 兴奋
    excitement_mood: str = "兴奋"
    excitement_intensity: float = 0.4
    excitement_window_seconds: float = 600.0
    excitement_min_messages: int = 6
    excitement_min_bot_messages: int = 2
    # 长时间没人理 → 无聊
    boredom_mood: str = "无聊"
    boredom_intensity: float = 0.3
    boredom_after_seconds: float = 3 * 3600.0
    # 情绪强度向平静衰减的半衰期
    half_life_seconds: float = 3600.0
    # 强度低于这个值就视为平静（不再注入、不再调阈值）
    min_intensity: float = 0.05

    # 意愿阈值微调：正数更难开口，负数更容易开口（按当前心情取）
    threshold_adjust: dict[str, float] = field(
        default_factory=lambda: {
            "开心": -0.05,
            "兴奋": -0.05,
            "无聊": -0.03,
            "委屈": 0.05,
            "低落": 0.05,
        }
    )

    def adjust_for(self, mood: str) -> float:
        """某心情对应的阈值微调量；没配置就是 0。"""
        value = self.threshold_adjust.get(mood, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class TwoStageConfig:
    """两段式生成：先「想」再「说」。"""

    # 默认允许闲聊碎片省一次 fast 请求；开启则所有档位都走完整两段。
    enabled: bool = False


@dataclass
class GenerationConfig:
    """回复生成的策略配置。"""

    two_stage: TwoStageConfig = field(default_factory=TwoStageConfig)


# 联网搜索支持的渠道；顺序只影响默认尝试顺序，不代表优先级
SEARCH_PROVIDERS: tuple[str, ...] = ("tavily", "deepseek")
# 用户拍板：Tavily 当主力、DeepSeek 当备胎
DEFAULT_SEARCH_ORDER: tuple[str, ...] = ("tavily", "deepseek")


@dataclass
class SearchChannelConfig:
    """单个搜索渠道的配置；目前只有密钥。"""

    api_key: str = ""

    def __post_init__(self) -> None:
        self.api_key = str(self.api_key or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def _normalize_order(raw: object) -> list[str]:
    """把 ``order`` 归一化成已知渠道名的列表。

    接受列表或逗号分隔字符串；未知渠道名忽略并打一行日志，重复项去重。
    一个都不剩时回落到默认顺序（缺省语义），不让配置写错就搜不了。
    """
    if isinstance(raw, str):
        items: list[object] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []
    order: list[str] = []
    for item in items:
        name = str(item or "").strip().lower()
        if not name:
            continue
        if name not in SEARCH_PROVIDERS:
            logger.warning("未知的搜索渠道 %r，已忽略", name)
            continue
        if name not in order:
            order.append(name)
    return order or list(DEFAULT_SEARCH_ORDER)


@dataclass
class SearchConfig:
    """联网搜索（LLM 工具 ``web_search``）的配置。

    搜索渠道改成**有序回退链**：``order`` 里的渠道按顺序尝试，没配 key 的
    自动跳过，抛错/超时/空结果就顺延下一个，第一个有结果的胜出。渠道目前
    有 ``tavily`` 与 ``deepseek``，各自在 ``tavily.api_key`` /
    ``deepseek.api_key`` 配密钥；支持 ``${ENV_VAR}``（加载时已统一展开）。
    整条链一个 key 都没有时工具不注册，模型看不见它。
    """

    order: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_ORDER))
    tavily: SearchChannelConfig = field(default_factory=SearchChannelConfig)
    deepseek: SearchChannelConfig = field(default_factory=SearchChannelConfig)
    timeout_seconds: float = 10.0
    # 每个会话两次真实搜索之间的冷却；整条链共用，冷却内不发任何请求
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.order = _normalize_order(self.order)
        for name in SEARCH_PROVIDERS:
            value = getattr(self, name)
            if isinstance(value, dict):
                value = SearchChannelConfig(**{k: v for k, v in value.items() if _has(SearchChannelConfig, k)})
            elif not isinstance(value, SearchChannelConfig):
                value = SearchChannelConfig()
            setattr(self, name, value)
        self.timeout_seconds = max(0.1, _as_float(self.timeout_seconds, 10.0))
        self.cooldown_seconds = max(0.0, _as_float(self.cooldown_seconds, 30.0))

    def channel(self, name: str) -> SearchChannelConfig:
        """按渠道名取配置；未知渠道返回空配置（调用方应先过滤）。"""
        value = getattr(self, name, None)
        return value if isinstance(value, SearchChannelConfig) else SearchChannelConfig()

    @property
    def configured(self) -> bool:
        """链上至少有一个渠道配了 key 才算可用。"""
        return any(self.channel(name).configured for name in self.order)


# 语音转写（STT）与语音合成（TTS）的可选后端
# qq_native 走 SnowLuma 的 fetch_ptt_text（QQ 官方转写），仅该适配器可用
STT_PROVIDERS: tuple[str, ...] = ("qq_native", "siliconflow", "openai_compatible")
TTS_PROVIDERS: tuple[str, ...] = ("custom", "siliconflow")
# tts_mode：off 从不发语音；reply_voice 对方发语音时用语音回；
# on_request 在 reply_voice 基础上，被明确要求用语音说时也发
TTS_MODES: tuple[str, ...] = ("off", "reply_voice", "on_request")


@dataclass
class VoiceConfig:
    """语音双向：入站语音条转写（STT）与出站语音回复（TTS）。

    STT 没配任何可用后端时语音消息只记 ``[语音]`` 占位，不发起任何请求。
    ``qq_native`` 走 SnowLuma 的 ``fetch_ptt_text``（QQ 官方转写），只对
    SnowLuma 来源生效；失败/空文本时按 :attr:`stt_providers` 的顺序顺延到
    下一个已配置的 HTTP 后端，全失败才留占位。

    TTS 默认 ``off``；``custom``（默认后端）是任意 OpenAI 兼容的
    ``/audio/speech`` 接口，缺 base_url 或 model 视为未配置，降级发文字。
    """

    # ---- STT ----
    stt_provider: str = "siliconflow"
    stt_api_key: str = ""
    # openai_compatible 用：任意 OpenAI 兼容接口的 base_url（如 https://host/v1）
    stt_base_url: str = ""
    stt_model: str = "FunAudioLLM/SenseVoiceSmall"
    stt_timeout_seconds: float = 30.0
    # 单条音频上限：超过任一上限就不转写，正文只留 [语音] 占位
    stt_max_seconds: float = 60.0
    stt_max_bytes: int = 5 * 1024 * 1024

    # ---- TTS ----
    tts_mode: str = "off"
    tts_provider: str = "custom"
    # custom：OpenAI 兼容 /audio/speech 的 base_url（如 https://host/v1）；
    # siliconflow：固定官方地址，用 tts_siliconflow_voice 选音色
    tts_base_url: str = ""
    tts_voice: str = ""
    tts_model: str = ""
    tts_siliconflow_voice: str = "FunAudioLLM/CosyVoice2-0.5B:alex"
    # 留空时 siliconflow TTS 复用 stt_api_key（同一账号）
    tts_api_key: str = ""
    # 正文超过这么多字就不合成语音（太长没人听），照常发文字
    tts_max_chars: int = 60
    tts_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        provider = str(self.stt_provider or "").strip().lower()
        self.stt_provider = provider if provider in STT_PROVIDERS else "siliconflow"
        tts_provider = str(self.tts_provider or "").strip().lower()
        self.tts_provider = tts_provider if tts_provider in TTS_PROVIDERS else "custom"
        mode = str(self.tts_mode or "").strip().lower()
        self.tts_mode = mode if mode in TTS_MODES else "off"
        for key in (
            "stt_api_key",
            "stt_base_url",
            "stt_model",
            "tts_api_key",
            "tts_base_url",
            "tts_voice",
            "tts_model",
            "tts_siliconflow_voice",
        ):
            setattr(self, key, str(getattr(self, key) or "").strip())
        self.stt_timeout_seconds = max(0.1, _as_float(self.stt_timeout_seconds, 30.0))
        self.tts_timeout_seconds = max(0.1, _as_float(self.tts_timeout_seconds, 30.0))
        self.stt_max_seconds = max(0.0, _as_float(self.stt_max_seconds, 60.0))
        self.stt_max_bytes = max(0, _as_int(self.stt_max_bytes, 5 * 1024 * 1024))
        self.tts_max_chars = max(1, _as_int(self.tts_max_chars, 60))

    @property
    def stt_providers(self) -> tuple[str, ...]:
        """转写尝试顺序：首选后端在前，后面是其余已配置的 HTTP 后端。

        ``qq_native`` 不是通用后端，只有被选为首选时才进链；siliconflow 看
        ``stt_api_key``，openai_compatible 看 ``stt_base_url``，没配的跳过。
        配错/没配时返回空元组，调用方直接留 ``[语音]`` 占位。
        """
        order: list[str] = []
        for name in (self.stt_provider, "siliconflow", "openai_compatible", "qq_native"):
            if name in order:
                continue
            if name == "qq_native":
                if self.stt_provider != "qq_native":
                    continue
            elif name == "siliconflow":
                if not self.stt_api_key:
                    continue
            elif name == "openai_compatible":
                if not self.stt_base_url:
                    continue
            order.append(name)
        return tuple(order)

    @property
    def stt_configured(self) -> bool:
        """STT 是否可用：链上至少有一个后端（qq_native 只要求被选中）。"""
        return bool(self.stt_providers)

    @property
    def tts_enabled(self) -> bool:
        return self.tts_mode != "off"

    @property
    def tts_configured(self) -> bool:
        """TTS 是否可用：custom 要 base_url + model，siliconflow 要有 key。"""
        if not self.tts_enabled:
            return False
        if self.tts_provider == "siliconflow":
            return bool(self.tts_api_key or self.stt_api_key)
        return bool(self.tts_base_url and self.tts_model)


@dataclass
class StickerConfig:
    """表情包行为与表情库位置。

    ``enabled`` 是总开关，默认关闭以保持升级后的零动作行为；只有打开后，
    ``collect`` 才会收集群聊表情，回复后才会按概率补发表情。
    """

    enabled: bool = False
    collect: bool = False
    send_probability: float = 0.15
    cooldown_seconds: float = 300.0
    # 接梗/短附和时，只发一张表情包的概率；不满足条件时回落文字。
    nonverbal_probability: float = 0.30
    dir: str = "data/stickers"

    def __post_init__(self) -> None:
        self.enabled = self.enabled is True
        self.collect = self.collect is True
        if isinstance(self.send_probability, bool) or not isinstance(self.send_probability, (int, float)):
            self.send_probability = 0.15
        self.send_probability = min(1.0, max(0.0, float(self.send_probability)))
        if isinstance(self.nonverbal_probability, bool) or not isinstance(self.nonverbal_probability, (int, float)):
            self.nonverbal_probability = 0.30
        self.nonverbal_probability = min(1.0, max(0.0, float(self.nonverbal_probability)))
        if isinstance(self.cooldown_seconds, bool) or not isinstance(self.cooldown_seconds, (int, float)):
            self.cooldown_seconds = 300.0
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds))
        self.dir = str(self.dir or "").strip() or "data/stickers"


@dataclass
class PluginsConfig:
    """插件目录与启停。

    插件采用**显式启用**制：``enabled`` 里没列出的插件都不会加载，
    未配置或空列表表示一个插件都不启用（默认全关）。WebUI 的插件开关
    会把选择写回这个列表。
    """

    dir: str = "plugins"
    enabled: list[str] = field(default_factory=list)
    command_prefix: str = "/"

    def __post_init__(self) -> None:
        """把 ``enabled`` 归一化成去重、去空白的字符串列表。

        兼容旧配置里写成 ``null`` 或单个字符串的写法：``None`` 按空列表
        处理（即不启用任何插件），不会意外变成「全部启用」。
        """
        raw = self.enabled
        if raw is None or raw == "":
            raw = []
        elif isinstance(raw, str):
            raw = [raw]
        cleaned: list[str] = []
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if item is None:
                    continue
                name = str(item).strip()
                if name and name not in cleaned:
                    cleaned.append(name)
        self.enabled = cleaned


@dataclass
class MarketConfig:
    """插件市场数据源。

    ``index_url`` 指向索引仓库的 ``plugins.json``（raw 地址）；留空或拉取
    失败时回退到随框架发布的内置官方索引（即插件目录里的官方插件）。
    改动在 WebUI 侧按请求读取，无需重启。
    """

    index_url: str = ""

    def __post_init__(self) -> None:
        self.index_url = str(self.index_url or "").strip()


@dataclass
class WebUIConfig:
    """WebUI 服务配置（第二阶段）。

    默认只绑 127.0.0.1；绑到非本地地址时必须设置 ``password``，否则拒绝启动。
    口令可以用 ``${ENV_VAR}`` 从环境变量读，避免写进配置文件。
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""
    # 登录会话有效期（小时）
    session_hours: float = 72.0


@dataclass
class TelegramConfig:
    """``adapters.telegram`` 配置段（Telegram Bot API 长轮询）。

    ``bot_token`` 与 llm 的 api_key 一样支持 ``${ENV_VAR}``（加载时已统一展开）。
    ``allowed_chat_ids`` 为空表示不限制会话；填了则只处理这些 chat_id 的消息。
    ``offset_file`` 落盘保存 getUpdates 的 offset，重启后不重复消费；留空表示不落盘。
    """

    bot_token: str = ""
    poll_timeout: float = 30.0
    allowed_chat_ids: list[str] = field(default_factory=list)
    offset_file: str = "data/telegram_offset.json"

    @classmethod
    def from_options(cls, options: Any) -> "TelegramConfig":
        if not isinstance(options, dict):
            return cls()
        token = options.get("bot_token")
        poll_timeout = _as_float(options.get("poll_timeout"), 30.0)
        allowed = options.get("allowed_chat_ids")
        if allowed is None or allowed == "":
            allowed_ids: list[str] = []
        elif isinstance(allowed, (str, int)):
            allowed_ids = [str(allowed)]
        elif isinstance(allowed, (list, tuple, set)):
            allowed_ids = [str(item) for item in allowed if str(item) != ""]
        else:
            allowed_ids = []
        offset_file = options.get("offset_file", "data/telegram_offset.json")
        return cls(
            bot_token=str(token or "").strip(),
            poll_timeout=max(0.0, poll_timeout),
            allowed_chat_ids=allowed_ids,
            offset_file="" if offset_file is None else str(offset_file),
        )


@dataclass
class QQOfficialConfig:
    """QQ 官方机器人（q.qq.com 开放平台）配置。

    ``client_secret`` 建议写 ``${ENV_VAR}`` 从环境变量读，避免明文进仓库。
    ``sandbox`` 打开后走沙箱域名 ``sandbox.api.sgroup.qq.com``，用于提审前联调。
    """

    app_id: str = ""
    client_secret: str = ""
    sandbox: bool = False
    # 入站图片附件是否下载（转 base64 放进内部 image 段）
    download_images: bool = True

    @property
    def configured(self) -> bool:
        """app_id 与 client_secret 都填了才算可用。"""
        return bool(self.app_id and self.client_secret)

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> "QQOfficialConfig":
        """从 ``adapters.qq_official`` 配置段解析；缺省/类型不对的字段回落默认值。

        ``${ENV_VAR}`` 展开在 :meth:`Config.load` 阶段已经做过，这里不用重复。
        """
        if not isinstance(options, dict):
            return cls()
        return cls(
            app_id=str(options.get("app_id") or ""),
            client_secret=str(options.get("client_secret") or ""),
            sandbox=options.get("sandbox") is True,
            download_images=options.get("download_images") is not False,
        )


def is_loopback_host(host: str) -> bool:
    """判断绑定地址是否只对本机可见。

    ``localhost``、``::1``、以及整个 127.0.0.0/8 都算本地；空字符串按本地处理。
    """
    value = (host or "").strip()
    if value in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass
class GatewayConfig:
    """通用适配器网关 + 事件通道（感知与行动）的配置。

    默认全关：网关不开时模型看不见 ``adapter_actions`` / ``adapter_call``，
    事件通道也只在 ``judge_events`` 打开时才做快判（落库照常进行，便于观察）。
    ``quiet_hours`` 与 ``proactive.quiet_hours`` 同格式：``HH:MM-HH:MM``。
    """

    # 总开关：关时网关工具不注册、目录不可见
    enabled: bool = False
    # 允许使用网关的会话（platform:chat_id 或裸 chat_id）；私聊主人会话恒可
    enabled_conversations: list[str] = field(default_factory=list)
    # 事件判断总开关（快判）；关时事件只落库
    judge_events: bool = False
    # sensitive 动作默认禁调；显式打开才允许，且审计标红
    allow_sensitive: bool = False
    # 安静时段：非必判事件只挂起记录，必判事件也只记录 + 挂待办
    quiet_hours: str = "23:00-08:00"
    # 每会话每天主动类动作上限
    daily_budget_per_conversation: int = 20
    # 全局每天动作上限（熔断）
    global_daily_cap: int = 200

    def __post_init__(self) -> None:
        cleaned: list[str] = []
        for item in self.enabled_conversations or []:
            name = str(item or "").strip()
            if name and name not in cleaned:
                cleaned.append(name)
        self.enabled_conversations = cleaned
        self.quiet_hours = str(self.quiet_hours or "").strip()


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    stickers: StickerConfig = field(default_factory=StickerConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    reminder: ReminderConfig = field(default_factory=ReminderConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    persona_path: str = "data/SOUL.md"
    database_path: str = "data/rubenbot.db"
    adapters: dict[str, Any] = field(default_factory=dict)
    # 后台归纳检查间隔（分钟）
    background_check_minutes: float = 5.0
    log_level: str = "INFO"

    # ---- 导出 ----

    @property
    def enabled_adapters(self) -> list[str]:
        """返回配置中启用（值不为 null/false）的适配器名；``{}`` 也算启用。"""
        return [name for name, opts in self.adapters.items() if opts is not None and opts is not False]

    def adapter_options(self, name: str) -> dict[str, Any]:
        """某个适配器的配置段；未配置或不是 dict 时返回空 dict。"""
        options = self.adapters.get(name)
        return options if isinstance(options, dict) else {}

    def qq_official_options(self) -> QQOfficialConfig:
        """``adapters.qq_official`` 段的强类型视图。"""
        return QQOfficialConfig.from_options(self.adapter_options("qq_official"))

    def adapter_capability_enabled(self, adapter_name: str, config_key: str, default: bool = True) -> bool:
        """读取 ``adapters.<adapter>.capabilities.<config_key>`` 开关。

        未写时用 ``default``；只接受布尔值，写成字符串等一律视为关闭，避免
        ``"false"`` 这种意外真值。
        """
        caps = self.adapter_options(adapter_name).get("capabilities")
        if not isinstance(caps, dict) or config_key not in caps:
            return default
        return caps[config_key] is True

    def adapter_capabilities(self, adapter_name: str) -> dict[str, bool]:
        """某个适配器配置里的能力开关表（原样返回布尔化后的副本）。"""
        caps = self.adapter_options(adapter_name).get("capabilities")
        if not isinstance(caps, dict):
            return {}
        return {str(key): value is True for key, value in caps.items()}

    def telegram_config(self) -> TelegramConfig:
        """解析 ``adapters.telegram`` 段；未配置时返回默认值。"""
        return TelegramConfig.from_options(self.adapter_options("telegram"))

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        """从 YAML 加载配置；文件不存在时返回全默认值。"""
        file = Path(path)
        if not file.exists():
            return cls()
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"配置文件格式错误：{file}")
        return cls.from_raw(data)

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "Config":
        """从已解析的 YAML 数据构造配置（展开 ``${ENV}``）。

        WebUI 结构化保存时先用它校验合并后的字典，通过了才写盘。
        """
        if not isinstance(data, dict):
            raise ValueError("配置的顶层必须是键值对（YAML mapping）")
        return cls.from_dict(_expand_env(data))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        llm = LLMConfig(**{k: v for k, v in (data.get("llm") or {}).items() if _has(LLMConfig, k)})
        memory = _load_memory(data.get("memory") or {})
        search = _load_search(data.get("search") or {})
        voice = VoiceConfig(
            **{k: v for k, v in (data.get("voice") or {}).items() if _has(VoiceConfig, k)}
        )
        chat = ChatConfig(**{k: v for k, v in (data.get("chat") or {}).items() if _has(ChatConfig, k)})
        decision = _load_decision(data.get("decision") or {})
        generation = _load_generation(data.get("generation") or {})
        emotion = _load_emotion(data.get("emotion") or {})
        plugins = PluginsConfig(**{k: v for k, v in (data.get("plugins") or {}).items() if _has(PluginsConfig, k)})
        stickers = StickerConfig(
            **{k: v for k, v in (data.get("stickers") or {}).items() if _has(StickerConfig, k)}
        )
        market = MarketConfig(**{k: v for k, v in (data.get("market") or {}).items() if _has(MarketConfig, k)})
        webui = WebUIConfig(**{k: v for k, v in (data.get("webui") or {}).items() if _has(WebUIConfig, k)})
        reminder = _load_reminder(data.get("reminder") or {})
        proactive = _load_proactive(data.get("proactive") or {})
        gateway = _load_gateway(data.get("gateway") or {})
        persona = data.get("persona") or {}
        if isinstance(persona, str):
            persona = {"path": persona}
        basics = {
            "persona_path": persona.get("path", "data/SOUL.md"),
            "database_path": data.get("database_path", "data/rubenbot.db"),
            "adapters": data.get("adapters") or {},
            "background_check_minutes": data.get("background_check_minutes", 5.0),
            "log_level": data.get("log_level", "INFO"),
        }
        return cls(
            llm=llm,
            memory=memory,
            search=search,
            voice=voice,
            chat=chat,
            decision=decision,
            generation=generation,
            emotion=emotion,
            plugins=plugins,
            stickers=stickers,
            market=market,
            webui=webui,
            reminder=reminder,
            proactive=proactive,
            gateway=gateway,
            **basics,
        )


def _load_reminder(data: dict[str, Any]) -> ReminderConfig:
    """解析 reminder 段：宽松取值，写错类型回落默认，不让配置问题挡住启动。"""
    if not isinstance(data, dict):
        return ReminderConfig()
    default = ReminderConfig()
    return ReminderConfig(
        enabled=data.get("enabled") is not False,
        max_pending_per_chat=_as_int(
            data.get("max_pending_per_chat"), default.max_pending_per_chat, minimum=0
        ),
        max_delay_minutes=_as_int(
            data.get("max_delay_minutes"), default.max_delay_minutes, minimum=1
        ),
        max_text_chars=_as_int(data.get("max_text_chars"), default.max_text_chars, minimum=20),
    )


def _load_gateway(data: dict[str, Any]) -> GatewayConfig:
    """解析 gateway 段：宽松取值，写错类型回落默认，不让配置问题挡住启动。"""
    if not isinstance(data, dict):
        return GatewayConfig()
    default = GatewayConfig()
    conversations = data.get("enabled_conversations", default.enabled_conversations)
    if isinstance(conversations, (str, int)):
        conversations = [conversations]
    if not isinstance(conversations, (list, tuple)):
        conversations = []
    return GatewayConfig(
        enabled=data.get("enabled") is True,
        enabled_conversations=[str(item) for item in conversations if str(item).strip()],
        judge_events=data.get("judge_events") is True,
        allow_sensitive=data.get("allow_sensitive") is True,
        quiet_hours=str(data.get("quiet_hours", default.quiet_hours) or "").strip(),
        daily_budget_per_conversation=_as_int(
            data.get("daily_budget_per_conversation"),
            default.daily_budget_per_conversation,
            minimum=0,
        ),
        global_daily_cap=_as_int(
            data.get("global_daily_cap"), default.global_daily_cap, minimum=0
        ),
    )


def _load_proactive(data: dict[str, Any]) -> ProactiveConfig:
    """解析 proactive 段：宽松取值，写错类型回落默认，不让配置问题挡住启动。"""
    if not isinstance(data, dict):
        return ProactiveConfig()
    default = ProactiveConfig()
    quiet = data.get("quiet_hours", default.quiet_hours)
    if isinstance(quiet, str):
        quiet = [quiet]
    if not isinstance(quiet, (list, tuple)):
        quiet = default.quiet_hours
    return ProactiveConfig(
        enabled=data.get("enabled") is True,
        idle_hours=max(0.1, _as_float(data.get("idle_hours"), default.idle_hours)),
        quiet_hours=[str(item) for item in quiet],
        daily_limit=_as_int(data.get("daily_limit"), default.daily_limit, minimum=0),
        cooldown_minutes=max(0.0, _as_float(data.get("cooldown_minutes"), default.cooldown_minutes)),
        base=_as_float(data.get("base"), default.base),
        threshold=_as_float(data.get("threshold"), default.threshold),
        threshold_noise=_as_float(data.get("threshold_noise"), default.threshold_noise),
        threshold_noise_band=_as_float(data.get("threshold_noise_band"), default.threshold_noise_band),
        relevance_bonus=_as_float(data.get("relevance_bonus"), default.relevance_bonus),
        relevance_scale=max(0.1, _as_float(data.get("relevance_scale"), default.relevance_scale)),
    )


def _load_memory(data: dict[str, Any]) -> MemoryConfig:
    """解析 memory 段；``rolling_summary`` 单独解析成嵌套配置。

    滚动摘要缺省开启（``enabled: true``），旧配置不写这个段时行为与新版
    默认一致；写错类型的字段回落默认值，不让配置问题挡住启动。
    """
    if not isinstance(data, dict):
        return MemoryConfig()
    plain = {k: v for k, v in data.items() if _has(MemoryConfig, k) and k != "rolling_summary"}
    config = MemoryConfig(**plain)
    raw = data.get("rolling_summary")
    if isinstance(raw, dict):
        default = RollingSummaryConfig()
        config.rolling_summary = RollingSummaryConfig(
            enabled=raw.get("enabled") is not False,
            trigger_after=_as_int(raw.get("trigger_after"), default.trigger_after, minimum=1),
            keep_recent=_as_int(raw.get("keep_recent"), default.keep_recent, minimum=0),
            max_chars=_as_int(raw.get("max_chars"), default.max_chars, minimum=100),
        )
    return config


def _load_search(data: dict[str, Any]) -> SearchConfig:
    """解析 search 段；兼容旧的平铺写法。

    新结构：``order`` + ``tavily.api_key`` / ``deepseek.api_key``。
    旧结构（``provider`` + ``api_key`` 平铺）自动映射成 ``order: [provider]``
    并把 key 填给对应渠道；新结构优先，两者都写时旧字段只作兜底。
    旧配置里的 ``provider: bocha`` 已移除：忽略该渠道并打一行日志，按链中
    其余已配 key 的渠道走。
    """
    if not isinstance(data, dict):
        return SearchConfig()
    plain = {k: v for k, v in data.items() if _has(SearchConfig, k)}
    for name in SEARCH_PROVIDERS:
        if isinstance(plain.get(name), dict):
            plain[name] = SearchChannelConfig(
                **{k: v for k, v in plain[name].items() if _has(SearchChannelConfig, k)}
            )
    config = SearchConfig(**plain)

    legacy_provider = str(data.get("provider") or "").strip().lower()
    legacy_key = str(data.get("api_key") or "").strip()
    if not legacy_provider and not legacy_key:
        return config
    if not legacy_provider:
        # 旧默认渠道就是 bocha：只写了 api_key 的旧配置同样按已移除处理，
        # 别把旧 key 悄悄塞给别的渠道
        logger.info("搜索渠道 bocha 已移除，旧配置中的该渠道被忽略")
        return config
    if legacy_provider not in SEARCH_PROVIDERS:
        logger.warning("搜索渠道 %r 已移除或未知，旧配置中的该渠道被忽略", legacy_provider)
        return config
    channel = config.channel(legacy_provider)
    # 新结构里显式写了渠道 key 时优先；只有渠道 key 为空才用旧平铺 key
    if not channel.api_key and legacy_key:
        channel.api_key = legacy_key
    # 旧写法的 provider 就是唯一渠道；但它没 key 时不必把别的渠道挤出链外
    if "order" not in data and channel.configured:
        config.order = [legacy_provider]
    return config


def _load_decision(data: dict[str, Any]) -> DecisionConfig:
    """解析 decision 段；``groups`` 单独转成 GroupDecisionConfig。"""
    if not isinstance(data, dict):
        return DecisionConfig()
    plain = {k: v for k, v in data.items() if _has(DecisionConfig, k) and k != "groups"}
    config = DecisionConfig(**plain)
    groups = data.get("groups") or {}
    if isinstance(groups, dict):
        for chat_id, options in groups.items():
            if not isinstance(options, dict):
                continue
            quiet = options.get("quiet_hours") or []
            if isinstance(quiet, str):
                quiet = [quiet]
            config.groups[str(chat_id)] = GroupDecisionConfig(
                activity_multiplier=float(options.get("activity_multiplier", 1.0)),
                base=None if options.get("base") is None else float(options["base"]),
                quiet_hours=[str(item) for item in quiet],
            )
    return config


def _load_generation(data: dict[str, Any]) -> GenerationConfig:
    """解析 generation 段；只接受写明的布尔开关，写错类型回落默认（关闭）。"""
    if not isinstance(data, dict):
        return GenerationConfig()
    two_stage_raw = data.get("two_stage")
    enabled = isinstance(two_stage_raw, dict) and two_stage_raw.get("enabled") is True
    return GenerationConfig(two_stage=TwoStageConfig(enabled=enabled))


def _load_emotion(data: dict[str, Any]) -> EmotionConfig:
    """解析 emotion 段：标量宽松解析，阈值微调表单独处理。

    写错类型不挡启动：能按默认值的类型转换的就用，转不了回落默认值；
    ``threshold_adjust`` 只接受「心情 → 数字」的映射，非数字的值忽略
    （保持默认表里对应的项）。
    """
    if not isinstance(data, dict):
        return EmotionConfig()
    defaults = EmotionConfig()
    plain: dict[str, Any] = {}
    for key, value in data.items():
        if key == "threshold_adjust" or not _has(EmotionConfig, key):
            continue
        coerced = _coerce_like_default(value, getattr(defaults, key))
        if coerced is not None:
            plain[key] = coerced
    config = EmotionConfig(**plain)
    raw_adjust = data.get("threshold_adjust")
    if isinstance(raw_adjust, dict):
        adjust = dict(config.threshold_adjust)
        for mood, value in raw_adjust.items():
            name = str(mood).strip()
            if not name or isinstance(value, bool):
                continue
            try:
                adjust[name] = float(value)
            except (TypeError, ValueError):
                continue
        config.threshold_adjust = adjust
    return config


def _coerce_like_default(value: Any, default: Any) -> Any:
    """按默认值的类型宽松转换配置值；类型对不上或转不了时返回 None（用默认值）。

    配置里 ``enabled: "false"`` 这类手误不该让整个 bot 起不来：布尔字段只认
    明确的 true/false（字符串也行），数值字段能转数字就转，其他情况留空。
    """
    if value is None:
        return None
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "yes", "on", "1"}:
                return True
            if text in {"false", "no", "off", "0"}:
                return False
        return None
    if isinstance(default, (int, float)):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if isinstance(default, int) else number
    if isinstance(default, str):
        return "" if value is None else str(value)
    return None


def _has(dataclass_type: type, key: str) -> bool:
    return key in getattr(dataclass_type, "__dataclass_fields__", {})


def _as_float(value: Any, default: float) -> float:
    """宽松转 float：空值/非法值回落到默认，避免配置写错就启动失败。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    """宽松转 int（配置里可能写成字符串），下限兜底到 ``minimum``。"""
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)
