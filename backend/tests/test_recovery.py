# SPDX-License-Identifier: GPL-3.0-or-later
"""T3 崩溃恢复测试（主文档 §6.3 恢复语义 / architecture.md §6.3）。

验收命令：``uv run pytest tests/test_recovery.py -q``

覆盖：
- 杀 worker 后重启可恢复，且**不丢已成功答案**；
- 过期租约 → 一次 ``abandoned`` attempt + 成员转 ``retry_wait``/``failed``；
- ``abandoned`` attempt 的 ``reserved_cost`` **保留**、不按 0 释放；
- 恢复后**不产生重复有效答案**。
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
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_LOAD_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "load"
if str(_LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_LOAD_DIR))

from mock_provider import MockProvider  # noqa: E402

from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
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
from app.worker.limits import build_request_for_persona, reservation_for_request  # noqa: E402
from app.worker.main import Worker  # noqa: E402
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING  # noqa: E402

TEST_DATABASE_URL = Settings.from_env().test_database_url
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0)


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------


class RecoveryEnv:
    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.workers: list[Worker] = []

    def worker(self, provider, **kwargs) -> Worker:
        settings = kwargs.pop("settings", None) or make_settings()
        worker = Worker(
            engine=self.engine,
            sessionmaker=self.maker,
            provider=provider,
            settings=settings,
            concurrency=kwargs.pop("concurrency", 4),
            lease_seconds=kwargs.pop("lease_seconds", 120.0),
            renew_interval=kwargs.pop("renew_interval", 20.0),
            scan_interval=kwargs.pop("scan_interval", 0.02),
            poll_interval=kwargs.pop("poll_interval", 0.01),
            lock_check_interval=kwargs.pop("lock_check_interval", 5.0),
            retry_policy=kwargs.pop("retry_policy", FAST_RETRY),
            **kwargs,
        )
        self.workers.append(worker)
        return worker


@pytest_asyncio.fixture
async def env(prepared_test_db: None):
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=20)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    environment = RecoveryEnv(engine, maker)
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


def _csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


class GatedMockProvider(MockProvider):
    """可控闸门 mock（复用 ``test_worker`` 的 barrier 思路，用于崩溃恢复场景）。

    - **``hold()`` 之前**：行为与 :class:`MockProvider` 完全一致（确定性合法答案）。
    - **``hold()`` 之后**：每一次 ``answer`` 都先登记、再阻塞在一个只有 :meth:`release`
      才会置位的事件上。测试在「杀掉 worker」窗口内**绝不**调用 :meth:`release`，因此被
      拦住的调用**不可能**完成 —— 其成员必停在 ``running``。

    关键前提（由实现保证，见 ``worker/main.py`` 的 ``_consumer``）：成员在
    ``provider.answer()`` 被调用**之前**，已在其自身的 claim 事务里提交为 ``running``。
    故「闸门内有一次调用在等待」⇔「DB 里有一个 ``running`` 成员」，且该 ``running`` 是
    **稳定**的（不随调度窗口漂移）。这正是原竞态断言所需的确定性替代。

    本类是**测试辅助**，不产出任何默认答案（红线 #1 不适用：它只在合法输出上包一层闸门）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hold = asyncio.Event()
        self._release_event = asyncio.Event()
        self._gated_started = 0
        self._first_gated = asyncio.Event()

    def hold(self) -> None:
        """翻转闸门：此后所有调用阻塞，直到 :meth:`release`（本用例不释放）。"""
        self._hold.set()

    def release(self) -> None:
        """放行被闸门拦住的调用（本用例在杀 worker 前**不会**调用）。"""
        self._release_event.set()

    @property
    def gated_started(self) -> int:
        """已进入闸门等待的调用数。"""
        return self._gated_started

    async def answer(self, request: ModelRequest) -> ModelResponse:
        if self._hold.is_set():
            self._gated_started += 1
            self._first_gated.set()
            await self._release_event.wait()
        return await super().answer(request)

    async def wait_first_gated(self, timeout: float = 15.0) -> bool:
        """等待第一个被闸门拦住的调用出现；超时返回 ``False``。"""
        try:
            await asyncio.wait_for(self._first_gated.wait(), timeout)
            return True
        except TimeoutError:
            return False


async def setup_run(
    maker, n: int, *, start: bool = True, budget_limit: Decimal | None = None
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
        )
        await session.commit()
        if start:
            await run_service.start_run(session, run.id)
        return run.id


async def status_counts(maker, run_id: UUID) -> dict[str, int]:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status, count(*) FROM run_members WHERE run_id = :r GROUP BY status"
                ),
                {"r": run_id},
            )
        ).all()
    return {row[0]: int(row[1]) for row in rows}


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return await predicate()


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------


async def test_kill_worker_recovers_without_losing_success(env: RecoveryEnv) -> None:
    """杀掉 worker（模拟崩溃）后重启：未完成任务被恢复，已成功答案不丢。

    **确定性崩溃点（P1 修复 / TEAM-BRIEF §7.11.1）**：原用例在「读 ``running`` 的那一刻」
    断言 ``running >= 1``，而 worker 是**滚动补位**、在途请求完成得很快 —— 从「succeeded
    达标」到 ``cancel()`` 生效之间存在调度窗口，读 ``mid`` 时那批在途很可能已全部落地为
    ``succeeded``（``running == 0``）。故原断言测的是**时序**而非不变量，无法稳定复现。

    现改用**可控闸门 provider**：先让若干成员正常成功（建立「不丢不改」基线），测试再翻转
    闸门；此后每次 ``answer`` 都阻塞在测试**在杀 worker 前绝不置位**的事件上。成员在
    ``answer`` 之前已按 claim 提交为 ``running``，故「闸门内有一次调用在等待」⇔「DB 里有一个
    ``running`` 成员」，且该 ``running`` 不可能自行完成 —— 断言从此确定成立。
    """
    n = 40
    run_id = await setup_run(env.maker, n)

    provider = GatedMockProvider(fixed_latency_s=0.02, fault_plan="none")
    worker1 = env.worker(provider, concurrency=4)
    assert await worker1.start() is True

    serve_task = asyncio.create_task(worker1.serve(run_id, timeout=30.0))

    # 1) 先让若干成员正常成功（为后续「已成功答案不丢不改」逐条比对建立基线）。
    async def some_succeeded() -> bool:
        return (await status_counts(env.maker, run_id)).get("succeeded", 0) >= 3

    assert await wait_for(some_succeeded, timeout=15.0)

    # 2) 翻转闸门：之后所有 provider 调用都在其自身事件上阻塞（本用例不释放它）。
    provider.hold()
    assert await provider.wait_first_gated(timeout=15.0), (
        "gate never engaged: no provider call was held after hold()"
    )

    # 3) 在途已**确定**成立：闸门内有一次阻塞调用 ⇒ 必有一个 running 成员，且它不会消失。
    async def a_member_is_running() -> bool:
        return (await status_counts(env.maker, run_id)).get("running", 0) >= 1

    assert await wait_for(a_member_is_running, timeout=15.0)

    # 4) 此刻「杀掉」worker（取消调度任务）。
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    await worker1.stop()

    mid = await status_counts(env.maker, run_id)
    assert mid.get("succeeded", 0) >= 3
    assert mid.get("running", 0) >= 1  # 崩溃时确有在途未完成（由闸门确定性保证）

    # 记录已成功答案（恢复后必须原样保留）。
    async with env.maker() as session:
        succeeded_before = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    text(
                        "SELECT persona_id, answer_json FROM run_members "
                        "WHERE run_id = :r AND status='succeeded'"
                    ),
                    {"r": run_id},
                )
            ).all()
        }

    # 模拟时间流逝：把在途成员的租约置为过期。
    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET lease_expires_at = now() - interval '5 seconds' "
                    "WHERE run_id = :r AND status='running'"
                ),
                {"r": run_id},
            )

    # 新 worker 接管（自动扫描过期租约并恢复）。
    worker2 = env.worker(MockProvider(fixed_latency_s=0.0, fault_plan="none"), concurrency=4)
    assert await worker2.start() is True
    final_status = await worker2.serve(run_id, timeout=30.0)

    assert final_status == "completed"
    counts = await status_counts(env.maker, run_id)
    assert counts.get("succeeded", 0) == n
    assert counts.get("failed", 0) == 0
    assert sum(counts.values()) == n

    async with env.maker() as session:
        succeeded_after = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    text(
                        "SELECT persona_id, answer_json FROM run_members "
                        "WHERE run_id = :r AND status='succeeded'"
                    ),
                    {"r": run_id},
                )
            ).all()
        }
    for persona_id, answer in succeeded_before.items():
        assert succeeded_after[persona_id] == answer  # 不丢、不改


async def test_expired_lease_becomes_abandoned_attempt(env: RecoveryEnv) -> None:
    """过期租约 → running attempt 记为 abandoned；成员按剩余次数转 retry_wait。"""
    run_id = await setup_run(env.maker, 1)
    worker = env.worker(MockProvider(fixed_latency_s=0.0))
    assert await worker.start() is True

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=1, lease_seconds=120.0
            )
    claim = result.claims[0]

    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE run_members SET lease_expires_at = now() - interval '1 second'"),
            )
    recovered = await worker.scan_expired_once(run_id)
    assert recovered == 1

    async with env.maker() as session:
        member = (
            await session.execute(
                text("SELECT status, lease_token FROM run_members WHERE id = :mid"),
                {"mid": claim.member_id},
            )
        ).one()
        attempt = (
            await session.execute(
                text("SELECT status FROM attempts WHERE id = :aid"), {"aid": claim.attempt_id}
            )
        ).one()
    assert attempt[0] == "abandoned"
    assert member[0] == "retry_wait"
    assert member[1] is None


async def test_abandoned_attempt_retains_reserved_cost(env: RecoveryEnv) -> None:
    """``abandoned`` attempt 的 ``reserved_cost`` 必须**保留**，不按 0 释放。"""
    settings = make_settings(
        input_price_per_million=Decimal("1000"),
        output_price_per_million=Decimal("2000"),
    )
    run_id = await setup_run(env.maker, 1)
    worker = env.worker(MockProvider(fixed_latency_s=0.0), settings=settings)
    assert await worker.start() is True

    async with env.maker() as session:
        run = await run_repository.get_run(session, run_id)
    snapshot = run.survey_snapshot
    survey = SurveyInput.model_validate(
        {
            "title": snapshot["title"],
            "product": snapshot["product"],
            "question": snapshot["question"],
        }
    )

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

    async with env.maker() as session:
        async with session.begin():
            result = await run_repository.claim_members(
                session, run_id=run_id, limit=1, lease_seconds=120.0, reservation_fn=reservation_fn
            )
    claim = result.claims[0]
    each = claim.reserved_cost
    assert each > 0

    async with env.maker() as session:
        run_after_claim = await run_repository.get_run(session, run_id)
    assert run_after_claim.reserved_cost == each

    # 过期 → abandoned；预留保留。
    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE run_members SET lease_expires_at = now() - interval '1 second'")
            )
    await worker.scan_expired_once(run_id)

    async with env.maker() as session:
        run_after = await run_repository.get_run(session, run_id)
        attempt = (
            await session.execute(
                text("SELECT status, reserved_cost, actual_cost FROM attempts WHERE id = :aid"),
                {"aid": claim.attempt_id},
            )
        ).one()
    assert attempt[0] == "abandoned"
    assert attempt[1] == each  # 预留保留
    assert attempt[2] is None  # 未知，不写成 0
    assert run_after.reserved_cost == each  # run 级预留未被释放
    assert run_after.actual_cost == Decimal("0")
    assert run_after.unknown_cost_count == 1


async def test_no_duplicate_valid_answer_after_recovery(env: RecoveryEnv) -> None:
    """恢复后不得出现重复有效答案：succeeded 数 == 有答案数 == N，且每成员唯一。"""
    n = 20
    run_id = await setup_run(env.maker, n)

    # 第一段：正常跑，跑到一半「崩溃」。
    worker1 = env.worker(MockProvider(fixed_latency_s=0.02), concurrency=4)
    assert await worker1.start() is True
    serve_task = asyncio.create_task(worker1.serve(run_id, timeout=30.0))

    async def half_done() -> bool:
        return (await status_counts(env.maker, run_id)).get("succeeded", 0) >= 5

    assert await wait_for(half_done, timeout=15.0)
    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    await worker1.stop()

    async with env.maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET lease_expires_at = now() - interval '5 seconds' "
                    "WHERE run_id = :r AND status='running'"
                ),
                {"r": run_id},
            )

    worker2 = env.worker(MockProvider(fixed_latency_s=0.0), concurrency=4)
    assert await worker2.start() is True
    assert await worker2.serve(run_id, timeout=30.0) == "completed"

    async with env.maker() as session:
        total = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r"), {"r": run_id}
        )
        succeeded = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND status='succeeded'"),
            {"r": run_id},
        )
        answered = await session.scalar(
            text("SELECT count(*) FROM run_members WHERE run_id = :r AND answer_json IS NOT NULL"),
            {"r": run_id},
        )
        distinct_answers = await session.scalar(
            text(
                "SELECT count(DISTINCT (persona_id, answer_json)) FROM run_members "
                "WHERE run_id = :r AND answer_json IS NOT NULL"
            ),
            {"r": run_id},
        )
        succeeded_attempts = await session.scalar(
            text(
                "SELECT count(*) FROM attempts a JOIN run_members m ON m.id=a.member_id "
                "WHERE m.run_id = :r AND a.status='succeeded'"
            ),
            {"r": run_id},
        )
    assert int(total) == n
    assert int(succeeded) == n
    assert int(answered) == n
    assert int(distinct_answers) == n
    assert int(succeeded_attempts) == n  # 每成员恰有一次成功 attempt
