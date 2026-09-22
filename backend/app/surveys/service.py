# SPDX-License-Identifier: GPL-3.0-or-later
"""问卷 CRUD 与版本（revision）冲突处理（主文档 §6.1 / §8.2 / §9）。

- 草稿可编辑；``PATCH`` 必须携带 ``expected_revision``，不匹配返回 **409**。
- 修改草稿只产生**新 revision**，不污染历史快照（已创建的 run 使用各自冻结快照）。
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import QuestionInput, SurveyInput
from app.models import SurveyRecord

#: 列表分页默认/上限（主文档 §8.2：默认 20、最大 100）。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class SurveyNotFoundError(Exception):
    """问卷不存在（API 层映射为 404）。"""

    code = "NOT_FOUND"


class RevisionConflictError(Exception):
    """``expected_revision`` 与当前 revision 不匹配（API 层映射为 409）。"""

    code = "REVISION_CONFLICT"

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"revision conflict: expected {expected}, actual {actual}"
        )
        self.expected = expected
        self.actual = actual

    def to_details(self) -> dict[str, Any]:
        return {"expected_revision": self.expected, "current_revision": self.actual}


class SurveyValidationError(Exception):
    """问卷内容不合法（API 层映射为 422）。"""

    code = "INVALID_SURVEY"


def _product_json(survey_input: SurveyInput) -> dict[str, Any]:
    return survey_input.product.model_dump(mode="json")


def _question_json(survey_input: SurveyInput) -> dict[str, Any]:
    return survey_input.question.model_dump(mode="json")


def frozen_snapshot(record: SurveyRecord) -> dict[str, Any]:
    """冻结快照（run 创建时复制；此后草稿编辑不影响它）。

    返回**深拷贝**，调用方对返回值的修改不会回写数据库对象。
    """
    return {
        "survey_id": str(record.id),
        "revision": record.revision,
        "title": record.title,
        "product": copy.deepcopy(record.product_json),
        "question": copy.deepcopy(record.question_json),
    }


class SurveyService:
    """问卷服务：创建 / 读取 / 列表 / 更新（revision CAS） / 克隆。"""

    async def create(self, session: AsyncSession, survey_input: SurveyInput) -> SurveyRecord:
        """创建问卷草稿（revision=1）。"""
        record = SurveyRecord(
            id=uuid4(),
            title=survey_input.title,
            product_json=_product_json(survey_input),
            question_json=_question_json(survey_input),
            revision=1,
        )
        session.add(record)
        await session.flush()
        return record

    async def get(self, session: AsyncSession, survey_id: UUID) -> SurveyRecord:
        """按 id 读取；不存在抛 :class:`SurveyNotFoundError`。"""
        result = await session.execute(
            select(SurveyRecord).where(SurveyRecord.id == survey_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise SurveyNotFoundError(f"survey not found: {survey_id}")
        return record

    async def get_for_update(self, session: AsyncSession, survey_id: UUID) -> SurveyRecord:
        """加行锁读取（更新路径使用，避免并发丢更新）。"""
        result = await session.execute(
            select(SurveyRecord)
            .where(SurveyRecord.id == survey_id)
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise SurveyNotFoundError(f"survey not found: {survey_id}")
        return record

    async def list(
        self,
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[list[SurveyRecord], int]:
        """分页列表（默认每页 20，最大 100）。返回 (记录列表, 总数)。"""
        if page < 1:
            page = 1
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        total = await session.scalar(select(func.count()).select_from(SurveyRecord))
        result = await session.execute(
            select(SurveyRecord)
            .order_by(SurveyRecord.created_at.desc(), SurveyRecord.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), int(total or 0)

    async def update(
        self,
        session: AsyncSession,
        survey_id: UUID,
        *,
        expected_revision: int,
        title: str | None = None,
        product: SurveyInput | None = None,
        question: QuestionInput | None = None,
    ) -> SurveyRecord:
        """PATCH 更新：``expected_revision`` 不匹配 → :class:`RevisionConflictError`（409）。

        成功时 ``revision += 1`` 且刷新 ``updated_at``。
        """
        record = await self.get_for_update(session, survey_id)
        if record.revision != expected_revision:
            raise RevisionConflictError(expected_revision, record.revision)

        if title is not None:
            if not title.strip():
                raise SurveyValidationError("title must not be empty")
            record.title = title
        if product is not None:
            record.product_json = _product_json(product)
        if question is not None:
            record.question_json = _question_json(question)

        record.revision = record.revision + 1
        record.updated_at = func.now()
        await session.flush()
        await session.refresh(record)
        return record

    async def clone(self, session: AsyncSession, survey_id: UUID) -> SurveyRecord:
        """复制已有问卷为**新草稿**（revision=1，新 id）。"""
        source = await self.get(session, survey_id)
        clone = SurveyRecord(
            id=uuid4(),
            title=f"{source.title} (copy)",
            product_json=copy.deepcopy(source.product_json),
            question_json=copy.deepcopy(source.question_json),
            revision=1,
        )
        session.add(clone)
        await session.flush()
        return clone


#: 默认服务实例（无状态，可安全共享）。
survey_service = SurveyService()


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "RevisionConflictError",
    "SurveyNotFoundError",
    "SurveyService",
    "SurveyValidationError",
    "frozen_snapshot",
    "survey_service",
]
