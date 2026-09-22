# SPDX-License-Identifier: GPL-3.0-or-later
"""runs 包：批次创建、幂等与可靠执行（主文档 §3 RunService / §6.1 / §6.3）。

- :mod:`app.runs.repository` —— DB 原语（建批/领取/租约/CAS/成本/收敛/限流重建查询）。
- :mod:`app.runs.service`    —— 领域服务（创建+幂等、启动+活动批次唯一性、收敛）。
"""

from app.runs.repository import (
    DEFAULT_ATTEMPT_LIMIT,
    DEFAULT_CHUNK_SIZE,
    REQUEST_LIMIT_FACTOR,
    TERMINAL_RUN_STATUSES,
    Claim,
    ClaimResult,
    MemberDraft,
    RunRepository,
    run_repository,
    utcnow,
)
from app.runs.service import (
    ActiveRunConflictError,
    IdempotencyConflictError,
    InvalidRunStateError,
    RunNotFoundError,
    RunService,
    RunValidationError,
    compute_request_hash,
    run_service,
)

__all__ = [
    "DEFAULT_ATTEMPT_LIMIT",
    "DEFAULT_CHUNK_SIZE",
    "REQUEST_LIMIT_FACTOR",
    "TERMINAL_RUN_STATUSES",
    "ActiveRunConflictError",
    "Claim",
    "ClaimResult",
    "IdempotencyConflictError",
    "InvalidRunStateError",
    "MemberDraft",
    "RunNotFoundError",
    "RunRepository",
    "RunService",
    "RunValidationError",
    "compute_request_hash",
    "run_repository",
    "run_service",
    "utcnow",
]
