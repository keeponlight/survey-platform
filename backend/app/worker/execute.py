# SPDX-License-Identifier: GPL-3.0-or-later
"""单次领取任务的执行（主文档 §6.3 / §6.4 / §6.6 / §7）。

流程（**关键：绝不持有数据库事务等待模型响应**）：
1. 由冻结问卷快照 + 当前 persona 构造 :class:`ModelRequest`。
2. 限流（RPM/TPM 平滑）→ **事务外** ``await provider.answer(request)``。
3. 分类：成功 / 可重试错误 / 永久错误；**无效输出记 ``INVALID_OUTPUT`` 有限重试，
   绝不填默认答案（红线 #1）**。
4. 结果用 **CAS**（``status='running' AND lease_token=:本次token``）保存；失租 → 丢弃结果。

未知计费映射（主文档 §7）：
- ``TIMEOUT`` / ``SERVER_ERROR`` → **保留预留**（不按 0 释放）。
- ``RATE_LIMITED`` / ``AUTH`` / ``PROTOCOL`` 属供应商**明确拒绝**（未处理/未计费）→ 按 0 结算。
- 无 usage 的成功/无效输出 → 费用未知 → 保留预留。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.contracts import (
    ERROR_AUTH,
    ERROR_INVALID_OUTPUT,
    ERROR_PROTOCOL,
    ERROR_RATE_LIMITED,
    ERROR_SERVER_ERROR,
    ERROR_TIMEOUT,
    ModelResponse,
    SurveyInput,
    ValidatedAnswer,
)
from app.inference.validation import InvalidOutputError, parse_and_validate
from app.models import RunRecord
from app.runs.repository import Claim, RunRepository, run_repository
from app.worker.limits import (
    SmoothRateLimiter,
    build_request_for_persona,
    compute_cost,
    estimate_input_tokens,
)

#: 可重试的 provider 错误码（有限重试）。
RECOVERABLE_ERROR_CODES: frozenset[str] = frozenset(
    {ERROR_TIMEOUT, ERROR_RATE_LIMITED, ERROR_SERVER_ERROR}
)

#: 供应商**明确拒绝**（未处理/未计费）→ 按 0 结算，不保留预留。
REJECTED_ERROR_CODES: frozenset[str] = frozenset(
    {ERROR_RATE_LIMITED, ERROR_AUTH, ERROR_PROTOCOL}
)

#: 未知计费（保留预留）的错误码。
UNKNOWN_COST_ERROR_CODES: frozenset[str] = frozenset({ERROR_TIMEOUT, ERROR_SERVER_ERROR})

#: 连续同类供应商失败达该次数 → 暂停 ``api_unavailable``（主文档 §6.4）。
CONSECUTIVE_FAILURE_PAUSE_THRESHOLD = 5


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略（主文档 §6.4：最多 3 次尝试；退避 2s、8s + 抖动；Retry-After 优先）。"""

    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (2.0, 8.0)
    jitter_ratio: float = 0.2


def compute_backoff_seconds(
    attempt_no: int,
    policy: RetryPolicy,
    *,
    retry_after: float | None = None,
    jitter: float | None = None,
) -> float:
    """计算第 ``attempt_no`` 次尝试失败后的退避秒数。

    - 退避基数取 ``policy.backoff_seconds[attempt_no-1]``（越界取最后一个）。
    - 叠加小幅随机抖动（``jitter`` 可注入以便测试确定性）。
    - 有 ``Retry-After`` 时**至少**等待该时长。
    """
    if attempt_no < 1:
        raise ValueError("attempt_no must be >= 1")
    index = min(attempt_no - 1, len(policy.backoff_seconds) - 1)
    base = policy.backoff_seconds[index]
    spread = policy.jitter_ratio * base
    applied_jitter = spread if jitter is None else max(0.0, jitter)
    delay = base + applied_jitter
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay


@dataclass
class ExecutionOutcome:
    """一次执行的结果（用于观测与暂停判定）。"""

    member_id: UUID
    status: str  # succeeded | retry_wait | failed | lost
    error_code: str | None
    saved: bool
    answer_json: dict[str, Any] | None = None
    actual_cost: Decimal | None = None
    pause_reason: str | None = None


class AttemptExecutor:
    """执行一次已领取的成员任务（构造请求 → 调用 → 校验 → CAS 保存）。"""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: Any,
        settings: Settings | None = None,
        repository: RunRepository | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: SmoothRateLimiter | None = None,
        rng: random.Random | None = None,
        allow_reason: bool = True,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = provider
        self._settings = settings or get_settings()
        self._repo = repository or run_repository
        self._retry_policy = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter
        self._rng = rng or random.Random()
        self._allow_reason = allow_reason
        #: 连续同类供应商失败计数（429/5xx）；取得响应后重置。
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def execute(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        survey: SurveyInput,
        now: datetime | None = None,
    ) -> ExecutionOutcome:
        """执行一次尝试；返回结果并已（在独立事务内）落库。"""
        moment = now or datetime.now(UTC)

        request = build_request_for_persona(
            survey=survey,
            persona_snapshot=claim.persona_snapshot,
            model=self._settings.model_name,
            provider=self._settings.model_provider,
            max_output_tokens=self._settings.model_max_output_tokens,
            timeout_seconds=self._settings.model_timeout_seconds,
            allow_reason=self._allow_reason,
        )

        # 限流在发请求前（平滑 RPM/TPM）。此处**不在任何事务内**。
        if self._rate_limiter is not None:
            tokens = estimate_input_tokens(
                request.system_prompt, request.user_message
            ) + int(request.max_output_tokens)
            await self._rate_limiter.acquire(tokens)

        response = await self._provider.answer(request)
        return await self._handle_response(run=run, claim=claim, response=response, now=moment)

    # ------------------------------------------------------------------
    # 响应处理
    # ------------------------------------------------------------------

    async def _handle_response(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        response: ModelResponse,
        now: datetime,
    ) -> ExecutionOutcome:
        usage_json, actual_cost = self._usage_and_cost(response)

        if response.error is not None:
            return await self._handle_provider_error(
                run=run,
                claim=claim,
                error_code=response.error.error_code,
                retry_after=response.error.retry_after_seconds,
                usage_json=usage_json,
                actual_cost=actual_cost,
                now=now,
            )

        # 取得响应 → 重置连续失败计数。
        self._consecutive_failures = 0

        try:
            answer: ValidatedAnswer = parse_and_validate(
                response.raw_text, allow_reason=self._allow_reason
            )
        except InvalidOutputError:
            # 无效输出：有限重试，**绝不填默认答案**。
            return await self._record_failure_outcome(
                run=run,
                claim=claim,
                error_code=ERROR_INVALID_OUTPUT,
                usage_json=usage_json,
                actual_cost=actual_cost,
                raw_output=self._truncate(response.raw_text),
                retry_after=None,
                pause_reason=None,
                now=now,
            )

        answer_json = answer.model_dump(mode="json", exclude_none=True)
        return await self._record_success(
            run=run,
            claim=claim,
            answer_json=answer_json,
            usage_json=usage_json,
            actual_cost=actual_cost,
            response=response,
            now=now,
        )

    async def _handle_provider_error(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        error_code: str,
        retry_after: float | None,
        usage_json: dict[str, Any] | None,
        actual_cost: Decimal | None,
        now: datetime,
    ) -> ExecutionOutcome:
        if error_code in (ERROR_RATE_LIMITED, ERROR_SERVER_ERROR):
            self._consecutive_failures += 1
        elif error_code == ERROR_AUTH:
            pass
        elif error_code == ERROR_PROTOCOL:
            pass

        pause_reason: str | None = None
        if error_code == ERROR_AUTH:
            pause_reason = "api_auth"
        elif error_code == ERROR_PROTOCOL:
            pause_reason = "api_unavailable"
        elif self._consecutive_failures >= CONSECUTIVE_FAILURE_PAUSE_THRESHOLD:
            pause_reason = "api_unavailable"

        # 明确拒绝（429/AUTH/PROTOCOL）→ 按 0 结算；TIMEOUT/SERVER_ERROR → 未知计费保留预留。
        settle_cost = Decimal("0") if error_code in REJECTED_ERROR_CODES else actual_cost

        if error_code == ERROR_AUTH:
            return await self._record_auth_pause(
                run=run,
                claim=claim,
                error_code=error_code,
                usage_json=usage_json,
                actual_cost=settle_cost,
                raw_output=None,
                now=now,
            )

        return await self._record_failure_outcome(
            run=run,
            claim=claim,
            error_code=error_code,
            usage_json=usage_json,
            actual_cost=settle_cost,
            raw_output=None,
            retry_after=retry_after,
            force_permanent=error_code not in RECOVERABLE_ERROR_CODES,
            pause_reason=pause_reason,
            now=now,
        )

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    async def _record_success(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        answer_json: dict[str, Any],
        usage_json: dict[str, Any] | None,
        actual_cost: Decimal | None,
        response: ModelResponse,
        now: datetime,
    ) -> ExecutionOutcome:
        async with self._sessionmaker() as session:
            async with session.begin():
                saved = await self._repo.finalize_success(
                    session,
                    claim=claim,
                    answer_json=answer_json,
                    usage_json=usage_json,
                    actual_cost=actual_cost,
                    duration_ms=response.duration_ms,
                    provider_request_id=response.provider_request_id,
                    raw_output=self._truncate(response.raw_text),
                    now=now,
                )
                await self._repo.converge_run(session, run.id, now=now)
        return ExecutionOutcome(
            member_id=claim.member_id,
            status="succeeded" if saved else "lost",
            error_code=None,
            saved=saved,
            answer_json=answer_json if saved else None,
            actual_cost=actual_cost,
        )

    async def _record_failure_outcome(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        error_code: str,
        usage_json: dict[str, Any] | None,
        actual_cost: Decimal | None,
        raw_output: str | None,
        retry_after: float | None,
        pause_reason: str | None,
        now: datetime,
        force_permanent: bool = False,
    ) -> ExecutionOutcome:
        retry_allowed = (not force_permanent) and claim.attempt_no < claim.attempt_limit
        next_attempt_at: datetime | None = None
        status = "failed"
        if retry_allowed:
            status = "retry_wait"
            index = min(claim.attempt_no - 1, len(self._retry_policy.backoff_seconds) - 1)
            base = self._retry_policy.backoff_seconds[index]
            jitter = (
                self._rng.uniform(0.0, self._retry_policy.jitter_ratio * base)
                if self._retry_policy.jitter_ratio > 0
                else 0.0
            )
            delay = compute_backoff_seconds(
                claim.attempt_no, self._retry_policy, retry_after=retry_after, jitter=jitter
            )
            next_attempt_at = now + timedelta(seconds=delay)

        async with self._sessionmaker() as session:
            async with session.begin():
                saved = await self._repo.finalize_failure(
                    session,
                    claim=claim,
                    status=status,
                    error_code=error_code,
                    next_attempt_at=next_attempt_at,
                    usage_json=usage_json,
                    actual_cost=actual_cost,
                    raw_output=raw_output,
                    now=now,
                )
                await self._repo.converge_run(session, run.id, now=now)
        return ExecutionOutcome(
            member_id=claim.member_id,
            status=status if saved else "lost",
            error_code=error_code,
            saved=saved,
            pause_reason=pause_reason,
        )

    async def _record_auth_pause(
        self,
        *,
        run: RunRecord,
        claim: Claim,
        error_code: str,
        usage_json: dict[str, Any] | None,
        actual_cost: Decimal | None,
        raw_output: str | None,
        now: datetime,
    ) -> ExecutionOutcome:
        """AUTH：成员回 ``retry_wait`` 并返还本次消耗的重试额度，暂停 ``api_auth``。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                saved = await self._repo.requeue_member(
                    session, member_id=claim.member_id, lease_token=claim.lease_token, now=now
                )
                if saved:
                    await self._repo.mark_attempt_failed(
                        session,
                        attempt_id=claim.attempt_id,
                        error_code=error_code,
                        usage_json=usage_json,
                        raw_output=raw_output,
                        now=now,
                    )
                    await self._repo.extend_attempt_limit(
                        session, member_id=claim.member_id, extra=1
                    )
                    await self._repo.apply_attempt_cost(
                        session,
                        run_id=claim.run_id,
                        reserved_release=claim.reserved_cost,
                        actual_cost=actual_cost if actual_cost is not None else Decimal("0"),
                    )
                await self._repo.converge_run(session, run.id, now=now)
        return ExecutionOutcome(
            member_id=claim.member_id,
            status="retry_wait" if saved else "lost",
            error_code=error_code,
            saved=saved,
            pause_reason="api_auth",
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _usage_and_cost(
        self, response: ModelResponse
    ) -> tuple[dict[str, Any] | None, Decimal | None]:
        if response.input_tokens is None and response.output_tokens is None:
            return None, None
        usage_json: dict[str, Any] = {
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
        }
        actual_cost = compute_cost(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            input_price_per_million=self._settings.input_price_per_million,
            output_price_per_million=self._settings.output_price_per_million,
        )
        return usage_json, actual_cost

    @staticmethod
    def _truncate(raw: str | None) -> str | None:
        if raw is None:
            return None
        return raw[:20000]


__all__ = [
    "CONSECUTIVE_FAILURE_PAUSE_THRESHOLD",
    "RECOVERABLE_ERROR_CODES",
    "REJECTED_ERROR_CODES",
    "UNKNOWN_COST_ERROR_CODES",
    "AttemptExecutor",
    "ExecutionOutcome",
    "RetryPolicy",
    "compute_backoff_seconds",
]
