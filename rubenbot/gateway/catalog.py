"""接口目录：每个适配器一份数据（名字 + 一句话说明 + 参数 schema + 风险级 + 默认冷却）。

目录是「数据」，不是「逻辑」：核心层只按目录里的风险级和冷却做闸门判断，
不关心具体是哪个平台的哪个接口。新增平台时只需要往
``rubenbot/gateway/catalogs/`` 放一份同名 JSON，核心零改动。

披露策略（渐进披露）：

- 列表模式（``catalog.search``）：只给名字、一句话说明与风险级；
- 详情模式（``catalog.describe``）：给出某个动作的参数 schema；
- ``denied`` 动作在两种模式下都不可见，调用入口直接拒绝。

参数 schema 的写法保持 OpenAI 工具参数风格（``type`` / ``required`` 由外层
``required`` 列表表达），没有精确 schema 的动作用 ``params=None`` 表示
free-form：调用时必须是可 JSON 序列化的对象。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..expression import CapabilityNote

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).parent / "catalogs"

# 风险级：denied 对模型不可见；sensitive 默认禁调；safe 正常可调
RISK_DENIED = "denied"
RISK_SENSITIVE = "sensitive"
RISK_SAFE = "safe"
_VALID_RISKS = (RISK_DENIED, RISK_SENSITIVE, RISK_SAFE)

# 只收已有真机验收记录的聊天动作，文案不超出实测范围。目录里新增自述不会自动发布。
_E2E_NOTES = {
    ('snowluma', 'delete_msg'): CapabilityNote(
        '撤回消息', '我能撤回自己刚在私聊发的消息。',
        '先看对方回复的是哪条；目标不清就问，不猜消息号。不承诺能撤掉别人的消息。',
        evidence=('snowluma:delete_msg:docs/gateway-e2e-results.md#D′5–D′7',),
    ),
}


@dataclass(frozen=True)
class ActionEntry:
    """目录里的一条动作。"""

    name: str
    summary: str
    risk: str = RISK_SAFE
    # 参数 schema：{"字段": {"type": ..., "description": ...}}；None 表示 free-form
    params: dict[str, Any] | None = None
    # 精确 schema 里必填的字段名
    required: tuple[str, ...] = ()
    # (动作, 会话) 冷却默认值（秒）；0 表示不冷却
    cooldown_seconds: float = 0.0
    # 主动类动作（发消息、贴表情、管理操作）计入每日预算与对象上限；
    # 只读查询不算，避免模型查目录/查群成员也耗预算
    proactive: bool = True
    # 可选的聊天能力说明；没有可面向用户的调用路径就不声明。
    capability_note: CapabilityNote | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any], *, adapter: str = '') -> "ActionEntry":
        risk = str(data.get("risk") or RISK_SAFE).strip().lower()
        if risk not in _VALID_RISKS:
            logger.warning("网关目录动作 %s 的风险级 %r 不认识，按 safe 处理", name, risk)
            risk = RISK_SAFE
        params = data.get("params")
        required = data.get("required") or ()
        proactive = data.get("proactive")
        if proactive is None:
            # 未写时按名字保守推断：get_ / can_ / get 开头的是只读
            proactive = not _looks_readonly(name)
        note = data.get("capability_note")
        fields = ("title", "summary", "detail")
        capability_note = None
        if isinstance(note, dict) and all(isinstance(note.get(k), str) and note[k].strip() for k in fields):
            capability_note = _E2E_NOTES.get((adapter, name)) if risk != RISK_DENIED else None
        return cls(
            name=name,
            capability_note=capability_note,
            summary=str(data.get("summary") or "").strip(),
            risk=risk,
            params=dict(params) if isinstance(params, dict) else None,
            required=tuple(str(item) for item in required if str(item).strip()),
            cooldown_seconds=max(0.0, float(data.get("cooldown_seconds") or 0.0)),
            proactive=bool(proactive),
        )

    def describe(self) -> str:
        """详情模式的文本：一句话说明 + 参数 schema。"""
        lines = [f"{self.name}：{self.summary or '（无说明）'}"]
        if self.params is None:
            lines.append("参数：自由对象（JSON），按接口文档自填")
            return "\n".join(lines)
        if not self.params:
            lines.append("参数：无")
            return "\n".join(lines)
        lines.append("参数：")
        for key, spec in self.params.items():
            spec = spec if isinstance(spec, dict) else {}
            type_name = spec.get("type") or "any"
            if isinstance(type_name, list):
                type_name = " / ".join(type_name)
            required = "必填" if key in self.required else "可选"
            desc = str(spec.get("description") or "").strip()
            tail = f" - {desc}" if desc else ""
            lines.append(f"- {key} ({type_name}, {required}){tail}")
        return "\n".join(lines)


def _looks_readonly(name: str) -> bool:
    return name.startswith(("get_", "can_", "fetch_ptt"))


@dataclass
class ActionCatalog:
    """某个适配器的完整动作目录。"""

    adapter: str
    actions: dict[str, ActionEntry] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, name: str) -> ActionEntry | None:
        """按名取动作（含 denied，调用入口靠它区分「不存在」与「禁止」）。"""
        return self.actions.get(str(name or "").strip())

    def visible(self) -> list[ActionEntry]:
        """对模型可见的动作（denied 不列）。"""
        return [entry for entry in self.actions.values() if entry.risk != RISK_DENIED]

    def search(self, keyword: str = "", *, limit: int = 30) -> list[ActionEntry]:
        """关键词过滤：匹配名字或说明；空关键词列出全部可见动作。

        说明按空格/标点切词后的逐词包含匹配，避免「发群」这种组合词搜不到。
        """
        words = [word for word in _split_keyword(keyword) if word]
        results: list[ActionEntry] = []
        for entry in self.visible():
            if not words:
                results.append(entry)
                continue
            haystack = f"{entry.name} {entry.summary}".lower()
            if all(word in haystack for word in words):
                results.append(entry)
        results.sort(key=lambda item: (item.risk != RISK_SAFE, item.name))
        return results[: max(1, int(limit))]

    def describe(self, name: str) -> str | None:
        """详情模式；denied 或不存在返回 None（对模型等同于不可见）。"""
        entry = self.get(name)
        if entry is None or entry.risk == RISK_DENIED:
            return None
        return entry.describe()

    def __len__(self) -> int:
        return len(self.actions)

    def __contains__(self, name: object) -> bool:
        return str(name) in self.actions

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_data(cls, adapter: str, data: Iterable[dict[str, Any]]) -> "ActionCatalog":
        actions: dict[str, ActionEntry] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in actions:
                continue
            actions[name] = ActionEntry.from_dict(name, item, adapter=adapter)
        return cls(adapter=adapter, actions=actions)

    @classmethod
    def load(cls, adapter: str, directory: str | Path | None = None) -> "ActionCatalog | None":
        """从 ``catalogs/<adapter>.json`` 读取；没有数据文件返回 None。"""
        base = Path(directory) if directory is not None else CATALOG_DIR
        path = base / f"{adapter}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取网关目录 %s 失败：%s", path, exc)
            return None
        if isinstance(data, dict):
            data = data.get("actions") or []
        if not isinstance(data, list):
            logger.warning("网关目录 %s 格式不对（顶层应为动作数组）", path)
            return None
        return cls.from_data(adapter, data)


def _split_keyword(keyword: str) -> list[str]:
    text = str(keyword or "").lower().strip()
    for sep in ("，", ",", "、", "；", ";", "/", "|", "  "):
        text = text.replace(sep, " ")
    return [word for word in text.split(" ") if word]


# 进程内缓存：目录文件很小，读一次就够；测试可用 ``clear_cache`` 重置
_CACHE: dict[str, ActionCatalog | None] = {}


def catalog_for(adapter: Any, *, directory: str | Path | None = None) -> ActionCatalog | None:
    """某适配器的目录。

    ``adapter`` 可以是适配器实例/类，也可以是名字。适配器自己声明了
    ``action_catalog`` 属性时优先用它（便于插件/测试注入）；否则按名字
    读数据文件。没有目录返回 None——网关工具据此不注册。
    """
    custom = getattr(adapter, "action_catalog", None)
    if isinstance(custom, ActionCatalog):
        return custom
    name = adapter if isinstance(adapter, str) else getattr(adapter, "name", "")
    name = str(name or "").strip()
    if not name:
        return None
    key = f"{directory or CATALOG_DIR}:{name}"
    if key not in _CACHE:
        _CACHE[key] = ActionCatalog.load(name, directory)
    return _CACHE[key]


def clear_cache() -> None:
    """清空目录缓存（测试与热更新用）。"""
    _CACHE.clear()
