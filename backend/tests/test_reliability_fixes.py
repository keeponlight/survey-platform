# SPDX-License-Identifier: GPL-3.0-or-later
"""P1-1 / P2-1 / P2-2 / P2-3 修复的回归测试（T3 复核缺陷批次）。

对应 ``docs/TEAM-BRIEF.md`` §7.6.2–§7.6.4 的裁定：

- **P1-1**：无答案成员的 ``answer_json`` 必须是 **SQL NULL**（而非 JSONB ``'null'``）；
  答复分布**无 ``NULL`` 桶**，且五档计数之和恒等于 ``valid``。
- **P2-1**：alembic ``fileConfig`` 不得禁用 ``app.worker`` 日志器。
- **P2-2**：并发槽位在**领取之前**获取 → DB 侧 ``attempts.status='running'`` 恒 ≤ 槽位上限
  （``consumers == cap`` 与 ``consumers > cap`` 两种配置）。
- **P2-3**：``SmoothRateLimiter.rebuild_from_history`` **幂等**（重复调用窗口不膨胀）。

全部打真实 PostgreSQL（``survey_test``，conftest 已 fail-fast）。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import pathlib
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_LOAD_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "load"
if str(_LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_LOAD_DIR))

from mock_provider import BarrierMockProvider, MockProvider  # noqa: E402

from app.config import Settings  # noqa: E402
from app.contracts import ProductInput, QuestionInput, SurveyInput  # noqa: E402
from app.personas.source import create_import  # noqa: E402
from app.reports.service import report_service  # noqa: E402
from app.runs.service import run_service  # noqa: E402
from app.surveys.service import survey_service  # noqa: E402
from app.worker.execute import RetryPolicy  # noqa: E402
from app.worker.limits import SmoothRateLimiter  # noqa: E402
from app.worker.main import Worker  # noqa: E402
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING  # noqa: E402

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = Settings.from_env().test_database_url
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0)


# ---------------------------------------------------------------------------
# 夹具与辅助（对齐 test_worker.py 的既有约定）
# ---------------------------------------------------------------------------


class FixEnv:
    """测试环境：引擎 + 会话工厂 + 自动回收的 worker 列表。"""

    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.workers: list[Worker] = []

    def worker(self, provider, **kwargs) -> Worker:
        worker = make_worker(self.engine, self.maker, provider, **kwargs)
        self.workers.append(worker)
        return worker


@pytest_asyncio.fixture
async def fix_env(prepared_test_db: None):
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=20)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    environment = FixEnv(engine, maker)
    try:
        yield environment
    finally:
        for worker in environment.workers:
            with contextlib.suppress(Exception):
                await worker.stop()
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


def make_settings(**overrides) -> Settings:
    base = Settings.from_env()
    return replace(
        base,
        database_url=TEST_DATABASE_URL,
        test_database_url=TEST_DATABASE_URL,
        model_rpm=1_000_000,
        model_tpm=1_000_000_000,
        **overrides,
    )


def make_worker(engine, maker, provider, *, concurrency=4, settings=None, **kwargs) -> Worker:
    active = settings or make_settings()
    return Worker(
        engine=engine,
        sessionmaker=maker,
        provider=provider,
        settings=active,
        concurrency=concurrency,
        lease_seconds=kwargs.pop("lease_seconds", 120.0),
        renew_interval=kwargs.pop("renew_interval", 20.0),
        scan_interval=kwargs.pop("scan_interval", 10.0),
        poll_interval=kwargs.pop("poll_interval", 0.01),
        lock_check_interval=kwargs.pop("lock_check_interval", 5.0),
        retry_policy=kwargs.pop("retry_policy", FAST_RETRY),
        **kwargs,
    )


def _csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


async def setup_run(maker, n: int, *, start: bool = True) -> UUID:
    """建一个含 n 个成员的 run（默认启动）。返回 run_id。"""
    async with maker() as session:
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
            data=_csv_bytes(n),
            filename="p.csv",
            column_mapping=DEFAULT_COLUMN_MAPPING,
        )
        await session.commit()
        run = await run_service.create_run(
            session,
            survey_id=survey.id,
            survey_revision=1,
            import_id=record.id,
            idempotency_key=uuid4().hex,
        )
        await session.commit()
        if start:
            await run_service.start_run(session, run.id)
        return run.id


async def _db_running_attempts(maker) -> int:
    async with maker() as session:
        value = await session.scalar(
            text("SELECT count(*) FROM attempts WHERE status = 'running'")
        )
    return int(value or 0)


# ===========================================================================
# P1-1：无答案必须是 SQL NULL；答复分布无 NULL 桶
# ===========================================================================


async def test_failed_member_answer_is_sql_null_not_json_null(fix_env: FixEnv) -> None:
    """恒非法输出 → failed：``answer_json`` 必须是 **SQL NULL**，不是 JSONB ``'null'``。

    **直接断言 SQL**（``answer_json IS NULL``），不用 ORM ``is None`` —— JSONB ``'null'``
    与 SQL NULL 经 ORM 反序列化都得到 ``None``，正是当年漏掉该 bug 的原因。
    """
    run_id = await setup_run(fix_env.maker, 1)
    provider = MockProvider(fixed_latency_s=0.0, invalid_personas={"p_000001"})
    worker = fix_env.worker(provider, concurrency=1)
    assert await worker.start() is True
    assert await worker.serve(run_id, timeout=30.0) == "failed"

    async with fix_env.maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, "
                    "answer_json IS NULL AS is_sql_null, "
                    "answer_json::text AS txt "
                    "FROM run_members WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one()
    status, is_sql_null, txt = row
    assert status == "failed"
    assert is_sql_null is True, "unanswered member must be SQL NULL, not JSONB 'null'"
    assert txt is None, f"expected SQL NULL rendering, got {txt!r}"


async def test_distribution_has_no_null_bucket(fix_env: FixEnv) -> None:
    """答复分布不出现 ``NULL`` 桶，且五档计数之和恒等于 ``valid``。"""
    run_id = await setup_run(fix_env.maker, 4)
    provider = MockProvider(fixed_latency_s=0.0, invalid_personas={"p_000002"})
    worker = fix_env.worker(provider, concurrency=4)
    assert await worker.start() is True
    assert await worker.serve(run_id, timeout=30.0) == "completed_with_errors"

    async with fix_env.maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT m.answer_json->>'value' AS value, count(*) AS n "
                    "FROM run_members m "
                    "WHERE m.run_id=:r AND m.answer_json IS NOT NULL "
                    "GROUP BY value ORDER BY value"
                ),
                {"r": run_id},
            )
        ).all()
        valid = await session.scalar(
            text(
                "SELECT count(*) FROM run_members WHERE run_id=:r AND status='succeeded'"
            ),
            {"r": run_id},
        )

    # failed 成员的 answer_json 是 SQL NULL → 不会进入分布；故无 NULL 桶。
    assert all(row[0] is not None for row in rows), f"NULL bucket appeared: {rows}"
    # 五档计数之和 == valid。
    assert sum(int(row[1]) for row in rows) == int(valid)
    assert int(valid) == 3

    # 报表服务口径复核：buckets 合计 == valid_count，无空 value 桶。
    async with fix_env.maker() as session:
        summary = await report_service.summary(session, run_id)
    assert summary.valid_count == 3
    assert summary.failed_count == 1
    assert all(bucket.value is not None for bucket in summary.buckets)
    assert sum(bucket.count for bucket in summary.buckets) == summary.valid_count


# ===========================================================================
# P2-1：alembic fileConfig 不得禁用 app.worker 日志器
# ===========================================================================


def test_alembic_migration_config_keeps_app_worker_logger_enabled() -> None:
    """运行 alembic ``env.py``（触发 ``fileConfig``）后，``app.worker`` 不得被禁用。"""
    from alembic import command
    from alembic.config import Config

    logger = logging.getLogger("app.worker")
    logger.disabled = False  # 复位为默认，模拟进程刚启动、迁移尚未运行

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")  # env.py 内即 fileConfig(config.config_file_name)

    assert logger.disabled is False, (
        "alembic fileConfig disabled the app.worker logger; "
        "env.py must pass disable_existing_loggers=False"
    )


def test_app_worker_logger_enabled_after_session_migrations(prepared_test_db: None) -> None:
    """会话级迁移（``prepared_test_db`` 已跑 alembic）之后，``app.worker`` 仍需可用。"""
    assert logging.getLogger("app.worker").disabled is False


# ===========================================================================
# P2-2：占槽在领取之前（DB 侧在途恒 ≤ 上限）
# ===========================================================================


async def test_inflight_never_exceeds_cap_when_consumers_equal_cap(fix_env: FixEnv) -> None:
    """``consumers == cap``：闸门期间 DB 侧 ``running`` 恒 ≤ cap。"""
    cap = 8
    run_id = await setup_run(fix_env.maker, 40)
    provider = BarrierMockProvider(target=cap, fixed_latency_s=0.0)
    worker = fix_env.worker(
        provider, concurrency=cap, settings=make_settings(agent_concurrency=cap)
    )
    assert worker.limiter.capacity == cap
    assert await worker.start() is True

    serve_task = asyncio.create_task(worker.serve(run_id, timeout=60.0))
    assert await provider.wait_reached(timeout=20.0)

    db_peak = 0
    for _ in range(15):
        db_peak = max(db_peak, await _db_running_attempts(fix_env.maker))
        await asyncio.sleep(0.02)
    assert provider.current == cap
    assert db_peak <= cap, f"DB running peak {db_peak} exceeded cap {cap}"

    await provider.open_gate()
    assert await serve_task == "completed"
    assert provider.peak <= cap


async def test_inflight_never_exceeds_cap_when_consumers_exceed_cap(fix_env: FixEnv) -> None:
    """``consumers > cap``：占槽先于领取 → DB 侧 ``running`` 仍恒 ≤ cap（结构性保证）。"""
    cap = 10
    consumers = 20
    run_id = await setup_run(fix_env.maker, 60)
    provider = BarrierMockProvider(target=cap, fixed_latency_s=0.0)
    worker = fix_env.worker(
        provider, concurrency=consumers, settings=make_settings(agent_concurrency=cap)
    )
    assert worker.limiter.capacity == cap  # min(consumers, agent_concurrency)
    assert await worker.start() is True

    serve_task = asyncio.create_task(worker.serve(run_id, timeout=90.0))
    assert await provider.wait_reached(timeout=30.0)

    samples: list[int] = []
    for _ in range(20):
        samples.append(await _db_running_attempts(fix_env.maker))
        await asyncio.sleep(0.02)
    assert max(samples) <= cap, f"DB running exceeded cap {cap}; samples={samples}"
    assert provider.current == cap
    assert provider.peak <= cap

    await provider.open_gate()
    assert await serve_task == "completed"


# ===========================================================================
# P2-3：限流窗口重建幂等
# ===========================================================================


def test_rebuild_from_history_is_idempotent() -> None:
    """同一批历史记录重复重建 → 窗口计数不变（2 → 2 → 2，而非 2 → 4 → 6）。"""
    limiter = SmoothRateLimiter(rpm=1000, tpm=1_000_000)
    now = datetime.now(UTC)
    records = [(now - timedelta(seconds=5), 10), (now - timedelta(seconds=10), 20)]

    added1 = limiter.rebuild_from_history(records)
    in1 = limiter.in_window_requests
    added2 = limiter.rebuild_from_history(records)
    in2 = limiter.in_window_requests
    added3 = limiter.rebuild_from_history(records)
    in3 = limiter.in_window_requests

    assert (added1, added2, added3) == (2, 2, 2)
    assert in1 == in2 == in3 == 2, f"non-idempotent rebuild: {in1} -> {in2} -> {in3}"
