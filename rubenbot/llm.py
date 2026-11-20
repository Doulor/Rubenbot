"""OpenAI 兼容的 LLM 客户端。

只实现 chat/completions 这一条路径，够用且不引入额外依赖。
``complete(role=...)`` 里的 role 决定使用哪个模型（chat / extract / consolidate，
以及 fast / summary / vision 等预置角色），回退规则见 :class:`LLMConfig.model_for`。
``task=`` 是给用量统计用的标签，默认取 role；插件可传自定义名字。

可选的 ``usage_recorder`` 在每次调用尝试后回调一条用量记录；上游没给 usage
时 token 记 ``None``（落库为 NULL），不猜不编。记录失败只记 debug，不影响主流程。
测试里可以用任意实现了 ``complete`` 的假对象替换。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
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
        用量（失败记 ok=0，token 为 NULL）。
        """
        model = self.config.model_for(role)
        task_name = (task or role or "chat").strip() or "chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": (
                self.config.chat_temperature
                if role == "chat" and temperature is None and self.config.chat_temperature is not None
                else self.config.temperature if temperature is None else temperature
            ),
        }
        if role == "chat" and self.config.chat_frequency_penalty:
            payload["frequency_penalty"] = self.config.chat_frequency_penalty
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = await client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                # 网络/超时层失败：这次尝试没有响应，token 记 NULL
                await self._record_usage(task_name, model, None, started, ok=False)
                last_error = exc
            else:
                try:
                    data: Any = resp.json()
                except ValueError:
                    data = None
                if resp.status_code >= 400:
                    # 429/5xx 也可能带 usage 计费信息，能读到就记上
                    await self._record_usage(task_name, model, data, started, ok=False)
                    error = LLMResponseError(
                        f"LLM 返回 {resp.status_code}: {resp.text[:300]}", status=resp.status_code
                    )
                    if not is_retryable_status(resp.status_code):
                        # 401/404 这类配置错误重试也没用，直接失败
                        raise error
                    last_error = error
                else:
                    choices = (data.get("choices") if isinstance(data, dict) else None) or []
                    if not choices:
                        await self._record_usage(task_name, model, data, started, ok=False)
                        last_error = LLMError(f"LLM 响应缺少 choices: {str(data)[:300]}")
                    else:
                        content = choices[0].get("message", {}).get("content") or ""
                        await self._record_usage(task_name, model, data, started, ok=True)
                        return content
            if attempt >= self.config.max_retries:
                break
            delay = retry_delay(attempt, self._retry_delays)
            logger.warning(
                "LLM 调用失败（第 %s 次），%.1fs 后重试：%s", attempt + 1, delay, last_error
            )
            await asyncio.sleep(delay)
        raise LLMError(f"LLM 调用最终失败: {last_error}") from last_error

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
        与 ``complete`` 一样按 ``task``（默认取 role）记一条用量。
        """
        if getattr(self, "_tools_unsupported", False):
            raise ToolUnsupported("已探测到当前模型不支持工具调用")
        model = self.config.model_for(role)
        task_name = (task or role or "chat").strip() or "chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        client = await self._get_client()
        started = time.perf_counter()
        try:
            resp = await client.post("/chat/completions", json=payload)
        except httpx.HTTPError:
            await self._record_usage(task_name, model, None, started, ok=False)
            raise
        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            # 400/422 且错误里提到 tools/tool 时视为不支持，标记后走退化路径
            await self._record_usage(task_name, model, data, started, ok=False)
            text = resp.text[:300]
            lowered = text.lower()
            if resp.status_code in (400, 404, 422, 501) and (
                "tool" in lowered or "function" in lowered or "unsupported" in lowered
            ):
                self._tools_unsupported = True
                raise ToolUnsupported(f"模型不支持工具调用：{text}")
            raise LLMError(f"LLM 返回 {resp.status_code}: {text}")
        choices = (data.get("choices") if isinstance(data, dict) else None) or []
        if not choices:
            await self._record_usage(task_name, model, data, started, ok=False)
            raise LLMError(f"LLM 响应缺少 choices: {str(data)[:300]}")
        await self._record_usage(task_name, model, data, started, ok=True)
        return choices[0].get("message") or {}

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
