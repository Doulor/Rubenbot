"""OpenAI 兼容的 LLM 客户端。

只实现 chat/completions 这一条路径，够用且不引入额外依赖。
``complete(role=...)`` 里的 role 决定使用哪个模型池（chat / extract / consolidate，
以及 fast / summary / vision 等预置角色），回退规则见 :class:`LLMConfig.model_for`，
模型池与策略见 :meth:`LLMConfig.models_for` / :meth:`LLMConfig.strategy_for`。
``task=`` 是给用量统计用的标签，默认取 role；插件可传自定义名字。

模型池（Q14）：一个角色可以配多个模型。``failover`` 按顺序调、失败切下一个；
``round_robin`` 每次调用轮换起点；``random`` 每次均匀随机。列表里只有一个模型
时三种策略与旧行为完全一致（沿用 ``max_retries`` 的退避重试）；多个模型时最多
按列表走一遍、不额外退避，全部失败抛出最后一次的错误。

可选的 ``usage_recorder`` 在每次调用尝试后回调一条用量记录；上游没给 usage
时 token 记 ``None``（落库为 NULL），不猜不编。记录失败只记 debug，不影响主流程。
测试里可以用任意实现了 ``complete`` 的假对象替换。

另外负责把上游偶发吐在 ``content`` 里的 DSML 文本工具调用（形如
``<...DSML... calls>`` 的标签块，竖线可能是全角）解析回与结构化调用相同
的内部表示；解析失败只剥除文本并记 warning，绝不抛异常打断正常聊天。
出站前再用 :func:`strip_dsml` 兜底剥除残留片段。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

import httpx

from .config import LLMConfig

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

# 值得重试的 HTTP 状态：超时/限流/服务端故障
RETRYABLE_STATUS = frozenset({408, 429})
# 指数退避间隔（秒）：最多重试 2 次 → 1s / 3s；次数更多时按 2 倍继续
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)


@dataclass
class LLMUsage:
    """一次上游调用的用量快照。

    ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` 为 None 表示
    上游响应里没有 usage 字段（或这次尝试根本没拿到响应）。
    """

    ts: float
    task: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int = 0
    ok: bool = False


# usage 回调签名：可能是同步的，也可能要写库（异步），两种都支持
UsageRecorder = Callable[[LLMUsage], Any | Awaitable[Any]]


class LLMProtocol(Protocol):
    """主循环只依赖这个最小接口，方便 mock。"""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "chat",
        task: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str: ...


class LLMError(RuntimeError):
    """LLM 调用失败。"""


class ToolUnsupported(LLMError):
    """当前中转/模型不支持 OpenAI 工具调用（拿它当正常退化信号，不当异常上报）。"""


class LLMResponseError(LLMError):
    """上游返回了非 2xx；``status`` 供重试策略判断。"""

    def __init__(self, message: str, *, status: int):
        super().__init__(message)
        self.status = status


def is_retryable_status(status: int) -> bool:
    """408 / 429 / 5xx 值得退避重试；401、404 这类配置错误不重试。"""
    return status in RETRYABLE_STATUS or 500 <= status < 600


def retry_delay(attempt: int, delays: tuple[float, ...] = RETRY_DELAYS) -> float:
    """第 ``attempt`` 次重试前等待的秒数（从 0 开始数）。"""
    if not delays:
        return 0.0
    if attempt < len(delays):
        return delays[attempt]
    return delays[-1] * (2 ** (attempt - len(delays) + 1))


def usage_from_response(data: Any) -> tuple[int | None, int | None, int | None]:
    """从响应 JSON 里取 (prompt, completion, total) tokens；缺失的项为 None。

    ``total`` 缺失时只在 prompt/completion 都有值时才相加，不伪造半份数据。
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    prompt = _as_token_count(usage.get("prompt_tokens"))
    completion = _as_token_count(usage.get("completion_tokens"))
    total = _as_token_count(usage.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return prompt, completion, total


def _as_token_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class LLMClient:
    """OpenAI 兼容的异步客户端。"""

    def __init__(
        self,
        config: LLMConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_recorder: UsageRecorder | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ):
        self.config = config
        self._transport = transport
        self._usage_recorder = usage_recorder
        # 测试可注入零延迟；生产用模块默认的 1s / 3s
        self._retry_delays = RETRY_DELAYS if retry_delays is None else retry_delays
        self._client: httpx.AsyncClient | None = None
        # 探测到不支持工具后置 True，后续不再白试（省一次失败请求）
        self._tools_unsupported = False
        # 轮询策略的调用计数：只放内存，重启/重建客户端后从头开始（规格允许）
        self._round_robin: dict[str, int] = {}

    @property
    def supports_tools(self) -> bool:
        """当前是否还能尝试工具调用；模型/中转不支持时由 ``chat_with_tools``
        抛出 :class:`ToolUnsupported` 并置 False。"""
        return not self._tools_unsupported

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "chat",
        task: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """调用 chat/completions，返回第一条回复的文本内容。

        ``task`` 只用于用量打标；默认与 ``role`` 相同。每次尝试都会记一条
        用量（失败记 ok=0，token 为 NULL）；记录里的 model 是这次实际用的模型。
        """
        task_name = (task or role or "chat").strip() or "chat"
        temperature_value = (
            self.config.chat_temperature
            if role == "chat" and temperature is None and self.config.chat_temperature is not None
            else self.config.temperature if temperature is None else temperature
        )
        payload: dict[str, Any] = {"messages": messages, "temperature": temperature_value}
        if role == "chat" and self.config.chat_frequency_penalty:
            payload["frequency_penalty"] = self.config.chat_frequency_penalty
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        def on_success(data: Any) -> str:
            choices = (data.get("choices") if isinstance(data, dict) else None) or []
            if not choices:
                # 已经被 _call_single 的 choices 检查拦下来，这里只是兜底
                return ""
            content = choices[0].get("message", {}).get("content") or ""
            # 普通生成路径用不到工具，残留的 DSML 文本只剥不留
            return strip_dsml(content)

        return await self._call_pool(
            role,
            task_name,
            payload,
            on_success=on_success,
            allow_tool_unsupported=False,
        )

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        role: str = "chat",
        task: str | None = None,
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """单次工具调用请求，返回原始 ``choices[0].message``（可能带 tool_calls）。

        只做一次往返，工具循环由调用方控制（core 里最多两轮）。中转或模型不
        支持工具调用时抛 :class:`ToolUnsupported`，由调用方退化为普通生成。
        与 ``complete`` 一样按 ``task``（默认取 role）记一条用量，并且同样
        支持模型池的故障转移（工具调用失败时切下一个模型）。
        """
        if getattr(self, "_tools_unsupported", False):
            raise ToolUnsupported("已探测到当前模型不支持工具调用")
        task_name = (task or role or "chat").strip() or "chat"
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        def on_success(data: Any) -> dict[str, Any]:
            choices = (data.get("choices") if isinstance(data, dict) else None) or []
            message = choices[0].get("message") or {}
            # 上游可能把工具调用写成 content 里的 DSML 文本；统一转成结构化 tool_calls，
            # 让调用方走同一条执行通道（权限/审计/回喂都一样）。
            return normalize_dsml_message(message)

        return await self._call_pool(
            role,
            task_name,
            payload,
            on_success=on_success,
            allow_tool_unsupported=True,
        )

    async def _call_pool(
        self,
        role: str,
        task: str,
        payload: dict[str, Any],
        *,
        on_success: Callable[[Any], Any],
        allow_tool_unsupported: bool,
    ) -> Any:
        """按角色的模型池与策略发请求；返回 ``on_success`` 的结果。

        - 单模型列表：完全沿用旧行为——``max_retries`` 次退避重试，最终失败
          抛 ``LLM 调用最终失败``。
        - 多模型列表：按策略排好的顺序最多走一遍；可重试的失败切下一个模型，
          400/401/403/404 这类换模型也救不了的错误直接抛（不重试、不降级）。
          全部失败时抛出最后一次的错误（包成 ``LLM 调用最终失败: ...``）。
        """
        models = self.config.models_for(role)
        strategy = self.config.strategy_for(role)
        order = self._model_order(role, models, strategy)
        client = await self._get_client()

        if len(order) == 1:
            return await self._call_single(
                client, order[0], task, payload, on_success=on_success,
                allow_tool_unsupported=allow_tool_unsupported,
                # 工具调用一直只做一次往返，不因 max_retries 变成多次
                retries=0 if allow_tool_unsupported else None,
            )

        # 按顺序尝试模型池，返回第一个成功的结果。可重试的失败切下一个；
        # 全部失败抛最后一次的错误（保留原异常类型，不吞上游信息）。
        last_error: Exception | None = None
        for index, model in enumerate(order):
            try:
                return await self._call_single(
                    client, model, task, payload, on_success=on_success,
                    allow_tool_unsupported=allow_tool_unsupported, retries=0,
                )
            except ToolUnsupported:
                raise
            except LLMResponseError as exc:
                if not is_retryable_status(exc.status):
                    # 401/403/404/400：换模型也救不了，直接报，不做无谓切换
                    raise
                last_error = exc
            except LLMError as exc:
                # 网络/超时/空响应这类换模型可能有用，试下一个；
                # 去掉单次调用那层「最终失败」包装，留下真正的错误原因
                cause = exc.__cause__
                last_error = cause if isinstance(cause, Exception) else exc
            except httpx.HTTPError as exc:
                last_error = exc
            if last_error is not None and index + 1 < len(order):
                logger.warning(
                    "模型 %s 调用失败（%s），切换到下一个：%s", model, strategy, last_error
                )
        assert last_error is not None
        # 全部失败时沿用与单模型一致的错误形状（信息里带最后一次的模型/状态），
        # 网络错误和空响应都包成 LLMError，调用方统一按 LLM 失败处理。
        raise LLMError(f"LLM 调用最终失败: {last_error}") from last_error

    def _model_order(self, role: str, models: list[str], strategy: str) -> list[str]:
        """按策略给出本次调用的尝试顺序。

        - ``failover``：原顺序，依次尝试；
        - ``round_robin``：以内存计数为起点轮换，计数只增不减（重启可重置）；
        - ``random``：均匀随机起点，后续仍按列表顺序（全部试到）。
        """
        if len(models) <= 1 or strategy == "failover":
            return list(models)
        key = str(role or "chat").strip() or "chat"
        if strategy == "round_robin":
            start = self._round_robin.get(key, 0) % len(models)
            self._round_robin[key] = start + 1
        else:  # random
            start = random.randrange(len(models))
        return models[start:] + models[:start]

    async def _call_single(
        self,
        client: httpx.AsyncClient,
        model: str,
        task: str,
        payload: dict[str, Any],
        *,
        on_success: Callable[[Any], Any],
        allow_tool_unsupported: bool,
        retries: int | None = None,
    ) -> Any:
        """用指定模型调一次（失败时按 ``retries`` 退避重试，默认 ``max_retries``）。

        ``allow_tool_unsupported`` 为 True 时把「不支持工具」的 4xx 转成
        :class:`ToolUnsupported` 并记住，后续不再白试。
        """
        body = {**payload, "model": model}
        retries = self.config.max_retries if retries is None else retries
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                resp = await client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                # 网络/超时层失败：这次尝试没有响应，token 记 NULL
                await self._record_usage(task, model, None, started, ok=False)
                last_error = exc
            else:
                try:
                    data: Any = resp.json()
                except ValueError:
                    data = None
                if resp.status_code >= 400:
                    # 429/5xx 也可能带 usage 计费信息，能读到就记上
                    await self._record_usage(task, model, data, started, ok=False)
                    text = resp.text[:300]
                    if allow_tool_unsupported and resp.status_code in (400, 404, 422, 501):
                        lowered = text.lower()
                        if "tool" in lowered or "function" in lowered or "unsupported" in lowered:
                            self._tools_unsupported = True
                            raise ToolUnsupported(f"模型不支持工具调用：{text}")
                    error: Exception = LLMResponseError(
                        f"LLM 返回 {resp.status_code}: {text}", status=resp.status_code
                    )
                    if not is_retryable_status(resp.status_code):
                        # 401/404 这类配置错误重试也没用，直接失败
                        raise error
                    last_error = error
                else:
                    choices = (data.get("choices") if isinstance(data, dict) else None) or []
                    if not choices:
                        await self._record_usage(task, model, data, started, ok=False)
                        last_error = LLMError(f"LLM 响应缺少 choices: {str(data)[:300]}")
                    else:
                        result = on_success(data)
                        await self._record_usage(task, model, data, started, ok=True)
                        return result
            if attempt >= retries:
                break
            delay = retry_delay(attempt, self._retry_delays)
            logger.warning(
                "LLM 调用失败（第 %s 次），%.1fs 后重试：%s", attempt + 1, delay, last_error
            )
            await asyncio.sleep(delay)
        raise LLMError(f"LLM 调用最终失败: {last_error}") from last_error

    async def _record_usage(
        self,
        task: str,
        model: str,
        data: Any,
        started: float,
        *,
        ok: bool,
    ) -> None:
        """写一条用量；没配回调或写失败都只记 debug，绝不影响调用结果。"""
        recorder = self._usage_recorder
        if recorder is None:
            return
        prompt, completion, total = usage_from_response(data)
        usage = LLMUsage(
            ts=time.time(),
            task=task,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            ok=ok,
        )
        try:
            result = recorder(usage)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 统计失败不能影响主流程
            logger.debug("LLM 用量记录失败：%s", exc)


def make_usage_recorder(store: Any) -> UsageRecorder | None:
    """把 store 包成用量回调；``store`` 没有用量表（或测试用的假 store）时返回 None。"""
    add = getattr(store, "add_llm_usage", None)
    if not callable(add):
        return None

    def record(usage: LLMUsage) -> None:
        add(
            task=usage.task,
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=usage.latency_ms,
            ok=usage.ok,
            ts=usage.ts,
        )

    return record


def parse_json_payload(text: str) -> Any:
    """尽力从模型输出里解析 JSON：支持 ``` 代码块包裹和前后废话。"""
    text = (text or "").strip()
    if not text:
        return None
    block = _JSON_BLOCK.search(text)
    if block:
        text = block.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退化：取第一个 [ 或 { 到最后一个 ] 或 }
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start >= 0:
        end = max(text.rfind("]"), text.rfind("}"))
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


# ----------------------------------------------------------------------
# DSML 文本工具调用
# ----------------------------------------------------------------------
# 上游（DeepSeek 系）有时不吐结构化 tool_calls，而是把调用写成 content 里的
# 标签文本。标签的竖线可能是全角 ｜、数量也不固定，这里一律宽松匹配：
#   <｜｜DSML｜｜ calls>      块边界
#   <｜｜DSML｜｜ invoke ...> 一次工具调用
#   <｜｜DSML｜｜ parameter ...> 一个参数
_DSML_TAG_RE = re.compile(
    r"<\s*(?P<closing>/?)\s*[|｜]*\s*DSML\s*[|｜]*\s*"
    r"(?P<name>calls|invoke|parameter)\b(?P<attrs>[^<>]*)>",
    re.IGNORECASE,
)
# 「疑似 DSML 痕迹」：只要看到这个词就不让原文漏到出站消息里。
# 标签竖线可能是全角、也可能被拆条从中间切断，所以不依赖具体标签形态；
# 大写 DSML 在正常中文聊天里几乎不会自然出现，按出现即处理。
_DSML_HINT_RE = re.compile(r"DSML")
_DSML_ATTR_RE = re.compile(r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")


def _dsml_attrs(raw: str) -> dict[str, str]:
    """解析标签属性（``name="x" string="true"``）；容忍单/双/无引号。"""
    attrs: dict[str, str] = {}
    for match in _DSML_ATTR_RE.finditer(raw or ""):
        value = next((group for group in match.groups()[1:] if group is not None), "")
        attrs[match.group(1).lower()] = value
    return attrs


def _dsml_value(raw: str, attrs: dict[str, str]) -> Any:
    """按 ``string`` 属性解释参数值：true 原样字符串，false 按 JSON 解析。

    解析失败不抛异常：按原文字符串回传并记 warning，让工具层自己去报参数问题。
    """
    text = (raw or "").strip()
    flag = str(attrs.get("string", "")).strip().lower()
    if flag in ("true", "1", "yes"):
        return text
    if not text:
        return ""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        if flag in ("false", "0", "no"):
            logger.warning("DSML 参数按 JSON 解析失败，按字符串回传：%r", text[:80])
        return text


def parse_dsml_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """把 content 里的 DSML 文本调用解析成结构化 tool_calls。

    返回 ``(调用列表, 块外正常文本)``。调用形如::

        {"id": "dsml_call_1", "type": "function",
         "function": {"name": "adapter_call", "arguments": '{"action": ...}'}}

    一个 calls 块可含多个 invoke，全部转换；块内无法识别的部分丢弃。
    没有闭合的 calls 块视为畸形：已完整闭合的 invoke 仍然保留，块尾文本丢弃。
    本函数不抛异常。
    """
    calls: list[dict[str, Any]] = []
    if not text or not _DSML_HINT_RE.search(text):
        return calls, text

    kept: list[str] = []
    cursor = 0
    inside = False
    invoke: dict[str, Any] | None = None
    param: dict[str, Any] | None = None

    def flush_param(end: int) -> None:
        nonlocal param
        if param is None:
            return
        name = str(param.get("name") or "").strip()
        if invoke is not None and name:
            invoke["args"][name] = _dsml_value(text[param["start"] : end], param["attrs"])
        elif not name:
            logger.warning("DSML parameter 缺少 name，已忽略")
        param = None

    def flush_invoke() -> None:
        nonlocal invoke
        if invoke is None:
            return
        name = str(invoke.get("name") or "").strip()
        if name:
            calls.append(
                {
                    "id": f"dsml_call_{len(calls) + 1}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(invoke["args"], ensure_ascii=False),
                    },
                }
            )
        else:
            logger.warning("DSML invoke 缺少 name，已忽略")
        invoke = None

    for match in _DSML_TAG_RE.finditer(text):
        if not inside:
            kept.append(text[cursor:match.start()])
        cursor = match.end()
        closing = bool(match.group("closing"))
        name = match.group("name").lower()
        attrs = _dsml_attrs(match.group("attrs"))
        if name == "calls":
            if closing:
                if inside:
                    flush_param(match.start())
                    flush_invoke()
                inside = False
            else:
                if inside:
                    # 嵌套 calls：按新块处理，上一块的完成部分照常保留
                    logger.warning("DSML calls 块嵌套，按新一轮处理")
                    flush_param(match.start())
                    flush_invoke()
                inside = True
        elif name == "invoke":
            if not inside:
                continue
            flush_param(match.start())
            flush_invoke()
            if not closing:
                invoke = {"name": str(attrs.get("name") or "").strip(), "args": {}}
        elif name == "parameter":
            if not inside or invoke is None:
                continue
            flush_param(match.start())
            if not closing:
                param = {
                    "name": str(attrs.get("name") or "").strip(),
                    "attrs": attrs,
                    "start": match.end(),
                }
    if inside:
        # 块没有闭合：块尾一律丢弃，宁可少说话也不让 DSML 文本漏出去
        logger.warning("DSML calls 块没有闭合，块尾文本已丢弃")
        flush_param(len(text))
        flush_invoke()
    else:
        kept.append(text[cursor:])
    return calls, "".join(kept)


def strip_dsml(text: str) -> str:
    """剥除回复里的任何 DSML 片段（含残缺/畸形块），出站前的最后一道闸。

    没有 DSML 痕迹时原样返回；整条只有畸形 DSML 块时返回空串，交给调用方
    走沉默/空回复路径。不抛异常。
    """
    if not text or not _DSML_HINT_RE.search(text):
        return text
    _calls, cleaned = parse_dsml_calls(text)
    # 块外孤立的完整标签（如拆条后的 ``</... parameter>``）
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    if _DSML_HINT_RE.search(cleaned):
        # 残缺标签没法可靠判断边界：含 DSML 字样的整行丢弃，其余行照常保留
        logger.warning("出站文本里有不完整的 DSML 残片，含残片的行已丢弃")
        cleaned = "\n".join(
            line for line in cleaned.splitlines() if not _DSML_HINT_RE.search(line)
        )
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_dsml_message(message: dict[str, Any]) -> dict[str, Any]:
    """把模型响应 message 里的 DSML 文本调用合并进结构化 ``tool_calls``。

    - 解析出的调用追加到已有 ``tool_calls`` 后面（ID 用 ``dsml_call_N``）；
    - ``content`` 只保留块外正常文本；
    - 没有可解析的调用但存在 DSML 痕迹：只剥除并记 warning，不抛异常。
    """
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, str) or not _DSML_HINT_RE.search(content):
        return message
    calls, cleaned = parse_dsml_calls(content)
    cleaned = strip_dsml(cleaned) if _DSML_HINT_RE.search(cleaned) else cleaned
    updated = dict(message)
    updated["content"] = cleaned
    if calls:
        existing = list(message.get("tool_calls") or [])
        updated["tool_calls"] = [*existing, *calls]
        logger.info("从 content 中解析出 %s 个 DSML 工具调用", len(calls))
    else:
        logger.warning("content 里有 DSML 片段但没有可用调用，已剥除")
    return updated
