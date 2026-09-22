# SPDX-License-Identifier: GPL-3.0-or-later
"""T3 批次创建、幂等与同事务性（主文档 §6.1 / §8.2 / architecture.md §6.8）。

验收命令：``uv run pytest tests/test_run_creation.py -q``

测试**直接驱动 service**（不经 HTTP）；全部打真实 PostgreSQL（``survey_test``）。
"""

from __future__ import annotations

import asyncio
import csv
import io
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.contracts import ProductInput, QuestionInput, SurveyInput
from app.models import ImportRecord
from app.personas.source import create_import
from app.runs.repository import run_repository
from app.runs.service import (
    ActiveRunConflictError,
    IdempotencyConflictError,
    RunService,
    run_service,
)
from app.surveys.service import survey_service
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING, write_personas_csv

#: 测试库 DSN（与 conftest 同源，避免与名为 ``tests`` 的包冲突）。
TEST_DATABASE_URL = Settings.from_env().test_database_url


# ---------------------------------------------------------------------------
# 测试夹具与辅助
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def big_engine(prepared_test_db: None):
    """大连接池引擎（并发 start 用例需要多条独立连接）。"""
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=40)
    try:
        yield engine
    finally:
        await engine.dispose()
        cleanup = create_async_engine(TEST_DATABASE_URL)
        async with cleanup.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE attempts, run_members, runs, surveys, imports "
                    "RESTART IDENTITY CASCADE"
                )
            )
        await cleanup.dispose()


def _csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


async def _make_survey(session) -> object:
    return await survey_service.create(
        session,
        SurveyInput(
            title="购买意向问卷",
            product=ProductInput(
                name="示例产品",
                description="用于演示的产品",
                price=Decimal("199.00"),
                price_unit="元/件",
            ),
            question=QuestionInput(),
        ),
    )


async def _make_import(session, n: int) -> ImportRecord:
    record, _ = await create_import(
        session,
        data=_csv_bytes(n),
        filename="personas.csv",
        column_mapping=DEFAULT_COLUMN_MAPPING,
    )
    return record


# ---------------------------------------------------------------------------
# 同事务建批
# ---------------------------------------------------------------------------


async def test_snapshot_and_members_same_txn(db_session) -> None:
    """冻结快照 + 全部成员在同一事务写入；快照为深拷贝，后续改草稿不污染它。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 5)
    await db_session.commit()

    run = await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="k-snapshot",
    )
    await db_session.commit()

    assert run.status == "ready"
    assert run.sample_size == 5
    assert run.survey_snapshot["revision"] == 1
    assert run.survey_snapshot["product"]["price"] == "199.00"
    assert run.source_version == str(record.id)

    members = await run_repository.list_members(db_session, run.id)
    assert len(members) == 5
    assert [m.row_no for m in members] == [1, 2, 3, 4, 5]
    assert all(m.status == "pending" for m in members)
    assert all(m.attempt_count == 0 for m in members)

    # 快照冻结：修改草稿不影响已创建 run 的快照。
    await survey_service.update(
        db_session, survey.id, expected_revision=1, product=SurveyInput(
            title="x",
            product=ProductInput(
                name="新名", description="d", price=Decimal("299.00"), price_unit="元"
            ),
            question=QuestionInput(),
        ),
    )
    await db_session.commit()
    refreshed = await run_service.get_run(db_session, run.id)
    assert refreshed.survey_snapshot["product"]["price"] == "199.00"


async def test_midway_failure_rolls_back(db_session) -> None:
    """中途失败必须全部回滚（不允许半批任务被执行）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 5)
    await db_session.commit()

    def fail_on_second_chunk(chunk_index: int) -> None:
        if chunk_index == 1:
            raise RuntimeError("simulated mid-way failure")

    with pytest.raises(RuntimeError):
        await run_service.create_run(
            db_session,
            survey_id=survey.id,
            survey_revision=1,
            import_id=record.id,
            idempotency_key="k-rollback",
            chunk_size=1,
            flush_hook=fail_on_second_chunk,
        )
    await db_session.rollback()

    total_runs = await db_session.scalar(text("SELECT count(*) FROM runs"))
    total_members = await db_session.scalar(text("SELECT count(*) FROM run_members"))
    assert int(total_runs) == 0
    assert int(total_members) == 0


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


async def test_idempotent_create_returns_same_run(db_session) -> None:
    """同 key + 同 request_hash → 返回原 run（不重复建批、不重复建成员）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 4)
    await db_session.commit()

    first = await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="dup-key",
        budget_limit=Decimal("100.0"),
    )
    await db_session.commit()

    second = await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="dup-key",
        budget_limit=Decimal("100.0"),
    )
    await db_session.commit()

    assert second.id == first.id
    assert int(await db_session.scalar(text("SELECT count(*) FROM runs"))) == 1
    assert int(await db_session.scalar(text("SELECT count(*) FROM run_members"))) == 4


async def test_same_key_diff_hash_conflict(db_session) -> None:
    """同 key + 异 request_hash → 冲突错误（T4 映射 409）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 3)
    await db_session.commit()

    await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="clash-key",
        budget_limit=Decimal("100.0"),
    )
    await db_session.commit()

    with pytest.raises(IdempotencyConflictError) as excinfo:
        await run_service.create_run(
            db_session,
            survey_id=survey.id,
            survey_revision=1,
            import_id=record.id,
            idempotency_key="clash-key",
            budget_limit=Decimal("200.0"),  # 不同 hash
        )
    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"


async def test_request_limit_default_is_3x_sample_size(db_session) -> None:
    """``request_limit`` 默认 = 3 × sample_size（由应用写入，DDL 无默认）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 7)
    await db_session.commit()

    run = await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="k-limit",
    )
    await db_session.commit()
    assert run.request_limit == 21
    assert run.requests_reserved == 0


async def test_20000_members_created_in_single_txn(db_session, tmp_path) -> None:
    """20,000 成员分块 insert 但同一事务提交；行号连续、persona_id 唯一。"""
    survey = await _make_survey(db_session)
    csv_path = write_personas_csv(tmp_path / "personas_20000.csv", 20_000)
    record, _ = await create_import(
        db_session,
        data=csv_path.read_bytes(),
        filename=csv_path.name,
        column_mapping=DEFAULT_COLUMN_MAPPING,
    )
    await db_session.commit()
    assert record.row_count == 20_000

    run = await run_service.create_run(
        db_session,
        survey_id=survey.id,
        survey_revision=1,
        import_id=record.id,
        idempotency_key="k-20000",
        chunk_size=1000,
    )
    await db_session.commit()

    assert run.sample_size == 20_000
    assert run.request_limit == 60_000
    count = await run_repository.count_members(db_session, run.id)
    assert count == 20_000

    row_min = await db_session.scalar(
        text("SELECT min(row_no) FROM run_members WHERE run_id = :rid"), {"rid": run.id}
    )
    row_max = await db_session.scalar(
        text("SELECT max(row_no) FROM run_members WHERE run_id = :rid"), {"rid": run.id}
    )
    distinct_personas = await db_session.scalar(
        text("SELECT count(DISTINCT persona_id) FROM run_members WHERE run_id = :rid"),
        {"rid": run.id},
    )
    assert int(row_min) == 1
    assert int(row_max) == 20_000
    assert int(distinct_personas) == 20_000


# ---------------------------------------------------------------------------
# 活动批次唯一性（改判 A1）
# ---------------------------------------------------------------------------


async def test_concurrent_start_only_one_active_run(db_session, big_engine) -> None:
    """并发发起 N 个 start：恰好 1 个成功，其余全部冲突（uq_runs_single_active 兜底）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 3)
    await db_session.commit()

    run_ids = []
    for index in range(4):
        run = await run_service.create_run(
            db_session,
            survey_id=survey.id,
            survey_revision=1,
            import_id=record.id,
            idempotency_key=f"start-{index}",
        )
        run_ids.append(run.id)
    await db_session.commit()

    maker = async_sessionmaker(big_engine, expire_on_commit=False)

    async def start_one(run_id):
        async with maker() as session:
            return await RunService().start_run(session, run_id)

    results = await asyncio.gather(
        *(start_one(run_id) for run_id in run_ids), return_exceptions=True
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ActiveRunConflictError)]
    assert len(successes) == 1, f"expected exactly 1 success, got {results}"
    assert len(conflicts) == 3
    assert all(
        not isinstance(r, Exception) or isinstance(r, ActiveRunConflictError) for r in results
    )

    async with maker() as session:
        active = await session.scalar(
            text(
                "SELECT count(*) FROM runs WHERE status IN "
                "('running','pausing','paused','cancelling')"
            )
        )
    assert int(active) == 1


async def test_start_with_existing_active_run_conflicts(db_session) -> None:
    """已存在活动批次时启动新 run → 冲突（前置校验）。"""
    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 2)
    await db_session.commit()

    first = await run_service.create_run(
        db_session, survey_id=survey.id, survey_revision=1, import_id=record.id,
        idempotency_key="a",
    )
    second = await run_service.create_run(
        db_session, survey_id=survey.id, survey_revision=1, import_id=record.id,
        idempotency_key="b",
    )
    await db_session.commit()

    await run_service.start_run(db_session, first.id)
    with pytest.raises(ActiveRunConflictError):
        await run_service.start_run(db_session, second.id)


async def test_start_non_ready_run_invalid_state(db_session) -> None:
    """对已 running 的 run 再次 start → 非法状态（T4 映射 409）。"""
    from app.runs.service import InvalidRunStateError

    survey = await _make_survey(db_session)
    record = await _make_import(db_session, 2)
    await db_session.commit()
    run = await run_service.create_run(
        db_session, survey_id=survey.id, survey_revision=1, import_id=record.id,
        idempotency_key="c",
    )
    await db_session.commit()
    await run_service.start_run(db_session, run.id)

    with pytest.raises(InvalidRunStateError):
        await run_service.start_run(db_session, run.id)


async def test_missing_run_start_raises_not_found(db_session) -> None:
    from app.runs.service import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        await run_service.start_run(db_session, uuid4())
