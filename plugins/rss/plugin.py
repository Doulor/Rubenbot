"""RSS 订阅插件。

订阅网站的 RSS/Atom，定时抓新条目，用 QQ 合并转发推到指定的群或私聊。
私聊里任何人都能发指令订阅；群里只有群管理员能订阅该群。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .fetcher import fetch_feed_text, new_entries, parse_feed
from .store import RssStore

logger = logging.getLogger(__name__)

# 连续抓取失败这么多次后，列表里标出该源有问题（订阅保留，恢复后自动续推）。
FAIL_THRESHOLD = 5
# 转发节点默认的发送者 QQ（拿不到 bot 自己的 QQ 时用）。
_FALLBACK_UIN = "10000"


@dataclass
class RssConfig:
    poll_interval_minutes: int = 30
    max_items_per_check: int = 5
    allow_private_subscribe: bool = True
    max_content_chars: int = 10000

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RssConfig":
        data = dict(data or {})
        return cls(
            poll_interval_minutes=int(data.get("poll_interval_minutes", 30) or 30),
            max_items_per_check=int(data.get("max_items_per_check", 5) or 5),
            allow_private_subscribe=bool(data.get("allow_private_subscribe", True)),
            max_content_chars=int(data.get("max_content_chars", 10000) or 10000),
        )


def load_config(ctx: Any) -> RssConfig:
    """读插件自己的 config.yaml；没有就用默认值。"""
    override = ctx.extras.get("rss_config")
    if isinstance(override, Mapping):
        return RssConfig.from_mapping(override)
    path = Path(getattr(getattr(ctx, "plugin", None), "path", Path(__file__).parent))
    config_path = path / "config.yaml"
    if not config_path.is_file():
        return RssConfig()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("[rss] 读取配置失败：%s", exc)
        return RssConfig()
    return RssConfig.from_mapping(data)


def _database_path(ctx: Any) -> Path:
    """把订阅账本放到运行数据目录，不放源码目录。"""
    override = ctx.extras.get("rss_db_path")
    if override:
        return Path(str(override))
    configured = str(getattr(ctx.config, "database_path", "data/rubenbot.db"))
    base = Path(configured).parent if configured != ":memory:" else Path("data")
    return base / "plugin_data" / "rss.sqlite3"


def render_prefix(prefix: str, feed_title: str, count: int, feed_url: str) -> str:
    """提醒前缀的占位符替换：{rss}=源标题、{count}=本轮新文章数、{url}=订阅地址。

    未知占位符原样保留；prefix 为空或全空白返回空串（不发提醒）。
    """
    if not prefix or not prefix.strip():
        return ""
    return (
        prefix.replace("{rss}", feed_title)
        .replace("{count}", str(count))
        .replace("{url}", feed_url)
    )


def _format_node(title: str, content: str, link: str, max_chars: int) -> str:
    """一篇文章拼成一个转发节点的纯文本内容：标题 + 全文 + 链接。"""
    body = content or ""
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + "…（内容过长，已截断）"
    parts = [title.strip()]
    if body.strip():
        parts.append(body.strip())
    if link.strip():
        parts.append(link.strip())
    return "\n".join(parts)


class RssService:
    """订阅管理、轮询与推送。"""

    def __init__(
        self,
        ctx: Any,
        config: RssConfig,
        store: RssStore,
        *,
        fetch_page: Any | None = None,
        clock: Any = time.time,
    ):
        self.ctx = ctx
        self.config = config
        self.store = store
        self._fetch_page = fetch_page or ctx.fetch_public_page
        self._clock = clock
        self._poll_task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------
    # 适配器
    # ------------------------------------------------------------------

    def _adapters(self, runtime: Any | None = None) -> dict:
        """拿当前可用的适配器：命令里传 runtime，轮询时用最近一次分发的。"""
        if runtime is not None:
            try:
                return dict(runtime.adapters or {})
            except Exception:  # noqa: BLE001 - 拿不到就当没有
                return {}
        try:
            current = getattr(self.ctx, "runtime", None)
        except Exception:  # noqa: BLE001
            return {}
        if current is None:
            return {}
        try:
            return dict(current.adapters or {})
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _pick_adapter(adapters: dict) -> Any | None:
        """只推 QQ：优先 snowluma，没有就拿第一个可用的。"""
        if not adapters:
            return None
        if "snowluma" in adapters:
            return adapters["snowluma"]
        return next(iter(adapters.values()))

    @staticmethod
    def _bot_uin(adapter: Any) -> str:
        uin = getattr(adapter, "self_id", None)
        return str(uin) if uin not in (None, "") else _FALLBACK_UIN

    # ------------------------------------------------------------------
    # 群管理员校验
    # ------------------------------------------------------------------

    async def _is_group_admin(self, runtime: Any, event: Any) -> bool:
        """群消息事件里 sender.role 是 owner/admin 即通过；没有就调接口查。"""
        sender = event.raw.get("sender") or {}
        role = str(sender.get("role") or "").lower()
        if role in ("owner", "admin"):
            return True
        if role == "member":
            return False
        adapter = self._pick_adapter(self._adapters(runtime))
        if adapter is None:
            return False
        call = getattr(adapter, "call_action", None)
        if not callable(call):
            return False
        try:
            info = await call(
                "get_group_member_info",
                {
                    "group_id": int(event.chat_id),
                    "user_id": int(event.sender_id),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 查不到就按非管理员处理
            logger.warning("[rss] 查群成员身份失败：%s", exc)
            return False
        # call_action 已把 OneBot 的 data 解包出来；再容错一层嵌套 data。
        if isinstance(info, dict) and isinstance(info.get("data"), dict):
            data = info["data"]
        elif isinstance(info, dict):
            data = info
        else:
            data = {}
        role = str(data.get("role") or "").lower()
        return role in ("owner", "admin")

    # ------------------------------------------------------------------
    # 订阅操作
    # ------------------------------------------------------------------

    async def subscribe(
        self, runtime: Any, event: Any, url: str, target_group: str | None = None
    ) -> str:
        """订阅一个源。私聊默认推到当前私聊；群里只认管理员，推到当前群。"""
        url = (url or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return "订阅链接不对，格式是：/rss 订阅 <链接> [群号]"
        if event.chat_type == "private":
            if not self.config.allow_private_subscribe:
                return "私聊订阅已关闭，请在 WebUI 里添加订阅"
            if target_group:
                target_type, target_id = "group", target_group
            else:
                target_type, target_id = "private", str(event.sender_id)
        else:
            if not await self._is_group_admin(runtime, event):
                return "只有群管理员能订阅"
            target_type, target_id = "group", str(event.chat_id)
        try:
            text = await fetch_feed_text(self._fetch_page, url)
            title, items = parse_feed(text)
        except Exception as exc:  # noqa: BLE001 - 抓不到/解析不出都算无效源
            logger.warning("[rss] 订阅验证失败 %s：%s", url, exc)
            return "这个源抓不到"
        if not items:
            return "这个源抓不到"
        # 首次订阅：水位直接设到最新一条，不推旧内容。
        self.store.add(
            url,
            target_type,
            target_id,
            feed_title=title,
            last_seen_guid=items[0]["guid"],
            created_by=str(event.sender_id),
        )
        self.store.set_title(url, title)
        where = f"群 {target_id}" if target_type == "group" else "这里"
        return f"已订阅「{title}」，新文章会推到{where}"

    def unsubscribe(
        self, runtime: Any, event: Any, url: str, target_group: str | None = None
    ) -> str:
        url = (url or "").strip()
        if event.chat_type == "private":
            if target_group:
                target_type, target_id = "group", target_group
            else:
                target_type, target_id = "private", str(event.sender_id)
        else:
            target_type, target_id = "group", str(event.chat_id)
        if self.store.remove(url, target_type, target_id):
            return "已取消订阅"
        return "没找到这条订阅"

    def list_subscriptions(self, event: Any) -> str:
        """列当前会话相关的订阅。私聊列自己订阅的；群里列该群的。"""
        if event.chat_type == "private":
            rows = self.store.list(created_by=str(event.sender_id))
        else:
            rows = self.store.list(target_type="group", target_id=str(event.chat_id))
        if not rows:
            return "这里还没有订阅，用 /rss 订阅 <链接> 添加"
        lines = ["当前订阅："]
        for row in rows:
            title = row["feed_title"] or row["feed_url"]
            where = f"群 {row['target_id']}" if row["target_type"] == "group" else "私聊"
            checked = row["last_checked_at"]
            when = (
                time.strftime("%m-%d %H:%M", time.localtime(checked)) if checked else "还没检查过"
            )
            warn = (
                f"（抓取失败 {row['fail_count']} 次）"
                if row["fail_count"] >= FAIL_THRESHOLD
                else ""
            )
            prefix = (row.get("prefix") or "").strip()
            suffix = f" 提醒「{prefix}」" if prefix else ""
            lines.append(f"- {title} → {where}，上次检查 {when}{warn}{suffix}")
        return "\n".join(lines)

    async def add_subscription(
        self,
        url: str,
        target_type: str,
        target_id: str,
        *,
        prefix: str = "",
        created_by: str = "",
    ) -> dict:
        """WebUI 订阅：先验证源有效，再入库；水位设到最新一条，不推旧内容。

        源无效时抛 ValueError（"这个源抓不到"），和指令订阅同一路验证。
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("订阅链接不对")
        try:
            text = await fetch_feed_text(self._fetch_page, url)
            title, items = parse_feed(text)
        except Exception as exc:  # noqa: BLE001 - 抓不到/解析不出都算无效源
            logger.warning("[rss] 订阅验证失败 %s：%s", url, exc)
            raise ValueError("这个源抓不到") from exc
        if not items:
            raise ValueError("这个源抓不到")
        self.store.add(
            url,
            target_type,
            target_id,
            feed_title=title,
            last_seen_guid=items[0]["guid"],
            created_by=created_by,
            prefix=prefix,
        )
        self.store.set_title(url, title)
        row = self.store.get(url, target_type, target_id)
        assert row is not None  # 刚写入的行一定在
        return row

    # ------------------------------------------------------------------
    # 推送（合并转发）
    # ------------------------------------------------------------------

    async def _send_prefix(self, adapter: Any, target_type: str, target_id: str, text: str) -> bool:
        """在合并转发之前发一条纯文本提醒；失败只记 warning，绝不抛异常。"""
        if target_type == "group":
            action, key = "send_group_msg", "group_id"
        else:
            action, key = "send_private_msg", "user_id"
        try:
            target_num = int(target_id)
        except (TypeError, ValueError):
            logger.warning("[rss] 目标 id 不是数字，跳过提醒：%s", target_id)
            return False
        call = getattr(adapter, "call_action", None)
        if not callable(call):
            logger.warning("[rss] 适配器没有 call_action，发不出提醒")
            return False
        try:
            await call(action, {key: target_num, "message": text})
        except Exception as exc:  # noqa: BLE001 - 提醒失败不阻塞转发
            logger.warning("[rss] 提醒发送失败：%s", exc)
            return False
        return True

    async def _send_forward(
        self,
        adapter: Any,
        target_type: str,
        target_id: str,
        feed_title: str,
        items: list[dict],
    ) -> bool:
        """把一轮的新文章包成一条合并转发；单条也走转发。"""
        bot_uin = self._bot_uin(adapter)
        nodes = []
        for item in items:
            content = _format_node(
                item["title"],
                item["content"],
                item["link"],
                self.config.max_content_chars,
            )
            nodes.append(
                {
                    "type": "node",
                    "data": {"name": feed_title, "uin": bot_uin, "content": content},
                }
            )
        if target_type == "group":
            action = "send_group_forward_msg"
            key = "group_id"
        else:
            action = "send_private_forward_msg"
            key = "user_id"
        try:
            target_num = int(target_id)
        except (TypeError, ValueError):
            logger.warning("[rss] 目标 id 不是数字，跳过推送：%s", target_id)
            return False
        params = {key: target_num, "messages": nodes}
        call = getattr(adapter, "call_action", None)
        if not callable(call):
            logger.warning("[rss] 适配器没有 call_action，发不出合并转发")
            return False
        try:
            await call(action, params)
        except Exception as exc:  # noqa: BLE001 - 发送失败留待下轮重试
            logger.warning("[rss] 合并转发发送失败：%s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------

    async def check_all(self, runtime: Any | None = None) -> int:
        """跑一轮：每个源只抓一次，多目标共享结果；返回新推的文章数。"""
        adapters = self._adapters(runtime)
        adapter = self._pick_adapter(adapters)
        subs = self.store.list()
        if not subs:
            return 0
        by_feed: dict[str, list[dict]] = {}
        for row in subs:
            by_feed.setdefault(row["feed_url"], []).append(row)
        pushed = 0
        for feed_url, rows in by_feed.items():
            try:
                text = await fetch_feed_text(self._fetch_page, feed_url)
                title, items = parse_feed(text)
            except Exception as exc:  # noqa: BLE001 - 单源失败不影响其他源
                logger.warning("[rss] 抓取失败 %s：%s", feed_url, exc)
                for row in rows:
                    self.store.update_after_check(
                        feed_url, row["target_type"], row["target_id"], failed=True
                    )
                continue
            self.store.set_title(feed_url, title)
            if not items:
                for row in rows:
                    self.store.update_after_check(feed_url, row["target_type"], row["target_id"])
                continue
            newest_guid = items[0]["guid"]
            for row in rows:
                fresh = new_entries(items, row["last_seen_guid"], self.config.max_items_per_check)
                if not fresh:
                    self.store.update_after_check(
                        feed_url,
                        row["target_type"],
                        row["target_id"],
                        last_seen_guid=newest_guid,
                    )
                    continue
                if adapter is None:
                    logger.warning("[rss] 没有可用适配器，跳过本轮推送")
                    continue
                # 有提醒前缀：先发纯文本提醒，再发合并转发；提醒失败不阻塞转发。
                prefix_text = render_prefix(row.get("prefix") or "", title, len(fresh), feed_url)
                if prefix_text:
                    await self._send_prefix(
                        adapter, row["target_type"], row["target_id"], prefix_text
                    )
                ok = await self._send_forward(
                    adapter, row["target_type"], row["target_id"], title, fresh
                )
                if ok:
                    pushed += len(fresh)
                    self.store.update_after_check(
                        feed_url,
                        row["target_type"],
                        row["target_id"],
                        last_seen_guid=newest_guid,
                    )
                # 发送失败不推水位，下轮重试。
        return pushed

    async def _poll_loop(self) -> None:
        while not self._stopping:
            interval = max(1, int(self.config.poll_interval_minutes or 30)) * 60
            await asyncio.sleep(interval)
            if self._stopping:
                break
            try:
                pushed = await self.check_all()
                if pushed:
                    logger.info("[rss] 本轮推送 %s 篇新文章", pushed)
            except Exception as exc:  # noqa: BLE001 - 轮询异常不能杀掉任务
                logger.warning("[rss] 轮询异常：%s", exc)

    def start(self) -> None:
        """拉起后台轮询任务。"""
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stopping = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[rss] 没有事件循环，轮询任务未启动")
            return
        self._poll_task = loop.create_task(self._poll_loop())
        logger.info("[rss] 轮询已启动，间隔 %s 分钟", self.config.poll_interval_minutes)

    async def stop(self) -> None:
        """停掉后台轮询任务。"""
        self._stopping = True
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    async def command(self, runtime: Any, event: Any, args: str) -> str:
        """处理 /rss 指令。返回字符串由宿主代为回复。"""
        parts = (args or "").split()
        if not parts:
            return self._usage()
        sub = parts[0]
        rest = parts[1:]
        if sub == "订阅":
            if not rest:
                return "格式是：/rss 订阅 <链接> [群号]"
            target_group = rest[1] if len(rest) > 1 else None
            if event.chat_type == "group" and target_group:
                return "群里订阅直接推到本群，不用再写群号"
            return await self.subscribe(runtime, event, rest[0], target_group)
        if sub == "取消":
            if not rest:
                return "格式是：/rss 取消 <链接> [群号]"
            if event.chat_type == "group":
                if not await self._is_group_admin(runtime, event):
                    return "只有群管理员能取消订阅"
                return self.unsubscribe(runtime, event, rest[0])
            target_group = rest[1] if len(rest) > 1 else None
            return self.unsubscribe(runtime, event, rest[0], target_group)
        if sub == "列表":
            return self.list_subscriptions(event)
        if sub == "检查":
            if event.chat_type == "group" and not await self._is_group_admin(runtime, event):
                return "只有群管理员能手动检查"
            pushed = await self.check_all(runtime)
            return f"已检查，新推 {pushed} 条"
        return self._usage()

    @staticmethod
    def _usage() -> str:
        return (
            "用法：\n"
            "/rss 订阅 <链接> [群号] —— 订阅 RSS，推到当前会话或指定群\n"
            "/rss 取消 <链接> [群号] —— 取消订阅\n"
            "/rss 列表 —— 看当前会话的订阅\n"
            "/rss 检查 —— 立刻检查一次"
        )


def register(ctx: Any) -> None:
    """插件入口。"""
    config = load_config(ctx)
    store = RssStore(_database_path(ctx))
    service = RssService(ctx, config, store)
    ctx.extras["rss"] = service

    @ctx.command("rss")
    async def rss_command(runtime: Any, event: Any, args: str) -> str:
        return await service.command(runtime, event, args)

    @ctx.on_start
    async def _start() -> None:
        service.start()

    @ctx.on_stop
    async def _stop() -> None:
        await service.stop()

    @ctx.on_config_change
    def _on_config_change(_ctx: Any, _old: Any, new: Any) -> None:
        service.config = RssConfig.from_mapping(new)
        logger.info(
            "[rss] 配置已更新，轮询间隔 %s 分钟",
            service.config.poll_interval_minutes,
        )

    logger.debug("[rss] 已注册（轮询间隔 %s 分钟）", config.poll_interval_minutes)
