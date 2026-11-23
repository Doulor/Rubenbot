"""关于页：本地版本与运行状态不依赖网络，仅显式检查更新时访问 GitHub。"""

from __future__ import annotations

import asyncio
import re
import subprocess
from importlib import metadata
from pathlib import Path

import httpx

from .. import __version__
from ..config import MODEL_ROLES
from ..plugins.loader import discover_plugin_dirs
from .state import WebState

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/Doulor/Rubenbot"
MAIN_API = "https://api.github.com/repos/Doulor/Rubenbot/commits/main"
_SHA = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")


def current_commit() -> tuple[str | None, str, str | None]:
    """只认代码目录自己的盖戳或 Git 工作树，不向上误认宿主项目。"""
    try:
        stamp = (ROOT / ".deploy-commit").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        stamp = ""
    if _SHA.fullmatch(stamp):
        return stamp.lower(), "deploy", None
    if (ROOT / ".git").exists():
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                check=True, timeout=2, encoding="ascii",
            ).stdout.strip()
            if _SHA.fullmatch(head):
                return head.lower(), "git", None
        except (OSError, UnicodeError, subprocess.SubprocessError):
            pass
    return None, "unknown", "没有有效的部署盖戳，也无法从本项目的 Git 工作树读取提交。"


def license_text() -> str | None:
    # 安装后 ROOT 是 site-packages，不能误读同目录下别的项目的 LICENSE。
    if (ROOT / "pyproject.toml").is_file():
        try:
            return (ROOT / "LICENSE").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            pass
    try:
        dist = metadata.distribution("rubenbot")
        # PEP 639 与旧版 setuptools 的 wheel 将许可证放在不同位置。
        return dist.read_text("licenses/LICENSE") or dist.read_text("LICENSE")
    except (metadata.PackageNotFoundError, OSError, UnicodeError):
        return None


def project_info() -> dict:
    commit, source, reason = current_commit()
    return {
        "project": {
            "name": "RubenBot", "version": __version__, "repository_url": REPOSITORY,
            "commit": commit, "commit_source": source, "commit_reason": reason,
            "commit_url": f"{REPOSITORY}/commit/{commit}" if commit else None,
        },
        "license": {"name": "MIT", "text": license_text()},
    }


def runtime_info(state: WebState) -> dict:
    # 独立 WebUI 仍可展示进程内配置，不读可能已经改动、尚未生效的 YAML。
    config = getattr(state.bot, "config", state.config)
    llm = config.llm
    roles = {}
    models = set()
    layered = llm.providers is not None
    configured = llm.role_pools if layered else llm.models
    for role in dict.fromkeys((*MODEL_ROLES, *configured, *llm.model_strategies)):
        pool = llm.targets_for(role)
        first = pool[0]
        roles[role] = {"model": f"{first.provider} / {first.model}" if layered else first.model,
                       "strategy": llm.strategy_for(role), "count": len(pool)}
        models.update((target.provider, target.model) for target in pool)

    adapters = getattr(state.bot, "adapters", {})
    snow = adapters.get("snowluma")
    if not state.is_running():
        snow_status = "not_running"
    elif snow is None:
        snow_status = "disabled"
    else:
        snow_status = "connected" if snow.connected else "disconnected"

    # 管理器已登记的插件即使被手工移走也可能仍在运行；不以读接口触发重扫或注册。
    manager = state.plugins
    if manager is not None:
        plugin_total, plugin_enabled = len(manager.plugins), len(manager.enabled_plugins)
    else:
        plugin_total, plugin_enabled = len(discover_plugin_dirs(config.plugins.dir)), 0
    return {
        "roles": roles,
        "models": {f"{role}_model": roles[role]["model"] if layered else getattr(llm, f"{role}_model") or None
                   for role in ("chat", "extract", "consolidate", "gate")},
        "model_strategy": llm.model_strategy, "model_count": len(models),
        "adapters": sorted(adapters), "snowluma_status": snow_status,
        "plugins": {"total": plugin_total, "enabled": plugin_enabled},
    }


async def check_update() -> dict:
    try:
        # httpx 的超时按 I/O 阶段计，外层再约束整次网络请求的总时长。
        async with asyncio.timeout(10), httpx.AsyncClient(timeout=10) as client:
            response = await client.get(MAIN_API)
            response.raise_for_status()
            payload = response.json()
            latest = payload.get("sha") if isinstance(payload, dict) else None
            if not isinstance(latest, str) or not _SHA.fullmatch(latest):
                raise ValueError("GitHub 没有返回完整的提交哈希")
            latest = latest.lower()
    except Exception:  # 代理配置、TLS、超时、GitHub 返回异常都只影响本次检查。
        return {"status": "error", "message": "检查失败，请稍后再试", "latest_commit": None,
                "commit_url": None}
    local, _, _ = await asyncio.to_thread(current_commit)
    if not local:
        status, message = "unknown", "本地提交未知，无法判断是否有更新"
    elif local == latest:
        status, message = "current", "已是最新"
    else:
        status, message = "available", f"有更新：{latest[:7]}"
    return {"status": status, "message": message, "latest_commit": latest,
            "commit_url": f"{REPOSITORY}/commit/{latest}"}
