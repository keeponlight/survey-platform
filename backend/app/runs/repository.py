# SPDX-License-Identifier: GPL-3.0-or-later
"""批次与成员的数据访问层（主文档 §6.1 / §6.3 / architecture.md §6）。

职责：**只做 DB 原语** —— 建批、幂等查询、领取、租约续期/过期扫描、
CAS 保存、成本结算（预留/释放/未知保留）、收敛判断、限流窗口重建查询。不含 HTTP、不含模型调用。

关键不变量（主文档 §6.3 + 主理人裁决）：
- 领取在**同一事务**内完成：锁 run 行 → 检查状态/请求额度/预算 → ``FOR UPDATE SKIP LOCKED``
  领取到期 ``pending``/``retry_wait`` → 生成新 ``lease_token``/``attempt_no`` → 写 ``attempts`` →
  **提交**。**提交之后**才由上层发起模型请求（本模块绝不 ``await`` 模型）。
- 结果保存用 **CAS**：``WHERE status='running' AND lease_token=:本次token``；影响 0 行 = 失租，
  迟到结果**不得**覆盖答案或状态，仅按 ``attempt.id`` 幂等结算费用。
- **未知计费（超时/崩溃/abandoned）保留预留**，不按 0 释放（主文档 §7 / 裁决）。
- ``requests_reserved`` 原子递增；额度或预算不足 → 以 ``budget`` 原因暂停。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ACTIVE_RUN_STATUSES
from app.models import Attempt, RunMember, RunRecord

#: ``request_limit`` 默认倍数（主文档 §7：3 × sample_size，由应用写入，DDL 无默认）。
REQUEST_LIMIT_FACTOR = 3

#: 成员默认最大尝试次数（首调 + 2 次重试）。
DEFAULT_ATTEMPT_LIMIT = 3

#: 分块 insert 的默认块大小（20,000 行分块，但**同一事务**提交）。
DEFAULT_CHUNK_SIZE = 1000

#: run 终态。
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
)

#: 需要暂停（停止领取新任务）的 run 状态。
PAUSING_RUN_STATUSES: tuple[str, ...] = ("pausing", "paused", "cancelling")

#: 可领取的成员状态。
CLAIMABLE_MEMBER_STATUSES: tuple[str, ...] = ("pending", "retry_wait")

#: 建批进度/故障注入钩子：``hook(chunk_index)``，每个分块 flush 后调用一次。
FlushHook = Callable[[int], None]


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberDraft:
    """建批时一个成员任务的草稿（row_no/persona_id/persona_snapshot 原样保留）。"""

    row_no: int
    persona_id: str
    persona_snapshot: dict[str, Any]


@dataclass(frozen=True)
class Claim:
    """一次成功领取的任务（提交后交给上层执行；**不持有事务**）。"""

    run_id: uuid.UUID
    member_id: uuid.UUID
    row_no: int
    persona_id: str
    persona_snapshot: dict[str, Any]
    attempt_id: uuid.UUID
    attempt_no: int
    attempt_limit: int
    lease_token: uuid.UUID
    lease_expires_at: datetime
    reserved_cost: Decimal


@dataclass
class ClaimResult:
    """领取结果：本次领取的成员 + 是否因额度/预算耗尽需暂停。"""

    claims: list[Claim] = field(default_factory=list)
    run_status: str = "running"
    pause_reason: str | None = None
    claimed: int = 0


@dataclass(frozen=True)
class ExpiredRecovery:
    """一次过期租约恢复的结果。"""

    member_id: uuid.UUID
    attempt_id: uuid.UUID | None
    new_status: str


@dataclass(frozen=True)
class MemberStatusCounts:
    """按状态统计的成员数量。"""

    counts: Mapping[str, int]

    def get(self, status: str) -> int:
        return int(self.counts.get(status, 0))

    @property
    def non_terminal(self) -> int:
        return sum(self.counts.get(s, 0) for s in ("pending", "running", "retry_wait"))

    @property
    def known_statuses(self) -> tuple[str, ...]:
        return (
            "pending",
            "running",
            "succeeded",
            "retry_wait",
            "failed",
            "cancelled",
        )


class RunRepository:
    """批次与成员的 DB 原语集合（无状态，可安全共享）。"""

    # ------------------------------------------------------------------
    # 建批（同一事务）
    # ------------------------------------------------------------------

    async def find_by_idempotency_key(
        self, session: AsyncSession, idempotency_key: str
    ) -> RunRecord | None:
        """按幂等键查询已存在的 run（幂等记录落库，非内存）。"""
        result = await session.execute(
            select(RunRecord).where(RunRecord.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()

    async def insert_run_with_members(
        self,
        session: AsyncSession,
        run: RunRecord,
        members: Sequence[MemberDraft],
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        flush_hook: FlushHook | None = None,
    ) -> None:
        """在同一事务内写入 run 行 + 全部分块成员（**不提交**，由调用方提交）。

        20,000 行分块 insert；任一块失败向上抛出 → 调用方回滚 → **不允许半批任务被执行**。
        ``flush_hook`` 为可选的进度/故障注入钩子（每块 flush 后调用一次）。
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")

        session.add(run)
        await session.flush()

        total = len(members)
        for chunk_index, start in enumerate(range(0, total, chunk_size)):
            chunk = members[start : start + chunk_size]
            rows = [
                {
                    "id": uuid.uuid4(),
                    "run_id": run.id,
                    "row_no": member.row_no,
                    "persona_id": member.persona_id,
                    "persona_snapshot": member.persona_snapshot,
                    "status": "pending",
                    "answer_json": None,
                    "attempt_count": 0,
                    "attempt_limit": DEFAULT_ATTEMPT_LIMIT,
                    "next_attempt_at": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                }
                for member in chunk
            ]
            if rows:
                await session.execute(insert(RunMember), rows)
            if flush_hook is not None:
                flush_hook(chunk_index)

    async def count_members(self, session: AsyncSession, run_id: uuid.UUID) -> int:
        """成员总数（N 进 N 出核对用）。"""
        total = await session.scalar(
            select(func.count()).select_from(RunMember).where(RunMember.run_id == run_id)
        )
        return int(total or 0)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get_run(self, session: AsyncSession, run_id: uuid.UUID) -> RunRecord | None:
        result = await session.execute(select(RunRecord).where(RunRecord.id == run_id))
        return result.scalar_one_or_none()

    async def get_run_for_update(
        self, session: AsyncSession, run_id: uuid.UUID
    ) -> RunRecord | None:
        """加行锁读取 run（启动/领取路径使用）。"""
        result = await session.execute(
            select(RunRecord).where(RunRecord.id == run_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def find_other_active_run(
        self, session: AsyncSession, run_id: uuid.UUID
    ) -> uuid.UUID | None:
        """可读的前置快速校验：是否存在**其它**活动批次（DB 部分唯一索引为最终裁决）。

        注意（改判 A1）：当不存在其它活动批次时结果集为空，``FOR UPDATE`` **不锁任何行**，
        故本查询**不具备竞态安全性**；真正的兜底是 ``uq_runs_single_active``。
        """
        result = await session.execute(
            select(RunRecord.id)
            .where(
                RunRecord.status.in_(ACTIVE_RUN_STATUSES),
                RunRecord.id != run_id,
            )
            .with_for_update()
        )
        return result.scalars().first()

    async def count_members_by_status(
        self, session: AsyncSession, run_id: uuid.UUID
    ) -> MemberStatusCounts:
        result = await session.execute(
            select(RunMember.status, func.count())
            .where(RunMember.run_id == run_id)
            .group_by(RunMember.status)
        )
        counts = {str(status): int(count) for status, count in result.all()}
        return MemberStatusCounts(counts=counts)

    async def list_members(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        *,
        statuses: Sequence[str] | None = None,
        orders: bool = True,
    ) -> list[RunMember]:
        stmt = select(RunMember).where(RunMember.run_id == run_id)
        if statuses is not None:
            stmt = stmt.where(RunMember.status.in_(tuple(statuses)))
        if orders:
            stmt = stmt.order_by(RunMember.row_no)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 领取（队列式消费；提交后发请求）
    # ------------------------------------------------------------------

    async def _lock_run_counters(self, session: AsyncSession, run_id: uuid.UUID) -> Any:
        """加锁读取 run 的计数列（直接读列，避免 ORM 身份映射返回陈旧属性）。

        锁顺序约定：**先成员行、后 run 行**（与 ``finalize_*`` 一致），
        避免「领取持 run 锁等成员」与「保存持成员锁等 run」形成死锁环。
        """
        result = await session.execute(
            select(
                RunRecord.status,
                RunRecord.request_limit,
                RunRecord.requests_reserved,
                RunRecord.budget_limit,
                RunRecord.actual_cost,
                RunRecord.reserved_cost,
            )
            .where(RunRecord.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.one_or_none()

    async def claim_members(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        limit: int,
        lease_seconds: float,
        reservation_fn: Callable[[dict[str, Any]], Decimal] | None = None,
        now: datetime | None = None,
    ) -> ClaimResult:
        """在**当前事务**内领取至多 ``limit`` 个到期成员并写 attempts（调用方随后提交）。

        - 非锁定读 run 做前置快速判断 → ``FOR UPDATE SKIP LOCKED`` **先锁成员行** →
          再锁 run 行并复核状态/额度/预算（统一 member→run 锁顺序，避免死锁）。
        - 生成新 ``lease_token``/``attempt_no``，写 ``attempts(status='running')``。
        - ``requests_reserved`` 与 ``reserved_cost`` 原子递增。
        - 请求额度或预算不足 → ``pause_reason='budget'``（上层据此暂停，不领取）。
        """
        moment = now or utcnow()
        result = ClaimResult()

        run = await self.get_run(session, run_id)  # 非锁定读
        if run is None:
            result.run_status = "missing"
            return result
        result.run_status = run.status
        if run.status != "running":
            return result
        if limit <= 0:
            return result

        # 先取候选成员（**不**按额度裁剪：额度/预算耗尽只有在「确有待领取成员」时才算暂停）。
        stmt = (
            select(RunMember)
            .where(
                RunMember.run_id == run_id,
                RunMember.status.in_(CLAIMABLE_MEMBER_STATUSES),
                or_(
                    RunMember.next_attempt_at.is_(None),
                    RunMember.next_attempt_at <= moment,
                ),
            )
            .order_by(RunMember.row_no)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        members = list((await session.execute(stmt)).scalars().all())
        if not members:
            # 无可领取成员 → 既不领取也不暂停（run 由收敛逻辑推进终态）。
            return result

        # 先成员、后 run：与 finalize 路径锁顺序一致，避免死锁环。
        locked = await self._lock_run_counters(session, run_id)
        if locked is None:
            result.run_status = "missing"
            return result
        (
            locked_status,
            locked_request_limit,
            locked_requests_reserved,
            budget_limit,
            actual_cost,
            reserved_cost,
        ) = locked
        result.run_status = str(locked_status)
        if locked_status != "running":
            return result

        remaining_requests = int(locked_request_limit) - int(locked_requests_reserved)
        if remaining_requests <= 0:
            # 确有待领取成员但请求额度已耗尽 → 以 budget 暂停。
            result.pause_reason = "budget"
            return result

        spent = Decimal(actual_cost or 0) + Decimal(reserved_cost or 0)
        accumulate = Decimal(0)
        selected: list[tuple[RunMember, Decimal, int, uuid.UUID]] = []

        for member in members[:remaining_requests]:
            reservation = (
                reservation_fn(member.persona_snapshot)
                if reservation_fn is not None
                else Decimal(0)
            )
            if budget_limit is not None:
                if spent + accumulate + reservation > Decimal(budget_limit):
                    break
            attempt_no = int(member.attempt_count) + 1
            lease_token = uuid.uuid4()
            selected.append((member, reservation, attempt_no, lease_token))
            accumulate += reservation

        if not selected:
            # 有可领取成员但预算不足一个 → budget 暂停。
            if budget_limit is not None:
                result.pause_reason = "budget"
            return result

        lease_expires_at = moment + timedelta(seconds=lease_seconds)
        attempt_rows: list[dict[str, Any]] = []
        for member, reservation, attempt_no, lease_token in selected:
            attempt_id = uuid.uuid4()
            await session.execute(
                update(RunMember)
                .where(RunMember.id == member.id)
                .values(
                    status="running",
                    attempt_count=attempt_no,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    next_attempt_at=None,
                    updated_at=moment,
                )
            )
            attempt_rows.append(
                {
                    "id": attempt_id,
                    "member_id": member.id,
                    "attempt_no": attempt_no,
                    "lease_token": lease_token,
                    "started_at": moment,
                    "finished_at": None,
                    "status": "running",
                    "provider_request_id": None,
                    "raw_output": None,
                    "error_code": None,
                    "usage_json": None,
                    "reserved_cost": reservation,
                    "actual_cost": None,
                    "duration_ms": None,
                }
            )
            result.claims.append(
                Claim(
                    run_id=run_id,
                    member_id=member.id,
                    row_no=int(member.row_no),
                    persona_id=str(member.persona_id),
                    persona_snapshot=dict(member.persona_snapshot),
                    attempt_id=attempt_id,
                    attempt_no=attempt_no,
                    attempt_limit=int(member.attempt_limit),
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    reserved_cost=reservation,
                )
            )

        await session.execute(insert(Attempt), attempt_rows)

        claimed = len(result.claims)
        result.claimed = claimed
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id)
            .values(
                requests_reserved=RunRecord.requests_reserved + claimed,
                reserved_cost=RunRecord.reserved_cost + accumulate,
            )
        )
        return result

    # ------------------------------------------------------------------
    # 租约续期 / 过期扫描
    # ------------------------------------------------------------------

    async def renew_lease(
        self,
        session: AsyncSession,
        *,
        member_id: uuid.UUID,
        lease_token: uuid.UUID,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """续租：``WHERE id AND lease_token AND status='running'``。

        返回 ``False`` 表示**已失租**（影响 0 行），调用方应立即停止后续处理。
        """
        moment = now or utcnow()
        result = await session.execute(
            update(RunMember)
            .where(
                RunMember.id == member_id,
                RunMember.lease_token == lease_token,
                RunMember.status == "running",
            )
            .values(lease_expires_at=moment + timedelta(seconds=lease_seconds))
        )
        return result.rowcount == 1

    async def abandon_expired(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        now: datetime | None = None,
    ) -> list[ExpiredRecovery]:
        """扫描过期租约：把``running`` attempt 记为 ``abandoned``，成员按剩余次数转
        ``retry_wait``/``failed``。

        - ``abandoned`` attempt 的 ``reserved_cost`` **保留、不按 0 释放**（未知计费）。
        - 该 attempt 的未知计费计入 ``unknown_cost_count``。
        """
        moment = now or utcnow()
        stmt = (
            select(RunMember)
            .where(
                RunMember.run_id == run_id,
                RunMember.status == "running",
                RunMember.lease_expires_at.is_not(None),
                RunMember.lease_expires_at < moment,
            )
            .with_for_update(skip_locked=True)
        )
        members = list((await session.execute(stmt)).scalars().all())
        recoveries: list[ExpiredRecovery] = []

        for member in members:
            attempt = await session.scalar(
                select(Attempt).where(
                    Attempt.member_id == member.id,
                    Attempt.lease_token == member.lease_token,
                    Attempt.status == "running",
                )
            )
            attempt_id: uuid.UUID | None = None
            if attempt is not None:
                attempt_id = attempt.id
                await session.execute(
                    update(Attempt)
                    .where(Attempt.id == attempt.id)
                    .values(status="abandoned", finished_at=moment)
                )
                # 未知计费：保留预留，仅计数。
                await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == run_id)
                    .values(unknown_cost_count=RunRecord.unknown_cost_count + 1)
                )

            remaining = int(member.attempt_count) < int(member.attempt_limit)
            new_status = "retry_wait" if remaining else "failed"
            values: dict[str, Any] = {
                "status": new_status,
                "lease_token": None,
                "lease_expires_at": None,
                "next_attempt_at": moment if new_status == "retry_wait" else None,
                "updated_at": moment,
            }
            updated = await session.execute(
                update(RunMember)
                .where(
                    RunMember.id == member.id,
                    RunMember.status == "running",
                    RunMember.lease_token == member.lease_token,
                )
                .values(**values)
            )
            if updated.rowcount == 1:
                recoveries.append(
                    ExpiredRecovery(
                        member_id=member.id,
                        attempt_id=attempt_id,
                        new_status=new_status,
                    )
                )
        return recoveries

    # ------------------------------------------------------------------
    # 结果 CAS 保存
    # ------------------------------------------------------------------

    async def finalize_success(
        self,
        session: AsyncSession,
        *,
        claim: Claim,
        answer_json: dict[str, Any],
        usage_json: dict[str, Any] | None = None,
        actual_cost: Decimal | None = None,
        duration_ms: int | None = None,
        provider_request_id: str | None = None,
        raw_output: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """CAS 保存成功结果 + 结算成本 + 标记 attempt 完成（**同一事务**）。

        返回 ``False`` 表示失租（CAS 0 行）：**丢弃本次结果**（不覆盖答案/状态），
        仅按 ``attempt.id`` 幂等结算迟到费用。
        """
        moment = now or utcnow()
        result = await session.execute(
            update(RunMember)
            .where(
                RunMember.id == claim.member_id,
                RunMember.status == "running",
                RunMember.lease_token == claim.lease_token,
            )
            .values(
                status="succeeded",
                answer_json=answer_json,
                attempt_count=claim.attempt_no,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=moment,
            )
        )
        if result.rowcount != 1:
            await self._settle_late_attempt(
                session,
                attempt_id=claim.attempt_id,
                run_id=claim.run_id,
                usage_json=usage_json,
                actual_cost=actual_cost,
                now=moment,
            )
            return False

        await session.execute(
            update(Attempt)
            .where(Attempt.id == claim.attempt_id)
            .values(
                status="succeeded",
                finished_at=moment,
                usage_json=usage_json,
                actual_cost=actual_cost,
                duration_ms=duration_ms,
                provider_request_id=provider_request_id,
                raw_output=raw_output,
            )
        )
        await self.apply_attempt_cost(
            session,
            run_id=claim.run_id,
            reserved_release=claim.reserved_cost,
            actual_cost=actual_cost,
        )
        return True

    async def finalize_failure(
        self,
        session: AsyncSession,
        *,
        claim: Claim,
        status: str,
        error_code: str,
        next_attempt_at: datetime | None = None,
        usage_json: dict[str, Any] | None = None,
        actual_cost: Decimal | None = None,
        duration_ms: int | None = None,
        raw_output: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """CAS 保存失败/重试结果（同一事务）。``status`` ∈ {``retry_wait``, ``failed``}。

        返回 ``False`` 表示失租（CAS 0 行）：丢弃结果，仅幂等结算迟到费用。
        """
        if status not in ("retry_wait", "failed"):
            raise ValueError(f"invalid failure status: {status!r}")
        moment = now or utcnow()
        values: dict[str, Any] = {
            "status": status,
            "attempt_count": claim.attempt_no,
            "last_error_code": error_code,
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": next_attempt_at if status == "retry_wait" else None,
            "updated_at": moment,
        }
        result = await session.execute(
            update(RunMember)
            .where(
                RunMember.id == claim.member_id,
                RunMember.status == "running",
                RunMember.lease_token == claim.lease_token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            await self._settle_late_attempt(
                session,
                attempt_id=claim.attempt_id,
                run_id=claim.run_id,
                usage_json=usage_json,
                actual_cost=actual_cost,
                now=moment,
            )
            return False

        await session.execute(
            update(Attempt)
            .where(Attempt.id == claim.attempt_id)
            .values(
                status="failed",
                finished_at=moment,
                error_code=error_code,
                usage_json=usage_json,
                actual_cost=actual_cost,
                duration_ms=duration_ms,
                raw_output=raw_output,
            )
        )
        await self.apply_attempt_cost(
            session,
            run_id=claim.run_id,
            reserved_release=claim.reserved_cost,
            actual_cost=actual_cost,
        )
        return True

    async def mark_attempt_failed(
        self,
        session: AsyncSession,
        *,
        attempt_id: uuid.UUID,
        error_code: str,
        usage_json: dict[str, Any] | None = None,
        raw_output: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """把一次 attempt 标记为 ``failed``（不触碰成员、不结算成本）。

        供 AUTH 类暂停路径使用：成员回 ``retry_wait``，但 attempt 审计仍需记录。
        """
        moment = now or utcnow()
        await session.execute(
            update(Attempt)
            .where(Attempt.id == attempt_id)
            .values(
                status="failed",
                finished_at=moment,
                error_code=error_code,
                usage_json=usage_json,
                raw_output=raw_output,
            )
        )

    async def extend_attempt_limit(
        self, session: AsyncSession, *, member_id: uuid.UUID, extra: int
    ) -> None:
        """返还因**供应商配置错误**暂停而消耗的自动重试额度（主文档 §6.4）。"""
        await session.execute(
            update(RunMember)
            .where(RunMember.id == member_id)
            .values(attempt_limit=RunMember.attempt_limit + extra)
        )

    async def requeue_member(
        self,
        session: AsyncSession,
        *,
        member_id: uuid.UUID,
        lease_token: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        """把仍持租的成员转回 ``retry_wait``（用于 AUTH 类暂停后保留待恢复成员）。"""
        moment = now or utcnow()
        result = await session.execute(
            update(RunMember)
            .where(
                RunMember.id == member_id,
                RunMember.status == "running",
                RunMember.lease_token == lease_token,
            )
            .values(
                status="retry_wait",
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=moment,
                attempt_count=RunMember.attempt_count,
                updated_at=moment,
            )
        )
        return result.rowcount == 1

    # ------------------------------------------------------------------
    # 成本结算
    # ------------------------------------------------------------------

    async def apply_attempt_cost(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        reserved_release: Decimal,
        actual_cost: Decimal | None,
    ) -> None:
        """结算一次 attempt 的成本（run 级账本）。

        - ``actual_cost`` 已知 → 释放本次预留、累加实际支出。
        - ``actual_cost`` 未知（超时/崩溃/无 usage）→ **保留预留**，仅累加
          ``unknown_cost_count``，**绝不按 0 释放**。
        """
        if actual_cost is None:
            await session.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id)
                .values(unknown_cost_count=RunRecord.unknown_cost_count + 1)
            )
            return
        await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id)
            .values(
                actual_cost=RunRecord.actual_cost + actual_cost,
                reserved_cost=func.greatest(
                    RunRecord.reserved_cost - Decimal(reserved_release), Decimal(0)
                ),
            )
        )

    async def _settle_late_attempt(
        self,
        session: AsyncSession,
        *,
        attempt_id: uuid.UUID,
        run_id: uuid.UUID,
        usage_json: dict[str, Any] | None,
        actual_cost: Decimal | None,
        now: datetime,
    ) -> bool:
        """失租的迟到结果：不覆盖答案/状态，仅按 ``attempt.id`` 幂等结算费用。

        ``actual_cost`` 未知时不做任何事（保留预留）。
        """
        attempt = await session.scalar(select(Attempt).where(Attempt.id == attempt_id))
        if attempt is None:
            return False
        if attempt.actual_cost is not None:
            return False  # 已结算，幂等返回
        if actual_cost is None:
            return False  # 未知计费：保留预留，不按 0 释放
        await session.execute(
            update(Attempt)
            .where(Attempt.id == attempt_id, Attempt.actual_cost.is_(None))
            .values(actual_cost=actual_cost, usage_json=usage_json, finished_at=now)
        )
        await self.apply_attempt_cost(
            session,
            run_id=run_id,
            reserved_release=Decimal(attempt.reserved_cost or 0),
            actual_cost=actual_cost,
        )
        return True

    # ------------------------------------------------------------------
    # 收敛
    # ------------------------------------------------------------------

    async def converge_run(
        self, session: AsyncSession, run_id: uuid.UUID, *, now: datetime | None = None
    ) -> str | None:
        """在途收敛后推进 run 终态（幂等；``UPDATE`` 以 ``status='running'`` 为守卫）。

        返回新的状态；未到收敛条件返回 ``None``。
        """
        moment = now or utcnow()
        run = await self.get_run(session, run_id)
        if run is None:
            return None
        status = run.status
        if status in TERMINAL_RUN_STATUSES:
            return status

        counts = await self.count_members_by_status(session, run_id)

        if status == "pausing":
            if counts.get("running") == 0:
                updated = await session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == run_id, RunRecord.status == "pausing")
                    .values(status="paused", finished_at=None)
                )
                if updated.rowcount == 1:
                    return "paused"
            return None

        if status == "cancelling":
            if counts.get("running") == 0:
                # 未开始/未成功成员转 cancelled；已成功答案保留。
                await session.execute(
                    update(RunMember)
                    .where(
                        RunMember.run_id == run_id,
                        RunMember.status.in_(("pending", "retry_wait")),
                    )
                    .values(status="cancelled", lease_token=None, lease_expires_at=None)
                )
                counts = await self.count_members_by_status(session, run_id)
                if counts.non_terminal == 0:
                    updated = await session.execute(
                        update(RunRecord)
                        .where(RunRecord.id == run_id, RunRecord.status == "cancelling")
                        .values(status="cancelled", finished_at=moment)
                    )
                    if updated.rowcount == 1:
                        return "cancelled"
            return None

        if status != "running":
            return None

        if counts.non_terminal > 0:
            return None

        succeeded = counts.get("succeeded")
        failed = counts.get("failed")
        if succeeded > 0 and failed == 0:
            new_status = "completed"
        elif succeeded == 0 and failed > 0:
            new_status = "failed"
        elif succeeded > 0 and failed > 0:
            new_status = "completed_with_errors"
        else:
            new_status = "failed"

        updated = await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id, RunRecord.status == "running")
            .values(status=new_status, finished_at=moment)
        )
        if updated.rowcount == 1:
            return new_status
        return None

    async def cancel_ready_run(
        self, session: AsyncSession, run_id: uuid.UUID, *, now: datetime | None = None
    ) -> bool:
        """把 ``ready`` 批次**一次性**收敛为 ``cancelled``（**不经**活动态 ``cancelling``）。

        根因（TEAM-BRIEF §7.9.1 / 主理人独立复现）：``ready`` 是**非活动**状态，而
        ``cancelling`` 是活动状态，故 ``ready → cancelling`` 会触碰部分唯一索引
        ``uq_runs_single_active``（当另有批次 ``running`` 时）→ ``IntegrityError`` 逃逸 → 500。
        ``ready`` 批次没有任何在途请求，无需持久化中间态：同事务把未开始（``pending``/
        ``retry_wait``）成员转 ``cancelled``、并把 run 直接落 ``cancelled``（写 ``finished_at``）。

        产出与经 ``cancelling`` 收敛（:meth:`converge_run` 的 ``cancelling`` 分支）**逐项等价**。
        返回 ``True`` 表示 run 已由 ``ready`` 转 ``cancelled``（影响 1 行）；``False`` 表示状态
        已非 ``ready``（并发竞态，调用方回退到普通路径）。
        """
        moment = now or utcnow()
        await session.execute(
            update(RunMember)
            .where(
                RunMember.run_id == run_id,
                RunMember.status.in_(("pending", "retry_wait")),
            )
            .values(status="cancelled", lease_token=None, lease_expires_at=None)
        )
        updated = await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id, RunRecord.status == "ready")
            .values(status="cancelled", finished_at=moment)
        )
        return updated.rowcount == 1

    async def request_pause(
        self, session: AsyncSession, *, run_id: uuid.UUID, reason: str
    ) -> bool:
        """请求暂停（``running → pausing`` + pause_reason）；幂等。

        ``reason`` ∈ {``user``, ``api_auth``, ``api_unavailable``, ``budget``}。
        """
        if reason not in ("user", "api_auth", "api_unavailable", "budget"):
            raise ValueError(f"invalid pause reason: {reason!r}")
        updated = await session.execute(
            update(RunRecord)
            .where(RunRecord.id == run_id, RunRecord.status == "running")
            .values(status="pausing", pause_reason=reason)
        )
        return updated.rowcount == 1

    # ------------------------------------------------------------------
    # 限流窗口重建（裁决 C3）
    # ------------------------------------------------------------------

    async def recent_attempt_tokens(
        self,
        session: AsyncSession,
        *,
        since: datetime,
        max_output_tokens: int,
        limit: int = 20000,
    ) -> list[tuple[datetime, int]]:
        """读取最近窗口内 attempts 的 ``(started_at, 计费 tokens 估计)``。

        worker 重启时据此**保守重建**限流窗口（重启即清空限额视为缺陷）。
        usage 缺失时按 ``max_output_tokens`` 保守上界计入。
        """
        result = await session.execute(
            select(Attempt.started_at, Attempt.usage_json)
            .where(Attempt.started_at >= since)
            .order_by(Attempt.started_at.desc())
            .limit(limit)
        )
        records: list[tuple[datetime, int]] = []
        for started_at, usage in result.all():
            tokens = 0
            if isinstance(usage, Mapping):
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                    tokens = input_tokens + output_tokens
            if tokens <= 0:
                tokens = max_output_tokens
            records.append((started_at, tokens))
        return records


#: 默认仓库实例（无状态）。
run_repository = RunRepository()


__all__ = [
    "CLAIMABLE_MEMBER_STATUSES",
    "DEFAULT_ATTEMPT_LIMIT",
    "DEFAULT_CHUNK_SIZE",
    "PAUSING_RUN_STATUSES",
    "REQUEST_LIMIT_FACTOR",
    "TERMINAL_RUN_STATUSES",
    "Claim",
    "ClaimResult",
    "ExpiredRecovery",
    "FlushHook",
    "MemberDraft",
    "MemberStatusCounts",
    "RunRepository",
    "run_repository",
    "utcnow",
]
