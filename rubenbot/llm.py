"""OpenAI 兼容的 LLM 客户端。

只实现 chat/completions 这一条路径，够用且不引入额外依赖。
``complete(role=...)`` 里的 role 决定使用哪个模型：chat / extract / consolidate。
测试里可以用任意实现了 ``complete`` 的假对象替换。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

import httpx

from .config import LLMConfig

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMProtocol(Protocol):
    """主循环只依赖这个最小接口，方便 mock。"""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str = "chat",
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str: ...


class LLMError(RuntimeError):
    """LLM 调用失败。"""


class ToolUnsupported(LLMError):
    """当前中转/模型不支持 OpenAI 工具调用（拿它当正常退化信号，不当异常上报）。"""


class LLMClient:
    """OpenAI 兼容的异步客户端。"""

    def __init__(self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
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
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """调用 chat/completions，返回第一条回复的文本内容。"""
        payload: dict[str, Any] = {
            "model": self.config.model_for(role),
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = await client.post("/chat/completions", json=payload)
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"LLM 响应缺少 choices: {str(data)[:300]}")
                content = choices[0].get("message", {}).get("content") or ""
                return content
            except (httpx.HTTPError, LLMError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    delay = 0.5 * (2**attempt)
                    logger.warning("LLM 调用失败（第 %s 次），%.1fs 后重试：%s", attempt + 1, delay, exc)
                    await asyncio.sleep(delay)
        raise LLMError(f"LLM 调用最终失败: {last_error}") from last_error

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        role: str = "chat",
        temperature: float | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """单次工具调用请求，返回原始 ``choices[0].message``（可能带 tool_calls）。

        只做一次往返，工具循环由调用方控制（core 里最多两轮）。中转或模型不
        支持工具调用时抛 :class:`ToolUnsupported`，由调用方退化为普通生成。
        """
        if getattr(self, "_tools_unsupported", False):
            raise ToolUnsupported("已探测到当前模型不支持工具调用")
        payload: dict[str, Any] = {
            "model": self.config.model_for(role),
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        client = await self._get_client()
        resp = await client.post("/chat/completions", json=payload)
        if resp.status_code >= 400:
            text = resp.text[:300]
            # 400/422 且错误里提到 tools/tool 时视为不支持，标记后走退化路径
            lowered = text.lower()
            if resp.status_code in (400, 404, 422, 501) and (
                "tool" in lowered or "function" in lowered or "unsupported" in lowered
            ):
                self._tools_unsupported = True
                raise ToolUnsupported(f"模型不支持工具调用：{text}")
            raise LLMError(f"LLM 返回 {resp.status_code}: {text}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 响应缺少 choices: {str(data)[:300]}")
        return choices[0].get("message") or {}


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
