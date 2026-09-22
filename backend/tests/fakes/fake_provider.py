# SPDX-License-Identifier: GPL-3.0-or-later
"""可配置的 fake provider（同真实 :class:`ProviderAdapter` 接口）。

用途：T2 单 persona 作答闭环测试（合法 JSON / 围栏 JSON / 缺题 / 未知 value /
额外字段 / 非 JSON / 429 / 401 / 超时 / usage 缺失）。

> 说明：fake provider 只模拟**模型侧的原始输出**（含非法输出与错误），
> 它**不**决定答案是否有效 —— 有效性一律由平台
> :func:`app.inference.validation.parse_and_validate` 判定。
> 因此本文件不含任何「补默认答案」逻辑（红线 #1）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.contracts import ModelRequest, ModelResponse, ProviderError

Responder = Callable[[ModelRequest, int], ModelResponse]


@dataclass(frozen=True)
class FakeCall:
    """一次 fake 调用的记录。"""

    index: int
    request: ModelRequest
    response: ModelResponse


class FakeProvider:
    """按预设脚本返回 :class:`ModelResponse` 的 provider。

    - ``raw_texts``：按调用顺序依次返回的原文（用尽后重复最后一个）。
    - ``error``：始终返回该错误（与 ``raw_texts`` 互斥，error 优先）。
    - ``responder``：完全自定义（优先级最高），签名 ``(request, index) -> ModelResponse``。
    """

    provider_name = "fake"

    def __init__(
        self,
        raw_texts: Sequence[str] | None = None,
        *,
        error: ProviderError | None = None,
        responder: Responder | None = None,
        input_tokens: int | None = 10,
        output_tokens: int | None = 5,
        provider_request_id: str | None = "fake-req-1",
        finish_reason: str | None = "stop",
        duration_ms: int = 1,
    ) -> None:
        self._raw_texts: list[str] = list(raw_texts) if raw_texts is not None else []
        self._error = error
        self._responder = responder
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._provider_request_id = provider_request_id
        self._finish_reason = finish_reason
        self._duration_ms = duration_ms

        self.calls: list[tuple[ModelRequest, ModelResponse]] = []
        self.requests: list[ModelRequest] = []

    # -- 便捷设置 ---------------------------------------------------------

    def set_raw_text(self, raw_text: str) -> FakeProvider:
        self._raw_texts = [raw_text]
        self._error = None
        self._responder = None
        return self

    def set_raw_texts(self, raw_texts: Sequence[str]) -> FakeProvider:
        self._raw_texts = list(raw_texts)
        self._error = None
        self._responder = None
        return self

    def set_error(self, error: ProviderError) -> FakeProvider:
        self._error = error
        self._raw_texts = []
        self._responder = None
        return self

    # -- 接口 -------------------------------------------------------------

    async def answer(self, request: ModelRequest) -> ModelResponse:
        index = len(self.calls)
        self.requests.append(request)

        if self._responder is not None:
            response = self._responder(request, index)
        elif self._error is not None:
            response = ModelResponse(
                raw_text="", duration_ms=self._duration_ms, error=self._error
            )
        else:
            if not self._raw_texts:
                raise AssertionError("FakeProvider has no scripted response configured")
            raw_text = (
                self._raw_texts[index]
                if index < len(self._raw_texts)
                else self._raw_texts[-1]
            )
            response = ModelResponse(
                raw_text=raw_text,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                provider_request_id=self._provider_request_id,
                finish_reason=self._finish_reason,
                duration_ms=self._duration_ms,
            )

        self.calls.append((request, response))
        return response


__all__ = ["FakeCall", "FakeProvider", "Responder"]
