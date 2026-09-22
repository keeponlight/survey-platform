# SPDX-License-Identifier: GPL-3.0-or-later
"""T4 控制 API 与状态收敛（主文档 §6.2/§6.4/§8.2；architecture.md §3.1/§3.3/§6.8）。

验收命令：``uv run pytest tests/test_run_controls.py tests/test_api.py -q``

覆盖：

- 暂停后不再发新请求；在途完成并收敛 ``paused``（**真实 worker + 可控 mock provider**）。
- 恢复只跑剩余成员（已成功成员 ``attempt_count`` 不变，不被重跑）。
- 取消收敛 ``cancelled``；未开始成员转 ``cancelled``；**已成功答案保留**。
- ``retry-failed`` 只处理 ``failed`` 成员；同幂等键重复调用不多加额度。
- 提预算逐字段不改快照；降预算 / 改币种 422。
- 第二活动批次 start → 409；重复 pause/cancel 幂等 200；并发 start 恰好 1 个成功。
- ``I1–I12`` 非法/合法转移矩阵（architecture.md §3.3）。

**全部打真实 PostgreSQL（``survey_test``）**；用真实领取事务，不 mock 数据库。
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
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

# --- 项目根 tests/load 加入 sys.path，以顶层模块名导入 mock（避免与 tests 包冲突）---
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

#: 快速重试（避免真实 2s/8s 退避拖慢用例）。
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
    # 测试环境的正确取值：本机唯一可用模式是 mock（TEAM-BRIEF §7.5 T6-d / §7.9.2）。
    # 显式声明，避免 Q6 门禁（真实 provider + 空 endpoint → 422）误伤既有 HTTP 用例；
    # 仍可被显式 overrides 覆盖（如门禁用例注入真实 provider）。
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


def survey_payload(title: str = "购买意向问卷") -> dict:
    return {"title": title, "product": PRODUCT, "question": {}}


def csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(engine, db_session):
    """装配测试 app（注入测试库会话工厂）并运行 lifespan。"""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(sessionmaker=maker, settings=make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


class WorkerRunner:
    """按需构造 worker 并在用例结束后统一停止（释放 advisory lock 专用连接）。"""

    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.workers: list[Worker] = []

    def make(self, provider, *, concurrency: int = 2, **kwargs) -> Worker:
        worker = Worker(
            engine=self.engine,
            sessionmaker=self.maker,
            provider=provider,
            settings=make_settings(),
            concurrency=concurrency,
            lease_seconds=kwargs.pop("lease_seconds", 120.0),
            renew_interval=kwargs.pop("renew_interval", 20.0),
            scan_interval=kwargs.pop("scan_interval", 30.0),
            poll_interval=kwargs.pop("poll_interval", 0.01),
            lock_check_interval=kwargs.pop("lock_check_interval", 5.0),
            retry_policy=kwargs.pop("retry_policy", FAST_RETRY),
            **kwargs,
        )
        self.workers.append(worker)
        return worker


@pytest_asyncio.fixture
async def workers(engine):
    runner = WorkerRunner(engine, async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield runner
    finally:
        for worker in runner.workers:
            with contextlib.suppress(Exception):
                await worker.stop()


class GatedProvider(MockProvider):
    """可控 mock provider：进入 ``answer`` 时计数，并阻塞到 :meth:`release` 被调用。

    用于实测「暂停后在途请求继续保存、且不再领取新成员」「取消收敛」等。
    """

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
        """放行所有在途请求；此后新请求不再阻塞。"""
        self._open = True
        self._gate_event.set()

    async def wait_entered(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.entered < n and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return self.entered >= n


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _create_survey(client: httpx.AsyncClient) -> dict:
    response = await client.post("/api/v1/surveys", json=survey_payload())
    assert response.status_code == 201, response.text
    return response.json()


async def _create_import(client: httpx.AsyncClient, n: int = 6) -> dict:
    response = await client.post(
        "/api/v1/imports",
        files={"file": ("p.csv", csv_bytes(n), "text/csv")},
        data={"column_mapping": json.dumps(DEFAULT_COLUMN_MAPPING)},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_run(
    client: httpx.AsyncClient,
    *,
    key: str | None = None,
    n: int = 6,
    budget_limit: str | None = None,
    request_limit: int | None = None,
) -> httpx.Response:
    survey = await _create_survey(client)
    imported = await _create_import(client, n)
    body: dict = {
        "survey_id": survey["id"],
        "survey_revision": survey["revision"],
        "import_id": imported["import_id"],
        "model_config_id": "default",
    }
    if budget_limit is not None:
        body["budget_limit"] = budget_limit
    if request_limit is not None:
        body["request_limit"] = request_limit
    headers = {"Idempotency-Key": key} if key else {}
    return await client.post("/api/v1/runs", json=body, headers=headers)


async def _run_id(client: httpx.AsyncClient, **kwargs) -> str:
    response = await _create_run(client, key=uuid4().hex, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _started_run(client: httpx.AsyncClient, **kwargs) -> str:
    run_id = await _run_id(client, **kwargs)
    started = await client.post(f"/api/v1/runs/{run_id}/start")
    assert started.status_code == 202, started.text
    return run_id


async def _run_with_status(
    client: httpx.AsyncClient, db_session, status: str, *, n: int = 3
) -> str:
    """建 run 并把状态强制为 ``status``（用于非法转移矩阵的状态构造）。"""
    run_id = await _run_id(client, n=n)
    await db_session.execute(
        text("UPDATE runs SET status = :s WHERE id = :r"),
        {"s": status, "r": UUID(run_id)},
    )
    await db_session.commit()
    return run_id


async def _get_view(client: httpx.AsyncClient, run_id: str) -> dict:
    response = await client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 暂停 / 收敛
# ---------------------------------------------------------------------------


async def test_pause_stops_new_requests(client: httpx.AsyncClient, workers: WorkerRunner) -> None:
    """暂停后 worker **不再领取新成员**（真实 worker + 可控 mock 实测，非断言配置值）。"""
    run_id = await _started_run(client, n=5)

    provider = GatedProvider()
    worker = workers.make(provider, concurrency=2)
    assert await worker.start() is True
    serve_task = asyncio.create_task(worker.serve(UUID(run_id), timeout=30.0))

    assert await provider.wait_entered(2, timeout=5.0), provider.entered
    await asyncio.sleep(0.2)
    entered_before_pause = provider.entered
    assert entered_before_pause == 2  # 两个槽位占满，不会再领取

    pause = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert pause.status_code == 202, pause.text
    assert pause.json()["pause_reason"] == "user"

    provider.release()
    status = await serve_task
    assert status == "paused"

    # 暂停后**没有**新请求（实测，非断言配置）。
    assert provider.entered == entered_before_pause

    view = await _get_view(client, run_id)
    assert view["status"] == "paused"
    assert view["counts"]["succeeded"] == 2
    assert view["counts"]["pending"] == 3
    assert view["in_flight"] == 0


async def test_pause_converges_after_inflight(
    client: httpx.AsyncClient, workers: WorkerRunner, db_session
) -> None:
    """在途请求允许完成并保存，全部收敛后 run 变 ``paused``；在途已成功答案不丢失。"""
    run_id = UUID(await _started_run(client, n=4))

    provider = GatedProvider()
    worker = workers.make(provider, concurrency=2)
    assert await worker.start() is True
    serve_task = asyncio.create_task(worker.serve(run_id, timeout=30.0))

    assert await provider.wait_entered(2, timeout=5.0)

    pause = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert pause.status_code == 202, pause.text
    mid = await _get_view(client, str(run_id))
    assert mid["status"] == "pausing"  # 尚有在途 → 未收敛
    assert mid["in_flight"] == 2

    provider.release()
    assert await serve_task == "paused"

    rows = (
        await db_session.execute(
            text(
                "SELECT status, answer_json FROM run_members WHERE run_id = :r ORDER BY row_no"
            ),
            {"r": run_id},
        )
    ).all()
    succeeded = [row for row in rows if row[0] == "succeeded"]
    assert len(succeeded) == 2
    for _status, answer in succeeded:
        assert isinstance(answer, dict) and answer.get("value")  # 在途答案已保存、未丢失

    view = await _get_view(client, str(run_id))
    assert view["status"] == "paused"
    assert view["counts"]["succeeded"] == 2
    assert view["in_flight"] == 0


async def test_pause_with_no_inflight_converges_to_paused(
    client: httpx.AsyncClient, db_session
) -> None:
    """P2-1（§7.9.3）：**无在途成员**时 ``pause`` → 响应体与 DB 均为 ``paused``（与 cancel 对称）。

    语义依据 §6.4「全部返回或收敛后设 paused」——在途为 0 时该条件平凡成立。
    （有在途时保持 ``pausing`` 已由 ``test_pause_converges_after_inflight`` 断言。）
    """
    run_id = UUID(await _started_run(client, n=3))  # 无 worker → 无在途成员

    response = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "paused"
    assert response.json()["pause_reason"] == "user"

    await db_session.commit()  # 读最新提交快照
    status = await db_session.scalar(
        text("SELECT status FROM runs WHERE id=:r"), {"r": run_id}
    )
    assert status == "paused", status
    in_flight = await db_session.scalar(
        text("SELECT count(*) FROM run_members WHERE run_id=:r AND status='running'"),
        {"r": run_id},
    )
    assert int(in_flight) == 0


# ---------------------------------------------------------------------------
# 恢复：只跑剩余
# ---------------------------------------------------------------------------


async def test_resume_only_remaining(
    client: httpx.AsyncClient, workers: WorkerRunner, db_session
) -> None:
    """resume 后只跑剩余成员；已成功成员不被重跑（``attempt_count`` 不变）。"""
    run_id = UUID(await _started_run(client, n=4))

    provider = GatedProvider()
    worker = workers.make(provider, concurrency=2)
    assert await worker.start() is True
    serve1 = asyncio.create_task(worker.serve(run_id, timeout=30.0))
    assert await provider.wait_entered(2, timeout=5.0)

    pause = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert pause.status_code == 202
    provider.release()
    assert await serve1 == "paused"
    assert provider.entered == 2

    succeeded_before = (
        await db_session.execute(
            text(
                "SELECT persona_id, attempt_count FROM run_members "
                "WHERE run_id = :r AND status = 'succeeded'"
            ),
            {"r": run_id},
        )
    ).all()
    assert len(succeeded_before) == 2
    before_map = {row[0]: int(row[1]) for row in succeeded_before}
    await db_session.commit()  # 结束只读事务，后续读取新快照

    resume = await client.post(f"/api/v1/runs/{run_id}/resume")
    assert resume.status_code == 202, resume.text
    assert resume.json()["status"] == "running"

    serve2 = asyncio.create_task(worker.serve(run_id, timeout=30.0))
    assert await serve2 == "completed"

    # 只调用 4 次（2 已成功成员未被重跑）。
    assert provider.entered == 4

    after = (
        await db_session.execute(
            text("SELECT persona_id, attempt_count, status FROM run_members WHERE run_id = :r"),
            {"r": run_id},
        )
    ).all()
    after_map = {row[0]: (int(row[1]), row[2]) for row in after}
    for persona_id, count_before in before_map.items():
        count_after, status_after = after_map[persona_id]
        assert count_after == count_before == 1  # 已成功成员 attempt_count 不变
        assert status_after == "succeeded"
    assert all(value[1] == "succeeded" for value in after_map.values())

    view = await _get_view(client, str(run_id))
    assert view["status"] == "completed"
    assert view["counts"]["succeeded"] == 4


# ---------------------------------------------------------------------------
# 取消：收敛 cancelled
# ---------------------------------------------------------------------------


async def test_cancel_converges(
    client: httpx.AsyncClient, workers: WorkerRunner, db_session
) -> None:
    """取消收敛到 ``cancelled``；未开始成员转 ``cancelled``；**已成功答案保留**。"""
    run_id = UUID(await _started_run(client, n=4))

    provider = GatedProvider()
    worker = workers.make(provider, concurrency=2)
    assert await worker.start() is True
    serve_task = asyncio.create_task(worker.serve(run_id, timeout=30.0))
    assert await provider.wait_entered(2, timeout=5.0)

    cancel = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancel.status_code == 202, cancel.text

    provider.release()
    assert await serve_task == "cancelled"

    view = await _get_view(client, str(run_id))
    assert view["status"] == "cancelled"
    assert view["counts"]["succeeded"] == 2  # 在途已成功答案保留
    assert view["counts"]["cancelled"] == 2  # 未开始成员转 cancelled
    assert view["counts"]["pending"] == 0

    rows = (
        await db_session.execute(
            text("SELECT status, answer_json FROM run_members WHERE run_id = :r"),
            {"r": run_id},
        )
    ).all()
    succeeded = [row for row in rows if row[0] == "succeeded"]
    assert len(succeeded) == 2
    for _status, answer in succeeded:
        assert isinstance(answer, dict) and answer.get("value")


async def test_cancel_ready_run_while_another_active_does_not_500(
    client: httpx.AsyncClient, db_session
) -> None:
    """**P1-1 回归**（TEAM-BRIEF §7.9.1）：另一批次 ``running`` 时 ``cancel`` 一个 ``ready`` run
    → **无 5xx**；该 run 一次性收敛为 ``cancelled``（不经活动态 ``cancelling``），
    其成员全部 ``cancelled``、租约清空。**用 DB 原生 SQL 断言**（不只看响应体）。

    根因：``ready``（非活动态）→ ``cancelling``（活动态）会撞部分唯一索引
    ``uq_runs_single_active``，旧实现未捕获 ``IntegrityError`` → 500。
    """
    active_id = await _started_run(client, n=2)  # A：running（占活动位）
    target_id = UUID(await _run_id(client, n=3))  # B：ready（与活动批次并存合法）

    response = await client.post(f"/api/v1/runs/{target_id}/cancel")
    assert response.status_code < 500, response.text
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert body["allowed_actions"] == []

    await db_session.commit()  # 结束只读事务，读最新提交快照
    run_row = (
        await db_session.execute(
            text("SELECT status, finished_at FROM runs WHERE id=:r"), {"r": target_id}
        )
    ).one()
    assert run_row[0] == "cancelled"
    assert run_row[1] is not None  # finished_at 已写

    # 该 run 的成员全部 cancelled；无 pending/retry_wait 残留；租约清空。
    statuses = (
        await db_session.execute(
            text("SELECT DISTINCT status FROM run_members WHERE run_id=:r"), {"r": target_id}
        )
    ).scalars().all()
    assert set(statuses) == {"cancelled"}, statuses
    pending_left = await db_session.scalar(
        text(
            "SELECT count(*) FROM run_members WHERE run_id=:r "
            "AND status IN ('pending','retry_wait')"
        ),
        {"r": target_id},
    )
    assert int(pending_left) == 0
    leased = await db_session.scalar(
        text(
            "SELECT count(*) FROM run_members WHERE run_id=:r "
            "AND (lease_token IS NOT NULL OR lease_expires_at IS NOT NULL)"
        ),
        {"r": target_id},
    )
    assert int(leased) == 0

    # 审计事件：追加 action="cancel"、result="cancelled"。
    events = await db_session.scalar(
        text("SELECT control_events_json FROM runs WHERE id=:r"), {"r": target_id}
    )
    cancel_events = [
        event for event in events if isinstance(event, dict) and event.get("action") == "cancel"
    ]
    assert cancel_events and cancel_events[-1]["result"] == "cancelled", events

    # 活动批次 A 不受影响，仍 running。
    assert (
        await db_session.scalar(text("SELECT status FROM runs WHERE id=:r"), {"r": UUID(active_id)})
        == "running"
    )


# ---------------------------------------------------------------------------
# retry-failed
# ---------------------------------------------------------------------------


async def test_retry_failed_only_failed_members(
    client: httpx.AsyncClient, db_session
) -> None:
    """只有 ``failed`` 成员被重新安排；``succeeded`` 成员完全不动。"""
    run_id = UUID(await _run_id(client, n=6))
    await db_session.execute(
        text(
            "UPDATE run_members SET status='succeeded', "
            "answer_json=jsonb_build_object('question_id','purchase_intent','value','unsure'), "
            "attempt_count=1 WHERE run_id=:r AND row_no IN (1,2)"
        ),
        {"r": run_id},
    )
    await db_session.execute(
        text(
            "UPDATE run_members SET status='failed', last_error_code='INVALID_OUTPUT', "
            "attempt_count=3, attempt_limit=3 WHERE run_id=:r AND row_no IN (3,4)"
        ),
        {"r": run_id},
    )
    await db_session.execute(
        text(
            "UPDATE run_members SET status='retry_wait', next_attempt_at=now(), attempt_count=1 "
            "WHERE run_id=:r AND row_no=5"
        ),
        {"r": run_id},
    )
    await db_session.execute(
        text("UPDATE runs SET status='completed_with_errors', started_at=now(), finished_at=now() "
             "WHERE id=:r"),
        {"r": run_id},
    )
    await db_session.commit()

    response = await client.post(
        f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["finished_at"] is None  # 清空
    assert body["started_at"] is not None  # 保留

    rows = (
        await db_session.execute(
            text(
                "SELECT row_no, status, attempt_limit, attempt_count FROM run_members "
                "WHERE run_id = :r ORDER BY row_no"
            ),
            {"r": run_id},
        )
    ).all()
    by_row = {int(row[0]): row for row in rows}

    # failed → pending，+3 额度；attempt_count 保留（单调递增）
    for row_no in (3, 4):
        _no, status, limit, count = by_row[row_no]
        assert status == "pending"
        assert int(limit) == 6  # 3 + 3
        assert int(count) == 3  # 保留

    # succeeded 完全不动
    for row_no in (1, 2):
        _no, status, limit, _count = by_row[row_no]
        assert status == "succeeded"
        assert int(limit) == 3

    # retry_wait 不动（不属于 failed）
    _no, status, limit, _count = by_row[5]
    assert status == "retry_wait"
    assert int(limit) == 3

    # 无成员仍为 failed
    remaining_failed = await db_session.scalar(
        text("SELECT count(*) FROM run_members WHERE run_id=:r AND status='failed'"),
        {"r": run_id},
    )
    assert int(remaining_failed) == 0


async def test_repeated_retry_failed_no_extra_attempts(
    client: httpx.AsyncClient, db_session
) -> None:
    """同 ``Idempotency-Key`` 重复调用，``attempt_limit`` 只加一次。"""
    run_id = UUID(await _run_id(client, n=4))
    await db_session.execute(
        text(
            "UPDATE run_members SET status='failed', attempt_count=3, attempt_limit=3 "
            "WHERE run_id=:r"
        ),
        {"r": run_id},
    )
    await db_session.execute(
        text("UPDATE runs SET status='failed', finished_at=now() WHERE id=:r"), {"r": run_id}
    )
    await db_session.commit()

    key = uuid4().hex
    first = await client.post(
        f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": key}
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        f"/api/v1/runs/{run_id}/retry-failed", headers={"Idempotency-Key": key}
    )
    assert second.status_code == 200, second.text

    limits = (
        await db_session.execute(
            text("SELECT DISTINCT attempt_limit FROM run_members WHERE run_id=:r"),
            {"r": run_id},
        )
    ).scalars().all()
    assert [int(value) for value in limits] == [6]  # 只加了一次（6，非 9）


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


async def test_budget_increase_keeps_snapshots(
    client: httpx.AsyncClient, db_session
) -> None:
    """提预算后 ``survey_snapshot``/``model_snapshot``/``sample_size`` 逐字段不变；降预算 422。"""
    run_id = UUID(await _run_id(client, n=3, budget_limit="10", request_limit=9))
    await db_session.commit()

    before = (
        await db_session.execute(
            text(
                "SELECT survey_snapshot, model_snapshot, sample_size, budget_currency "
                "FROM runs WHERE id=:r"
            ),
            {"r": run_id},
        )
    ).one()

    patch = await client.patch(
        f"/api/v1/runs/{run_id}/budget",
        json={"budget_limit": "20", "request_limit": 12},
    )
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert Decimal(body["budget_limit"]) == Decimal("20")
    assert body["request_limit"] == 12

    await db_session.commit()
    after = (
        await db_session.execute(
            text(
                "SELECT survey_snapshot, model_snapshot, sample_size, budget_currency "
                "FROM runs WHERE id=:r"
            ),
            {"r": run_id},
        )
    ).one()

    assert after[0] == before[0]  # survey_snapshot 逐字段不变
    assert after[1] == before[1]  # model_snapshot 逐字段不变
    assert int(after[2]) == int(before[2])  # sample_size 不变
    assert after[3] == before[3] == "CNY"  # 币种不变

    events = (
        await db_session.execute(
            text("SELECT control_events_json FROM runs WHERE id=:r"), {"r": run_id}
        )
    ).scalar_one()
    budget_events = [e for e in events if e.get("action") == "increase-budget"]
    assert budget_events, events
    latest = budget_events[-1]
    assert Decimal(latest["old_budget_limit"]) == Decimal("10")
    assert Decimal(latest["new_budget_limit"]) == Decimal("20")

    # 降预算 → 422
    lower = await client.patch(
        f"/api/v1/runs/{run_id}/budget", json={"budget_limit": "5"}
    )
    assert lower.status_code == 422, lower.text
    assert lower.json()["code"] == "INVALID_RUN"

    # 改币种 → 422
    currency = await client.patch(
        f"/api/v1/runs/{run_id}/budget", json={"budget_limit": "30", "budget_currency": "USD"}
    )
    assert currency.status_code == 422, currency.text


# ---------------------------------------------------------------------------
# 第二活动批次 / 重复 pause·cancel / 并发 start
# ---------------------------------------------------------------------------


async def test_second_active_run_start_409(client: httpx.AsyncClient) -> None:
    """存在活动批次时启动第二个 run → 409。"""
    await _started_run(client, n=2)
    second = await _run_id(client, n=2)

    response = await client.post(f"/api/v1/runs/{second}/start")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "ACTIVE_RUN_EXISTS"


async def test_repeated_pause_cancel_idempotent(client: httpx.AsyncClient) -> None:
    """重复 pause/cancel → **200**（已达目标状态，非 409）。"""
    run_id = await _started_run(client, n=3)

    first_pause = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert first_pause.status_code == 202, first_pause.text
    second_pause = await client.post(f"/api/v1/runs/{run_id}/pause")
    assert second_pause.status_code == 200, second_pause.text
    assert second_pause.json()["pause_reason"] == "user"

    first_cancel = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert first_cancel.status_code == 202, first_cancel.text
    second_cancel = await client.post(f"/api/v1/runs/{run_id}/cancel")
    assert second_cancel.status_code == 200, second_cancel.text
    assert second_cancel.json()["status"] in ("cancelling", "cancelled")


async def test_concurrent_start_only_one_active_run(client: httpx.AsyncClient) -> None:
    """API 层复测：并发发起 N 个 start，**恰好 1 个 202、其余全 409**。"""
    run_ids = [await _run_id(client, n=2) for _ in range(4)]

    responses = await asyncio.gather(
        *(client.post(f"/api/v1/runs/{run_id}/start") for run_id in run_ids)
    )
    codes = [response.status_code for response in responses]
    assert codes.count(202) == 1, codes
    assert codes.count(409) == 3, codes

    again = await client.get("/api/v1/runs?page_size=100")
    active = [
        item for item in again.json()["items"]
        if item["status"] in ("running", "pausing", "paused", "cancelling")
    ]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# 模型配置门禁（裁决 Q6，只落在 API 层；TEAM-BRIEF §7.9.2）
# ---------------------------------------------------------------------------


async def test_start_requires_model_config_422(engine, db_session) -> None:
    """**Q6**：真实 provider 且 ``MODEL_ENDPOINT`` 为空 → ``POST /start`` 返回 **422**
    ``MODEL_CONFIG_INVALID``，且 run **仍为** ``ready``（门禁在任何状态转移之前，
    DB 原生 SQL 断言）；同一 run 在 ``MODEL_PROVIDER=mock`` 下 → **202**。
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)

    real_app = create_app(
        sessionmaker=maker,
        settings=make_settings(
            model_provider="openai_compatible", model_endpoint="", model_name="gpt-x"
        ),
    )
    async with real_app.router.lifespan_context(real_app):
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            run_id = await _run_id(http, n=2)

            rejected = await http.post(f"/api/v1/runs/{run_id}/start")
            assert rejected.status_code == 422, rejected.text
            error_body = rejected.json()
            assert set(error_body) >= {"code", "message", "details"}, error_body
            assert error_body["code"] == "MODEL_CONFIG_INVALID"

            # 门禁在状态转移之前 → run 未被启动（DB 原生 SQL 断言）。
            await db_session.commit()
            still = await db_session.scalar(
                text("SELECT status FROM runs WHERE id=:r"), {"r": UUID(run_id)}
            )
            assert still == "ready", still

    # 同一 run 在 mock 下可正常启动（mock 是本机唯一可用模式）。
    mock_app = create_app(
        sessionmaker=maker, settings=make_settings(model_provider="mock")
    )
    async with mock_app.router.lifespan_context(mock_app):
        transport = httpx.ASGITransport(app=mock_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            accepted = await http.post(f"/api/v1/runs/{run_id}/start")
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["status"] == "running"


# ---------------------------------------------------------------------------
# 非法/合法转移矩阵 I1–I12（architecture.md §3.3）
# ---------------------------------------------------------------------------


async def _transition_responses(
    client: httpx.AsyncClient, db_session, case_id: str
) -> list[httpx.Response]:
    if case_id == "I1":
        run_id = await _started_run(client, n=2)
        return [await client.post(f"/api/v1/runs/{run_id}/start")]

    if case_id == "I2":
        await _started_run(client, n=2)  # 活动批次
        other = await _run_id(client, n=2)
        return [await client.post(f"/api/v1/runs/{other}/start")]

    if case_id == "I3":
        run_id = await _run_id(client, n=2)  # ready
        return [
            await client.post(f"/api/v1/runs/{run_id}/pause"),
            await client.post(f"/api/v1/runs/{run_id}/resume"),
            await client.post(
                f"/api/v1/runs/{run_id}/retry-failed",
                headers={"Idempotency-Key": uuid4().hex},
            ),
        ]

    if case_id == "I4":
        run_id = await _run_with_status(client, db_session, "completed")
        return [
            await client.post(f"/api/v1/runs/{run_id}/pause"),
            await client.post(f"/api/v1/runs/{run_id}/resume"),
            await client.post(f"/api/v1/runs/{run_id}/cancel"),
        ]

    if case_id == "I5":
        run_id = await _run_with_status(client, db_session, "cancelled")
        return [
            await client.post(f"/api/v1/runs/{run_id}/start"),
            await client.post(f"/api/v1/runs/{run_id}/resume"),
            await client.post(f"/api/v1/runs/{run_id}/pause"),
        ]

    if case_id == "I6":
        run_id = await _run_with_status(client, db_session, "completed")
        return [
            await client.post(
                f"/api/v1/runs/{run_id}/retry-failed",
                headers={"Idempotency-Key": uuid4().hex},
            )
        ]

    if case_id == "I7":
        survey = await _create_survey(client)
        payload = survey_payload("改名")
        payload["expected_revision"] = 999
        return [await client.patch(f"/api/v1/surveys/{survey['id']}", json=payload)]

    if case_id == "I8":
        survey = await _create_survey(client)
        imported = await _create_import(client, 2)
        body = {
            "survey_id": survey["id"],
            "survey_revision": survey["revision"],
            "import_id": imported["import_id"],
            "model_config_id": "default",
        }
        key = uuid4().hex
        created = await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert created.status_code == 201, created.text
        return [
            await client.post(
                "/api/v1/runs",
                json={**body, "model_config_id": "another"},
                headers={"Idempotency-Key": key},
            )
        ]

    if case_id == "I9":
        # I9 是**合法**幂等重放（同 key 同 hash）→ 201（非 409 的对照用例）。
        survey = await _create_survey(client)
        imported = await _create_import(client, 2)
        body = {
            "survey_id": survey["id"],
            "survey_revision": survey["revision"],
            "import_id": imported["import_id"],
            "model_config_id": "default",
        }
        key = uuid4().hex
        await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        return [await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})]

    if case_id == "I10":
        run_id = await _run_with_status(client, db_session, "ready")
        return [
            await client.post(
                f"/api/v1/runs/{run_id}/retry-failed",
                headers={"Idempotency-Key": uuid4().hex},
            )
        ]

    if case_id == "I11":
        run_id = await _run_id(client, n=2, budget_limit="10")
        return [await client.patch(f"/api/v1/runs/{run_id}/budget", json={"budget_limit": "5"})]

    if case_id == "I12":
        run_id = await _run_with_status(client, db_session, "paused")
        return [await client.post(f"/api/v1/runs/{run_id}/start")]

    raise AssertionError(f"unknown case: {case_id}")


#: ``(case_id, expected_status)``（architecture.md §3.3 逐条）。
#: 多数非法转移 → 409；I9 为**合法**幂等重放（201）对照；I11（降预算/改币种）→ 422。
ILLEGAL_TRANSITIONS: tuple[tuple[str, int], ...] = (
    ("I1", 409),
    ("I2", 409),
    ("I3", 409),
    ("I4", 409),
    ("I5", 409),
    ("I6", 409),
    ("I7", 409),
    ("I8", 409),
    ("I9", 201),
    ("I10", 409),
    ("I11", 422),
    ("I12", 409),
)


@pytest.mark.parametrize(
    ("case_id", "expected"),
    ILLEGAL_TRANSITIONS,
    ids=[case_id for case_id, _ in ILLEGAL_TRANSITIONS],
)
async def test_transition_matrix(
    client: httpx.AsyncClient, db_session, case_id: str, expected: int
) -> None:
    """转移矩阵 I1–I12（architecture.md §3.3）。**名实相符**（P2-2 / TEAM-BRIEF §7.9.3）：
    12 条里多数非法转移 → 409，但 ``I9`` 为**合法**幂等重放（期望 **201**）、``I11`` 为
    预算守卫（期望 **422**）—— 故不再取名 ``_409``（那会让 ``-k 409`` / CI 列表误导）。
    """
    responses = await _transition_responses(client, db_session, case_id)
    assert responses, case_id
    for response in responses:
        assert response.status_code == expected, (
            f"{case_id}: expected {expected}, got {response.status_code}: {response.text}"
        )
        if expected == 409:
            body = response.json()
            assert set(body) >= {"code", "message", "details"}, body
