# SPDX-License-Identifier: GPL-3.0-or-later
"""逐人 CSV 导出（主文档 §8.2 / §8.3 / PRD-v1 §4.4）。

硬约束：

- 字段**固定 15 列**：``run_id, row_no, persona_id, age, city_tier, member_status,
  question_id, value, score, reason, attempt_count, error_code, model, prompt_version, as_of``。
- **UTF-8 BOM**（Excel 友好）。
- **失败/待完成行 value 为空**（不用空值冒充中立答案）。
- 文本以 ``= + - @`` 开头时**转义**（前缀 ``'``），避免电子表格公式执行。
- **不导出完整画像**（``profile_text`` 不进 CSV）。
- 一致性快照（导出全程使用同一 ``REPEATABLE READ`` 事务）。
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import OPTION_SCORES, PURCHASE_INTENT_QUESTION_ID
from app.reports.service import ReportNotFoundError

#: CSV 列（顺序固定，禁止增删改）。
CSV_FIELDS: tuple[str, ...] = (
    "run_id",
    "row_no",
    "persona_id",
    "age",
    "city_tier",
    "member_status",
    "question_id",
    "value",
    "score",
    "reason",
    "attempt_count",
    "error_code",
    "model",
    "prompt_version",
    "as_of",
)

#: UTF-8 BOM（写入响应体最前面）。
CSV_BOM: str = "\ufeff"

#: CSV MIME 类型。
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

#: 会触发电子表格公式解释的前缀字符。
_FORMULA_PREFIXES = ("=", "+", "-", "@")

#: 流式导出每次读取的成员批大小。
DEFAULT_BATCH_SIZE = 1000


def escape_spreadsheet_prefix(value: str) -> str:
    """文本以 ``= + - @`` 开头时前缀 ``'``；其余原样返回。"""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


@dataclass(frozen=True)
class RunExportMeta:
    """导出所需的 run 元数据。"""

    run_id: UUID
    model: str
    prompt_version: str
    is_partial: bool


@dataclass(frozen=True)
class MemberExportRow:
    """一行成员导出数据（只含导出所需字段，**不含完整画像**）。"""

    row_no: int
    persona_id: str
    age: int | None
    city_tier: str | None
    member_status: str
    value: str | None
    score: int | None
    reason: str | None
    attempt_count: int
    error_code: str | None


async def load_run_meta(session: AsyncSession, run_id: UUID) -> RunExportMeta:
    """读取导出所需的 run 元数据；不存在抛 :class:`ReportNotFoundError`（→ 404）。"""
    result = await session.execute(
        text(
            "SELECT model_snapshot->>'model' AS model, prompt_version, status "
            "FROM runs WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )
    row = result.one_or_none()
    if row is None:
        raise ReportNotFoundError(f"run not found: {run_id}")
    model = row[0] or ""
    prompt_version = row[1] or ""
    status = row[2]
    is_partial = status in {"ready", "running", "pausing", "paused", "cancelling"}
    return RunExportMeta(
        run_id=run_id,
        model=str(model),
        prompt_version=str(prompt_version),
        is_partial=is_partial,
    )


def _int_or_empty(value: int | None) -> str:
    return "" if value is None else str(value)


def member_export_cells(
    meta: RunExportMeta, row: MemberExportRow, as_of: datetime
) -> list[str]:
    """把一行成员数据渲染为 15 个 CSV 单元格（失败行 value/score 为空）。"""
    return [
        str(meta.run_id),
        str(row.row_no),
        escape_spreadsheet_prefix(row.persona_id),
        _int_or_empty(row.age),
        escape_spreadsheet_prefix(row.city_tier or ""),
        row.member_status,
        PURCHASE_INTENT_QUESTION_ID,
        escape_spreadsheet_prefix(row.value or ""),
        _int_or_empty(row.score),
        escape_spreadsheet_prefix(row.reason or ""),
        str(row.attempt_count),
        escape_spreadsheet_prefix(row.error_code or ""),
        escape_spreadsheet_prefix(meta.model),
        meta.prompt_version,
        as_of.astimezone(UTC).isoformat(),
    ]


def _parse_member_row(raw: Sequence[Any]) -> MemberExportRow:
    row_no, persona_id, snapshot, status, answer_json, attempt_count, error_code = raw
    age = None
    city_tier = None
    if isinstance(snapshot, dict):
        raw_age = snapshot.get("age")
        if isinstance(raw_age, int):
            age = raw_age
        elif isinstance(raw_age, str) and raw_age.strip():
            try:
                age = int(float(raw_age))
            except ValueError:
                age = None
        raw_city = snapshot.get("city_tier")
        if isinstance(raw_city, str) and raw_city.strip():
            city_tier = raw_city

    value: str | None = None
    score: int | None = None
    reason: str | None = None
    if status == "succeeded" and isinstance(answer_json, dict):
        raw_value = answer_json.get("value")
        if isinstance(raw_value, str):
            value = raw_value
            score = OPTION_SCORES.get(raw_value)
        raw_reason = answer_json.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason

    return MemberExportRow(
        row_no=int(row_no),
        persona_id=str(persona_id),
        age=age,
        city_tier=city_tier,
        member_status=str(status),
        value=value,
        score=score,
        reason=reason,
        attempt_count=int(attempt_count),
        error_code=str(error_code) if error_code is not None else None,
    )


async def iter_export_rows(
    session: AsyncSession,
    run_id: UUID,
    *,
    as_of: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[list[str]]:
    """按 ``row_no`` 顺序逐行产出 15 列（同一一致性事务内分批读取）。

    调用方须保证 ``session`` 处于一致性读事务（``REPEATABLE READ``）。
    """
    moment = as_of or datetime.now(UTC)
    meta = await load_run_meta(session, run_id)
    last_row_no = 0
    while True:
        result = await session.execute(
            text(
                "SELECT row_no, persona_id, persona_snapshot, status, answer_json, "
                "attempt_count, last_error_code "
                "FROM run_members WHERE run_id = :run_id AND row_no > :last_row_no "
                "ORDER BY row_no LIMIT :limit"
            ),
            {"run_id": run_id, "last_row_no": last_row_no, "limit": batch_size},
        )
        rows = result.all()
        if not rows:
            break
        for raw in rows:
            yield member_export_cells(meta, _parse_member_row(raw), moment)
        last_row_no = int(rows[-1][0])
        if len(rows) < batch_size:
            break


def _csv_line(cells: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(cells))
    return buffer.getvalue()


async def export_csv_bytes(
    session: AsyncSession, run_id: UUID, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> bytes:
    """一次性返回完整 CSV（BOM + 表头 + 全部成员行）。测试/小批量用。"""
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    buffer = io.StringIO()
    buffer.write(CSV_BOM)
    buffer.write(_csv_line(CSV_FIELDS))
    async for row in iter_export_rows(session, run_id, batch_size=batch_size):
        buffer.write(_csv_line(row))
    return buffer.getvalue().encode("utf-8")


async def stream_csv(
    sessionmaker: Any,
    run_id: UUID,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[bytes]:
    """流式产出 CSV 字节块（供 ``StreamingResponse`` 使用）。

    自管会话生命周期；全程单一 ``REPEATABLE READ`` 一致性快照。
    """
    async with sessionmaker() as session:
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            yield (CSV_BOM + _csv_line(CSV_FIELDS)).encode("utf-8")
            chunk: list[str] = []
            async for row in iter_export_rows(session, run_id, batch_size=batch_size):
                chunk.append(_csv_line(row))
                if len(chunk) >= batch_size:
                    yield "".join(chunk).encode("utf-8")
                    chunk.clear()
            if chunk:
                yield "".join(chunk).encode("utf-8")
        finally:
            await session.rollback()


__all__ = [
    "CSV_BOM",
    "CSV_FIELDS",
    "CSV_MEDIA_TYPE",
    "DEFAULT_BATCH_SIZE",
    "MemberExportRow",
    "RunExportMeta",
    "escape_spreadsheet_prefix",
    "export_csv_bytes",
    "iter_export_rows",
    "load_run_meta",
    "member_export_cells",
    "stream_csv",
]
