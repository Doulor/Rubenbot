"""QQ 空间说说插件：决定「什么时候发、发什么」，发送一律走核心能力入口。

触发方式：

- **命令**：聊天里发 ``/说说 [内容]``。带内容直接发；不带内容则由 LLM
  按当前人格生成一条短说说。命令名可在插件配置里改。
- **定时**：``interval_hours`` > 0 时，插件启动一个后台任务，按间隔自动
  生成并发布一条说说（题材优先取最近群聊，其次记忆，都没有就写日常）。

插件不碰任何平台协议，发布只调用核心能力 ``qzone.publish``；适配器没声明、
配置里关了（``adapters.<name>.capabilities.qzone``）或调用出错时，都会把
原因原样告诉用户并记日志。

配置放在插件目录下的 ``config.yaml``（可选，未写用默认值）；嵌入式场景也
可以直接把同结构的 dict 放进 ``ctx.extras["qzone_post_config"]`` 覆盖。
字段见 README.md 与 :class:`QzonePostConfig`。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 适配器里实际注册的能力名（SnowLuma 的 send_qzone_msg）
CAPABILITY = "qzone.publish"
# 核心流水里 bot 自己的发送者 id
_BOT_SENDER_ID = "__bot__"
_QUOTES = "「」『』“”\"'‘’"
_SPACE_RE = re.compile(r"\s+")
# 生成结果的兜底截断：句末标点优先
_SENTENCE_END = "。！？!?…~～；;，,"


@dataclass
class QzonePostConfig:
    """插件配置。"""

    # 定时发布间隔（小时）；0 表示关闭定时
    interval_hours: float = 0.0
    # 只允许 owner_ids 里的用户触发命令
    owner_only: bool = False
    # 主人 QQ / 平台 uid 列表；owner_only 开启时必须配置
    owner_ids: list[str] = field(default_factory=list)
    # 命令名（不带前缀），默认「说说」，即 /说说
    command: str = "说说"
    # 生成的说说最长字数
    max_chars: int = 60
    # 取热点时回看最近多少小时的群聊
    topic_window_hours: float = 6.0
    # 一次取多少条群聊消息作为题材
    topic_limit: int = 20


def read_config(directory: str | Path) -> QzonePostConfig:
    """读取插件目录下的 ``config.yaml``；不存在或格式不对时用默认值。"""
    path = Path(directory) / "config.yaml"
    if not path.exists():
        return QzonePostConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("[qzone_post] 读取 %s 失败，使用默认配置：%s", path, exc)
        return QzonePostConfig()
    return config_from_dict(data if isinstance(data, dict) else {})


def config_from_dict(data: dict[str, Any]) -> QzonePostConfig:
    """把配置 dict 转成 :class:`QzonePostConfig`；非法值退回默认值。"""
    defaults = QzonePostConfig()

    def _float(key: str) -> float:
        try:
            value = float(data.get(key, getattr(defaults, key)))
        except (TypeError, ValueError):
            return float(getattr(defaults, key))
        return max(0.0, value)

    def _int(key: str) -> int:
        try:
            value = int(data.get(key, getattr(defaults, key)))
        except (TypeError, ValueError):
            return int(getattr(defaults, key))
        return max(1, value)

    command = data.get("command", defaults.command)
    command = str(command).strip().lstrip("/") if command is not None else defaults.command
    owner_only = data.get("owner_only", defaults.owner_only)
    owner_ids = data.get("owner_ids", defaults.owner_ids)
    if isinstance(owner_ids, (str, int)):
        owner_ids = [owner_ids]
    if not isinstance(owner_ids, (list, tuple)):
        owner_ids = []
    return QzonePostConfig(
        interval_hours=_float("interval_hours"),
        owner_only=owner_only is True,
        owner_ids=[str(item).strip() for item in owner_ids if str(item).strip()],
        command=command or defaults.command,
        max_chars=_int("max_chars"),
        topic_window_hours=_float("topic_window_hours") or defaults.topic_window_hours,
        topic_limit=_int("topic_limit"),
    )


def clean_generated(text: str, max_chars: int = 60) -> str:
    """整理 LLM 输出：去掉包装引号/代码块，合并空白，超长时截断。"""
    value = (text or "").strip()
    if value.startswith("```"):
        # 去掉偶发的代码块包装
        value = re.sub(r"^```[^\n]*\n?", "", value)
        value = re.sub(r"\n?```$", "", value).strip()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    value = " ".join(lines) if lines else ""
    value = _SPACE_RE.sub(" ", value).strip()
    while len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    if max_chars > 0 and len(value) > max_chars:
        head = value[:max_chars]
        # 尽量在句末标点处收住，别把半句话硬切
        cut = max(head.rfind(ch) for ch in _SENTENCE_END)
        if cut >= max_chars // 2:
            head = head[: cut + 1]
        value = head.rstrip()
    return value


class QzonePostPlugin:
    """说说插件实例；loader 只调 :func:`register`，其余由实例自己管。"""

    def __init__(self, ctx: Any, config: QzonePostConfig):
        self.ctx = ctx
        self.config = config
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def task(self) -> asyncio.Task | None:
        """定时后台任务；interval_hours=0 或尚未启动时为 None。"""
        return self._task

    async def start(self) -> None:
        """插件启用：interval_hours > 0 时拉起后台任务。"""
        self._ensure_task()

    async def stop(self) -> None:
        """插件停用：让后台任务干净退出。"""
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        self._task = None
        self._stop_event = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 停用清理不应抛给宿主
                logger.warning("[qzone_post] 后台任务退出异常：%s", exc)

    def ensure_started(self) -> None:
        """懒启动：宿主第一次分发消息时如果后台任务还没起来，在这里补上。

        不需要宿主改动启动流程；interval_hours=0 时什么也不做。
        """
        self._ensure_task()

    def _ensure_task(self) -> None:
        if self.config.interval_hours <= 0:
            return
        if not getattr(self.ctx.plugin, "enabled", True):
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环（例如同步加载），等首次分发时再起
            return
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._interval_loop(), name="qzone_post_interval")

    def _bot_stop_event(self) -> asyncio.Event | None:
        """宿主 bot 的停止事件；插件据此随 bot 停机干净退出。"""
        event = getattr(self.ctx.bot, "_stop", None)
        return event if isinstance(event, asyncio.Event) else None

    async def _wait_tick(self, stop_event: asyncio.Event, timeout: float) -> bool:
        """等一次间隔或任一停止信号；返回 True 表示应该退出。

        分小段等待（最长 60 秒一段）：即使宿主没跑 on_stop，插件被禁用或
        bot 停机后，任务也能在一个小段内自己发现并退出。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if stop_event.is_set() or not getattr(self.ctx.plugin, "enabled", True):
                return True
            bot_stop = self._bot_stop_event()
            if bot_stop is not None and bot_stop.is_set():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            waiters: list[asyncio.Future] = [asyncio.ensure_future(stop_event.wait())]
            if bot_stop is not None:
                waiters.append(asyncio.ensure_future(bot_stop.wait()))
            try:
                await asyncio.wait(
                    waiters,
                    timeout=min(remaining, 60.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    async def _interval_loop(self) -> None:
        """按间隔发一条；任何异常只记录，绝不让任务死掉或带崩主循环。"""
        interval = max(0.1, float(self.config.interval_hours) * 3600)
        stop_event = self._stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            try:
                if await self._wait_tick(stop_event, interval):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 调度异常只记录，重新等待
                logger.warning("[qzone_post] 定时等待异常：%s", exc)
                await asyncio.sleep(min(interval, 60.0))
                continue
            # 刚停用/停机时不补发这一轮
            bot_stop = self._bot_stop_event()
            if stop_event.is_set() or not getattr(self.ctx.plugin, "enabled", True):
                return
            if bot_stop is not None and bot_stop.is_set():
                return
            try:
                await self.publish_scheduled()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次失败不影响下一轮
                logger.warning("[qzone_post] 定时发说说失败：%s", exc)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    async def handle_command(self, runtime: Any, event: Any, args: str) -> str:
        if self.config.owner_only:
            if not self.config.owner_ids:
                return "只有主人能让我发说说，但插件还没配置主人（owner_ids）"
            if str(getattr(event, "sender_id", "")) not in self.config.owner_ids:
                return "只有主人能让我发说说"

        content = (args or "").strip()
        if content:
            return await self._publish(content, prefix="说说已经发了：")
        content = await self.generate_post()
        if not content:
            return "这次没想出要说什么，晚点再试试？"
        return await self._publish(content, prefix="说说已经发了：")

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    async def _publish(self, content: str, *, prefix: str = "") -> str:
        ok, detail = await self.ctx.call_capability(CAPABILITY, content=content)
        if not ok:
            logger.warning("[qzone_post] 发布失败：%s", detail)
            return f"发不了说说：{detail}"
        return f"{prefix}{content}" if prefix else detail

    async def publish_scheduled(self, *, now: float | None = None) -> tuple[bool, str]:
        """定时路径：生成一条并发布。返回 ``(是否成功, 说明/原因)``。"""
        topic = self._recent_topic(now=now)
        content = await self.generate_post(topic=topic)
        if not content:
            logger.warning("[qzone_post] 定时发说说：没能生成内容，跳过这一轮")
            return False, "没能生成内容"
        ok, detail = await self.ctx.call_capability(CAPABILITY, content=content)
        if ok:
            logger.info("[qzone_post] 定时发说说成功：%s", content)
        else:
            logger.warning("[qzone_post] 定时发说说失败：%s", detail)
        return ok, detail

    # ------------------------------------------------------------------
    # 内容生成
    # ------------------------------------------------------------------

    async def generate_post(self, *, topic: str | None = None) -> str:
        """按当前人格让核心 LLM 写一条短说说；失败/空内容返回空串。"""
        llm = self.ctx.llm
        if llm is None:
            logger.warning("[qzone_post] 宿主还没就绪，拿不到 LLM")
            return ""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._generation_prompt()},
        ]
        if topic:
            messages.append({"role": "user", "content": f"可以顺着最近这些聊天写，别照搬：\n{topic}"})
        messages.append({"role": "user", "content": "直接写说说正文，只输出正文。"})
        try:
            raw = await llm.complete(messages, role="chat")
        except Exception as exc:  # noqa: BLE001 - LLM 失败不拖垮插件
            logger.warning("[qzone_post] 生成说说失败：%s", exc)
            return ""
        content = clean_generated(raw, self.config.max_chars)
        if not content:
            logger.warning("[qzone_post] LLM 返回了空内容")
        return content

    def _generation_prompt(self) -> str:
        persona = self._persona_text()
        instruction = (
            "现在你要在 QQ 空间发一条说说，是发在自己空间里、所有人都能看到的动态，"
            "不是对着某个人聊天。\n"
            f"要求：\n"
            f"- 用上面这个人的口吻，口语、随手写，别端着\n"
            f"- 不超过 {self.config.max_chars} 字，越短越好\n"
            "- 不用话题标签，不要 #…#，不要 emoji 刷屏\n"
            "- 不解释、不总结、不加引号，只写正文\n"
            "- 不知道写什么就写点日常：天气、路上看到的、吃的、困、无聊，都行"
        )
        return f"{persona}\n\n{instruction}".strip()

    def _persona_text(self) -> str:
        runtime = getattr(self.ctx, "runtime", None)
        persona = getattr(runtime, "persona", None)
        if persona is None:
            persona = getattr(self.ctx.bot, "persona", None)
        prompt = getattr(persona, "system_prompt", None)
        if callable(prompt):
            try:
                return str(prompt()).strip()
            except Exception:  # noqa: BLE001
                pass
        return getattr(persona, "text", "") or ""

    # ------------------------------------------------------------------
    # 题材
    # ------------------------------------------------------------------

    def _recent_topic(self, *, now: float | None = None) -> str:
        """最近群聊优先，其次最近记忆；都没有返回空串（写日常向）。"""
        store = self.ctx.store
        now = time.time() if now is None else now
        since = now - self.config.topic_window_hours * 3600
        try:
            chats = store.list_chats(limit=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_post] 读取会话列表失败：%s", exc)
            chats = []
        for chat in chats:
            if chat.get("chat_type") != "group":
                continue
            if float(chat.get("last_ts") or 0) < since:
                continue
            try:
                messages = store.recent_messages(
                    chat["chat_id"],
                    limit=self.config.topic_limit,
                    since=since,
                    platform=chat.get("platform"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[qzone_post] 读取最近消息失败：%s", exc)
                continue
            lines = [
                f"{msg.sender_name or msg.sender_id}：{msg.text}"
                for msg in messages
                if msg.sender_id != _BOT_SENDER_ID and (msg.text or "").strip()
            ]
            if len(lines) >= 2:
                return "\n".join(lines[-self.config.topic_limit :])
        try:
            memories = store.list_memories(limit=6)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_post] 读取记忆失败：%s", exc)
            memories = []
        if memories:
            return "\n".join(f"- {memory.content}" for memory in memories)
        return ""


def load_config(ctx: Any) -> QzonePostConfig:
    """优先用 ``ctx.extras["qzone_post_config"]`` 覆盖，其次读插件目录 config.yaml。"""
    override = ctx.extras.get("qzone_post_config")
    if isinstance(override, dict):
        return config_from_dict(override)
    path = getattr(getattr(ctx, "plugin", None), "path", None)
    return read_config(path) if path is not None else QzonePostConfig()


def register(ctx: Any) -> None:
    """插件入口。"""
    config = load_config(ctx)
    plugin = QzonePostPlugin(ctx, config)
    # 便于测试和宿主排查；插件名在本目录里唯一
    ctx.extras["qzone_post"] = plugin

    async def handle_command(runtime: Any, event: Any, args: str) -> str:
        plugin.ensure_started()
        return await plugin.handle_command(runtime, event, args)

    @ctx.on_message
    async def ensure_running(runtime: Any, event: Any):
        # 宿主是同步加载插件的，这里没有事件循环；第一条消息到来时懒启动定时任务
        plugin.ensure_started()
        return None

    ctx.command(config.command, handle_command)
    # 宿主加载/重新启用插件时会跑 on_start，在事件循环里可直接拉起任务
    ctx.on_start(plugin.start)
    ctx.on_stop(plugin.stop)
