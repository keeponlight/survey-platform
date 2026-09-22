# SPDX-License-Identifier: GPL-3.0-or-later
"""跨层数据契约（Pydantic v2）。

命名严格照抄主文档（snake_case）：``PersonaSnapshot``、``SurveyInput``、``RunCreate``、
``ModelRequest``、``ModelResponse``、``ValidatedAnswer``。

本模块**不依赖**任何其它应用模块（T0 边界），可被所有层安全导入。

对应主文档：
    §4    数据衔接（PersonaSnapshot / row_no / persona_id / profile_text）
    §5.1  单题与五档固定契约（purchase_intent）
    §5.3  第三方 API 适配统一接口（ModelRequest / ModelResponse）
    §6.1  持久化字段命名
    §8.2  API 契约（RunCreate）
    §8.3  统计口径（score）
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# 固定枚举与常量（主文档 §5.1 / §6.2；task-list.md §8.1，禁止自创）
# ---------------------------------------------------------------------------

#: 首版唯一的题目 ID（固定，不可改）。
PURCHASE_INTENT_QUESTION_ID: Literal["purchase_intent"] = "purchase_intent"

#: 首版唯一的题型（上游 4 类中本项目只用 single_choice）。
QUESTION_TYPE_SINGLE_CHOICE: Literal["single_choice"] = "single_choice"

#: 提示词模板版本（固定）。
PROMPT_VERSION: str = "purchase-intent-zh-v1"

#: 五档 value，顺序即展示顺序（固定）。
OPTION_VALUES: tuple[str, ...] = (
    "definitely_not",
    "probably_not",
    "unsure",
    "probably_yes",
    "definitely_yes",
)

#: 五档中文展示。
OPTION_LABELS_ZH: dict[str, str] = {
    "definitely_not": "肯定不会购买",
    "probably_not": "可能不会购买",
    "unsure": "不确定",
    "probably_yes": "可能会购买",
    "definitely_yes": "肯定会购买",
}

#: 五档 score（1–5，固定）。
OPTION_SCORES: dict[str, int] = {
    "definitely_not": 1,
    "probably_not": 2,
    "unsure": 3,
    "probably_yes": 4,
    "definitely_yes": 5,
}

#: 默认题干（主文档 §5.1，可编辑）。
DEFAULT_QUESTION_PROMPT: str = (
    "结合你的实际需求、预算和现有替代方案，按照上述价格与购买条件，"
    "你在未来 30 天购买该产品的意向是？"
)

#: 默认购买时间范围（主文档 §5.1，作为问卷字段可编辑）。
DEFAULT_TIME_RANGE: str = "未来 30 天"

# --- 错误码（task-list.md §8.1）---
ERROR_INVALID_OUTPUT: str = "INVALID_OUTPUT"
ERROR_CONTEXT_TOO_LONG: str = "CONTEXT_TOO_LONG"
ERROR_INVALID_USER_TABLE: str = "INVALID_USER_TABLE"
# provider 错误映射
ERROR_TIMEOUT: str = "TIMEOUT"
ERROR_RATE_LIMITED: str = "RATE_LIMITED"
ERROR_AUTH: str = "AUTH"
ERROR_SERVER_ERROR: str = "SERVER_ERROR"
ERROR_PROTOCOL: str = "PROTOCOL"

# --- 状态枚举（主文档 §6.2）---
RunStatus = Literal[
    "ready",
    "running",
    "pausing",
    "paused",
    "cancelling",
    "cancelled",
    "completed",
    "completed_with_errors",
    "failed",
]

MemberStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "retry_wait",
    "failed",
    "cancelled",
]

AttemptStatus = Literal["running", "succeeded", "failed", "abandoned"]

PauseReason = Literal["user", "api_auth", "api_unavailable", "budget"]

#: run 处于「活动态」的集合（供 uq_runs_single_active 部分唯一索引与前置校验复用）。
ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "pausing", "paused", "cancelling")


def score_for_value(value: str) -> int:
    """返回某个 value 对应的 1–5 分；非法 value 抛 ``ValueError``（不回落中点/首项）。"""
    try:
        return OPTION_SCORES[value]
    except KeyError as exc:  # pragma: no cover - 由调用方保证 value 合法
        raise ValueError(f"unknown value: {value!r}") from exc


def default_options() -> list[QuestionOption]:
    """构造五档固定选项（顺序与 score 均固定）。"""
    return [
        QuestionOption(
            value=value,
            label=OPTION_LABELS_ZH[value],
            score=OPTION_SCORES[value],
        )
        for value in OPTION_VALUES
    ]


# ---------------------------------------------------------------------------
# 输入侧契约
# ---------------------------------------------------------------------------


class PersonaSnapshot(BaseModel):
    """输入表一行经列映射后的画像快照（主文档 §4）。

    - ``row_no`` 与 ``persona_id`` **必须保留**，不得重编号/丢弃。
    - ``age`` / ``city_tier`` 仅为**已有可选字段**；
      本模型对年龄**不加任何区间约束**，平台**不得按年龄等字段自动过滤输入行**。
    """

    model_config = ConfigDict(extra="forbid")

    row_no: int = Field(ge=1, description="输入表数据行序号（1 起，稳定排序键）")
    persona_id: str = Field(min_length=1, description="唯一用户 ID（输入表提供）")
    profile_text: str = Field(min_length=1, description="可供模型判断的画像文本")
    age: int | None = Field(default=None)
    city_tier: str | None = Field(default=None)
    source_version: str | None = Field(default=None, description="导入批次标识（import uuid）")


class QuestionOption(BaseModel):
    """单题的一个选项。

    ``value`` / ``label`` / ``score`` 三者均由主文档 §5.1 固定，且必须**互相一致**
    （例如 ``value="definitely_not"`` ⇒ ``label="肯定不会购买"`` 且 ``score=1``）。
    类型层用 ``Literal`` 锁死取值，``model_validator`` 兜住「三元组错配」，
    避免错配分数静默污染报表均分。
    """

    model_config = ConfigDict(extra="forbid")

    value: Literal[
        "definitely_not",
        "probably_not",
        "unsure",
        "probably_yes",
        "definitely_yes",
    ]
    label: Literal[
        "肯定不会购买",
        "可能不会购买",
        "不确定",
        "可能会购买",
        "肯定会购买",
    ]
    score: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def _check_triple_canonical(self) -> QuestionOption:
        expected_score = OPTION_SCORES[self.value]
        if self.score != expected_score:
            raise ValueError(
                f"score for {self.value!r} must be {expected_score}, got {self.score}"
            )
        expected_label = OPTION_LABELS_ZH[self.value]
        if self.label != expected_label:
            raise ValueError(
                f"label for {self.value!r} must be {expected_label!r}, got {self.label!r}"
            )
        return self


class QuestionInput(BaseModel):
    """单题契约（首版固定一道 ``purchase_intent`` 题）。

    结构上只允许**一个** ``question``：``SurveyInput`` 以单数字段承载之。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: Literal["purchase_intent"] = PURCHASE_INTENT_QUESTION_ID
    prompt: str = Field(default=DEFAULT_QUESTION_PROMPT, min_length=1)
    type: Literal["single_choice"] = QUESTION_TYPE_SINGLE_CHOICE
    options: list[QuestionOption] = Field(default_factory=default_options)
    required: bool = True

    @model_validator(mode="after")
    def _check_five_fixed_values(self) -> QuestionInput:
        values = [option.value for option in self.options]
        if tuple(values) != OPTION_VALUES:
            raise ValueError(
                "options must be exactly the five fixed purchase-intent values "
                f"in order: {list(OPTION_VALUES)}"
            )
        for option in self.options:
            expected = OPTION_SCORES[option.value]
            if option.score != expected:
                raise ValueError(
                    f"score for {option.value!r} must be {expected}, got {option.score}"
                )
        return self


class ProductInput(BaseModel):
    """产品信息（主文档 §5.1）。**价格与计价单位为必填**。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="产品名称")
    description: str = Field(min_length=1, description="产品说明")
    price: Decimal = Field(gt=0, description="实际人民币价格（必填）")
    price_unit: str = Field(min_length=1, description="计价单位（必填）")
    time_range: str = Field(default=DEFAULT_TIME_RANGE, min_length=1, description="购买时间范围")
    purchase_conditions: str | None = Field(default=None, description="购买条件（可选）")


class SurveyInput(BaseModel):
    """一份问卷（首版只有一道购买意向题）。

    ``question`` 为**单数字段**，从结构上保证「一份问卷只有一道题」。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    product: ProductInput
    question: QuestionInput


class RunCreate(BaseModel):
    """``POST /runs`` 请求体（主文档 §8.2）。

    ``model_config_id`` 引用后端受控模型配置；**不含任何密钥**。
    """

    # protected_namespaces=() 以允许 model_ 前缀字段名（model_config_id）。
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    survey_id: UUID
    survey_revision: int = Field(ge=1)
    import_id: UUID
    model_config_id: str = Field(min_length=1)
    budget_limit: Decimal | None = Field(default=None, gt=0)
    budget_currency: str = Field(default="CNY", min_length=1)
    request_limit: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# 模型调用侧契约（主文档 §5.3）
# ---------------------------------------------------------------------------


class ModelRequest(BaseModel):
    """第三方模型的统一请求（本平台内部契约）。

    ``system_prompt`` / ``user_message`` 为**独立 messages**；同一批受访者之间不共享上下文。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    system_prompt: str
    user_message: str
    provider: str = Field(default="openai_compatible", min_length=1)
    model: str = Field(min_length=1)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    max_output_tokens: int = Field(default=256, ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0)
    temperature: float | None = Field(default=None)
    seed: int | None = Field(default=None)


class ProviderError(BaseModel):
    """第三方返回的原始错误映射（不泄漏密钥；不回填默认答案）。"""

    model_config = ConfigDict(extra="forbid")

    error_code: str = Field(
        description="TIMEOUT / RATE_LIMITED / AUTH / SERVER_ERROR / PROTOCOL ..."
    )
    message: str = ""
    http_status: int | None = None
    retry_after_seconds: float | None = None


class ModelResponse(BaseModel):
    """第三方模型的统一响应（主文档 §5.3）。

    ``input_tokens`` / ``output_tokens`` / ``provider_request_id`` **允许为空**（空 ≠ 0）。
    出错时通过 ``error`` 字段返回原始错误映射，``raw_text`` 可能为空；
    平台**绝不**据此补默认答案。
    """

    model_config = ConfigDict(extra="forbid")

    raw_text: str = ""
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    finish_reason: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    error: ProviderError | None = None


# ---------------------------------------------------------------------------
# 校验后的答案（唯一合法产出）
# ---------------------------------------------------------------------------


class ValidatedAnswer(BaseModel):
    """通过了严格校验的答案（主文档 §5.2）。

    仅允许 ``question_id`` / ``value`` / 可选 ``reason``；``extra="forbid"`` 拒绝额外字段。
    ``score`` 为派生字段（不属于输入）。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(default=PURCHASE_INTENT_QUESTION_ID)
    value: str
    reason: str | None = Field(default=None, max_length=100)

    @field_validator("question_id")
    @classmethod
    def _fixed_question_id(cls, value: str) -> str:
        if value != PURCHASE_INTENT_QUESTION_ID:
            raise ValueError(
                f"question_id must be {PURCHASE_INTENT_QUESTION_ID!r}, got {value!r}"
            )
        return value

    @field_validator("value")
    @classmethod
    def _legal_value(cls, value: str) -> str:
        if value not in OPTION_VALUES:
            raise ValueError(f"value must be one of {list(OPTION_VALUES)}, got {value!r}")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> int:
        """1–5 分（由 value 派生，不属于输入字段）。"""
        return OPTION_SCORES[self.value]


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "AttemptStatus",
    "DEFAULT_QUESTION_PROMPT",
    "DEFAULT_TIME_RANGE",
    "ERROR_AUTH",
    "ERROR_CONTEXT_TOO_LONG",
    "ERROR_INVALID_OUTPUT",
    "ERROR_INVALID_USER_TABLE",
    "ERROR_PROTOCOL",
    "ERROR_RATE_LIMITED",
    "ERROR_SERVER_ERROR",
    "ERROR_TIMEOUT",
    "MemberStatus",
    "ModelRequest",
    "ModelResponse",
    "OPTION_LABELS_ZH",
    "OPTION_SCORES",
    "OPTION_VALUES",
    "PROMPT_VERSION",
    "PURCHASE_INTENT_QUESTION_ID",
    "PauseReason",
    "PersonaSnapshot",
    "ProductInput",
    "ProviderError",
    "QUESTION_TYPE_SINGLE_CHOICE",
    "QuestionInput",
    "QuestionOption",
    "RunCreate",
    "RunStatus",
    "SurveyInput",
    "ValidatedAnswer",
    "default_options",
    "score_for_value",
]
