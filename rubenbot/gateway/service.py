"""网关服务：把目录、闸门、执行层和工具定义串起来。

核心层只认抽象动作名与归一化参数；具体怎么调由适配器的
``call_action(name, params)`` 实现。这个模块对 core 暴露：

- :meth:`GatewayService.available` / :meth:`GatewayService.tools`：工具是否
  注册、注册哪些（``adapter_actions`` / ``adapter_call``）；
- :meth:`GatewayService.list_actions` / :meth:`GatewayService.describe_action`：
  目录的渐进披露；
- :meth:`GatewayService.call`：闸门 → 执行 → 审计，结果/错误摘要回模型。

注册条件（三个同时满足）：配置 ``gateway.enabled``、当前适配器有目录、
会话在 ``enabled_conversations`` 内——**私聊主人会话恒可**，依据见
:func:`is_owner_chat` 的注释。
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

from ..adapters.base import ActionNotSupported, BaseAdapter
from ..config import Config
from ..expression import CapabilityNote
from .catalog import RISK_DENIED, RISK_SENSITIVE, ActionCatalog, ActionEntry, catalog_for
from .gatekeeper import CREDENTIAL_ACTIONS, GateDecision, Gatekeeper, object_id_from_params

logger = logging.getLogger(__name__)

# 回给模型的错误/结果摘要长度上限（错误截断 500 字供其重试）
RESULT_MAX_CHARS = 1500
ERROR_MAX_CHARS = 500

# 贴表情的抽象动作：目录里有它时 react 才可用
REACT_ACTION = "set_msg_emoji_like"

ADAPTER_ACTIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "adapter_actions",
        "description": (
            "查询当前平台适配器暴露的接口目录。不给 action 时按关键词列出可用接口"
            "（名字 + 一句话说明）；给了 action 就返回它的参数 schema。"
            "调用任何平台接口之前先用它查清楚，不要凭空猜参数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "关键词过滤（如「表情」「群成员」「撤回」）；留空列出全部",
                },
                "action": {
                    "type": "string",
                    "description": "要看参数详情的接口名；给了它就不再按关键词列",
                },
            },
        },
    },
}

ADAPTER_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "adapter_call",
        "description": (
            "调用平台适配器的一个接口。action 必须是 adapter_actions 里查到的名字，"
            "params 是参数对象。返回结果或失败原因；失败会说明原因，可调整后重试。"
            "有冷却/额度限制时会把剩余时间告诉你，不要硬试。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "接口名"},
                "params": {
                    "type": "object",
                    "description": "参数对象（字段见 adapter_actions 的详情）",
                },
            },
            "required": ["action"],
        },
    },
}

# 系统提示里的单行指针（不许塞目录全文）
GATEWAY_GUIDE = (
    "【平台接口】需要调平台接口时，先用 adapter_actions 查目录确认名字与参数，"
    "再用 adapter_call 调用；给消息贴表情可用 set_msg_emoji_like。"
)


def is_owner_chat(config: Config, platform: str, chat_id: str, chat_type: str = "private") -> bool:
    """这个会话是不是「私聊主人」——网关与敏感动作恒可的会话。

    依据：本项目没有单独的主人账号配置（插件 qzone_post 用 ``owner_ids``，
    核心层不引入第二套主人概念）。私聊就是「只对一个人说话」的场合：
    能私聊到 bot 的人就是这台机器的主人/维护者自己，因此在私聊会话里
    开放网关不会把平台接口暴露给群里任何人。这是「私聊主人恒可」在
    当前配置模型下的等价实现；以后若引入 ``owner_ids``，这里改一处即可。
    """
    return str(chat_type).lower() == "private"


class GatewayService:
    """一次进程内的网关服务。目录按适配器缓存，闸门状态落库。"""

    def __init__(self, store: Any, config: Config):
        self.store = store
        self.config = config
        self.gatekeeper = Gatekeeper(store, config)

    # ------------------------------------------------------------------
    # 注册条件
    # ------------------------------------------------------------------

    def catalog_for(self, adapter: BaseAdapter | str | None) -> ActionCatalog | None:
        if adapter is None:
            return None
        return catalog_for(adapter)

    def available(
        self,
        adapter: BaseAdapter | None,
        *,
        chat_id: str = "",
        chat_type: str = "group",
    ) -> bool:
        """工具注册的总条件：开关、目录、适配器能力、会话白名单。"""
        if not self.config.gateway.enabled:
            return False
        if adapter is None or not adapter.supports_actions():
            return False
        if self.catalog_for(adapter) is None:
            return False
        return self.conversation_allowed(adapter, chat_id=chat_id, chat_type=chat_type)

    def conversation_allowed(
        self,
        adapter: BaseAdapter | str | None,
        *,
        chat_id: str,
        chat_type: str = "group",
    ) -> bool:
        """会话是否在允许范围：私聊主人恒可；其余必须在 enabled_conversations 里。

        白名单为空时群聊一律不放行（默认拒绝）：打开总开关不该等于把平台
        接口暴露到所有群里，要开哪个群就显式写进名单。
        """
        name = adapter if isinstance(adapter, str) else getattr(adapter, "name", "")
        name = str(name or "")
        if is_owner_chat(self.config, name, chat_id, chat_type):
            return True
        allowed = set(self.config.gateway.enabled_conversations)
        if not allowed:
            return False
        return f"{name}:{chat_id}" in allowed or str(chat_id) in allowed

    def tools(
        self,
        adapter: BaseAdapter | None,
        *,
        chat_id: str = "",
        chat_type: str = "group",
    ) -> list[dict[str, Any]]:
        """当前会话可用的网关工具；不可用时返回空列表。"""
        if not self.available(adapter, chat_id=chat_id, chat_type=chat_type):
            return []
        return [ADAPTER_ACTIONS_TOOL, ADAPTER_CALL_TOOL]

    def capability_notes(self, adapter: BaseAdapter, *, chat_id: str, chat_type: str) -> list[CapabilityNote]:
        """从本会话开放目录生成聊天能力说明；说明不授予原始工具权限。"""
        if not self.available(adapter, chat_id=chat_id, chat_type=chat_type):
            return []
        catalog = self.catalog_for(adapter)
        return [entry.capability_note for entry in catalog.visible() if entry.capability_note]

    def guide(self, adapter: BaseAdapter | None, *, chat_id: str = "", chat_type: str = "group") -> str:
        """系统提示里的一行指针；工具不可用时为空。"""
        if not self.available(adapter, chat_id=chat_id, chat_type=chat_type):
            return ""
        catalog = self.catalog_for(adapter)
        if catalog is not None and REACT_ACTION not in catalog:
            return "【平台接口】需要调平台接口时，先用 adapter_actions 查目录，再用 adapter_call 调用。"
        return GATEWAY_GUIDE

    # ------------------------------------------------------------------
    # 目录披露
    # ------------------------------------------------------------------

    def list_actions(
        self, adapter: BaseAdapter, *, keyword: str = "", limit: int = 30
    ) -> str:
        catalog = self.catalog_for(adapter)
        if catalog is None:
            return "当前适配器没有接口目录。"
        entries = catalog.search(keyword, limit=limit)
        if not entries:
            return f"没有匹配「{keyword}」的接口。" if keyword else "目录是空的。"
        lines = [f"共 {len(entries)} 个接口（用 action 参数可看单个接口的参数）："]
        for entry in entries:
            lines.append(f"- {entry.name}：{entry.summary or '（无说明）'}")
        return "\n".join(lines)

    def describe_action(self, adapter: BaseAdapter, name: str) -> str:
        catalog = self.catalog_for(adapter)
        if catalog is None:
            return "当前适配器没有接口目录。"
        text = catalog.describe(name)
        if text is None:
            # denied 与不存在对模型是同一个答案：不给任何线索
            return f"没有名为 {name or '（空）'} 的接口。"
        return text

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def call(
        self,
        adapter: BaseAdapter | None,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        chat_id: str = "",
        chat_type: str = "group",
        source: str = "model",
        now: float | None = None,
    ) -> str:
        """闸门 → 执行 → 审计；永远返回给模型看的文本，不抛异常。"""
        action = str(action or "").strip()
        credential_action = action in CREDENTIAL_ACTIONS
        chat_recall = source == "chat" and action == "delete_msg"
        chat_failure = "没能撤回这条消息，平台没有确认成功。"
        chat_key = self._chat_key(adapter, chat_id)
        params = params if isinstance(params, dict) else {}

        def unavailable(reason: str) -> str:
            # 撤回请求即便被总开关或会话白名单拦下，也留一笔；此时不查台账。
            if action == "delete_msg":
                self.gatekeeper.audit(action=action, source=source, chat_key=chat_key, chat_type=chat_type,
                                      params=params, reject_reason=reason, sensitive=True, now=now)
            return reason

        if adapter is None:
            return unavailable("当前没有可用的适配器。")
        if not self.config.gateway.enabled:
            return unavailable("平台接口网关未开启。")
        # 没有动作通道的适配器（Telegram / QQ 官方）：明确说不支持，不静默
        if not adapter.supports_actions():
            return unavailable(f"当前适配器不支持动作调用（{action or '未知动作'}）。")
        catalog = self.catalog_for(adapter)
        if catalog is None:
            return unavailable("当前适配器没有接口目录。")
        if not self.conversation_allowed(adapter, chat_id=chat_id, chat_type=chat_type):
            return unavailable("这个会话不在网关允许范围内。")
        entry = catalog.get(action)
        if entry is None:
            # 不存在与 denied 对模型是同一个答案：不泄露被禁动作
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                reject_reason="unknown action",
                now=now,
            )
            return f"没有名为 {action or '（空）'} 的接口。"
        if entry.risk == RISK_DENIED:
            reason = "这个接口被禁止调用（账号破坏性操作）。"
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                reject_reason=reason,
                sensitive=True,
                now=now,
            )
            return reason

        problem = validate_params(entry, params)
        if problem is not None:
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                reject_reason=problem,
                sensitive=entry.risk == RISK_SENSITIVE,
                now=now,
            )
            return "凭据接口参数不对。" if credential_action else f"参数不对：{problem}"

        decision: GateDecision = self.gatekeeper.check(
            entry,
            chat_key=chat_key,
            chat_type=chat_type,
            params=params,
            object_id=object_id_from_params(params),
            now=now,
        )
        if not decision.allowed:
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                reject_reason=decision.reason,
                sensitive=decision.sensitive,
                now=now,
            )
            return decision.reason

        try:
            result = await adapter.call_action(action, params)
        except ActionNotSupported as exc:
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                reject_reason="不支持此动作" if credential_action else str(exc),
                sensitive=decision.sensitive,
                now=now,
            )
            return chat_failure if chat_recall else f"当前适配器不支持动作 {action}。"
        except Exception as exc:  # noqa: BLE001 - 失败原因回给模型供其重试
            summary = (
                "凭据接口错误详情已省略" if credential_action
                else _truncate(str(exc) or exc.__class__.__name__, ERROR_MAX_CHARS)
            )
            self.gatekeeper.note_failure(entry, now=now)
            self.gatekeeper.audit(
                action=action,
                source=source,
                chat_key=chat_key,
                chat_type=chat_type,
                params=params,
                ok=False,
                result=summary,
                sensitive=decision.sensitive,
                now=now,
            )
            logger.warning("网关动作 %s 失败：%s", action, summary)
            return chat_failure if chat_recall else f"{action} 调用失败：{summary}"

        self.gatekeeper.note_success(entry, chat_key=chat_key, object_id=decision.object_id, now=now)
        # 事件调用方只把返回文本写日志，不消费凭据；模型工具仍拿原有结果摘要。
        summary = "" if credential_action and source == "event" else _summarize_result(result)
        self.gatekeeper.audit(
            action=action,
            source=source,
            chat_key=chat_key,
            chat_type=chat_type,
            params=params,
            ok=True,
            result=summary,
            sensitive=decision.sensitive,
            now=now,
        )
        logger.info("网关动作 %s 成功（%s）", action, chat_key,
                    extra={"platform": adapter.name, "chat_type": chat_type, "chat_id": chat_id})
        if chat_recall:
            return "已撤回。"
        return f"{action} 调用成功：{summary}" if summary else f"{action} 调用成功。"

    # ------------------------------------------------------------------

    def _chat_key(self, adapter: BaseAdapter | None, chat_id: str) -> str:
        name = adapter if isinstance(adapter, str) else getattr(adapter, "name", "")
        return f"{name}:{chat_id}" if chat_id else str(name or "")


def validate_params(entry: ActionEntry, params: dict[str, Any]) -> str | None:
    """参数校验：必填、类型、可 JSON 序列化；返回问题描述或 None。

    free-form 动作（``params=None``）只要求是对象且可序列化。
    """
    if not isinstance(params, dict):
        return "params 必须是对象（JSON dict）。"
    try:
        json.dumps(params, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return f"params 不能序列化成 JSON：{exc}"
    if entry.params is None:
        return None
    for key in entry.required:
        if params.get(key) in (None, ""):
            return f"缺少必填参数 {key}。"
    for key, spec in entry.params.items():
        if key not in params:
            continue
        expected = spec.get("type") if isinstance(spec, dict) else None
        types = expected if isinstance(expected, list) else [expected or "any"]
        if not any(_type_ok(kind, params[key]) for kind in types):
            return f"参数 {key} 应该是 {' / '.join(types)}。"
    return None


def _type_ok(expected: str, value: Any) -> bool:
    if expected in ("", "any"):
        return True
    if expected in ("integer", "int"):
        return isinstance(value, int) and not isinstance(value, bool) or (
            isinstance(value, str) and value.strip().lstrip("-").isdigit()
        )
    if expected in ("number", "float"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _summarize_result(result: Any) -> str:
    """把返回值压成一段短文本：优先 message_id 之类的关键字段。"""
    if result is None:
        return "ok"
    if isinstance(result, (str, int, float)) and not isinstance(result, bool):
        return _truncate(str(result), RESULT_MAX_CHARS)
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return _truncate(text, RESULT_MAX_CHARS)


def _truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
