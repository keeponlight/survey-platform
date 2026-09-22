# SPDX-License-Identifier: GPL-3.0-or-later
"""T5 报表与导出测试（主文档 §8.3 / PRD-v1 §5 / task-list.md T5）。

验收命令：``uv run pytest tests/test_reports.py -q``

固定用例：**5 档各 2 人 + 2 failed ⇒ planned=12 / valid=10 / 每档 20% /
Top-2-Box=40% / 均分=3 / coverage=10/12**。全部打真实 PostgreSQL（``survey_test``）。
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.contracts import OPTION_VALUES, ProductInput, QuestionInput, SurveyInput
from app.models import Attempt, RunMember, RunRecord
from app.personas.source import create_import
from app.reports.export import CSV_BOM, CSV_FIELDS, export_csv_bytes
from app.reports.service import report_service
from app.surveys.service import frozen_snapshot, survey_service
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING

PROMPT_VERSION = "purchase-intent-zh-v1"

#: 5 档各 2 人 → 10 个有效成员。
_FIXED_VALUES: list[str] = [value for value in OPTION_VALUES for _ in range(2)]


@pytest_asyncio.fixture
async def maker(engine, db_session):
    """读写用会话工厂；复用 ``db_session`` 的用例后清库。"""
    return async_sessionmaker(engine, expire_on_commit=False)


def _csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


def _member(
    row_no: int,
    *,
    status: str,
    value: str | None = None,
    age: int | None = None,
    city_tier: str | None = None,
    reason: str | None = None,
    persona_id: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    answer = None
    if value is not None:
        answer = {"question_id": "purchase_intent", "value": value}
        if reason is not None:
            answer["reason"] = reason
    return {
        "row_no": row_no,
        "persona_id": persona_id or f"p_{row_no:06d}",
        "snapshot": {
            "row_no": row_no,
            "persona_id": persona_id or f"p_{row_no:06d}",
            "profile_text": f"画像 {row_no}",
            "age": age,
            "city_tier": city_tier,
            "source_version": "v",
        },
        "status": status,
        "answer": answer,
        "error_code": error_code,
    }


async def seed_run(
    session, members: list[dict[str, Any]], *, status: str = "running"
) -> RunRecord:
    """直接在 DB 建一个含 ``members`` 的 run（用于统计口径的确定性构造）。"""
    survey = await survey_service.create(
        session,
        SurveyInput(
            title="t",
            product=ProductInput(
                name="p", description="d", price=Decimal("199"), price_unit="元"
            ),
            question=QuestionInput(),
        ),
    )
    record, _ = await create_import(
        session,
        data=_csv_bytes(len(members)),
        filename="p.csv",
        column_mapping=DEFAULT_COLUMN_MAPPING,
    )
    await session.flush()

    run = RunRecord(
        id=uuid4(),
        survey_id=survey.id,
        survey_snapshot=frozen_snapshot(survey),
        import_id=record.id,
        model_snapshot={"model": "mock-model", "config_hash": "h"},
        prompt_version=PROMPT_VERSION,
        source_version="v",
        sample_size=len(members),
        status=status,
        request_limit=3 * len(members),
        request_hash="test-hash",
    )
    session.add(run)
    await session.flush()

    for member in members:
        session.add(
            RunMember(
                run_id=run.id,
                row_no=member["row_no"],
                persona_id=member["persona_id"],
                persona_snapshot=member["snapshot"],
                status=member["status"],
                answer_json=member["answer"],
                attempt_count=1 if member["status"] in ("succeeded", "failed") else 0,
                last_error_code=member["error_code"],
            )
        )
    await session.flush()
    return run


def _fixed_members() -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    row_no = 0
    for value in _FIXED_VALUES:
        row_no += 1
        members.append(_member(row_no, status="succeeded", value=value))
    for _ in range(2):
        row_no += 1
        members.append(_member(row_no, status="failed", error_code="INVALID_OUTPUT"))
    return members


async def read_summary(engine, run_id):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        return await report_service.summary(session, run_id)


async def read_export(engine, run_id) -> bytes:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        return await export_csv_bytes(session, run_id)


# ---------------------------------------------------------------------------
# 固定用例口径
# ---------------------------------------------------------------------------


async def test_fixed_12_planned_10_valid(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.sample_size == 12
    assert summary.valid_count == 10
    assert summary.failed_count == 2
    assert sum(bucket.count for bucket in summary.buckets) == summary.valid_count


async def test_each_bucket_20pct(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert [bucket.value for bucket in summary.buckets] == list(OPTION_VALUES)
    for bucket in summary.buckets:
        assert bucket.count == 2
        assert bucket.rate == 0.2


async def test_top2box_40pct(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.top2box == pytest.approx(0.4)


async def test_mean_score_3(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.mean_score == pytest.approx(3.0)


async def test_coverage_10_of_12(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.coverage == pytest.approx(10 / 12)


async def test_failed_not_change_denominator(maker, engine, db_session) -> None:
    """失败行不改变分母（valid），五档计数之和恒等于 valid。"""
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.valid_count == 10  # 2 个 failed 不进分母
    assert sum(bucket.count for bucket in summary.buckets) == 10
    for bucket in summary.buckets:
        assert bucket.rate == 0.2  # 分母为 10，而非 12


async def test_zero_valid_returns_null(maker, engine, db_session) -> None:
    members = [
        _member(index + 1, status="failed", error_code="TIMEOUT") for index in range(4)
    ]
    run = await seed_run(db_session, members)
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.valid_count == 0
    assert all(bucket.count == 0 for bucket in summary.buckets)
    assert all(bucket.rate is None for bucket in summary.buckets)
    assert summary.top2box is None
    assert summary.mean_score is None


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------


async def test_age_city_grouping(maker, engine, db_session) -> None:
    members = [
        _member(1, status="succeeded", value="definitely_yes", age=20, city_tier="tier_1"),
        _member(2, status="succeeded", value="definitely_yes", age=28, city_tier="tier_2"),
        _member(3, status="succeeded", value="unsure", age=33, city_tier="tier_1"),
        _member(4, status="succeeded", value="definitely_not", age=50, city_tier="三线"),
        _member(5, status="succeeded", value="probably_yes", age=None, city_tier=None),
        _member(6, status="failed", error_code="INVALID_OUTPUT", age=22, city_tier="tier_2"),
    ]
    run = await seed_run(db_session, members)
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.age_groups is not None
    assert summary.city_groups is not None

    age_by_label = {group.label: group for group in summary.age_groups}
    assert set(age_by_label) == {"18-24", "25-29", "30-35", "其他", "未知"}
    assert age_by_label["18-24"].planned == 2  # 成员1 + 成员6（failed）
    assert age_by_label["18-24"].valid == 1
    assert age_by_label["18-24"].distribution["definitely_yes"] == 1
    assert age_by_label["未知"].planned == 1

    city_by_label = {group.label: group for group in summary.city_groups}
    assert city_by_label["一线"].planned == 2
    assert city_by_label["二线"].planned == 2
    assert city_by_label["其他"].planned == 1


async def test_grouping_hidden_when_column_absent(maker, engine, db_session) -> None:
    members = [
        _member(index + 1, status="succeeded", value="unsure", age=None, city_tier=None)
        for index in range(3)
    ]
    run = await seed_run(db_session, members)
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.age_groups is None
    assert summary.city_groups is None


# ---------------------------------------------------------------------------
# 重试不重复计数
# ---------------------------------------------------------------------------


async def test_retry_not_double_counted(maker, engine, db_session) -> None:
    """成员 1 首次 attempt 失败、第二次成功：valid 仍只算 1。"""
    run = await seed_run(
        db_session, [_member(1, status="succeeded", value="probably_yes")]
    )
    await db_session.flush()

    member = (
        await db_session.execute(
            RunMember.__table__.select().where(RunMember.run_id == run.id)
        )
    ).one()
    # 两条 attempt：先失败后成功（attempt_no 单调递增，不删记录）。
    db_session.add_all(
        [
            Attempt(
                id=uuid4(),
                member_id=member.id,
                attempt_no=1,
                lease_token=uuid4(),
                started_at=member.updated_at,
                status="failed",
                error_code="INVALID_OUTPUT",
                actual_cost=Decimal("0"),
            ),
            Attempt(
                id=uuid4(),
                member_id=member.id,
                attempt_no=2,
                lease_token=uuid4(),
                started_at=member.updated_at,
                status="succeeded",
                actual_cost=Decimal("0"),
            ),
        ]
    )
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    assert summary.valid_count == 1
    assert summary.buckets[3].value == "probably_yes"
    assert summary.buckets[3].count == 1


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------


async def test_csv_contains_all_12_rows(maker, engine, db_session) -> None:
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    data = await read_export(engine, run.id)
    assert data.startswith(CSV_BOM.encode("utf-8"))

    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert rows[0] == list(CSV_FIELDS)
    assert len(rows[0]) == 15
    assert len(rows) == 13  # 表头 + 12 成员

    # 失败行 value 与 score 为空（不用空值冒充中立答案）。
    failed_rows = [row for row in rows[1:] if row[5] == "failed"]
    assert len(failed_rows) == 2
    for row in failed_rows:
        assert row[7] == ""  # value
        assert row[8] == ""  # score
        assert row[11] == "INVALID_OUTPUT"  # error_code


async def test_csv_special_chars_escaped(maker, engine, db_session) -> None:
    reason = '含,逗号 与 "引号" 与\n换行'
    run = await seed_run(
        db_session,
        [_member(1, status="succeeded", value="unsure", reason=reason)],
    )
    await db_session.commit()

    data = await read_export(engine, run.id)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[1][9] == reason  # 往返一致，csv 已正确转义


async def test_csv_formula_prefix_escaped(maker, engine, db_session) -> None:
    run = await seed_run(
        db_session,
        [
            _member(
                1,
                status="succeeded",
                value="unsure",
                persona_id="=1+1",
                reason="@SUM(A1)",
                city_tier="+危险",
            )
        ],
    )
    await db_session.commit()

    data = await read_export(engine, run.id)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    row = rows[1]
    assert row[2] == "'=1+1"      # persona_id
    assert row[4] == "'+危险"     # city_tier
    assert row[9] == "'@SUM(A1)"  # reason


async def test_export_uses_same_snapshot_as_summary(maker, engine, db_session) -> None:
    """同一时刻 summary 与 export 使用同一份数据（口径可复核）。"""
    run = await seed_run(db_session, _fixed_members())
    await db_session.commit()

    summary = await read_summary(engine, run.id)
    data = await read_export(engine, run.id)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    succeeded_in_csv = [row for row in rows[1:] if row[5] == "succeeded"]
    assert len(succeeded_in_csv) == summary.valid_count
