# SPDX-License-Identifier: GPL-3.0-or-later
"""五张业务表、索引与约束（主文档 §6.1 / architecture.md §2）。

表名 / 字段名 / 约束名一律照抄主文档，不得改名。

关键 DB 级不变量：
- ``uq_members_run_persona`` UNIQUE(run_id, persona_id) —— 每个成功成员只有一条有效答案。
- ``uq_members_run_row``     UNIQUE(run_id, row_no)     —— 与上者合起来保证
                              row_no ↔ persona_id 双射（N 进 N 出）。
- ``uq_runs_single_active``  部分唯一索引 ((true)) WHERE status IN (活动态)
                             —— 「同一时刻至多一个活动批次」的 DB 硬兜底（改判 A1）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: run 活动态（与 contracts.ACTIVE_RUN_STATUSES 一致；此处以字面量写在 SQL 片段中）。
ACTIVE_RUN_STATUSES_SQL = "('running','pausing','paused','cancelling')"


class Base(DeclarativeBase):
    """声明式基类。"""


class ImportRecord(Base):
    """``imports`` —— 用户表导入记录（仅保存映射后必要字段 + 内容 hash）。"""

    __tablename__ = "imports"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_hash: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_mapping_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshots_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "row_count >= 0 AND row_count <= 20000", name="ck_imports_row_count"
        ),
        Index("idx_imports_created_at", text("created_at DESC")),
    )


class SurveyRecord(Base):
    """``surveys`` —— 问卷草稿与版本。"""

    __tablename__ = "surveys"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    product_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    question_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_surveys_revision"),
    )


class RunRecord(Base):
    """``runs`` —— 批次（冻结配置 + 预算 + 幂等）。"""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    survey_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("surveys.id"), nullable=False
    )
    survey_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    import_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("imports.id"), nullable=False
    )
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="ready", server_default="ready"
    )
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    budget_currency: Mapped[str] = mapped_column(
        Text, nullable=False, default="CNY", server_default="CNY"
    )
    actual_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    reserved_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    unknown_cost_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    request_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    requests_reserved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    control_events_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "sample_size > 0 AND sample_size <= 20000", name="ck_runs_sample_size"
        ),
        CheckConstraint(
            "status IN ('ready','running','pausing','paused','cancelling','cancelled',"
            "'completed','completed_with_errors','failed')",
            name="ck_runs_status",
        ),
        CheckConstraint(
            "pause_reason IS NULL OR pause_reason IN "
            "('user','api_auth','api_unavailable','budget')",
            name="ck_runs_pause_reason",
        ),
        Index("idx_runs_status", "status"),
        Index("idx_runs_created_at", text("created_at DESC")),
        # 活动批次唯一性 —— DB 级兜底（改判 A1）。注意不能用 UNIQUE(status)。
        Index(
            "uq_runs_single_active",
            text("(true)"),
            unique=True,
            postgresql_where=text(
                f"status IN {ACTIVE_RUN_STATUSES_SQL}"
            ),
        ),
    )


class RunMember(Base):
    """``run_members`` —— 一个 persona 一条成员任务。"""

    __tablename__ = "run_members"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    persona_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    #: ``none_as_null=True``（P1-1 修复）：Python ``None`` 必须落成 **SQL NULL**，而非
    #: JSONB ``'null'``（后者是一个**真实值**，会使 ``answer_json IS NULL`` 过滤失效）。
    answer_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempt_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("row_no >= 1", name="ck_members_row_no"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','retry_wait','failed','cancelled')",
            name="ck_members_status",
        ),
        UniqueConstraint("run_id", "persona_id", name="uq_members_run_persona"),
        UniqueConstraint("run_id", "row_no", name="uq_members_run_row"),
        Index("idx_members_run_status_next", "run_id", "status", "next_attempt_at"),
        Index("idx_members_lease", "run_id", "lease_expires_at"),
        Index("idx_members_run_row", "run_id", "row_no"),
    )


class Attempt(Base):
    """``attempts`` —— 单次调用记录（含 usage / 费用 / 原始错误）。"""

    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("run_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running", server_default="running"
    )
    provider_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 同 :attr:`RunMember.answer_json`：``none_as_null=True`` 保证「无 usage」落成 SQL
    #: NULL，而非 JSONB ``'null'``（同类编码问题的预防性修复；现无 SQL 依赖其空值语义）。
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    reserved_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="ck_attempts_attempt_no"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','abandoned')",
            name="ck_attempts_status",
        ),
        CheckConstraint(
            "raw_output IS NULL OR length(raw_output) <= 20000",
            name="ck_attempts_raw_output_len",
        ),
        UniqueConstraint("member_id", "attempt_no", name="uq_attempts_member_no"),
        Index("idx_attempts_started", text("started_at DESC")),
        Index("idx_attempts_member", "member_id"),
    )


__all__ = [
    "ACTIVE_RUN_STATUSES_SQL",
    "Attempt",
    "Base",
    "ImportRecord",
    "RunMember",
    "RunRecord",
    "SurveyRecord",
]
