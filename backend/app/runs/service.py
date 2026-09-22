# SPDX-License-Identifier: GPL-3.0-or-later
"""批次创建、幂等与控制状态（主文档 §6.1 / §6.2 / §8.2 / architecture.md §6.8）。

职责：
- ``create_run``：同一事务写入**冻结快照** + 全部成员（20,000 分块但同一事务提交）；
  幂等键同 key 同 hash → 返回原 run；同 key 异 hash → 冲突错误（→ 409）。
- ``start_run``：``ready → running``；两层活动批次唯一性校验（可读前置 + DB 唯一索引兜底）。
- ``converge_run``：在途收敛后推进终态。

本模块**不含 HTTP**：错误以领域异常表达，由 T4 的 API 层映射为 409/404/422。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.contracts import PROMPT_VERSION, SurveyInput
from app.models import RunMember, RunRecord
from app.personas.source import load_snapshots
from app.runs.repository import (
    DEFAULT_CHUNK_SIZE,
    FlushHook,
    MemberDraft,
    RunRepository,
    run_repository,
)
from app.surveys.service import RevisionConflictError, frozen_snapshot, survey_service

# ---------------------------------------------------------------------------
# 领域异常（由 T4 的 API 层映射为状态码）
# ---------------------------------------------------------------------------


class RunNotFoundError(Exception):
    """批次不存在（→ 404）。"""

    code = "NOT_FOUND"


class InvalidRunStateError(Exception):
    """run 当前状态不允许该操作（非法转移 → 409）。"""

    code = "INVALID_RUN_STATE"

    def __init__(self, current: str, expected: str) -> None:
        super().__init__(
            f"run status {current!r} does not allow this action (expected {expected!r})"
        )
        self.current = current
        self.expected = expected

    def to_details(self) -> dict[str, Any]:
        return {"current_status": self.current, "expected_status": self.expected}


class ActiveRunConflictError(Exception):
    """已存在其它活动批次（→ 409）。

    由 **两层**触发：可读前置校验，或 DB 部分唯一索引 ``uq_runs_single_active``
    命中 ``IntegrityError``（最终裁决）。
    """

    code = "ACTIVE_RUN_EXISTS"

    def __init__(self, message: str, *, source: str = "precheck") -> None:
        super().__init__(message)
        self.source = source


class IdempotencyConflictError(Exception):
    """同 ``Idempotency-Key`` 但 ``request_hash`` 不同（→ 409）。"""

    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            f"idempotency key {idempotency_key!r} reused with a different request_hash"
        )
        self.idempotency_key = idempotency_key


class RunValidationError(Exception):
    """批次创建参数不合法（→ 422）。"""

    code = "INVALID_RUN"


#: ``retry-failed`` 给每个失败成员追加的尝试额度（主文档 §6.4：+3 次）。
RETRY_FAILED_EXTRA_ATTEMPTS = 3

#: 允许 ``retry-failed`` 的 run 终态（主文档 §6.4：仅这两个）。
RETRY_FAILED_ALLOWED_STATUSES: tuple[str, ...] = ("completed_with_errors", "failed")

#: 可被 ``cancel`` 的 run 状态（主文档 §6.2：``ready/running/pausing/paused``）。
CANCELLABLE_RUN_STATUSES: tuple[str, ...] = ("ready", "running", "pausing", "paused")

#: ``cancel`` 已达目标状态（重复取消 → 200，不报 409）。
CANCEL_TARGET_STATUSES: tuple[str, ...] = ("cancelling", "cancelled")

#: ``pause`` 已达目标状态（重复暂停 → 200，不报 409）。
PAUSE_TARGET_STATUSES: tuple[str, ...] = ("pausing", "paused")


@dataclass(frozen=True)
class ControlOutcome:
    """一次控制动作的结果（服务层**不涉及 HTTP**；由 ``api/routes.py`` 映射状态码）。

    - ``status_code``：本次动作的 HTTP 语义（``202`` = 接受并改变了状态；``200`` = 读取/幂等）。
    - ``repeat``：``True`` 表示重复点击且 run 已达目标状态（幂等，未做实质改变）。
    """

    run: RunRecord
    status_code: int = 202
    repeat: bool = False


# ---------------------------------------------------------------------------
# 幂等 hash
# ---------------------------------------------------------------------------


def canonical_request_payload(
    *,
    survey_id: UUID,
    survey_revision: int,
    import_id: UUID,
    model_config_id: str,
    budget_limit: Decimal | None,
    budget_currency: str,
    request_limit: int | None,
) -> dict[str, Any]:
    """构造参与 ``request_hash`` 的规范化载荷（排序键、字符串化金额）。"""
    return {
        "survey_id": str(survey_id),
        "survey_revision": int(survey_revision),
        "import_id": str(import_id),
        "model_config_id": model_config_id,
        "budget_limit": str(budget_limit) if budget_limit is not None else None,
        "budget_currency": budget_currency,
        "request_limit": request_limit,
    }


def compute_request_hash(payload: Mapping[str, Any]) -> str:
    """``request_hash`` = 规范化 JSON 的 SHA-256（确定性）。"""
    blob = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _control_event(
    action: str,
    *,
    idempotency_key: str | None,
    request_hash: str,
    result: str,
    now: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    event: dict[str, Any] = {
        "action": action,
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "result": result,
        "at": moment.isoformat(),
    }
    if extra:
        event.update(extra)
    return event


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class RunService:
    """批次服务：创建 / 启动 / 收敛 / 读取。"""

    def __init__(self, repository: RunRepository | None = None) -> None:
        self._repo = repository or run_repository

    # -- 创建 ---------------------------------------------------------------

    async def create_run(
        self,
        session: AsyncSession,
        *,
        survey_id: UUID,
        survey_revision: int,
        import_id: UUID,
        model_config_id: str = "default",
        budget_limit: Decimal | None = None,
        budget_currency: str = "CNY",
        request_limit: int | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        settings: Settings | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        flush_hook: FlushHook | None = None,
    ) -> RunRecord:
        """创建 ``ready`` 批次并写入全部成员（同一事务）。

        - 同一事务写入冻结快照 + 全部成员；任一分块失败 → 全部回滚（不允许半批）。
        - 幂等：同 key 同 hash → 返回原 run；同 key 异 hash → :class:`IdempotencyConflictError`。
        - ``request_limit`` 默认 ``3 * sample_size``（由应用写入）。
        """
        active_settings = settings or get_settings()
        payload = canonical_request_payload(
            survey_id=survey_id,
            survey_revision=survey_revision,
            import_id=import_id,
            model_config_id=model_config_id,
            budget_limit=budget_limit,
            budget_currency=budget_currency,
            request_limit=request_limit,
        )
        effective_hash = request_hash or compute_request_hash(payload)

        if idempotency_key:
            existing = await self._repo.find_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return self._resolve_idempotent(existing, effective_hash, idempotency_key)

        survey = await survey_service.get(session, survey_id)
        if survey.revision != survey_revision:
            raise RevisionConflictError(survey_revision, survey.revision)

        snapshots = await load_snapshots(session, import_id)
        if not snapshots:
            raise RunValidationError(f"import {import_id} has no rows")
        sample_size = len(snapshots)

        effective_request_limit = (
            int(request_limit)
            if request_limit is not None
            else 3 * sample_size
        )
        if effective_request_limit < 1:
            raise RunValidationError("request_limit must be >= 1")

        source_version = snapshots[0].source_version or str(import_id)

        model_snapshot = active_settings.model_snapshot()
        model_snapshot["model_config_id"] = model_config_id

        run = RunRecord(
            id=uuid4(),
            survey_id=survey_id,
            survey_snapshot=frozen_snapshot(survey),
            import_id=import_id,
            model_snapshot=model_snapshot,
            prompt_version=PROMPT_VERSION,
            source_version=source_version,
            sample_size=sample_size,
            status="ready",
            pause_reason=None,
            budget_limit=budget_limit,
            budget_currency=budget_currency,
            actual_cost=Decimal("0"),
            reserved_cost=Decimal("0"),
            unknown_cost_count=0,
            request_limit=effective_request_limit,
            requests_reserved=0,
            control_events_json=[
                _control_event(
                    "create",
                    idempotency_key=idempotency_key,
                    request_hash=effective_hash,
                    result="ready",
                )
            ],
            idempotency_key=idempotency_key,
            request_hash=effective_hash,
        )

        members = [
            MemberDraft(
                row_no=snapshot.row_no,
                persona_id=snapshot.persona_id,
                persona_snapshot=snapshot.model_dump(mode="json"),
            )
            for snapshot in snapshots
        ]

        try:
            await self._repo.insert_run_with_members(
                session, run, members, chunk_size=chunk_size, flush_hook=flush_hook
            )
        except IntegrityError:
            await session.rollback()
            if idempotency_key:
                # 可能是并发同 key 创建：唯一约束把后者挡下 → 复用已存在 run。
                existing = await self._repo.find_by_idempotency_key(session, idempotency_key)
                if existing is not None:
                    return self._resolve_idempotent(existing, effective_hash, idempotency_key)
            raise
        return run

    @staticmethod
    def _resolve_idempotent(
        existing: RunRecord, request_hash: str, idempotency_key: str
    ) -> RunRecord:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(idempotency_key)
        return existing

    # -- 启动 ---------------------------------------------------------------

    async def start_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> RunRecord:
        """``ready → running``。

        两层活动批次唯一性：
        1. 前置可读校验（``SELECT … FOR UPDATE``，非竞态安全，仅作快速校验）。
        2. DB 部分唯一索引 ``uq_runs_single_active`` 兜底：命中 ``IntegrityError``
           → :class:`ActiveRunConflictError`（**不得泄漏 500**）。
        """
        moment = now or datetime.now(UTC)
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        if run.status != "ready":
            raise InvalidRunStateError(run.status, "ready")

        other = await self._repo.find_other_active_run(session, run_id)
        if other is not None:
            raise ActiveRunConflictError(
                f"another active run exists: {other}", source="precheck"
            )

        run.status = "running"
        if run.started_at is None:
            run.started_at = moment
        run.pause_reason = None
        run.control_events_json = list(run.control_events_json) + [
            _control_event(
                "start",
                idempotency_key=run.idempotency_key,
                request_hash=run.request_hash,
                result="running",
                now=moment,
            )
        ]
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ActiveRunConflictError(
                "uq_runs_single_active violated: another run became active",
                source="db_index",
            ) from exc
        return run

    # -- 控制动作（主文档 §6.2 / §6.4 / §8.2；architecture.md §3.1/§3.3/§6.8）----
    #
    # 所有控制动作都在**锁定 run 行的事务内**完成：``get_run_for_update`` 取行锁 →
    # 校验状态 → 变更 + 追加 ``control_events_json`` → 提交。行锁使并发控制动作串行化，
    # 审计事件随状态同一事务落库（**非内存**）。

    async def pause_run(
        self, session: AsyncSession, run_id: UUID, *, now: datetime | None = None
    ) -> ControlOutcome:
        """``running → pausing``（写 ``pause_reason='user'``）。

        - 置 ``pausing`` 后**同事务**调用 :meth:`RunRepository.converge_run`（与 ``cancel_run``
          对称，P2-1 修复 / TEAM-BRIEF §7.9.3）：语义依据 §6.4「全部返回或收敛后设 paused」
          —— **在途为 0 时该条件平凡成立**，应立即落 ``paused``。
        - 有在途时 ``converge_run`` 返回 ``None`` → 保持 ``pausing``（行为不变，等待 worker 收敛）。
        - HTTP 仍返回 **202**（命令已接受）；响应体状态可能是 ``pausing`` 或 ``paused``。
        - 幂等：已达 ``pausing``/``paused`` 的重复 ``pause`` 返回 **200**（非 409）；
          对 ``ready``/``completed``/``cancelled`` 等非法状态 →
          :class:`InvalidRunStateError`（409）。
        """
        moment = now or datetime.now(UTC)
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        if run.status == "running":
            run.status = "pausing"
            run.pause_reason = "user"
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "pause",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result="pausing",
                    now=moment,
                )
            ]
            await session.flush()
            # 无在途 → 同事务立即收敛 paused；有在途 → converge_run 返回 None，保持 pausing。
            await self._repo.converge_run(session, run_id, now=moment)
            await session.commit()
            await session.refresh(run)  # 响应体反映真实状态（pausing 或 paused）
            return ControlOutcome(run=run, status_code=202)

        if run.status in PAUSE_TARGET_STATUSES:
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "pause",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result=f"already_{run.status}",
                    now=moment,
                )
            ]
            await session.commit()
            return ControlOutcome(run=run, status_code=200, repeat=True)

        raise InvalidRunStateError(run.status, "running")

    async def resume_run(
        self, session: AsyncSession, run_id: UUID, *, now: datetime | None = None
    ) -> ControlOutcome:
        """``paused → running``（**同一成员与模型快照，只跑剩余任务**）。

        幂等：已 ``running`` 的重复 ``resume`` 返回 **200**；对 ``ready``/``completed``/
        ``cancelled`` 等非法状态 → :class:`InvalidRunStateError`（409）。
        恢复**不重建快照、不重置已成功成员**（只改 run 状态）。
        """
        moment = now or datetime.now(UTC)
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        if run.status == "paused":
            run.status = "running"
            run.pause_reason = None
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "resume",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result="running",
                    now=moment,
                )
            ]
            await session.commit()
            return ControlOutcome(run=run, status_code=202)

        if run.status == "running":
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "resume",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result="already_running",
                    now=moment,
                )
            ]
            await session.commit()
            return ControlOutcome(run=run, status_code=200, repeat=True)

        raise InvalidRunStateError(run.status, "paused")

    async def cancel_run(
        self, session: AsyncSession, run_id: UUID, *, now: datetime | None = None
    ) -> ControlOutcome:
        """``ready/running/pausing/paused → cancelled``（未开始成员随后转 ``cancelled``）。

        - **``ready`` 批次不经 ``cancelling``**（P1-1 结构性修复 / TEAM-BRIEF §7.9.1）：
          ``ready`` 是**非活动**状态，而 ``cancelling`` 是活动状态，写入后者会撞上部分唯一索引
          ``uq_runs_single_active``（另有批次 ``running`` 时）→ ``IntegrityError`` 逃逸 → 500。
          ``ready`` 无任何在途请求，故同事务**一次性**收敛到 ``cancelled``（不经中间态）。
        - ``running/pausing/paused`` 批次经 ``cancelling``；**无在途**时同事务立即收敛
          ``cancelled``（``converge_run`` 幂等），有在途时保持 ``cancelling`` 等待 worker 收敛。
        - 幂等：已达 ``cancelling``/``cancelled`` 的重复 ``cancel`` 返回 **200**；
          对终态（``completed``/``completed_with_errors``/``failed``）→ 409。
        - 在途请求允许完成并保存；**已成功答案保留**。
        - **防御兜底**（§7.3 A1，不得省略）：任何路径的 ``IntegrityError`` 一律映射为
          :class:`ActiveRunConflictError`（→ **409**），**绝不泄漏 500**。
        """
        moment = now or datetime.now(UTC)
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        if run.status == "ready":
            # 结构性修复：ready 无在途请求 → 同事务一次性收敛 cancelled，**不写活动态 cancelling**。
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "cancel",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result="cancelled",
                    now=moment,
                )
            ]
            try:
                await session.flush()  # 先落审计事件（仍在同一事务内）
                await self._repo.cancel_ready_run(session, run_id, now=moment)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ActiveRunConflictError(
                    "uq_runs_single_active violated while cancelling a ready run",
                    source="db_index",
                ) from exc
            await session.refresh(run)
            return ControlOutcome(run=run, status_code=202)

        if run.status in CANCELLABLE_RUN_STATUSES:  # running/pausing/paused（ready 已在上分支处理）
            run.status = "cancelling"
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "cancel",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result="cancelling",
                    now=moment,
                )
            ]
            try:
                await session.flush()
                # 无在途则立即收敛（有在途时 converge 保持 cancelling，等待 worker 收敛）。
                await self._repo.converge_run(session, run_id, now=moment)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ActiveRunConflictError(
                    "uq_runs_single_active violated while cancelling an active run",
                    source="db_index",
                ) from exc
            await session.refresh(run)
            return ControlOutcome(run=run, status_code=202)

        if run.status in CANCEL_TARGET_STATUSES:
            run.control_events_json = list(run.control_events_json) + [
                _control_event(
                    "cancel",
                    idempotency_key=run.idempotency_key,
                    request_hash=run.request_hash,
                    result=f"already_{run.status}",
                    now=moment,
                )
            ]
            await session.commit()
            await session.refresh(run)
            return ControlOutcome(run=run, status_code=200, repeat=True)

        raise InvalidRunStateError(run.status, "cancellable status")

    async def retry_failed(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        idempotency_key: str,
        request_hash: str | None = None,
        now: datetime | None = None,
    ) -> ControlOutcome:
        """仅重新安排 ``status='failed'`` 的失败成员（**不含** ``retry_wait``）。

        - 只处理 ``completed_with_errors``/``failed`` 的失败成员：给它们 **+3 次**尝试额度、
          回到 ``pending``；``attempt_count``/``attempt_no`` **保留并单调递增**。
        - run 转 ``ready``（``finished_at`` 清空，``started_at`` **保留**），需再显式 ``start``。
        - **`Idempotency-Key` 必填**；同 key 重复调用视为幂等重放（返回 200，**不多加额度**）；
          同 key 但 ``request_hash`` 不同 → :class:`IdempotencyConflictError`（409）。
        - 对 ``completed``（全成功）等非法状态 → :class:`InvalidRunStateError`（409）。
        """
        if not idempotency_key or not idempotency_key.strip():
            raise RunValidationError("Idempotency-Key header is required")

        moment = now or datetime.now(UTC)
        effective_hash = request_hash or compute_request_hash(
            {"action": "retry-failed", "run_id": str(run_id)}
        )
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        # 幂等重放：同 key 事件已存在 → 不重复加额度（先于状态校验，保证可在 ready 上重放）。
        for event in run.control_events_json or []:
            if (
                isinstance(event, dict)
                and event.get("action") == "retry-failed"
                and event.get("idempotency_key") == idempotency_key
            ):
                if event.get("request_hash") != effective_hash:
                    raise IdempotencyConflictError(idempotency_key)
                return ControlOutcome(run=run, status_code=200, repeat=True)

        if run.status not in RETRY_FAILED_ALLOWED_STATUSES:
            raise InvalidRunStateError(run.status, "completed_with_errors|failed")

        result = await session.execute(
            update(RunMember)
            .where(RunMember.run_id == run_id, RunMember.status == "failed")
            .values(
                status="pending",
                attempt_limit=RunMember.attempt_limit + RETRY_FAILED_EXTRA_ATTEMPTS,
                next_attempt_at=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )
        retried = int(result.rowcount or 0)

        run.status = "ready"
        run.pause_reason = None
        run.finished_at = None  # started_at **保留**（首次开始时间，审计用）
        run.control_events_json = list(run.control_events_json) + [
            _control_event(
                "retry-failed",
                idempotency_key=idempotency_key,
                request_hash=effective_hash,
                result="ready",
                now=moment,
                extra={"retried_members": retried},
            )
        ]
        await session.commit()
        await session.refresh(run)
        return ControlOutcome(run=run, status_code=202)

    async def update_budget(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        budget_limit: Decimal,
        budget_currency: str | None = None,
        request_limit: int | None = None,
        now: datetime | None = None,
    ) -> ControlOutcome:
        """``PATCH /runs/{id}/budget``：**只允许提高** ``budget_limit``，**不得改币种**。

        - 降低 ``budget_limit`` 或改变 ``budget_currency`` → :class:`RunValidationError`（422）。
        - 允许**提高** ``request_limit``（同样只在提高时）。
        - 记录旧值/新值/时间到 ``control_events_json``；**不得**改 ``survey_snapshot``/
          ``model_snapshot``/``sample_size``。
        """
        moment = now or datetime.now(UTC)
        run = await self._repo.get_run_for_update(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")

        if budget_currency is not None and budget_currency != run.budget_currency:
            raise RunValidationError(
                f"budget currency is fixed at {run.budget_currency!r}; changing it is not allowed"
            )

        old_budget = run.budget_limit
        if old_budget is not None and Decimal(budget_limit) < Decimal(old_budget):
            raise RunValidationError("budget_limit may only be increased")

        old_request_limit = run.request_limit
        if request_limit is not None and int(request_limit) < int(old_request_limit):
            raise RunValidationError("request_limit may only be increased")

        run.budget_limit = Decimal(budget_limit)
        if request_limit is not None:
            run.request_limit = int(request_limit)

        run.control_events_json = list(run.control_events_json) + [
            _control_event(
                "increase-budget",
                idempotency_key=run.idempotency_key,
                request_hash=run.request_hash,
                result="updated",
                now=moment,
                extra={
                    "old_budget_limit": str(old_budget) if old_budget is not None else None,
                    "new_budget_limit": str(run.budget_limit),
                    "old_request_limit": old_request_limit,
                    "new_request_limit": run.request_limit,
                },
            )
        ]
        await session.commit()
        await session.refresh(run)
        return ControlOutcome(run=run, status_code=200)

    # -- 收敛 / 读取 --------------------------------------------------------

    async def converge_run(
        self, session: AsyncSession, run_id: UUID, *, now: datetime | None = None
    ) -> str | None:
        """推进 run 终态（无 pending/running/retry_wait 后）。"""
        return await self._repo.converge_run(session, run_id, now=now)

    async def get_run(self, session: AsyncSession, run_id: UUID) -> RunRecord:
        run = await self._repo.get_run(session, run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return run

    async def survey_snapshot_model(self, run: RunRecord) -> SurveyInput:
        """把冻结的 ``survey_snapshot`` 还原为可渲染的 :class:`SurveyInput`。"""
        snapshot = copy.deepcopy(run.survey_snapshot)
        return SurveyInput.model_validate(
            {
                "title": snapshot["title"],
                "product": snapshot["product"],
                "question": snapshot["question"],
            }
        )


#: 默认服务实例（无状态）。
run_service = RunService()


__all__ = [
    "CANCEL_TARGET_STATUSES",
    "CANCELLABLE_RUN_STATUSES",
    "ActiveRunConflictError",
    "ControlOutcome",
    "IdempotencyConflictError",
    "InvalidRunStateError",
    "PAUSE_TARGET_STATUSES",
    "RETRY_FAILED_ALLOWED_STATUSES",
    "RETRY_FAILED_EXTRA_ATTEMPTS",
    "RunNotFoundError",
    "RunService",
    "RunValidationError",
    "canonical_request_payload",
    "compute_request_hash",
    "run_service",
]
