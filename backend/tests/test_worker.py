# SPDX-License-Identifier: GPL-3.0-or-later
"""T3 worker 可靠性测试（主文档 §6.3 / §6.4 / §7 / architecture.md §6.7）。

验收命令：``uv run pytest tests/test_worker.py -q``

**全部打真实 PostgreSQL（``survey_test``）**；并发峰值用例用真实领取事务 + barrier mock，
断言「恰有 100 在途、峰值恒 ≤ 100、释放后滚动补位」。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import pathlib
import sys
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# --- 项目根 tests/load 加入 sys.path，以顶层模块名导入 mock（避免与 tests 包冲突）---
_LOAD_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "load"
if str(_LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_LOAD_DIR))

from mock_provider import BarrierMockProvider, MockProvider, valid_value_for  # noqa: E402

from app.config import Settings  # noqa: E402
from app.contracts import ProductInput, QuestionInput, SurveyInput  # noqa: E402
from app.personas.source import create_import  # noqa: E402
from app.runs.repository import Claim, run_repository  # noqa: E402
from app.runs.service import run_service  # noqa: E402
from app.surveys.service import survey_service  # noqa: E402
from app.worker.execute import AttemptExecutor, RetryPolicy, compute_backoff_seconds  # noqa: E402
from app.worker.limits import (  # noqa: E402
    SmoothRateLimiter,
    build_request_for_persona,
    compute_cost,
    reservation_for_request,
)
from app.worker.main import Worker  # noqa: E402
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING  # noqa: E402

TEST_DATABASE_URL = Settings.from_env().test_database_url

#: 测试用快速重试（避免真实 2s/8s 退避拖慢用例）。
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0)


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


class WorkerEnv:
    """测试环境：大连接池引擎 + 会话工厂 + 自动回收的 worker 列表。"""

    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.workers: list[Worker] = []

    def worker(self, provider, **kwargs) -> Worker:
        worker = make_worker(self.engine, self.maker, provider, **kwargs)
        self.workers.append(worker)
        return worker


@pytest_asyncio.fixture
async def env(prepared_test_db: None):
    """小池引擎 + 会话工厂（100 消费者复用有限连接；PG ``max_connections=100``）。

    worker 的模型调用在**事务外**进行，DB 连接只在领取/保存的短事务中短暂占用，
    因此 100 个消费者只需一个小连接池即可（30 条）。
    """
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=20)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    environment = WorkerEnv(engine, maker)
    try:
        yield environment
    finally:
        # 必须先停止 worker（释放 advisory lock 的专用连接），再 dispose 引擎，
        # 否则仍被签出的连接不会被 dispose 关闭，会话级锁会残留到后续用例。
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
    """建一个含 n 个成员的 run（可选启动）。返回 run_id。"""
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


async def fetch_run(maker, run_id: UUID):
    async with maker() as session:
        return await run_repository.get_run(session, run_id)


def survey_from_run(run) -> SurveyInput:
    snap = run.survey_snapshot
    return SurveyInput.model_validate(
        {"title": snap["title"], "product": snap["product"], "question": snap["question"]}
    )


def make_reservation_fn(settings: Settings, survey: SurveyInput):
    def reservation_fn(persona_snapshot: dict) -> Decimal:
        request = build_request_for_persona(
            survey=survey,
            persona_snapshot=persona_snapshot,
            model=settings.model_name,
            max_output_tokens=settings.model_max_output_tokens,
            timeout_seconds=settings.model_timeout_seconds,
            allow_reason=True,
        )
        return reservation_for_request(
            request,
            input_price_per_million=settings.input_price_per_million,
            output_price_per_million=settings.output_price_per_million,
        )

    return reservation_fn


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


# ---------------------------------------------------------------------------
# advisory lock
# ---------------------------------------------------------------------------


async def test_advisory_lock_second_worker_fails(env: WorkerEnv) -> None:
    """第二个 worker 抢不到会话级 advisory lock → 启动失败（并留日志）。"""
    run_id = await setup_run(env.maker, 2)

    first = env.worker(MockProvider(fixed_latency_s=0.0))
    second = env.worker(MockProvider(fixed_latency_s=0.0))

    assert await first.start() is True
    assert first.advisory_lock.held is True
    assert await second.start() is False
    assert second.advisory_lock.held is False

    # 首个 worker 释放后，第二个才能拿到锁。
    await first.stop()
    assert await second.start() is True
    assert run_id is not None


async def test_advisory_lock_released_stops_claiming(env: WorkerEnv) -> None:
    """锁丢失 → 立即停止领取（不再 claim 任何成员）。"""
    run_id = await setup_run(env.maker, 5)
    worker = env.worker(MockProvider(fixed_latency_s=0.0))
    assert await worker.start() is True
    assert worker.advisory_lock.held is True

    # 模拟锁连接断开/被释放。
    await worker.advisory_lock.release()
    assert worker.advisory_lock.held is False
    assert await worker.advisory_lock.health_check() is False

    result = await worker.claim_once(run_id)
    assert result.claims == []
    assert result.run_status == "locked"

    async with env.maker() as session:
        running = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND status='running'"),
            {"r": run_id},
        )
    assert int(running) == 0


# ---------------------------------------------------------------------------
# 租约续期 / 过期扫描
# ---------------------------------------------------------------------------


async def test_lease_renewal_and_expiry_scan(env: WorkerEnv) -> None:
    """续租延长 lease_expires_at；过期扫描 → abandoned attempt + 成员转 retry_wait。"""
    run_id = await setup_run(env.maker, 2)
    worker = env.worker(MockProvider(fixed_latency_s=0.0), lease_seconds=1.0)
    assert await worker.start() is True

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=2, lease_seconds=1.0
            )
    assert result.claimed == 2
    claim_a, claim_b = result.claims

    renewed = await worker.renew_leases_once(run_id)
    assert renewed == 2
    async with env.maker() as session:
        after = await session.scalar(
            text("SELECT lease_expires_at FROM run_members WHERE id = :mid"),
            {"mid": claim_a.member_id},
        )
    assert after > claim_a.lease_expires_at

    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET lease_expires_at = now() - interval '5 seconds' "
                    "WHERE id = :mid"
                ),
                {"mid": claim_a.member_id},
            )
    recovered = await worker.scan_expired_once(run_id)
    assert recovered == 1

    async with env.maker() as session:
        member_status = await session.scalar(
            text("SELECT status FROM run_members WHERE id = :mid"), {"mid": claim_a.member_id}
        )
        attempt_status = await session.scalar(
            text("SELECT status FROM attempts WHERE id = :aid"), {"aid": claim_a.attempt_id}
        )
    assert member_status == "retry_wait"
    assert attempt_status == "abandoned"
    assert claim_b is not None


# ---------------------------------------------------------------------------
# CAS：迟到结果不得覆盖
# ---------------------------------------------------------------------------


async def test_stale_token_cannot_overwrite(env: WorkerEnv) -> None:
    """失租/旧 token 的迟到结果不能覆盖答案或状态（CAS 0 行 → 丢弃）。"""
    run_id = await setup_run(env.maker, 1)

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=1, lease_seconds=120.0
            )
    claim = result.claims[0]
    answer = {"question_id": "purchase_intent", "value": "definitely_yes"}

    stale = Claim(
        run_id=claim.run_id,
        member_id=claim.member_id,
        row_no=claim.row_no,
        persona_id=claim.persona_id,
        persona_snapshot=claim.persona_snapshot,
        attempt_id=claim.attempt_id,
        attempt_no=claim.attempt_no,
        attempt_limit=claim.attempt_limit,
        lease_token=uuid4(),  # 错误 token
        lease_expires_at=claim.lease_expires_at,
        reserved_cost=claim.reserved_cost,
    )
    async with env.maker() as session:
        async with session.begin():
            saved = await run_repository.finalize_success(
                session, claim=stale, answer_json=answer
            )
    assert saved is False
    async with env.maker() as session:
        status = await session.scalar(
            text("SELECT status FROM run_members WHERE id = :mid"), {"mid": claim.member_id}
        )
        answer_json = await session.scalar(
            text("SELECT answer_json FROM run_members WHERE id = :mid"), {"mid": claim.member_id}
        )
    assert status == "running"
    assert answer_json is None

    # 正确 token → 保存成功。
    async with env.maker() as session:
        async with session.begin():
            saved = await run_repository.finalize_success(
                session, claim=claim, answer_json=answer
            )
    assert saved is True

    # 旧 token 再次迟到（成员已 succeeded）→ CAS 0 行 → 不覆盖已成功答案。
    new_answer = {"question_id": "purchase_intent", "value": "definitely_not"}
    async with env.maker() as session:
        async with session.begin():
            saved = await run_repository.finalize_success(
                session, claim=claim, answer_json=new_answer
            )
    assert saved is False
    async with env.maker() as session:
        final = await session.scalar(
            text("SELECT answer_json FROM run_members WHERE id = :mid"), {"mid": claim.member_id}
        )
    assert final["value"] == "definitely_yes"


# ---------------------------------------------------------------------------
# 重试 → 单一有效答案
# ---------------------------------------------------------------------------


async def test_retry_success_yields_single_valid_answer(env: WorkerEnv) -> None:
    """可重试故障后成功：每成员恰有一个有效答案，attempts 无重复 attempt_no。"""
    run_id = await setup_run(env.maker, 1)
    provider = MockProvider(fixed_latency_s=0.0, fault_plan="recoverable")
    worker = env.worker(provider, concurrency=2)
    assert await worker.start() is True

    status = await worker.serve(run_id, timeout=15.0)
    assert status == "completed"

    async with env.maker() as session:
        succeeded = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND status='succeeded'"),
            {"r": run_id},
        )
        answered = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND answer_json IS NOT NULL"),
            {"r": run_id},
        )
        attempts = await session.scalar(
            text(
                "SELECT count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id "
                "WHERE m.run_id = :r"
            ),
            {"r": run_id},
        )
        distinct_attempts = await session.scalar(
            text(
                "SELECT count(DISTINCT (a.member_id, a.attempt_no)) FROM attempts a "
                "JOIN run_members m ON m.id=a.member_id WHERE m.run_id = :r"
            ),
            {"r": run_id},
        )
        value = await session.scalar(
            text("SELECT answer_json->>'value' FROM run_members WHERE run_id = :r"),
            {"r": run_id},
        )
    assert int(succeeded) == 1
    assert int(answered) == 1
    assert int(attempts) == 3  # 1 error + 1 invalid + 1 success
    assert int(distinct_attempts) == 3
    assert value == valid_value_for("p_000001")
    statuses = [call.status for call in provider.log.calls_for("p_000001")]
    assert statuses == ["error", "invalid", "ok"]


async def test_invalid_output_never_becomes_valid_answer(env: WorkerEnv) -> None:
    """无效输出只落到 retry_wait/failed，**绝不**出现在 succeeded 的 answer_json 里。"""
    run_id = await setup_run(env.maker, 2)
    provider = MockProvider(
        fixed_latency_s=0.0,
        fault_plan="recoverable",
        invalid_personas={"p_000002"},  # 恒非法输出
    )
    worker = env.worker(provider, concurrency=2)
    assert await worker.start() is True

    status = await worker.serve(run_id, timeout=15.0)
    assert status == "completed_with_errors"

    async with env.maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT persona_id, status, answer_json FROM run_members "
                    "WHERE run_id = :r ORDER BY row_no"
                ),
                {"r": run_id},
            )
        ).all()
    by_persona = {row[0]: (row[1], row[2]) for row in rows}

    good_status, good_answer = by_persona["p_000001"]
    assert good_status == "succeeded"
    assert good_answer["value"] == valid_value_for("p_000001")

    bad_status, bad_answer = by_persona["p_000002"]
    assert bad_status == "failed"
    assert bad_answer is None  # 绝不补默认答案

    bad_calls = provider.log.calls_for("p_000002")
    assert bad_calls, "expected calls for p_000002"
    assert all(call.status != "ok" for call in bad_calls)
    assert all(
        call.error_code in ("INVALID_OUTPUT", "RATE_LIMITED", "SERVER_ERROR")
        for call in bad_calls
    )


# ---------------------------------------------------------------------------
# 100 并发峰值 + 滚动补位（真实 PG + barrier mock）
# ---------------------------------------------------------------------------


async def test_peak_concurrency_is_100_with_rolling_refill(env: WorkerEnv) -> None:
    """恰有 100 在途、峰值恒 ≤ 100、释放后滚动补位、最终全部成功。"""
    run_id = await setup_run(env.maker, 1000)

    provider = BarrierMockProvider(target=100, fixed_latency_s=0.0)
    worker = env.worker(provider, concurrency=100)
    assert await worker.start() is True

    serve_task = asyncio.create_task(worker.serve(run_id, timeout=60.0))

    # 1) 恰有 100 在途。
    reached = await provider.wait_reached(timeout=20.0)
    assert reached, f"barrier never reached target=100 (current={provider.current})"
    assert provider.peak == 100, f"expected peak == 100, got {provider.peak}"
    assert provider.current == 100

    # 2) 峰值恒 ≤ 100。
    assert provider.peak <= 100

    # 3) 滚动补位：放行 1 个 → 立刻有新请求补位到 100（不是整轮等待）。
    await provider.release(1)
    refilled = await provider.wait_until(
        lambda p: p.started > 100 and p.current == p.target, timeout=10.0
    )
    assert refilled, (
        f"no rolling refill observed: started={provider.started}, current={provider.current}"
    )
    assert provider.peak <= 100

    # 4) 全程放行 → 收敛，全部成功、每成员仅 1 条有效答案。
    await provider.open_gate()
    status = await serve_task
    assert status == "completed"
    assert provider.peak == 100  # 峰值恒为 100，从未越界

    async with env.maker() as session:
        succeeded = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND status='succeeded'"),
            {"r": run_id},
        )
        answered = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND answer_json IS NOT NULL"),
            {"r": run_id},
        )
    assert int(succeeded) == 1000
    assert int(answered) == 1000


# ---------------------------------------------------------------------------
# 预算预留 / 未知保留
# ---------------------------------------------------------------------------


async def test_budget_reservation_and_unknown_retained(env: WorkerEnv) -> None:
    """发请求前预留；有 usage 后释放；未知计费（超时）保留预留、不按 0 释放。"""
    settings = make_settings(
        input_price_per_million=Decimal("1000"),
        output_price_per_million=Decimal("2000"),
    )
    run_id = await setup_run(env.maker, 2)

    async with env.maker() as session:
        run = await run_repository.get_run(session, run_id)
    survey = survey_from_run(run)
    reservation_fn = make_reservation_fn(settings, survey)

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session,
                run_id=run_id,
                limit=2,
                lease_seconds=120.0,
                reservation_fn=reservation_fn,
            )
    assert result.claimed == 2
    each = result.claims[0].reserved_cost
    assert each > 0

    run_after_claim = await fetch_run(env.maker, run_id)
    assert run_after_claim.reserved_cost == each * 2
    assert run_after_claim.requests_reserved == 2

    # 未知计费：timeout 成员 → 保留预留、unknown_cost_count +1。
    timeout_claim = result.claims[0]
    timeout_provider = MockProvider(
        fixed_latency_s=0.0,
        timeout_personas={timeout_claim.persona_id},
        timeout_latency_s=0.01,
    )
    executor = AttemptExecutor(
        sessionmaker=env.maker,
        provider=timeout_provider,
        settings=settings,
        retry_policy=FAST_RETRY,
    )
    outcome = await executor.execute(run=run_after_claim, claim=timeout_claim, survey=survey)
    assert outcome.status == "retry_wait"

    run_after_timeout = await fetch_run(env.maker, run_id)
    assert run_after_timeout.reserved_cost == each * 2  # 未释放
    assert run_after_timeout.unknown_cost_count == 1

    # 已知 usage：成功 → 释放该次预留、累加实际支出。
    valid_claim = result.claims[1]
    valid_provider = MockProvider(
        fixed_latency_s=0.0, usage_mode="present", input_tokens=32, output_tokens=8
    )
    executor2 = AttemptExecutor(
        sessionmaker=env.maker,
        provider=valid_provider,
        settings=settings,
        retry_policy=FAST_RETRY,
    )
    outcome2 = await executor2.execute(
        run=run_after_timeout, claim=valid_claim, survey=survey
    )
    assert outcome2.status == "succeeded"

    run_final = await fetch_run(env.maker, run_id)
    expected_actual = compute_cost(
        input_tokens=32,
        output_tokens=8,
        input_price_per_million=Decimal("1000"),
        output_price_per_million=Decimal("2000"),
    )
    assert run_final.reserved_cost == each  # 一次被释放
    assert run_final.actual_cost == expected_actual


async def test_budget_exhaustion_requests_pause(env: WorkerEnv) -> None:
    """预算不足以再预留一个成员 → 以 ``budget`` 原因请求暂停（不领取）。"""
    settings = make_settings(
        input_price_per_million=Decimal("1000000"),
        output_price_per_million=Decimal("1000000"),
    )
    run_id = await setup_run(env.maker, 4, budget_limit=Decimal("0.000001"))
    worker = env.worker(MockProvider(fixed_latency_s=0.0), concurrency=1, settings=settings)
    assert await worker.start() is True

    result = await worker.claim_once(run_id)
    assert result.claims == []
    assert result.pause_reason == "budget"

    run = await fetch_run(env.maker, run_id)
    assert run.status in ("pausing", "paused")
    assert run.pause_reason == "budget"


# ---------------------------------------------------------------------------
# 重启重建限流窗口（裁决 C3）
# ---------------------------------------------------------------------------


async def test_restart_rebuilds_rate_window(env: WorkerEnv) -> None:
    """worker 启动必须依据 attempts 最近 60s 记录保守重建限流窗口（重启≠清空限额）。"""
    run_id = await setup_run(env.maker, 5)

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=5, lease_seconds=120.0
            )
    assert result.claimed == 5
    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE attempts SET started_at = now() - interval '10 seconds'")
            )

    rate_limiter = SmoothRateLimiter(rpm=1000, tpm=1_000_000)
    assert rate_limiter.in_window_requests == 0  # 未重建 → 空窗口（缺陷形态）

    worker = env.worker(MockProvider(fixed_latency_s=0.0), rate_limiter=rate_limiter)
    assert await worker.start() is True
    assert rate_limiter.in_window_requests == 5  # 已按最近 60s 记录重建

    # 窗口外的历史被忽略（保守重建只覆盖最近 60s）。
    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE attempts SET started_at = now() - interval '3600 seconds'")
            )
    limiter2 = SmoothRateLimiter(rpm=1000, tpm=1_000_000)
    worker2 = env.worker(MockProvider(fixed_latency_s=0.0), rate_limiter=limiter2)
    added = await worker2.rebuild_rate_window()
    assert added == 0
    assert limiter2.in_window_requests == 0


# ---------------------------------------------------------------------------
# next_attempt_at 不占并发位
# ---------------------------------------------------------------------------


async def test_next_attempt_at_does_not_consume_slot(env: WorkerEnv) -> None:
    """``retry_wait`` 且 ``next_attempt_at`` 在未来的成员不可领取，且不占并发位。"""
    run_id = await setup_run(env.maker, 2)
    worker = env.worker(MockProvider(fixed_latency_s=0.0), concurrency=1)
    assert await worker.start() is True

    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET status='retry_wait', "
                    "next_attempt_at = now() + interval '1 hour', attempt_count = 1 "
                    "WHERE run_id = :r AND row_no = 2"
                ),
                {"r": run_id},
            )

    result = await worker.claim_once(run_id)
    assert result.claimed == 1
    assert result.claims[0].row_no == 1  # 只领取到期的那个
    assert worker.limiter.in_flight == 0  # 领取不占用并发槽位

    async with env.maker() as session:
        deferred = (
            await session.execute(
                text(
                    "SELECT status, lease_token FROM run_members WHERE run_id = :r AND row_no = 2"
                ),
                {"r": run_id},
            )
        ).one()
    assert deferred[0] == "retry_wait"
    assert deferred[1] is None


# ---------------------------------------------------------------------------
# 错误退避策略 / 连续失败暂停
# ---------------------------------------------------------------------------


def test_retry_backoff_default_is_2s_8s() -> None:
    """默认退避为 2s、8s，且有 Retry-After 时至少等待其时长。"""
    policy = RetryPolicy()
    assert compute_backoff_seconds(1, policy, jitter=0.0) == 2.0
    assert compute_backoff_seconds(2, policy, jitter=0.0) == 8.0
    assert compute_backoff_seconds(1, policy, retry_after=30.0, jitter=0.0) == 30.0
    assert policy.max_attempts == 3


async def test_consecutive_provider_failures_pause_api_unavailable(env: WorkerEnv) -> None:
    """连续 5 次同类供应商失败 → 暂停 ``api_unavailable``。"""
    run_id = await setup_run(env.maker, 3)
    provider = MockProvider(fixed_latency_s=0.0, forced_error_code="SERVER_ERROR")
    worker = env.worker(provider, concurrency=1)
    assert await worker.start() is True

    status = await worker.serve(run_id, timeout=20.0)
    assert status in ("pausing", "paused", "failed")

    run = await fetch_run(env.maker, run_id)
    assert run.pause_reason == "api_unavailable"
    assert worker.executor.consecutive_failures >= 5
