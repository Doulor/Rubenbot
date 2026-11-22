"""WebUI 访问口令与会话签名。

安全底线：默认只绑 127.0.0.1，不需要口令；绑非本地地址时必须有口令，
否则拒绝启动（校验在 :func:`check_bind_security`，启动时调用）。

口令本身不存明文比对：配置里写口令，进程内计算 ``sha256(口令)`` 后
用 HMAC 签发一个带过期时间的 token，放进 HttpOnly Cookie。重启后
token 失效（密钥随机生成），需要重新登录。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path

from ..config import WebUIConfig, is_loopback_host

COOKIE_NAME = "rubenbot_session"

_SESSION_SECRET_FILE = ".webui_session_secret"


def load_or_create_session_secret(db_path: str) -> bytes | None:
    """从数据库同目录读会话签名密钥；没有就生成一份落盘（0600）。

    密钥落盘后，进程重启不再使已签发的登录 Cookie 失效。读写失败时
    返回 None，调用方退回进程内临时密钥（旧行为），不阻断启动。
    """
    if not db_path or db_path == ":memory:":
        return None
    path = Path(db_path).parent / _SESSION_SECRET_FILE
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if len(data) >= 16:
                return data
        secret = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return secret
    except OSError:
        return None


class BindSecurityError(RuntimeError):
    """绑定到非本地地址却没有设置口令。"""


def check_bind_security(config: WebUIConfig) -> None:
    """非本地绑定必须设口令；空口令直接拒绝启动。"""
    if not is_loopback_host(config.host) and not (config.password or "").strip():
        raise BindSecurityError(
            f"WebUI 绑定到非本地地址 {config.host!r} 时必须设置 webui.password，"
            "否则拒绝启动（可写明文口令，也可用 ${ENV_VAR} 从环境变量读取）"
        )


@dataclass
class Session:
    user: str
    issued_at: float
    expires_at: float


class SessionSigner:
    """HMAC-SHA256 签名的会话 token，Base64URL 编码，无第三方依赖。"""

    def __init__(self, password: str, *, ttl_hours: float = 72.0, secret: bytes | None = None):
        self.password = password or ""
        # 盐保证同一口令在不同进程/不同配置下签出的 token 不通用
        self._secret = secret or secrets.token_bytes(32)
        self.ttl_seconds = max(60.0, ttl_hours * 3600)
        self._password_hash = hashlib.sha256(self.password.encode("utf-8")).digest()

    @property
    def secret(self) -> bytes:
        """当前签名密钥；热更新重建 signer 时带过去，避免无故登出。"""
        return self._secret

    # ---- 口令 ----

    @property
    def auth_required(self) -> bool:
        return bool(self.password)

    def verify_password(self, candidate: str) -> bool:
        digest = hashlib.sha256((candidate or "").encode("utf-8")).digest()
        return hmac.compare_digest(digest, self._password_hash)

    # ---- token ----

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, self._password_hash + b"." + payload, hashlib.sha256).digest()

    def issue(self, user: str = "admin", *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = json.dumps(
            {"u": user, "iat": int(now), "exp": int(now + self.ttl_seconds)},
            separators=(",", ":"),
        ).encode("utf-8")
        body = urlsafe_b64encode(payload).rstrip(b"=")
        signature = urlsafe_b64encode(self._sign(body)).rstrip(b"=")
        return (body + b"." + signature).decode("ascii")

    def parse(self, token: str | None, *, now: float | None = None) -> Session | None:
        """校验并解析 token；无效、被篡改或过期都返回 None。"""
        if not token or "." not in token:
            return None
        body, _, signature = token.partition(".")
        try:
            raw_body = body.encode("ascii")
            provided = _b64decode(signature)
        except (UnicodeEncodeError, ValueError):
            return None
        if not hmac.compare_digest(self._sign(raw_body), provided):
            return None
        try:
            data = json.loads(_b64decode(body))
            expires_at = float(data["exp"])
            issued_at = float(data.get("iat", 0))
            user = str(data.get("u") or "admin")
        except (ValueError, KeyError, TypeError):
            return None
        now = time.time() if now is None else now
        if expires_at < now:
            return None
        return Session(user=user, issued_at=issued_at, expires_at=expires_at)


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return urlsafe_b64decode(padded.encode("ascii"))


def mask_secret(value: object) -> str:
    """设置页展示配置时，把明显的机密字段打码。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


SECRET_KEY_HINTS = ("key", "token", "password", "secret")
