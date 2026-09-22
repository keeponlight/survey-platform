# SPDX-License-Identifier: GPL-3.0-or-later
"""T4 阶段 A —— HTTP 契约测试（主文档 §8.2 / task-list.md T4）。

范围（阶段 A，零文件重叠）：应用装配、``/api/v1`` 前缀、统一错误体、只读端点、
``POST /surveys``/``PATCH``、``POST /imports``、``POST /runs/preview``、``POST /runs``、
``GET /runs`` / ``{id}`` / ``results`` / ``summary`` / ``export.csv``、健康检查。

验收命令：``uv run pytest tests/test_api.py -q``

> 控制动作（``start`` / ``pause`` / ``resume`` / ``cancel`` / ``retry-failed`` / ``budget``）
> 与其非法转移用例属**阶段 B**（需扩展 ``runs/service.py``），待冻结解除后追加。
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import sys
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
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

PRODUCT = {
    "name": "示例产品",
    "description": "用于演示的产品",
    "price": "199.00",
    "price_unit": "元/件",
    "time_range": "未来 30 天",
    "purchase_conditions": None,
}


def survey_payload(title: str = "购买意向问卷") -> dict:
    return {"title": title, "product": PRODUCT, "question": {}}


def csv_bytes(n: int) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


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
        **overrides,
    )


@pytest_asyncio.fixture
async def client(engine, db_session):
    """装配测试 app（注入测试库会话工厂）并运行 lifespan。"""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(sessionmaker=maker, settings=make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


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


async def _create_run(client: httpx.AsyncClient, *, key: str | None = None, n: int = 6) -> dict:
    survey = await _create_survey(client)
    imported = await _create_import(client, n)
    body = {
        "survey_id": survey["id"],
        "survey_revision": survey["revision"],
        "import_id": imported["import_id"],
        "model_config_id": "default",
    }
    headers = {"Idempotency-Key": key} if key else {}
    response = await client.post("/api/v1/runs", json=body, headers=headers)
    return response, body


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


async def test_health_live_and_ready(client: httpx.AsyncClient) -> None:
    live = await client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    ready = await client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ok"}


# ---------------------------------------------------------------------------
# 问卷
# ---------------------------------------------------------------------------


async def test_create_and_get_survey(client: httpx.AsyncClient) -> None:
    created = await _create_survey(client)
    assert created["revision"] == 1
    assert created["product"]["price"] == "199.00"

    fetched = await client.get(f"/api/v1/surveys/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]

    listing = await client.get("/api/v1/surveys")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


async def test_patch_survey_revision_conflict_409(client: httpx.AsyncClient) -> None:
    created = await _create_survey(client)
    payload = survey_payload("改名")
    payload["expected_revision"] = 1
    ok = await client.patch(f"/api/v1/surveys/{created['id']}", json=payload)
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2
    assert ok.json()["title"] == "改名"

    stale = survey_payload("再改")
    stale["expected_revision"] = 1
    conflict = await client.patch(f"/api/v1/surveys/{created['id']}", json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REVISION_CONFLICT"


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


async def test_import_upload_and_get(client: httpx.AsyncClient) -> None:
    imported = await _create_import(client, 5)
    assert imported["row_count"] == 5
    assert len(imported["preview"]) == 3
    assert imported["preview"][0]["row_no"] == 1

    fetched = await client.get(f"/api/v1/imports/{imported['import_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["row_count"] == 5


async def test_import_invalid_mapping_422(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/imports",
        files={"file": ("p.csv", csv_bytes(3), "text/csv")},
        data={"column_mapping": "{not json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_USER_TABLE"


async def test_import_not_found_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/api/v1/imports/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 运行预览 / 创建
# ---------------------------------------------------------------------------


async def test_preview_run_returns_estimate_without_model(client: httpx.AsyncClient) -> None:
    survey = await _create_survey(client)
    imported = await _create_import(client, 4)
    response = await client.post(
        "/api/v1/runs/preview",
        json={
            "survey_id": survey["id"],
            "survey_revision": survey["revision"],
            "import_id": imported["import_id"],
            "model_config_id": "default",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sample_size"] == 4
    assert body["target_concurrency"] == 100
    assert body["cost_is_unknown"] is False
    assert body["estimated_cost"] is not None
    assert len(body["prompt_previews"]) == 3


async def test_preview_run_missing_import_404(client: httpx.AsyncClient) -> None:
    survey = await _create_survey(client)
    response = await client.post(
        "/api/v1/runs/preview",
        json={
            "survey_id": survey["id"],
            "survey_revision": 1,
            "import_id": str(uuid4()),
            "model_config_id": "default",
        },
    )
    assert response.status_code == 404


async def test_create_run_requires_idempotency_key_422(client: httpx.AsyncClient) -> None:
    response, _ = await _create_run(client, key=None)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_RUN"


async def test_create_run_survey_revision_mismatch_409(client: httpx.AsyncClient) -> None:
    survey = await _create_survey(client)
    imported = await _create_import(client, 3)
    response = await client.post(
        "/api/v1/runs",
        json={
            "survey_id": survey["id"],
            "survey_revision": 99,  # 与当前 revision=1 不符
            "import_id": imported["import_id"],
            "model_config_id": "default",
        },
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "REVISION_CONFLICT"


def test_domain_error_status_mapping() -> None:
    """T4 三项核心映射必须就位（不得泄漏 500）。"""
    from app.api.errors import STATUS_BY_EXCEPTION
    from app.runs.service import (
        ActiveRunConflictError,
        IdempotencyConflictError,
        RunValidationError,
    )

    assert STATUS_BY_EXCEPTION[ActiveRunConflictError] == 409
    assert STATUS_BY_EXCEPTION[IdempotencyConflictError] == 409
    assert STATUS_BY_EXCEPTION[RunValidationError] == 422


async def test_create_run_and_get_view(client: httpx.AsyncClient) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=6)
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["status"] == "ready"
    assert view["sample_size"] == 6
    assert view["target_concurrency"] == 100
    assert view["in_flight"] == 0
    assert view["throttled"] == 0
    assert view["counts"]["pending"] == 6
    assert view["counts"]["valid_count"] == 0
    assert view["allowed_actions"] == ["start", "cancel", "increase-budget"]

    fetched = await client.get(f"/api/v1/runs/{view['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == view["id"]


async def test_create_run_idempotent_same_key_returns_same_run(client: httpx.AsyncClient) -> None:
    key = uuid4().hex
    survey = await _create_survey(client)
    imported = await _create_import(client, 5)
    body = {
        "survey_id": survey["id"],
        "survey_revision": 1,
        "import_id": imported["import_id"],
        "model_config_id": "default",
    }

    first = await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text
    run_id = first.json()["id"]

    # 相同 key + 相同 request_hash → 返回原 run（幂等成功，只产生一个批次）。
    replay = await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
    assert replay.status_code == 201
    assert replay.json()["id"] == run_id

    listing = await client.get("/api/v1/runs")
    assert listing.json()["total"] == 1

    # 相同 key + 不同 request_hash → 409。
    conflict = await client.post(
        "/api/v1/runs",
        json={**body, "model_config_id": "another"},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# 错误体 / 分页 / allowed_actions
# ---------------------------------------------------------------------------


async def test_error_body_shape(client: httpx.AsyncClient) -> None:
    not_found = await client.get(f"/api/v1/runs/{uuid4()}")
    assert not_found.status_code == 404
    body = not_found.json()
    assert set(body) >= {"code", "message", "details"}
    assert isinstance(body["code"], str) and body["code"]

    invalid = await client.post("/api/v1/surveys", json={"title": "x", "product": {"name": "n"}})
    assert invalid.status_code == 422
    body = invalid.json()
    assert set(body) >= {"code", "message", "details"}
    assert body["code"] == "VALIDATION_ERROR"


async def test_not_found_404_semantics(client: httpx.AsyncClient) -> None:
    for path in (
        f"/api/v1/surveys/{uuid4()}",
        f"/api/v1/imports/{uuid4()}",
        f"/api/v1/runs/{uuid4()}",
        f"/api/v1/runs/{uuid4()}/summary",
        f"/api/v1/runs/{uuid4()}/results",
        f"/api/v1/runs/{uuid4()}/export.csv",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] == "NOT_FOUND"


async def test_pagination_bounds(client: httpx.AsyncClient) -> None:
    # surveys：page_size 上限 100、下限 1，page 下限 1。
    await _create_survey(client)
    high = await client.get("/api/v1/surveys?page=1&page_size=1000")
    assert high.json()["page_size"] == 100
    low = await client.get("/api/v1/surveys?page=0&page_size=0")
    assert low.json()["page"] == 1
    assert low.json()["page_size"] == 1

    # results：page_size 上限 200。
    response, _ = await _create_run(client, key=uuid4().hex, n=6)
    run_id = response.json()["id"]
    over = await client.get(f"/api/v1/runs/{run_id}/results?page=1&page_size=1000")
    assert over.status_code == 200
    assert over.json()["page_size"] == 200
    assert over.json()["total"] == 6
    assert [item["row_no"] for item in over.json()["items"]] == [1, 2, 3, 4, 5, 6]


async def test_results_status_filter_and_invalid(client: httpx.AsyncClient) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=4)
    run_id = response.json()["id"]

    pending = await client.get(f"/api/v1/runs/{run_id}/results?status=pending")
    assert pending.json()["total"] == 4

    bad = await client.get(f"/api/v1/runs/{run_id}/results?status=bogus")
    assert bad.status_code == 422
    assert bad.json()["code"] == "INVALID_RUN"


async def test_allowed_actions_present(client: httpx.AsyncClient, db_session) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=3)
    run_id = response.json()["id"]

    expected = {
        "ready": ["start", "cancel", "increase-budget"],
        "running": ["pause", "cancel", "increase-budget"],
        "paused": ["resume", "cancel", "increase-budget"],
        "completed": [],
        "completed_with_errors": ["retry-failed"],
        "failed": ["retry-failed"],
        "cancelled": [],
    }
    for status, actions in expected.items():
        await db_session.execute(
            text("UPDATE runs SET status = :s WHERE id = :r"), {"s": status, "r": UUID(run_id)}
        )
        await db_session.commit()
        view = await client.get(f"/api/v1/runs/{run_id}")
        assert view.json()["allowed_actions"] == actions, status


async def test_in_flight_and_throttled_are_real_values(
    client: httpx.AsyncClient, db_session
) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=4)
    run_id = UUID(response.json()["id"])

    await db_session.execute(
        text(
            "UPDATE run_members SET status='running' "
            "WHERE run_id = :r AND row_no = 1"
        ),
        {"r": run_id},
    )
    await db_session.execute(
        text(
            "UPDATE run_members SET status='retry_wait', next_attempt_at = now() "
            "WHERE run_id = :r AND row_no = 2"
        ),
        {"r": run_id},
    )
    await db_session.commit()

    view = await client.get(f"/api/v1/runs/{run_id}")
    body = view.json()
    assert body["in_flight"] == 1
    assert body["throttled"] == 1


# ---------------------------------------------------------------------------
# 汇总 / 导出
# ---------------------------------------------------------------------------


async def test_summary_endpoint_shape(client: httpx.AsyncClient) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=6)
    run_id = response.json()["id"]

    summary = await client.get(f"/api/v1/runs/{run_id}/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["sample_size"] == 6
    assert body["valid_count"] == 0
    assert body["top2box"] is None
    assert body["mean_score"] is None
    assert len(body["buckets"]) == 5
    assert body["age_groups"] is None or isinstance(body["age_groups"], list)
    assert "report_revision" in body and "as_of" in body


async def test_export_csv_endpoint(client: httpx.AsyncClient) -> None:
    response, _ = await _create_run(client, key=uuid4().hex, n=5)
    run_id = response.json()["id"]

    exported = await client.get(f"/api/v1/runs/{run_id}/export.csv")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    data = exported.content
    assert data.startswith("\ufeff".encode())
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert len(rows) == 6  # 表头 + 5 成员
    assert len(rows[0]) == 15


# ---------------------------------------------------------------------------
# API 进程重启不影响 worker（进度全在 DB，不在进程内存）
# ---------------------------------------------------------------------------


async def test_api_restart_does_not_affect_worker(engine, db_session) -> None:
    """重建 app 实例（模拟 API 重启）后 worker 仍能推进 run；进度来自 DB 而非进程内存。"""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    # --- 第一个 API 实例：创建并启动批次 ---
    app1 = create_app(sessionmaker=maker, settings=make_settings())
    async with app1.router.lifespan_context(app1):
        transport = httpx.ASGITransport(app=app1)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http1:
            response, _ = await _create_run(http1, key=uuid4().hex, n=8)
            assert response.status_code == 201, response.text
            run_id = response.json()["id"]
            started = await http1.post(f"/api/v1/runs/{run_id}/start")
            assert started.status_code == 202, started.text

    # --- 模拟 API 进程重启：全新 app 实例（无共享内存状态）---
    app2 = create_app(sessionmaker=maker, settings=make_settings())
    async with app2.router.lifespan_context(app2):
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http2:
            # 重启后仍能查到原批次（进度来自 DB）。
            after_restart = await http2.get(f"/api/v1/runs/{run_id}")
            assert after_restart.status_code == 200
            assert after_restart.json()["status"] == "running"

            # worker 独立推进（worker 状态不在 API 进程内存）。
            worker = Worker(
                engine=engine,
                sessionmaker=maker,
                provider=MockProvider(fixed_latency_s=0.0),
                settings=make_settings(),
                concurrency=4,
                retry_policy=RetryPolicy(
                    max_attempts=3, backoff_seconds=(0.01, 0.02), jitter_ratio=0.0
                ),
                scan_interval=30.0,
                renew_interval=20.0,
                poll_interval=0.01,
            )
            try:
                assert await worker.start() is True
                status = await worker.serve(UUID(run_id), timeout=20.0)
                assert status == "completed"
            finally:
                await worker.stop()

            # 重启后的 API 实例读到的进度与 worker 写的一致。
            final = await http2.get(f"/api/v1/runs/{run_id}")
            assert final.status_code == 200
            body = final.json()
            assert body["status"] == "completed"
            assert body["counts"]["succeeded"] == 8
            assert body["in_flight"] == 0
