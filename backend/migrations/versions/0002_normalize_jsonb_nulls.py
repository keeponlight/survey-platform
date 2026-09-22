"""normalize JSONB 'null' -> SQL NULL for nullable JSONB columns (P1-1)

Revision ID: 0002_normalize_jsonb_nulls
Revises: 0001_initial
Create Date: 2026-09-20

背景（P1-1，已实测）：
- ``'null'::jsonb IS NOT NULL`` = **true**，``NULL::jsonb IS NOT NULL`` = false。
- 修复前 ``app.models`` 的 ``JSONB`` 列未设 ``none_as_null=True`` → 由 ORM/Core 写入的
  Python ``None`` 落库为 JSONB ``'null'``（一个**真实值**），使
  ``run_members.answer_json IS NULL`` 之类的过滤失效。

本迁移把**既有**的 ``'null'::jsonb`` 行规整为 SQL ``NULL``：
- ``run_members.answer_json``（该列是本缺陷的原始受害列，见 reports/service.py 过滤）。
- ``attempts.usage_json``（同类可空 JSONB 列；无 SQL 空值依赖，一并归一以消除同类隐患）。

只影响「值为 JSONB ``'null'``」的行，不触碰任何非空答案/usage。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_normalize_jsonb_nulls"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSONB ``'null'`` 是真实值 → 归一为 SQL NULL（仅匹配确为 JSON null 的行）。
    op.execute(
        "UPDATE run_members SET answer_json = NULL WHERE answer_json = 'null'::jsonb"
    )
    op.execute("UPDATE attempts SET usage_json = NULL WHERE usage_json = 'null'::jsonb")


def downgrade() -> None:
    """不可逆迁移：JSONB ``'null'`` 与 SQL ``NULL`` 归一后无法还原原始形态。

    「把 SQL NULL 重新写成 JSONB ``'null'``」会重新引入 P1-1 缺陷（无答案成员被当作
    有值），故有意**不回滚**。
    """
    return
