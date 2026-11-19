"""QQ 空间 Web 接口（SnowLuma OneBot v11 之上的扩展能力）。

OneBot v11 本身没有空间互动动作，社区通行做法是：

1. 通过 OneBot ``get_cookies`` 拿 qzone 域的登录态 Cookie（uin / skey / p_skey）；
2. 用 p_skey（没有就用 skey）按 QQ 空间的 hash33 算法算出 ``g_tk``；
3. 直接请求 ``user.qzone.qq.com`` 的 cgi-bin 接口（说说列表、点赞、评论、回复）。

接口与参数参考（只参考协议细节，代码为本项目原创；这些接口均未公开，腾讯可能随时调整）：

- Zhalslar/astrbot_plugin_qzone（core/qzone/api.py、session.py、model.py）：
  ``emotion_cgi_msglist_v6`` / ``emotion_cgi_re_feeds`` / ``internal_dolike_app``
  的端点与表单；回复评论走 PC 代理时 topicId 不带 ``__1``、``format=json``，
  用 ``commentId`` / ``replyUin`` / ``commentUin`` 指定被回复评论。
- diaomin66/astrbot_plugin_qzone_ultra（qzone_bridge/client.py、parser.py）：
  点赞的 ``unikey`` / ``curkey`` 必须是 ``http://`` 前缀（用 https 时服务端
  可能返回成功但实际不记赞）；说说列表字段（msglist/pic/commentlist/list_3）。
- SucRoseZT/astrbot_plugin_qzone_kit（qzone_api.py）：
  ``emotion_cgi_msglist_v6`` 的完整参数、评论/回复共用表单、hash33 算 g_tk。

登录态失效（HTTP 401/403、跳登录页、业务码 -3000）统一收敛成
:class:`CapabilityError`，由核心入口转成 ``(False, 原因)``，绝不打断主循环。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .base import CapabilityError

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 接口地址
# ----------------------------------------------------------------------

LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
LIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"

BASE_URL = "https://user.qzone.qq.com"
# 点赞的 unikey/curkey 必须用 http://，见模块注释
LIKE_KEY_BASE = "http://user.qzone.qq.com"

# 按此顺序向协议端要 Cookie。qzone.qq.com 在 pskey 白名单里，
# user.qzone.qq.com 在部分协议端实现上取不到 p_skey（会正常报错），所以放后面兜底。
COOKIE_DOMAINS = ("qzone.qq.com", "user.qzone.qq.com", "qq.com")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 空间接口遇到这些状态码一律不跟随（跟随到登录页会掩盖登录态问题）
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# 登录态失效的业务码与关键词（不同 CGI 的字段名不一致，code/ret/retcode 都看）
AUTH_CODES = {-3000}
AUTH_KEYWORDS = ("登录", "失效", "请先登录", "skey", "g_tk", "cookie", "expired", "login")

_CALLBACK_RE = re.compile(r"^[^(]*\(\s*(?P<body>.*?)\s*\)\s*;?\s*$", re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_EM_RE = re.compile(r"\[em\].*?\[/em\]", re.S)


class QzoneError(CapabilityError):
    """QQ 空间能力执行失败（登录态、参数、接口返回错误等）。"""


# ----------------------------------------------------------------------
# Cookie / g_tk
# ----------------------------------------------------------------------


def compute_gtk(skey: str) -> int:
    """hash33：由 p_skey / skey 计算 g_tk（Qzone 的 djb2 变体）。"""
    value = 5381
    for ch in skey or "":
        value += (value << 5) + ord(ch)
    return value & 0x7FFFFFFF


def parse_cookie_string(raw: str) -> dict[str, str]:
    """``a=1; b=2`` 形式的 Cookie 串 -> 字典；同名键保留第一个。"""
    cookies: dict[str, str] = {}
    for part in str(raw or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        value = value.strip().strip('"')
        # 没有值的键（浏览器里常见）也保留，避免丢登录态相关字段
        if key and key not in cookies:
            cookies[key] = value if sep else ""
    return cookies


def uin_from_cookies(cookies: dict[str, str]) -> str:
    """从 Cookie 里取登录 QQ 号（兼容 ``uin=o123456`` 写法）。"""
    for key in ("uin", "p_uin", "ptui_loginuin", "luin"):
        value = str(cookies.get(key) or "").strip().lstrip("oO")
        if value.isdigit():
            return value
    return ""


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


# ----------------------------------------------------------------------
# 响应解析
# ----------------------------------------------------------------------


def clean_text(value: Any) -> str:
    """说说/评论正文转纯文本：去 HTML 标签、QQ 内置表情标记、反转义实体。

    连续空格（``<br>`` 转换产生）压成一个；换行保留，方便正文分段。
    """
    if value is None:
        return ""
    text = str(value)
    text = _BR_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    text = _EM_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_qzone_payload(text: str) -> dict[str, Any]:
    """解析空间接口返回：JSON 或 JSONP（``_preloadCallback({...});``）。"""
    raw = (text or "").strip()
    if not raw:
        raise QzoneError("QQ 空间接口返回为空")
    body = raw
    match = _CALLBACK_RE.match(raw)
    if match and "{" in match.group("body"):
        body = match.group("body")
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end < start:
        raise QzoneError(f"QQ 空间接口返回无法解析：{raw[:120]}")
    candidate = body[start : end + 1].replace("undefined", "null")
    try:
        payload = json.loads(candidate)
    except ValueError as exc:
        raise QzoneError(f"QQ 空间接口返回 JSON 解析失败：{raw[:120]}") from exc
    if not isinstance(payload, dict):
        raise QzoneError("QQ 空间接口返回的 JSON 不是对象")
    return payload


def payload_code(payload: dict[str, Any]) -> int | None:
    for key in ("code", "ret", "retcode"):
        if key in payload and payload[key] not in (None, ""):
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                return None
    return None


def unwrap_payload(payload: Any) -> dict[str, Any]:
    """部分 CGI 把结果包在 ``{"code": 0, "data": {...}}`` 里，逐层剥到业务数据。"""
    while isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and len(payload) <= 3:
            payload = data
            continue
        break
    return payload if isinstance(payload, dict) else {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool_or_none(value: Any) -> bool | None:
    """``isliked`` 之类的标记：字段缺失返回 None（区分“未赞”和“不知道”）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _comment_id(node: dict[str, Any]) -> str:
    for key in ("commentid", "commentId", "tid", "id"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------


@dataclass(slots=True)
class QzoneSession:
    """一次登录态：uin、Cookie 与算好的 g_tk。"""

    uin: str
    cookies: dict[str, str]
    g_tk: int
    cookie_header: str


CookieFetcher = Callable[[str], Awaitable[str]]


class QzoneClient:
    """QQ 空间接口客户端。

    只依赖协议端提供的 ``cookie_fetcher(domain) -> Cookie 串``，与 OneBot
    实现解耦，便于单测直接注入假 HTTP 层。
    """

    def __init__(
        self,
        cookie_fetcher: CookieFetcher,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        session_ttl: float = 600.0,
    ):
        self._fetch_cookie = cookie_fetcher
        self._client = http_client
        self._owns_client = http_client is None
        self.timeout = timeout
        self.session_ttl = session_ttl
        self._session: QzoneSession | None = None
        self._session_at = 0.0
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def invalidate_session(self) -> None:
        self._session = None
        self._session_at = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
            self._owns_client = True
        return self._client

    async def session(self, *, force: bool = False) -> QzoneSession:
        """取当前登录态；缓存过期或未命中时向协议端重新要 Cookie。"""
        async with self._lock:
            now = time.monotonic()
            if not force and self._session is not None and now - self._session_at < self.session_ttl:
                return self._session
            uin = self._session.uin if self._session is not None else ""
            try:
                self._session = await self._refresh_session()
            except QzoneError:
                self._session = None
                self._session_at = 0.0
                raise
            self._session_at = time.monotonic()
            if uin and self._session.uin != uin:
                logger.info("QQ 空间登录账号已切换：%s -> %s", uin, self._session.uin)
            return self._session

    async def _refresh_session(self) -> QzoneSession:
        reasons: list[str] = []
        raw_cookie = ""
        for domain in COOKIE_DOMAINS:
            try:
                raw_cookie = str(await self._fetch_cookie(domain) or "").strip()
            except Exception as exc:  # noqa: BLE001 - 换下一个域继续试
                reasons.append(f"{domain}: {exc}")
                continue
            if raw_cookie:
                break
            reasons.append(f"{domain}: 未返回 Cookie")
        if not raw_cookie:
            detail = "；".join(reasons) or "协议端未返回 Cookie"
            raise QzoneError(f"无法获取 QQ 空间登录态（get_cookies 失败：{detail}）")

        cookies = parse_cookie_string(raw_cookie)
        uin = uin_from_cookies(cookies)
        if not uin:
            raise QzoneError("QQ 空间 Cookie 缺少 uin，无法识别登录账号")
        skey = cookies.get("p_skey") or cookies.get("skey") or ""
        if not skey:
            raise QzoneError("QQ 空间 Cookie 缺少 p_skey/skey，无法计算 g_tk")
        return QzoneSession(
            uin=uin,
            cookies=cookies,
            g_tk=compute_gtk(skey),
            cookie_header=cookie_header(cookies),
        )

    # ---- HTTP ----

    def _headers(self, session: QzoneSession, referer: str) -> dict[str, str]:
        return {
            "User-Agent": _UA,
            "Cookie": session.cookie_header,
            "Referer": referer or f"{BASE_URL}/{session.uin}",
            "Origin": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        referer: str = "",
    ) -> dict[str, Any]:
        session = await self.session()
        query = dict(params or {})
        query.setdefault("g_tk", session.g_tk)
        client = await self._get_client()
        try:
            response = await client.request(
                method,
                url,
                params=query,
                data=data,
                headers=self._headers(session, referer),
            )
        except httpx.HTTPError as exc:
            raise QzoneError(f"QQ 空间请求失败：{exc}") from exc

        location = response.headers.get("location", "")
        if response.status_code in _REDIRECT_STATUSES:
            if "ptlogin" in location.lower() or "login" in location.lower():
                self.invalidate_session()
                raise QzoneError("QQ 空间登录态失效（请求被重定向到登录页），请检查协议端 QQ 登录")
            raise QzoneError(f"QQ 空间接口跳转异常（HTTP {response.status_code}）：{location[:120]}")
        if response.status_code in (401, 403):
            self.invalidate_session()
            raise QzoneError(f"QQ 空间登录态失效或无权限（HTTP {response.status_code}）")
        if response.status_code >= 400:
            raise QzoneError(f"QQ 空间接口 HTTP {response.status_code}")

        payload = parse_qzone_payload(response.text)
        self._check_code(payload)
        # 部分 CGI 把业务数据（连同业务码）包在 data 里；统一剥一层再校验，
        # 调用方只看业务字段
        unwrapped = unwrap_payload(payload)
        if unwrapped is not payload:
            self._check_code(unwrapped)
        return unwrapped

    def _check_code(self, payload: dict[str, Any]) -> None:
        code = payload_code(payload)
        if code in (None, 0):
            return
        message = str(payload.get("message") or payload.get("msg") or "")
        lowered = message.lower()
        if code in AUTH_CODES or any(key in message or key in lowered for key in AUTH_KEYWORDS):
            self.invalidate_session()
            detail = f"：{message}" if message else ""
            raise QzoneError(f"QQ 空间登录态失效（code={code}{detail}），请检查协议端 QQ 登录")
        raise QzoneError(f"QQ 空间接口返回错误 code={code}" + (f"：{message}" if message else ""))

    # ---- 读 ----

    async def feeds(
        self,
        uin: str | int | None = None,
        *,
        num: int = 10,
        pos: int = 0,
    ) -> list[dict[str, Any]]:
        """读指定 QQ（缺省为自己）最近 ``num`` 条说说，返回结构化列表。"""
        session = await self.session()
        target = _target_uin(uin, session)
        items = await self._fetch_feed_items(target, num=num, pos=pos)
        return [parsed for item in items if (parsed := _parse_feed(item))]

    async def my_comments(self, *, num: int = 10, pos: int = 0) -> list[dict[str, Any]]:
        """读自己最近说说下收到的评论（含楼中楼），扁平化成列表。"""
        session = await self.session()
        items = await self._fetch_feed_items(session.uin, num=num, pos=pos)
        comments: list[dict[str, Any]] = []
        for item in items:
            tid = str(item.get("tid") or "")
            if not tid:
                continue
            feed_uin = str(item.get("uin") or session.uin)
            for comment in _flatten_comments(item.get("commentlist")):
                # “收到的评论”：跳过自己发的评论
                if comment["comment_uin"] == session.uin:
                    continue
                comments.append(
                    {
                        "tid": tid,
                        "feed_uin": feed_uin,
                        "comment_id": comment["comment_id"],
                        "comment_uin": comment["comment_uin"],
                        "nickname": comment["nickname"],
                        "content": comment["content"],
                        "created_time": comment["created_time"],
                        "parent_id": comment["parent_id"],
                    }
                )
        return comments

    # ---- 写 ----

    async def like(self, uin: str | int, tid: str | int) -> dict[str, Any]:
        """给指定 (uin, tid) 的说说点赞。"""
        feed_id = _require_id(tid, "tid")
        session = await self.session()
        target = _target_uin(uin, session)
        unikey = f"{LIKE_KEY_BASE}/{target}/mood/{feed_id}"
        referer = f"{BASE_URL}/{target}/mood/{feed_id}"
        await self._request(
            "POST",
            LIKE_URL,
            data={
                "qzreferrer": f"{BASE_URL}/{session.uin}",
                "opuin": session.uin,
                "unikey": unikey,
                "curkey": unikey,
                "appid": 311,
                "from": 1,
                "typeid": 0,
                "abstime": int(time.time()),
                "fid": feed_id,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            },
            referer=referer,
        )
        return {"uin": target, "tid": feed_id, "liked": True}

    async def comment(
        self,
        uin: str | int,
        tid: str | int,
        content: str,
    ) -> dict[str, Any]:
        """在指定 (uin, tid) 的说说下发表纯文本评论。"""
        feed_id = _require_id(tid, "tid")
        text = _require_content(content, "qzone.comment")
        session = await self.session()
        target = _target_uin(uin, session)
        referer = f"{BASE_URL}/{target}/mood/{feed_id}"
        payload = await self._request(
            "POST",
            COMMENT_URL,
            data=_comment_form(target, feed_id, text, session.uin),
            referer=referer,
        )
        return {
            "uin": target,
            "tid": feed_id,
            "comment_id": str(payload.get("commentid") or ""),
        }

    async def reply(
        self,
        uin: str | int,
        tid: str | int,
        comment_id: str | int,
        content: str,
        *,
        comment_uin: str | int | None = None,
        reply_uin: str | int | None = None,
        comment_nickname: str | None = None,
    ) -> dict[str, Any]:
        """回复指定评论（楼中楼）。

        未提供 ``comment_uin``（被回复评论作者的 QQ）时会回读该说说，
        从评论列表里找；找不到则报错（空间会拒绝 commentUin 不对的写操作）。
        正文前加 ``@{uin:..,nick:..,auto:1}`` 提及标记，与网页版行为一致。
        """
        feed_id = _require_id(tid, "tid")
        cid = _require_id(comment_id, "comment_id")
        text = _require_content(content, "qzone.reply")
        session = await self.session()
        target = _target_uin(uin, session)

        if comment_uin in (None, ""):
            found = await self._find_comment(target, feed_id, cid)
            if found is None:
                raise QzoneError(
                    f"qzone.reply 找不到评论 {cid}（tid={feed_id}），"
                    "无法确定被回复者，请显式传 comment_uin"
                )
            comment_uin = found["comment_uin"]
            comment_nickname = comment_nickname or found["nickname"]
        target_uin = _require_id(comment_uin, "comment_uin")
        reply_target = _require_id(reply_uin, "reply_uin") if reply_uin not in (None, "") else target_uin
        nick = str(comment_nickname or "").strip() or reply_target

        if not text.startswith("@{"):
            text = f"@{{uin:{reply_target},nick:{nick},auto:1}} {text}"

        referer = f"{BASE_URL}/{target}/mood/{feed_id}"
        await self._request(
            "POST",
            COMMENT_URL,
            # PC 代理回复评论：topicId 不带 __1、format=json，
            # 用 commentId / replyUin / commentUin 指定被回复评论
            # （见 Zhalslar/astrbot_plugin_qzone 的 reply 实现）。
            data={
                "hostUin": target,
                "topicId": f"{target}_{feed_id}",
                "content": text,
                "format": "json",
                "qzreferrer": f"{BASE_URL}/{session.uin}/main",
                "commentId": cid,
                "replyUin": reply_target,
                "commentUin": target_uin,
            },
            referer=referer,
        )
        return {"uin": target, "tid": feed_id, "comment_id": cid, "reply_to": reply_target}

    # ---- 内部 ----

    async def _fetch_feed_items(self, uin: str, *, num: int, pos: int = 0) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            LIST_URL,
            params={
                "uin": uin,
                "hostUin": uin,
                "ftype": 0,
                "sort": 0,
                "pos": max(0, int(pos)),
                "num": max(1, min(int(num), 20)),
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
            referer=f"{BASE_URL}/{uin}",
        )
        items = payload.get("msglist")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def _find_comment(self, uin: str, tid: str, comment_id: str) -> dict[str, Any] | None:
        """在目标 QQ 的最近说说里按 (tid, comment_id) 找评论作者。"""
        items = await self._fetch_feed_items(uin, num=20)
        for item in items:
            if str(item.get("tid") or "") != tid:
                continue
            for comment in _flatten_comments(item.get("commentlist")):
                if comment["comment_id"] == comment_id:
                    return comment
        return None


# ----------------------------------------------------------------------
# 解析与校验
# ----------------------------------------------------------------------


def _target_uin(uin: str | int | None, session: QzoneSession) -> str:
    if uin in (None, ""):
        return session.uin
    value = str(uin).strip().lstrip("oO")
    if not value.isdigit():
        raise QzoneError(f"QQ 号格式不对：{uin}")
    return value


def _require_id(value: Any, name: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise QzoneError(f"{name} 不能为空")
    return text


def _require_content(value: Any, capability: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise QzoneError(f"{capability} 需要非空 content")
    return text


def _parse_feed(item: dict[str, Any]) -> dict[str, Any] | None:
    tid = str(item.get("tid") or "")
    if not tid:
        return None
    pics = item.get("pic")
    pics = pics if isinstance(pics, list) else []
    comments = item.get("commentlist")
    comments = comments if isinstance(comments, list) else []
    comment_count = _to_int(item.get("commentnum") or item.get("reply_num"), len(comments))
    return {
        "tid": tid,
        "uin": str(item.get("uin") or item.get("hostuin") or ""),
        "nickname": str(item.get("name") or item.get("nickname") or ""),
        "content": clean_text(item.get("content")),
        "created_time": _to_int(item.get("created_time")),
        "image_count": len(pics),
        "comment_count": comment_count,
        "is_liked": _to_bool_or_none(item.get("isliked")),
    }


def _flatten_comments(nodes: Any, parent_id: str | None = None) -> list[dict[str, Any]]:
    """把 commentlist（含 list_3/replylist 楼中楼）拉平成评论列表。"""
    result: list[dict[str, Any]] = []
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if not isinstance(node, dict):
            continue
        cid = _comment_id(node)
        result.append(
            {
                "comment_id": cid,
                "comment_uin": str(node.get("uin") or ""),
                "nickname": str(node.get("name") or node.get("nickname") or ""),
                "content": clean_text(node.get("content")),
                "created_time": _to_int(node.get("create_time") or node.get("time")),
                "parent_id": parent_id,
            }
        )
        nested: list[Any] = []
        for key in ("list_3", "replylist", "replies"):
            value = node.get(key)
            if isinstance(value, list):
                nested.extend(value)
        if nested:
            result.extend(_flatten_comments(nested, parent_id=cid or parent_id))
    return result


def _comment_form(target: str, tid: str, content: str, login_uin: str) -> dict[str, Any]:
    """评论表单；回复单走 PC 代理那套参数，这里只发普通评论。"""
    return {
        "topicId": f"{target}_{tid}__1",
        "uin": login_uin,
        "hostUin": target,
        "feedsType": 100,
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "platformid": 50,
        "format": "fs",
        "ref": "feeds",
        "content": content,
        "private": 0,
        "paramstr": "1",
        "isSignIn": "0",
        "richval": "",
        "richtype": "",
    }
