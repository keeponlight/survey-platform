# SPDX-License-Identifier: GPL-3.0-or-later
"""T1 问卷草稿与版本测试（主文档 §6.1 / §8.2）。

验收命令：``uv run pytest tests/test_personas.py tests/test_surveys.py -q``
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.contracts import ProductInput, QuestionInput, SurveyInput
from app.surveys.service import (
    RevisionConflictError,
    SurveyNotFoundError,
    frozen_snapshot,
    survey_service,
)


def _make_input(title: str = "购买意向问卷", price: str = "199.00") -> SurveyInput:
    return SurveyInput(
        title=title,
        product=ProductInput(
            name="示例产品",
            description="用于演示的产品",
            price=Decimal(price),
            price_unit="元/件",
            time_range="未来 30 天",
        ),
        question=QuestionInput(),
    )


async def test_create_get_list(db_session) -> None:
    created = await survey_service.create(db_session, _make_input())
    await db_session.commit()

    fetched = await survey_service.get(db_session, created.id)
    assert fetched.revision == 1
    assert fetched.product_json["price"] == "199.00"

    records, total = await survey_service.list(db_session)
    assert total == 1
    assert records[0].id == created.id


async def test_get_missing_raises_not_found(db_session) -> None:
    with pytest.raises(SurveyNotFoundError):
        await survey_service.get(db_session, uuid4())


async def test_revision_conflict_409(db_session) -> None:
    """PATCH 带 expected_revision，不匹配必须抛 RevisionConflictError（→409）。"""
    created = await survey_service.create(db_session, _make_input())
    await db_session.commit()

    # 正确 revision：更新成功，revision 2
    updated = await survey_service.update(
        db_session, created.id, expected_revision=1, title="改名"
    )
    await db_session.commit()
    assert updated.revision == 2
    assert updated.title == "改名"

    # 过期的 expected_revision：冲突
    with pytest.raises(RevisionConflictError) as excinfo:
        await survey_service.update(db_session, created.id, expected_revision=1, title="再改")
    assert excinfo.value.code == "REVISION_CONFLICT"
    assert excinfo.value.to_details() == {"expected_revision": 1, "current_revision": 2}


async def test_draft_edit_does_not_pollute_history(db_session) -> None:
    """修改草稿只产生新 revision，不污染此前已冻结的快照。"""
    created = await survey_service.create(db_session, _make_input(price="199.00"))
    await db_session.commit()

    history_snapshot = frozen_snapshot(created)
    assert history_snapshot["revision"] == 1
    assert history_snapshot["product"]["price"] == "199.00"

    # 修改价格
    new_input = _make_input(price="299.00")
    updated = await survey_service.update(
        db_session, created.id, expected_revision=1, product=new_input
    )
    await db_session.commit()
    assert updated.revision == 2
    assert updated.product_json["price"] == "299.00"

    # 历史快照保持不变（深拷贝，未被污染）。
    assert history_snapshot["revision"] == 1
    assert history_snapshot["product"]["price"] == "199.00"

    # 新的冻结快照反映新版本。
    fresh_snapshot = frozen_snapshot(updated)
    assert fresh_snapshot["revision"] == 2
    assert fresh_snapshot["product"]["price"] == "299.00"


async def test_clone_creates_new_draft(db_session) -> None:
    created = await survey_service.create(db_session, _make_input())
    await db_session.commit()

    clone = await survey_service.clone(db_session, created.id)
    await db_session.commit()

    assert clone.id != created.id
    assert clone.revision == 1
    assert clone.product_json == created.product_json
    assert clone.question_json == created.question_json
    assert clone.title.endswith("(copy)")
