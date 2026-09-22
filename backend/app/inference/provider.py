# SPDX-License-Identifier: GPL-3.0-or-later
"""第三方模型异步适配器（主文档 §5.3 / architecture.md §7）。

统一业务接口：``async def answer(request: ModelRequest) -> ModelResponse``。

要点：
- 使用 :class:`httpx.AsyncClient`；**显式关闭 SDK/HTTP 层的不透明重试**
  （``httpx.AsyncHTTPTransport(retries=0)``），重试统一由 worker 负责。
- 错误分类：timeout / 429 / 401 / 403 / 5xx / 协议错误 —— 返回在
  :class:`~app.contracts.ModelResponse` 的 ``error`` 字段，**绝不**据此补默认答案。
- ``input_tokens`` / ``output_tokens`` / ``provider_request_id`` 允许为空（空 ≠ 0）。
- 本机无真实第三方 API key：真实调用未测，T0–T6 走 mock provider（同接口）。
- :func:`build_provider` 按 ``MODEL_PROVIDER`` 选择：``"mock"`` → 运行时
  :class:`~app.inference.mock_provider.MockProvider`（确定性、不发起真实调用）；
  其它 → :class:`OpenAICompatibleProvider`（裁决 N7-b / §7.5 T6-d）。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import Settings, get_settings
from app.contracts import (
    ERROR_AUTH,
    ERROR_PROTOCOL,
    ERROR_RATE_LIMITED,
    ERROR_SERVER_ERROR,
    ERROR_TIMEOUT,
    ModelRequest,
    ModelResponse,
    ProviderError,
)
from app.inference.mock_provider import MockProvider

#: 模型响应原文保留上限（防止超长写入日志/库）。
MAX_RAW_OUTPUT_CHARS = 20_000


@runtime_checkable
class ProviderAdapter(Protocol):
    """provider 统一接口（真实适配器与 mock 共用）。"""

    async def answer(self, request: ModelRequest) -> ModelResponse:
        """执行一次模型调用，返回统一响应。"""
        ...


def classify_http_status(status_code: int) -> str:
    """把 HTTP 状态码映射为错误码（主文档 §6.4 / task-list.md §8.1）。"""
    if status_code in (401, 403):
        return ERROR_AUTH
    if status_code == 429:
        return ERROR_RATE_LIMITED
    if 500 <= status_code <= 599:
        return ERROR_SERVER_ERROR
    # 400 / 404 / 422 等：请求协议错误（模型不存在 / 参数非法）。
    return ERROR_PROTOCOL


def parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After`` 头（仅支持秒数形式）。"""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class OpenAICompatibleProvider:
    """OpenAI-compatible 协议适配器（``POST {endpoint}/chat/completions``）。

    ``client`` 可注入（测试用 :class:`httpx.MockTransport` 构造）。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        model: str,
        provider: str = "openai_compatible",
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 256,
        max_connections: int = 100,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._max_connections = max_connections
        self._client = client
        self._owns_client = client is None

    # -- 客户端管理 -------------------------------------------------------

    def _build_client(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self._timeout_seconds),
            # 关键：关闭 HTTP 层不透明重试，重试交由 worker 统一控制。
            transport=httpx.AsyncHTTPTransport(retries=0),
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_connections,
            ),
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        """关闭自建客户端（注入的客户端由调用方负责）。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OpenAICompatibleProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- 主接口 -----------------------------------------------------------

    async def answer(self, request: ModelRequest) -> ModelResponse:
        """调用第三方 API 并返回统一响应（不抛业务异常，错误经 ``error`` 返回）。"""
        client = await self._get_client()
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_message},
            ],
            "max_tokens": request.max_output_tokens or self._max_output_tokens,
        }
        if request.output_schema:
            payload["response_format"] = {"type": "json_object"}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.seed is not None:
            payload["seed"] = request.seed

        url = f"{self._endpoint}/chat/completions"
        started = time.perf_counter()

        try:
            response = await client.post(
                url, json=payload, timeout=request.timeout_seconds
            )
        except httpx.TimeoutException as exc:
            return self._error_response(
                ERROR_TIMEOUT, f"request timed out: {exc}", started
            )
        except httpx.TransportError as exc:
            # 网络层错误（连接失败/读错误）—— 归为可重试的服务端错误。
            return self._error_response(
                ERROR_SERVER_ERROR, f"network error: {exc}", started
            )
        except httpx.HTTPError as exc:  # pragma: no cover - 兜底
            return self._error_response(ERROR_PROTOCOL, f"http error: {exc}", started)

        duration_ms = self._elapsed_ms(started)

        if response.status_code != 200:
            error_code = classify_http_status(response.status_code)
            return ModelResponse(
                raw_text=response.text[:MAX_RAW_OUTPUT_CHARS],
                duration_ms=duration_ms,
                error=ProviderError(
                    error_code=error_code,
                    message=f"provider returned HTTP {response.status_code}",
                    http_status=response.status_code,
                    retry_after_seconds=parse_retry_after(
                        response.headers.get("retry-after")
                    ),
                ),
            )

        try:
            data = response.json()
        except ValueError as exc:
            return self._error_response(
                ERROR_PROTOCOL, f"response is not valid JSON: {exc}", started
            )

        if not isinstance(data, dict):
            return self._error_response(
                ERROR_PROTOCOL, "response JSON is not an object", started
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return self._error_response(
                ERROR_PROTOCOL, "response missing 'choices'", started
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return self._error_response(
                ERROR_PROTOCOL, "response choice is not an object", started
            )
        message = first_choice.get("message") or {}
        raw_text = message.get("content") if isinstance(message, dict) else None
        if raw_text is None:
            # 部分供应商把内容放在 text 字段；仍视为协议不匹配时不猜答案。
            raw_text = first_choice.get("text")
        if raw_text is None:
            return self._error_response(
                ERROR_PROTOCOL, "response missing message content", started
            )

        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}

        return ModelResponse(
            raw_text=str(raw_text)[:MAX_RAW_OUTPUT_CHARS],
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            provider_request_id=data.get("id"),
            finish_reason=first_choice.get("finish_reason"),
            duration_ms=duration_ms,
        )

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    def _error_response(self, error_code: str, message: str, started: float) -> ModelResponse:
        return ModelResponse(
            raw_text="",
            duration_ms=self._elapsed_ms(started),
            error=ProviderError(error_code=error_code, message=message),
        )


def build_provider(settings: Settings | None = None) -> ProviderAdapter:
    """按配置构造 provider（裁决 N7-b / §7.5 T6-d：``MODEL_PROVIDER`` 为唯一选择依据）。

    - ``settings.model_provider == "mock"`` → 运行时 :class:`MockProvider`
      （确定性、恒返回合法答案、**不发起任何真实付费调用**）。
    - 其它取值 → :class:`OpenAICompatibleProvider`（真实第三方适配器）。
      **无 endpoint 时仍可构造**；调用前应由 API 层校验配置（见 §7.9.2）。

    返回类型为 :class:`ProviderAdapter`（真实适配器与 mock **共用**同一业务接口）。
    """
    active = settings or get_settings()
    if active.model_provider == "mock":
        return MockProvider(model=active.model_name)
    return OpenAICompatibleProvider(
        endpoint=active.model_endpoint,
        api_key=active.api_key(),
        model=active.model_name,
        provider=active.model_provider,
        timeout_seconds=active.model_timeout_seconds,
        max_output_tokens=active.model_max_output_tokens,
        max_connections=active.agent_concurrency,
    )


__all__ = [
    "MAX_RAW_OUTPUT_CHARS",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderAdapter",
    "build_provider",
    "classify_http_status",
    "parse_retry_after",
]
