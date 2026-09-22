# SPDX-License-Identifier: GPL-3.0-or-later
"""T2 单 persona 作答闭环测试（主文档 §5 / task-list.md T2）。

覆盖：合法 JSON、围栏 JSON、缺题、未知 value、额外字段、非 JSON、429、401、超时、
usage 缺失；**无补默认答案路径**；两个 persona 上下文隔离。

验收命令：``uv run pytest tests/test_inference.py -q``
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.contracts import (
    ERROR_AUTH,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    PROMPT_VERSION,
    ModelRequest,
    PersonaSnapshot,
    ProductInput,
    ProviderError,
    QuestionInput,
    SurveyInput,
)
from app.inference.prompt import (
    SYSTEM_INSTRUCTION,
    build_model_request,
    build_output_schema,
    build_prompt,
    prompt_hash,
)
from app.inference.provider import OpenAICompatibleProvider
from app.inference.validation import InvalidOutputError, parse_and_validate, strip_single_fence
from tests.fakes.fake_provider import FakeProvider

# ---------------------------------------------------------------------------
# 公共构造
# ---------------------------------------------------------------------------


def make_survey() -> SurveyInput:
    return SurveyInput(
        title="购买意向问卷",
        product=ProductInput(
            name="示例产品",
            description="用于演示的产品",
            price=Decimal("199.00"),
            price_unit="元/件",
            time_range="未来 30 天",
        ),
        question=QuestionInput(),
    )


def make_persona(persona_id: str, profile: str = "26岁，女性，上海") -> PersonaSnapshot:
    return PersonaSnapshot(row_no=1, persona_id=persona_id, profile_text=profile)


def make_request(persona: PersonaSnapshot | None = None) -> ModelRequest:
    return build_model_request(
        survey=make_survey(),
        persona=persona or make_persona("p_1"),
        model="test-model",
    )


def _provider_with(handler) -> OpenAICompatibleProvider:  # noqa: ANN001
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://example.test/v1")
    return OpenAICompatibleProvider(
        endpoint="https://example.test/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )


# ---------------------------------------------------------------------------
# 提示词模板
# ---------------------------------------------------------------------------


def test_prompt_version_and_hash_stable() -> None:
    assert PROMPT_VERSION == "purchase-intent-zh-v1"
    assert prompt_hash() == prompt_hash()
    assert len(prompt_hash()) == 64


def test_prompt_user_message_contains_blocks() -> None:
    bundle = build_prompt(make_survey(), make_persona("p_42"))
    for key in ("persona_snapshot", "product", "question", "output_schema"):
        assert f'"{key}"' in bundle.user_message
    assert "p_42" in bundle.user_message
    assert bundle.system_prompt == SYSTEM_INSTRUCTION


def test_output_schema_five_values() -> None:
    schema = build_output_schema()
    assert schema["properties"]["value"]["enum"] == [
        "definitely_not",
        "probably_not",
        "unsure",
        "probably_yes",
        "definitely_yes",
    ]
    assert schema["additionalProperties"] is False
    # 默认关闭短理由
    assert "reason" not in schema["properties"]
    assert "reason" in build_output_schema(allow_reason=True)["properties"]


# ---------------------------------------------------------------------------
# 严格校验：合法 / 围栏 / 无效
# ---------------------------------------------------------------------------


def test_valid_json() -> None:
    answer = parse_and_validate('{"question_id":"purchase_intent","value":"probably_yes"}')
    assert answer.value == "probably_yes"
    assert answer.score == 4


def test_fenced_json_single_layer() -> None:
    raw = '```json\n{"question_id":"purchase_intent","value":"definitely_yes"}\n```'
    answer = parse_and_validate(raw)
    assert answer.value == "definitely_yes"
    assert answer.score == 5

    # 无语言标记的单层围栏也可
    raw_plain = '```\n{"question_id":"purchase_intent","value":"unsure"}\n```'
    assert parse_and_validate(raw_plain).value == "unsure"


def test_double_fence_not_stripped_recursively() -> None:
    """只允许剥单层：双层围栏剥一层后仍非 JSON → 拒绝。"""
    raw = '```\n```json\n{"question_id":"purchase_intent","value":"unsure"}\n```\n```'
    with pytest.raises(InvalidOutputError):
        parse_and_validate(raw)


def test_missing_question_rejected() -> None:
    with pytest.raises(InvalidOutputError):
        parse_and_validate('{"value":"probably_yes"}')


def test_wrong_question_id_rejected() -> None:
    with pytest.raises(InvalidOutputError):
        parse_and_validate('{"question_id":"other","value":"probably_yes"}')


def test_unknown_value_rejected() -> None:
    with pytest.raises(InvalidOutputError):
        parse_and_validate('{"question_id":"purchase_intent","value":"maybe_yes"}')
    # 数值型 value 也拒绝
    with pytest.raises(InvalidOutputError):
        parse_and_validate('{"question_id":"purchase_intent","value":4}')


def test_extra_field_rejected() -> None:
    with pytest.raises(InvalidOutputError):
        parse_and_validate(
            '{"question_id":"purchase_intent","value":"unsure","confidence":0.9}'
        )


def test_reason_field_gating() -> None:
    raw = '{"question_id":"purchase_intent","value":"probably_not","reason":"预算有限"}'
    # 默认关闭短理由：reason 属额外字段 → 拒绝
    with pytest.raises(InvalidOutputError):
        parse_and_validate(raw)
    # 开启后允许，且不影响 value 有效性
    answer = parse_and_validate(raw, allow_reason=True)
    assert answer.value == "probably_not"
    assert answer.reason == "预算有限"


def test_non_json_rejected() -> None:
    with pytest.raises(InvalidOutputError):
        parse_and_validate("I think probably yes")
    with pytest.raises(InvalidOutputError):
        parse_and_validate("[1, 2, 3]")
    with pytest.raises(InvalidOutputError):
        parse_and_validate("")
    with pytest.raises(InvalidOutputError):
        parse_and_validate(None)


# ---------------------------------------------------------------------------
# 红线：绝不补默认答案
# ---------------------------------------------------------------------------


def test_no_default_answer_fill() -> None:
    """缺失/非法输出只能报错，**不得**产出任何默认答案。"""
    for raw in [
        "no json here",
        "{}",
        '{"value":"unsure"}',
        '{"question_id":"purchase_intent"}',
        '{"question_id":"purchase_intent","value":"n/a"}',
    ]:
        with pytest.raises(InvalidOutputError):
            parse_and_validate(raw)


def test_no_fallback_to_unsure_or_first_option() -> None:
    """非法 value **绝不**回落到首选项/中点/unsure。"""
    with pytest.raises(InvalidOutputError) as excinfo:
        parse_and_validate('{"question_id":"purchase_intent","value":"definitely_maybe"}')
    assert excinfo.value.code == "INVALID_OUTPUT"

    # 该函数不会返回任何值（因此不可能返回 unsure / definitely_not）。
    result = None
    try:
        result = parse_and_validate('{"question_id":"purchase_intent","value":"??"}')
    except InvalidOutputError:
        result = None
    assert result is None


def test_strip_single_fence_helper() -> None:
    assert strip_single_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_single_fence('{"a":1}') == '{"a":1}'


# ---------------------------------------------------------------------------
# fake provider 闭环（含错误分类）
# ---------------------------------------------------------------------------


async def test_fake_provider_valid_roundtrip() -> None:
    provider = FakeProvider(['{"question_id":"purchase_intent","value":"definitely_not"}'])
    response = await provider.answer(make_request())
    assert response.error is None
    answer = parse_and_validate(response.raw_text)
    assert answer.value == "definitely_not"
    assert answer.score == 1


async def test_usage_missing_allowed() -> None:
    """usage 缺失必须允许（空 ≠ 0），且不影响答案有效性。"""
    provider = FakeProvider(
        ['{"question_id":"purchase_intent","value":"probably_yes"}'],
        input_tokens=None,
        output_tokens=None,
        provider_request_id=None,
    )
    response = await provider.answer(make_request())
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.provider_request_id is None
    assert parse_and_validate(response.raw_text).value == "probably_yes"


async def test_fake_provider_retry_sequence_then_success() -> None:
    """前两次无效、第三次有效：仅第三次产出有效答案（无补值）。"""
    provider = FakeProvider(
        [
            "not json",
            '{"question_id":"purchase_intent"}',
            '{"question_id":"purchase_intent","value":"unsure"}',
        ]
    )
    outcomes: list[str] = []
    for _ in range(3):
        response = await provider.answer(make_request())
        try:
            outcomes.append(parse_and_validate(response.raw_text).value)
        except InvalidOutputError:
            outcomes.append("INVALID")
    assert outcomes == ["INVALID", "INVALID", "unsure"]


async def test_two_personas_isolated_context() -> None:
    """两个 persona 的请求不含对方画像（独立上下文）。"""
    provider = FakeProvider(['{"question_id":"purchase_intent","value":"unsure"}'])
    survey = make_survey()
    persona_alpha = make_persona("p_alpha", "26岁，女性，上海，关注性价比")
    persona_beta = make_persona("p_beta", "45岁，男性，成都，已有替代品")

    request_alpha = build_model_request(survey=survey, persona=persona_alpha, model="m")
    request_beta = build_model_request(survey=survey, persona=persona_beta, model="m")

    await provider.answer(request_alpha)
    await provider.answer(request_beta)

    alpha_msg, beta_msg = provider.requests[0].user_message, provider.requests[1].user_message
    assert "p_alpha" in alpha_msg and "p_beta" not in alpha_msg
    assert "p_beta" in beta_msg and "p_alpha" not in beta_msg
    assert "关注性价比" in alpha_msg
    assert "已有替代品" in beta_msg
    # 系统指令一致（模板固定）
    assert provider.requests[0].system_prompt == SYSTEM_INSTRUCTION


# ---------------------------------------------------------------------------
# 真实 provider 适配器：错误分类（httpx.MockTransport）
# ---------------------------------------------------------------------------


async def test_valid_openai_compatible_response_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "choices": [
                    {
                        "message": {
                            "content": '{"question_id":"purchase_intent","value":"probably_yes"}'
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is None
    assert response.input_tokens is None  # usage 缺失 → None（不是 0）
    assert response.provider_request_id == "req-1"
    assert parse_and_validate(response.raw_text).value == "probably_yes"
    await provider.aclose()


async def test_429_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "slow down"})

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == ERROR_RATE_LIMITED
    assert response.error.retry_after_seconds == 2.0
    assert response.error.http_status == 429
    await provider.aclose()


async def test_401_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == ERROR_AUTH
    await provider.aclose()


async def test_403_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == ERROR_AUTH
    await provider.aclose()


async def test_500_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == "SERVER_ERROR"
    await provider.aclose()


async def test_timeout_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == ERROR_TIMEOUT
    await provider.aclose()


async def test_protocol_error_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    provider = _provider_with(handler)
    response = await provider.answer(make_request())
    assert response.error is not None
    assert response.error.error_code == "PROTOCOL"
    await provider.aclose()


async def test_provider_returns_error_without_default_answer() -> None:
    """provider 层返回错误时 raw_text 为空，绝不构造答案。"""
    provider = FakeProvider(
        error=ProviderError(error_code=ERROR_RATE_LIMITED, message="429")
    )
    response = await provider.answer(make_request())
    assert response.raw_text == ""
    with pytest.raises(InvalidOutputError):
        parse_and_validate(response.raw_text)
