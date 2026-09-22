# SPDX-License-Identifier: GPL-3.0-or-later
"""单 worker 调度循环（主文档 §6.3 / §7 / architecture.md §6.1–§6.7）。

- **单 worker**：``pg_try_advisory_lock``（会话级）；抢不到 → 记录日志并退出；
  锁连接断开 → 立即停止领取（watchdog）。
- **100 个异步消费者共享调度器**：完成一个即滚动补位（**不**「每轮取 100 行等整轮」）；
  初次调用与重试共用同一个全局 100 槽位计数器。
- 租约 **120s**；每 **20s** 续租；每 **10s** 扫描过期租约。
- 发请求前**在领取事务内**预留费用上界；有 usage 后结算释放；未知计费保留预留。
- RPM/TPM 平滑发出；重启**依据 attempts 最近 60s 保守重建**限流窗口（裁决 C3）。

**进程入口（本次补齐，裁决 N7-d）**：``python -m app.worker.main`` 是
``deploy/compose.yaml`` 与 ``docs/runbook.md`` 使用的**唯一规范启动命令**。它执行
:func:`main`——建引擎/会话/provider/worker → 抢单 worker advisory lock（失败则**非 0 退出并留
明确日志**）→ 按 attempts 重建限流窗口 → 进入 :func:`serve_until_stopped` 调度循环 →
收到 SIGINT/SIGTERM 时优雅停止（退出循环 → ``stop()`` 释放 advisory lock → dispose engine）
并以退出码 0 结束。**worker 是独立进程**，调度循环**绝不**跑在 API 进程/HTTP 请求线程里（红线 C2）。

> ⚠️ **无害 RuntimeWarning**：以 ``python -m app.worker.main`` 启动时，CPython 可能打印一条
> ``RuntimeWarning: 'app.worker.main' found in sys.modules after import of package 'app.worker',
> but prior to execution of 'app.worker.main'``。原因是 ``app/worker/__init__.py`` 会**先行导入**
> ``app.worker.main``（以复用 :class:`Worker` 等导出），随后 ``-m`` 又把它当 ``__main__`` 执行。
> 该告警**不影响** worker 启动、调度或退出码，属已知且无害；本项目保留该启动命令不变
> （compose 与 runbook 均已使用）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.contracts import SurveyInput
from app.db import create_engine, create_sessionmaker
from app.inference.provider import build_provider
from app.models import RunRecord
from app.runs.repository import (
    PAUSING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    Claim,
    ClaimResult,
    RunRepository,
    run_repository,
)
from app.worker.execute import AttemptExecutor, ExecutionOutcome, RetryPolicy
from app.worker.limits import (
    RATE_WINDOW_SECONDS,
    ConcurrencyLimiter,
    SmoothRateLimiter,
    build_request_for_persona,
    reservation_for_request,
)

logger = logging.getLogger("app.worker")

#: 单 worker advisory lock 键（会话级；同一键只能被一个会话持有）。
ADVISORY_LOCK_KEY = 728_100_001

#: 租约时长（秒，主文档 §6.3）。
DEFAULT_LEASE_SECONDS = 120.0
#: 续租间隔（秒）。
DEFAULT_RENEW_INTERVAL = 20.0
#: 过期扫描间隔（秒）。
DEFAULT_SCAN_INTERVAL = 10.0
#: 空闲轮询间隔（秒）。
DEFAULT_POLL_INTERVAL = 0.05
#: 锁连接健康检查间隔（秒）。
DEFAULT_LOCK_CHECK_INTERVAL = 5.0


class AdvisoryLock:
    """会话级 advisory lock（占一条专用连接；锁随连接断开而释放）。"""

    def __init__(self, engine: AsyncEngine, key: int = ADVISORY_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: Any | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def key(self) -> int:
        return self._key

    async def try_acquire(self) -> bool:
        """尝试获取；已被其它会话持有 → ``False``（不阻塞）。"""
        if self._conn is not None and self._held:
            return True
        self._conn = await self._engine.connect()
        try:
            result = await self._conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            )
            acquired = bool(result.scalar())
        except Exception:
            await self._close_connection()
            raise
        if not acquired:
            await self._close_connection()
            return False
        self._held = True
        return True

    async def health_check(self) -> bool:
        """检查锁连接是否仍可用；断开 → 标记失锁并返回 ``False``。"""
        if self._conn is None or not self._held:
            return False
        try:
            await self._conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.warning("advisory lock connection lost; stopping claims")
            self._held = False
            await self._close_connection()
            return False

    async def release(self) -> None:
        """显式释放锁（测试与优雅关闭用）。"""
        if self._conn is not None and self._held:
            with contextlib.suppress(Exception):
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                )
        self._held = False
        await self._close_connection()

    async def _close_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None


class Worker:
    """单进程 worker：advisory lock + 100 消费者调度器 + 租约/限流维护。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        provider: Any,
        settings: Settings | None = None,
        repository: RunRepository | None = None,
        concurrency: int | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        renew_interval: float = DEFAULT_RENEW_INTERVAL,
        scan_interval: float = DEFAULT_SCAN_INTERVAL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        lock_check_interval: float = DEFAULT_LOCK_CHECK_INTERVAL,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: SmoothRateLimiter | None = None,
        limiter: ConcurrencyLimiter | None = None,
        executor: AttemptExecutor | None = None,
        lock_key: int = ADVISORY_LOCK_KEY,
        allow_reason: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        self._repo = repository or run_repository
        self._concurrency = concurrency or self._settings.agent_concurrency
        self._lease_seconds = lease_seconds
        self._renew_interval = renew_interval
        self._scan_interval = scan_interval
        self._poll_interval = poll_interval
        self._lock_check_interval = lock_check_interval

        # 全局槽位上限 = min(消费者数, 配置并发)：初次调用与重试合计在途 ≤ 上限。
        slot_capacity = min(self._concurrency, self._settings.agent_concurrency)
        self._limiter = limiter or ConcurrencyLimiter(capacity=slot_capacity)
        self._rate_limiter = rate_limiter or SmoothRateLimiter(
            rpm=self._settings.model_rpm, tpm=self._settings.model_tpm
        )
        self._executor = executor or AttemptExecutor(
            sessionmaker=sessionmaker,
            provider=provider,
            settings=self._settings,
            repository=self._repo,
            retry_policy=retry_policy,
            rate_limiter=self._rate_limiter,
            allow_reason=allow_reason,
        )
        self._advisory_lock = AdvisoryLock(engine, lock_key)
        self._sessionmaker = sessionmaker

        self._stop_event = asyncio.Event()
        self._outcomes: list[ExecutionOutcome] = []
        self._run_cache: dict[UUID, RunRecord] = {}
        self._survey_cache: dict[UUID, SurveyInput] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def advisory_lock(self) -> AdvisoryLock:
        return self._advisory_lock

    @property
    def limiter(self) -> ConcurrencyLimiter:
        return self._limiter

    @property
    def rate_limiter(self) -> SmoothRateLimiter:
        return self._rate_limiter

    @property
    def executor(self) -> AttemptExecutor:
        return self._executor

    @property
    def outcomes(self) -> list[ExecutionOutcome]:
        return list(self._outcomes)

    async def start(self) -> bool:
        """获取 advisory lock + 重建限流窗口。抢不到锁 → 记录日志并返回 ``False``。"""
        acquired = await self._advisory_lock.try_acquire()
        if not acquired:
            logger.error(
                "another worker holds advisory lock %s; exiting without scheduling",
                self._advisory_lock.key,
            )
            return False
        await self.rebuild_rate_window()
        self._stop_event.clear()
        return True

    async def stop(self) -> None:
        """优雅停止：置停止位并释放锁。"""
        self._stop_event.set()
        await self._advisory_lock.release()

    # ------------------------------------------------------------------
    # 限流窗口重建（裁决 C3）
    # ------------------------------------------------------------------

    async def rebuild_rate_window(self) -> int:
        """依据 ``attempts`` 最近 60 秒记录保守重建限流窗口。

        重启即清空限额视为缺陷；本方法在 :meth:`start` 中被调用。
        """
        since = datetime.now(UTC) - timedelta(seconds=RATE_WINDOW_SECONDS)
        async with self._sessionmaker() as session:
            records = await self._repo.recent_attempt_tokens(
                session,
                since=since,
                max_output_tokens=self._settings.model_max_output_tokens,
            )
        added = self._rate_limiter.rebuild_from_history(records)
        logger.info("rate window rebuilt from %d recent attempts", added)
        return added

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    async def serve(
        self,
        run_id: UUID,
        *,
        stop_event: asyncio.Event | None = None,
        timeout: float | None = None,
    ) -> str:
        """调度 ``run_id`` 直至收敛/暂停/停止。返回最终 run 状态。

        - 启动 ``concurrency`` 个异步消费者（每个完成一个即再领取一个 → 滚动补位）。
        - 后台任务：租约续期、过期扫描、锁健康检查。
        """
        if not self._advisory_lock.held:
            started = await self.start()
            if not started:
                return "locked"

        external_stop = stop_event
        own_stop = self._stop_event

        def should_stop() -> bool:
            return own_stop.is_set() or (external_stop is not None and external_stop.is_set())

        consumers = [
            asyncio.create_task(self._consumer(run_id, should_stop))
            for _ in range(self._concurrency)
        ]
        background = [
            asyncio.create_task(self._renew_loop(run_id, should_stop)),
            asyncio.create_task(self._scan_loop(run_id, should_stop)),
            asyncio.create_task(self._lock_watchdog(should_stop)),
        ]
        try:
            if timeout is not None:
                await asyncio.wait_for(asyncio.gather(*consumers), timeout)
            else:
                await asyncio.gather(*consumers)
        except TimeoutError:
            logger.warning("worker serve timed out for run %s", run_id)
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)

        async with self._sessionmaker() as session:
            async with session.begin():
                await self._repo.converge_run(session, run_id)
        async with self._sessionmaker() as session:
            run = await self._repo.get_run(session, run_id)
        return run.status if run is not None else "missing"

    async def _consumer(self, run_id: UUID, should_stop: Any) -> None:
        while not should_stop():
            if not self._advisory_lock.held:
                break
            try:
                # P2-2（结构性修复）：**先占槽，再领取，再发请求**。
                # 这样「DB 侧在途（attempts.status='running'）≤ 槽位上限」由结构保证，
                # 而非依赖 consumers == agent_concurrency 的配置巧合。
                async with self._limiter.slot():
                    result = await self._claim_once(run_id)
                    if result.claims:
                        await self._process(run_id, result.claims[0])
                if result.claims:
                    continue
                if result.run_status in TERMINAL_RUN_STATUSES:
                    break
                if result.run_status in PAUSING_RUN_STATUSES or result.run_status == "missing":
                    break
                if result.pause_reason is not None:
                    break
                if await self._all_members_terminal(run_id):
                    await self._converge_once(run_id)
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单个消费者异常不得让调度器减员；记录后继续（避免并发容量悄悄下降）。
                logger.exception("worker consumer error for run %s", run_id)
            await asyncio.sleep(self._poll_interval)

    async def _process(self, run_id: UUID, claim: Claim) -> None:
        """执行一个已领取的成员（**调用方须已持有并发槽位**，见 :meth:`_consumer`）。"""
        run = self._run_cache.get(run_id)
        if run is None:
            run = await self._get_run(run_id)
        if run is None:
            return
        survey = self._survey_for(run)
        try:
            outcome = await self._executor.execute(run=run, claim=claim, survey=survey)
        except Exception:
            # 单个成员异常不得拖垮调度器；成员保持 running，租约到期后由恢复路径处理。
            logger.exception("unexpected error executing member %s", claim.member_id)
            return
        self._outcomes.append(outcome)
        if outcome.pause_reason is not None:
            await self._apply_pause(run_id, outcome.pause_reason)

    async def _claim_once(self, run_id: UUID) -> ClaimResult:
        if not self._advisory_lock.held:
            return ClaimResult(run_status="locked")
        run = await self._get_run(run_id)
        if run is None:
            return ClaimResult(run_status="missing")
        self._survey_for(run)  # 填充问卷缓存（供预留估计使用）
        reservation_fn = self._make_reservation_fn(run_id)
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await self._repo.claim_members(
                    session,
                    run_id=run_id,
                    limit=1,
                    lease_seconds=self._lease_seconds,
                    reservation_fn=reservation_fn,
                )
        if result.pause_reason is not None:
            await self._apply_pause(run_id, result.pause_reason)
        return result

    async def claim_once(self, run_id: UUID) -> ClaimResult:
        """公开的单次领取（测试/观测用；语义同 :meth:`_claim_once`）。"""
        return await self._claim_once(run_id)

    async def _apply_pause(self, run_id: UUID, reason: str) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._repo.request_pause(session, run_id=run_id, reason=reason)

    async def _converge_once(self, run_id: UUID) -> str | None:
        async with self._sessionmaker() as session:
            async with session.begin():
                return await self._repo.converge_run(session, run_id)

    async def _all_members_terminal(self, run_id: UUID) -> bool:
        async with self._sessionmaker() as session:
            counts = await self._repo.count_members_by_status(session, run_id)
        return counts.non_terminal == 0

    # ------------------------------------------------------------------
    # 租约续期 / 过期扫描 / 锁看门狗
    # ------------------------------------------------------------------

    async def _renew_loop(self, run_id: UUID, should_stop: Any) -> None:
        while not should_stop():
            await asyncio.sleep(self._renew_interval)
            if should_stop():
                return
            await self.renew_leases_once(run_id)

    async def renew_leases_once(self, run_id: UUID) -> int:
        """给所有仍持租的 ``running`` 成员续租；失租（0 行）由 CAS 后续保证不覆盖。"""
        renewed = 0
        async with self._sessionmaker() as session:
            async with session.begin():
                members = await self._repo.list_members(
                    session, run_id, statuses=("running",)
                )
                for member in members:
                    if member.lease_token is None:
                        continue
                    ok = await self._repo.renew_lease(
                        session,
                        member_id=member.id,
                        lease_token=member.lease_token,
                        lease_seconds=self._lease_seconds,
                    )
                    if ok:
                        renewed += 1
        return renewed

    async def _scan_loop(self, run_id: UUID, should_stop: Any) -> None:
        while not should_stop():
            await asyncio.sleep(self._scan_interval)
            if should_stop():
                return
            await self.scan_expired_once(run_id)

    async def scan_expired_once(self, run_id: UUID) -> int:
        """扫描过期租约 → abandoned attempt + 成员 retry_wait/failed，并尝试收敛。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                recoveries = await self._repo.abandon_expired(session, run_id=run_id)
                await self._repo.converge_run(session, run_id)
        if recoveries:
            logger.warning("recovered %d expired leases for run %s", len(recoveries), run_id)
        return len(recoveries)

    async def _lock_watchdog(self, should_stop: Any) -> None:
        while not should_stop():
            await asyncio.sleep(self._lock_check_interval)
            if should_stop():
                return
            healthy = await self._advisory_lock.health_check()
            if not healthy:
                self._stop_event.set()
                return

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _survey_for(self, run: RunRecord) -> SurveyInput:
        cached = self._survey_cache.get(run.id)
        if cached is None:
            snapshot = run.survey_snapshot
            cached = SurveyInput.model_validate(
                {
                    "title": snapshot["title"],
                    "product": snapshot["product"],
                    "question": snapshot["question"],
                }
            )
            self._survey_cache[run.id] = cached
        return cached

    async def _get_run(self, run_id: UUID) -> RunRecord | None:
        async with self._sessionmaker() as session:
            run = await self._repo.get_run(session, run_id)
        if run is not None:
            self._run_cache[run_id] = run
        return run

    def _make_reservation_fn(self, run_id: UUID) -> Any:
        """构造费用预留估计函数（使用真实冻结问卷 + 当前 persona）。"""
        survey = self._survey_cache.get(run_id)
        settings = self._settings

        def reservation_fn(persona_snapshot: dict[str, Any]) -> Decimal:
            if survey is None:
                return Decimal("0")
            request = build_request_for_persona(
                survey=survey,
                persona_snapshot=persona_snapshot,
                model=settings.model_name,
                max_output_tokens=settings.model_max_output_tokens,
                timeout_seconds=settings.model_timeout_seconds,
            )
            return reservation_for_request(
                request,
                input_price_per_million=settings.input_price_per_million,
                output_price_per_million=settings.output_price_per_million,
            )

        return reservation_fn


def build_worker(
    *,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    provider: Any,
    settings: Settings | None = None,
    **kwargs: Any,
) -> Worker:
    """按配置构造 worker（便捷工厂）。"""
    return Worker(
        engine=engine,
        sessionmaker=sessionmaker,
        provider=provider,
        settings=settings,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 进程入口（``python -m app.worker.main``；裁决 N7-d）
# ---------------------------------------------------------------------------


#: 无活动批次时，查询下一个活动 run 的轮询间隔（秒）。
ENTRYPOINT_POLL_SECONDS = 0.5


async def find_active_run_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> UUID | None:
    """查找当前应调度的活动批次（同一时刻至多一个，见 ``uq_runs_single_active``）。

    只返回 ``running`` / ``pausing`` / ``cancelling``：``paused`` 无待办（等 ``resume``），
    不需要调度。**只读查询**，不修改任何状态；无活动批次时返回 ``None``。
    """
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT id FROM runs "
                "WHERE status IN ('running', 'pausing', 'cancelling') "
                "ORDER BY created_at LIMIT 1"
            )
        )
        value = result.scalar_one_or_none()
    return value


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """等待 ``seconds`` 秒，或在 ``stop_event`` 被置位时立即返回。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


async def serve_until_stopped(
    worker: Worker,
    sessionmaker: async_sessionmaker[AsyncSession],
    stop_event: asyncio.Event,
) -> None:
    """调度循环：反复查找活动批次并 :meth:`Worker.serve`，直至 ``stop_event`` 被置位。

    - 无活动批次 → 轮询等待（保持进程存活，而非空转退出）。
    - 有活动批次 → ``serve()`` 调度至收敛/暂停/停止后，再查询下一个。
    - 查询数据库异常**不**终止进程，记录后重试（避免瞬时抖动使 worker 掉线）。
    """
    while not stop_event.is_set():
        try:
            run_id = await find_active_run_id(sessionmaker)
        except Exception:
            logger.exception("failed to look up active run; retrying")
            run_id = None
        if run_id is None:
            await _sleep_or_stop(stop_event, ENTRYPOINT_POLL_SECONDS)
            continue
        status = await worker.serve(run_id, stop_event=stop_event)
        logger.info("scheduling finished for run %s (run_status=%s)", run_id, status)


async def _close_provider(provider: Any) -> None:
    """关闭 provider 自建资源（真实 provider 有 ``aclose``；mock 无 → 跳过）。"""
    close = getattr(provider, "aclose", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        await close()


async def run_worker(settings: Settings, stop_event: asyncio.Event) -> int:
    """装配并运行 worker，直到 ``stop_event`` 置位。返回**进程退出码**。

    流程（裁决 N7-d）：
    1. 按 ``settings.database_url`` 建 engine + sessionmaker；按 ``MODEL_PROVIDER`` 建 provider。
    2. :meth:`Worker.start` 抢单 worker advisory lock **并**依据 attempts 最近 60s 重建限流窗口；
       抢锁失败 → 明确日志 + 返回 ``1``（非 0）。
    3. 进入 :func:`serve_until_stopped` 调度循环。
    4. 停止时 :meth:`Worker.stop`（释放锁）→ 关闭 provider → dispose engine → 返回 ``0``。
    """
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    provider = build_provider(settings)
    worker = Worker(
        engine=engine,
        sessionmaker=sessionmaker,
        provider=provider,
        settings=settings,
    )
    try:
        logger.info(
            "worker process starting: concurrency=%d rpm=%d tpm=%d provider=%s model=%s "
            "advisory_lock_key=%d",
            worker.limiter.capacity,
            worker.rate_limiter.rpm,
            worker.rate_limiter.tpm,
            settings.model_provider,
            settings.model_name,
            worker.advisory_lock.key,
        )
        acquired = await worker.start()
        if not acquired:
            logger.error(
                "worker cannot start: another worker already holds advisory lock %d "
                "on this database; second worker must exit (code 1)",
                worker.advisory_lock.key,
            )
            return 1
        logger.info(
            "worker started: advisory_lock_held=%s concurrency=%d rpm=%d tpm=%d "
            "(single-worker advisory lock acquired; scheduling loop begins)",
            worker.advisory_lock.held,
            worker.limiter.capacity,
            worker.rate_limiter.rpm,
            worker.rate_limiter.tpm,
        )
        try:
            await serve_until_stopped(worker, sessionmaker, stop_event)
        finally:
            await worker.stop()
            logger.info("worker stopping: advisory lock released")
        return 0
    finally:
        await _close_provider(provider)
        await engine.dispose()


def _install_stop_signals(stop_event: asyncio.Event) -> None:
    """把 SIGINT / SIGTERM 映射为 ``stop_event``（优雅停止，退出码 0）。"""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


def main() -> int:
    """进程入口：``python -m app.worker.main``。返回退出码（``0`` 正常，``1`` 抢锁失败）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()

    async def _runner() -> int:
        stop_event = asyncio.Event()
        _install_stop_signals(stop_event)
        return await run_worker(settings, stop_event)

    return asyncio.run(_runner())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADVISORY_LOCK_KEY",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_LOCK_CHECK_INTERVAL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_RENEW_INTERVAL",
    "DEFAULT_SCAN_INTERVAL",
    "ENTRYPOINT_POLL_SECONDS",
    "AdvisoryLock",
    "Worker",
    "build_worker",
    "find_active_run_id",
    "main",
    "run_worker",
    "serve_until_stopped",
]
