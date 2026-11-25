"""插件市场：索引拉取、本地插件合并、安装 / 更新 / 卸载。

市场页的数据都从这里来，路由只负责鉴权和转发：

- **索引**：``market.index_url`` 指向索引仓库的 ``plugins.json``；留空或拉取
  失败（5 秒超时）时回退到随框架发布的官方插件（读取仓库 ``plugins/`` 里
  各插件的 manifest），页面会带着来源与失败原因，不会静默空白。
- **合并**：本地插件（已安装）与索引条目按 manifest 的 ``id`` 合成一行，
  输出统一的状态：``not_installed`` / ``installed`` / ``enabled``，附加
  ``updatable`` / ``invalid`` / ``core_too_old`` 标记。
- **安装 / 更新**：下载 zip（支持 http(s) 与本地路径，测试用本地 fixture），
  先校验每个条目的落点都在目标目录内（防路径穿越，整包拒绝），再解压、
  校验 manifest 与 ``min_core_version``，最后原子换目录。安装后**不自动启用**。
- **卸载**：先停用再删目录；插件自己的配置文件（如 ``config.yaml``）随目录
  一起删除，但 ``config.yaml`` 里的其它配置段不受影响。更新时插件目录里的
  ``config.yaml`` 会被保留，启用状态也保持不变。

安装类操作串行执行（一把进程内锁），避免两个请求同时改同一个目录。
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import __version__ as CORE_VERSION
from ..plugins.config_schema import (
    ConfigValueError,
    coerce_value,
    effective_values,
    normalize_schema,
    validate_values,
)
from ..plugins.manifest import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    PERMISSION_INFO,
    PERMISSIONS,
    Manifest,
    ManifestError,
    load_manifest,
    version_less,
)

logger = logging.getLogger(__name__)

# 随框架发布的官方插件目录（仓库根目录下的 plugins/）
OFFICIAL_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"

# 索引条目里必须有的字段；download_url / category / icon / hooks 可选
INDEX_REQUIRED = ("id", "name", "version", "description", "author", "permissions", "path")

INDEX_TIMEOUT_SECONDS = 5.0
DOWNLOAD_TIMEOUT_SECONDS = 30.0

# 更新时保留的插件本地文件（插件自己的配置，不属于代码）
PRESERVED_FILES = ("config.yaml",)


class MarketError(ValueError):
    """市场操作失败；路由层转成 4xx/5xx 响应。

    ``fields`` 只在配置校验失败时出现，形如
    ``[{"field": "cooldown_seconds", "message": "不能小于 0"}]``，供前端定位
    出错控件；其它错误的 ``fields`` 为空列表。
    """

    def __init__(self, message: str, *, status: int = 400, fields: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.fields = fields or []


@dataclasses.dataclass(frozen=True)
class IndexEntry:
    """索引里的一条插件记录。"""

    id: str
    name: str
    version: str
    description: str
    author: str
    permissions: tuple[str, ...]
    path: str
    download_url: str | None = None
    category: str = DEFAULT_CATEGORY
    icon: str = ""
    min_core_version: str = "0.0.0"
    hooks: tuple[str, ...] = ()
    # 以下只在索引愿意声明时出现；缺省为空，详情页对未装插件只展示已有信息
    repository: str = ""
    triggers: tuple[str, ...] = ()
    config_schema: tuple[dict[str, Any], ...] = ()

    @property
    def has_download(self) -> bool:
        return bool(self.download_url)


# ----------------------------------------------------------------------
# 索引解析
# ----------------------------------------------------------------------


def parse_index(data: Any) -> list[IndexEntry]:
    """解析索引 JSON；条目字段不全或权限越界的单条跳过（记日志）。

    顶层既接受 ``{"plugins": [...]}``，也接受裸数组。
    """
    if isinstance(data, dict):
        raw_items = data.get("plugins")
    else:
        raw_items = data
    if not isinstance(raw_items, list):
        raise MarketError("索引格式不对：顶层应为 {plugins: [...]} 或数组", status=502)

    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for raw in raw_items:
        try:
            entry = _parse_entry(raw)
        except ValueError as exc:
            logger.warning("跳过不合规的索引条目：%s", exc)
            continue
        if entry.id in seen:
            logger.warning("索引里有重复的插件 id：%s（保留第一条）", entry.id)
            continue
        seen.add(entry.id)
        entries.append(entry)
    return entries


def _parse_entry(raw: Any) -> IndexEntry:
    if not isinstance(raw, dict):
        raise ValueError("条目应为对象")
    missing = [key for key in INDEX_REQUIRED if not str(raw.get(key) or "").strip()]
    if missing:
        raise ValueError(f"缺少字段 {'、'.join(missing)}")
    plugin_id = str(raw["id"]).strip()
    if not plugin_id.replace("-", "").replace("_", "").isalnum() or plugin_id.lower() != plugin_id:
        raise ValueError(f"id 不合法：{plugin_id}")

    raw_permissions = raw.get("permissions")
    if not isinstance(raw_permissions, list):
        raise ValueError(f"{plugin_id} 的 permissions 应为数组")
    permissions: list[str] = []
    for item in raw_permissions:
        name = str(item).strip()
        if name not in PERMISSIONS:
            raise ValueError(f"{plugin_id} 的权限不在枚举内：{name}")
        if name not in permissions:
            permissions.append(name)

    category = str(raw.get("category") or "").strip()
    if category not in CATEGORIES:
        category = DEFAULT_CATEGORY
    hooks = raw.get("hooks")
    hooks = tuple(str(item).strip() for item in hooks if str(item).strip()) if isinstance(hooks, list) else ()
    triggers = raw.get("triggers")
    triggers = (
        tuple(str(item).strip() for item in triggers if str(item).strip())
        if isinstance(triggers, list)
        else ()
    )
    schema = raw.get("config_schema")
    config_schema = (
        tuple(dict(item) for item in schema if isinstance(item, dict))
        if isinstance(schema, list)
        else ()
    )
    download_url = str(raw.get("download_url") or "").strip() or None
    return IndexEntry(
        id=plugin_id,
        name=str(raw["name"]).strip(),
        version=str(raw["version"]).strip(),
        description=str(raw["description"]).strip(),
        author=str(raw["author"]).strip(),
        permissions=tuple(permissions),
        path=str(raw["path"]).strip(),
        download_url=download_url,
        category=category,
        icon=str(raw.get("icon") or "").strip()[:4],
        min_core_version=str(raw.get("min_core_version") or "0.0.0").strip(),
        hooks=hooks,
        repository=str(raw.get("repository") or "").strip(),
        triggers=triggers,
        config_schema=config_schema,
    )


def builtin_index(official_dir: str | Path | None = None) -> list[IndexEntry]:
    """内置官方索引：读取仓库 ``plugins/`` 下各插件的 manifest。"""
    root = Path(official_dir or OFFICIAL_PLUGINS_DIR)
    if not root.is_dir():
        return []
    entries: list[IndexEntry] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        try:
            manifest = load_manifest(entry)
        except ManifestError as exc:
            logger.debug("内置索引跳过 %s：%s", entry.name, exc.reason)
            continue
        entries.append(
            IndexEntry(
                id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                description=manifest.description,
                author=manifest.author,
                permissions=manifest.permissions,
                path=entry.name,
                category=manifest.category,
                icon=manifest.icon,
                min_core_version=manifest.min_core_version,
                hooks=manifest.hooks,
                repository=manifest.repository,
                triggers=manifest.triggers,
                config_schema=tuple(dict(item) for item in manifest.config_schema),
            )
        )
    return entries


# ----------------------------------------------------------------------
# 网络与下载（可注入，测试不碰真实网络）
# ----------------------------------------------------------------------


def _http_get(url: str, *, timeout: float) -> bytes:
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


Fetcher = Callable[..., bytes]


def _fetch_json(url: str, fetcher: Fetcher | None = None) -> Any:
    """拉取索引 JSON；网络错误/超时统一转成 :class:`MarketError`。"""
    reader = fetcher or _http_get
    try:
        payload = reader(url, timeout=INDEX_TIMEOUT_SECONDS)
    except MarketError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络、超时、HTTP 错误都算拉取失败
        raise MarketError(_describe_fetch_error(exc), status=502) from exc
    try:
        return json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else str(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketError(f"索引不是合法 JSON：{exc}", status=502) from exc


def _describe_fetch_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    if "Timeout" in name:
        return f"拉取索引超时（{INDEX_TIMEOUT_SECONDS:g} 秒）"
    text = str(exc).strip()
    return f"拉取索引失败：{text}" if text else f"拉取索引失败：{name}"


def read_source(url: str, fetcher: Fetcher | None = None) -> bytes:
    """读取下载源：http(s)、``file://`` 或本地路径（测试与内网镜像用）。"""
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        reader = fetcher or _http_get
        try:
            return reader(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise MarketError(f"下载插件包失败：{exc}", status=502) from exc
    if parsed.scheme == "file":
        path = Path(parsed.path)
    elif parsed.scheme == "":
        path = Path(url)
    else:
        raise MarketError(f"不支持的下载地址：{url}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MarketError(f"读取插件包失败：{exc}", status=502) from exc


# ----------------------------------------------------------------------
# 安全解压
# ----------------------------------------------------------------------


def _safe_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """列出可解压的成员并剥离单层顶层目录；有危险条目整包拒绝。"""
    infos = [info for info in zf.infolist() if info.filename and not info.is_dir()]
    if not infos:
        raise MarketError("插件包是空的")
    names = [info.filename for info in infos]
    prefix = _common_top_dir(names)
    members: list[tuple[zipfile.ZipInfo, str]] = []
    for info in infos:
        name = info.filename
        if prefix:
            name = name[len(prefix) + 1 :]
        if not name or name.endswith("/"):
            continue
        _check_member_safe(name)
        if _is_symlink(info):
            raise MarketError(f"插件包含符号链接，已拒绝：{info.filename}")
        members.append((info, name))
    if not members:
        raise MarketError("插件包是空的")
    return members


def _common_top_dir(names: list[str]) -> str | None:
    """所有文件都在同一个顶层目录下时返回该目录名（GitHub zip 常见结构）。"""
    firsts = {name.split("/", 1)[0] for name in names}
    if len(firsts) != 1:
        return None
    top = next(iter(firsts))
    # 顶层是单个文件（不是目录）时不剥离
    if all(name == top for name in names):
        return None
    return top


def _check_member_safe(name: str) -> None:
    """条目相对路径必须落在目标目录内：拒绝绝对路径与 ``..``。"""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise MarketError(f"插件包含绝对路径，已拒绝：{name}")
    parts = Path(normalized).parts
    if any(part == ".." for part in parts):
        raise MarketError(f"插件包含越界路径，已拒绝：{name}")


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _extract_zip(data: bytes, target: Path) -> None:
    """把 zip 解压到 ``target``；先整体校验再写盘，不合规一个字节都不留。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise MarketError(f"插件包不是合法的 zip：{exc}") from exc
    with archive:
        members = _safe_members(archive)
        root = target.resolve()
        for info, name in members:
            destination = (root / name).resolve()
            if destination != root and root not in destination.parents:
                raise MarketError(f"插件包含越界路径，已拒绝：{info.filename}")
        for info, name in members:
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)


# ----------------------------------------------------------------------
# 市场服务
# ----------------------------------------------------------------------


class MarketService:
    """一个 WebUI 进程内共享的市场服务。"""

    def __init__(
        self,
        state,
        *,
        fetcher: Fetcher | None = None,
        official_dir: str | Path | None = None,
    ):
        self.state = state
        self._fetcher = fetcher
        # 内置官方索引的来源目录：显式参数 > 测试/嵌入场景的 extras 覆盖 > 仓库 plugins/
        override = state.extras.get("market_official_dir")
        self.official_dir = Path(official_dir or override or OFFICIAL_PLUGINS_DIR)
        self._lock = threading.Lock()
        # 最近一次索引结果，只用于诊断；页面刷新时仍会重新拉
        self.last_index_error: str | None = None

    @property
    def fetcher(self) -> Fetcher | None:
        """下载源：构造时注入的优先，其次读 ``extras["market_fetcher"]``。

        按请求现读，测试/嵌入场景可以随时换掉，不用重建服务。
        """
        return self._fetcher or self.state.extras.get("market_fetcher")

    # ------------------------------------------------------------------
    # 数据来源
    # ------------------------------------------------------------------

    def index(self) -> tuple[list[IndexEntry], dict[str, Any]]:
        """返回 ``(索引条目, 来源说明)``；失败时回退内置索引并带上原因。"""
        url = str(getattr(self.state.config.market, "index_url", "") or "").strip()
        if not url:
            self.last_index_error = None
            return builtin_index(self.official_dir), {
                "source": "builtin",
                "label": "内置官方索引",
                "url": None,
                "available": True,
                "error": None,
                "note": "未配置远程索引地址，展示随框架发布的官方插件",
            }
        try:
            entries = parse_index(_fetch_json(url, self.fetcher))
        except MarketError as exc:
            self.last_index_error = exc.message
            logger.warning("插件索引不可用，回退内置官方索引：%s", exc.message)
            return builtin_index(self.official_dir), {
                "source": "builtin",
                "label": "内置官方索引",
                "url": url,
                "available": False,
                "error": exc.message,
                "note": "远程索引拉取失败，已回退到内置官方索引",
            }
        self.last_index_error = None
        return entries, {
            "source": "remote",
            "label": "远程索引",
            "url": url,
            "available": True,
            "error": None,
            "note": "",
        }

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """市场页需要的全部数据：来源、合并后的卡片列表、分类与权限说明。"""
        entries, source = self.index()
        by_id = {entry.id: entry for entry in entries}
        local = self._local_plugins()
        local_ids = {item["plugin_id"] for item in local}

        items: list[dict[str, Any]] = []
        for item in local:
            items.append(self._local_item(item, by_id.get(item["plugin_id"])))
        for entry in entries:
            if entry.id not in local_ids:
                items.append(self._index_item(entry))
        items.sort(key=_sort_key)
        return {
            "index": source,
            "items": items,
            "categories": list(CATEGORIES),
            "permissions": [
                {"id": name, **meta} for name, meta in PERMISSION_INFO.items()
            ],
            "core_version": CORE_VERSION,
        }

    def _local_plugins(self) -> list[dict[str, Any]]:
        """本地已装插件：id 优先用 manifest，缺失时退回目录名。"""
        rows = []
        for item in self.state.plugin_list():
            manifest = item.get("manifest")
            manifest = manifest if isinstance(manifest, dict) else {}
            plugin_id = str(manifest.get("id") or item["name"])
            rows.append({**item, "plugin_id": plugin_id, "manifest": manifest})
        return rows

    def _local_item(self, item: dict[str, Any], entry: IndexEntry | None) -> dict[str, Any]:
        manifest = item.get("manifest") or {}
        plugin_id = item["plugin_id"]
        valid = bool(item.get("valid", manifest != {}))
        core_too_old = bool(item.get("core_too_old"))
        version = str(manifest.get("version") or "")
        enabled = bool(item.get("enabled"))
        state_name = "enabled" if enabled else "installed"
        # manifest 不合规（比如缺文件）时，展示信息退回索引条目，卡片才不至于全空
        fallback = entry
        permissions = manifest.get("permissions")
        if not valid:
            permissions = entry.permissions if fallback else []
        updatable = bool(
            entry is not None
            and entry.has_download
            and (not valid or not version or version_less(version, entry.version))
        )
        return {
            "id": plugin_id,
            "plugin_name": item["name"],
            "name": manifest.get("name") or (entry.name if entry else item["name"]),
            "version": version or (entry.version if entry else None),
            "description": manifest.get("description")
            or (entry.description if entry else ""),
            "author": manifest.get("author") or (entry.author if entry else ""),
            "permissions": list(permissions),
            "permission_meta": _permission_meta(permissions),
            # 已授权了哪几项（详情页只读展示逐项对照）
            "granted_permissions": list(
                (item.get("permissions") or {}).get("granted") or []
            ),
            "hooks": list(manifest.get("hooks") or (entry.hooks if entry else [])),
            "category": manifest.get("category")
            or (entry.category if entry else DEFAULT_CATEGORY),
            "icon": manifest.get("icon") or (entry.icon if entry else ""),
            # 详情页展示用的可选元信息：本地 manifest 优先，缺失时用索引声明
            "repository": manifest.get("repository") or (entry.repository if entry else ""),
            "triggers": list(
                manifest.get("triggers") or (entry.triggers if entry else [])
            ),
            "installed": True,
            "enabled": enabled,
            "loaded": bool(item.get("loaded")),
            "state": state_name,
            "updatable": updatable,
            "latest_version": entry.version if entry else None,
            "invalid": not valid,
            "invalid_reason": item.get("invalid_reason"),
            "core_too_old": core_too_old,
            "download_available": bool(entry and entry.has_download),
            "has_index": entry is not None,
            "has_config": bool(manifest.get("config_schema")) if valid else False,
            "local_path": item.get("path"),
        }

    def _index_item(self, entry: IndexEntry) -> dict[str, Any]:
        core_too_old = version_less(CORE_VERSION, entry.min_core_version)
        return {
            "id": entry.id,
            "plugin_name": entry.id,
            "name": entry.name,
            "version": entry.version,
            "description": entry.description,
            "author": entry.author,
            "permissions": list(entry.permissions),
            "permission_meta": _permission_meta(entry.permissions),
            "hooks": list(entry.hooks),
            "category": entry.category,
            "icon": entry.icon,
            "repository": entry.repository,
            "triggers": list(entry.triggers),
            # 索引愿意声明 schema 时，未安装插件的详情页也能展示「有哪些配置项」
            "config_schema": [dict(item) for item in entry.config_schema],
            "installed": False,
            "enabled": False,
            "loaded": False,
            "state": "not_installed",
            "updatable": False,
            "latest_version": entry.version,
            "invalid": False,
            "invalid_reason": None,
            "core_too_old": core_too_old,
            "download_available": entry.has_download,
            "has_index": True,
            "has_config": False,
            "local_path": None,
        }

    def get_plugin_config(self, plugin_id: str) -> dict[str, Any]:
        """读取已安装插件的 schema 与配置值；不回显密钥字段的原值。

        返回的 ``schema`` 是规范化后的视图（类型已归一、范围 / 可选值原样
        保留），WebUI 直接按它生成表单；``secrets`` 标出哪些密钥字段已经
        有值（前端用「已设置，留空保留」占位，不回显内容）。
        """
        local = self._local_plugin(plugin_id)
        if local is None or not local.get("valid"):
            raise MarketError(f"插件不存在：{plugin_id}", status=404)
        manifest = local.get("manifest") or {}
        fields = normalize_schema(manifest.get("config_schema") or [])
        if not fields:
            raise MarketError(f"插件 {plugin_id} 没有可配置项", status=409)
        directory = self._safe_local_dir(local)
        raw = self._read_plugin_yaml(directory)
        values: dict[str, Any] = {}
        secrets: dict[str, bool] = {}
        for field in fields:
            if field.key in raw:
                # 配置值可能被手改成错误类型：回读时按 schema 归一，坏的退回默认值
                try:
                    value = coerce_value(field, raw[field.key])
                except ConfigValueError:
                    value = field.default
            else:
                value = field.default
            if field.secret:
                secrets[field.key] = bool(value)
                value = ""
            values[field.key] = value
        return {
            "schema": [field.as_dict() for field in fields],
            "values": values,
            "secrets": secrets,
        }

    def save_plugin_config(self, plugin_id: str, values: Any) -> dict[str, Any]:
        """按 manifest schema 校验并原子写入插件目录 config.yaml。

        返回值在 :meth:`get_plugin_config` 的基础上多带 ``hot`` / ``message``：
        运行中的插件管理器会拿写入前后的配置快照调插件声明的
        ``on_config_change``，因此「是否热生效」由插件自己决定，WebUI 只如实
        转述——没声明钩子的插件明确提示需要重启，不假装热更新。

        校验失败时抛 422，``fields`` 逐项列出哪个字段、为什么（前端据此在
        对应控件旁提示），不写盘。
        """
        local = self._local_plugin(plugin_id)
        if local is None or not local.get("valid"):
            raise MarketError(f"插件不存在：{plugin_id}", status=404)
        manifest = local.get("manifest") or {}
        fields = normalize_schema(manifest.get("config_schema") or [])
        if not fields:
            raise MarketError(f"插件 {plugin_id} 没有可配置项", status=409)
        if not isinstance(values, dict):
            raise MarketError("values 必须是对象", status=422)
        allowed = {field.key for field in fields}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise MarketError(f"包含未声明的配置项：{', '.join(unknown)}", status=422)
        directory = self._safe_local_dir(local)
        existing = self._read_plugin_yaml(directory)
        parsed, errors = validate_values(fields, values)
        if errors:
            raise MarketError(
                "插件配置校验失败：" + "；".join(item["field"] + " " + item["message"] for item in errors),
                status=422,
                fields=errors,
            )
        updated = dict(existing)
        updated.update(parsed)
        # 先算出「对插件而言真正生效的配置」的新旧值（schema 里声明到的键，
        # 缺省用默认值），写盘后再交给插件决定能否热生效。
        before = effective_values(fields, existing)
        after = effective_values(fields, updated)
        self._write_plugin_yaml(directory, updated)
        unchanged = before == after
        if unchanged:
            # 值没变：没有要热应用的东西，也不该借「已生效」的提示掩盖这一点
            hot, message = True, "插件配置没有变化，无需重启。"
        else:
            hot, message = self._apply_config_change(local["name"], before, after)
        logger.info(
            "插件 %s 配置已保存（%s）",
            local["name"],
            "无需重启" if unchanged else ("已热生效" if hot else "需重启后生效"),
        )
        payload = self.get_plugin_config(plugin_id)
        return {**payload, "hot": hot, "message": message}

    def _apply_config_change(
        self,
        name: str,
        old: dict[str, Any],
        new: dict[str, Any],
    ) -> tuple[bool, str]:
        """把配置变更交给插件热应用；没有运行中的管理器时如实返回需要重启。"""
        manager = getattr(self.state, "plugins", None)
        if manager is None:
            # 独立 WebUI：配置已写盘，但没有运行中的插件实例能接收变更
            return False, "插件配置已保存；当前 WebUI 没有运行中的 bot，重启后生效。"
        try:
            hot, reason = manager.apply_config_change(name, old, new)
        except Exception as exc:  # noqa: BLE001 - 热应用失败不能吞掉已保存的配置
            logger.warning("插件 %s 配置热应用失败：%s", name, exc)
            return False, f"插件配置已保存，但热应用失败（{exc}），请重启 bot 后确认。"
        if hot:
            return True, "插件配置已保存并热生效，无需重启。"
        return False, f"插件配置已保存，{reason}"

    def _safe_local_dir(self, local: dict[str, Any]) -> Path:
        root = self._plugins_root().resolve()
        directory = Path(str(local.get("path") or "")).resolve()
        if not _is_within(directory, root, allow_equal=False) or not (directory / "manifest.json").is_file():
            raise MarketError("插件目录不安全，拒绝访问", status=400)
        return directory

    @staticmethod
    def _read_plugin_yaml(directory: Path) -> dict[str, Any]:
        import yaml
        path = directory / "config.yaml"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise MarketError("插件配置文件是符号链接，拒绝访问", status=400)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise MarketError(f"插件 config.yaml 无法读取：{exc}", status=422) from exc
        if not isinstance(data, dict):
            raise MarketError("插件 config.yaml 顶层必须是对象", status=422)
        return data

    @staticmethod
    def _write_plugin_yaml(directory: Path, data: dict[str, Any]) -> None:
        import os
        import yaml
        path = directory / "config.yaml"
        if path.is_symlink():
            raise MarketError("插件配置文件是符号链接，拒绝写入", status=400)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(temp, path)

    def find_entry(self, plugin_id: str) -> IndexEntry:
        entries, _source = self.index()
        for entry in entries:
            if entry.id == plugin_id:
                return entry
        raise MarketError(f"索引里没有插件：{plugin_id}", status=404)

    # ------------------------------------------------------------------
    # 安装 / 更新 / 卸载
    # ------------------------------------------------------------------

    def install(self, plugin_id: str) -> dict[str, Any]:
        """下载并安装到 ``plugins/<id>``；安装后不启用。"""
        entry = self.find_entry(plugin_id)
        if not entry.has_download:
            raise MarketError("该插件只提供展示，没有下载地址，无法安装")
        if version_less(CORE_VERSION, entry.min_core_version):
            raise MarketError(
                f"插件需要框架版本 >= {entry.min_core_version}（当前 {CORE_VERSION}）"
            )
        root = self._plugins_root()
        target = self._target_dir(root, plugin_id)
        with self._lock:
            if target.exists() or self._local_plugin(plugin_id) is not None:
                raise MarketError(f"插件已安装：{plugin_id}", status=409)
            data = read_source(entry.download_url, self.fetcher)
            installed, manifest = self._install(root, plugin_id, data, entry=entry)
            # 安装不代表信任，只登记空记录。
            self._register_new_plugin(installed.name)
            logger.info("市场安装插件 %s %s 到 %s", plugin_id, manifest.version, installed)
            self._refresh()
        return {
            "id": plugin_id,
            "path": str(installed),
            "version": manifest.version,
            "message": (
                f"插件 {entry.name} 已安装，尚未启用；显式启用即授予当前声明的权限。"
            ),
        }

    def update(self, plugin_id: str) -> dict[str, Any]:
        """用索引里的新版本替换代码，保留插件目录里的 config.yaml 与启用状态。"""
        entry = self.find_entry(plugin_id)
        if not entry.has_download:
            raise MarketError("该插件没有下载地址，无法更新")
        local = self._local_plugin(plugin_id)
        if local is None:
            raise MarketError(f"插件未安装：{plugin_id}", status=404)
        version = str(local.get("manifest", {}).get("version") or "")
        # manifest 不合规（缺文件/字段坏掉）时也算「需要更新」：用索引里的版本修好它
        if self._local_valid(local) and (
            not version or not version_less(version, entry.version)
        ):
            raise MarketError("已是最新版本，无需更新")

        root = self._plugins_root()
        target = Path(local["path"])
        was_enabled = bool(local.get("enabled"))
        root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = read_source(entry.download_url, self.fetcher)
            if was_enabled:
                # 先停用：旧目录要整体换掉，后台任务不能还跑着旧代码
                self._set_enabled(local["name"], False)
            try:
                installed, manifest = self._install(
                    root,
                    plugin_id,
                    data,
                    entry=entry,
                    old_dir=target,
                    preserve=PRESERVED_FILES,
                )
            except Exception:
                if was_enabled:
                    # 换包失败：目录已还原，把启用状态恢复回去
                    self._set_enabled(local["name"], True)
                raise
            # 换过代码后旧插件对象里的钩子还是旧模块的：从管理器里摘掉，
            # 让 discover 重新建对象，重新启用时才会 import 新代码
            manager = getattr(self.state, "plugins", None)
            if manager is not None:
                manager.plugins.pop(local["name"], None)
            # 更新保留已有授权；新版本新增声明的权限因为不在记录里，
            # 自然保持未授权。已有记录不会被覆盖。
            self._register_new_plugin(local["name"])
            self._refresh()
            enabled_now = False
            if was_enabled:
                enabled_now = self._set_enabled(local["name"], True)
                if not enabled_now:
                    logger.warning("更新后重新启用插件 %s 失败", local["name"])
            self._persist_enabled()
            logger.info("市场更新插件 %s → %s", plugin_id, manifest.version)
        return {
            "id": plugin_id,
            "path": str(installed),
            "version": manifest.version,
            "enabled": enabled_now,
            "message": (
                f"插件 {entry.name} 已更新到 {manifest.version}，"
                + ("并保持启用。" if enabled_now else "当前为已安装未启用。")
            ),
        }

    def uninstall(self, plugin_id: str) -> dict[str, Any]:
        """卸载插件：启用中先停用，再删目录；不动 config.yaml 的其它段。"""
        local = self._local_plugin(plugin_id)
        if local is None:
            raise MarketError(f"插件未安装：{plugin_id}", status=404)
        path = Path(local["path"])
        root = self._plugins_root()
        if not _is_within(path, root):
            raise MarketError("插件目录不在 plugins/ 下，拒绝删除")
        with self._lock:
            if local.get("enabled"):
                self._set_enabled(local["name"], False)
                self._persist_enabled()
            manager = getattr(self.state, "plugins", None)
            if manager is not None:
                # 删目录前给插件 on_unload：清定时任务、后台线程与临时资源
                try:
                    manager.unload_plugin(local["name"])
                except Exception as exc:  # noqa: BLE001 - 清理失败不挡卸载
                    logger.warning("卸载前清理插件 %s 失败：%s", local["name"], exc)
            shutil.rmtree(path)
            if manager is not None:
                manager.plugins.pop(local["name"], None)
            self._forget_plugin(local["name"])
            logger.info("市场卸载插件 %s（删除 %s）", plugin_id, path)
            self._refresh()
        return {
            "id": plugin_id,
            "message": (
                f"插件 {local.get('manifest', {}).get('name') or local['name']} 已卸载。"
                "插件目录里的文件（含自带的 config.yaml）随目录删除；"
                "框架 config.yaml 里该插件的配置段保留不动，只从 plugins.enabled 摘除。"
            ),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _plugins_root(self) -> Path:
        return Path(self.state.config.plugins.dir)

    def _target_dir(self, root: Path, plugin_id: str) -> Path:
        target = (root / plugin_id).resolve()
        if not _is_within(target, root.resolve(), allow_equal=False):
            raise MarketError(f"插件 id 不合法：{plugin_id}")
        return target

    def _local_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        for item in self._local_plugins():
            if item["plugin_id"] == plugin_id:
                return item
        return None

    def _local_valid(self, local: dict[str, Any]) -> bool:
        """本地插件是否拿到了一份合规 manifest（没有就当成需要更新/修好）。"""
        return "valid" not in local or bool(local.get("valid"))

    def _install(
        self,
        root: Path,
        plugin_id: str,
        data: bytes,
        *,
        entry: IndexEntry,
        old_dir: Path | None = None,
        preserve: tuple[str, ...] = (),
    ) -> tuple[Path, Manifest]:
        """安装 / 更新共用的落盘流程，返回 ``(插件目录, 校验后的 manifest)``。

        1. 解压到 ``plugins/`` 下的临时目录并校验（路径穿越、manifest、
           plugin.py）；不合规时删掉临时目录，目标目录一个字节都不动。
        2. 需要替换时先保留 ``preserve`` 里的插件本地文件，再把旧目录改名
           备份；新目录就位后才删备份，换包中途失败会还原旧目录。
        """
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".market-{plugin_id}-", dir=root))
        if old_dir is not None:
            # 更新：就地在原目录换代码，目录名（= 插件在 plugins.enabled 里的名字）不变
            destination = Path(old_dir)
            if not _is_within(destination, root, allow_equal=False):
                raise MarketError("插件目录不在 plugins/ 下，拒绝更新")
        else:
            destination = self._target_dir(root, plugin_id)
        backup: Path | None = None
        placed = False
        try:
            _extract_zip(data, staging)
            manifest = self._validate_package(staging, entry, plugin_id)
            if old_dir is not None and Path(old_dir).exists():
                _preserve_files(Path(old_dir), staging, preserve)
            if destination.exists():
                backup = root / f".market-old-{plugin_id}-{uuid.uuid4().hex[:8]}"
                destination.rename(backup)
            staging.rename(destination)
            placed = True
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
            return destination, manifest
        except Exception:
            # 只清理本次新放上去的目录；旧目录原样还原
            if placed and destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if backup is not None and backup.exists():
                backup.rename(destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _validate_package(self, directory: Path, entry: IndexEntry, plugin_id: str) -> Manifest:
        try:
            manifest = load_manifest(directory)
        except ManifestError as exc:
            raise MarketError(f"插件 manifest 不合规：{exc.reason}") from exc
        if manifest.id != plugin_id:
            raise MarketError(
                f"manifest 的 id（{manifest.id}）与索引（{plugin_id}）不一致，已拒绝安装"
            )
        if version_less(CORE_VERSION, manifest.min_core_version):
            raise MarketError(
                f"插件需要框架版本 >= {manifest.min_core_version}（当前 {CORE_VERSION}）"
            )
        if not (directory / "plugin.py").is_file():
            raise MarketError("插件包缺少 plugin.py，已拒绝安装")
        return manifest

    def _register_new_plugin(self, name: str) -> None:
        manager = self.state.plugins or self.state.offline_plugin_manager()
        manager.register_new_plugin(name)

    def _forget_plugin(self, name: str) -> None:
        manager = self.state.plugins or self.state.offline_plugin_manager()
        manager.forget_plugin(name)

    def _refresh(self) -> None:
        manager = getattr(self.state, "plugins", None)
        if manager is None:
            return
        try:
            manager.discover()
        except OSError as exc:  # noqa: BLE001 - 扫描失败不影响本次操作结果
            logger.warning("安装后重新扫描插件目录失败：%s", exc)

    def _set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/停用插件：有管理器走热生效，独立 WebUI 模式只改内存配置。"""
        if self.state.plugins is not None:
            return self.state.set_plugin_enabled(name, enabled, trust=False)
        names = [item for item in self.state.config.plugins.enabled if item != name]
        if enabled:
            names.append(name)
        self.state.config.plugins.enabled = names
        return True

    def _persist_enabled(self) -> None:
        """把启用列表写回 config.yaml；失败只提示，不回滚目录操作。"""
        from .state import write_plugin_enabled

        try:
            write_plugin_enabled(self.state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("插件启用列表写回 config.yaml 失败：%s", exc)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _permission_meta(permissions: Any) -> list[dict[str, Any]]:
    result = []
    for name in permissions or []:
        meta = PERMISSION_INFO.get(str(name))
        if meta is None:
            continue
        result.append({"id": str(name), **meta})
    return result


def _is_within(path: Path, root: Path, *, allow_equal: bool = True) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    if resolved == base:
        return allow_equal
    return base in resolved.parents


def _preserve_files(old: Path, new: Path, names: tuple[str, ...]) -> None:
    for name in names:
        source = old / name
        target = new / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def _sort_key(item: dict[str, Any]):
    rank = {"enabled": 0, "installed": 1, "not_installed": 2}.get(item["state"], 3)
    return (rank, item.get("category") or DEFAULT_CATEGORY, item.get("name") or item["id"])


__all__ = [
    "MarketError",
    "MarketService",
    "OFFICIAL_PLUGINS_DIR",
    "builtin_index",
    "parse_index",
    "read_source",
]
