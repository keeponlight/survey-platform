# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP 请求 / 响应契约（Pydantic v2，主文档 §8.2 / §8.3）。

字段名一律照抄主文档；金额以**字符串**出参（``Decimal`` → ``str``），``null`` 表示**未知**。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import PersonaSnapshot, SurveyInput

#: pause_reason → 可操作说明（前端也会兜底，但以服务端为准）。
PAUSE_HINTS: dict[str, str] = {
    "user": "已按操作暂停。点击「恢复」继续剩余任务。",
    "api_auth": "API 认证失败，请修复服务端模型配置后恢复。",
    "api_unavailable": "模型服务暂不可用或持续限流，请稍后恢复。",
    "budget": "预算或请求额度不足，请提高预算后恢复。",
}


# ---------------------------------------------------------------------------
# 问卷
# ---------------------------------------------------------------------------


class SurveyOut(BaseModel):
    """问卷详情/列表项。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    title: str
    product: dict[str, Any]
    question: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class SurveyListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SurveyOut]
    total: int
    page: int
    page_size: int


class SurveyPatchRequest(SurveyInput):
    """``PATCH /surveys/{id}`` 请求体 = 问卷内容 + ``expected_revision``（乐观锁）。"""

    expected_revision: int = Field(ge=1)


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


class ImportPreviewOut(BaseModel):
    """``POST /imports`` / ``GET /imports/{id}`` 出参（不返回全表）。"""

    model_config = ConfigDict(extra="forbid")

    import_id: UUID
    row_count: int
    sheet_name: str | None
    source_version: str
    preview: list[PersonaSnapshot]
    errors: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


class RunPreviewRequest(BaseModel):
    """``POST /runs/preview`` 请求体（**不调用模型**）。"""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    survey_id: UUID
    survey_revision: int = Field(ge=1)
    import_id: UUID
    model_config_id: str = Field(min_length=1)


class RunPreviewOut(BaseModel):
    """预算/时长估算与材料预览（创建与预览均**不调用模型**）。"""

    model_config = ConfigDict(extra="forbid")

    sample_size: int
    target_concurrency: int
    #: 预估成本（字符串）；``None`` = 未知（未配置单价）。
    estimated_cost: str | None
    cost_is_unknown: bool
    estimated_duration_seconds: float | None
    prompt_previews: list[str]


class RunCountsOut(BaseModel):
    """成员计数（``valid_count`` = succeeded，主文档 §8.3）。"""

    model_config = ConfigDict(extra="forbid")

    succeeded: int
    failed: int
    pending: int
    running: int
    retry_wait: int
    cancelled: int
    valid_count: int


class RunViewOut(BaseModel):
    """``GET /runs`` / ``GET /runs/{id}`` / 控制动作出参。

    ``in_flight`` / ``throttled`` 取**真实值**（分别 = ``running`` / ``retry_wait`` 成员数），
    不得缺省为 0（避免前端把缺失字段兜成好看的假数字）。
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    survey_id: UUID
    import_id: UUID
    status: str
    pause_reason: str | None
    pause_hint: str | None
    error_summary: str | None
    prompt_version: str

    target_concurrency: int
    in_flight: int
    throttled: int

    sample_size: int
    counts: RunCountsOut

    #: 已知费用（字符串）；``None`` = 未知（不写成 0）。
    actual_cost: str | None
    unknown_cost_count: int
    budget_limit: str | None
    budget_currency: str
    request_limit: int | None
    requests_reserved: int | None

    allowed_actions: list[str]

    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunViewOut]
    total: int
    page: int
    page_size: int


class RunBudgetPatchRequest(BaseModel):
    """``PATCH /runs/{id}/budget`` 请求体（主文档 §8.2：只允许提高预算，不得改币种）。

    - ``budget_limit`` **必填且必须高于现值**（降低 → 422）。
    - ``budget_currency`` 若提供则必须等于 run 现有币种（不一致 → 422）。
    - ``request_limit`` 可选，**仅允许提高**。
    """

    model_config = ConfigDict(extra="forbid")

    budget_limit: Decimal = Field(gt=0)
    budget_currency: str | None = Field(default=None, min_length=1)
    request_limit: int | None = Field(default=None, ge=1)


class MemberRowOut(BaseModel):
    """``GET /runs/{id}/results`` 明细项（按 ``row_no`` 排序）。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    row_no: int
    persona_id: str
    age: int | None
    city_tier: str | None
    member_status: str
    value: str | None
    score: int | None
    reason: str | None
    attempt_count: int
    error_code: str | None


class ResultsPageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MemberRowOut]
    total: int
    page: int
    page_size: int


class HealthOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    database: str | None = None


__all__ = [
    "PAUSE_HINTS",
    "HealthOut",
    "ImportPreviewOut",
    "MemberRowOut",
    "ResultsPageOut",
    "RunBudgetPatchRequest",
    "RunCountsOut",
    "RunListOut",
    "RunPreviewOut",
    "RunPreviewRequest",
    "RunViewOut",
    "SurveyListOut",
    "SurveyOut",
    "SurveyPatchRequest",
]
