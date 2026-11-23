"""点歌插件（按 hydrogen_astrbot-plugin-music 复刻）。

命令面、选歌列表、发送链、热评/歌词这些行为都对齐参考插件
（homecloud ``dsh-readonly/maibot-plugins/hydrogen_astrbot-plugin-music``，MIT），
但落在 RubenBot 的插件框架与 SnowLuma 协议端上，所以有三处有意不同：

1. **发送链序**：参考插件默认 card → record → file → text；2026-11-23/24 实测
   新版 QQ 不渲染 OneBot ``music`` 段（只显示「发送者版本过低」），而
   ``record``（语音段传音频 URL）能让协议端自己下载、转码并发成能播的语音条
   （实测整首转成 SILK，时长与源一致）。因此默认链是 ``record → file → text``，
   ``card`` 保留为配置可选项，不默认启用。
2. **发送前先探活音频**：网易云的 ``song/media/outer/url`` 对 VIP / 无版权曲目
   会 302 到 ``music.163.com/404``（text/html），协议端拿它转语音会直接失败
   （实测 retcode 100）。所以搜索结果先并发探活，探不出音频的条目直接不进
   选歌列表；一条能放的都没有时如实告诉对方，并建议换平台，而不是塞一首放
   不出来的。
3. **撤回走适配器动作通道**：选歌列表超时/选中/取消后要撤回，需要平台侧的
   ``delete_msg``。RubenBot 把这类动作收在 ``capabilities`` 授权后面，所以
   manifest 声明了该权限；机主没授权时撤回静默降级（列表留着，状态照常清理），
   其余功能不受影响。

此外选歌只认点歌人自己的回复：参考插件按会话记录待选列表，同一会话里任何人
的数字都会被当成选择；群里这容易误伤普通聊天，这里额外核对发送者。

流程：
- 命中点歌命令 → 单人冷却 → 搜歌（探活过滤）→ 单结果直发，多结果发编号列表
- 回复序号 → 撤回列表 → 发歌；回复「取消」→ 撤回列表 → 回一句已取消
- 列表超时 → 撤回列表 → 回一句已超时
- 发送链逐档降级，最差也会把歌名、歌手、时长和链接发出去
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx
import yaml

from rubenbot.adapters.base import Segment
from rubenbot.expression import CapabilityNote

from .renderer import LyricsRenderer
from .sender import MusicSender, parse_send_modes
from .sources import NETEASE_SOURCE, TXQQ_SOURCE, Song, SongsUnplayable, probe_audio

logger = logging.getLogger(__name__)

DEFAULT_DEFAULT_PLAYER = "网易点歌"
DEFAULT_SONG_LIMIT = 5
DEFAULT_SELECT_MODE = "text"
# card 不放在默认链里：新版 QQ 不渲染 OneBot music 段（见模块文档）
DEFAULT_SEND_MODES = ("record", "file", "text")
DEFAULT_TIMEOUT = 30
DEFAULT_COOLDOWN_SECONDS = 60.0
REQUEST_TIMEOUT = 15.0
LYRICS_TEXT_LIMIT = 2000

# 搜索池：网易云 VIP 曲目多，先多取一些再探活过滤，否则 5 条里可能一条能放的都没有
SEARCH_POOL_FACTOR = 4
SEARCH_POOL_MAX = 20
# 探活并发度：探活是纯 IO，但别一次打 20 个 CDN；攒够结果就停，不再探后面的
PROBE_CONCURRENCY = 6
# 整批探活的总时限：慢 CDN 不能让人干等着，到点就用手上已有结果
PROBE_BUDGET_SECONDS = 12.0

# 平台命令别名 → (来源, 子平台, 显示名, 后面是否必须空白)。按长度从长到短
# 匹配，避免「点歌」抢先吃掉「QQ点歌」。裸平台名（酷狗 / 网易云 等）后面要求
# 空白分隔，免得「酷狗音乐不错」这种闲聊被当成点歌；带「点歌」的命令别名
# 则允许直接跟歌名（「点歌晴天」）。
PLATFORM_ALIASES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("5sing原创", "txqq", "5singyc", "5sing 原创", False),
    ("5sing翻唱", "txqq", "5singfc", "5sing 翻唱", False),
    ("喜马拉雅点歌", "txqq", "ximalaya", "喜马拉雅", False),
    ("喜马拉雅", "txqq", "ximalaya", "喜马拉雅", True),
    ("全民K歌", "txqq", "kg", "全民K歌", False),
    ("全民k歌", "txqq", "kg", "全民K歌", False),
    ("全民点歌", "txqq", "kg", "全民K歌", False),
    ("QQ点歌", "txqq", "qq", "QQ音乐", False),
    ("qq点歌", "txqq", "qq", "QQ音乐", False),
    ("网易点歌", "netease", "netease", "网易云音乐", False),
    ("网易云音乐", "netease", "netease", "网易云音乐", True),
    ("网易云", "netease", "netease", "网易云音乐", True),
    ("酷狗点歌", "txqq", "kugou", "酷狗音乐", False),
    ("酷狗", "txqq", "kugou", "酷狗音乐", True),
    ("酷我点歌", "txqq", "kuwo", "酷我音乐", False),
    ("酷我", "txqq", "kuwo", "酷我音乐", True),
    ("百度点歌", "txqq", "baidu", "百度音乐", False),
    ("百度", "txqq", "baidu", "百度音乐", True),
    ("一听点歌", "txqq", "1ting", "一听音乐", False),
    ("一听", "txqq", "1ting", "一听音乐", True),
    ("咪咕点歌", "txqq", "migu", "咪咕音乐", False),
    ("咪咕", "txqq", "migu", "咪咕音乐", True),
    ("荔枝点歌", "txqq", "lizhi", "荔枝FM", False),
    ("荔枝", "txqq", "lizhi", "荔枝FM", True),
    ("蜻蜓点歌", "txqq", "qingting", "蜻蜓FM", False),
    ("蜻蜓", "txqq", "qingting", "蜻蜓FM", True),
    ("我想听", "", "", "", False),
)
DEFAULT_ALIAS = "点歌"
DISPLAY_NAME = {
    "netease": "网易云音乐",
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "baidu": "百度音乐",
    "1ting": "一听音乐",
    "migu": "咪咕音乐",
    "lizhi": "荔枝FM",
    "qingting": "蜻蜓FM",
    "ximalaya": "喜马拉雅",
    "5singyc": "5sing 原创",
    "5singfc": "5sing 翻唱",
    "kg": "全民K歌",
}

_TRIM_CHARS = " \t\r\n：:，,、。.!！?？~～…—-"
_HOLLOW_KEYWORDS = frozenset({"歌", "一首歌", "首歌", "一个", "个"})
_LYRICS_PATTERN = re.compile(r"^(?:查歌词|歌词)\s*(?P<song>.+)?$")
_CANCEL_WORDS = frozenset({"取消", "取消点歌"})


@dataclass(frozen=True)
class Request:
    """一条点歌命令的解析结果；``song`` 为空表示只说了平台没给歌名。"""

    song: str
    index: int
    source: str
    platform_type: str
    label: str


@dataclass
class PendingSelection:
    """一次待选中的选歌列表。"""

    songs: list[Song]
    label: str
    sender_id: str
    timestamp: float
    chat_id: str
    chat_type: str
    adapter: Any = None
    message_id: Any = None
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MusicConfig:
    """归一化后的插件配置；类型不对的项退回默认值，不让配置错误炸掉插件。"""

    song_limit: int = DEFAULT_SONG_LIMIT
    select_mode: str = DEFAULT_SELECT_MODE
    timeout: int = DEFAULT_TIMEOUT
    send_modes: tuple[str, ...] = DEFAULT_SEND_MODES
    enable_comments: bool = True
    enable_lyrics: bool = False
    timeout_recall: bool = True
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    default_player: str = DEFAULT_DEFAULT_PLAYER

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "MusicConfig":
        raw = data if isinstance(data, Mapping) else {}
        select_mode = str(raw.get("select_mode") or DEFAULT_SELECT_MODE).strip().lower()
        if select_mode not in ("text", "single"):
            select_mode = DEFAULT_SELECT_MODE
        return cls(
            song_limit=_as_int(raw.get("song_limit"), DEFAULT_SONG_LIMIT, low=1, high=20),
            select_mode=select_mode,
            timeout=_as_int(raw.get("timeout"), DEFAULT_TIMEOUT, low=5, high=600),
            send_modes=parse_send_modes(raw.get("send_modes"), DEFAULT_SEND_MODES),
            enable_comments=_as_bool(raw.get("enable_comments"), True),
            enable_lyrics=_as_bool(raw.get("enable_lyrics"), False),
            timeout_recall=_as_bool(raw.get("timeout_recall"), True),
            cooldown_seconds=_as_non_negative(
                raw.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS
            ),
            default_player=str(raw.get("default_player") or DEFAULT_DEFAULT_PLAYER).strip(),
        )

    @property
    def real_song_limit(self) -> int:
        return 1 if self.select_mode == "single" else self.song_limit


def read_config(directory: str | Path | None = None) -> MusicConfig:
    """读取插件目录下的 config.yaml（其次 config.example.yaml）；没有就用默认值。"""
    if directory is None:
        return MusicConfig()
    base = Path(directory)
    file = next(
        (item for item in (base / "config.yaml", base / "config.example.yaml") if item.exists()),
        None,
    )
    if file is None:
        return MusicConfig()
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能启动
        logger.warning("music 读取配置 %s 失败，使用默认配置：%s", file, exc)
        return MusicConfig()
    return MusicConfig.from_mapping(data)


def load_config(ctx: Any = None) -> MusicConfig:
    """优先用 ``ctx.extras["music_config"]`` 覆盖，其次读插件目录 config.yaml。"""
    extras = getattr(ctx, "extras", None) if ctx is not None else None
    if isinstance(extras, Mapping):
        override = extras.get("music_config")
        if isinstance(override, Mapping):
            return MusicConfig.from_mapping(override)
    directory = getattr(getattr(ctx, "plugin", None), "path", None)
    return read_config(directory)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


def parse_request(
    text: str, *, default_player: str = DEFAULT_DEFAULT_PLAYER
) -> Request | None:
    """识别点歌命令，返回歌名、序号与音源；不命中返回 None。

    触发词按长度从长到短匹配。裸平台名（酷狗 / 网易云 等）后面必须跟空白，
    「点歌」类命令则允许直接跟歌名（「点歌晴天」）。
    """
    value = _strip_leading_mentions(text)
    if not value:
        return None
    aliases = sorted(
        (*PLATFORM_ALIASES, (DEFAULT_ALIAS, "", "", "", False)),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for alias, source, platform_type, label, need_space in aliases:
        if not value.startswith(alias):
            continue
        remainder = value[len(alias) :]
        # 裸平台名要求分隔：「酷狗音乐不错」≠ 点歌
        if need_space and remainder and not remainder[0].isspace():
            continue
        song_part = remainder.strip().strip(_TRIM_CHARS).strip()
        if song_part in _HOLLOW_KEYWORDS:
            song_part = ""
        index = 0
        match = re.search(r"\s+(\d{1,2})$", song_part)
        if match:
            index = int(match.group(1))
            song_part = song_part[: match.start()].strip().strip(_TRIM_CHARS).strip()
        if source == "":
            source, platform_type, label = _default_target(default_player)
        return Request(song_part, index, source, platform_type, label)
    return None


def _default_target(default_player: str) -> tuple[str, str, str]:
    """把配置里的默认点歌平台（如「网易点歌」「QQ音乐」）翻成来源。

    先按完整别名 / 显示名精确匹配，再按「别名出现在配置串里」兜底（配置写成
    「QQ点歌（默认）」也能认出 QQ）。配置只写「点歌」时就是网易云；否则
    「点歌」这个子串会让「喜马拉雅点歌」这类更长的别名抢先命中。
    """
    word = (default_player or "").strip()
    if word and word != DEFAULT_ALIAS:
        for alias, source, platform_type, label, _ in PLATFORM_ALIASES:
            if word.lower() in (alias.lower(), label.lower()):
                return source, platform_type, label
        for alias, source, platform_type, label, _ in PLATFORM_ALIASES:
            if alias and alias in word:
                return source, platform_type, label
    return "netease", "netease", DISPLAY_NAME["netease"]


def _strip_leading_mentions(text: str) -> str:
    """去掉正文开头的 ``@某人`` 占位，让「@bot 点歌 晴天」也能命中。

    统一事件里 @ 段渲染成 ``@昵称 ``；其他人在开头 @ 别人时同样剥掉，
    后面紧跟「点歌」才触发，不改变「正文里提到 @ 字样不算寻址」的约定。
    """
    value = (text or "").strip()
    # 统一事件把 @ 段渲染成「@账号id 」（见 Segment.render），整段剥掉即可
    stripped = re.sub(r"^(?:@\S+\s+)+", "", value)
    return stripped.strip()


def parse_lyrics_request(text: str) -> str | None:
    """``查歌词 <歌名>`` / ``歌词 <歌名>``；不命中返回 None（空歌名返回空串）。"""
    match = _LYRICS_PATTERN.match(_strip_leading_mentions(text))
    if not match:
        return None
    return (match.group("song") or "").strip().strip(_TRIM_CHARS).strip()


def format_selection_list(songs: list[Song], label: str, *, timeout: int) -> str:
    lines = [f"【{label}】"]
    lines.extend(
        f"{index}. {song.name} - {song.artist_text}"
        for index, song in enumerate(songs, start=1)
    )
    lines.append("")
    lines.append(f"请回复序号选择歌曲，或回复「取消」取消点歌（{timeout} 秒内有效）")
    return "\n".join(lines)


def selection_label(request: Request, songs: list[Song]) -> str:
    """选歌列表标题用结果的真实来源。

    指定平台（酷狗 / 咪咕 等）没结果时 :meth:`MusicPlayer.search` 会退回网易云，
    这时标题若还写「酷狗音乐」就是骗人；按第一首的 ``platform_type`` 取名。
    """
    first = songs[0].platform_type if songs else ""
    return DISPLAY_NAME.get(first) or request.label


def search_pool_size(limit: int) -> int:
    """搜索时实际取多少条：留出被探活过滤掉的余量。"""
    return min(max(limit * SEARCH_POOL_FACTOR, limit), SEARCH_POOL_MAX)


# ----------------------------------------------------------------------
# 插件主体
# ----------------------------------------------------------------------


class MusicPlayer:
    """点歌的识别、搜索与发送；HTTP 客户端、时钟可注入，便于测试。"""

    def __init__(
        self,
        config: MusicConfig | Mapping[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
        renderer: LyricsRenderer | None = None,
    ):
        self.config = (
            config if isinstance(config, MusicConfig) else MusicConfig.from_mapping(config)
        )
        self._client = client
        self._owns_client = client is None
        self.clock = clock or time.monotonic
        self.renderer = renderer if renderer is not None else LyricsRenderer()
        self.sender = MusicSender(self.config, self._get_client, self.renderer)
        # (平台, 会话类型, 会话) -> 待选列表；只认点歌人自己的选择
        self._pending: dict[tuple[str, str, str], PendingSelection] = {}
        # (平台, 点歌人) -> 上次点歌时间
        self._last_played_at: dict[tuple[str, str], float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    async def on_message(self, runtime, event):
        """命中点歌/选歌/查歌词就处理并拦截，否则放行（返回 None）。"""
        text = getattr(event, "text", "") or ""
        pending = self._pending.get(self._pending_key(event))
        claimed = (
            (pending is not None and pending.sender_id == str(getattr(event, "sender_id", "")))
            or parse_lyrics_request(text) is not None
            or parse_request(text, default_player=self.config.default_player) is not None
        )
        try:
            return await self._handle(runtime, event)
        except Exception as exc:  # noqa: BLE001 - 插件异常不能拖垮主流程
            logger.exception("music 处理点歌异常：%s", exc)
            if claimed:
                await self._reply(runtime, event, "点歌出了点问题，稍后再试试")
                return False
            return None

    async def _handle(self, runtime, event):
        adapter = _adapter_for(runtime, event)
        text = _strip_leading_mentions(getattr(event, "text", "") or "")
        sender_id = str(getattr(event, "sender_id", "") or "")

        pending = self._pending.get(self._pending_key(event))
        if pending is not None and pending.sender_id == sender_id:
            if text in _CANCEL_WORDS or text.isdigit():
                if adapter is not None:
                    await self._handle_selection(adapter, event, pending, text)
                return False

        lyrics_name = parse_lyrics_request(text)
        if lyrics_name is not None:
            if adapter is not None:
                await self._handle_lyrics(adapter, event, lyrics_name)
            return False

        request = parse_request(text, default_player=self.config.default_player)
        if request is None:
            return None
        if not request.song:
            await self._reply(runtime, event, "想听哪首歌？说「点歌 歌名」就好")
            return False
        if adapter is None:
            return False

        key = (str(getattr(event, "platform", "") or ""), sender_id)
        now = self.clock()
        last = self._last_played_at.get(key)
        if last is not None:
            remaining = self.config.cooldown_seconds - (now - last)
            if remaining > 0:
                await self._reply(
                    runtime, event, f"刚点过啦，{math.ceil(remaining)} 秒后再点吧"
                )
                return False
        # 先记冷却再请求：接口挂了也不在冷却期内反复打它
        self._last_played_at[key] = now

        try:
            songs = await self.search(
                request.song,
                source=request.source,
                platform_type=request.platform_type,
            )
        except SongsUnplayable as exc:
            logger.info("music 搜索「%s」有结果但都放不了", request.song)
            await self._reply(runtime, event, _unplayable_hint(exc, request))
            return False
        except Exception as exc:  # noqa: BLE001 - 网络/解析失败都降级
            logger.warning("music 搜索「%s」失败：%s", request.song, exc)
            await self._reply(runtime, event, "搜不到，换个关键词试试")
            return False
        if not songs:
            logger.info("music 搜索「%s」没有结果", request.song)
            await self._reply(runtime, event, f"没找到《{request.song}》，换个关键词试试")
            return False

        if request.index and 1 <= request.index <= len(songs):
            await self._send_song(adapter, event, songs[request.index - 1])
            return False
        if len(songs) == 1:
            await self._send_song(adapter, event, songs[0])
            return False

        await self._offer_selection(adapter, event, songs, request)
        return False

    async def aclose(self) -> None:
        """插件停用时取消超时任务并释放自己创建的 HTTP 连接池。

        取消后要等任务真正结束再返回：停用流程随后会关事件循环，不等的话
        超时任务可能停在 ``sleep`` 的取消点上，留下「已取消未收尾」的告警。
        """
        for pending in list(self._pending.values()):
            if pending.task is not None:
                pending.task.cancel()
        self._pending.clear()
        tasks = [task for task in self._tasks if not task.done()]
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client = self._client
        if client is None or not self._owns_client:
            return
        self._client = None
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不致命
            logger.debug("music 关闭 HTTP 客户端失败：%s", exc)

    # ------------------------------------------------------------------
    # 选歌
    # ------------------------------------------------------------------

    async def _offer_selection(self, adapter, event, songs, request: Request) -> None:
        key = self._pending_key(event)
        # 同一会话里还有没选完的旧列表：撤回它，免得两个编号列表并存
        await self._drop_pending(key, recall=True)
        label = selection_label(request, songs)
        text = format_selection_list(songs, label, timeout=self.config.timeout)
        message_id = await _safe_send(adapter, event, [Segment.text(text)])
        pending = PendingSelection(
            songs=list(songs),
            label=label,
            sender_id=str(getattr(event, "sender_id", "") or ""),
            timestamp=self.clock(),
            chat_id=str(getattr(event, "chat_id", "") or ""),
            chat_type=str(getattr(event, "chat_type", "") or "group"),
            adapter=adapter,
            message_id=message_id,
        )
        self._pending[key] = pending
        if message_id in (None, ""):
            # 没有 message_id 只是没法撤回（如 Telegram 的 send 不回 id），
            # 选歌本身照常可用；超时后清状态并提示一句
            logger.warning("music 没有拿到选歌列表的 message_id，本次无法撤回")
        # 超时任务总是登记：即使不撤回，也要在超时后清掉待选状态并提示一句
        pending.task = self._schedule(self._expire_selection(key, pending))

    async def _handle_selection(
        self, adapter, event, pending: PendingSelection, text: str
    ) -> None:
        key = self._pending_key(event)
        if self.clock() - pending.timestamp > self.config.timeout:
            await self._drop_pending(key, recall=True)
            await self._reply_text(adapter, event, "点歌已超时，请重新点歌")
            return
        if text in _CANCEL_WORDS:
            await self._drop_pending(key, recall=True)
            await self._reply_text(adapter, event, "已取消点歌")
            return
        index = int(text)
        if index < 1 or index > len(pending.songs):
            await self._reply_text(
                adapter, event, f"序号超出范围，请输入 1-{len(pending.songs)} 之间的数字"
            )
            return
        song = pending.songs[index - 1]
        await self._drop_pending(key, recall=True)
        await self._send_song(adapter, event, song)

    async def _expire_selection(self, key, pending: PendingSelection) -> None:
        """超时：撤回选歌列表、清掉待选状态，并提示一句已超时。"""
        await asyncio.sleep(self.config.timeout)
        # 期间可能已经换过一次列表：只清理自己那一份
        if self._pending.get(key) is not pending:
            return
        self._pending.pop(key, None)
        if self.config.timeout_recall:
            await self._recall(pending.adapter, pending.message_id)
        await self._send_text(
            pending.adapter, pending.chat_id, pending.chat_type, "点歌已超时，请重新点歌"
        )

    async def _drop_pending(self, key, *, recall: bool) -> None:
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending.task
        if recall:
            await self._recall(pending.adapter, pending.message_id)

    async def _recall(self, adapter, message_id) -> None:
        """撤回选歌列表：走适配器动作通道（需要 capabilities 授权），失败只记日志。"""
        if adapter is None or message_id in (None, ""):
            return
        call = getattr(adapter, "call_action", None)
        if not callable(call):
            return
        try:
            await call("delete_msg", {"message_id": message_id})
            logger.info("music 选歌列表已撤回：%s", message_id)
        except Exception as exc:  # noqa: BLE001 - 撤回失败不影响点歌
            logger.warning("music 选歌列表撤回失败（%s）：%s", message_id, exc)

    # ------------------------------------------------------------------
    # 搜索 / 发送
    # ------------------------------------------------------------------

    async def search(
        self, keyword: str, *, source: str = "netease", platform_type: str = "netease"
    ) -> list[Song]:
        """搜歌并过滤掉放不出来的条目。

        网易云 VIP / 无版权曲目会拿到 ``music.163.com/404`` 的 HTML，协议端
        转语音必失败；txqq 的 netease 源同理，所以这两路都先并发探活。
        其余聚合源给的是 CDN 直链，不做探活（省一轮网络往返），只丢掉占位
        地址。聚合接口对部分子平台（酷狗 / 咪咕 / 喜马拉雅 / 5sing / 全民K歌）
        经常整页空结果，这时退回网易云搜索，不让对方白点一次。

        一条能放的都没有时抛 :class:`SongsUnplayable`，让调用方给不一样的提示。
        """
        client = await self._get_client()
        limit = self.config.real_song_limit
        pool = search_pool_size(limit)
        if source == "txqq":
            platform = (platform_type or "qq").lower()
            songs = await TXQQ_SOURCE.search(client, keyword, pool, platform_type=platform)
            if platform != "netease":
                # 聚合源给的是 CDN 直链，不做探活（省一轮网络往返）；但偶尔会
                # 返回只有域名没有路径的占位地址（解析时已清成空串），这种条目
                # 哪一档都放不出来，直接不进选歌列表
                playable = [song for song in songs if song.audio_url]
                if playable:
                    return playable[:limit]
                # 聚合接口对酷狗 / 咪咕 / 喜马拉雅等子平台经常整页空结果：
                # 按「音源不可用」退回网易云搜索，不让对方白点一次
                logger.info(
                    "music 平台 %s 没搜到「%s」，退回网易云搜索", platform, keyword
                )
            elif songs:
                # 聚合接口里的 netease 源同样是 outer url，VIP 曲目照样 404
                playable = await self.filter_playable(client, songs, limit=limit)
                if playable:
                    return playable
        songs = await NETEASE_SOURCE.search(client, keyword, pool)
        if not songs:
            return []
        playable = await self.filter_playable(client, songs, limit=limit)
        if not playable and songs:
            raise SongsUnplayable(keyword, len(songs))
        return playable

    async def filter_playable(
        self, client: httpx.AsyncClient, songs: list[Song], *, limit: int = 0
    ) -> list[Song]:
        """并发探活；明确不可播的丢掉，探不出结论的保留（交给发送链降级）。

        攒够 ``limit`` 条就停，不再探后面的：搜索池是结果的 4 倍，全探一遍
        要多等好几秒，而多出来的条目本来也用不上。整批探活另有总时限
        :data:`PROBE_BUDGET_SECONDS`——CDN 慢的时候不能让人干等：到点就用手上
        已经探完的结果（探不出结论的条目本来也会保留）。返回顺序保持搜索顺序。
        """
        if not songs:
            return []
        target = limit if limit > 0 else len(songs)
        lock = asyncio.Lock()
        queue = list(enumerate(songs))
        kept: list[tuple[int, Song]] = []
        dead = 0

        async def worker() -> None:
            nonlocal dead
            while True:
                async with lock:
                    if len(kept) >= target or not queue:
                        return
                    position, song = queue.pop(0)
                if not song.audio_url:
                    async with lock:
                        dead += 1
                    continue
                verdict = await probe_audio(client, song.audio_url)
                async with lock:
                    if verdict is False:
                        dead += 1
                    else:
                        kept.append((position, song))

        try:
            await asyncio.wait_for(
                asyncio.gather(*(worker() for _ in range(min(PROBE_CONCURRENCY, len(songs))))),
                timeout=PROBE_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            async with lock:
                pending = len(queue)
            # 超时不算「放不了」：没探完的条目留到下次，这里只用已探完的结果
            logger.info(
                "music 探活超过 %.0f 秒，先用手上 %d 首（还有 %d 首没探完）",
                PROBE_BUDGET_SECONDS,
                len(kept),
                pending,
            )
        result = [song for _, song in sorted(kept, key=lambda item: item[0])]
        if dead:
            logger.info("music 探活过滤掉 %d 首拿不到音频的结果（共 %d 首）", dead, len(songs))
        return result

    async def _send_song(self, adapter, event, song: Song) -> None:
        source = TXQQ_SOURCE if song.source == "txqq" else NETEASE_SOURCE
        await self.sender.send_song(adapter, event, song, source)

    async def _handle_lyrics(self, adapter, event, name: str) -> None:
        if not name:
            await self._reply_text(adapter, event, "请输入歌曲名称\n用法：查歌词 <歌名>")
            return
        # 查歌词不涉及播放，不探活：VIP 曲目拿不到音频但歌词照样查得到
        limit = self.config.real_song_limit
        try:
            songs = await NETEASE_SOURCE.search(await self._get_client(), name, limit)
        except Exception as exc:  # noqa: BLE001 - 网络失败降级
            logger.warning("music 查歌词搜索「%s」失败：%s", name, exc)
            songs = []
        if not songs:
            await self._reply_text(adapter, event, f"没找到《{name}》，换个关键词试试")
            return
        song = songs[0]
        if await self.sender.send_lyrics_image(adapter, event, song, NETEASE_SOURCE):
            return
        lyrics = song.lyrics or await NETEASE_SOURCE.lyrics(await self._get_client(), song)
        if not lyrics:
            await self._reply_text(adapter, event, f"《{song.name}》暂无歌词")
            return
        body = lyrics[:LYRICS_TEXT_LIMIT]
        await _safe_send(adapter, event, [Segment.text(f"《{song.name}》\n{body}")])

    async def _reply(self, runtime, event, text: str) -> None:
        adapter = _adapter_for(runtime, event)
        await self._reply_text(adapter, event, text)

    async def _reply_text(self, adapter, event, text: str) -> None:
        await self._send_text(
            adapter,
            str(getattr(event, "chat_id", "") or ""),
            str(getattr(event, "chat_type", "") or "group"),
            text,
        )

    async def _send_text(self, adapter, chat_id: str, chat_type: str, text: str) -> None:
        if adapter is None:
            return
        try:
            await adapter.send(chat_id, [Segment.text(text)], chat_type=chat_type)
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮主流程
            logger.warning("music 回复发送失败：%s", exc)

    # ------------------------------------------------------------------
    # 能力说明
    # ------------------------------------------------------------------

    def capability_note(self, event, adapter) -> CapabilityNote | None:
        return CapabilityNote(
            "点歌",
            "我能按歌名点歌，多个结果会发编号列表让你挑。",
            "有人想听歌时，可以让他发「点歌 歌名」；要指定平台就说「QQ点歌 歌名」"
            "「网易点歌 歌名」，列表出来后回复序号就行。",
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _pending_key(self, event) -> tuple[str, str, str]:
        return (
            str(getattr(event, "platform", "") or ""),
            str(getattr(event, "chat_type", "") or ""),
            str(getattr(event, "chat_id", "") or ""),
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True)
            self._owns_client = True
        return self._client

    def _schedule(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _unplayable_hint(exc: SongsUnplayable, request: Request) -> str:
    """搜到歌但一首能放的都没有时的提示；默认音源就给个换平台的建议。"""
    hint = f"《{exc.keyword}》这边找到的版本都放不了（多半是 VIP 曲目）"
    if request.source == "netease":
        return hint + "，试试「QQ点歌 " + exc.keyword + "」"
    return hint + "，换个歌名试试"


async def _safe_send(adapter, event, segments) -> Any:
    if adapter is None:
        return None
    try:
        return await adapter.send(event.chat_id, segments, chat_type=event.chat_type)
    except Exception as exc:  # noqa: BLE001 - 列表发不出去就不登记待选
        logger.warning("music 消息发送失败：%s", exc)
        return None


def _adapter_for(runtime, event):
    adapters = getattr(runtime, "adapters", None)
    if not isinstance(adapters, Mapping):
        return None
    return adapters.get(getattr(event, "platform", ""))


def _as_int(value: Any, default: int, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return number if low <= number <= high else default


def _as_non_negative(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, float(value))


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def register(ctx) -> None:
    """插件入口：注册消息钩子与停用清理。"""
    player = MusicPlayer(load_config(ctx))
    ctx.extras["music"] = player
    ctx.on_message(player.on_message)
    ctx.on_stop(player.aclose)
    ctx.capability_note(player.capability_note)
    logger.debug(
        "music 已注册（发送链 %s，冷却 %.0fs，默认音源 %s）",
        " → ".join(player.config.send_modes),
        player.config.cooldown_seconds,
        player.config.default_player,
    )
