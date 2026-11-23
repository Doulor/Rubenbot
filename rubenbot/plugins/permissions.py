"""插件装后授权：每插件一份授权记录 + 执行期准入。

manifest 只回答「插件声明了什么」，这里回答「机主实际批了什么」。记录落在
数据库同目录的 ``plugin_permissions.json``（数据库是内存库时只驻内存），
重启不丢。权限枚举沿用 :mod:`rubenbot.plugins.manifest`，不另起一套格式。

**默认拒绝**：新发现的插件没有任何授权；manifest 升级新增的权限同样默认
未授权。唯一例外是首次升级到本授权层的迁移——授权文件从未存在时，当时已经
装好的插件按各自 manifest 全部视为已授权，保证线上正在用的插件不断线。
迁移只发生一次：文件写好后，之后放进插件目录或从市场安装的插件一律从空
授权开始。授权文件存在但读不出来（损坏 / 格式不对 / 无权限）**不会**再自动
全授：按默认拒绝处理并记 WARNING，请到 WebUI 重新逐项授权。

授权变更与执行期拦截都通过 ``rubenbot.plugins.permissions`` 这个 logger
留痕，可在 WebUI「日志」页查到。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .manifest import PERMISSION_INFO, PERMISSIONS

logger = logging.getLogger(__name__)

FILENAME = "plugin_permissions.json"
FILE_VERSION = 1

# qzone.* 能力属于空间读写，统一按 qzone_write 这一项授权；其余能力走 capabilities。
_QZONE_CAPABILITY_PREFIX = "qzone."

# 不在 manifest 枚举里的内部审计标记（插件不该直接写宿主存储，没有可授权的项）
_INTERNAL_LABELS = {"store_write": "写存储"}


def _label(permission: str) -> str:
    return PERMISSION_INFO.get(permission, {}).get("label") or _INTERNAL_LABELS.get(
        permission, permission
    )


class PluginPermissionError(PermissionError):
    """插件调用了未获授权的入口。"""

    def __init__(self, plugin: str, permission: str, action: str = ""):
        self.plugin = plugin
        self.permission = permission
        self.action = action
        label = _label(permission)
        detail = f"插件 {plugin} 未获授权：{label}（{permission}）"
        if action:
            detail += f"，已拦截 {action}"
        super().__init__(detail)


class PluginPermissionStore:
    """每插件授权记录；``path`` 为 None 时只在内存里生效。"""

    def __init__(self, path: str | Path | None = None, *, clock=time.time):
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._plugins: dict[str, set[str]] = {}
        self._migrated = False
        # 最近一次写盘失败的原因；读不到/写不了不阻断主流程，但要能排查
        self.last_error: str | None = None
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.last_error = str(exc)
            # 文件在但读不了：按默认拒绝处理，不再触发「首次升级全授」
            self._migrated = True
            logger.warning(
                "插件授权文件读取失败（%s）：%s；按未授权处理，请到 WebUI 重新授权", self.path, exc
            )
            return
        try:
            data = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            self.last_error = str(exc)
            self._migrated = True
            logger.warning(
                "插件授权文件不是合法 JSON（%s）：%s；按未授权处理，请到 WebUI 重新授权",
                self.path,
                exc,
            )
            return
        if not isinstance(data, dict):
            self.last_error = "顶层不是对象"
            self._migrated = True
            logger.warning(
                "插件授权文件格式不对（%s）：顶层应为对象；按未授权处理，请到 WebUI 重新授权",
                self.path,
            )
            return
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            for name, value in plugins.items():
                if not isinstance(value, list):
                    continue
                cleaned = {str(item) for item in value if str(item) in PERMISSION_INFO}
                self._plugins[str(name)] = cleaned
        self._migrated = bool(data.get("migrated"))

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": FILE_VERSION,
            "migrated": self._migrated,
            "updated_at": self._clock(),
            "plugins": {
                name: sorted(permissions)
                for name, permissions in sorted(self._plugins.items())
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            logger.warning("插件授权文件写入失败（%s）：%s", self.path, exc)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @property
    def migrated(self) -> bool:
        """是否已完成「首次升级视为全授」的迁移。"""
        with self._lock:
            return self._migrated

    def grants(self, name: str) -> frozenset[str]:
        with self._lock:
            return frozenset(self._plugins.get(name, ()))

    def allows(self, name: str, permission: str) -> bool:
        with self._lock:
            return permission in self._plugins.get(name, ())

    def snapshot(self, name: str, declared: Iterable[str]) -> list[dict[str, Any]]:
        """逐项对照：manifest 声明的每一项是否已授权。"""
        declared_set = {item for item in declared if item in PERMISSION_INFO}
        granted = self.grants(name)
        return [
            {
                "id": permission,
                **PERMISSION_INFO[permission],
                "declared": permission in declared_set,
                "granted": permission in granted,
            }
            for permission in PERMISSIONS
            if permission in declared_set
        ]

    def summary(self, name: str, declared: Iterable[str]) -> dict[str, Any]:
        declared_list = [item for item in declared if item in PERMISSION_INFO]
        granted = [item for item in declared_list if item in self.grants(name)]
        meta = self.snapshot(name, declared_list)
        return {
            "declared": declared_list,
            "granted": granted,
            "permission_meta": meta,
            "all_granted": len(granted) == len(declared_list),
        }

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def set_permissions(
        self,
        name: str,
        permissions: Iterable[str],
        *,
        declared: Iterable[str] | None = None,
    ) -> frozenset[str]:
        """整表替换某插件的授权；返回写入后的授权集合。

        权限值必须在固定枚举内；给了 ``declared`` 时还不能超出声明的范围。
        """
        cleaned: set[str] = set()
        for item in permissions:
            permission = str(item).strip()
            if permission not in PERMISSION_INFO:
                raise ValueError(f"权限不在枚举内：{permission}")
            cleaned.add(permission)
        declared_set = None if declared is None else {item for item in declared}
        if declared_set is not None:
            extra = sorted(cleaned - declared_set)
            if extra:
                raise ValueError(f"插件没有声明这些权限：{'、'.join(extra)}")

        with self._lock:
            old = set(self._plugins.get(name, ()))
            self._plugins[name] = set(cleaned)
            self._migrated = True
            self._save()
        for permission in sorted(cleaned - old):
            logger.info(
                "插件授权变更：授予 %s %s（%s）",
                name,
                PERMISSION_INFO[permission]["label"],
                permission,
            )
        for permission in sorted(old - cleaned):
            logger.info(
                "插件授权变更：撤回 %s %s（%s）",
                name,
                PERMISSION_INFO[permission]["label"],
                permission,
            )
        return frozenset(cleaned)

    def grant(self, name: str, permission: str, *, declared: Iterable[str] | None = None) -> frozenset[str]:
        return self.set_permissions(name, self.grants(name) | {permission}, declared=declared)

    def revoke(self, name: str, permission: str) -> frozenset[str]:
        return self.set_permissions(name, self.grants(name) - {permission})

    def ensure_registered(self, name: str, declared: Iterable[str]) -> None:
        """为新装插件登记一条空授权记录（不改动迁移标记）。

        新装 = 从零开始：即使授权层还没做过「首次升级」迁移，也不能把这枚
        刚装进来的插件当成「已装插件」自动全授。记录存在后再迁移会保留它
        （迁移不会覆盖已有记录）。
        """
        with self._lock:
            if name in self._plugins:
                return
            self._plugins[name] = set()
            self._save()

    def forget(self, name: str) -> None:
        """插件卸载后清掉授权记录：同名重装时从空授权开始。"""
        with self._lock:
            if self._plugins.pop(name, None) is None:
                return
            self._save()

    def migrate(self, installed: Mapping[str, Iterable[str]]) -> list[str]:
        """首次升级：把当时已安装插件的 manifest 权限全部视为已授权。

        只在授权层从未初始化过（``migrated`` 为假）时执行；返回本次授予授权的
        插件名。授权文件写不进去也会在内存里完成迁移，并记录告警。
        """
        with self._lock:
            if self._migrated:
                return []
            granted_names: list[str] = []
            for name, permissions in installed.items():
                cleaned = {item for item in permissions if item in PERMISSION_INFO}
                # 迁移时已经有的显式记录优先（含新装插件登记的空记录），不覆盖
                if name in self._plugins:
                    continue
                self._plugins[name] = cleaned
                if cleaned:
                    granted_names.append(name)
            self._migrated = True
            self._save()
        for name in sorted(granted_names):
            labels = "、".join(
                PERMISSION_INFO[item]["label"] for item in PERMISSIONS if item in self._plugins.get(name, ())
            )
            logger.info(
                "插件授权迁移：已装插件 %s 按当前 manifest 视为已授权（%s）", name, labels
            )
        if not granted_names:
            logger.info("插件授权迁移完成：当前没有已装插件需要授权")
        return sorted(granted_names)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def audit_denial(self, name: str, permission: str, action: str = "") -> None:
        """拦截一条未授权调用，留痕但不抛异常。"""
        detail = f"插件 {name} 调用未授权的能力：{_label(permission)}（{permission}）"
        if action:
            detail += f"；已拦截 {action}"
        logger.warning(detail)


def capability_permission(name: str) -> str:
    """能力名对应的授权项：空间能力按 qzone_write，其余按 capabilities。"""
    if str(name).startswith(_QZONE_CAPABILITY_PREFIX):
        return "qzone_write"
    return "capabilities"


def path_for_store(store: Any) -> Path | None:
    """授权文件位置：与数据库同目录；内存库返回 None（只在进程内生效）。"""
    path = getattr(store, "path", None)
    if not path or str(path) == ":memory:":
        return None
    return Path(path).parent / FILENAME


__all__ = [
    "FILENAME",
    "PluginPermissionError",
    "PluginPermissionStore",
    "capability_permission",
    "path_for_store",
]
