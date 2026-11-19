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
- ``GET  /api/plugins``         插件列表
- ``POST /api/plugins/{name}``  启用/禁用（运行时生效）
- ``GET  /api/logs``            运行日志尾部
- ``GET  /api/config``          查看 config.yaml
- ``PUT  /api/config``          编辑并校验后保存，返回需重启提示
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import GroupDecisionConfig, is_loopback_host
from ..decision import parse_quiet_hours
from ..memory.store import make_person_id
from .auth import COOKIE_NAME, BindSecurityError, check_bind_security, mask_secret
from .logbuffer import records as log_records
from .state import WebState, save_config_text

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

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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


def _person_payload(person, count: int = 0) -> dict[str, Any]:
    return {
        "person_id": person.person_id,
        "platform": person.platform,
        "platform_uid": person.platform_uid,
        "display_name": person.display_name,
        "first_seen": person.first_seen,
        "last_seen": person.last_seen,
        "memory_count": count,
    }


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


def _write_group_decision(config, config_path: Path, chat_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """把群覆盖写回 config.yaml 的 ``decision.groups``，保留其余内容与顺序。"""
    import yaml

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
        _apply_groups(config, data)
        save_config_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return _group_decision_payload(config, chat_id)

    entry: dict[str, Any] = {"activity_multiplier": multiplier}
    if base is not None:
        entry["base"] = base
    if quiet:
        entry["quiet_hours"] = quiet
    groups[str(chat_id)] = entry
    _apply_groups(config, data)
    save_config_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return _group_decision_payload(config, chat_id)


def _apply_groups(config, data: dict[str, Any]) -> None:
    """保存成功后同步内存中的配置，页面立即看到新值。"""
    from ..config import _load_decision

    config.decision = _load_decision(data.get("decision") or {})


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
        return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)

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
        return await call_next(request)

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
            "recent_decisions": state.recent_decisions(limit=10),
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
            "items": [_memory_payload(mem) for mem in memories],
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
        return {"ok": True, "item": _memory_payload(memory)}

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
        return {"ok": True, "item": _memory_payload(updated)}

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
        return {"ok": True, "items": [_log_payload(entry) for entry in logs]}

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
    # 人物
    # ------------------------------------------------------------------

    @app.get("/api/people")
    async def list_people(request: Request, limit: int = 200, q: str | None = None):
        require_session(request)
        limit = max(1, min(int(limit), 500))
        keyword = (q or "").strip().lower()
        rows = state.store.list_people_with_counts(limit=limit)
        items = [_person_payload(person, count) for person, count in rows]
        if keyword:
            items = [
                item
                for item in items
                if keyword in item["display_name"].lower()
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
        return {
            "ok": True,
            "person": _person_payload(person, len(memories)),
            "memories": [_memory_payload(mem) for mem in memories],
        }

    # ------------------------------------------------------------------
    # 群组与意愿
    # ------------------------------------------------------------------

    @app.get("/api/chats")
    async def list_chats(request: Request, limit: int = 200):
        require_session(request)
        chats = state.store.list_chats(limit=max(1, min(int(limit), 500)))
        configured = state.config.decision.groups
        for chat in chats:
            override = configured.get(str(chat["chat_id"]))
            chat["activity_multiplier"] = override.activity_multiplier if override else 1.0
            chat["quiet_hours"] = list(override.quiet_hours) if override else []
            chat["configured"] = override is not None
            chat["recent_decisions"] = state.store.list_decision_logs(
                chat_id=str(chat["chat_id"]), limit=3
            )
        return {"ok": True, "items": chats}

    @app.get("/api/chats/{chat_id}/decision")
    async def get_chat_decision(chat_id: str, request: Request):
        require_session(request)
        return {"ok": True, **_group_decision_payload(state.config, chat_id)}

    @app.put("/api/chats/{chat_id}/decision")
    async def put_chat_decision(chat_id: str, request: Request):
        require_session(request)
        body = await _json_body(request)
        payload = _write_group_decision(state.config, state.config_path, chat_id, body)
        logger.info("WebUI 更新群 %s 的决策配置", chat_id)
        return {"ok": True, **payload, "restart_required": False}

    @app.get("/api/decisions")
    async def list_decisions(
        request: Request,
        chat_id: str | None = None,
        limit: int = 50,
    ):
        require_session(request)
        limit = max(1, min(int(limit), 200))
        items = state.store.list_decision_logs(chat_id=chat_id or None, limit=limit)
        return {"ok": True, "items": items}

    # ------------------------------------------------------------------
    # 插件
    # ------------------------------------------------------------------

    @app.get("/api/plugins")
    async def list_plugins(request: Request):
        require_session(request)
        return {"ok": True, "items": state.plugin_list()}

    @app.post("/api/plugins/{name}")
    async def toggle_plugin(name: str, request: Request):
        require_session(request)
        body = await _json_body(request)
        enabled = bool(body.get("enabled"))
        if state.plugins is None:
            raise ApiError(503, "插件管理器未初始化")
        if not state.plugins.set_enabled(name, enabled):
            raise ApiError(404, f"插件不存在或启用失败：{name}")
        logger.info("WebUI %s 插件 %s", "启用" if enabled else "禁用", name)
        return {"ok": True, "items": state.plugin_list()}

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    async def get_logs(request: Request, limit: int = 200, level: str | None = None, q: str | None = None):
        require_session(request)
        limit = max(1, min(int(limit), 1000))
        return {"ok": True, "items": log_records(limit=limit, level=level, query=q)}

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(request: Request):
        require_session(request)
        path = state.config_path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {
            "ok": True,
            "path": str(path),
            "text": text,
            "exists": path.exists(),
            "effective": _mask_config(_config_to_dict(state.config)),
        }

    @app.put("/api/config")
    async def put_config(request: Request):
        require_session(request)
        body = await _json_body(request)
        text = str(body.get("text") or "")
        try:
            backup = save_config_text(state.config_path, text)
        except ValueError as exc:
            raise ApiError(422, f"配置校验失败：{exc}") from exc
        logger.info("WebUI 保存配置到 %s", state.config_path)
        return {
            "ok": True,
            "restart_required": True,
            "message": "配置已保存。v1 需要重启 bot 才能生效。",
            "backup": str(backup) if backup else None,
        }

    # ------------------------------------------------------------------
    # 静态页
    # ------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

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


def _config_to_dict(config) -> dict[str, Any]:
    """设置页展示用的「当前生效配置」，口令字段留空。"""
    from dataclasses import asdict

    data = asdict(config)
    data["llm"]["api_key"] = config.llm.api_key
    data["webui"]["password"] = ""
    return data


# 供测试/外部调用：明确导出
__all__ = ["create_app", "ApiError", "BindSecurityError", "STATIC_DIR"]
