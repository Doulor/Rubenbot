"""插件配置 schema：规范化声明、默认值、逐字段校验与表单描述。

插件在 ``manifest.json`` 里用 ``config_schema`` 声明可配置项，WebUI 按它
自动生成表单、保存时按同一份规则校验。本模块是这份声明的唯一解读入口，
``rubenbot/web/market.py``（读写插件 ``config.yaml``）与插件详情页都从这里
取字段定义，避免前后端各写一套。

字段形状（只有 ``key`` 必填，其余可省略）::

    {
      "key": "cooldown_seconds",
      "type": "number",              # bool / int / number / string / text / list
      "default": 60,
      "description": "两次点歌之间的冷却秒数",
      "label": "冷却（秒）",           # 可选；表单标签，缺省用 description
      "min": 0, "max": 86400,        # 数字范围；字符串 / 列表用 min_length / max_length
      "step": 1,
      "options": ["manual", {"value": "auto", "label": "自动"}],
      "secret": false,                # 密钥类字段；缺省按 key 名猜
      "placeholder": "单位：秒"
    }

向后兼容：没有 ``config_schema`` 的插件照常工作；声明里不合法的条目会被
跳过并记 debug 日志，不会让插件变得不可加载。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 规范类型：其他写法都映射到这里
TYPE_ALIASES: dict[str, str] = {
    "bool": "bool",
    "boolean": "bool",
    "switch": "bool",
    "int": "int",
    "integer": "int",
    "float": "number",
    "number": "number",
    "string": "string",
    "str": "string",
    "text": "text",
    "list": "list",
    "string_list": "list",
    "strings": "list",
}

TYPE_LABELS: dict[str, str] = {
    "bool": "开关",
    "int": "整数",
    "number": "数字",
    "string": "文本",
    "text": "长文本",
    "list": "字符串列表",
}

# 密钥字段的保守识别：显式 secret 优先，没有声明时按 key 名猜
SECRET_TOKENS = ("key", "token", "secret", "password", "passwd")

_NUMERIC = frozenset({"int", "number"})

# as_dict 由规范化结果负责的字段：raw 里同名字段不再重复回填
_CANONICAL_FIELDS = frozenset({
    "key", "type", "default", "description", "label", "secret",
    "options", "min", "max", "step", "min_length", "max_length", "placeholder",
})


class ConfigValueError(ValueError):
    """单个配置值不合规；``field`` 用于前端定位出错字段。"""

    def __init__(self, field_key: str, message: str):
        super().__init__(f"{field_key} {message}")
        self.field_key = field_key
        self.message = message


@dataclass(frozen=True)
class SchemaField:
    """一份规范化的配置项声明。"""

    key: str
    type: str
    default: Any = None
    description: str = ""
    label: str = ""
    options: tuple[dict[str, Any], ...] = ()
    min: int | float | None = None
    max: int | float | None = None
    step: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    secret: bool = False
    placeholder: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """给 WebUI / 详情页的 JSON 视图（列表默认值复制一份，避免调用方改到原声明）。"""
        data: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "default": _copy(self.default),
            "description": self.description,
            "label": self.label,
            "secret": self.secret,
        }
        if self.options:
            data["options"] = [dict(item) for item in self.options]
        for name in ("min", "max", "step", "min_length", "max_length"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.placeholder:
            data["placeholder"] = self.placeholder
        # 保留声明里的额外展示字段（例如 future 的 group / advanced），但以规范值为准；
        # 已知字段不再从 raw 回填，避免把被修正掉的非法范围 / 可选值又带回来
        for name, value in self.raw.items():
            if name not in _CANONICAL_FIELDS:
                data.setdefault(name, _copy(value))
        return data


def _copy(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _type_default(kind: str) -> Any:
    if kind == "bool":
        return False
    if kind == "int":
        return 0
    if kind == "number":
        return 0.0
    if kind == "list":
        return []
    return ""


def is_secret_key(key: str) -> bool:
    """按 key 名猜是不是密钥字段（与市场页原口径一致）。"""
    lower = str(key or "").lower()
    return any(token in lower for token in SECRET_TOKENS)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _option_value_key(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def normalize_options(raw: Any, *, key: str = "") -> tuple[dict[str, Any], ...]:
    """把 ``options`` 归一化成 ``[{"value", "label", "description"?}]``。

    既接受裸值数组（``["direct", "planner"]``），也接受带标签的对象数组。
    值重复时保留第一项；空数组返回空元组（表示不限制可选值）。
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        logger.debug("插件配置项 %s 的 options 不是数组，忽略", key or "?")
        return ()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            if "value" not in item:
                continue
            value = item.get("value")
            label = str(item.get("label") or value if value is not None else "").strip()
            description = str(item.get("description") or "").strip()
        else:
            value = item
            label = str(value)
            description = ""
        if value is None:
            continue
        marker = _option_value_key(value)
        if marker in seen:
            continue
        seen.add(marker)
        option = {"value": _copy(value), "label": label or str(value)}
        if description:
            option["description"] = description
        result.append(option)
    return tuple(result)


def normalize_schema(schema: Any) -> tuple[SchemaField, ...]:
    """把 manifest 的 ``config_schema`` 归一化成字段元组。

    非数组、非对象条目、未知类型和重复 key 都跳过；不合法的范围 / 默认值
    会退回安全值。这个函数不抛异常，调用方可以放心地用它处理旧插件声明。
    """
    if not isinstance(schema, (list, tuple)):
        return ()
    fields: list[SchemaField] = []
    seen: set[str] = set()
    for item in schema:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        if key in seen:
            logger.warning("插件配置 schema 有重复字段 %s，保留第一项", key)
            continue
        raw_type = str(item.get("type") or "string").strip().lower()
        kind = TYPE_ALIASES.get(raw_type)
        if kind is None:
            logger.debug("插件配置项 %s 的类型 %s 不支持，已跳过", key, raw_type)
            continue
        seen.add(key)

        options = normalize_options(item.get("options"), key=key)
        low = high = step = None
        if kind in _NUMERIC:
            low = _number(item.get("min"))
            high = _number(item.get("max"))
            step = _number(item.get("step"))
            if low is not None and high is not None and low > high:
                logger.warning("插件配置项 %s 的 min > max，忽略范围", key)
                low = high = None
            if step is not None and step <= 0:
                step = None
        min_length = _int_or_none(item.get("min_length"))
        max_length = _int_or_none(item.get("max_length"))
        if min_length is not None and min_length < 0:
            min_length = None
        if max_length is not None and max_length < 0:
            max_length = None
        if (
            min_length is not None
            and max_length is not None
            and min_length > max_length
        ):
            logger.warning("插件配置项 %s 的 min_length > max_length，忽略长度范围", key)
            min_length = max_length = None

        secret = bool(item.get("secret")) or is_secret_key(key)
        default = item.get("default") if "default" in item else _type_default(kind)
        field_obj = SchemaField(
            key=key,
            type=kind,
            default=default,
            description=str(item.get("description") or "").strip(),
            label=str(item.get("label") or "").strip(),
            options=options,
            min=low,
            max=high,
            step=step,
            min_length=min_length,
            max_length=max_length,
            secret=secret,
            placeholder=str(item.get("placeholder") or "").strip(),
            raw=dict(item),
        )
        try:
            default = coerce_value(field_obj, default)
        except ConfigValueError as exc:
            logger.warning("插件配置项 %s 的默认值不合规（%s），改用类型默认值", key, exc.message)
            default = _type_default(kind)
        if default is not field_obj.default:
            # dataclass 是 frozen 的：默认值被归一或修正后生成一份新字段
            field_obj = dataclasses.replace(field_obj, default=default)
        fields.append(field_obj)
    return tuple(fields)


def effective_values(
    fields: tuple[SchemaField, ...] | list[SchemaField],
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """按「有值用值、缺省用默认值」展开 schema 里的键，忽略未知键。"""
    raw = raw if isinstance(raw, dict) else {}
    return {
        field.key: _copy(raw[field.key]) if field.key in raw else _copy(field.default)
        for field in fields
    }


def coerce_value(field: SchemaField, value: Any) -> Any:
    """校验并归一化单个值；不合规时抛 :class:`ConfigValueError`。

    - ``bool`` 只接受布尔值（不接受 0/1 或字符串）
    - ``int`` / ``number`` 必须是有限数字，不能是布尔值
    - ``string`` / ``text`` 必须是字符串，可声明长度范围
    - ``list`` 必须是字符串数组，去掉空项；可声明长度范围
    - 声明了 ``options`` 时，归一化后的值必须落在可选值内
    """
    kind = field.type
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigValueError(field.key, "必须是布尔值")
        result: Any = value
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigValueError(field.key, "必须是整数")
        if not float(value).is_integer():
            raise ConfigValueError(field.key, "必须是整数")
        result = int(value)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigValueError(field.key, "必须是数字")
        if not math.isfinite(float(value)):
            raise ConfigValueError(field.key, "必须是有限数字")
        result = float(value)
    elif kind == "list":
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise ConfigValueError(field.key, "必须是字符串列表")
        result = []
        for item in value:
            item = item.strip()
            if item and item not in result:
                result.append(item)
        if field.min_length is not None and len(result) < field.min_length:
            raise ConfigValueError(field.key, f"至少需要 {field.min_length} 项")
        if field.max_length is not None and len(result) > field.max_length:
            raise ConfigValueError(field.key, f"最多 {field.max_length} 项")
    else:  # string / text
        if not isinstance(value, str):
            raise ConfigValueError(field.key, "必须是字符串")
        result = value
        if field.min_length is not None and len(result) < field.min_length:
            raise ConfigValueError(field.key, f"不能少于 {field.min_length} 个字符")
        if field.max_length is not None and len(result) > field.max_length:
            raise ConfigValueError(field.key, f"不能超过 {field.max_length} 个字符")

    if kind in _NUMERIC:
        if field.min is not None and result < field.min:
            raise ConfigValueError(field.key, f"不能小于 {field.min}")
        if field.max is not None and result > field.max:
            raise ConfigValueError(field.key, f"不能大于 {field.max}")

    if field.options:
        allowed = [option["value"] for option in field.options]
        if kind == "list":
            wrong = [item for item in result if item not in allowed]
            if wrong:
                names = "、".join(str(item) for item in wrong)
                raise ConfigValueError(field.key, f"包含未允许的值：{names}")
        elif result not in allowed:
            allowed_text = "、".join(str(item) for item in allowed)
            raise ConfigValueError(field.key, f"只能是：{allowed_text}")
    return result


def validate_values(
    fields: tuple[SchemaField, ...] | list[SchemaField],
    values: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """校验一批已提交字段，返回 ``(规范化值, 错误列表)``。

    只处理 ``values`` 里出现的键；调用方先确认未知键。错误列表的每项形如
    ``{"field": "cooldown_seconds", "message": "不能小于 0"}``，可直接回给前端。
    """
    parsed: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for field_obj in fields:
        if field_obj.key not in values:
            continue
        value = values[field_obj.key]
        if field_obj.secret and value in (None, ""):
            # 密钥留空表示保留原值，由调用方决定怎么写盘
            continue
        try:
            parsed[field_obj.key] = coerce_value(field_obj, value)
        except ConfigValueError as exc:
            errors.append({"field": field_obj.key, "message": exc.message})
    return parsed, errors


def type_label(kind: str) -> str:
    return TYPE_LABELS.get(kind, kind)


__all__ = [
    "ConfigValueError",
    "SchemaField",
    "TYPE_ALIASES",
    "TYPE_LABELS",
    "coerce_value",
    "effective_values",
    "is_secret_key",
    "normalize_options",
    "normalize_schema",
    "type_label",
    "validate_values",
]
