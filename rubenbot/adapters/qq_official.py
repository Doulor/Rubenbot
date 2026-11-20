"""QQ 官方机器人适配器（q.qq.com 开放平台）。

这是与 SnowLuma（OneBot v11）完全不同的一条通道：

- REST：拿 access token（约 7200 秒有效，提前刷新）、发消息、上传媒体
- WebSocket 网关：Hello → Identify → Dispatch；心跳、断线重连、Resume

接口细节均按官方文档核对（2026-11，https://bot.q.qq.com/wiki/develop/api-v2/）：

- access token：``POST https://api.bot.qq.com/app/getAppAccessToken``，
  请求 ``{"appId", "clientSecret"}``，返回 ``{"access_token", "expires_in"}``
  （秒，约 7200）；鉴权头 ``Authorization: QQBot {access_token}``。
  来源：dev-prepare/access-token.html
- REST 域名：正式 ``https://api.sgroup.qq.com``，沙箱
  ``https://sandbox.api.sgroup.qq.com``（官方 Python SDK botpy 的
  ``Route.DOMAIN`` / ``Route.SANDBOX_DOMAIN``）。
- 网关：``GET /gateway`` 返回 ``{"url": "wss://..."}``。
  来源：openapi/wss/url_get.html
- OpCode：0 Dispatch、1 Heartbeat、2 Identify、6 Resume、7 Reconnect、
  9 Invalid Session、10 Hello、11 Heartbeat ACK。
  来源：dev-prepare/interface-framework/opcode.html
- Identify 的 ``d``：``{"token": "QQBot {token}", "intents": ..., "shard": [0, 1]}``；
  Resume 的 ``d``：``{"token", "session_id", "seq"}``。
  来源：dev-prepare/interface-framework/event-emit.html
- 事件：``C2C_MESSAGE_CREATE``（单聊）与 ``GROUP_AT_MESSAGE_CREATE``（群聊被 @），
  两者都在 ``GROUP_AND_C2C_EVENT (1 << 25)`` intent 下。
  字段：``id``、``content``、``timestamp``、``attachments[]``、``group_openid``；
  单聊作者 ``author.user_openid``，群聊作者 ``author.member_openid`` /
  ``member_nickname`` / ``username``。
  来源：autogen/event/c2c_message_create.html、autogen/event/group_at_message_create.html
- 发消息：``POST /v2/users/{user_openid}/messages``、
  ``POST /v2/groups/{group_openid}/messages``；body 含 ``msg_type``（0 文本、
  7 富媒体）、``content``、``msg_id``（被动回复）、``msg_seq``、``media.file_info``。
  来源：autogen/api/v2_users_user_openid_messages.post.html、
  autogen/api/v2_groups_group_openid_messages.post.html
- 媒体上传：``POST /v2/users/{user_openid}/files``、
  ``POST /v2/groups/{group_openid}/files``；``file_type=1``（图片），
  ``url`` 或 ``file_data``（base64，二进制上限 5M），``srv_send_msg=false``
  时只返回 ``file_info``（有时效，过期需重新上传）。
  来源：autogen/api/v2_users_user_openid_files.post.html
- 被动回复：同一 ``msg_id`` 可回复多次，但必须携带递增的 ``msg_seq``；
  相同 ``msg_id + msg_seq`` 重复发送会失败。群聊 5 分钟 / 最多 5 次，
  单聊 60 分钟 / 最多 4 次。
  来源：server-inter/message/overview.html（单聊接口页写的是 msg_id 5 分钟内
  有效，与概述页不一致；这里按概述页的宽松值记窗口，超时/超额由本地判定后
  直接给出明确失败原因，不静默改发主动消息）。

设计取舍：

- 出站必须借最近的入站 ``msg_id`` 做被动回复，因此按会话记住最近一条消息；
  超过窗口或次数上限时抛 :class:`QQOfficialError`（RuntimeError 子类），
  沿用 core 现有的「send 失败只记日志」语义，不会卡死主循环。
- 一条消息里既有文字又有图片时，按原顺序拆成多条（QQ 官方没有图文混排的
  纯文本消息类型），每条各自占用一个 ``msg_seq``。
- 入站图片附件默认下载成 base64 放进 image 段（``download_images=False`` 可关），
  下载失败时保留 url，不影响消息进入主循环。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

try:  # websockets 13+ 的新版客户端 API
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 兼容旧版
    from websockets.client import connect as ws_connect  # type: ignore

from .base import (
    SEND_STICKER,
    BaseAdapter,
    CapabilityError,
    CapabilityNotSupported,
    EventCallback,
    MessageEvent,
    Segment,
    as_segment,
    render_segments,
)

logger = logging.getLogger(__name__)

# 网关 opcode（见模块文档）
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# GROUP_AND_C2C_EVENT (1 << 25)：C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE 都在这下面
INTENT_GROUP_AND_C2C = 1 << 25

MESSAGE_EVENTS = frozenset({"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"})

# 被动回复窗口与次数（来源见模块文档「被动回复」）
PASSIVE_WINDOW_SECONDS: dict[str, float] = {"group": 300.0, "private": 3600.0}
PASSIVE_REPLY_LIMIT: dict[str, int] = {"group": 5, "private": 4}

_TOKEN_REFRESH_MARGIN = 60.0
_TOKEN_BACKOFF_MIN = 1.0
_TOKEN_BACKOFF_MAX = 60.0

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0

# 图片走 base64 上传时官方限制二进制 5M 以内
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 入站图片下载超时：下载慢不能把事件处理卡太久
_IMAGE_DOWNLOAD_TIMEOUT = 10.0

DEFAULT_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"

# 与被驳回的被动回复相关的错误码，给失败原因补一句人话
_ERROR_HINTS = {
    304103: "消息 ID 已过期，不能回复",
    40034005: "回复消息 msg_id 已过期",
    40034128: "被动回复时间或次数超限",
    40034024: "msg_id 无效或越权",
    40054005: "消息被去重（msg_seq 需递增）",
    40054002: "机器人被禁言",
    40054003: "机器人不是群成员",
}


class QQOfficialError(RuntimeError):
    """QQ 官方适配器的可预期失败（发送、配置、被动回复超限等）。"""


class QQTokenError(QQOfficialError):
    """access token 获取/刷新失败。"""


class QQAPIError(QQOfficialError):
    """REST 接口返回业务错误。"""

    def __init__(self, code: Any, message: str, *, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        text = f"QQ 官方 API 错误 {code}: {message}"
        hint = _ERROR_HINTS.get(code) if isinstance(code, int) else None
        if hint:
            text += f"（{hint}）"
        super().__init__(text)


@dataclass
class _PassiveReply:
    """某个会话最近一条入站消息的被动回复状态。"""

    msg_id: str
    received_at: float  # time.monotonic()
    used: int = 0  # 已经用掉的 msg_seq 个数


class QQOfficialAdapter(BaseAdapter):
    """QQ 官方机器人：REST + WebSocket 网关。"""

    name = "qq_official"

    # QQ 官方通道没有原生表情接口：send_sticker 按图片兜底（msg_type=7）
    capabilities = frozenset({SEND_STICKER})
    capability_config_keys = {SEND_STICKER: "sticker"}

    def __init__(
        self,
        app_id: str = "",
        client_secret: str = "",
        sandbox: bool = False,
        *,
        api_base: str | None = None,
        token_url: str = DEFAULT_TOKEN_URL,
        intents: int = INTENT_GROUP_AND_C2C,
        reconnect: bool = True,
        download_images: bool = True,
        http_client: httpx.AsyncClient | None = None,
        passive_window: Mapping[str, float] | None = None,
        passive_limit: Mapping[str, int] | None = None,
    ):
        self.app_id = str(app_id)
        self.client_secret = str(client_secret)
        self.sandbox = bool(sandbox)
        self.api_base = (api_base or (SANDBOX_API_BASE if sandbox else API_BASE)).rstrip("/")
        self.token_url = token_url
        self.intents = int(intents)
        self.reconnect = reconnect
        self.download_images = download_images

        self.passive_window = {**PASSIVE_WINDOW_SECONDS, **dict(passive_window or {})}
        self.passive_limit = {**PASSIVE_REPLY_LIMIT, **dict(passive_limit or {})}

        self._client = http_client
        self._owns_client = http_client is None

        self._stop = asyncio.Event()
        self._on_event: EventCallback | None = None
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None

        # token 缓存与刷新退避
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_retry_at = 0.0
        self._token_backoff = _TOKEN_BACKOFF_MIN
        self._token_error = ""
        self._token_lock = asyncio.Lock()

        # 网关会话（Resume 用）
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._shard: list[int] = [0, 1]
        self._reconnect_requested = False

        # (chat_type, chat_id) -> 被动回复状态
        self._reply_state: dict[tuple[str, str], _PassiveReply] = {}

        # 统计信息，便于调试
        self.reconnect_count = 0
        self.event_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any) -> "QQOfficialAdapter":
        """从 :class:`rubenbot.config.Config` 的 ``adapters.qq_official`` 段构造。

        ``config`` 只要求有 ``qq_official_options()``（不导入 config 模块，避免环）。
        """
        options = config.qq_official_options()
        return cls(
            app_id=options.app_id,
            client_secret=options.client_secret,
            sandbox=options.sandbox,
            download_images=options.download_images,
        )

    async def start(self, on_event: EventCallback) -> None:
        """连接网关并持续收事件；返回意味着已 stop。"""
        self._on_event = on_event
        self._stop.clear()
        delay = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                await self._run_once()
                delay = _RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/鉴权错误都要重试
                if self._stop.is_set():
                    break
                logger.warning("QQ 官方网关断开：%s（%.0fs 后重连）", exc, delay)
            if not self.reconnect or self._stop.is_set():
                break
            self.reconnect_count += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, _RECONNECT_MAX)

    async def _run_once(self) -> None:
        url = await self._gateway_url()
        self._reconnect_requested = False
        async with ws_connect(url, ping_interval=None, max_size=None) as ws:
            self._ws = ws
            logger.info("QQ 官方网关已连接：%s", url)
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    try:
                        await self._handle_payload(raw)
                    except QQTokenError:
                        # 心跳/鉴权拿不到 token：这条连接已经废了，交给外层退避重连
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单条事件失败不能断连接
                        logger.warning("处理 QQ 官方网关消息失败：%s", exc)
                    if self._reconnect_requested:
                        break
            finally:
                self._ws = None
                await self._stop_heartbeat()
                # 4004 = 鉴权失败，丢掉 token 下次强制刷新
                if getattr(ws, "close_code", None) == 4004:
                    self._invalidate_token()

    async def stop(self) -> None:
        self._stop.set()
        await self._stop_heartbeat()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        # 主动退出后下次 start 走完整 Identify，而不是拿旧会话 Resume
        self._session_id = None
        self._last_seq = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # access token
    # ------------------------------------------------------------------

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

    async def _ensure_token(self) -> str:
        """取有效 token；过期前 60 秒刷新，刷新失败时按指数退避。"""
        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
                return self._token
            # 旧 token 还没到期就算可用；刷新失败时优先用它撑过退避期
            usable = self._token if self._token and now < self._token_expires_at else None
            if now < self._token_retry_at:
                if usable is not None:
                    return usable
                raise QQTokenError(
                    f"access token 刷新失败后退避中（还剩 "
                    f"{self._token_retry_at - now:.0f} 秒）：{self._token_error}"
                )
            try:
                token, expires_in = await self._fetch_token()
            except Exception as exc:  # noqa: BLE001 - 网络/业务错误统一处理
                delay = self._token_backoff
                self._token_backoff = min(self._token_backoff * 2, _TOKEN_BACKOFF_MAX)
                self._token_retry_at = time.monotonic() + delay
                self._token_error = str(exc)
                if usable is not None:
                    # 旧 token 尚未过期：继续用，别因为一次刷新失败打断发送
                    logger.warning(
                        "刷新 access token 失败，继续使用未过期的旧 token"
                        "（%.0fs 后重试）：%s",
                        delay,
                        exc,
                    )
                    return usable
                raise QQTokenError(
                    f"获取 access token 失败：{exc}（{delay:.0f} 秒后重试）"
                ) from exc
            self._token = token
            self._token_expires_at = now + expires_in
            self._token_retry_at = 0.0
            self._token_backoff = _TOKEN_BACKOFF_MIN
            self._token_error = ""
            return token

    async def _fetch_token(self) -> tuple[str, float]:
        client = await self._get_client()
        resp = await client.post(
            self.token_url,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        data = _json_body(resp)
        token = data.get("access_token")
        if resp.status_code >= 400 or not token:
            raise QQTokenError(
                f"HTTP {resp.status_code}：{data.get('code') or ''} {data.get('message') or data}"
            )
        try:
            expires_in = float(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200.0
        return str(token), expires_in

    # ------------------------------------------------------------------
    # 网关
    # ------------------------------------------------------------------

    async def _gateway_url(self) -> str:
        data = await self._api("GET", "/gateway")
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            raise QQOfficialError(f"网关接入点返回异常：{data}")
        return str(url)

    async def _handle_payload(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return
        op = data.get("op")
        if op == OP_HELLO:
            await self._on_hello(data.get("d") or {})
            return
        if op == OP_DISPATCH:
            # 先记 seq：断线后 Resume 用最后收到的序号，避免重复回放
            if data.get("s") is not None:
                try:
                    self._last_seq = int(data["s"])
                except (TypeError, ValueError):
                    pass
            event_type = str(data.get("t") or "")
            body = data.get("d") or {}
            if event_type == "READY":
                self._session_id = str(body.get("session_id") or "") or None
                shard = body.get("shard")
                if isinstance(shard, list) and len(shard) == 2:
                    self._shard = [int(shard[0]), int(shard[1])]
                logger.info("QQ 官方网关就绪：session=%s shard=%s", self._session_id, self._shard)
            elif event_type == "RESUMED":
                logger.info("QQ 官方网关会话已恢复")
            elif event_type in MESSAGE_EVENTS:
                await self._dispatch_message(event_type, body)
            return
        if op == OP_HEARTBEAT_ACK:
            return
        if op == OP_RECONNECT:
            logger.info("QQ 官方网关要求重连")
            self._reconnect_requested = True
            return
        if op == OP_INVALID_SESSION:
            # Identify / Resume 参数有误：丢弃会话，下次重新 Identify
            logger.warning("QQ 官方网关会话失效，重新鉴权")
            self._session_id = None
            self._last_seq = None
            self._reconnect_requested = True
            return
        logger.debug("忽略未知 opcode：%s", op)

    async def _on_hello(self, data: dict[str, Any]) -> None:
        interval_ms = data.get("heartbeat_interval") or 30000
        try:
            interval = max(0.1, float(interval_ms) / 1000.0)
        except (TypeError, ValueError):
            interval = 30.0
        await self._send_connection_payload()
        self._start_heartbeat(interval)

    async def _send_connection_payload(self) -> None:
        """有会话就 Resume，否则 Identify。"""
        token = await self._ensure_token()
        auth = f"QQBot {token}"
        if self._session_id and self._last_seq is not None:
            payload = {
                "op": OP_RESUME,
                "d": {"token": auth, "session_id": self._session_id, "seq": self._last_seq},
            }
        else:
            payload = {
                "op": OP_IDENTIFY,
                "d": {"token": auth, "intents": self.intents, "shard": list(self._shard)},
            }
        await self._send_json(payload)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise QQOfficialError("网关未连接")
        await ws.send(json.dumps(payload, ensure_ascii=False))

    def _start_heartbeat(self, interval: float) -> None:
        self._stop_heartbeat_sync()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    def _stop_heartbeat_sync(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停止时任何异常都忽略
            pass

    async def _heartbeat_loop(self, interval: float) -> None:
        """按 Hello 给的周期发 op 1，d 为最近收到的 seq（首次为 null）。"""
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send_json({"op": OP_HEARTBEAT, "d": self._last_seq})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接没了交给外层重连
                logger.warning("QQ 官方网关心跳发送失败：%s", exc)
                return

    # ------------------------------------------------------------------
    # 事件解析
    # ------------------------------------------------------------------

    async def _dispatch_message(self, event_type: str, body: dict[str, Any]) -> None:
        event = parse_qq_official_event(event_type, body)
        if event is None:
            return
        self.event_count += 1
        await self._enrich_images(event)
        self._remember_inbound(event)
        if self._on_event is not None:
            await self._on_event(event)

    async def _enrich_images(self, event: MessageEvent) -> None:
        """把图片附件下载成 base64 塞进 image 段；失败保留 url。"""
        if not self.download_images:
            return
        images = [segment for segment in event.segments if segment.type == "image"]
        if not images:
            return
        client = await self._get_client()
        for segment in images:
            url = str(segment.data.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            try:
                resp = await client.get(url, timeout=_IMAGE_DOWNLOAD_TIMEOUT)
                resp.raise_for_status()
                content = resp.content
                if not content:
                    continue
                if len(content) > _MAX_IMAGE_BYTES:
                    logger.info("跳过过大的图片附件（%s 字节）：%s", len(content), url)
                    continue
                segment.data["base64"] = base64.b64encode(content).decode("ascii")
            except Exception as exc:  # noqa: BLE001 - 下载失败不影响消息处理
                logger.debug("下载附件图片失败（%s）：%s", url, exc)

    def _remember_inbound(self, event: MessageEvent) -> None:
        msg_id = str((event.raw or {}).get("_reply_message_id") or "")
        if not msg_id:
            return
        key = (event.chat_type, event.chat_id)
        self._reply_state[key] = _PassiveReply(msg_id=msg_id, received_at=time.monotonic())
        if len(self._reply_state) > 500:
            # 简单裁剪：丢掉已经超出窗口的旧会话
            now = time.monotonic()
            self._reply_state = {
                key: state
                for key, state in self._reply_state.items()
                if now - state.received_at <= max(self.passive_window.values(), default=3600.0)
            }

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, segments: list[Segment], *, chat_type: str = "group") -> None:
        """被动回复一条消息；超窗或超额时抛 :class:`QQOfficialError`。

        QQ 官方要求带上最近一条入站消息的 ``msg_id`` 才能走被动回复；
        纯文本发送失败时 core 已按现有语义记日志继续，不会卡死主循环。
        """
        chat_type = chat_type or "group"
        state = self._reply_state.get((chat_type, chat_id))
        if state is None:
            raise QQOfficialError(
                f"无法发送：会话 {chat_id} 还没收到过消息，QQ 官方通道只支持被动回复"
            )
        window = self.passive_window.get(chat_type, PASSIVE_WINDOW_SECONDS["group"])
        elapsed = time.monotonic() - state.received_at
        if elapsed > window:
            raise QQOfficialError(
                f"被动回复已超时：距最近一条消息 {elapsed:.0f} 秒，"
                f"超过 {window:.0f} 秒有效期（chat_type={chat_type}）"
            )

        parts = split_outgoing_segments(segments)
        if not parts:
            return
        for kind, payload in parts:
            if kind == "text":
                seq = self._next_seq(state, chat_type)
                await self._post_message(
                    chat_id,
                    chat_type,
                    {"msg_type": 0, "content": payload, "msg_id": state.msg_id, "msg_seq": seq},
                )
            else:
                # 先上传再占 seq：上传失败时不浪费一次被动回复额度
                file_info = await self._upload_media(chat_id, chat_type, payload)
                seq = self._next_seq(state, chat_type)
                await self._post_message(
                    chat_id,
                    chat_type,
                    {
                        "msg_type": 7,
                        "media": {"file_info": file_info},
                        "msg_id": state.msg_id,
                        "msg_seq": seq,
                    },
                )

    def _next_seq(self, state: _PassiveReply, chat_type: str) -> int:
        limit = self.passive_limit.get(chat_type, PASSIVE_REPLY_LIMIT["group"])
        if state.used >= limit:
            raise QQOfficialError(
                f"被动回复次数已用完：同一 msg_id 最多回复 {limit} 次"
                f"（chat_type={chat_type}）"
            )
        state.used += 1
        return state.used

    async def _post_message(self, chat_id: str, chat_type: str, body: dict[str, Any]) -> dict[str, Any]:
        path = (
            f"/v2/users/{chat_id}/messages"
            if chat_type == "private"
            else f"/v2/groups/{chat_id}/messages"
        )
        return await self._api("POST", path, body)

    async def _upload_media(self, chat_id: str, chat_type: str, data: dict[str, Any]) -> str:
        """上传图片拿 file_info（file_type=1，srv_send_msg=false）。"""
        body = media_upload_payload(data)
        path = (
            f"/v2/users/{chat_id}/files" if chat_type == "private" else f"/v2/groups/{chat_id}/files"
        )
        result = await self._api("POST", path, body)
        file_info = result.get("file_info") if isinstance(result, dict) else None
        if not file_info:
            raise QQOfficialError(f"上传媒体失败：返回里没有 file_info：{result}")
        return str(file_info)

    # ------------------------------------------------------------------
    # 平台能力
    # ------------------------------------------------------------------

    async def call_capability(self, name: str, **kwargs: Any) -> Any:
        """QQ 官方通道只额外提供 send_sticker（图片兜底）；其余能力不支持。"""
        if name != SEND_STICKER:
            raise CapabilityNotSupported(self.name, name)
        sticker = kwargs.get("sticker")
        data = sticker_image_data(sticker)
        if data is None:
            raise CapabilityError("send_sticker 缺少可用的图片文件或 URL")
        chat_id = kwargs.get("chat_id")
        if chat_id in (None, ""):
            raise CapabilityError("send_sticker 需要 chat_id")
        chat_type = str(kwargs.get("chat_type") or "group")
        try:
            # 走与普通图片相同的发送路径：先上传拿 file_info，再被动回复
            await self.send(str(chat_id), [Segment("image", data)], chat_type=chat_type)
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - 能力失败收敛成统一异常
            raise CapabilityError(f"send_sticker 失败：{exc}") from exc
        # QQ 官方不回原生表情引用，返回值只表示「已发出」
        return {"sent": True}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.api_base, timeout=20.0)
            self._owns_client = True
        return self._client

    async def _api(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """带鉴权的 REST 调用；业务错误统一转 :class:`QQAPIError`。"""
        token = await self._ensure_token()
        client = await self._get_client()
        headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": self.app_id}
        try:
            resp = await client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise QQOfficialError(f"请求 {path} 失败：{exc}") from exc
        data = _json_body(resp)
        if resp.status_code == 401:
            # token 失效：下次调用强制刷新
            self._invalidate_token()
        if resp.status_code >= 400 or _looks_like_error(data):
            raise QQAPIError(
                data.get("code", resp.status_code),
                str(data.get("message") or data),
                status=resp.status_code,
            )
        return data


# ----------------------------------------------------------------------
# 纯函数（便于单测）
# ----------------------------------------------------------------------


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:  # 含 json.JSONDecodeError
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _looks_like_error(data: dict[str, Any]) -> bool:
    """QQ 部分错误用 200 返回，形状是 ``{"code", "message"}`` 且没有成功字段。"""
    if "code" not in data or "message" not in data:
        return False
    return not any(key in data for key in ("id", "url", "file_info", "access_token"))


def _is_image_attachment(attachment: Mapping[str, Any]) -> bool:
    content_type = str(attachment.get("content_type") or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = str(attachment.get("filename") or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def parse_qq_official_event(
    event_type: str,
    data: Mapping[str, Any],
) -> MessageEvent | None:
    """C2C / 群 @ 事件 -> 统一 :class:`MessageEvent`；其他事件返回 None。

    群聊的 ``GROUP_AT_MESSAGE_CREATE`` 本身语义就是「@ 了机器人」，因此
    ``mentions_bot`` 恒为 True；私聊全部收下，同样置 True。
    事件 ``id`` 主要用于被动回复，放进 ``raw["_reply_message_id"]``。
    """
    if event_type not in MESSAGE_EVENTS or not isinstance(data, Mapping):
        return None
    author = data.get("author") or {}
    if not isinstance(author, Mapping):
        author = {}
    if author.get("bot") is True:
        # 机器人自己（或别的机器人）的消息一律忽略，避免自问自答的死循环
        return None
    msg_id = data.get("id")
    content = str(data.get("content") or "")

    segments: list[Segment] = []
    if content:
        segments.append(Segment.text(content))
    attachments = data.get("attachments") or []
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, Mapping) or not _is_image_attachment(attachment):
                continue
            url = str(attachment.get("url") or "")
            if not url:
                continue
            image_data: dict[str, Any] = {"url": url}
            filename = attachment.get("filename")
            if filename:
                image_data["filename"] = str(filename)
            content_type = attachment.get("content_type")
            if content_type:
                image_data["content_type"] = str(content_type)
            segments.append(Segment("image", image_data))

    if event_type == "C2C_MESSAGE_CREATE":
        openid = author.get("user_openid") or author.get("id")
        if openid in (None, ""):
            return None
        chat_type = "private"
        chat_id = str(openid)
        sender_id = str(openid)
        sender_name = str(author.get("username") or openid)
    else:
        group_openid = data.get("group_openid")
        member_openid = author.get("member_openid") or author.get("id")
        if group_openid in (None, "") or member_openid in (None, ""):
            return None
        chat_type = "group"
        chat_id = str(group_openid)
        sender_id = str(member_openid)
        sender_name = str(
            author.get("member_nickname") or author.get("username") or member_openid
        )

    return MessageEvent(
        platform="qq_official",
        chat_type=chat_type,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=render_segments(segments).strip(),
        reply_to_id=None,
        mentions_bot=True,
        raw={
            **dict(data),
            "_event_type": event_type,
            "_reply_message_id": str(msg_id) if msg_id is not None else "",
        },
        segments=segments,
    )


def split_outgoing_segments(
    segments: list[Segment] | list[Mapping[str, Any]],
) -> list[tuple[str, Any]]:
    """统一消息段 -> QQ 官方发送批次：``("text", 文本)`` / ``("image", 段数据)``。

    连续的文本（含无法直发的段，投影成占位符）合并成一条文本消息；图片单独
    成一条富媒体消息。at/reply 在 QQ 官方通道没有对应发送段，直接跳过。
    """
    parts: list[tuple[str, Any]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "".join(buffer)
        buffer.clear()
        if text.strip():
            parts.append(("text", text))

    for raw in segments or []:
        segment = as_segment(raw)
        if segment.type == "image":
            if media_upload_payload(segment.data) is None:
                # 没有可用来源时退化成占位文本，别把消息整个丢掉
                buffer.append(segment.render())
                continue
            flush()
            parts.append(("image", dict(segment.data)))
        elif segment.type in ("at", "reply"):
            continue
        elif segment.type == "text":
            buffer.append(str(segment.data.get("text", "")))
        else:
            buffer.append(segment.render())
    flush()
    return parts


def media_upload_payload(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """图片段 -> ``/files`` 上传请求体；没有可用来源时返回 None。

    支持 url、base64（``base64://`` / ``data:`` 前缀均可）、本地 file 路径。
    """
    base: dict[str, Any] = {"file_type": 1, "srv_send_msg": False}
    url = data.get("url")
    if url:
        return {**base, "url": str(url)}
    raw = data.get("base64")
    if raw:
        value = str(raw)
        if value.startswith("base64://"):
            value = value[len("base64://") :]
        elif value.startswith("data:"):
            _, _, value = value.partition(",")
        name = str(data.get("filename") or data.get("name") or "image.png")
        return {**base, "file_data": value, "file_name": name}
    path = data.get("file")
    if path:
        file_path = Path(str(path))
        try:
            size = file_path.stat().st_size
        except OSError:
            return None
        if size > _MAX_IMAGE_BYTES:
            raise QQOfficialError(f"图片超过 5M 限制，无法上传：{file_path}（{size} 字节）")
        try:
            content = file_path.read_bytes()
        except OSError:
            return None
        name = str(data.get("filename") or file_path.name or "image.png")
        return {
            **base,
            "file_data": base64.b64encode(content).decode("ascii"),
            "file_name": name,
        }
    return None


def sticker_image_data(sticker: Any) -> dict[str, Any] | None:
    """表情引用 -> image 段数据（``url`` / ``base64`` / ``file`` 之一）。

    QQ 官方没有原生表情接口，只能当图片发；表情库的 ``file`` 是本地绝对
    路径，正好能交给 ``media_upload_payload`` 读成 base64 上传。
    """
    if not isinstance(sticker, Mapping):
        return None
    data: dict[str, Any] = {}
    for key in ("file", "url", "base64"):
        value = sticker.get(key)
        if value:
            data[key] = str(value)
    if not data:
        return None
    if data.get("url"):
        # url 优先，省一次本地读盘；media_upload_payload 也认
        return {"url": data["url"]}
    return data
