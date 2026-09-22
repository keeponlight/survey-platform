# SPDX-License-Identifier: GPL-3.0-or-later
"""worker 包：单 worker 锁与调度循环（主文档 §3 Worker / §6.3 / §7）。

- :mod:`app.worker.main`    —— advisory lock + 100 消费者调度器 + 租约/过期/看门狗。
- :mod:`app.worker.execute` —— 单次领取的执行与 CAS 保存、错误分类、重试策略。
- :mod:`app.worker.limits`  —— 并发槽位、平滑 RPM/TPM 限流、预算/费用估算。
"""

from app.worker.execute import (
    AttemptExecutor,
    ExecutionOutcome,
    RetryPolicy,
    compute_backoff_seconds,
)
from app.worker.limits import (
    DEFAULT_CONCURRENCY,
    RATE_WINDOW_SECONDS,
    ConcurrencyLimiter,
    SmoothRateLimiter,
    build_request_for_persona,
    compute_cost,
    estimate_input_tokens,
    estimate_request_reservation,
    reservation_for_request,
)
from app.worker.main import (
    ADVISORY_LOCK_KEY,
    DEFAULT_LEASE_SECONDS,
    AdvisoryLock,
    Worker,
    build_worker,
)

__all__ = [
    "ADVISORY_LOCK_KEY",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_LEASE_SECONDS",
    "RATE_WINDOW_SECONDS",
    "AdvisoryLock",
    "AttemptExecutor",
    "ConcurrencyLimiter",
    "ExecutionOutcome",
    "RetryPolicy",
    "SmoothRateLimiter",
    "Worker",
    "build_request_for_persona",
    "build_worker",
    "compute_backoff_seconds",
    "compute_cost",
    "estimate_input_tokens",
    "estimate_request_reservation",
    "reservation_for_request",
]
