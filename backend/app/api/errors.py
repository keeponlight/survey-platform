# SPDX-License-Identifier: GPL-3.0-or-later
"""统一错误体与领域异常 → HTTP 状态码映射（主文档 §8.2）。

错误体统一为 ``{"code","message","details"}``（ID 为 UUID，时间 ISO 8601 UTC）。

关键映射（**不得泄漏 500**）：

- ``ActiveRunConflictError`` → **409**
- ``IdempotencyConflictError`` → **409**
- ``RevisionConflictError`` / ``InvalidRunStateError`` → **409**
- ``RunValidationError`` / ``SurveyValidationError`` / ``InvalidUserTableError`` → **422**
- ``ModelConfigInvalidError``（Q6 门禁）→ **422**（code ``MODEL_CONFIG_INVALID``）
- ``RunNotFoundError`` / ``SurveyNotFoundError`` / ``ReportNotFoundError`` → **404**
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.personas.source import InvalidUserTableError
from app.reports.service import ReportNotFoundError
from app.runs.service import (
    ActiveRunConflictError,
    IdempotencyConflictError,
    InvalidRunStateError,
    RunNotFoundError,
    RunValidationError,
)
from app.surveys.service import (
    RevisionConflictError,
    SurveyNotFoundError,
    SurveyValidationError,
)

logger = logging.getLogger("app.api.errors")


class NotFoundError(Exception):
    """通用「资源不存在」（→ 404）。"""

    code = "NOT_FOUND"


class ModelConfigInvalidError(Exception):
    """模型配置不满足 ``start`` 的前置条件（裁决 Q6；→ 422，code ``MODEL_CONFIG_INVALID``）。

    由 **API 层门禁**（``POST /runs/{id}/start``）抛出。它是「HTTP 语义的配置门禁」，
    故**不**落到 ``RunService.start_run``（服务层是内部接口，直调属合法用法；改服务层会
    连带改 16 条无关用例，见 TEAM-BRIEF §7.9.2）。
    """

    code = "MODEL_CONFIG_INVALID"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self._details: dict[str, Any] = dict(details) if details else {}

    def to_details(self) -> dict[str, Any]:
        return dict(self._details)


#: 领域异常 → HTTP 状态码（T4 三项核心映射已包含）。
STATUS_BY_EXCEPTION: dict[type[Exception], int] = {
    NotFoundError: 404,
    RunNotFoundError: 404,
    SurveyNotFoundError: 404,
    ReportNotFoundError: 404,
    RevisionConflictError: 409,
    InvalidRunStateError: 409,
    ActiveRunConflictError: 409,
    IdempotencyConflictError: 409,
    RunValidationError: 422,
    SurveyValidationError: 422,
    InvalidUserTableError: 422,
    ModelConfigInvalidError: 422,
}


def error_content(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造统一错误体。"""
    return {"code": code, "message": message, "details": details or {}}


def _details_for(exc: Exception) -> dict[str, Any]:
    to_details = getattr(exc, "to_details", None)
    if callable(to_details):
        result = to_details()
        if isinstance(result, dict):
            return result
    extra: dict[str, Any] = {}
    for attr in ("idempotency_key", "source", "current", "expected"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            extra[attr] = value
    return extra


def register_exception_handlers(app: FastAPI) -> None:
    """把领域异常、请求校验错误与兜底异常统一为 ``{"code","message","details"}``。"""

    for exc_type, status_code in STATUS_BY_EXCEPTION.items():
        app.add_exception_handler(
            exc_type,
            _make_domain_handler(status_code),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(  # noqa: ANN202
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_content(
                "VALIDATION_ERROR", "request validation failed", {"errors": errors}
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(  # noqa: ANN202
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            code = str(detail.get("code"))
            message = str(detail.get("message", code))
            details = detail.get("details") or {}
        else:
            code = f"HTTP_{exc.status_code}"
            message = str(detail)
            details = {}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_content(code, message, details),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(  # noqa: ANN202
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_content("INTERNAL_ERROR", "internal server error", {}),
        )


def _make_domain_handler(status_code: int):  # noqa: ANN202
    async def _handler(request: Request, exc: Exception) -> JSONResponse:
        code = str(getattr(exc, "code", "ERROR"))
        return JSONResponse(
            status_code=status_code,
            content=error_content(code, str(exc), _details_for(exc)),
        )

    return _handler


__all__ = [
    "ModelConfigInvalidError",
    "NotFoundError",
    "STATUS_BY_EXCEPTION",
    "error_content",
    "register_exception_handlers",
]
