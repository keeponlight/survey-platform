# SPDX-License-Identifier: GPL-3.0-or-later
"""路由绑定（主文档 §8.2；统一前缀 ``/api/v1`` 由 :mod:`app.main` 挂载）。

**业务逻辑保留在 service 层**；本模块负责：参数解析、依赖注入、错误抛出（领域异常由
:mod:`app.api.errors` 统一映射为 ``{"code","message","details"}``）与响应序列化。

> **P2-b 口径（`row_no`）**：``row_no`` 是**数据行序号**（从 1 起、**不含表头**），
> 比 Excel 工作表物理行号小 1（即物理行号 = ``row_no + 1``）。本模块所有 ``row_no`` 出参
> 均为此语义，**不改后端字段含义**；导出 CSV 同此口径。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.actions import allowed_actions_for
from app.api.deps import get_app_settings, get_session
from app.api.errors import ModelConfigInvalidError, NotFoundError, error_content
from app.api.schemas import (
    PAUSE_HINTS,
    HealthOut,
    ImportPreviewOut,
    MemberRowOut,
    ResultsPageOut,
    RunBudgetPatchRequest,
    RunCountsOut,
    RunListOut,
    RunPreviewOut,
    RunPreviewRequest,
    RunViewOut,
    SurveyListOut,
    SurveyOut,
    SurveyPatchRequest,
)
from app.config import Settings
from app.contracts import OPTION_SCORES, PersonaSnapshot, RunCreate, SurveyInput
from app.models import ImportRecord, RunMember, RunRecord
from app.personas.source import build_preview, create_import
from app.reports.export import CSV_MEDIA_TYPE, load_run_meta, stream_csv
from app.reports.service import RunSummary, report_service
from app.runs.repository import run_repository
from app.runs.service import RunValidationError, run_service
from app.surveys.service import (
    DEFAULT_PAGE_SIZE as SURVEY_DEFAULT_PAGE_SIZE,
)
from app.surveys.service import (
    MAX_PAGE_SIZE as SURVEY_MAX_PAGE_SIZE,
)
from app.surveys.service import (
    RevisionConflictError,
    survey_service,
)
from app.worker.limits import (
    build_request_for_persona,
    estimate_input_tokens,
    reservation_for_request,
)

api_router = APIRouter()

#: results 分页（主文档 §8.2：默认 50、最大 200）。
RESULTS_DEFAULT_PAGE_SIZE = 50
RESULTS_MAX_PAGE_SIZE = 200

#: runs 列表分页（默认 20、最大 100）。
RUNS_DEFAULT_PAGE_SIZE = 20
RUNS_MAX_PAGE_SIZE = 100

#: prompt 材料预览条数（主文档 §8.1：3 条画像提示词预览）。
PROMPT_PREVIEW_COUNT = 3

#: 成本估算采样条数上限（避免 20,000 行逐条构造请求）。
PREVIEW_SAMPLE_LIMIT = 50

#: 时长估算假定的平均单请求秒数（文档 §7 算例用 L=3s；**仅估算**）。
ASSUMED_LATENCY_SECONDS = 3.0

#: 合法成员状态（results 过滤）。
_MEMBER_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "succeeded", "retry_wait", "failed", "cancelled"}
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _clamp(value: int, *, minimum: int, maximum: int, default: int) -> int:
    if value is None:
        return default
    return max(minimum, min(value, maximum))


def _decimal_str(value: Decimal | None) -> str | None:
    """``Decimal`` → 字符串；``None`` 保持 ``None``（未知 ≠ 0）。"""
    return None if value is None else str(value)


def _survey_out(record: Any) -> SurveyOut:
    return SurveyOut(
        id=record.id,
        title=record.title,
        product=record.product_json,
        question=record.question_json,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _get_import_record(session: AsyncSession, import_id: UUID) -> ImportRecord:
    result = await session.execute(select(ImportRecord).where(ImportRecord.id == import_id))
    record = result.scalar_one_or_none()
    if record is None:
        raise NotFoundError(f"import not found: {import_id}")
    return record


def _import_preview_out(
    record: ImportRecord, snapshots: list[PersonaSnapshot]
) -> ImportPreviewOut:
    first = snapshots[0] if snapshots else None
    source_version = (first.source_version if first and first.source_version else None) or str(
        record.id
    )
    return ImportPreviewOut(
        import_id=record.id,
        row_count=record.row_count,
        sheet_name=record.sheet_name,
        source_version=str(source_version),
        preview=snapshots[:3],
        errors=[],
    )


async def _error_summary(session: AsyncSession, run_id: UUID, failed: int) -> str | None:
    if failed <= 0:
        return None
    result = await session.execute(
        text(
            "SELECT last_error_code, count(*) FROM run_members "
            "WHERE run_id = :run_id AND status = 'failed' "
            "GROUP BY last_error_code ORDER BY count(*) DESC"
        ),
        {"run_id": run_id},
    )
    parts = [f"{code or 'UNKNOWN'}×{int(count)}" for code, count in result.all()]
    return f"失败 {failed}：" + ", ".join(parts)


async def _run_view(session: AsyncSession, run: RunRecord, settings: Settings) -> RunViewOut:
    counts = await run_repository.count_members_by_status(session, run.id)
    failed = counts.get("failed")
    succeeded = counts.get("succeeded")
    return RunViewOut(
        id=run.id,
        survey_id=run.survey_id,
        import_id=run.import_id,
        status=run.status,
        pause_reason=run.pause_reason,
        pause_hint=PAUSE_HINTS.get(run.pause_reason) if run.pause_reason else None,
        error_summary=await _error_summary(session, run.id, failed),
        prompt_version=run.prompt_version,
        target_concurrency=settings.agent_concurrency,
        in_flight=counts.get("running"),
        throttled=counts.get("retry_wait"),
        sample_size=run.sample_size,
        counts=RunCountsOut(
            succeeded=succeeded,
            failed=failed,
            pending=counts.get("pending"),
            running=counts.get("running"),
            retry_wait=counts.get("retry_wait"),
            cancelled=counts.get("cancelled"),
            valid_count=succeeded,
        ),
        actual_cost=_decimal_str(run.actual_cost),
        unknown_cost_count=run.unknown_cost_count,
        budget_limit=_decimal_str(run.budget_limit),
        budget_currency=run.budget_currency,
        request_limit=run.request_limit,
        requests_reserved=run.requests_reserved,
        allowed_actions=allowed_actions_for(run.status),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _member_out(member: RunMember) -> MemberRowOut:
    answer = member.answer_json if isinstance(member.answer_json, dict) else {}
    value: str | None = None
    score: int | None = None
    reason: str | None = None
    if member.status == "succeeded":
        raw_value = answer.get("value")
        if isinstance(raw_value, str):
            value = raw_value
            score = OPTION_SCORES.get(raw_value)
        raw_reason = answer.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason
    snapshot = member.persona_snapshot if isinstance(member.persona_snapshot, dict) else {}
    age = snapshot.get("age") if isinstance(snapshot.get("age"), int) else None
    city_tier = snapshot.get("city_tier") if isinstance(snapshot.get("city_tier"), str) else None
    return MemberRowOut(
        id=member.id,
        row_no=member.row_no,
        persona_id=member.persona_id,
        age=age,
        city_tier=city_tier,
        member_status=member.status,
        value=value,
        score=score,
        reason=reason,
        attempt_count=member.attempt_count,
        error_code=member.last_error_code,
    )


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


@api_router.get("/health/live", response_model=HealthOut)
async def health_live() -> HealthOut:
    """活性检查（仅表示进程存活）。"""
    return HealthOut(status="ok")


@api_router.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)) -> Any:
    """就绪检查：验证数据库可用性。"""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content=error_content("DB_UNAVAILABLE", "database not ready", {}),
        )
    return HealthOut(status="ok", database="ok")


# ---------------------------------------------------------------------------
# 问卷
# ---------------------------------------------------------------------------


@api_router.post("/surveys", status_code=201, response_model=SurveyOut)
async def create_survey(
    payload: SurveyInput, session: AsyncSession = Depends(get_session)
) -> SurveyOut:
    record = await survey_service.create(session, payload)
    await session.commit()
    return _survey_out(record)


@api_router.get("/surveys", response_model=SurveyListOut)
async def list_surveys(
    page: int = Query(default=1),
    page_size: int = Query(default=SURVEY_DEFAULT_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> SurveyListOut:
    effective_page = max(1, page)
    effective_size = _clamp(
        page_size, minimum=1, maximum=SURVEY_MAX_PAGE_SIZE, default=SURVEY_DEFAULT_PAGE_SIZE
    )
    records, total = await survey_service.list(
        session, page=effective_page, page_size=effective_size
    )
    return SurveyListOut(
        items=[_survey_out(record) for record in records],
        total=total,
        page=effective_page,
        page_size=effective_size,
    )


@api_router.get("/surveys/{survey_id}", response_model=SurveyOut)
async def get_survey(
    survey_id: UUID, session: AsyncSession = Depends(get_session)
) -> SurveyOut:
    record = await survey_service.get(session, survey_id)
    return _survey_out(record)


@api_router.patch("/surveys/{survey_id}", response_model=SurveyOut)
async def patch_survey(
    survey_id: UUID,
    payload: SurveyPatchRequest,
    session: AsyncSession = Depends(get_session),
) -> SurveyOut:
    """PATCH 携带 ``expected_revision``；不匹配 → 409（已有 run 使用各自冻结快照不受影响）。"""
    survey_input = SurveyInput(
        title=payload.title, product=payload.product, question=payload.question
    )
    # 说明：T1 的 SurveyService.update() 的 product/question 形参均接收完整 SurveyInput
    # （内部取 .product/.question），此处传入同一实例以同时刷新两部分内容。
    record = await survey_service.update(
        session,
        survey_id,
        expected_revision=payload.expected_revision,
        title=survey_input.title,
        product=survey_input,
        question=survey_input,
    )
    await session.commit()
    return _survey_out(record)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


@api_router.post("/imports", status_code=201, response_model=ImportPreviewOut)
async def create_import_endpoint(
    file: UploadFile = File(...),
    sheet: str | None = Form(default=None),
    column_mapping: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> ImportPreviewOut:
    """上传 CSV/XLSX、列映射；返回 import_id、row_count 与 3 行预览。"""
    from app.personas.source import InvalidUserTableError

    data = await file.read()
    try:
        mapping = json.loads(column_mapping)
    except json.JSONDecodeError as exc:
        raise InvalidUserTableError("column_mapping must be valid JSON") from exc

    record, parsed = await create_import(
        session,
        data=data,
        filename=file.filename,
        sheet_name=sheet or None,
        column_mapping=mapping,
    )
    await session.commit()
    preview = build_preview(record, parsed)
    return ImportPreviewOut(
        import_id=preview.import_id,
        row_count=preview.row_count,
        sheet_name=preview.sheet_name,
        source_version=preview.source_version,
        preview=preview.preview,
        errors=preview.errors,
    )


@api_router.get("/imports/{import_id}", response_model=ImportPreviewOut)
async def get_import(
    import_id: UUID, session: AsyncSession = Depends(get_session)
) -> ImportPreviewOut:
    """返回导入元数据与 3 行预览（**不返回全表**）。"""
    record = await _get_import_record(session, import_id)
    snapshots = [PersonaSnapshot.model_validate(item) for item in record.snapshots_json]
    return _import_preview_out(record, snapshots)


# ---------------------------------------------------------------------------
# 运行预览 / 创建
# ---------------------------------------------------------------------------


@api_router.post("/runs/preview", response_model=RunPreviewOut)
async def preview_run(
    payload: RunPreviewRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunPreviewOut:
    """预算/时长估算与 3 条画像材料预览。**不调用模型**（主文档 §8.2 / Q6）。"""
    survey = await survey_service.get(session, payload.survey_id)
    if survey.revision != payload.survey_revision:
        raise RevisionConflictError(payload.survey_revision, survey.revision)

    record = await _get_import_record(session, payload.import_id)
    snapshots = [PersonaSnapshot.model_validate(item) for item in record.snapshots_json]
    sample_size = len(snapshots)

    survey_input = SurveyInput.model_validate(
        {
            "title": survey.title,
            "product": survey.product_json,
            "question": survey.question_json,
        }
    )

    sample = snapshots[:PREVIEW_SAMPLE_LIMIT]
    requests = [
        build_request_for_persona(
            survey=survey_input,
            persona_snapshot=snapshot.model_dump(mode="json"),
            model=settings.model_name,
            provider=settings.model_provider,
            max_output_tokens=settings.model_max_output_tokens,
            timeout_seconds=settings.model_timeout_seconds,
            allow_reason=True,
        )
        for snapshot in sample
    ]

    prices_missing = (
        settings.input_price_per_million is None or settings.output_price_per_million is None
    )
    estimated_cost_str: str | None = None
    if not prices_missing and requests:
        reservations = [
            reservation_for_request(
                request,
                input_price_per_million=settings.input_price_per_million,
                output_price_per_million=settings.output_price_per_million,
            )
            for request in requests
        ]
        average = sum(reservations, Decimal("0")) / len(reservations)
        total = (average * sample_size).quantize(Decimal("0.000001"))
        estimated_cost_str = str(total)

    duration_seconds: float | None = None
    if requests:
        avg_input = sum(
            estimate_input_tokens(request.system_prompt, request.user_message)
            for request in requests
        ) / len(requests)
        tokens_per_request = avg_input + settings.model_max_output_tokens
        throughput = min(
            settings.agent_concurrency / ASSUMED_LATENCY_SECONDS,
            settings.model_rpm / 60.0,
            settings.model_tpm / (60.0 * tokens_per_request),
        )
        if throughput > 0:
            duration_seconds = sample_size / throughput

    prompt_previews = [request.user_message for request in requests[:PROMPT_PREVIEW_COUNT]]

    return RunPreviewOut(
        sample_size=sample_size,
        target_concurrency=settings.agent_concurrency,
        estimated_cost=estimated_cost_str,
        cost_is_unknown=estimated_cost_str is None,
        estimated_duration_seconds=duration_seconds,
        prompt_previews=prompt_previews,
    )


@api_router.post("/runs", status_code=201, response_model=RunViewOut)
async def create_run(
    payload: RunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """创建 ``ready`` 批次（同事务写入冻结快照 + 全部成员）。**必须** ``Idempotency-Key``。"""
    if not idempotency_key or not idempotency_key.strip():
        raise RunValidationError("Idempotency-Key header is required")

    run = await run_service.create_run(
        session,
        survey_id=payload.survey_id,
        survey_revision=payload.survey_revision,
        import_id=payload.import_id,
        model_config_id=payload.model_config_id,
        budget_limit=payload.budget_limit,
        budget_currency=payload.budget_currency,
        request_limit=payload.request_limit,
        idempotency_key=idempotency_key,
        # P2-⑤（TEAM-BRIEF §7.11.2）：快照必须取自**路由注入的** settings，而非进程级单例，
        # 否则 `runs.model_snapshot["provider"]` 会与 `start` 门禁（Q6）所用的 provider 分歧，
        # 使红线 #2「mock 必须可溯源」出现不可核查的缝隙。生产下二者同一单例，行为不变。
        settings=settings,
    )
    await session.commit()
    return await _run_view(session, run, settings)


# ---------------------------------------------------------------------------
# 运行控制（主文档 §8.2 / §6.2 / §6.4；状态机与幂等见 architecture.md §3.1/§3.3/§6.8）
#
# 状态码：首次生效的控制命令 **202**；``pause``/``cancel`` 重复点击且已达目标状态 **200**；
# 非法转移 / 幂等冲突 **409**；``retry-failed`` 缺少 ``Idempotency-Key`` **422**。
# 业务逻辑全部在 ``runs/service.py``；本层只做参数解析/依赖注入/异常抛出/序列化。
# ---------------------------------------------------------------------------


@api_router.post("/runs/{run_id}/start", status_code=202, response_model=RunViewOut)
async def start_run_endpoint(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """``ready → running``。存在其它活动批次 → 409；非 ``ready`` 的重复 start → 409（I1/I12）。

    **模型配置门禁（裁决 Q6，`TEAM-BRIEF §7.9.2`；只落在 API 层）**：``MODEL_PROVIDER == "mock"``
    时**跳过**校验（mock 是本机唯一可用模式，§7.5 T6-d 约定它是唯一选择依据）；否则要求
    ``is_model_configured()`` 为真**且** ``api_key()`` 非空，任一不满足 → **422**
    ``MODEL_CONFIG_INVALID``。门禁在**任何状态转移之前**触发：被拒绝的 ``start`` 之后该 run
    仍为 ``ready``。**不改 ``RunService.start_run`` 的签名与行为**（服务层直调属合法用法）。
    """
    if settings.model_provider != "mock":
        configured = settings.is_model_configured()
        api_key_present = bool(settings.api_key())
        if not configured or not api_key_present:
            raise ModelConfigInvalidError(
                "model is not configured; set MODEL_PROVIDER=mock or provide a valid "
                "MODEL_ENDPOINT/MODEL_NAME and API key before starting a run",
                details={
                    "provider": settings.model_provider,
                    "model_configured": configured,
                    "api_key_present": api_key_present,
                },
            )
    run = await run_service.start_run(session, run_id)
    return await _run_view(session, run, settings)


@api_router.post("/runs/{run_id}/pause", status_code=202, response_model=RunViewOut)
async def pause_run_endpoint(
    run_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """``running → pausing``（``pause_reason='user'``）；重复 pause 且已达目标状态 → 200。"""
    outcome = await run_service.pause_run(session, run_id)
    response.status_code = outcome.status_code
    return await _run_view(session, outcome.run, settings)


@api_router.post("/runs/{run_id}/resume", status_code=202, response_model=RunViewOut)
async def resume_run_endpoint(
    run_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """``paused → running``（同一成员/模型快照，只跑剩余）；非法状态 → 409。"""
    outcome = await run_service.resume_run(session, run_id)
    response.status_code = outcome.status_code
    return await _run_view(session, outcome.run, settings)


@api_router.post("/runs/{run_id}/cancel", status_code=202, response_model=RunViewOut)
async def cancel_run_endpoint(
    run_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """``ready/running/pausing/paused → cancelling``；重复 cancel 且已达目标状态 → 200。"""
    outcome = await run_service.cancel_run(session, run_id)
    response.status_code = outcome.status_code
    return await _run_view(session, outcome.run, settings)


@api_router.post("/runs/{run_id}/retry-failed", status_code=202, response_model=RunViewOut)
async def retry_failed_endpoint(
    run_id: UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """仅重新安排 ``failed`` 成员（+3 额度回 ``pending``；run → ``ready``）。

    **必须** ``Idempotency-Key``；同 key 重复调用幂等（不多加额度）；对 ``completed`` → 409。
    """
    if not idempotency_key or not idempotency_key.strip():
        raise RunValidationError("Idempotency-Key header is required")
    outcome = await run_service.retry_failed(
        session, run_id, idempotency_key=idempotency_key
    )
    response.status_code = outcome.status_code
    return await _run_view(session, outcome.run, settings)


@api_router.patch("/runs/{run_id}/budget", status_code=200, response_model=RunViewOut)
async def update_budget_endpoint(
    run_id: UUID,
    payload: RunBudgetPatchRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    """**只允许提高** ``budget_limit``，**不得改币种**（降低/改币种 → 422）；不改快照/样本数。"""
    outcome = await run_service.update_budget(
        session,
        run_id,
        budget_limit=payload.budget_limit,
        budget_currency=payload.budget_currency,
        request_limit=payload.request_limit,
    )
    return await _run_view(session, outcome.run, settings)


@api_router.get("/runs", response_model=RunListOut)
async def list_runs(
    page: int = Query(default=1),
    page_size: int = Query(default=RUNS_DEFAULT_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunListOut:
    effective_page = max(1, page)
    effective_size = _clamp(
        page_size, minimum=1, maximum=RUNS_MAX_PAGE_SIZE, default=RUNS_DEFAULT_PAGE_SIZE
    )
    total = await session.scalar(select(func.count()).select_from(RunRecord))
    result = await session.execute(
        select(RunRecord)
        .order_by(RunRecord.created_at.desc(), RunRecord.id)
        .offset((effective_page - 1) * effective_size)
        .limit(effective_size)
    )
    runs = list(result.scalars().all())
    items = [await _run_view(session, run, settings) for run in runs]
    return RunListOut(
        items=items, total=int(total or 0), page=effective_page, page_size=effective_size
    )


@api_router.get("/runs/{run_id}", response_model=RunViewOut)
async def get_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> RunViewOut:
    run = await run_service.get_run(session, run_id)
    return await _run_view(session, run, settings)


@api_router.get("/runs/{run_id}/results", response_model=ResultsPageOut)
async def get_results(
    run_id: UUID,
    page: int = Query(default=1),
    page_size: int = Query(default=RESULTS_DEFAULT_PAGE_SIZE),
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ResultsPageOut:
    """逐人明细（按 ``row_no`` 排序，可按成员状态过滤；默认 50、最大 200）。"""
    await run_service.get_run(session, run_id)  # 不存在 → 404

    if status is not None and status not in _MEMBER_STATUSES:
        raise RunValidationError(f"unknown member status: {status!r}")

    effective_page = max(1, page)
    effective_size = _clamp(
        page_size, minimum=1, maximum=RESULTS_MAX_PAGE_SIZE, default=RESULTS_DEFAULT_PAGE_SIZE
    )

    base = select(RunMember).where(RunMember.run_id == run_id)
    count_stmt = select(func.count()).select_from(RunMember).where(RunMember.run_id == run_id)
    if status is not None:
        base = base.where(RunMember.status == status)
        count_stmt = count_stmt.where(RunMember.status == status)

    total = await session.scalar(count_stmt)
    result = await session.execute(
        base.order_by(RunMember.row_no)
        .offset((effective_page - 1) * effective_size)
        .limit(effective_size)
    )
    members = list(result.scalars().all())
    return ResultsPageOut(
        items=[_member_out(member) for member in members],
        total=int(total or 0),
        page=effective_page,
        page_size=effective_size,
    )


@api_router.get("/runs/{run_id}/summary", response_model=RunSummary)
async def get_summary(
    run_id: UUID, session: AsyncSession = Depends(get_session)
) -> RunSummary:
    """五档计数/比率/分组 + coverage（一致性读事务；**ReportService 只做 SQL 聚合**）。"""
    return await report_service.summary(session, run_id)


@api_router.get("/runs/{run_id}/export.csv")
async def export_csv(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """流式导出全成员 CSV（UTF-8 BOM；失败行 value 为空；不导出完整画像）。"""
    await load_run_meta(session, run_id)  # 不存在 → 404（在开始流式前返回）
    maker = request.app.state.sessionmaker
    filename = f"run-{run_id}.csv"
    return StreamingResponse(
        stream_csv(maker, run_id),
        media_type=CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["api_router"]
