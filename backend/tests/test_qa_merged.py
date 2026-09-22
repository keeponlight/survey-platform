# SPDX-License-Identifier: GPL-3.0-or-later
"""合并对抗性复核（批次 A + B + C）—— QA 严过关 独立交叉验证。

**不复用工程师的断言方式**：Q6 四态用**自建 settings 注入 + DB 原生 SQL** 复核；mock
跨进程确定性用**自有 persona 集 + 不同 PYTHONHASHSEED 的独立子进程 + 测试内独立 SHA-256
重算**复核；mock 可溯源直接查 ``runs.model_snapshot``；无 5xx 用**自有探针**。

对应任务书 §5.2 的 1 / 2 / 3 / 7 项。运行：``uv run pytest tests/test_qa_merged.py -q``。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.main import create_app
from tests.fixtures.generate_personas import DEFAULT_COLUMN_MAPPING

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = Settings.from_env().test_database_url

#: 五档（与主文档 §5.1 一致；测试内独立列出，不 import 被测常量）。
OPTION_VALUES = (
    "definitely_not",
    "probably_not",
    "unsure",
    "probably_yes",
    "definitely_yes",
)

PRODUCT = {
    "name": "示例产品",
    "description": "用于演示的产品",
    "price": "199.00",
    "price_unit": "元/件",
    "time_range": "未来 30 天",
    "purchase_conditions": None,
}

COLUMN_MAPPING = DEFAULT_COLUMN_MAPPING


def base_settings(**overrides) -> Settings:
    return replace(
        Settings.from_env(),
        database_url=TEST_DATABASE_URL,
        test_database_url=TEST_DATABASE_URL,
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("1"),
        model_rpm=1_000_000,
        model_tpm=1_000_000_000,
        **overrides,
    )


def csv_bytes(n: int) -> bytes:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([f"p_{index:06d}", 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


@asynccontextmanager
async def http_for(engine, settings, *, raise_app_exceptions: bool = True):
    """自建 app + 测试库会话工厂；yield ``(http, maker)``。"""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(sessionmaker=maker, settings=settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(
            app=app, raise_app_exceptions=raise_app_exceptions
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qa") as http:
            yield http, maker


async def create_ready_run(http: httpx.AsyncClient, *, n: int = 2) -> str:
    """建 survey + import + ready run；返回 run_id。"""
    survey = (
        await http.post(
            "/api/v1/surveys",
            json={"title": "q", "product": PRODUCT, "question": {}},
        )
    ).json()
    imported = (
        await http.post(
            "/api/v1/imports",
            files={"file": ("p.csv", csv_bytes(n), "text/csv")},
            data={"column_mapping": json.dumps(COLUMN_MAPPING)},
        )
    ).json()
    body = {
        "survey_id": survey["id"],
        "survey_revision": survey["revision"],
        "import_id": imported["import_id"],
        "model_config_id": "default",
    }
    response = await http.post(
        "/api/v1/runs", json=body, headers={"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _scalar(session, sql: str, **params):
    return await session.scalar(text(sql), params)


# ===========================================================================
# §5.2.1 Q6 门禁四态（自建 settings 注入 + DB 原生 SQL 断言 run 真实状态）
# ===========================================================================
#: 门禁用例使用的「存放 key 的环境变量名」（隔离，避免污染真实环境）。
_QA_KEY_ENV = "QA_MERGED_MODEL_API_KEY"


async def test_qa_merged_q6_real_provider_with_key_202(engine, db_session, monkeypatch) -> None:
    """(a) 真实 provider + endpoint 非空 + key 有值 → **202**（门禁不得误伤正常配置）。"""
    monkeypatch.setenv(_QA_KEY_ENV, "qa-secret-value")
    settings = base_settings(
        model_provider="openai_compatible",
        model_endpoint="https://example.test/v1",
        model_name="gpt-x",
        model_api_key_env=_QA_KEY_ENV,
    )
    async with http_for(engine, settings) as (http, _maker):
        run_id = await create_ready_run(http)
        resp = await http.post(f"/api/v1/runs/{run_id}/start")
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "running"

    await db_session.commit()
    still = await _scalar(
        db_session, "SELECT status FROM runs WHERE id=:r", r=UUID(run_id)
    )
    assert still == "running", still


async def test_qa_merged_q6_real_provider_without_key_422(
    engine, db_session, monkeypatch
) -> None:
    """(b) 真实 provider + endpoint 非空 + key **缺失** → **422**；run 仍 ``ready``。"""
    monkeypatch.delenv(_QA_KEY_ENV, raising=False)
    settings = base_settings(
        model_provider="openai_compatible",
        model_endpoint="https://example.test/v1",
        model_name="gpt-x",
        model_api_key_env=_QA_KEY_ENV,
    )
    async with http_for(engine, settings) as (http, _maker):
        run_id = await create_ready_run(http)
        resp = await http.post(f"/api/v1/runs/{run_id}/start")
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "MODEL_CONFIG_INVALID"
        assert resp.json()["details"]["api_key_present"] is False

        await db_session.commit()
        assert (
            await _scalar(db_session, "SELECT status FROM runs WHERE id=:r", r=UUID(run_id))
        ) == "ready"
        # 未产生任何成员状态变化（全部仍 pending）。
        moved = await _scalar(
            db_session,
            "SELECT count(*) FROM run_members WHERE run_id=:r AND status <> 'pending'",
            r=UUID(run_id),
        )
        assert int(moved) == 0


async def test_qa_merged_q6_real_provider_empty_endpoint_422(
    engine, db_session, monkeypatch
) -> None:
    """(c) 真实 provider + endpoint **为空** → **422**；run 仍 ``ready``、成员无变化。"""
    monkeypatch.setenv(_QA_KEY_ENV, "qa-secret-value")
    settings = base_settings(
        model_provider="openai_compatible",
        model_endpoint="",
        model_name="gpt-x",
        model_api_key_env=_QA_KEY_ENV,
    )
    async with http_for(engine, settings) as (http, _maker):
        run_id = await create_ready_run(http)
        resp = await http.post(f"/api/v1/runs/{run_id}/start")
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "MODEL_CONFIG_INVALID"
        assert resp.json()["details"]["model_configured"] is False

        await db_session.commit()
        assert (
            await _scalar(db_session, "SELECT status FROM runs WHERE id=:r", r=UUID(run_id))
        ) == "ready"
        moved = await _scalar(
            db_session,
            "SELECT count(*) FROM run_members WHERE run_id=:r AND status <> 'pending'",
            r=UUID(run_id),
        )
        assert int(moved) == 0


async def test_qa_merged_q6_mock_skips_gate_202(engine, db_session) -> None:
    """(d) ``MODEL_PROVIDER=mock`` → **202**（空 endpoint/无 key 也放行，mock 为唯一依据）。"""
    settings = base_settings(
        model_provider="mock",
        model_endpoint="",
        model_name="mock-model",
    )
    async with http_for(engine, settings) as (http, _maker):
        run_id = await create_ready_run(http)
        resp = await http.post(f"/api/v1/runs/{run_id}/start")
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "running"

    await db_session.commit()
    assert (
        await _scalar(db_session, "SELECT status FROM runs WHERE id=:r", r=UUID(run_id))
    ) == "running"


# ===========================================================================
# §5.2.2 mock 跨进程确定性 + 未使用内置 hash()
# ===========================================================================

#: 复核自有 persona 集（与工程师用例不同）。
_QA_PERSONAS = ("qa_p1", "qa_p2", "qa_p3", "qa_p4", "qa_p5", "qa_p6", "qa_p7")


def _independent_value(persona_id: str) -> str:
    """测试内**独立**用 SHA-256 重算档位（不 import 被测实现）。"""
    digest = hashlib.sha256(persona_id.encode("utf-8")).hexdigest()
    return OPTION_VALUES[int(digest[:16], 16) % len(OPTION_VALUES)]


def test_qa_merged_mock_cross_process_seed_independent() -> None:
    """不同 ``PYTHONHASHSEED`` 的独立子进程 → 档位逐字一致，且等于测试内独立 SHA-256 重算。"""
    script = "\n".join(
        [
            "import json, sys",
            "from app.inference.mock_provider import valid_value_for",
            "ids = json.loads(sys.argv[1])",
            "print(json.dumps([valid_value_for(i) for i in ids]))",
        ]
    )
    expected = [_independent_value(pid) for pid in _QA_PERSONAS]
    outputs: dict[str, list[str]] = {}
    for seed in ("0", "1", "2", "999"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script, json.dumps(list(_QA_PERSONAS))],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        outputs[seed] = json.loads(completed.stdout.strip())
        assert outputs[seed] == expected, (seed, outputs[seed], expected)
    assert len({tuple(v) for v in outputs.values()}) == 1  # 跨进程完全一致


def test_qa_merged_mock_does_not_use_builtin_hash() -> None:
    """实现未用内置 ``hash()``：源码引用 ``hashlib.sha256`` 且 **AST 中无 ``hash(...)`` 调用**。"""
    source = (BACKEND_DIR / "app" / "inference" / "mock_provider.py").read_text("utf-8")
    assert "hashlib.sha256" in source
    tree = ast.parse(source)
    builtin_hash_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hash"
    ]
    assert builtin_hash_calls == [], (
        f"builtin hash( call at line(s) {[n.lineno for n in builtin_hash_calls]}"
    )


# ===========================================================================
# §5.2.3 mock 可溯源（红线 #2）：model_snapshot["provider"] == "mock"
# ===========================================================================


async def test_qa_merged_run_snapshot_provider_is_mock(engine, db_session) -> None:
    """mock 可溯源（红线 #2）：``runs.model_snapshot["provider"] == "mock"``。

    生产环境下 ``MODEL_PROVIDER`` 来自 env（compose 的 mock.env），即进程级单例；
    故此处以 ``set_settings`` 模拟该环境（``POST /runs`` 的快照取自单例，见下一条观察）。
    """
    from app import config as app_config

    previous = app_config.get_settings()
    mock_settings = base_settings(model_provider="mock", model_name="mock-model")
    app_config.set_settings(mock_settings)
    try:
        async with http_for(engine, mock_settings) as (http, _maker):
            run_id = await create_ready_run(http, n=2)
    finally:
        app_config.set_settings(previous)

    await db_session.commit()
    snapshot = await _scalar(
        db_session, "SELECT model_snapshot FROM runs WHERE id=:r", r=UUID(run_id)
    )
    assert isinstance(snapshot, dict), snapshot
    assert snapshot.get("provider") == "mock", snapshot
    # 快照**不得**含任何密钥本体（红线 #3）：只允许 api_key_env（变量名）。
    dumped = json.dumps(snapshot)
    assert "qa-secret" not in dumped
    assert "sk-" not in dumped


async def test_qa_merged_run_snapshot_follows_injected_settings(
    engine, db_session
) -> None:
    """P2-⑤ **已修复**：``POST /runs`` 的 ``model_snapshot`` 取自 ``create_app(settings=...)``
    注入的 settings，而非**进程级单例**。

    修复前（QA 记录的缺陷）：路由未把 ``settings`` 传给 ``create_run``，快照错记为单例
    的 provider，与 ``start`` 门禁所用 provider 分歧 → 红线 #2「mock 可溯源」出现不可核查
    缝隙。修复后：快照如实反映**注入**的 provider（见 ``routes.py`` 的 ``settings=settings``）。

    说明：本用例原为「观察（证据级）」的缺陷记录；缺陷既已修复，期望值必须翻转为**修复后**
    的契约，否则用例恒红。**未削弱断言**——仍以 DB 原生 SQL 读 ``runs.model_snapshot`` 并对
    「注入值」做**正向**断言（而非仅断言"不等"）。
    """
    from app import config as app_config

    previous = app_config.get_settings()
    # 全局单例显式设为 openai_compatible；注入 app 的 settings 设为 mock，形成区分。
    app_config.set_settings(base_settings(model_provider="openai_compatible", model_name="gpt-x"))
    injected = base_settings(model_provider="mock", model_name="mock-model")
    try:
        async with http_for(engine, injected) as (http, _maker):
            run_id = await create_ready_run(http, n=2)
    finally:
        app_config.set_settings(previous)

    await db_session.commit()
    snapshot = await _scalar(
        db_session, "SELECT model_snapshot FROM runs WHERE id=:r", r=UUID(run_id)
    )
    assert snapshot.get("provider") == injected.model_provider  # 跟随**注入的** settings
    assert snapshot.get("provider") != "openai_compatible"  # 不再跟随进程单例


# ===========================================================================
# §5.2.7 无 5xx 回归（自有探针；含「另一批次活动时 cancel 一个 ready run」）
# ===========================================================================


async def test_qa_merged_no_5xx_on_control_endpoints(engine) -> None:
    settings = base_settings(model_provider="mock")
    async with http_for(engine, settings, raise_app_exceptions=False) as (http, _maker):
        run_id = await create_ready_run(http, n=2)
        long_key = "k" * 8000
        probes: list[tuple[str, httpx.Response]] = []
        for action in ("start", "pause", "resume", "cancel", "retry-failed"):
            probes.append(
                (f"baduuid:{action}", await http.post(f"/api/v1/runs/not-a-uuid/{action}"))
            )
            probes.append(
                (
                    f"junk:{action}",
                    await http.post(
                        f"/api/v1/runs/{run_id}/{action}",
                        content=b"junk",
                        headers={"Content-Type": "application/json"},
                    ),
                )
            )
        probes.append(
            ("retry:no-key", await http.post(f"/api/v1/runs/{run_id}/retry-failed"))
        )
        probes.append(
            (
                "retry:long-key",
                await http.post(
                    f"/api/v1/runs/{run_id}/retry-failed",
                    headers={"Idempotency-Key": long_key},
                ),
            )
        )
        probes.append(
            (
                "budget:bad-json",
                await http.patch(
                    f"/api/v1/runs/{run_id}/budget",
                    content=b"{not json",
                    headers={"Content-Type": "application/json"},
                ),
            )
        )
        probes.append(
            (
                "budget:unknown-field",
                await http.patch(
                    f"/api/v1/runs/{run_id}/budget",
                    json={"budget_limit": "10", "zzz": 1},
                ),
            )
        )

        # 曾经的 500 路径：A running 时 cancel ready 的 B。
        a = await create_ready_run(http, n=2)
        assert (await http.post(f"/api/v1/runs/{a}/start")).status_code == 202
        b = await create_ready_run(http, n=2)
        probes.append(
            ("cancel-ready-while-active", await http.post(f"/api/v1/runs/{b}/cancel"))
        )

        offenders = [
            (name, r.status_code, r.text[:160])
            for name, r in probes
            if r.status_code >= 500
        ]
        assert offenders == [], f"5xx leaked: {offenders}"
