"""initial schema: imports / surveys / runs / run_members / attempts

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20

五张业务表 + CHECK + UNIQUE + 索引（含 uq_runs_single_active 部分唯一索引）。
表名/字段名/约束名照抄主文档 §6.1 与 architecture.md §2。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- imports ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE imports (
            id UUID PRIMARY KEY,
            file_hash TEXT NOT NULL,
            sheet_name TEXT,
            column_mapping_json JSONB NOT NULL,
            row_count INTEGER NOT NULL,
            snapshots_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_imports_row_count CHECK (row_count >= 0 AND row_count <= 20000)
        )
        """
    )
    op.execute("CREATE INDEX idx_imports_created_at ON imports (created_at DESC)")

    # --- surveys ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE surveys (
            id UUID PRIMARY KEY,
            title TEXT NOT NULL,
            product_json JSONB NOT NULL,
            question_json JSONB NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_surveys_revision CHECK (revision >= 1)
        )
        """
    )

    # --- runs ------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE runs (
            id UUID PRIMARY KEY,
            survey_id UUID NOT NULL REFERENCES surveys(id),
            survey_snapshot JSONB NOT NULL,
            import_id UUID NOT NULL REFERENCES imports(id),
            model_snapshot JSONB NOT NULL,
            prompt_version TEXT NOT NULL,
            source_version TEXT NOT NULL,
            sample_size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            pause_reason TEXT,
            budget_limit NUMERIC(18,6),
            budget_currency TEXT NOT NULL DEFAULT 'CNY',
            actual_cost NUMERIC(18,6) NOT NULL DEFAULT 0,
            reserved_cost NUMERIC(18,6) NOT NULL DEFAULT 0,
            unknown_cost_count INTEGER NOT NULL DEFAULT 0,
            request_limit INTEGER NOT NULL,
            requests_reserved INTEGER NOT NULL DEFAULT 0,
            control_events_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            idempotency_key TEXT UNIQUE,
            request_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            CONSTRAINT ck_runs_sample_size CHECK (sample_size > 0 AND sample_size <= 20000),
            CONSTRAINT ck_runs_status CHECK (status IN (
                'ready','running','pausing','paused','cancelling','cancelled',
                'completed','completed_with_errors','failed')),
            CONSTRAINT ck_runs_pause_reason CHECK (
                pause_reason IS NULL OR pause_reason IN
                ('user','api_auth','api_unavailable','budget'))
        )
        """
    )
    op.execute("CREATE INDEX idx_runs_status ON runs (status)")
    op.execute("CREATE INDEX idx_runs_created_at ON runs (created_at DESC)")
    # 活动批次唯一性 —— DB 级兜底（改判 A1）。注意：绝不能写成 UNIQUE(status)。
    op.execute(
        """
        CREATE UNIQUE INDEX uq_runs_single_active ON runs ((true))
          WHERE status IN ('running','pausing','paused','cancelling')
        """
    )

    # --- run_members -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE run_members (
            id UUID PRIMARY KEY,
            run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            row_no INTEGER NOT NULL,
            persona_id TEXT NOT NULL,
            persona_snapshot JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            answer_json JSONB,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_limit INTEGER NOT NULL DEFAULT 3,
            next_attempt_at TIMESTAMPTZ,
            lease_token UUID,
            lease_expires_at TIMESTAMPTZ,
            last_error_code TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_members_row_no CHECK (row_no >= 1),
            CONSTRAINT ck_members_status CHECK (status IN (
                'pending','running','succeeded','retry_wait','failed','cancelled')),
            CONSTRAINT uq_members_run_persona UNIQUE (run_id, persona_id),
            CONSTRAINT uq_members_run_row UNIQUE (run_id, row_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_members_run_status_next ON run_members (run_id, status, next_attempt_at)"
    )
    op.execute("CREATE INDEX idx_members_lease ON run_members (run_id, lease_expires_at)")
    op.execute("CREATE INDEX idx_members_run_row ON run_members (run_id, row_no)")

    # --- attempts --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE attempts (
            id UUID PRIMARY KEY,
            member_id UUID NOT NULL REFERENCES run_members(id) ON DELETE CASCADE,
            attempt_no INTEGER NOT NULL,
            lease_token UUID NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'running',
            provider_request_id TEXT,
            raw_output TEXT,
            error_code TEXT,
            usage_json JSONB,
            reserved_cost NUMERIC(18,6) NOT NULL DEFAULT 0,
            actual_cost NUMERIC(18,6),
            duration_ms INTEGER,
            CONSTRAINT ck_attempts_attempt_no CHECK (attempt_no >= 1),
            CONSTRAINT ck_attempts_status CHECK (status IN (
                'running','succeeded','failed','abandoned')),
            CONSTRAINT ck_attempts_raw_output_len CHECK (
                raw_output IS NULL OR length(raw_output) <= 20000),
            CONSTRAINT uq_attempts_member_no UNIQUE (member_id, attempt_no)
        )
        """
    )
    op.execute("CREATE INDEX idx_attempts_started ON attempts (started_at DESC)")
    op.execute("CREATE INDEX idx_attempts_member ON attempts (member_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS attempts")
    op.execute("DROP TABLE IF EXISTS run_members")
    op.execute("DROP INDEX IF EXISTS uq_runs_single_active")
    op.execute("DROP TABLE IF EXISTS runs")
    op.execute("DROP TABLE IF EXISTS surveys")
    op.execute("DROP TABLE IF EXISTS imports")
