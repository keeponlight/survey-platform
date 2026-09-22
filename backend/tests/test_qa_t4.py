# SPDX-License-Identifier: GPL-3.0-or-later
"""T4 对抗性复核 —— QA 独立交叉验证（严过关 / software-qa-engineer）。

**不复用工程师的断言方式**：本文件自带 ``QAGatedProvider``、独立 DB 原生 SQL 采样、
自有状态机构造路径。覆盖任务书 §5.2 的 1–9 项。

运行：``uv run pytest tests/test_qa_t4.py -q``（真实 PostgreSQL survey_test）。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import pathlib
import sys
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

_LOAD_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "load"
if str(_LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_LOAD_DIR))

from mock_provider import MockProvider  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.worker.execute import RetryPolicy  # noqa: E402
from app.worker.main import Worker  # noqa: E402
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING  # noqa: E402

TEST_DATABASE_URL = Settings.from_env().test_database_url
FAST_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0)

PRODUCT = {
    "name": "示例产品",
    "description": "用于演示的产品",
    "price": "199.00",
    "price_unit": "元/件",
    "time_range": "未来 30 天",
    "purchase_conditions": None,
}


def make_settings(**overrides) -> Settings:
    base = Settings.from_env()
    # 测试环境的正确取值：mock 是本机唯一可用模式（TEAM-BRIEF §7.5 T6-d / §7.9.2）。
    overrides.setdefault("model_provider", "mock")
    return replace(
        base,
        database_url=TEST_DATABASE_URL,
        test_database_url=TEST_DATABASE_URL,
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("1"),
        model_rpm=1_000_000,
        model_tpm=1_000_000_000,
        **overrides,
    )


def csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


@pytest_asyncio.fixture
async def qa(engine, db_session):
    """独立装配：HTTP client + 独立 sessionmaker（供 DB 原生 SQL 采样）。"""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(sessionmaker=maker, settings=make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://qa") as http:
            yield SimpleNamespace(http=http, maker=maker, engine=engine)


class QAGatedProvider(MockProvider):
    """QA 自带闸门 mock：进入 ``answer`` 计数并阻塞至 ``release()``。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(fixed_latency_s=0.0, **kwargs)
        self._gate_event = asyncio.Event()
        self._open = False
        self.entered = 0

    async def answer(self, request):  # noqa: ANN001, ANN201
        self.entered += 1
        if not self._open:
            await self._gate_event.wait()
        return await super().answer(request)

    def release(self) -> None:
        self._open = True
        self._gate_event.set()

    async def wait_entered(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.entered < n and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return self.entered >= n


async def _raw(maker, sql: str, **params):
    async with maker() as session:
        return (await session.execute(text(sql), params)).all()


async def _scalar(maker, sql: str, **params):
    async with maker() as session:
        return await session.scalar(text(sql), params)


async def _new_run(http, *, key: str | None = None, n: int = 6, **extra) -> str:
    key = key or uuid4().hex
    survey = (await http.post("/api/v1/surveys", json={
        "title": "q", "product": PRODUCT, "question": {},
    })).json()
    imported = (await http.post(
        "/api/v1/imports",
        files={"file": ("p.csv", csv_bytes(n), "text/csv")},
        data={"column_mapping": json.dumps(DEFAULT_COLUMN_MAPPING)},
    )).json()
    body = {
        "survey_id": survey["id"],
        "survey_revision": survey["revision"],
        "import_id": imported["import_id"],
        "model_config_id": "default",
        **extra,
    }
    headers = {"Idempotency-Key": key}
    response = await http.post("/api/v1/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ===========================================================================
# §5.2.1 并发幂等：两个并发 retry-failed 同 Idempotency-Key → attempt_limit 只 +3 一次
# ===========================================================================


async def test_qa_concurrent_duplicate_retry_failed_adds_attempts_once(qa) -> None:
    run_id = UUID(await _new_run(qa.http, n=4))
    async with qa.maker() as s:
        await s.execute(text(
            "UPDATE run_members SET status='failed', attempt_count=3, attempt_limit=3 "
            "WHERE run_id=:r"
        ), {"r": run_id})
        await s.execute(text("UPDATE runs SET status='failed' WHERE id=:r"), {"r": run_id})
        await s.commit()

    key = uuid4().hex
    r1, r2 = await asyncio.gather(
        qa.http.post(f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": key}),
        qa.http.post(f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": key}),
    )
    codes = sorted([r1.status_code, r2.status_code])
    # 串行化（run 行锁）：恰一个 202（首次生效）+ 一个 200（幂等重放）。
    assert codes == [200, 202], (r1.text, r2.text)

    limits = await _raw(
        qa.maker, "SELECT DISTINCT attempt_limit FROM run_members WHERE run_id=:r", r=run_id
    )
    assert [int(x[0]) for x in limits] == [6], limits  # 3 + 3，只加一次（非 9）

    # 幂等记录**已落库**（control_events_json，非内存）。
    events = await _scalar(qa.maker, "SELECT control_events_json FROM runs WHERE id=:r", r=run_id)
    retry_events = [
        e
        for e in events
        if isinstance(e, dict)
        and e.get("action") == "retry-failed"
        and e.get("idempotency_key") == key
    ]
    assert len(retry_events) == 1, events


# ===========================================================================
# §5.2.2 pause 的 DB 侧交叉采样：pending/running 收敛、mock 在途与 DB 逐点一致
# ===========================================================================


async def test_qa_pause_db_crosscheck_converges_to_zero(qa) -> None:
    run_id = UUID(await _new_run(qa.http, n=5))
    assert (await qa.http.post(f"/api/v1/runs/{run_id}/start")).status_code == 202

    provider = QAGatedProvider()
    worker = Worker(
        engine=qa.engine, sessionmaker=qa.maker, provider=provider,
        settings=make_settings(), concurrency=2, lease_seconds=120.0,
        renew_interval=20.0, scan_interval=30.0, poll_interval=0.01,
        lock_check_interval=5.0, retry_policy=FAST_RETRY,
    )
    assert await worker.start() is True
    serve = asyncio.create_task(worker.serve(run_id, timeout=30.0))
    try:
        assert await provider.wait_entered(2, timeout=5.0)
        # 采样：DB running 成员数 与 mock 在途数逐点一致。
        for _ in range(8):
            db_running = await _scalar(
                qa.maker,
                "SELECT count(*) FROM run_members WHERE run_id=:r AND status='running'",
                r=run_id,
            )
            assert int(db_running) == provider.entered == 2, (db_running, provider.entered)
            await asyncio.sleep(0.02)

        assert (await qa.http.post(f"/api/v1/runs/{run_id}/pause")).status_code == 202
        provider.release()
        assert await serve == "paused"

        # 收敛后：DB running=0、attempts running=0。
        assert int(await _scalar(
            qa.maker,
            "SELECT count(*) FROM run_members WHERE run_id=:r AND status='running'",
            r=run_id,
        )) == 0
        assert int(await _scalar(
            qa.maker, "SELECT count(*) FROM attempts WHERE status='running'"
        )) == 0
    finally:
        with contextlib.suppress(Exception):
            await worker.stop()


# ===========================================================================
# §5.2.3 快照不变性（逐字段，原生 SQL diff）
# ===========================================================================


async def test_qa_budget_patch_keeps_all_snapshot_fields(qa) -> None:
    run_id = UUID(await _new_run(qa.http, n=3, budget_limit="10", request_limit=9))
    cols = ("survey_snapshot", "model_snapshot", "sample_size", "prompt_version",
            "source_version", "import_id", "survey_id", "request_hash", "idempotency_key",
            "budget_currency")
    select = ", ".join(cols)
    before = (await _raw(qa.maker, f"SELECT {select} FROM runs WHERE id=:r", r=run_id))[0]

    patch = await qa.http.patch(f"/api/v1/runs/{run_id}/budget",
                                json={"budget_limit": "20", "request_limit": 12})
    assert patch.status_code == 200, patch.text

    after = (await _raw(qa.maker, f"SELECT {select} FROM runs WHERE id=:r", r=run_id))[0]
    diffs = {c: (b, a) for c, b, a in zip(cols, before, after, strict=False) if b != a}
    assert diffs == {}, f"snapshot fields changed: {diffs}"

    # 只有 budget_limit / request_limit 变了。
    assert Decimal(str(patch.json()["budget_limit"])) == Decimal("20")
    assert patch.json()["request_limit"] == 12


# ===========================================================================
# §5.2.4 状态机全量重放 I1–I12（QA 自有构造路径）
# ===========================================================================


async def _force_status(qa, run_id, status: str) -> None:
    rid = run_id if isinstance(run_id, UUID) else UUID(run_id)
    async with qa.maker() as s:
        if status in ("running", "pausing", "paused", "cancelling"):
            # 唯一索引 uq_runs_single_active 是全局的：造活动态前先清掉其它活动行。
            await s.execute(text(
                "UPDATE runs SET status='completed' "
                "WHERE status IN ('running','pausing','paused','cancelling') AND id <> :r"
            ), {"r": rid})
        await s.execute(text("UPDATE runs SET status=:s WHERE id=:r"),
                        {"s": status, "r": rid})
        await s.commit()


async def test_qa_state_machine_replay_I1_I12(qa) -> None:
    results: dict[str, int] = {}

    # I1: 对 running run 再次 start → 409
    r = await _new_run(qa.http, n=2)
    await qa.http.post(f"/api/v1/runs/{r}/start")
    results["I1"] = (await qa.http.post(f"/api/v1/runs/{r}/start")).status_code

    # I2: 存在活动批次时启动新 run → 409
    other = await _new_run(qa.http, n=2)
    results["I2"] = (await qa.http.post(f"/api/v1/runs/{other}/start")).status_code

    # I3: ready run pause/resume/retry-failed → 409
    ready = await _new_run(qa.http, n=2)
    results["I3a"] = (await qa.http.post(f"/api/v1/runs/{ready}/pause")).status_code
    results["I3b"] = (await qa.http.post(f"/api/v1/runs/{ready}/resume")).status_code
    results["I3c"] = (await qa.http.post(f"/api/v1/runs/{ready}/retry-failed",
                                         headers={"Idempotency-Key": uuid4().hex})).status_code

    # I4: completed run pause/resume/cancel → 409
    done = await _new_run(qa.http, n=2)
    await _force_status(qa, done, "completed")
    results["I4a"] = (await qa.http.post(f"/api/v1/runs/{done}/pause")).status_code
    results["I4b"] = (await qa.http.post(f"/api/v1/runs/{done}/resume")).status_code
    results["I4c"] = (await qa.http.post(f"/api/v1/runs/{done}/cancel")).status_code

    # I5: cancelled run start/resume/pause → 409
    cancelled = await _new_run(qa.http, n=2)
    await _force_status(qa, cancelled, "cancelled")
    results["I5a"] = (await qa.http.post(f"/api/v1/runs/{cancelled}/start")).status_code
    results["I5b"] = (await qa.http.post(f"/api/v1/runs/{cancelled}/resume")).status_code
    results["I5c"] = (await qa.http.post(f"/api/v1/runs/{cancelled}/pause")).status_code

    # I6: completed（全成功）retry-failed → 409
    results["I6"] = (await qa.http.post(f"/api/v1/runs/{done}/retry-failed",
                                        headers={"Idempotency-Key": uuid4().hex})).status_code

    # I10: ready retry-failed（无失败成员）→ 409
    results["I10"] = (await qa.http.post(f"/api/v1/runs/{ready}/retry-failed",
                                         headers={"Idempotency-Key": uuid4().hex})).status_code

    # I11: 降预算 → 422；改币种 → 422
    bud = await _new_run(qa.http, n=2, budget_limit="10")
    results["I11a"] = (await qa.http.patch(f"/api/v1/runs/{bud}/budget",
                                           json={"budget_limit": "5"})).status_code
    results["I11b"] = (
        await qa.http.patch(
            f"/api/v1/runs/{bud}/budget",
            json={"budget_limit": "30", "budget_currency": "USD"},
        )
    ).status_code

    # I12: 对 paused run 直接 start → 409
    paused = await _new_run(qa.http, n=2)
    await _force_status(qa, paused, "paused")
    results["I12"] = (await qa.http.post(f"/api/v1/runs/{paused}/start")).status_code

    expected = {
        "I1": 409, "I2": 409, "I3a": 409, "I3b": 409, "I3c": 409,
        "I4a": 409, "I4b": 409, "I4c": 409,
        "I5a": 409, "I5b": 409, "I5c": 409,
        "I6": 409, "I10": 409, "I11a": 422, "I11b": 422, "I12": 409,
    }
    assert results == expected, {
        k: (v, expected[k]) for k, v in results.items() if v != expected[k]
    }


async def test_qa_repeat_pause_cancel_at_target_is_200_start_is_409(qa) -> None:
    """重复 pause/cancel 已达目标状态 → 200；重复 start → 409。"""
    r = await _new_run(qa.http, n=2)
    await qa.http.post(f"/api/v1/runs/{r}/start")
    assert (await qa.http.post(f"/api/v1/runs/{r}/pause")).status_code == 202
    # 已 pausing：重复 pause → 200
    assert (await qa.http.post(f"/api/v1/runs/{r}/pause")).status_code == 200
    # 重复 start（非 ready）→ 409
    assert (await qa.http.post(f"/api/v1/runs/{r}/start")).status_code == 409
    # cancel → 202；重复 cancel（已 cancelling/cancelled）→ 200
    assert (await qa.http.post(f"/api/v1/runs/{r}/cancel")).status_code == 202
    assert (await qa.http.post(f"/api/v1/runs/{r}/cancel")).status_code == 200


# ===========================================================================
# §5.2.5 不得泄漏 500：对每个控制端点灌非法输入
# ===========================================================================


async def test_qa_no_5xx_on_malformed_control_inputs(qa) -> None:
    run_id = await _new_run(qa.http, n=2)
    bad_uuid = "not-a-uuid"
    long_key = "x" * 10000
    probes: list[tuple[str, object]] = []

    # 坏 UUID（路径参数）
    for action in ("start", "pause", "resume", "cancel", "retry-failed"):
        probes.append(
            (f"bad-uuid {action}", await qa.http.post(f"/api/v1/runs/{bad_uuid}/{action}"))
        )
    probes.append(("bad-uuid budget", await qa.http.patch(f"/api/v1/runs/{bad_uuid}/budget",
                                                          json={"budget_limit": "10"})))

    # retry-failed：缺 Idempotency-Key / 超长 key
    probes.append(("retry no key", await qa.http.post(f"/api/v1/runs/{run_id}/retry-failed")))
    probes.append(("retry long key", await qa.http.post(
        f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": long_key})))

    # budget：坏 JSON / 未知字段 / 空 body / 非数字 / 错误 Content-Type
    probes.append(("budget bad json", await qa.http.patch(
        f"/api/v1/runs/{run_id}/budget",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )))
    probes.append(("budget unknown field", await qa.http.patch(
        f"/api/v1/runs/{run_id}/budget", json={"budget_limit": "10", "foo": "bar"})))
    probes.append(("budget empty body", await qa.http.patch(
        f"/api/v1/runs/{run_id}/budget",
        content=b"",
        headers={"Content-Type": "application/json"},
    )))
    probes.append(("budget non-numeric", await qa.http.patch(
        f"/api/v1/runs/{run_id}/budget", json={"budget_limit": "abc"})))
    probes.append(("budget wrong content-type", await qa.http.patch(
        f"/api/v1/runs/{run_id}/budget", content=b"budget_limit=10",
        headers={"Content-Type": "text/plain"})))

    # 控制端点灌 JSON body / 错误 Content-Type（应被忽略或 4xx）
    for action in ("start", "pause", "resume", "cancel"):
        probes.append((f"{action} junk body", await qa.http.post(
            f"/api/v1/runs/{run_id}/{action}",
            content=b"junk",
            headers={"Content-Type": "application/json"},
        )))
        probes.append((f"{action} wrong ct", await qa.http.post(
            f"/api/v1/runs/{run_id}/{action}",
            content=b"x",
            headers={"Content-Type": "text/plain"},
        )))

    offenders = [(name, resp.status_code, resp.text[:200])
                 for name, resp in probes if resp.status_code >= 500]
    assert offenders == [], f"5xx leaked: {offenders}"


# ===========================================================================
# §5.2.6 活动批次唯一性：paused 仍占位；running 期间第二个 409；恰 1×202
# ===========================================================================


async def test_qa_paused_run_still_occupies_active_slot(qa) -> None:
    a = await _new_run(qa.http, n=2)
    assert (await qa.http.post(f"/api/v1/runs/{a}/start")).status_code == 202
    await _force_status(qa, a, "paused")
    # 造第二个 run，start → 409（paused 仍占活动位）
    b = await _new_run(qa.http, n=2)
    resp = await qa.http.post(f"/api/v1/runs/{b}/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "ACTIVE_RUN_EXISTS"


async def test_qa_concurrent_start_exactly_one_202(qa) -> None:
    ids = [await _new_run(qa.http, n=2) for _ in range(5)]
    responses = await asyncio.gather(*(qa.http.post(f"/api/v1/runs/{r}/start") for r in ids))
    codes = [r.status_code for r in responses]
    assert codes.count(202) == 1, codes
    assert codes.count(409) == 4, codes
    active = await _scalar(
        qa.maker,
        "SELECT count(*) FROM runs WHERE status IN ('running','pausing','paused','cancelling')",
    )
    assert int(active) == 1


# ===========================================================================
# §5.2.7 取消的收敛责任：cancel(ready, 有活动批次) 是否泄漏 500
# ===========================================================================


async def test_qa_cancel_ready_while_another_active_does_not_500(qa, engine) -> None:
    """ready run cancel 会转为 cancelling（活动态）；若已有活动批次，会命中
    ``uq_runs_single_active`` —— 必须映射为 4xx，**不得 500**。

    用 ``raise_app_exceptions=False`` 的 client 捕获真实 HTTP 状态码（否则未处理异常会
    在 ASGI 传输层直接抛出，看不到 500）。
    """
    # 用真实 HTTP 语义的 client（未处理异常 → 500，而非抛进测试进程）。
    maker = qa.maker
    app = create_app(sessionmaker=maker, settings=make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://qa") as http:
            a = await _new_run(http, n=2)
            assert (await http.post(f"/api/v1/runs/{a}/start")).status_code == 202  # A 活动
            b = await _new_run(http, n=2)  # B 为 ready（与活动批次并存是允许的）
            resp = await http.post(f"/api/v1/runs/{b}/cancel")
            assert resp.status_code < 500, f"cancel leaked {resp.status_code}: {resp.text[:300]}"
            assert resp.status_code in (200, 202, 409), resp.text


async def test_qa_cancelling_waits_for_convergence_owner(qa) -> None:
    """查清 ``cancelling → cancelled`` 的收敛责任方：无 worker 时是否永久停留。"""
    run_id = UUID(await _new_run(qa.http, n=3))
    assert (await qa.http.post(f"/api/v1/runs/{run_id}/start")).status_code == 202
    # 人为造一个「在途」成员（持租 running、租约已过期），模拟请求发出后进程崩溃。
    async with qa.maker() as s:
        await s.execute(text(
            "UPDATE run_members SET status='running', lease_token=gen_random_uuid(), "
            "lease_expires_at=now() - interval '1 second', attempt_count=1 "
            "WHERE run_id=:r AND row_no=1"
        ), {"r": run_id})
        await s.commit()

    assert (await qa.http.post(f"/api/v1/runs/{run_id}/cancel")).status_code == 202
    # 无 worker：cancelling 不会自行推进（责任方 = worker 的过期扫描/收敛循环）。
    statuses = []
    for _ in range(5):
        statuses.append(await _scalar(qa.maker, "SELECT status FROM runs WHERE id=:r", r=run_id))
        await asyncio.sleep(0.05)
    assert set(statuses) == {"cancelling"}, statuses

    # worker 的过期扫描 + 收敛负责推进（证明「可恢复」，非死结）。
    worker = Worker(
        engine=qa.engine, sessionmaker=qa.maker, provider=MockProvider(fixed_latency_s=0.0),
        settings=make_settings(), concurrency=2, lease_seconds=120.0,
        renew_interval=20.0, scan_interval=0.05, poll_interval=0.01,
        lock_check_interval=5.0, retry_policy=FAST_RETRY,
    )
    recovered = await worker.scan_expired_once(run_id)
    assert recovered == 1, recovered
    final = await _scalar(qa.maker, "SELECT status FROM runs WHERE id=:r", r=run_id)
    assert final == "cancelled", final


# ===========================================================================
# §5.2.8 失败不可隐藏：completed_with_errors 原样出现在 status 与 allowed_actions
# ===========================================================================


async def test_qa_completed_with_errors_not_normalized(qa) -> None:
    run_id = await _new_run(qa.http, n=3)
    await _force_status(qa, run_id, "completed_with_errors")
    view = (await qa.http.get(f"/api/v1/runs/{run_id}")).json()
    assert view["status"] == "completed_with_errors"
    assert view["allowed_actions"] == ["retry-failed"]
    assert view["counts"]["valid_count"] == 0  # 未兜成假数字

    summary = (await qa.http.get(f"/api/v1/runs/{run_id}/summary")).json()
    assert summary["valid_count"] == 0


# ===========================================================================
# §5.2.9 resume 后 pause_reason 必须清为 NULL（DB 原值）
# ===========================================================================


async def test_qa_resume_clears_pause_reason_in_db(qa) -> None:
    run_id = await _new_run(qa.http, n=2)
    await _force_status(qa, run_id, "paused")
    async with qa.maker() as s:
        await s.execute(
            text("UPDATE runs SET pause_reason='user' WHERE id=:r"), {"r": UUID(run_id)}
        )
        await s.commit()
    assert (
        await _scalar(qa.maker, "SELECT pause_reason FROM runs WHERE id=:r", r=UUID(run_id))
    ) == "user"

    resp = await qa.http.post(f"/api/v1/runs/{run_id}/resume")
    assert resp.status_code == 202, resp.text
    pause_reason = await _scalar(
        qa.maker, "SELECT pause_reason FROM runs WHERE id=:r", r=UUID(run_id)
    )
    assert pause_reason is None, f"pause_reason not cleared: {pause_reason!r}"
    assert resp.json()["pause_reason"] is None
    assert resp.json()["pause_hint"] is None


# ===========================================================================
# §5.3 retry-failed 幂等记录确实落库（control_events_json，非内存）
# ===========================================================================


async def test_qa_retry_failed_idempotency_record_persisted_in_db(qa) -> None:
    run_id = UUID(await _new_run(qa.http, n=3))
    await _force_status(qa, run_id, "failed")
    async with qa.maker() as s:
        await s.execute(
            text("UPDATE run_members SET status='failed', attempt_count=3 WHERE run_id=:r"),
            {"r": run_id},
        )
        await s.commit()

    key = uuid4().hex
    first = await qa.http.post(
        f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": key}
    )
    assert first.status_code == 202, first.text
    # 从**独立连接**读库（非 API 出参、非内存）。
    events = await _scalar(
        qa.maker, "SELECT control_events_json::text FROM runs WHERE id=:r", r=run_id
    )
    assert key in events, events
    assert "retry-failed" in events, events


# ===========================================================================
# §5.2.5 补充：bad-uuid 路径参数必须为 422（FastAPI 校验），不得 500
# ===========================================================================


@pytest.mark.parametrize("action", ["start", "pause", "resume", "cancel", "retry-failed"])
async def test_qa_bad_uuid_path_returns_422_not_500(qa, action: str) -> None:
    resp = await qa.http.post(f"/api/v1/runs/not-a-uuid/{action}")
    assert resp.status_code == 422, resp.text


# ===========================================================================
# P2-⑤ 修复验证：POST /runs 的 model_snapshot 取自**注入的** settings（非进程单例）
# ===========================================================================


async def test_qa_post_runs_snapshot_uses_injected_settings(qa) -> None:
    """P2-⑤（TEAM-BRIEF §7.11.2）修复验证。

    ``POST /runs`` 的 ``model_snapshot`` 必须取自路由**注入的** settings
    （``create_app(settings=...)`` → ``app.state.settings``），而非进程级
    ``get_settings()`` 单例。

    区分构造：注入 app 的 settings（``qa`` fixture 的 ``make_settings()`` → ``mock``）与
    进程级单例**故意设成不同**（``openai_compatible``）。用 **DB 原生 SQL** 读
    ``runs.model_snapshot``，断言其 ``provider`` == 注入值，且 != 单例值。
    """
    from app import config as app_config

    previous = app_config.get_settings()
    singleton_provider = "openai_compatible"
    injected_provider = "mock"  # 与 qa fixture 的 make_settings() 一致
    app_config.set_settings(
        replace(app_config.get_settings(), model_provider=singleton_provider, model_name="gpt-x")
    )
    try:
        run_id = await _new_run(qa.http, n=2)
    finally:
        app_config.set_settings(previous)

    snapshot = await _scalar(
        qa.maker, "SELECT model_snapshot FROM runs WHERE id=:r", r=UUID(run_id)
    )
    assert isinstance(snapshot, dict), snapshot
    # 快照**如实反映注入的 provider**（修复前会错记为进程单例的 openai_compatible）。
    assert snapshot.get("provider") == injected_provider, snapshot
    assert snapshot.get("provider") != singleton_provider
