"""WebUI 后端：FastAPI 应用（REST + JSON，前缀 ``/api``）。

可选依赖：模块顶层不导入 FastAPI，只有 :func:`create_app` 真正被调用时
才导入，这样没装 ``rubenbot[webui]`` 时核心功能完全不受影响。

路由一览（全部 JSON）：

- ``GET  /api/health``          健康检查（无需登录）
- ``POST /api/login``           口令登录，下发 HttpOnly 会话 Cookie
- ``POST /api/logout``          退出
- ``GET  /api/me``              当前会话
- ``GET  /api/overview``        概览：运行状态、计数、最近决策
- ``GET  /api/memories``        记忆搜索/浏览（person_id、q、limit、offset）
- ``POST /api/memories``        手动新增
- ``PUT  /api/memories/{id}``   编辑
- ``DELETE /api/memories/{id}`` 删除（写 memory_log）
- ``GET  /api/memory-log``      变更日志
- ``POST /api/memory-log/{id}/restore`` 撤销
- ``GET  /api/people``          人物列表（带记忆条数）
- ``GET  /api/people/{id}``     人物档案 + 记忆
- ``GET  /api/chats``           会话列表
- ``GET  /api/chats/{id}/decision`` 群决策配置
- ``PUT  /api/chats/{id}/decision`` 保存群决策配置（写 config.yaml）
- ``GET  /api/decisions``       决策日志（可按 chat_id 过滤）
- ``GET  /api/emotion``         当前心情 + 好感 Top5/Bottom3 + 最近变化流水（只读）
- ``GET  /api/stickers``        表情库列表；``GET /api/stickers/{id}/image`` 取图片；``DELETE`` 删除
- ``GET  /api/plugins``         插件列表（含目录里发现但未启用的）
- ``GET/PUT /api/plugins/{id}/config`` 读写已安装插件配置
- ``POST /api/plugins/{name}``  启用/禁用：热生效并写回 plugins.enabled
- ``GET  /api/market``          插件市场：数据来源 + 本地/索引合并后的卡片列表
- ``POST /api/market/plugins/{id}/install``   从索引安装（默认不启用）
- ``POST /api/market/plugins/{id}/update``    更新代码与 manifest（保留配置与启用状态）
- ``POST /api/market/plugins/{id}/uninstall`` 卸载（启用中先停用再删目录）
- ``GET  /api/logs``            运行日志尾部
- ``GET  /api/config``          查看 config.yaml
- ``PUT  /api/config``          编辑并校验后保存，返回需重启提示
- ``GET  /api/persona``         查看人格文件内容与当前实际生效的人格
- ``PUT  /api/persona``         保存人格并热加载到运行中的 bot
- ``POST /api/persona/reset``   恢复默认人格（同样热加载）
- ``GET  /api/persona/preview`` 拼装后的系统提示词示例
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..adapters.base import MessageEvent
from ..config import Config, GroupDecisionConfig, is_loopback_host
from ..core import format_scene
from ..decision import parse_quiet_hours
from ..identity import IdentityService, avatar_url
from ..emotion import EmotionEngine
from ..memory.store import make_person_id
from ..persona import DEFAULT_PERSONA, Persona, load_persona
from .auth import COOKIE_NAME, BindSecurityError, check_bind_security, mask_secret
from .config_schema import (
    ConfigValidationError,
    config_layout,
    merge_config,
    mcp_form,
    structured_config,
    validate_structured,
)
from .logbuffer import records as log_records
from .market import MarketError, MarketService
from .state import (
    WebState,
    describe_restart_fields,
    restart_fields,
    save_config_text,
    save_persona_text,
    write_plugin_enabled,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

MEMORY_SOURCES = {"extracted", "user", "system"}
MEMORY_CATEGORIES = {
    "identity",
    "preference",
    "habit",
    "relationship",
    "experience",
    "promise",
    "fact",
}


class ApiError(Exception):
    """业务校验失败；由异常处理器统一转成 JSON 错误。"""

    def __init__(self, status: int, message: str, *, fields: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fields = fields or []


def _require_text(value: Any, field: str, *, max_len: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApiError(422, f"{field} 不能为空")
    if len(text) > max_len:
        raise ApiError(422, f"{field} 太长（最多 {max_len} 字）")
    return text


def _memory_payload(mem) -> dict[str, Any]:
    return {
        "id": mem.id,
        "person_id": mem.person_id,
        "scope_type": mem.scope_type,
        "scope_id": mem.scope_id,
        "content": mem.content,
        "category": mem.category,
        "source": mem.source,
        "source_ref": mem.source_ref,
        "created_at": mem.created_at,
        "updated_at": mem.updated_at,
    }


def _person_payload(person, count: int = 0, *, identities: IdentityService | None = None) -> dict[str, Any]:
    item = {
        "person_id": person.person_id,
        "platform": person.platform,
        "platform_uid": person.platform_uid,
        "display_name": person.display_name,
        "first_seen": person.first_seen,
        "last_seen": person.last_seen,
        "memory_count": count,
    }
    if identities is not None:
        item["identity"] = identities.person(person)
    return item


def _log_payload(entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "action": entry.action,
        "person_id": entry.person_id,
        "before": entry.before,
        "after": entry.after,
        "reason": entry.reason,
        "created_at": entry.created_at,
        "reverted": entry.reverted,
    }


def _group_decision_payload(config, chat_id: str) -> dict[str, Any]:
    """生效中的群决策参数（全局默认 + 群覆盖）。"""
    decision = config.decision
    settings: GroupDecisionConfig | None = decision.groups.get(str(chat_id))
    groups = decision.groups or {}
    effective = {
        "base": decision.base if settings is None or settings.base is None else settings.base,
        "threshold": decision.threshold,
        "activity_multiplier": settings.activity_multiplier if settings else 1.0,
        "quiet_hours": list(settings.quiet_hours) if settings else [],
    }
    return {
        "chat_id": str(chat_id),
        "configured": settings is not None,
        "override": {
            "activity_multiplier": settings.activity_multiplier if settings else 1.0,
            "base": settings.base if settings else None,
            "quiet_hours": list(settings.quiet_hours) if settings else [],
        },
        "effective": effective,
        "defaults": {
            "base": decision.base,
            "threshold": decision.threshold,
            "activity_multiplier": 1.0,
        },
        "configured_chats": sorted(groups),
    }


def _write_group_decision(state, chat_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """把群覆盖写回 config.yaml 的 ``decision.groups``，保留其余内容与顺序。"""
    import yaml

    config = state.config
    config_path = state.config_path
    path = Path(config_path)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ApiError(500, "config.yaml 顶层不是键值对，无法写入")
    else:
        data = {}
    decision = data.setdefault("decision", {})
    if not isinstance(decision, dict):
        raise ApiError(500, "config.yaml 里的 decision 段格式不正确")
    groups = decision.setdefault("groups", {})
    if not isinstance(groups, dict):
        raise ApiError(500, "config.yaml 里的 decision.groups 格式不正确")

    multiplier = body.get("activity_multiplier")
    try:
        multiplier = float(1.0 if multiplier is None else multiplier)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "活跃度倍率必须是数字") from exc
    if not (0.0 <= multiplier <= 10.0):
        raise ApiError(422, "活跃度倍率应在 0 到 10 之间")

    base_value = body.get("base")
    if base_value in (None, ""):
        base = None
    else:
        try:
            base = float(base_value)
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "基础意愿必须是数字") from exc
        if not (0.0 <= base <= 1.0):
            raise ApiError(422, "基础意愿应在 0 到 1 之间")

    quiet_raw = body.get("quiet_hours") or []
    if isinstance(quiet_raw, str):
        quiet_raw = [quiet_raw]
    if not isinstance(quiet_raw, list):
        raise ApiError(422, "静默时段应为字符串列表")
    quiet = [str(item).strip() for item in quiet_raw if str(item or "").strip()]
    for item in quiet:
        if not parse_quiet_hours([item]):
            raise ApiError(422, f"静默时段格式不对：{item}（应为 HH:MM-HH:MM）")

    if bool(body.get("remove")):
        groups.pop(str(chat_id), None)
        _apply_groups(state, data)
        save_config_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return _group_decision_payload(config, chat_id)

    entry: dict[str, Any] = {"activity_multiplier": multiplier}
    if base is not None:
        entry["base"] = base
    if quiet:
        entry["quiet_hours"] = quiet
    groups[str(chat_id)] = entry
    _apply_groups(state, data)
    save_config_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return _group_decision_payload(config, chat_id)


def _apply_groups(state, data: dict[str, Any]) -> None:
    """保存成功后同步内存中的配置，页面与意愿引擎立即看到新值。"""
    from ..config import _load_decision

    config = state.config
    config.decision = _load_decision(data.get("decision") or {})
    bot = getattr(state, "bot", None)
    if bot is not None and hasattr(bot, "apply_config"):
        try:
            bot.apply_config(config, rebuild_llm=False)
        except Exception as exc:  # noqa: BLE001 - 热应用失败不影响已写盘的配置
            logger.warning("群决策配置热应用失败：%s", exc)


def _write_plugin_enabled(state) -> None:
    """把当前启用的插件列表写回 config.yaml 的 ``plugins.enabled``。

    只动这一个键，保留其余内容与顺序；写盘沿用设置页的原子替换 + 备份。
    真正的写入逻辑在 :func:`rubenbot.web.state.write_plugin_enabled`，市场页
    的启停/卸载复用同一条路径。
    """
    write_plugin_enabled(state)


def _save_plugin_without_manager(state: WebState, name: str, enabled: bool) -> dict[str, Any]:
    """独立 WebUI 模式：只把开关写进配置，并如实提示需要重启生效。"""
    known = {item["name"] for item in state.plugin_list()}
    if name not in known:
        raise ApiError(404, f"插件不存在：{name}")
    names = [item for item in state.config.plugins.enabled if item != name]
    if enabled:
        names.append(name)
    state.config.plugins.enabled = names
    try:
        _write_plugin_enabled(state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("插件开关写回 config.yaml 失败：%s", exc)
        raise ApiError(500, f"写回配置失败：{exc}") from exc
    return {
        "ok": True,
        "items": state.plugin_list(),
        "restart_required": True,
        "persisted": True,
        "message": (
            f"插件 {name} 的开关已写入配置；当前 WebUI 没有运行中的 bot，"
            "重启 RubenBot 后生效。"
        ),
    }


def _mask_config(data: Any) -> Any:
    """递归打码 api_key / access_token / password 之类的字段。"""
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            lowered = str(key).lower()
            if isinstance(value, (dict, list)):
                masked[key] = _mask_config(value)
            elif any(hint in lowered for hint in ("api_key", "token", "password", "secret")):
                masked[key] = mask_secret(value)
            else:
                masked[key] = value
        return masked
    if isinstance(data, list):
        return [_mask_config(item) for item in data]
    return data


def _persona_file_info(state: WebState) -> tuple[Path, bool, str, float | None]:
    """返回（路径、是否存在、文件内容、修改时间）。"""
    path = Path(state.config.persona_path)
    exists = path.exists()
    text = path.read_text(encoding="utf-8") if exists else ""
    mtime = path.stat().st_mtime if exists else None
    return path, exists, text, mtime


def _effective_persona(state: WebState, file_text: str, exists: bool):
    """当前实际生效的人格：运行中的 bot 持有对象优先，否则按文件内容构造。

    GET 是只读操作：文件不存在时直接按默认人格展示，不顺手创建文件。
    """
    persona = state.bot_persona()
    if persona is not None:
        return persona, "bot"
    text = file_text if exists else DEFAULT_PERSONA
    persona = Persona(name=_extract_name_safe(text), text=text, **state.config.persona_example_options())
    return persona, "file" if exists else "default"


def _persona_payload(state: WebState) -> dict[str, Any]:
    path, exists, file_text, mtime = _persona_file_info(state)
    persona, source = _effective_persona(state, file_text, exists)
    effective_text = persona.text
    # 文件还不存在时，编辑区预填默认人格（与启动时 load_persona 会写入的一致），
    # 免得用户面对一片空白不知道从哪改；exists 字段告诉前端这一点。
    editor_text = file_text if exists else effective_text
    return {
        "ok": True,
        "path": str(path),
        "exists": exists,
        "text": editor_text,
        "chars": len(editor_text),
        "mtime": mtime,
        "name": persona.name,
        "effective": effective_text,
        "effective_chars": len(effective_text),
        "effective_name": persona.name,
        "effective_source": source,
        "hot_reload": state.bot_persona() is not None,
        "in_sync": editor_text == effective_text,
    }


def _extract_name_safe(text: str) -> str:
    """预览用：不落地文件，直接猜名字。"""
    from ..persona import _extract_name

    return _extract_name(text) if text.strip() else "Ruben"


def _preview_prompt(persona) -> str:
    """拼一次系统提示词示例：人格 + 场景行 + 空记忆（与 core.build_context 同构）。"""
    event = MessageEvent(
        platform="webui",
        chat_type="group",
        chat_id="preview",
        sender_id="preview",
        sender_name="阿花",
        text="",
    )
    sections = [persona.system_prompt(), "（这里会放检索到的记忆；示例里为空）", format_scene(event, "阿花")]
    return "\n\n".join(sections)


def _apply_persona(state: WebState, text: str) -> dict[str, Any]:
    """写人格文件并热加载；返回给前端的落盘/生效信息。"""
    backup = save_persona_text(state.config.persona_path, text)
    persona = load_persona(state.config.persona_path, **state.config.persona_example_options())
    applied = state.apply_persona(persona)
    return {
        "backup": str(backup) if backup else None,
        "applied": applied,
        "restart_required": not applied,
        "message": (
            "已生效，无需重启。后续消息会直接使用新人设。"
            if applied
            else "人格文件已保存。当前没有运行中的 bot，下次启动生效。"
        ),
    }


def create_app(state: WebState):
    """构造 FastAPI 应用。

    测试里用 :class:`WebState` 直接注入 bot/存储；生产里由
    :func:`rubenbot.__main__` 同进程拉起。
    """
    # 必须先校验再起服务：非本地绑定缺口令时直接拒绝
    check_bind_security(state.config.webui)

    app = FastAPI(
        title="RubenBot WebUI",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web = state
    identities = IdentityService(state)

    def decision_payload(row):
        platform, chat_id = row["platform"], row["chat_id"]
        return {**row,
                "chat_identity": identities.chat(platform, chat_id, chat_type=row["chat_type"]),
                "sender_identity": identities.person_id(
                    f"{platform}:{row['sender_id']}", hint=row.get("sender_name", ""),
                    group_id=chat_id if row["chat_type"] == "group" else "",
                ) if row.get("sender_id") else None}

    def configured_group(chat_id):
        # 每群覆盖沿用原始群号作键；不能用「第一个适配器」猜名字。
        platforms = state.store.group_platforms(chat_id)
        return identities.chat(platforms[0] if len(platforms) == 1 else "", chat_id)

    def memory_payload(mem):
        person = identities.person_id(mem.person_id)
        return {**_memory_payload(mem), "person_identity": person,
                "chat_identity": identities.chat(person["platform"] if person else "", mem.scope_id)
                if mem.scope_type == "group" else None}

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------

    def signer():
        return state.signer

    def current_session(request: Request):
        """返回 True（无需登录）、Session（已登录）或 None（未登录）。"""
        sign = signer()
        if sign is None or not sign.auth_required:
            return True
        token = request.cookies.get(COOKIE_NAME)
        return sign.parse(token)

    def require_session(request: Request) -> None:
        if not current_session(request):
            raise ApiError(401, "未登录或会话已过期")

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        payload = {"ok": False, "error": exc.message}
        if exc.fields:
            payload["fields"] = exc.fields
        return JSONResponse(payload, status_code=exc.status)

    @app.exception_handler(sqlite3.Error)
    async def _db_error_handler(request: Request, exc: sqlite3.Error):
        logger.warning("WebUI 数据库错误：%s", exc)
        return JSONResponse({"ok": False, "error": "数据库读写失败"}, status_code=500)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        """统一鉴权：``/api`` 下除登录/健康检查外都要会话；静态页放行。"""
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/health", "/api/login", "/api/me"):
            if not current_session(request):
                return JSONResponse(
                    {"ok": False, "error": "未登录或会话已过期", "auth_required": True},
                    status_code=401,
                )
        response = await call_next(request)
        # index 与无构建链静态资源必须重新校验，避免部署后旧 JS 与新页面混搭。
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "name": "RubenBot WebUI",
            "running": state.is_running(),
            "auth_required": state.auth_required,
            "loopback_only": is_loopback_host(state.config.webui.host),
        }

    @app.get("/api/me")
    async def me(request: Request):
        session = current_session(request)
        if session is not None and session is not True:
            return {"ok": True, "authenticated": True, "user": session.user, "auth_required": True}
        if session is True:
            return {"ok": True, "authenticated": True, "user": "local", "auth_required": False}
        return {"ok": True, "authenticated": False, "user": None, "auth_required": True}

    @app.post("/api/login")
    async def login(request: Request):
        sign = signer()
        if sign is None or not sign.auth_required:
            return {"ok": True, "auth_required": False}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 空 body/非法 JSON 都按未提交处理
            body = {}
        password = str((body or {}).get("password") or "")
        if not sign.verify_password(password):
            # 简单节流：错误口令也睡一小会儿，降低暴力尝试速度
            await asyncio.sleep(0.25)
            raise ApiError(401, "口令不正确")
        token = sign.issue()
        response = JSONResponse({"ok": True, "auth_required": True})
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=int(sign.ttl_seconds),
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview(request: Request):
        require_session(request)
        counts = state.store.chat_counts()
        stats = state.store.decision_stats()
        since = time.time() - 86400
        return {
            "ok": True,
            "bot": {
                "name": state.bot_name,
                "running": state.is_running(),
                "uptime_seconds": state.uptime_seconds(),
                "started_at": state.started_at,
                "last_message_at": getattr(state.bot, "last_message_at", None),
                "persona_path": state.config.persona_path,
            },
            "counts": {
                "messages": counts["messages"],
                "replies": counts["replies"],
                "chats": counts["chats"],
                "memories": state.store.count_memories(),
                "memories_24h": state.store.count_memories(since=since),
                "people": state.store.count_people(),
                "decisions": stats["decisions"],
                "decision_replies": stats["decision_replies"],
                "hard_triggers": stats["hard_triggers"],
            },
            "adapters": state.adapters_status(),
            "recent_decisions": [decision_payload(row) for row in state.recent_decisions(limit=10)],
            "plugins": {
                "total": len(state.plugin_list()),
                "enabled": sum(1 for p in state.plugin_list() if p.get("enabled")),
            },
            "webui": {
                "host": state.config.webui.host,
                "port": state.config.webui.port,
                "auth_required": state.auth_required,
            },
        }

    # ------------------------------------------------------------------
    # 记忆
    # ------------------------------------------------------------------

    @app.get("/api/usage")
    async def usage(request: Request, days: int = 7):
        """近 N 天 LLM 用量聚合（默认 7 天）；前端页面本轮不做，只留接口。"""
        require_session(request)
        return {"ok": True, **state.usage_summary(days=max(1, min(days, 90)))}

    # ------------------------------------------------------------------
    # 事件通道与网关审计（只读）
    # ------------------------------------------------------------------

    @app.get("/api/events")
    async def platform_events(
        request: Request,
        kind: str | None = None,
        pending: bool | None = None,
        platform: str | None = None,
        limit: int = 50,
    ):
        """平台事件（notice / request）最近记录；``pending`` 只看未处理申请。"""
        require_session(request)
        limit = max(1, min(int(limit), 200))
        items = state.store.list_platform_events(
            kind=kind or None,
            pending=pending,
            platform=platform or None,
            limit=limit,
        )
        return {
            "ok": True,
            "items": items,
            "pending_requests": state.store.count_pending_platform_events(),
        }

    @app.get("/api/gateway/audit")
    async def gateway_audit(
        request: Request,
        action: str | None = None,
        sensitive: bool | None = None,
        limit: int = 50,
    ):
        """网关调用审计（只读）：成功与拒绝都列，sensitive 动作带标记。"""
        require_session(request)
        limit = max(1, min(int(limit), 200))
        items = state.store.list_gateway_audit(
            action=action or None,
            sensitive=sensitive,
            limit=limit,
        )
        for item in items:
            if not item["chat_id"]:
                item["chat_identity"] = None
            elif item["chat_type"] in ("private", "group"):
                item["chat_identity"] = identities.chat(item["platform"], item["chat_id"], chat_type=item["chat_type"])
            else:
                # 旧记录没有会话类型，同号可能是群也可能是人。保留原 ID，不猜头像。
                item["chat_identity"] = {"kind": "chat", "name": "未知会话", "avatar_url": "",
                                         "id": item["chat_id"], "platform": item["platform"]}
        return {"ok": True, "items": items}

    @app.post("/api/identities/refresh")
    async def refresh_identities(request: Request):
        require_session(request)
        body = await _json_body(request)
        result = await identities.refresh(
            platform=str(body.get("platform") or ""),
            chat_id=str(body.get("chat_id") or ""),
            person_id=str(body.get("person_id") or ""), force=True,
        )
        return result

    @app.get("/api/identities")
    async def list_identities(request: Request, platform: str | None = None, kind: str | None = None):
        require_session(request)
        return {"ok": True, "items": state.store.list_identities(platform=platform or None, kind=kind or None)}

    @app.get("/api/memories")
    async def list_memories(
        request: Request,
        person_id: str | None = None,
        q: str | None = None,
        scope_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        require_session(request)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        memories, total = state.store.search_memories(
            q or "",
            person_id=person_id or None,
            scope_type=scope_type or None,
            limit=limit,
            offset=offset,
        )
        return {
            "ok": True,
            "total": total,
            "items": [memory_payload(mem) for mem in memories],
        }

    @app.post("/api/memories")
    async def create_memory(request: Request):
        require_session(request)
        body = await _json_body(request)
        person_id = _require_text(body.get("person_id"), "person_id", max_len=120)
        content = _require_text(body.get("content"), "内容", max_len=500)
        category = str(body.get("category") or "fact").strip().lower()
        if category not in MEMORY_CATEGORIES:
            raise ApiError(422, f"未知类别：{category}")
        scope_type = str(body.get("scope_type") or "global").strip().lower()
        if scope_type not in ("global", "group"):
            raise ApiError(422, "scope_type 只能是 global 或 group")
        scope_id = str(body.get("scope_id") or "").strip()
        if scope_type == "group" and not scope_id:
            raise ApiError(422, "群记忆必须填写 scope_id（群号）")
        memory = state.store.add_memory(
            person_id,
            content,
            scope_type=scope_type,
            scope_id=scope_id if scope_type == "group" else "",
            category=category,
            source="user",
            source_ref="webui",
        )
        logger.info("WebUI 新增记忆 #%s：%s", memory.id, content[:40])
        return {"ok": True, "item": memory_payload(memory)}

    @app.put("/api/memories/{memory_id}")
    async def update_memory(memory_id: int, request: Request):
        require_session(request)
        mem = state.store.get_memory(memory_id)
        if mem is None:
            raise ApiError(404, "记忆不存在")
        body = await _json_body(request)
        content = _require_text(body.get("content", mem.content), "内容", max_len=500)
        category = str(body.get("category") or mem.category).strip().lower()
        if category not in MEMORY_CATEGORIES:
            raise ApiError(422, f"未知类别：{category}")
        updated = state.store.update_memory(memory_id, content=content, category=category)
        logger.info("WebUI 编辑记忆 #%s", memory_id)
        return {"ok": True, "item": memory_payload(updated)}

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: int, request: Request):
        require_session(request)
        mem = state.store.get_memory(memory_id)
        if mem is None:
            raise ApiError(404, "记忆不存在")
        entry = state.store.apply_changes(
            action="drop",
            delete_ids=[memory_id],
            reason="WebUI 手动删除",
            person_id=mem.person_id,
        )
        logger.info("WebUI 删除记忆 #%s（日志 #%s）", memory_id, entry.id)
        return {"ok": True, "log_id": entry.id}

    @app.get("/api/memory-log")
    async def memory_log(request: Request, limit: int = 50, person_id: str | None = None):
        require_session(request)
        limit = max(1, min(int(limit), 200))
        logs = state.store.list_logs(limit=limit, person_id=person_id or None)
        items = []
        for entry in logs:
            item = _log_payload(entry)
            item["person_identity"] = identities.person_id(entry.person_id or "")
            items.append(item)
        return {"ok": True, "items": items}

    @app.post("/api/memory-log/{log_id}/restore")
    async def restore_log(log_id: int, request: Request):
        require_session(request)
        entry = state.store.get_log(log_id)
        if entry is None:
            raise ApiError(404, "日志不存在")
        if entry.reverted:
            raise ApiError(409, "这条日志已经撤销过了")
        if not state.store.restore(log_id):
            raise ApiError(409, "撤销失败（可能已撤销或记录有冲突）")
        logger.info("WebUI 撤销记忆变更 #%s", log_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # 提醒（只读列表：一期不做取消工具，这里只给用户看一眼）
    # ------------------------------------------------------------------

    @app.get("/api/reminders")
    async def list_reminders(request: Request, limit: int = 100):
        require_session(request)
        limit = max(1, min(int(limit), 500))
        jobs = state.store.list_scheduled_jobs(status="pending", kind="reminder", limit=limit)
        return {
            "ok": True,
            "items": [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "platform": job.platform,
                    "chat_id": job.chat_id,
                    "chat_type": job.chat_type,
                    "chat_identity": identities.chat(job.platform, job.chat_id, chat_type=job.chat_type),
                    "fire_at": job.fire_at,
                    "text": str(job.payload.get("text") or ""),
                    "requester_name": str(job.payload.get("requester_name") or ""),
                    "created_by": job.created_by,
                    "requester_identity": identities.person_id(job.created_by, hint=str(job.payload.get("requester_name") or "")),
                    "created_at": job.created_at,
                }
                for job in jobs
            ],
        }

    # ------------------------------------------------------------------
    # 人物
    # ------------------------------------------------------------------

    @app.get("/api/people")
    async def list_people(request: Request, limit: int = 200, q: str | None = None):
        require_session(request)
        limit = max(1, min(int(limit), 500))
        keyword = (q or "").strip().lower()
        rows = state.store.list_people_with_counts(limit=limit)
        items = [_person_payload(person, count, identities=identities) for person, count in rows]
        if keyword:
            items = [
                item
                for item in items
                if keyword in item["identity"]["name"].lower()
                or keyword in item["display_name"].lower()
                or keyword in item["person_id"].lower()
                or keyword in item["platform_uid"].lower()
            ]
        return {"ok": True, "total": len(items), "items": items}

    @app.get("/api/people/{person_id:path}")
    async def get_person(person_id: str, request: Request):
        require_session(request)
        person = state.store.get_person(person_id)
        if person is None:
            raise ApiError(404, "人物不存在")
        memories = state.store.list_memories(person_id=person_id, limit=200)
        profile = state.store.get_profile(person_id)
        timeline = state.store.list_events(person_id=person_id, limit=200)
        profile_payload = asdict(profile) if profile is not None else None
        timeline_payload = []
        for event in timeline:
            row = event.to_dict()
            row["chat_identity"] = identities.chat_source(event.source_chat, audience=event.audience)
            timeline_payload.append(row)
        return {
            "ok": True,
            "person": _person_payload(person, len(memories), identities=identities),
            "profile": profile_payload,
            "timeline": timeline_payload,
            "memories": [memory_payload(mem) for mem in memories],
        }

    # ------------------------------------------------------------------
    # 群组与意愿
    # ------------------------------------------------------------------

    @app.get("/api/chats")
    async def list_chats(request: Request, limit: int = 200):
        require_session(request)
        limit = max(1, min(int(limit), 500))
        chats = state.store.list_chats(limit=limit)
        # 列表一次读群名，不为名单里的每个群/人发一个 HTTP 请求。
        for platform in getattr(state.bot, "adapters", {}):
            await identities.refresh(platform=platform)
        configured = state.config.decision.groups
        seen = {(row["platform"], row["chat_id"]) for row in chats if row["chat_type"] == "group"}
        for row in state.store.list_identities(kind="group"):
            key = row["platform"], row["object_id"]
            if len(chats) >= limit:
                break
            if key not in seen:
                chats.append({"platform": key[0], "chat_id": key[1], "chat_type": "group",
                              "last_ts": None, "message_count": 0})
                seen.add(key)
        # 仅配置过、没有消息的群也保留卡片；配置键没有平台，不跨平台猜测。
        for chat_id in sorted(configured.keys() - {key[1] for key in seen}):
            group = configured_group(chat_id)
            chats.append({"platform": group["platform"], "chat_id": chat_id, "chat_type": "group",
                          "last_ts": None, "message_count": 0})
        for chat in chats:
            platform, chat_id = chat["platform"], chat["chat_id"]
            chat["chat_identity"] = identities.chat(platform, chat_id, chat_type=chat["chat_type"])
            override = configured.get(chat_id) if chat["chat_type"] == "group" else None
            chat["activity_multiplier"] = override.activity_multiplier if override else 1.0
            chat["quiet_hours"] = list(override.quiet_hours) if override else []
            chat["configured"] = override is not None
            chat["recent_decisions"] = [decision_payload(row) for row in state.store.list_decision_logs(
                chat_id=chat_id, platform=platform, chat_type=chat["chat_type"], limit=3
            )]
        return {"ok": True, "items": chats}

    @app.get("/api/chats/{chat_id}/decision")
    async def get_chat_decision(chat_id: str, request: Request):
        require_session(request)
        return {"ok": True, **_group_decision_payload(state.config, chat_id)}

    @app.put("/api/chats/{chat_id}/decision")
    async def put_chat_decision(chat_id: str, request: Request):
        require_session(request)
        body = await _json_body(request)
        payload = _write_group_decision(state, chat_id, body)
        logger.info("WebUI 更新群 %s 的决策配置", chat_id,
                    extra={"platform": configured_group(chat_id)["platform"], "chat_type": "group", "chat_id": chat_id})
        return {"ok": True, **payload, "restart_required": False}

    @app.get("/api/decisions")
    async def list_decisions(
        request: Request,
        chat_id: str | None = None,
        platform: str | None = None,
        chat_type: str | None = None,
        limit: int = 50,
    ):
        require_session(request)
        limit = max(1, min(int(limit), 200))
        rows = state.store.list_decision_logs(chat_id=chat_id or None, platform=platform, chat_type=chat_type, limit=limit)
        return {"ok": True, "items": [decision_payload(row) for row in rows]}

    @app.get("/api/stickers")
    async def list_stickers(request: Request):
        require_session(request)
        store = state.sticker_store()
        items = []
        for sticker in store.find_by_tags(None):
            items.append({
                "id": sticker.id,
                "tags": list(sticker.tags),
                "image_url": f"/api/stickers/{sticker.id}/image" if store.path_of(sticker) else None,
                "created_at": sticker.created_at,
            })
        return {"ok": True, "items": items}

    @app.get("/api/stickers/{sticker_id}/image")
    async def sticker_image(sticker_id: str, request: Request):
        require_session(request)
        store = state.sticker_store()
        sticker = store.get(sticker_id)
        path = store.path_of(sticker)
        if sticker is None or path is None:
            raise ApiError(404, "表情图片不存在")
        return FileResponse(path)

    @app.delete("/api/stickers/{sticker_id}")
    async def delete_sticker(sticker_id: str, request: Request):
        require_session(request)
        try:
            deleted = state.sticker_store().delete(sticker_id)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(500, f"删除表情失败：{exc}") from exc
        if not deleted:
            raise ApiError(404, "表情不存在")
        return {"ok": True, "id": sticker_id}

    @app.get("/api/computer/audit")
    async def computer_audit(request: Request, limit: int = 30):
        """只读审计：即使工具已关闭，仍可查历史操作；不返回原始参数或输出。"""
        require_session(request)
        rows = state.store.recent_computer_actions(limit=max(1, min(limit, 200)))
        fields = ("who", "context", "tool", "success", "duration_ms", "created_at")
        items = [
            {**{key: row[key] for key in fields}, "args_summary": {
                key: row["args_summary"][key] for key in ("arg_count", "input_bytes")
            }} for row in rows
        ]
        if request.query_params.get("identities", "").lower() in ("1", "true", "yes"):
            for item in items:
                who = str(item.get("who", ""))
                item["person_identity"] = identities.person_id(who)
        return {"ok": True, "items": items}

    @app.get("/api/mcp/status")
    async def mcp_status(request: Request):
        """读取当前连接快照；独立 WebUI 不建立连接，也不假装知道运行状态。"""
        require_session(request)
        manager = getattr(state.bot, "mcp", None)
        if manager is None:
            servers = [
                {"name": item.name, "transport": item.transport,
                 "state": "unavailable" if item.enabled else "disabled", "tool_count": 0, "last_error": ""}
                for item in state.config.mcp.servers
            ]
        else:
            fields = ("name", "transport", "state", "tool_count", "last_error")
            servers = [{key: row[key] for key in fields} for row in manager.status()]
        return {"ok": True, "servers": servers}

    @app.get("/api/emotion")
    async def get_emotion(request: Request, limit: int = 12):
        """情绪与好感度状态卡（只读）：心情 + 好感 Top5/Bottom3 + 最近变化。

        运行中的 bot 用同一个引擎（时间口径与注入一致）；没有 bot 的独立
        WebUI 模式现场建一个，只读不改库。
        """
        require_session(request)
        limit = max(1, min(int(limit), 100))
        # 有运行中的 bot 就用它的引擎（窗口与注入同一份状态）；独立 WebUI
        # 模式没有 bot，现场建一个只读引擎。
        engine = getattr(state.bot, "emotion", None) or EmotionEngine(
            state.store, state.config.emotion
        )
        try:
            payload = engine.status(recent_limit=limit)
        except sqlite3.Error as exc:
            raise ApiError(500, f"读取情绪状态失败：{exc}") from exc
        for item in payload.get("affinity", {}).get("top", []) + payload.get("affinity", {}).get("bottom", []):
            item["person_identity"] = identities.person_id(item.get("person_id", ""), hint=item.get("display_name", ""))
        for item in payload.get("recent_changes", []):
            item["person_identity"] = identities.person_id(item.get("person_id", ""), hint=item.get("display_name", ""))
        return {"ok": True, **payload}

    # ------------------------------------------------------------------
    # 插件
    # ------------------------------------------------------------------

    @app.get("/api/plugins")
    async def list_plugins(request: Request):
        require_session(request)
        hot = state.plugins is not None
        return {
            "ok": True,
            "items": state.plugin_list(),
            "hot_reload": hot,
            "restart_required": not hot,
            "message": (
                "插件默认全部关闭；开关会立即生效，并写回 config.yaml 的 plugins.enabled。"
                if hot
                else "插件默认全部关闭；当前 WebUI 没有运行中的 bot，开关会写入配置，重启后生效。"
            ),
        }

    @app.post("/api/plugins/{name}")
    async def toggle_plugin(name: str, request: Request):
        """启用/停用插件：热生效，并写回 config.yaml 的 plugins.enabled。

        独立 WebUI 模式（没有插件管理器）没有运行中的 bot，只能写配置，
        响应里会如实标注需要重启。
        """
        require_session(request)
        body = await _json_body(request)
        enabled = bool(body.get("enabled"))
        if state.plugins is None:
            return _save_plugin_without_manager(state, name, enabled)
        if not state.set_plugin_enabled(name, enabled):
            # manifest 不合规（含核心版本过低）时把原因带回去，别只说「不存在」
            plugin = state.plugins.plugins.get(name)
            if plugin is not None and not plugin.valid:
                raise ApiError(
                    409, f"插件 {name} 的 manifest 不合规，不能启用：{plugin.invalid_reason}"
                )
            raise ApiError(404, f"插件不存在或启用失败：{name}")
        try:
            _write_plugin_enabled(state)
        except Exception as exc:  # noqa: BLE001 - 已热生效，写盘失败只说清楚
            logger.warning("插件开关写回 config.yaml 失败：%s", exc)
            return {
                "ok": True,
                "items": state.plugin_list(),
                "restart_required": False,
                "persisted": False,
                "message": f"插件已{'启用' if enabled else '停用'}，但写回配置失败：{exc}",
            }
        logger.info("WebUI %s 插件 %s（已写回 config.yaml）", "启用" if enabled else "禁用", name)
        return {
            "ok": True,
            "items": state.plugin_list(),
            "restart_required": False,
            "persisted": True,
            "message": f"插件已{'启用' if enabled else '停用'}，立即生效并已写回配置。",
        }

    # ------------------------------------------------------------------
    # 插件市场
    # ------------------------------------------------------------------

    # 一个进程一个市场服务；索引拉取按请求进行（页面刷新即重新拉），
    # 安装类操作在服务内部串行。
    # 这些路由用同步函数（不是 async def）：索引拉取最多会阻塞 5 秒、
    # 下载与解压更久，交给 FastAPI 的线程池跑，别把整个 WebUI 的事件循环卡住。
    market = MarketService(state)

    @app.get("/api/plugins/{plugin_id}/config")
    def plugin_config(plugin_id: str, request: Request):
        require_session(request)
        try:
            payload = market.get_plugin_config(plugin_id)
        except MarketError as exc:
            raise ApiError(exc.status, exc.message) from exc
        return {"ok": True, **payload}

    @app.put("/api/plugins/{plugin_id}/config")
    async def put_plugin_config(plugin_id: str, request: Request):
        require_session(request)
        body = await _json_body(request)
        try:
            payload = market.save_plugin_config(plugin_id, body.get("values"))
        except MarketError as exc:
            raise ApiError(exc.status, exc.message) from exc
        return {"ok": True, **payload, "restart_required": True, "message": "插件配置已保存，重启 bot 后生效。"}

    @app.get("/api/market")
    def market_snapshot(request: Request):
        """市场页数据：来源标注 + 本地/索引合并后的卡片列表。"""
        require_session(request)
        return {"ok": True, **market.snapshot()}

    def _market_action(func, plugin_id: str):
        try:
            result = func(plugin_id)
        except MarketError as exc:
            raise ApiError(exc.status, exc.message) from exc
        return {"ok": True, **result, "market": market.snapshot()}

    def _market_log(plugin_id: str, action: str):
        """操作完成后记一行日志，方便事后对账。"""
        logger.info("WebUI 从市场%s插件 %s", action, plugin_id)

    @app.post("/api/market/plugins/{plugin_id}/install")
    def market_install(plugin_id: str, request: Request):
        """安装：下载 zip → 校验 manifest → 落盘；默认不启用。"""
        require_session(request)
        result = _market_action(market.install, plugin_id)
        _market_log(plugin_id, "安装")
        return result

    @app.post("/api/market/plugins/{plugin_id}/update")
    def market_update(plugin_id: str, request: Request):
        """更新：换代码与 manifest，保留插件配置与启用状态。"""
        require_session(request)
        result = _market_action(market.update, plugin_id)
        _market_log(plugin_id, "更新")
        return result

    @app.post("/api/market/plugins/{plugin_id}/uninstall")
    def market_uninstall(plugin_id: str, request: Request):
        """卸载：启用中先停用，再删目录。"""
        require_session(request)
        result = _market_action(market.uninstall, plugin_id)
        _market_log(plugin_id, "卸载")
        return result

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    async def get_logs(request: Request, limit: int = 200, level: str | None = None, q: str | None = None):
        require_session(request)
        limit = max(1, min(int(limit), 1000))
        # 按昵称或 ID 也能查；同一页重复的发送者/会话只解析一次。
        person_for = lru_cache(maxsize=1000)(identities.person_id)
        chat_for = lru_cache(maxsize=1000)(identities.chat)
        query = (q or "").strip().casefold()
        items = []
        for row in log_records(limit=1000 if query else limit, level=level):
            item = dict(row)
            platform = row.get("platform", "")
            person_id = row.get("person_id")
            if not person_id and platform and row.get("sender_id"):
                person_id = f"{platform}:{row['sender_id']}"
            if person_id:
                item["person_identity"] = person_for(person_id, hint=row.get("sender_name", ""),
                                                  group_id=row.get("chat_id", "") if row.get("chat_type") == "group" else "")
            if row.get("chat_id") and row.get("chat_type"):
                item["chat_identity"] = chat_for(platform, row["chat_id"], chat_type=row["chat_type"])
            fields = [row["message"], row["logger"], person_id or ""]
            for key in ("person_identity", "chat_identity"):
                if value := item.get(key):
                    fields.extend([value["name"], value["id"]])
            if not query or any(query in value.casefold() for value in fields):
                items.append(item)
                if len(items) == limit:
                    break
        return {"ok": True, "items": items}

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(request: Request):
        require_session(request)
        path = state.config_path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        # 整个原文编辑器无法可靠遮盖命令行、URL、任意 env/header 里的密钥。
        # 配了 MCP 时不下发原文，仍可用结构化表单编辑，或在本机编辑文件。
        try:
            raw = yaml.safe_load(text)
            section = raw.get("mcp") if isinstance(raw, dict) else None
            raw_hidden = bool(section.get("servers")) if isinstance(section, dict) else bool(section)
        except yaml.YAMLError:
            raw_hidden = True
        return {
            "ok": True,
            "path": str(path),
            "text": "" if raw_hidden else text,
            "raw_hidden": raw_hidden,
            "exists": path.exists(),
            "effective": _mask_config(_config_to_dict(state.config)),
            "structured": structured_config(state.config),
            "group_identities": {key: configured_group(key) for key in state.config.decision.groups},
            "layout": config_layout(),
            "plugins_available": state.plugin_list(),
            "restart_fields": _pending_restart_fields(state),
        }

    @app.put("/api/config")
    async def put_config(request: Request):
        require_session(request)
        body = await _json_body(request)
        if "config" in body:
            return await _save_structured_config(state, body.get("config"))
        # 兼容旧的原始 YAML 文本模式
        text = str(body.get("text") or "")
        try:
            backup = save_config_text(state.config_path, text)
        except ValueError as exc:
            raise ApiError(422, f"配置校验失败：{exc}") from exc
        logger.info("WebUI 保存配置到 %s", state.config_path)
        return {
            "ok": True,
            "mode": "text",
            "restart_required": True,
            "requires_restart": ["全部字段（原始 YAML 模式）"],
            "applied": [],
            "message": "配置已保存。原始 YAML 文本模式下需要重启 bot 才能生效。",
            "backup": str(backup) if backup else None,
        }

    @app.post("/api/restart")
    async def restart(request: Request):
        require_session(request)
        try:
            command = state.request_restart()
        except Exception as exc:  # noqa: BLE001 - 启动命令构造失败要说清楚
            raise ApiError(500, f"重启失败：{exc}") from exc
        logger.info("WebUI 请求立即重启：%s", " ".join(command))
        return {
            "ok": True,
            "message": "进程正在重启，稍后会自动刷新页面。",
            "command": command,
            "webui_only": state.webui_only,
        }

    # ------------------------------------------------------------------
    # 人设
    # ------------------------------------------------------------------

    @app.get("/api/persona")
    async def get_persona(request: Request):
        require_session(request)
        return _persona_payload(state)

    @app.put("/api/persona")
    async def put_persona(request: Request):
        require_session(request)
        body = await _json_body(request)
        text = str(body.get("text") or "")
        if not text.strip():
            raise ApiError(422, "人格内容不能为空")
        result = _apply_persona(state, text)
        logger.info("WebUI 保存人格到 %s（%s）", state.config.persona_path, result["message"])
        return {"ok": True, **result, "persona": _persona_payload(state)}

    @app.post("/api/persona/reset")
    async def reset_persona(request: Request):
        require_session(request)
        result = _apply_persona(state, DEFAULT_PERSONA)
        logger.info("WebUI 恢复默认人格（%s）", result["message"])
        return {"ok": True, **result, "persona": _persona_payload(state)}

    @app.get("/api/persona/preview")
    async def preview_persona(request: Request):
        require_session(request)
        persona = state.bot_persona()
        if persona is None:
            path, exists, file_text, _ = _persona_file_info(state)
            persona, _source = _effective_persona(state, file_text, exists)
        return {
            "ok": True,
            "prompt": _preview_prompt(persona),
            "source": "bot" if state.bot_persona() is not None else "file",
        }

    # ------------------------------------------------------------------
    # 静态页
    # ------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    # 浏览器图标：跳到 bot 当前登录 QQ 的头像（腾讯 qlogo 外部接口现取），
    # 解析不到账号时回退内置 favicon.svg。解析结果缓存 1 小时，避免每次
    # 取图标都打协议端。
    _bot_avatar_cache: dict[str, Any] = {"uid": "", "expires": 0.0}

    async def _bot_avatar_target() -> str:
        now = time.time()
        cached = _bot_avatar_cache["uid"]
        if cached and now < _bot_avatar_cache["expires"]:
            return avatar_url("snowluma", cached)
        uid = ""
        adapters_cfg = getattr(state.config, "adapters", {}) or {}
        snow_cfg = adapters_cfg.get("snowluma") if isinstance(adapters_cfg, dict) else None
        if isinstance(snow_cfg, dict):
            uid = str(snow_cfg.get("self_id") or "").strip()
        bot = state.bot
        adapter = (getattr(bot, "adapters", {}) or {}).get("snowluma") if bot is not None else None
        if not uid and adapter is not None:
            uid = str(getattr(adapter, "self_id", "") or "").strip()
        if not uid and adapter is not None and hasattr(adapter, "call_api"):
            try:
                payload = await adapter.call_api("get_login_info", {})
                uid = str((payload.get("data") or {}).get("user_id") or "").strip()
            except Exception:  # noqa: BLE001 - 取不到就回退默认图标，不影响页面
                logger.debug("获取 bot 登录信息失败，图标回退默认", exc_info=True)
        url = avatar_url("snowluma", uid) if uid else ""
        if url:
            _bot_avatar_cache.update(uid=uid, expires=now + 3600)
            return url
        return "/static/favicon.svg"

    @app.get("/bot-avatar", include_in_schema=False)
    async def bot_avatar():
        return RedirectResponse(
            await _bot_avatar_target(),
            status_code=302,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(400, "请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return body


def _read_raw_config(path: Path) -> dict[str, Any]:
    """读取 config.yaml 的原始字典；不存在或为空返回 ``{}``。"""
    import yaml

    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ApiError(500, "config.yaml 顶层不是键值对，无法写入")
    return data


def _pending_restart_fields(state: WebState) -> list[str]:
    """还没重启生效的字段：本次保存记下的 + 磁盘与内存的差异。"""
    fields = list(state.extras.get("pending_restart") or [])
    if state.config_path.exists():
        try:
            disk = Config.load(state.config_path)
        except Exception:  # noqa: BLE001 - 文件坏了不阻塞查看页面
            disk = None
        if disk is not None:
            for field in restart_fields(state.config, disk):
                if field not in fields:
                    fields.append(field)
    return fields


async def _save_structured_config(state: WebState, payload: Any) -> dict[str, Any]:
    """结构化保存：校验 → 合并 → 写盘 → 热应用 → 返回生效方式。"""
    import yaml

    path = state.config_path
    try:
        update = validate_structured(payload)
        merged = merge_config(_read_raw_config(path), update)
    except ConfigValidationError as exc:
        raise ApiError(422, f"配置校验失败：{exc}", fields=exc.fields) from exc
    try:
        new_config = Config.from_raw(merged)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            422, f"配置校验失败：{exc}", fields=[{"field": "config", "message": str(exc)}]
        ) from exc

    # 安全底线与启动时一致：绑非本地地址必须有口令，不允许通过表单绕过
    try:
        check_bind_security(new_config.webui)
    except BindSecurityError as exc:
        raise ApiError(
            422, str(exc), fields=[{"field": "webui.password", "message": str(exc)}]
        ) from exc

    text = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False)
    try:
        backup = save_config_text(path, text)
    except ValueError as exc:
        raise ApiError(422, f"配置校验失败：{exc}") from exc

    # 先把旧配置的快照留下（apply_config 会改 state.config）
    before = _config_snapshot(state.config)
    requires_restart = state.apply_config(new_config)
    after = _config_snapshot(new_config)
    # 独立 WebUI 模式没有运行中的插件管理器，插件开关只能写配置、重启后生效
    if (
        state.plugins is None
        and before.get("plugins.enabled") != after.get("plugins.enabled")
        and "plugins.enabled" not in requires_restart
    ):
        requires_restart.append("plugins.enabled")

    # 逐个提交字段判定生效方式：只看真正改过的叶子，避免「全部已生效」的误导
    resolution: dict[str, str] = {}
    applied_groups: list[str] = []

    def _is_restart(field: str) -> bool:
        return any(
            field == item or field.startswith(item + ".") for item in requires_restart
        )

    for group, value in update.items():
        if group == "database_path":
            leaves = ["database_path"]
        else:
            leaves = _leaf_paths(group, value)
            # 群覆盖单独整段比较（增删群、改某群参数都只算这一项），
            # 避免每个群各占一行，也避免删群时被当成「没有变化」
            if group == "decision" and isinstance(value, dict) and "groups" in value:
                leaves = [path for path in leaves if not path.startswith("decision.groups.")]
        if not leaves and not (group == "decision" and isinstance(value, dict) and "groups" in value):
            continue
        hot_changed = False
        for leaf in leaves:
            if _is_restart(leaf):
                resolution[leaf] = "restart"
                continue
            resolution[leaf] = "hot"
            if _get_path(before, leaf) != _get_path(after, leaf):
                hot_changed = True
        if group == "decision" and isinstance(value, dict) and "groups" in value:
            # 群覆盖整段比较：删掉某个群后 leaf 列表可能为空，仍要报生效
            resolution["decision.groups"] = "hot"
            if _get_path(before, "decision.groups") != _get_path(after, "decision.groups"):
                hot_changed = True
        if hot_changed:
            applied_groups.append(group)

    old_password = before.get("webui.password")
    logger.info(
        "WebUI 保存结构化配置到 %s（热生效：%s；需重启：%s）",
        path,
        ", ".join(applied_groups) or "无",
        ", ".join(requires_restart) or "无",
    )
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "structured",
        "applied": applied_groups,
        "requires_restart": requires_restart,
        "restart_required": bool(requires_restart),
        "resolution": resolution,
        "message": _save_message(applied_groups, requires_restart),
        "backup": str(backup) if backup else None,
        "structured": structured_config(new_config),
        # 口令改了当前会话会失效，提示前端重新登录
        "relogin": bool(
            "webui.password" in resolution
            and old_password != new_config.webui.password
        ),
    }
    return payload


def _leaf_paths(prefix: str, value: Any) -> list[str]:
    """把嵌套字典展开成点分字段路径；列表/标量视为叶子。"""
    if isinstance(value, dict) and value:
        paths: list[str] = []
        for key, item in value.items():
            paths.extend(_leaf_paths(f"{prefix}.{key}", item))
        return paths
    return [prefix]


def _config_snapshot(config: Config) -> dict[str, Any]:
    """把 Config 摊平成点分路径 → 值的字典，替代字段可以直接比较。"""
    from dataclasses import asdict

    data = asdict(config)
    flat: dict[str, Any] = {"persona.path": config.persona_path}
    flat.update({f"persona.{key}": value for key, value in config.persona_example_options().items()})
    for section, value in data.items():
        if not isinstance(value, dict):
            flat[section] = value
            continue
        for key, item in value.items():
            path = f"{section}.{key}"
            if isinstance(item, dict) and item:
                for sub_key, sub_item in item.items():
                    flat[f"{path}.{sub_key}"] = sub_item
            else:
                flat[path] = item
    # 「留空」在这几个字段上有两种等价写法（None 与空串），比较时统一，
    # 否则「打开设置直接保存」会被误判为改过；``llm.models`` 也是整段比较
    for path in ("llm.extract_model", "llm.consolidate_model", "llm.gate_model", "llm.models"):
        if flat.get(path) is None:
            flat[path] = ""
    return flat


def _get_path(flat: dict[str, Any], path: str) -> Any:
    """按点分路径从摊平字典取值；没这个键时按前缀取（例如整段 groups）。"""
    if path in flat:
        return flat[path]
    prefix = path + "."
    matches = {key: value for key, value in flat.items() if key.startswith(prefix)}
    return matches or flat.get(path)


def _save_message(applied: list[str], requires_restart: list[str]) -> str:
    groups = {
        "llm": "模型与接口",
        "memory": "记忆",
        "chat": "聊天与回复",
        "decision": "回复意愿",
        "computer": "操作电脑",
        "generation": "生成",
        "emotion": "情绪与好感度",
        "plugins": "插件",
        "webui": "WebUI",
        "persona": "人格",
        "adapters": "适配器",
        "log_level": "日志级别",
        "background_check_minutes": "后台检查",
        "database_path": "数据库路径",
    }
    parts = []
    if applied:
        parts.append("已立即生效：" + "、".join(groups.get(g, g) for g in applied))
    if requires_restart:
        parts.append(
            "需重启后生效：" + describe_restart_fields(requires_restart)
        )
    return "。".join(parts) or "配置已保存（与当前一致，无需变更）"


def _config_to_dict(config) -> dict[str, Any]:
    """设置页展示用的「当前生效配置」，口令字段留空。"""
    from dataclasses import asdict

    data = asdict(config)
    data["mcp"] = mcp_form(config.mcp)
    data["llm"]["api_key"] = config.llm.api_key
    data["webui"]["password"] = ""
    return data


# 供测试/外部调用：明确导出
__all__ = ["create_app", "ApiError", "BindSecurityError", "STATIC_DIR"]
