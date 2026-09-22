# SPDX-License-Identifier: GPL-3.0-or-later
"""限流与预算（主文档 §7 / architecture.md §6.5）。

三类约束**同时**生效：
1. **并发槽位**：全局 100 个异步槽位（初次调用与重试合计在途 ≤ 100）。
2. **RPM/TPM**：**平滑发出**（避免分钟边界突发）；进程内滑动窗口；
   worker 重启**必须**依据 ``attempts`` 最近 60 秒记录**保守重建**窗口（裁决 C3）。
3. **预算预留**：发请求前在领取事务内预留费用上界；有 usage 后结算释放；
   未知计费（超时/崩溃）**保留预留**，不按 0 释放。

金额一律 :class:`decimal.Decimal`；``unknown`` 不写成 ``0``。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.contracts import ModelRequest, PersonaSnapshot, SurveyInput
from app.inference.prompt import build_model_request

#: 全局异步槽位（并发上限）默认值（task-list.md §8.1）。
DEFAULT_CONCURRENCY = 100

#: 限流滑动窗口（秒）。重启重建窗口的观测区间即最近 60 秒。
RATE_WINDOW_SECONDS = 60.0

#: 每百万 input/output token 的价格换算基数。
_MILLION = Decimal(1_000_000)

#: 保守字符→token 估计系数：1 字符 ≈ 1 token 是**上界**（宁多估不少估）。
CONSERVATIVE_CHARS_PER_TOKEN = 1


# ---------------------------------------------------------------------------
# 并发槽位
# ---------------------------------------------------------------------------


class ConcurrencyLimiter:
    """全局并发槽位计数器（``capacity`` 个异步槽位）。

    - 初次调用与重试走同一实例，故二者合计在途数 ≤ ``capacity``。
    - ``in_flight`` / ``peak_in_flight`` 供运行详情页与测试观测。
    """

    def __init__(self, capacity: int = DEFAULT_CONCURRENCY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self._in_flight = 0
        self._peak_in_flight = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        return self._peak_in_flight

    @property
    def free_slots(self) -> int:
        return self._capacity - self._in_flight

    def reset_peak(self) -> None:
        self._peak_in_flight = self._in_flight

    async def acquire(self) -> None:
        await self._semaphore.acquire()
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    def release(self) -> None:
        if self._in_flight <= 0:
            raise RuntimeError("release() called with no slot held")
        self._in_flight -= 1
        self._semaphore.release()

    def slot(self) -> _Slot:
        """``async with limiter.slot():`` 上下文管理器（自动 acquire/release）。"""
        return _Slot(self)


class _Slot:
    """``ConcurrencyLimiter`` 的 async 上下文管理器。"""

    __slots__ = ("_limiter",)

    def __init__(self, limiter: ConcurrencyLimiter) -> None:
        self._limiter = limiter

    async def __aenter__(self) -> None:
        await self._limiter.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        self._limiter.release()


# ---------------------------------------------------------------------------
# 平滑限流（RPM / TPM）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateRecord:
    """一次已发出请求的观测记录（wall-clock 时间 + 计入配额的 tokens）。"""

    at: datetime
    tokens: int


class _MonoRecord:
    """内部记录：单调时钟时间 + tokens（``mono`` 供窗口裁剪）。"""

    __slots__ = ("mono", "tokens")

    def __init__(self, mono: float, tokens: int) -> None:
        self.mono = mono
        self.tokens = tokens


class SmoothRateLimiter:
    """平滑 RPM/TPM 限流器（滑动窗口 + 最小间隔）。

    - **平滑发出**：相邻两次发出至少间隔 ``window/rpm`` 秒，避免分钟边界突发。
    - **滑动窗口**：窗口内请求数 ≤ ``rpm``、窗口内 tokens ≤ ``tpm``。
    - 时钟可注入（测试用假时钟）；默认 :func:`time.monotonic`。
    """

    def __init__(
        self,
        *,
        rpm: int,
        tpm: int,
        window_seconds: float = RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if rpm < 1:
            raise ValueError("rpm must be >= 1")
        if tpm < 1:
            raise ValueError("tpm must be >= 1")
        self._rpm = rpm
        self._tpm = tpm
        self._window = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._events: deque[_MonoRecord] = deque()
        self._next_allowed: float = 0.0

    @property
    def rpm(self) -> int:
        return self._rpm

    @property
    def tpm(self) -> int:
        return self._tpm

    @property
    def min_interval(self) -> float:
        """平滑发出的最小间隔（秒）。"""
        return self._window / self._rpm

    @property
    def in_window_requests(self) -> int:
        self._evict(self._clock())
        return len(self._events)

    @property
    def in_window_tokens(self) -> int:
        self._evict(self._clock())
        return sum(record.tokens for record in self._events)

    # -- 内部 -------------------------------------------------------------

    def _evict(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self._window
        while self._events and self._events[0].mono < cutoff:
            self._events.popleft()

    def _wait_seconds(self, now_monotonic: float, tokens: int) -> float:
        """返回需要等待的秒数；<=0 表示可立即发出。"""
        self._evict(now_monotonic)
        wait = 0.0

        if now_monotonic < self._next_allowed:
            wait = max(wait, self._next_allowed - now_monotonic)

        if len(self._events) >= self._rpm and self._events:
            wait = max(wait, self._events[0].mono + self._window - now_monotonic)

        window_tokens = sum(record.tokens for record in self._events)
        if window_tokens + tokens > self._tpm and self._events:
            running = 0
            for record in self._events:
                running += record.tokens
                if window_tokens + tokens - running <= self._tpm:
                    wait = max(wait, record.mono + self._window - now_monotonic)
                    break
        return wait

    # -- 公开接口 ---------------------------------------------------------

    async def acquire(self, tokens: int, *, timeout: float | None = None) -> None:
        """等待到可发出为止（平滑 + 窗口约束），随后登记本次发出。"""
        start = self._clock()
        while True:
            now_monotonic = self._clock()
            wait = self._wait_seconds(now_monotonic, tokens)
            if wait <= 0:
                break
            if timeout is not None and now_monotonic - start + wait > timeout:
                raise TimeoutError("rate limiter timed out waiting for capacity")
            await self._sleep(wait)

        now_monotonic = self._clock()
        self._events.append(_MonoRecord(now_monotonic, max(0, int(tokens))))
        self._next_allowed = max(now_monotonic, self._next_allowed) + self.min_interval

    def rebuild_from_history(self, records: Sequence[RateRecord | tuple[datetime, int]]) -> int:
        """依据 ``attempts`` 最近窗口记录**保守重建**滑动窗口（裁决 C3）。

        - 每条历史记录都计入窗口（请求数与 tokens），避免“重启即清空限额”。
        - 历史 wall-clock 时间换算到单调时钟坐标系，仅保留窗口内的记录。
        - 返回实际纳入窗口的记录数。
        - **幂等（P2-3 修复）**：调用前先重置 ``self._events``，重复调用同一批历史记录
          得到相同窗口（否则 ``start()`` 二次触发会 2→4→6 重复累加）。
        """
        now_wall = datetime.now(UTC)
        now_mono = self._clock()
        added = 0
        # 先重置既有窗口，再纳入历史 —— 保证幂等（不得基于旧 ``_events`` 追加）。
        merged: list[_MonoRecord] = []
        for item in records:
            if isinstance(item, RateRecord):
                at, tokens = item.at, item.tokens
            else:
                at, tokens = item
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            age = (now_wall - at).total_seconds()
            if age < 0 or age > self._window:
                continue
            merged.append(_MonoRecord(now_mono - age, max(0, int(tokens))))
            added += 1
        merged.sort(key=lambda record: record.mono)
        self._events = deque(merged)
        return added


# ---------------------------------------------------------------------------
# 预算/费用
# ---------------------------------------------------------------------------


def estimate_input_tokens(*texts: str) -> int:
    """保守估计输入 tokens 上界（无真实 tokenizer 时用字符数上界）。"""
    total_chars = sum(len(text or "") for text in texts)
    return max(1, total_chars // CONSERVATIVE_CHARS_PER_TOKEN)


def compute_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    input_price_per_million: Decimal | None,
    output_price_per_million: Decimal | None,
) -> Decimal | None:
    """按单价计算费用；任一价格或 token 数缺失 → 返回 ``None``（= 未知，不写 0）。"""
    if input_price_per_million is None or output_price_per_million is None:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    cost = (
        Decimal(int(input_tokens)) * Decimal(input_price_per_million)
        + Decimal(int(output_tokens)) * Decimal(output_price_per_million)
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))


def estimate_request_reservation(
    *,
    input_tokens_estimate: int,
    max_output_tokens: int,
    input_price_per_million: Decimal | None,
    output_price_per_million: Decimal | None,
) -> Decimal:
    """本次请求的费用上界预留（输入保守估计 + max_output_tokens + 全部计费类别）。

    未配置单价时金额上界无法计算 → 返回 ``0``（此时由 ``request_limit`` 提供保护，
    主文档 §7：无价格时费用显示“未知”，提供请求次数/token 上限保护）。
    """
    cost = compute_cost(
        input_tokens=input_tokens_estimate,
        output_tokens=max_output_tokens,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
    )
    if cost is None:
        return Decimal("0")
    return cost


def reservation_for_request(
    request: ModelRequest,
    *,
    input_price_per_million: Decimal | None,
    output_price_per_million: Decimal | None,
) -> Decimal:
    """由 :class:`ModelRequest` 计算预留上界。"""
    input_estimate = estimate_input_tokens(request.system_prompt, request.user_message)
    return estimate_request_reservation(
        input_tokens_estimate=input_estimate,
        max_output_tokens=request.max_output_tokens,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
    )


def build_request_for_persona(
    *,
    survey: SurveyInput,
    persona_snapshot: dict[str, Any],
    model: str,
    provider: str = "openai_compatible",
    max_output_tokens: int = 256,
    timeout_seconds: float = 60.0,
    allow_reason: bool = False,
) -> ModelRequest:
    """由冻结问卷快照 + 单 persona 快照构造 :class:`ModelRequest`（只含当前 persona）。"""
    persona = PersonaSnapshot.model_validate(persona_snapshot)
    return build_model_request(
        survey=survey,
        persona=persona,
        model=model,
        provider=provider,
        allow_reason=allow_reason,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "CONSERVATIVE_CHARS_PER_TOKEN",
    "DEFAULT_CONCURRENCY",
    "RATE_WINDOW_SECONDS",
    "ConcurrencyLimiter",
    "RateRecord",
    "SmoothRateLimiter",
    "build_request_for_persona",
    "compute_cost",
    "estimate_input_tokens",
    "estimate_request_reservation",
    "reservation_for_request",
]
