# SPDX-License-Identifier: GPL-3.0-or-later
"""T0 契约测试：单题/五档固定、必填校验、行号与 ID 保留、禁止额外字段。

验收命令：``cd backend && uv run pytest tests/test_contracts.py -q``
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts import (
    DEFAULT_QUESTION_PROMPT,
    OPTION_LABELS_ZH,
    OPTION_SCORES,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    PersonaSnapshot,
    ProductInput,
    QuestionInput,
    QuestionOption,
    RunCreate,
    SurveyInput,
    ValidatedAnswer,
    default_options,
)


def _product(**overrides: object) -> ProductInput:
    data: dict[str, object] = {
        "name": "示例产品",
        "description": "一款用于演示的产品",
        "price": Decimal("199.00"),
        "price_unit": "元/件",
        "time_range": "未来 30 天",
    }
    data.update(overrides)
    return ProductInput(**data)


def _survey(**overrides: object) -> SurveyInput:
    data: dict[str, object] = {
        "title": "购买意向问卷",
        "product": _product(),
        "question": QuestionInput(),
    }
    data.update(overrides)
    return SurveyInput(**data)


# ---------------------------------------------------------------------------
# 单题 / 五档固定
# ---------------------------------------------------------------------------


def test_single_question_only() -> None:
    """一份问卷只允许 1 道题：列表形式的 question 必须被拒。"""
    with pytest.raises(ValidationError):
        SurveyInput(
            title="多题问卷",
            product=_product(),
            question=[QuestionInput(), QuestionInput()],  # type: ignore[arg-type]
        )

    # 也不允许通过额外的 questions 字段夹带第二题（extra=forbid）。
    with pytest.raises(ValidationError):
        SurveyInput(
            title="多题问卷",
            product=_product(),
            question=QuestionInput(),
            questions=[QuestionInput()],  # type: ignore[call-arg]
        )

    # 合法：恰好一道题。
    survey = _survey()
    assert survey.question.question_id == PURCHASE_INTENT_QUESTION_ID


def test_five_fixed_values() -> None:
    """题目只允许 5 个固定 value，顺序与 score 均固定。"""
    question = QuestionInput()
    assert tuple(option.value for option in question.options) == OPTION_VALUES
    assert len(question.options) == 5
    for option in question.options:
        assert option.score == OPTION_SCORES[option.value]
        assert option.label == OPTION_LABELS_ZH[option.value]

    # 自定义（非固定）选项集合必须被拒。
    with pytest.raises(ValidationError):
        QuestionInput(
            options=[QuestionOption(value="yes", label="是", score=1)]
        )
    with pytest.raises(ValidationError):
        # value 固定但 score 被篡改
        bad = default_options()
        bad[0] = QuestionOption(value="definitely_not", label="肯定不会购买", score=5)
        QuestionInput(options=bad)

    # 题目 ID 固定，不允许改成其它值。
    with pytest.raises(ValidationError):
        QuestionInput(question_id="other_question")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 必填项校验
# ---------------------------------------------------------------------------


def test_reject_missing_price() -> None:
    """缺价格必须被拒。"""
    with pytest.raises(ValidationError):
        ProductInput(
            name="无价产品",
            description="缺少价格",
            price_unit="元/件",
        )
    with pytest.raises(ValidationError):
        ProductInput(
            name="价格为 0",
            description="价格非法",
            price=Decimal("0"),
            price_unit="元/件",
        )
    with pytest.raises(ValidationError):
        ProductInput(
            name="缺单位",
            description="缺计价单位",
            price=Decimal("1"),
        )


def test_reject_missing_persona_id() -> None:
    """缺画像 ID 必须被拒。"""
    with pytest.raises(ValidationError):
        PersonaSnapshot(row_no=1, profile_text="26岁，居住上海")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PersonaSnapshot(row_no=1, persona_id="", profile_text="x")


def test_reject_missing_profile_text() -> None:
    """缺画像文本必须被拒。"""
    with pytest.raises(ValidationError):
        PersonaSnapshot(row_no=1, persona_id="p_1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PersonaSnapshot(row_no=1, persona_id="p_1", profile_text="")


# ---------------------------------------------------------------------------
# 行号 / ID 保留 + 不按年龄过滤
# ---------------------------------------------------------------------------


def test_row_no_and_persona_id_preserved() -> None:
    """row_no 与 persona_id 必须原样保留（不重编号、不改写）。"""
    snapshot = PersonaSnapshot(row_no=7, persona_id="p_000007", profile_text="26岁，女性，上海")
    assert snapshot.row_no == 7
    assert snapshot.persona_id == "p_000007"
    dumped = snapshot.model_dump()
    assert dumped["row_no"] == 7
    assert dumped["persona_id"] == "p_000007"


def test_no_auto_age_filter() -> None:
    """不得按年龄（或缺失年龄）自动过滤输入行：任意年龄/None 均被接受。"""
    ages: list[int | None] = [None, 15, 26, 45, 70]
    snapshots = [
        PersonaSnapshot(
            row_no=index + 1,
            persona_id=f"p_{index}",
            profile_text="画像文本",
            age=age,
        )
        for index, age in enumerate(ages)
    ]
    # 全部保留，数量不变，row_no 原样。
    assert len(snapshots) == len(ages)
    assert [snapshot.row_no for snapshot in snapshots] == [1, 2, 3, 4, 5]
    assert [snapshot.age for snapshot in snapshots] == ages


# ---------------------------------------------------------------------------
# 额外字段禁止
# ---------------------------------------------------------------------------


def test_extra_field_forbidden() -> None:
    """ValidatedAnswer 使用 extra='forbid'，额外字段必须被拒。"""
    answer = ValidatedAnswer(question_id="purchase_intent", value="probably_yes")
    assert answer.score == OPTION_SCORES["probably_yes"]

    with pytest.raises(ValidationError):
        ValidatedAnswer(  # type: ignore[call-arg]
            question_id="purchase_intent",
            value="probably_yes",
            confidence=0.9,
        )
    # 非法 value 也要被拒。
    with pytest.raises(ValidationError):
        ValidatedAnswer(question_id="purchase_intent", value="maybe")
    # 错误 question_id 也要被拒。
    with pytest.raises(ValidationError):
        ValidatedAnswer(question_id="other", value="unsure")


def test_validated_answer_score_derived_not_input() -> None:
    """score 由 value 派生，明示输入 score 属额外字段，必须被拒。"""
    with pytest.raises(ValidationError):
        ValidatedAnswer(  # type: ignore[call-arg]
            question_id="purchase_intent",
            value="definitely_yes",
            score=5,
        )
    assert ValidatedAnswer(question_id="purchase_intent", value="definitely_yes").score == 5


def test_run_create_contract() -> None:
    """RunCreate 基本契约（含 model_config_id，不含密钥）。"""
    import uuid

    run = RunCreate(
        survey_id=uuid.uuid4(),
        survey_revision=1,
        import_id=uuid.uuid4(),
        model_config_id="default",
    )
    assert run.budget_currency == "CNY"
    assert run.budget_limit is None
    assert run.request_limit is None
    with pytest.raises(ValidationError):
        RunCreate(  # type: ignore[call-arg]
            survey_id=uuid.uuid4(),
            survey_revision=0,  # revision 必须 >= 1
            import_id=uuid.uuid4(),
            model_config_id="default",
        )


def test_default_prompt_is_editable_but_defaulted() -> None:
    """题干默认为主文档 §5.1 文本，但允许编辑。"""
    assert QuestionInput().prompt == DEFAULT_QUESTION_PROMPT
    assert QuestionInput(prompt="自定义题干").prompt == "自定义题干"


def test_option_triple_is_canonical() -> None:
    """value ↔ label ↔ score 三元组固定且必须一致；错配 score / 非法 label 均被拒。"""
    canonical: dict[str, tuple[str, int]] = {
        "definitely_not": ("肯定不会购买", 1),
        "probably_not": ("可能不会购买", 2),
        "unsure": ("不确定", 3),
        "probably_yes": ("可能会购买", 4),
        "definitely_yes": ("肯定会购买", 5),
    }
    # 逐档：默认选项三元组与 canonical 完全一致，且可用显式三元组重建。
    for option in default_options():
        label, score = canonical[option.value]
        assert option.label == label
        assert option.score == score
        rebuilt = QuestionOption(value=option.value, label=label, score=score)
        assert (rebuilt.value, rebuilt.label, rebuilt.score) == (option.value, label, score)

    # 错配 score（value 对、label 对、score 错）必须被拒。
    with pytest.raises(ValidationError):
        QuestionOption(value="definitely_not", label="肯定不会购买", score=5)
    with pytest.raises(ValidationError):
        QuestionOption(value="definitely_yes", label="肯定会购买", score=1)
    # 非法 / 自由文本 label 必须被拒（Literal 层）。
    with pytest.raises(ValidationError):
        QuestionOption(value="definitely_not", label="FREE-TEXT-LABEL", score=1)
    # 非法 value 必须被拒。
    with pytest.raises(ValidationError):
        QuestionOption(value="maybe", label="肯定不会购买", score=1)
