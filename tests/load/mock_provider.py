# SPDX-License-Identifier: GPL-3.0-or-later
"""确定性 Mock Provider（主文档 §5.3 / architecture.md §5 / task-list.md T3·T7）。

与真实 :class:`~app.inference.provider.ProviderAdapter` **同一业务接口**：
``async def answer(request: ModelRequest) -> ModelResponse``。

**确定性可复现**：故障仅由 ``(persona_id, attempt_no)`` 决定，与调用顺序/并发无关；
所有随机性以 ``seed`` + ``(persona_id, attempt_no)`` 派生，**不使用全局 random**。
（哈希用 :func:`stable_hash`（SHA-256）而非内置 ``hash()``——内置哈希受 ``PYTHONHASHSEED``
影响、跨进程不确定，会破坏可复现性。）

> **红线 #1 边界（R2）**：本文件按 T7 要求**确定性地制造**非法输出 / 429 / 5xx，
> 属**正常故障模拟**，**不属于**红线 #1。红线 #1 禁止的是**平台**消化无效输出后
> 补默认答案。本 mock **不产出任何默认答案**——非法输出只是非法原文，
> 其有效性一律由平台 :func:`app.inference.validation.parse_and_validate` 判定。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.contracts import (
    ERROR_AUTH,
    ERROR_PROTOCOL,
    ERROR_RATE_LIMITED,
    ERROR_SERVER_ERROR,
    ERROR_TIMEOUT,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    ModelRequest,
    ModelResponse,
    ProviderError,
)

#: 故障计划（architecture.md §5.2）。
FAULT_PLAN_NONE = "none"
FAULT_PLAN_RECOVERABLE = "recoverable"
FAULT_PLAN_PERMANENT = "permanent"
FAULT_PLANS: frozenset[str] = frozenset(
    {FAULT_PLAN_NONE, FAULT_PLAN_RECOVERABLE, FAULT_PLAN_PERMANENT}
)

#: 错误码 → 模拟 HTTP 状态（仅供 mock 记录使用）。
_HTTP_STATUS_FOR_ERROR: dict[str, int] = {
    ERROR_TIMEOUT: 0,
    ERROR_RATE_LIMITED: 429,
    ERROR_AUTH: 401,
    ERROR_SERVER_ERROR: 503,
    ERROR_PROTOCOL: 400,
}


def stable_hash(value: str) -> int:
    """跨进程稳定的字符串哈希（SHA-256 前 16 hex）。"""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def valid_value_for(persona_id: str) -> str:
    """由 persona_id **确定性**映射到五档之一（分布可预期）。"""
    return OPTION_VALUES[stable_hash(persona_id) % len(OPTION_VALUES)]


def extract_persona_id(request: ModelRequest) -> str:
    """从 ``user_message`` 反解当前 persona_id（只含当前 persona）。"""
    payload = json.loads(request.user_message)
    snapshot = payload["persona_snapshot"]
    return str(snapshot["persona_id"])


@dataclass(frozen=True)
class MockCall:
    """一次 mock 调用的记录（供压测后核对「重试成功只产生一个有效答案」等）。"""

    persona_id: str
    attempt_no: int
    status: str  # ok | error | invalid
    error_code: str | None
    value: str | None
    raw_text: str | None
    at: datetime


class MockCallLog:
    """mock 调用日志（记录 ``(persona_id, attempt_no, status, error_code, ts)``）。"""

    def __init__(self) -> None:
        self.records: list[MockCall] = []

    def add(self, call: MockCall) -> None:
        self.records.append(call)

    def calls_for(self, persona_id: str) -> list[MockCall]:
        return [call for call in self.records if call.persona_id == persona_id]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.records:
            counts[call.status] = counts.get(call.status, 0) + 1
        return counts

    def error_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.records:
            if call.error_code:
                counts[call.error_code] = counts.get(call.error_code, 0) + 1
        return counts

    @property
    def total(self) -> int:
        return len(self.records)


# 非法输出的确定性变体（各按 hash(persona_id) % 5 选一种）。
INVALID_OUTPUT_VARIANTS: tuple[str, ...] = (
    "这不是 JSON，只是普通文本",  # 非 JSON
    json.dumps({"value": "definitely_yes"}, ensure_ascii=False),  # 缺 question_id
    json.dumps(
        {"question_id": "monthly_income", "value": "definitely_yes"}, ensure_ascii=False
    ),  # 错误 question_id
    json.dumps(
        {"question_id": PURCHASE_INTENT_QUESTION_ID, "value": "maybe_yes"}, ensure_ascii=False
    ),  # 非法 value
    json.dumps(
        {
            "question_id": PURCHASE_INTENT_QUESTION_ID,
            "value": "unsure",
            "confidence": 0.9,
        },
        ensure_ascii=False,
    ),  # 额外字段
)


class MockProvider:
    """确定性故障注入 mock provider。"""

    provider_name = "mock"

    def __init__(
        self,
        *,
        fixed_latency_s: float = 0.2,
        fault_plan: str = FAULT_PLAN_NONE,
        seed: int = 0,
        timeout_personas: Iterable[str] = frozenset(),
        fail_personas: Iterable[str] = frozenset(),
        invalid_personas: Iterable[str] = frozenset(),
        timeout_latency_s: float = 0.05,
        usage_mode: str = "present",
        input_tokens: int = 32,
        output_tokens: int = 8,
        forced_error_code: str | None = None,
    ) -> None:
        if fault_plan not in FAULT_PLANS:
            raise ValueError(f"unknown fault_plan: {fault_plan!r}")
        if usage_mode not in ("present", "missing"):
            raise ValueError(f"unknown usage_mode: {usage_mode!r}")
        self.fixed_latency_s = fixed_latency_s
        self.fault_plan = fault_plan
        self.seed = seed
        self.timeout_personas = set(timeout_personas)
        self.fail_personas = set(fail_personas)
        self.invalid_personas = set(invalid_personas)
        self.timeout_latency_s = timeout_latency_s
        self.usage_mode = usage_mode
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        #: 强制所有成员恒返回该 provider 错误（用于「连续同类失败」等场景）。
        self.forced_error_code = forced_error_code

        self._attempts: dict[str, int] = {}
        self.log = MockCallLog()

    # -- 接口 -------------------------------------------------------------

    async def answer(self, request: ModelRequest) -> ModelResponse:
        """执行一次 mock 调用（故障由 ``(persona_id, attempt_no)`` 确定）。"""
        persona_id = extract_persona_id(request)
        attempt_no = self._next_attempt(persona_id)

        await self._gate(persona_id, attempt_no)

        if persona_id in self.timeout_personas:
            await asyncio.sleep(self.timeout_latency_s)
            return self._record(
                persona_id,
                attempt_no,
                ModelResponse(
                    raw_text="",
                    duration_ms=0,
                    error=ProviderError(
                        error_code=ERROR_TIMEOUT, message="mock connection timeout"
                    ),
                ),
                status="error",
                error_code=ERROR_TIMEOUT,
            )

        await asyncio.sleep(self.fixed_latency_s)
        return self._build_response(persona_id, attempt_no)

    # -- 故障构造 ---------------------------------------------------------

    def _build_response(self, persona_id: str, attempt_no: int) -> ModelResponse:
        plan_error = self._planned_error(persona_id, attempt_no)
        if plan_error is not None:
            return self._record(
                persona_id,
                attempt_no,
                ModelResponse(raw_text="", duration_ms=0, error=plan_error),
                status="error",
                error_code=plan_error.error_code,
            )

        raw_text = self._planned_text(persona_id, attempt_no)
        is_valid = self._is_valid_text(raw_text)
        response = ModelResponse(
            raw_text=raw_text,
            input_tokens=self._input_tokens if self.usage_mode == "present" else None,
            output_tokens=self._output_tokens if self.usage_mode == "present" else None,
            provider_request_id=f"mock-{persona_id}-{attempt_no}",
            finish_reason="stop",
            duration_ms=max(0, int(self.fixed_latency_s * 1000)),
        )
        value: str | None = None
        if is_valid:
            value = json.loads(raw_text)["value"]
        return self._record(
            persona_id,
            attempt_no,
            response,
            status="ok" if is_valid else "invalid",
            error_code=None if is_valid else "INVALID_OUTPUT",
            value=value,
            raw_text=raw_text,
        )

    def _planned_error(self, persona_id: str, attempt_no: int) -> ProviderError | None:
        bucket = stable_hash(persona_id) % 2

        if self.forced_error_code is not None:
            return ProviderError(
                error_code=self.forced_error_code,
                message=f"mock forced {self.forced_error_code}",
                http_status=_HTTP_STATUS_FOR_ERROR.get(self.forced_error_code, 500),
            )

        if persona_id in self.fail_personas:
            # 永久错误：模型不存在 / 请求协议错误（HTTP 400）。
            return ProviderError(
                error_code=ERROR_PROTOCOL,
                message="mock model not found",
                http_status=400,
            )

        if self.fault_plan == FAULT_PLAN_RECOVERABLE and attempt_no == 1:
            if bucket == 0:
                return ProviderError(
                    error_code=ERROR_RATE_LIMITED,
                    message="mock rate limited",
                    http_status=429,
                    retry_after_seconds=1.0,
                )
            return ProviderError(
                error_code=ERROR_SERVER_ERROR,
                message="mock server error",
                http_status=503,
            )

        if self.fault_plan == FAULT_PLAN_PERMANENT and persona_id in self.fail_personas:
            return ProviderError(
                error_code=ERROR_PROTOCOL, message="mock permanent error", http_status=400
            )
        return None

    def _planned_text(self, persona_id: str, attempt_no: int) -> str:
        if persona_id in self.invalid_personas:
            return self._invalid_variant_for(persona_id)

        if self.fault_plan == FAULT_PLAN_RECOVERABLE and attempt_no == 2:
            return self._invalid_variant_for(persona_id)

        return json.dumps(
            {
                "question_id": PURCHASE_INTENT_QUESTION_ID,
                "value": valid_value_for(persona_id),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _invalid_variant_for(persona_id: str) -> str:
        index = stable_hash(persona_id) % len(INVALID_OUTPUT_VARIANTS)
        return INVALID_OUTPUT_VARIANTS[index]

    @staticmethod
    def _is_valid_text(raw_text: str) -> bool:
        """仅用于**记录**本次输出是否形似合法答案（不作为平台判定）。"""
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        if set(payload) - {"question_id", "value"}:
            return False
        return (
            payload.get("question_id") == PURCHASE_INTENT_QUESTION_ID
            and payload.get("value") in OPTION_VALUES
        )

    # -- 辅助 -------------------------------------------------------------

    def _next_attempt(self, persona_id: str) -> int:
        attempt_no = self._attempts.get(persona_id, 0) + 1
        self._attempts[persona_id] = attempt_no
        return attempt_no

    async def _gate(self, persona_id: str, attempt_no: int) -> None:
        """并发闸门（基类：无操作；子类可覆写以观测/阻塞在途）。"""
        return None

    def _record(
        self,
        persona_id: str,
        attempt_no: int,
        response: ModelResponse,
        *,
        status: str,
        error_code: str | None,
        value: str | None = None,
        raw_text: str | None = None,
    ) -> ModelResponse:
        self.log.add(
            MockCall(
                persona_id=persona_id,
                attempt_no=attempt_no,
                status=status,
                error_code=error_code,
                value=value,
                raw_text=raw_text,
                at=datetime.now(UTC),
            )
        )
        return response

    def attempts_for(self, persona_id: str) -> int:
        return self._attempts.get(persona_id, 0)


class BarrierMockProvider(MockProvider):
    """带并发闸门的 mock：断言「恰有 N 个在途、峰值恒 ≤ N、释放后滚动补位」。

    - 进入 ``answer`` 时 ``current += 1``、``peak = max(peak, current)``；
      到达 ``target`` 置 "reached"。
    - 每个请求等待自己的释放事件；测试用 :meth:`release` 放行若干个，
      用 :meth:`open_gate` 全部放行并关闭闸门。
    """

    def __init__(self, *, target: int = 100, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if target < 1:
            raise ValueError("target must be >= 1")
        self._target = target
        self._waiters: list[asyncio.Event] = []
        self._lock = asyncio.Lock()
        self._current = 0
        self._peak = 0
        self._started = 0
        self._reached = asyncio.Event()
        self._open = False

    @property
    def target(self) -> int:
        return self._target

    @property
    def current(self) -> int:
        return self._current

    @property
    def peak(self) -> int:
        return self._peak

    @property
    def started(self) -> int:
        return self._started

    @property
    def is_open(self) -> bool:
        return self._open

    async def _gate(self, persona_id: str, attempt_no: int) -> None:
        async with self._lock:
            if self._open:
                return
            event = asyncio.Event()
            self._waiters.append(event)
            self._current += 1
            self._started += 1
            self._peak = max(self._peak, self._current)
            if self._current >= self._target:
                self._reached.set()
        await event.wait()
        async with self._lock:
            self._current -= 1

    async def wait_reached(self, timeout: float = 10.0) -> bool:
        """等待在途数达到 ``target``；超时返回 ``False``。"""
        try:
            await asyncio.wait_for(self._reached.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def wait_until(
        self, predicate: Any, timeout: float = 10.0, interval: float = 0.005
    ) -> bool:
        """轮询等待谓词成立（用于观测“滚动补位”：current 回到 target）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self):
                return True
            await asyncio.sleep(interval)
        return bool(predicate(self))

    async def release(self, n: int = 1) -> int:
        """放行最早的 ``n`` 个等待者；返回实际放行数。"""
        released = 0
        async with self._lock:
            while released < n and self._waiters:
                self._waiters.pop(0).set()
                released += 1
        return released

    async def open_gate(self) -> None:
        """关闭闸门：放行所有等待者，后续请求不再被阻塞。"""
        async with self._lock:
            self._open = True
            for event in self._waiters:
                event.set()
            self._waiters.clear()


__all__ = [
    "FAULT_PLAN_NONE",
    "FAULT_PLAN_PERMANENT",
    "FAULT_PLAN_RECOVERABLE",
    "FAULT_PLANS",
    "INVALID_OUTPUT_VARIANTS",
    "BarrierMockProvider",
    "MockCall",
    "MockCallLog",
    "MockProvider",
    "extract_persona_id",
    "stable_hash",
    "valid_value_for",
]
