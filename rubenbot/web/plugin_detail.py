"""插件详情：把 manifest、README、授权与实际运行状态拼成详情页数据。

详情页只读，数据全部来自本进程已有的东西：

- 市场快照（``MarketService.snapshot``）给出已装 / 可装合并后的条目，
  版本、作者、分类、权限声明与「可更新」等状态都以它为准；
- 插件管理器（或独立 WebUI 的目录扫描）给出命令、钩子计数、加载状态；
- 插件目录里的 ``README.md`` 原文（限长、拒绝符号链接）作为介绍；
- manifest 的 ``config_schema`` 规范化后作为配置说明，已装插件还带上当前
  值（密钥字段不回声）。

本模块只组装数据，不写任何文件；路由层负责鉴权和错误转换。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..plugins.config_schema import normalize_schema, type_label
from ..plugins.manifest import PERMISSION_INFO, read_manifest
from .market import MarketError

logger = logging.getLogger(__name__)

# README 原文上限：详情页是给人看的，超大文件没有展示意义，也避免读盘失控
README_MAX_BYTES = 128 * 1024
README_NAMES = ("README.md", "README.MD", "readme.md", "README.rst", "README.txt")

HOOK_TEXT = {
    "on_message": "每条消息（可拦截）",
    "on_before_reply": "生成回复前拼上下文",
    "on_after_reply": "发出回复后",
    "on_start": "启用时启动后台任务",
    "on_stop": "停用时收尾后台任务",
    "on_load": "加载完成后",
    "on_unload": "停用 / 卸载 / 重载前",
    "on_config_change": "WebUI 保存配置后",
}


class PluginDetailError(ValueError):
    """详情读取失败；``status`` 直接给路由层用。"""

    def __init__(self, message: str, *, status: int = 404):
        super().__init__(message)
        self.message = message
        self.status = status


def _find_item(snapshot: dict[str, Any], plugin_id: str) -> dict[str, Any] | None:
    """按 manifest id 或目录名找市场条目（WebUI 传哪个都能定位）。"""
    for item in snapshot.get("items") or []:
        if item.get("id") == plugin_id or item.get("plugin_name") == plugin_id:
            return item
    return None


def _plugin_rows(state) -> list[dict[str, Any]]:
    """运行时插件列表；扫描失败时返回空列表（详情页退回只有目录信息）。"""
    try:
        return list(state.plugin_list())
    except Exception as exc:  # noqa: BLE001 - 详情页不能因为扫描失败整个打不开
        logger.warning("读取插件运行时信息失败：%s", exc)
        return []


def _runtime_row(state, item: dict[str, Any]) -> dict[str, Any] | None:
    """从插件列表里取运行时信息（命令、钩子计数、加载状态）。"""
    names = {str(item.get("plugin_name") or ""), str(item.get("id") or "")}
    for row in _plugin_rows(state):
        if row.get("name") in names:
            return row
    return None


def _directory_of(item: dict[str, Any], runtime: dict[str, Any] | None) -> Path | None:
    """插件目录：本地条目优先，其次运行时行里的路径。"""
    path = item.get("local_path") or (runtime or {}).get("path")
    return Path(str(path)) if path else None


def _read_readme(directory: Path | None, root: Path) -> dict[str, Any] | None:
    """读取插件 README 原文；越界、符号链接、读不了都按「没有」处理。"""
    if directory is None:
        return None
    try:
        directory = directory.resolve()
    except OSError:
        return None
    if directory == root or root not in directory.parents:
        return None
    for name in README_NAMES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("读取插件 README 失败（%s）：%s", path, exc)
            return None
        truncated = len(raw) > README_MAX_BYTES
        text = raw[:README_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n…（原文超过 128 KiB，已截断）"
        return {"name": name, "text": text, "truncated": truncated}
    return None


def _permissions(item: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
    """权限清单：声明、中文说明、是否已授权（未安装的插件不做授权判断）。"""
    meta = item.get("permission_meta")
    if not meta:
        meta = [
            {"id": name, **PERMISSION_INFO.get(name, {"label": name, "description": "", "high_risk": False})}
            for name in item.get("permissions") or []
        ]
    granted = set(
        (runtime or {}).get("permissions", {}).get("granted")
        or item.get("granted_permissions")
        or []
    )
    installed = bool(item.get("installed"))
    rows = []
    for info in meta:
        row = {
            "id": info.get("id"),
            "label": info.get("label") or info.get("id"),
            "description": info.get("description") or "",
            "high_risk": bool(info.get("high_risk")),
            "declared": True,
        }
        if installed:
            row["granted"] = row["id"] in granted
        rows.append(row)
    return {
        "items": rows,
        "declared": [row["id"] for row in rows],
        "granted": sorted(granted) if installed else [],
        "all_granted": installed and bool(rows) and all(row.get("granted") for row in rows),
    }


def _hooks(item: dict[str, Any], runtime: dict[str, Any] | None) -> list[dict[str, Any]]:
    """钩子清单：声明 +（已加载时）实际注册数量。"""
    declared = list(item.get("hooks") or [])
    counts = (runtime or {}).get("hooks") or {}
    rows = []
    for name in declared:
        count = counts.get(name)
        rows.append({
            "name": name,
            "description": HOOK_TEXT.get(name, name),
            "registered": None if runtime is None else int(count or 0),
        })
    return rows


def _triggers(
    declared: list[str],
    schema: list[dict[str, Any]],
    runtime: dict[str, Any] | None,
) -> list[str]:
    """触发方式：manifest / 索引显式声明的优先，其次从 schema 触发词默认值推导。"""
    explicit = [str(word) for word in declared or [] if str(word).strip()]
    if explicit:
        return explicit
    found: list[str] = []
    for spec in schema:
        key = str(spec.get("key") or "")
        if "trigger" not in key.lower():
            continue
        default = spec.get("default")
        if isinstance(default, list):
            found.extend(str(word) for word in default if str(word).strip())
    if found:
        return found
    commands = (runtime or {}).get("commands") or []
    if commands:
        return [f"/{name}" for name in commands]
    return []


def _schema_docs(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """配置说明：每个字段的类型、默认值、范围 / 可选值与用途。"""
    docs = []
    for spec in schema:
        kind = str(spec.get("type") or "string")
        row = {
            "key": spec.get("key"),
            "label": spec.get("label") or spec.get("description") or spec.get("key"),
            "description": spec.get("description") or "",
            "type": kind,
            "type_label": type_label(kind),
            "default": spec.get("default"),
            "secret": bool(spec.get("secret")),
            "options": list(spec.get("options") or []),
        }
        limits: list[str] = []
        if spec.get("min") is not None or spec.get("max") is not None:
            low = spec.get("min")
            high = spec.get("max")
            if low is not None and high is not None:
                limits.append(f"{low} – {high}")
            elif low is not None:
                limits.append(f"≥ {low}")
            else:
                limits.append(f"≤ {high}")
        if spec.get("min_length") is not None or spec.get("max_length") is not None:
            low = spec.get("min_length")
            high = spec.get("max_length")
            if low is not None and high is not None:
                limits.append(f"{low} – {high} 个")
            elif low is not None:
                limits.append(f"≥ {low} 个")
            else:
                limits.append(f"≤ {high} 个")
        row["range"] = "，".join(limits)
        docs.append(row)
    return docs


def _current_config(
    market,
    plugin_id: str,
    item: dict[str, Any],
    schema: list[dict[str, Any]],
) -> dict[str, Any]:
    """已装插件的当前配置值（密钥字段不回显），失败时只返回 schema。"""
    values: dict[str, Any] = {}
    secrets: dict[str, bool] = {}
    if item.get("installed") and not item.get("invalid") and schema:
        try:
            payload = market.get_plugin_config(plugin_id)
            values = payload.get("values") or {}
            secrets = payload.get("secrets") or {}
        except MarketError as exc:
            logger.debug("插件 %s 当前配置不可读：%s", plugin_id, exc.message)
    return {"values": values, "secrets": secrets}


def _manifest_of(item: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
    """取插件 manifest：运行时行优先，其次从插件目录读一遍（详情只读）。

    未安装的插件没有本地目录，manifest 为空；索引里声明的可选字段
    （``triggers`` / ``repository`` / ``config_schema``）由
    :func:`_index_manifest` 拼成同形状的映射。
    """
    manifest = (runtime or {}).get("manifest")
    if isinstance(manifest, dict) and manifest:
        return manifest
    manifest = item.get("manifest")
    if isinstance(manifest, dict) and manifest:
        return manifest
    path = item.get("local_path") or (runtime or {}).get("path")
    if path:
        try:
            loaded, _reason, _too_old = read_manifest(path)
        except Exception as exc:  # noqa: BLE001 - 详情页不能因为坏 manifest 打不开
            logger.debug("读取插件 manifest 失败（%s）：%s", path, exc)
            return {}
        if loaded is not None:
            return loaded.as_dict()
    return _index_manifest(item)


def _index_manifest(item: dict[str, Any]) -> dict[str, Any]:
    """未安装插件：用索引条目里声明过的可选字段凑出 manifest 形状。"""
    if item.get("installed"):
        return {}
    declared = {
        "repository": item.get("repository") or "",
        "triggers": list(item.get("triggers") or []),
        "config_schema": [dict(spec) for spec in item.get("config_schema") or []],
    }
    return declared if any(declared.values()) else {}


def plugin_detail(state, market, plugin_id: str) -> dict[str, Any]:
    """组装一个插件的完整详情。找不到时抛 :class:`PluginDetailError`。"""
    plugin_id = str(plugin_id or "").strip()
    if not plugin_id:
        raise PluginDetailError("插件 id 不能为空", status=422)
    try:
        snapshot = market.snapshot()
    except Exception as exc:  # noqa: BLE001 - 索引故障不该让本地详情打不开
        logger.warning("读取插件市场快照失败，详情退回本地信息：%s", exc)
        snapshot = {"items": []}
    item = _find_item(snapshot, plugin_id)
    runtime = _runtime_row(state, item) if item is not None else None
    if item is None:
        # 市场快照按 id 合并；再按目录名从运行时列表兜底找一次
        for row in _plugin_rows(state):
            if plugin_id in (row.get("name"), (row.get("manifest") or {}).get("id")):
                runtime = row
                break
        if runtime is None:
            raise PluginDetailError(f"插件不存在：{plugin_id}", status=404)
        manifest = runtime.get("manifest") or {}
        item = {
            "id": manifest.get("id") or runtime.get("name"),
            "plugin_name": runtime.get("name"),
            "name": manifest.get("name") or runtime.get("name"),
            "version": manifest.get("version"),
            "description": manifest.get("description") or "",
            "author": manifest.get("author") or "",
            "permissions": list(manifest.get("permissions") or []),
            "permission_meta": [
                {"id": name, **PERMISSION_INFO.get(name, {"label": name, "description": "", "high_risk": False})}
                for name in manifest.get("permissions") or []
            ],
            "hooks": list(manifest.get("hooks") or []),
            "repository": manifest.get("repository") or "",
            "triggers": list(manifest.get("triggers") or []),
            "category": manifest.get("category") or "工具",
            "icon": manifest.get("icon") or "",
            "installed": True,
            "enabled": bool(runtime.get("enabled")),
            "loaded": bool(runtime.get("loaded")),
            "state": "enabled" if runtime.get("enabled") else "installed",
            "invalid": not runtime.get("valid", False),
            "invalid_reason": runtime.get("invalid_reason"),
            "core_too_old": bool(runtime.get("core_too_old")),
            "updatable": False,
            "latest_version": None,
            "local_path": runtime.get("path"),
        }

    manifest = _manifest_of(item, runtime)
    fields = normalize_schema(manifest.get("config_schema") or [])
    schema = [field.as_dict() for field in fields]

    directory = _directory_of(item, runtime)
    root = Path(str(getattr(state.config.plugins, "dir", "plugins") or "plugins")).resolve()

    triggers = _triggers(item.get("triggers") or [], schema, runtime)
    permissions = _permissions(item, runtime)
    hooks = _hooks(item, runtime)
    # 市场条目按 manifest id 合并；配置读写也要按这个 id 走（路由可能收到目录名）
    values = _current_config(market, str(item.get("id") or plugin_id), item, schema)

    enabled = bool(item.get("enabled"))
    installed = bool(item.get("installed"))
    valid = not item.get("invalid")
    if not installed:
        status_text = "未安装"
    elif not valid:
        status_text = "已安装 · manifest 不合规"
    elif enabled:
        status_text = "已启用"
    else:
        status_text = "已安装 · 未启用"

    return {
        "id": item.get("id") or plugin_id,
        "plugin_name": item.get("plugin_name") or plugin_id,
        "name": item.get("name") or plugin_id,
        "version": item.get("version"),
        "latest_version": item.get("latest_version"),
        "description": item.get("description") or "",
        "author": item.get("author") or "",
        "category": item.get("category") or "工具",
        "icon": item.get("icon") or "",
        "repository": item.get("repository") or manifest.get("repository") or "",
        "installed": installed,
        "enabled": enabled,
        "loaded": bool(item.get("loaded")),
        "valid": valid,
        "invalid_reason": item.get("invalid_reason"),
        "core_too_old": bool(item.get("core_too_old")),
        "updatable": bool(item.get("updatable")),
        "state": item.get("state"),
        "status_text": status_text,
        "can_configure": bool(installed and valid and schema),
        "hot_config": bool((runtime or {}).get("hot_config")),
        "local_path": item.get("local_path"),
        "readme": _read_readme(directory, root),
        "permissions": permissions,
        "hooks": hooks,
        "commands": list((runtime or {}).get("commands") or []),
        "triggers": triggers,
        "schema": schema,
        "schema_docs": _schema_docs(schema),
        "config": values,
        "lifecycle": {
            "hot_config": bool((runtime or {}).get("hot_config")),
            "active": bool((runtime or {}).get("lifecycle")),
        },
    }


__all__ = ["PluginDetailError", "plugin_detail"]
