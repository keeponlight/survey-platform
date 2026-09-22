# SPDX-License-Identifier: GPL-3.0-or-later
"""T3 对抗性独立复核（QA / 严过关）。

本文件**不复用**工程师的 ``BarrierMockProvider``：自带 :class:`QABarrierProvider`
（共享计数器 + ``asyncio.Event``）并**同时从数据库侧交叉采样**，用来证伪
「并发上限 / 租约 CAS / 崩溃恢复 / 预算不漏账」等不可见属性。

覆盖：
- 并发上限：既受构造参数约束、又受配置约束；DB ``attempts.status='running'`` 与
  mock 在途计数**逐点一致**且恒定 ≤ 上限（真并发，非「假跑」）。
- 限流窗口重建**读表**而非内存（删掉 attempts → 窗口归零；插入 → 计入）。
- 租约 CAS：迟到结果不产生第二条有效答案，迟到**已知**计费如实结算。
- 重试耗尽**绝不补默认答案**：终态 failed、answer_json 为 NULL、attempt_count 封顶 3、
  run 收敛 ``failed`` / ``completed_with_errors``、全程无 ``unsure``。
- 建批原子性：跨**独立连接**核对「失败后无任何该 run 的行被提交」。
- 单 worker 锁：第二个 worker 留 ERROR 日志并 ``serve`` 立即返回 ``locked``（非空转）。
- ``retry_wait`` 的未来 ``next_attempt_at`` 不占并发位、不被提前领取。

全部打真实 PostgreSQL（``survey_test``，conftest 已 fail-fast）。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import pathlib
import sys
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_LOAD_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "load"
if str(_LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_LOAD_DIR))

from mock_provider import MockProvider  # noqa: E402

from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    ModelRequest,
    ModelResponse,
    ProductInput,
    QuestionInput,
    SurveyInput,
)
from app.personas.source import create_import  # noqa: E402
from app.runs.repository import run_repository  # noqa: E402
from app.runs.service import run_service  # noqa: E402
from app.surveys.service import survey_service  # noqa: E402
from app.worker.execute import RetryPolicy  # noqa: E402
from app.worker.limits import (  # noqa: E402
    SmoothRateLimiter,
    build_request_for_persona,
    reservation_for_request,
)
from app.worker.main import ADVISORY_LOCK_KEY, Worker  # noqa: E402
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING  # noqa: E402

TEST_DATABASE_URL = Settings.from_env().test_database_url
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class QaEnv:
    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.workers: list[Worker] = []

    def worker(self, provider, **kwargs) -> Worker:
        worker = make_worker(self.engine, self.maker, provider, **kwargs)
        self.workers.append(worker)
        return worker


@pytest_asyncio.fixture
async def qa_env(prepared_test_db: None):
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=20)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    environment = QaEnv(engine, maker)
    try:
        yield environment
    finally:
        import contextlib

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
        scan_interval=kwargs.pop("scan_interval", 0.02),
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


async def setup_run(
    maker,
    n: int,
    *,
    start: bool = True,
    budget_limit: Decimal | None = None,
    request_limit: int | None = None,
) -> UUID:
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
            budget_limit=budget_limit,
            request_limit=request_limit,
        )
        await session.commit()
        if start:
            await run_service.start_run(session, run.id)
        return run.id


def reservation_fn_for(settings: Settings, run_snapshot: dict, allow_reason: bool = True):
    survey = SurveyInput.model_validate(
        {
            "title": run_snapshot["title"],
            "product": run_snapshot["product"],
            "question": run_snapshot["question"],
        }
    )

    def reservation_fn(persona_snapshot: dict) -> Decimal:
        request = build_request_for_persona(
            survey=survey,
            persona_snapshot=persona_snapshot,
            model=settings.model_name,
            max_output_tokens=settings.model_max_output_tokens,
            timeout_seconds=settings.model_timeout_seconds,
            allow_reason=allow_reason,
        )
        return reservation_for_request(
            request,
            input_price_per_million=settings.input_price_per_million,
            output_price_per_million=settings.output_price_per_million,
        )

    return reservation_fn


# ---------------------------------------------------------------------------
# 独立并发探测（自带计数器 + 数据库侧交叉采样）
# ---------------------------------------------------------------------------


def _value_for(persona_id: str) -> str:
    digest = hashlib.sha256(persona_id.encode("utf-8")).hexdigest()
    return OPTION_VALUES[int(digest[:8], 16) % len(OPTION_VALUES)]


class QABarrierProvider:
    """QA 自带 barrier provider：记录在途 current/peak/started，闸门可控。"""

    provider_name = "qa-barrier"

    def __init__(self, *, target: int, latency: float = 0.0) -> None:
        self._target = target
        self._latency = latency
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Event] = []
        self._current = 0
        self._peak = 0
        self._started = 0
        self._open = False
        self._reached = asyncio.Event()

    @property
    def current(self) -> int:
        return self._current

    @property
    def peak(self) -> int:
        return self._peak

    @property
    def started(self) -> int:
        return self._started

    async def answer(self, request: ModelRequest) -> ModelResponse:
        pid = str(json.loads(request.user_message)["persona_snapshot"]["persona_id"])
        waiter: asyncio.Event | None = None
        async with self._lock:
            if not self._open:
                waiter = asyncio.Event()
                self._waiters.append(waiter)
                self._current += 1
                self._started += 1
                self._peak = max(self._peak, self._current)
                if self._current >= self._target:
                    self._reached.set()
        if waiter is not None:
            await waiter.wait()
            async with self._lock:
                self._current -= 1
        if self._latency:
            await asyncio.sleep(self._latency)
        return ModelResponse(
            raw_text=json.dumps(
                {"question_id": PURCHASE_INTENT_QUESTION_ID, "value": _value_for(pid)},
                ensure_ascii=False,
            ),
            input_tokens=10,
            output_tokens=5,
            provider_request_id=f"qa-{pid}",
            duration_ms=0,
        )

    async def wait_reached(self, timeout: float = 20.0) -> bool:
        try:
            await asyncio.wait_for(self._reached.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def release_all(self) -> None:
        async with self._lock:
            self._open = True
            for event in self._waiters:
                event.set()
            self._waiters.clear()


async def _db_running_attempts(maker) -> int:
    async with maker() as session:
        value = await session.scalar(
            text("SELECT count(*) FROM attempts WHERE status = 'running'")
        )
    return int(value or 0)


async def _db_running_members(maker, run_id: UUID) -> int:
    async with maker() as session:
        value = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND status='running'"),
            {"r": run_id},
        )
    return int(value or 0)


# ===========================================================================
# 1) 并发上限：受构造参数约束，且 DB 侧在途 == mock 侧在途
# ===========================================================================


async def test_qa_concurrency_cap_tracks_constructed_limit_with_db_crosscheck(
    qa_env: QaEnv,
) -> None:
    """capacity = min(concurrency, agent_concurrency)；DB 侧在途数逐点 == mock 计数 == 上限。"""
    cap = 7
    run_id = await setup_run(qa_env.maker, 60)
    provider = QABarrierProvider(target=cap)
    worker = qa_env.worker(provider, concurrency=cap)  # agent_concurrency=100 → min=7
    assert worker.limiter.capacity == cap
    assert await worker.start() is True

    serve_task = asyncio.create_task(worker.serve(run_id, timeout=60.0))
    assert await provider.wait_reached(), (
        f"barrier never reached target={cap} (current={provider.current})"
    )

    # 闸门关着 → 反复采样 DB：必须恒等于 mock 在途数，且 == cap。
    observed: set[int] = set()
    for _ in range(12):
        observed.add(await _db_running_attempts(qa_env.maker))
        observed.add(await _db_running_members(qa_env.maker, run_id))
        await asyncio.sleep(0.02)
    assert provider.current == cap
    assert provider.peak == cap
    assert observed == {cap}, f"DB-side in-flight mismatch: {sorted(observed)} (mock={cap})"

    await provider.release_all()
    assert await serve_task == "completed"
    assert provider.peak == cap  # 全程未越界

    async with qa_env.maker() as session:
        succeeded = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id=:r AND status='succeeded'"),
            {"r": run_id},
        )
    assert int(succeeded) == 60


async def test_qa_inflight_never_exceeds_100_at_db_level(qa_env: QaEnv) -> None:
    """300 成员、默认 100 并发：DB 侧 ``status='running'`` 峰值恒 ≤ 100 且 == mock。"""
    run_id = await setup_run(qa_env.maker, 300)
    provider = QABarrierProvider(target=100)
    worker = qa_env.worker(provider, concurrency=100)
    assert worker.limiter.capacity == 100
    assert await worker.start() is True

    serve_task = asyncio.create_task(worker.serve(run_id, timeout=90.0))
    assert await provider.wait_reached(timeout=30.0)

    db_peak = 0
    for _ in range(15):
        db_peak = max(db_peak, await _db_running_attempts(qa_env.maker))
        await asyncio.sleep(0.02)
    assert db_peak <= 100, f"DB-level in-flight exceeded cap: {db_peak}"
    assert db_peak == provider.current == 100

    await provider.release_all()
    assert await serve_task == "completed"
    assert provider.peak <= 100

    async with qa_env.maker() as session:
        answered = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id=:r AND answer_json IS NOT NULL"),
            {"r": run_id},
        )
        distinct = await session.scalar(
            text(
                "SELECT count(DISTINCT (persona_id, answer_json)) FROM run_members "
                "WHERE run_id=:r AND answer_json IS NOT NULL"
            ),
            {"r": run_id},
        )
    assert int(answered) == 300
    assert int(distinct) == 300


# ===========================================================================
# 2) 限流窗口重建：读表，不是内存
# ===========================================================================


async def test_qa_rate_window_rebuild_is_table_driven(qa_env: QaEnv) -> None:
    """删掉 attempts → 窗口归零（证明读表）；按 started_at 计入窗口内记录。"""
    run_id = await setup_run(qa_env.maker, 6)
    async with qa_env.maker() as session:
        async with session.begin():
            await run_repository.claim_members(
                session, run_id=run_id, limit=6, lease_seconds=120.0
            )

    # 3 条最近（10s 前）+ 3 条窗口外（3600s 前）。
    async with qa_env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE attempts SET started_at = now() - interval '10 seconds' "
                    "WHERE id IN (SELECT id FROM attempts ORDER BY id LIMIT 3)"
                )
            )
            await session.execute(
                text(
                    "UPDATE attempts SET started_at = now() - interval '3600 seconds' "
                    "WHERE id IN (SELECT id FROM attempts ORDER BY id DESC LIMIT 3)"
                )
            )

    limiter = SmoothRateLimiter(rpm=1000, tpm=1_000_000)
    worker = qa_env.worker(MockProvider(fixed_latency_s=0.0), rate_limiter=limiter)
    added = await worker.rebuild_rate_window()
    assert added == 3, f"expected 3 in-window records, got {added}"
    assert limiter.in_window_requests == 3

    # 证伪「读内存」：把 attempts 全删掉，新 limiter 重建必须为 0。
    async with qa_env.maker() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM attempts"))
    limiter2 = SmoothRateLimiter(rpm=1000, tpm=1_000_000)
    worker2 = qa_env.worker(MockProvider(fixed_latency_s=0.0), rate_limiter=limiter2)
    added2 = await worker2.rebuild_rate_window()
    assert added2 == 0, "rebuild used in-memory state instead of the attempts table"
    assert limiter2.in_window_requests == 0


# ===========================================================================
# 3) 租约 CAS：迟到结果不产生第二条答案，迟到已知计费如实结算
# ===========================================================================


async def test_qa_stale_result_no_second_answer_and_late_cost_settled(qa_env: QaEnv) -> None:
    """过期→abandoned 后，用原 claim 迟到提交：CAS 丢弃（无第二条答案），已知计费结算。"""
    run_id = await setup_run(qa_env.maker, 1)
    worker = qa_env.worker(MockProvider(fixed_latency_s=0.0))
    assert await worker.start() is True

    async with qa_env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=1, lease_seconds=120.0
            )
    claim = result.claims[0]

    # 租约过期 → abandoned（保留预留）。
    async with qa_env.maker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE run_members SET lease_expires_at = now() - interval '5 seconds'")
            )
    await worker.scan_expired_once(run_id)

    late_answer = {"question_id": "purchase_intent", "value": "definitely_yes"}
    late_cost = Decimal("0.000123")
    async with qa_env.maker() as session:
        async with session.begin():
            saved = await run_repository.finalize_success(
                session,
                claim=claim,  # 原 token（已失租）
                answer_json=late_answer,
                actual_cost=late_cost,
                usage_json={"input_tokens": 10, "output_tokens": 5},
            )
    assert saved is False  # CAS 0 行 → 丢弃

    async with qa_env.maker() as session:
        member = (
            await session.execute(
                text("SELECT status, answer_json FROM run_members WHERE id=:m"),
                {"m": claim.member_id},
            )
        ).one()
        attempt = (
            await session.execute(
                text("SELECT status, actual_cost, reserved_cost FROM attempts WHERE id=:a"),
                {"a": claim.attempt_id},
            )
        ).one()
        run = await run_repository.get_run(session, run_id)

    assert member[0] == "retry_wait"  # 成员按剩余次数转 retry_wait，未被迟到结果改回
    assert member[1] is None  # 绝不产生第二条答案
    assert attempt[0] == "abandoned"
    assert attempt[1] == late_cost  # 迟到**已知**计费被如实结算到 attempt
    # run 级：该次预留被释放、实际支出累加（不重复）。
    assert run.actual_cost == late_cost


# ===========================================================================
# 4) 重试耗尽：绝不补默认答案
# ===========================================================================


async def test_qa_retry_exhaustion_never_fills_default(qa_env: QaEnv) -> None:
    """恒非法输出的成员跑到底：failed、answer_json=NULL、attempt_count=3、无 unsure。"""
    run_id = await setup_run(qa_env.maker, 2)
    provider = MockProvider(fixed_latency_s=0.0, invalid_personas={"p_000002"})
    worker = qa_env.worker(provider, concurrency=2)
    assert await worker.start() is True

    status = await worker.serve(run_id, timeout=30.0)
    assert status == "completed_with_errors"

    async with qa_env.maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT persona_id, status, answer_json, attempt_count FROM run_members "
                    "WHERE run_id=:r ORDER BY row_no"
                ),
                {"r": run_id},
            )
        ).all()
        all_answers = await session.scalar(
            text(
                "SELECT string_agg(answer_json::text, '|') FROM run_members WHERE run_id=:r"
            ),
            {"r": run_id},
        )
    by = {row[0]: row for row in rows}

    assert by["p_000001"][1] == "succeeded"
    bad = by["p_000002"]
    assert bad[1] == "failed"
    assert bad[2] is None  # 绝不补默认答案
    assert int(bad[3]) == 3  # 尝试次数封顶
    assert "unsure" not in (all_answers or "")


async def test_qa_all_invalid_run_converges_to_failed(qa_env: QaEnv) -> None:
    """全部成员恒非法 → run 收敛为 ``failed``（非 completed），且无任何答案。"""
    run_id = await setup_run(qa_env.maker, 1)
    provider = MockProvider(fixed_latency_s=0.0, invalid_personas={"p_000001"})
    worker = qa_env.worker(provider, concurrency=1)
    assert await worker.start() is True

    status = await worker.serve(run_id, timeout=30.0)
    assert status == "failed"

    async with qa_env.maker() as session:
        # P1-1 修复后，未作答成员的 ``answer_json`` 已是 SQL NULL；这里用**语义化**的
        # 「无 value」判定（两种编码下均成立），断言冻结链路上无任何补值产出。
        answered = await session.scalar(
            text(
                "SELECT count(*) FROM run_members WHERE run_id=:r "
                "AND answer_json->>'value' IS NOT NULL"
            ),
            {"r": run_id},
        )
        diag = (
            await session.execute(
                text(
                    "SELECT persona_id,status,answer_json,attempt_count,last_error_code "
                    "FROM run_members WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).all()
        total = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id=:r"), {"r": run_id}
        )
    assert int(answered) == 0, f"answered={answered} total={total} rows={diag}"


async def test_qa_no_answer_is_semantically_null_and_sql_null(qa_env: QaEnv) -> None:
    """P1-1 **已修复**：failed 成员的 ``answer_json`` 语义与物理上都是 SQL NULL。

    修复前（QA 记录）：JSONB 列未设 ``none_as_null=True`` → 无答案落成 JSONB ``'null'``
    （``answer_json IS NULL`` = false）。修复后：``answer_json IS NULL`` = **true**，
    故任何以 SQL ``IS NULL`` 判「未答复」的代码（T4 接口 / 导出）语义正确。
    """
    run_id = await setup_run(qa_env.maker, 1)
    provider = MockProvider(fixed_latency_s=0.0, invalid_personas={"p_000001"})
    worker = qa_env.worker(provider, concurrency=1)
    assert await worker.start() is True
    assert await worker.serve(run_id, timeout=30.0) == "failed"

    async with qa_env.maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, answer_json::text AS txt, "
                    "answer_json IS NULL AS sql_null, "
                    "answer_json->>'value' IS NULL AS no_value "
                    "FROM run_members WHERE run_id=:r"
                ),
                {"r": run_id},
            )
        ).one()
    status, txt, sql_null, no_value = row
    assert status == "failed"
    assert no_value is True  # 语义上确实「无答案」
    assert sql_null is True  # 修复后：物理上是 SQL NULL
    assert txt is None  # 不再是字符串 'null'


# ===========================================================================
# 5) 建批原子性：跨独立连接核对「无半批被提交」
# ===========================================================================


async def test_qa_batch_atomicity_no_partial_commit(qa_env: QaEnv) -> None:
    """分块中途失败 → 从**另一条连接**看到的 runs/run_members 均为 0（无半批）。"""
    async with qa_env.maker() as session:
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
            data=_csv_bytes(8),
            filename="p.csv",
            column_mapping=DEFAULT_COLUMN_MAPPING,
        )
        await session.commit()
        survey_id, import_id = survey.id, record.id

    def boom(chunk_index: int) -> None:
        if chunk_index == 2:
            raise RuntimeError("qa injected mid-way failure")

    async with qa_env.maker() as session:
        with pytest.raises(RuntimeError):
            await run_service.create_run(
                session,
                survey_id=survey_id,
                survey_revision=1,
                import_id=import_id,
                idempotency_key="qa-atomic",
                chunk_size=1,
                flush_hook=boom,
            )
        await session.rollback()

    # 独立连接（全新会话）核对：不能看到任何该批次残留。
    async with qa_env.maker() as session:
        runs = await session.scalar(
            text("SELECT count(*) FROM runs WHERE idempotency_key='qa-atomic'")
        )
        members = await session.scalar(text("SELECT count(*) FROM run_members"))
    assert int(runs) == 0
    assert int(members) == 0


# ===========================================================================
# 6) 单 worker 锁：明确日志 + serve 返回 locked
# ===========================================================================


async def test_qa_second_worker_logs_and_serve_returns_locked(qa_env: QaEnv) -> None:
    """第二个 worker 抢锁失败必须留 ERROR 日志，且 serve 立即返回 ``locked``（非空转）。

    注意：``app.worker`` 日志器在 alembic ``fileConfig`` 之后被 **disabled=True**
    （见 qa-report-t3.md「P2-1」），故这里直接挂临时 handler 观测记录，而非用 caplog。
    """
    run_id = await setup_run(qa_env.maker, 3)
    first = qa_env.worker(MockProvider(fixed_latency_s=0.0))
    second = qa_env.worker(MockProvider(fixed_latency_s=0.0))

    assert await first.start() is True

    logger = logging.getLogger("app.worker")
    captured: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            captured.append(record)

    handler = _Collect(level=logging.ERROR)
    was_disabled = logger.disabled
    logger.disabled = False  # 绕过 alembic fileConfig 的副作用，仅用于观测
    logger.addHandler(handler)
    try:
        assert await second.start() is False
    finally:
        logger.removeHandler(handler)
        logger.disabled = was_disabled

    msgs = [(r.levelname, r.getMessage()) for r in captured]
    assert any(
        level == "ERROR" and "advisory lock" in message for level, message in msgs
    ), f"second worker must log an explicit ERROR when it cannot take the lock; got {msgs}"

    # serve 抢不到锁 → 立即返回 "locked"，且不领取任何成员。
    assert await second.serve(run_id) == "locked"
    assert await _db_running_members(qa_env.maker, run_id) == 0

    # 首个释放后第二个可接管。
    await first.stop()
    assert await second.start() is True
    assert second.advisory_lock.key == ADVISORY_LOCK_KEY


# ===========================================================================
# 7) 未来 next_attempt_at 不占并发位、不被提前领取
# ===========================================================================


async def test_qa_deferred_retry_not_claimed_no_slot(qa_env: QaEnv) -> None:
    """``retry_wait`` 且 ``next_attempt_at`` 在未来的成员不可领取、不占槽位。"""
    run_id = await setup_run(qa_env.maker, 5)
    worker = qa_env.worker(MockProvider(fixed_latency_s=0.0), concurrency=5)
    assert await worker.start() is True

    async with qa_env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET status='retry_wait', "
                    "next_attempt_at = now() + interval '1 hour', attempt_count = 1 "
                    "WHERE run_id=:r AND row_no IN (3,4,5)"
                ),
                {"r": run_id},
            )

    async with qa_env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=10, lease_seconds=120.0
            )
    claimed_rows = sorted(claim.row_no for claim in result.claims)
    assert claimed_rows == [1, 2]  # 只看得到期成员
    assert worker.limiter.in_flight == 0  # 领取本身不占并发槽位

    async with qa_env.maker() as session:
        deferred_attempts = await session.scalar(
            text(
                "SELECT count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id "
                "WHERE m.run_id=:r AND m.row_no IN (3,4,5)"
            ),
            {"r": run_id},
        )
    assert int(deferred_attempts) == 0  # 未来重试成员无 attempt
