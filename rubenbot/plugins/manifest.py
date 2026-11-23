"""插件 manifest：规范、解析与校验。

每个插件目录根部放一份 ``manifest.json``，声明插件身份、版本、权限与钩子。
loader 加载前校验；市场页也用它区分「已安装 / 可更新 / 不合规 / 核心版本过低」。

必填字段：``id`` / ``name`` / ``version`` / ``description`` / ``author`` /
``min_core_version`` / ``permissions``；``config_schema`` 与 ``hooks`` 可省略
（省略时按空列表处理），``category`` / ``icon`` / ``repository`` / ``triggers``
为可选展示字段。``triggers`` 是给 WebUI 详情页看的人类可读触发方式说明，
不是运行时配置；真正的触发词仍由各插件 ``config_schema`` 里的字段承担。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__ as CORE_VERSION

logger = logging.getLogger(__name__)

# 权限枚举固定为这 7 项（见 SPEC「插件市场设计」）
PERMISSION_INFO: dict[str, dict[str, Any]] = {
    "read_messages": {
        "label": "读消息",
        "description": "读取聊天消息、历史与记忆",
        "high_risk": False,
    },
    "send_messages": {
        "label": "发消息",
        "description": "以 bot 的身份发送消息",
        "high_risk": False,
    },
    "capabilities": {
        "label": "平台能力",
        "description": "调用平台能力层（如贴表情）",
        "high_risk": False,
    },
    "qzone_write": {
        "label": "空间写入",
        "description": "在 QQ 空间发布说说、点赞、评论（对外写操作）",
        "high_risk": True,
    },
    "scheduler": {
        "label": "定时任务",
        "description": "注册并运行后台定时任务",
        "high_risk": True,
    },
    "llm": {
        "label": "调用模型",
        "description": "调用核心的大模型接口（会产生费用）",
        "high_risk": False,
    },
    "network": {
        "label": "出网请求",
        "description": "向外部网络发起请求",
        "high_risk": False,
    },
}
PERMISSIONS: tuple[str, ...] = tuple(PERMISSION_INFO)

# 市场分类标签；manifest 的 category 不在这四项内时归「工具」
CATEGORIES: tuple[str, ...] = ("互动", "空间", "工具", "系统")
DEFAULT_CATEGORY = "工具"

REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "version",
    "description",
    "author",
    "min_core_version",
    "permissions",
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_PATTERN = re.compile(r"^\d+(\.\d+)*([-+][0-9A-Za-z.-]+)?$")


class ManifestError(ValueError):
    """manifest 缺失或不合规；``core_too_old`` 表示原因是框架版本过低。"""

    def __init__(self, reason: str, *, core_too_old: bool = False, missing: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.core_too_old = core_too_old
        self.missing = missing


@dataclass(frozen=True)
class Manifest:
    """一份校验通过的 manifest。"""

    id: str
    name: str
    version: str
    description: str
    author: str
    min_core_version: str
    permissions: tuple[str, ...] = ()
    config_schema: tuple[dict[str, Any], ...] = ()
    hooks: tuple[str, ...] = ()
    category: str = DEFAULT_CATEGORY
    icon: str = ""
    repository: str = ""
    triggers: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """给 WebUI/市场用的 JSON 友好视图。"""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "min_core_version": self.min_core_version,
            "permissions": list(self.permissions),
            "config_schema": [dict(item) for item in self.config_schema],
            "hooks": list(self.hooks),
            "category": self.category,
            "icon": self.icon,
            "repository": self.repository,
            "triggers": list(self.triggers),
        }

    @property
    def permission_meta(self) -> list[dict[str, Any]]:
        return [
            {"id": name, **PERMISSION_INFO.get(name, {"label": name, "description": "", "high_risk": False})}
            for name in self.permissions
        ]


def version_key(value: str) -> tuple[int, ...]:
    """把版本号转成可比较的数字元组；后半段无法解析的部分忽略。"""
    numbers: list[int] = []
    for part in re.split(r"[.+-]", str(value or "").strip()):
        if part.isdigit():
            numbers.append(int(part))
        else:
            break
    return tuple(numbers)


def version_less(left: str, right: str) -> bool:
    """``left`` 是否低于 ``right``（按数字段比较，例如 1.0.0 < 1.0.1）。"""
    return version_key(left) < version_key(right)


def _clean_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        raise ManifestError(f"manifest.json 缺少字段：{key}")
    text = str(value).strip()
    if not text:
        raise ManifestError(f"manifest.json 的 {key} 不能为空")
    return text


def _clean_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"manifest.json 的 {key} 应为数组")
    return value


def parse_manifest(data: Any, *, core_version: str = CORE_VERSION) -> Manifest:
    """校验并构造 :class:`Manifest`；不合规时抛 :class:`ManifestError`。"""
    if not isinstance(data, dict):
        raise ManifestError("manifest.json 顶层应为对象")

    missing = [key for key in REQUIRED_FIELDS if key not in data]
    if missing:
        raise ManifestError("manifest.json 缺少字段：" + "、".join(missing))

    plugin_id = _clean_text(data, "id")
    if not _ID_PATTERN.match(plugin_id):
        raise ManifestError(f"插件 id 不合法：{plugin_id}（只能用英文小写、数字、- 或 _）")

    name = _clean_text(data, "name")
    description = _clean_text(data, "description")
    author = _clean_text(data, "author")
    version = _clean_text(data, "version")
    if not _VERSION_PATTERN.match(version):
        raise ManifestError(f"version 不是合法的版本号：{version}（应形如 1.0.0）")
    min_core_version = _clean_text(data, "min_core_version")
    if not _VERSION_PATTERN.match(min_core_version):
        raise ManifestError(
            f"min_core_version 不是合法的版本号：{min_core_version}（应形如 0.1.0）"
        )

    raw_permissions = _clean_list(data, "permissions")
    permissions: list[str] = []
    for item in raw_permissions:
        permission = str(item).strip()
        if permission not in PERMISSION_INFO:
            raise ManifestError(
                f"权限不在枚举内：{permission}（可选：{' / '.join(PERMISSIONS)}）"
            )
        if permission not in permissions:
            permissions.append(permission)
    if not permissions:
        # 什么都不声明的插件几乎没有意义，当成写漏了处理
        raise ManifestError("permissions 不能为空数组（至少要声明一项权限）")

    hooks = [
        str(item).strip()
        for item in _clean_list(data, "hooks")
        if str(item or "").strip()
    ]

    config_schema: list[dict[str, Any]] = []
    for item in _clean_list(data, "config_schema"):
        if isinstance(item, dict):
            config_schema.append(dict(item))

    category = str(data.get("category") or "").strip()
    if category not in CATEGORIES:
        if category:
            logger.debug("插件 %s 的分类 %s 不在预设内，归入%s", plugin_id, category, DEFAULT_CATEGORY)
        category = DEFAULT_CATEGORY

    icon = str(data.get("icon") or "").strip()[:4]
    repository = str(data.get("repository") or "").strip()
    # 纯展示字段写坏了不该让插件变得不合规：不是数组就按「没声明」处理
    raw_triggers = data.get("triggers")
    if raw_triggers is not None and not isinstance(raw_triggers, list):
        logger.debug("插件 %s 的 triggers 不是数组，忽略", plugin_id)
        raw_triggers = None
    triggers = [
        str(item).strip()
        for item in (raw_triggers or [])
        if str(item or "").strip()
    ]

    if version_less(core_version, min_core_version):
        raise ManifestError(
            f"需要框架版本 >= {min_core_version}（当前 {core_version}）",
            core_too_old=True,
        )

    return Manifest(
        id=plugin_id,
        name=name,
        version=version,
        description=description,
        author=author,
        min_core_version=min_core_version,
        permissions=tuple(permissions),
        config_schema=tuple(config_schema),
        hooks=tuple(hooks),
        category=category,
        icon=icon,
        repository=repository,
        triggers=tuple(triggers),
        raw=dict(data),
    )


def load_manifest(plugin_dir: str | Path, *, core_version: str = CORE_VERSION) -> Manifest:
    """读取并校验 ``<plugin_dir>/manifest.json``，失败抛 :class:`ManifestError`。"""
    path = Path(plugin_dir) / "manifest.json"
    if not path.is_file():
        raise ManifestError("缺少 manifest.json", missing=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest.json 读取失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest.json 不是合法 JSON：{exc}") from exc
    return parse_manifest(data, core_version=core_version)


def read_manifest(
    plugin_dir: str | Path, *, core_version: str = CORE_VERSION
) -> tuple[Manifest | None, str | None, bool]:
    """宽松读取：返回 ``(manifest, 失败原因, 是否核心版本过低)``。

    manifest 不合规时 ``manifest`` 为 None、原因为中文说明；文件正常时原因为 None。
    """
    try:
        return load_manifest(plugin_dir, core_version=core_version), None, False
    except ManifestError as exc:
        return None, exc.reason, exc.core_too_old


__all__ = [
    "CATEGORIES",
    "CORE_VERSION",
    "DEFAULT_CATEGORY",
    "Manifest",
    "ManifestError",
    "PERMISSION_INFO",
    "PERMISSIONS",
    "REQUIRED_FIELDS",
    "load_manifest",
    "parse_manifest",
    "read_manifest",
    "version_key",
    "version_less",
]
