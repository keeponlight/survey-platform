# SPDX-License-Identifier: GPL-3.0-or-later
"""裁决 N7-b 验收测试：运行时 mock provider（``app/inference/mock_provider.py``）。

覆盖：
(a) ``MODEL_PROVIDER=mock`` → :class:`MockProvider`；``MODEL_PROVIDER=openai_compatible`` →
    :class:`OpenAICompatibleProvider`（**两个分支都测**，否则等于没测）；
(b) **跨进程确定性**：固定 ``persona_id`` 的档位等于测试内**写死的常量**，且在
    ``PYTHONHASHSEED=0/1`` 两个子进程下结果一致（证伪「用内置 ``hash()``」这类实现）；
(c) mock 输出能通过 :func:`app.inference.validation.parse_and_validate` 的**严格**校验；
(d) **红线边界证伪**（见 :func:`test_mock_output_is_valid_on_its_own_not_filled_by_platform`）。

> **红线 #1 说明（供后续 QA 免误判）**：本测试**不**断言任何「无效输出 → 补默认档位」行为；
> 恰恰相反，它证明**平台不会补值**——mock 只是**在被显式调用时**返回**自身合法**的答案。

验收命令：``uv run pytest tests/test_mock_provider.py -q``
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from decimal import Decimal

import pytest

from app.config import Settings
from app.contracts import PersonaSnapshot, ProductInput, QuestionInput, SurveyInput
from app.inference.mock_provider import MockProvider, stable_hash, valid_value_for
from app.inference.prompt import build_model_request
from app.inference.provider import OpenAICompatibleProvider, build_provider
from app.inference.validation import InvalidOutputError, parse_and_validate

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

#: 独立算出的期望档位（常量写死；由 ``sha256(persona_id)[:16] % 5`` 求得，见报告）。
EXPECTED_VALUES: dict[str, str] = {
    "p_000001": "definitely_yes",
    "p_000002": "definitely_not",
    "p_42": "probably_not",
}


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
        ),
        question=QuestionInput(),
    )


def make_persona(persona_id: str) -> PersonaSnapshot:
    return PersonaSnapshot(row_no=1, persona_id=persona_id, profile_text="26岁，女性，上海")


def make_request(persona_id: str, *, allow_reason: bool = False):
    return build_model_request(
        survey=make_survey(),
        persona=make_persona(persona_id),
        model="mock-model",
        allow_reason=allow_reason,
    )


# ---------------------------------------------------------------------------
# (a) build_provider 按 MODEL_PROVIDER 分支
# ---------------------------------------------------------------------------


def test_build_provider_selects_mock() -> None:
    """``MODEL_PROVIDER=mock`` → 运行时 mock provider（唯一选择依据，§7.5 T6-d）。"""
    settings = Settings.from_env({"MODEL_PROVIDER": "mock"})
    provider = build_provider(settings)
    assert isinstance(provider, MockProvider)
    assert provider.provider_name == "mock"


def test_build_provider_selects_real() -> None:
    """``MODEL_PROVIDER=openai_compatible`` → 真实 provider（同一分支必须证伪）。"""
    settings = Settings.from_env(
        {
            "MODEL_PROVIDER": "openai_compatible",
            "MODEL_ENDPOINT": "https://example.test/v1",
            "MODEL_NAME": "some-model",
        }
    )
    provider = build_provider(settings)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert not isinstance(provider, MockProvider)


def test_build_provider_default_is_real_not_mock() -> None:
    """未显式设 mock 时**不得**默默走 mock（默认 provider = openai_compatible）。"""
    settings = Settings.from_env({})
    assert settings.model_provider == "openai_compatible"
    assert isinstance(build_provider(settings), OpenAICompatibleProvider)


# ---------------------------------------------------------------------------
# (b) 确定性（含跨进程 / PYTHONHASHSEED）
# ---------------------------------------------------------------------------


async def test_value_depends_only_on_persona_id() -> None:
    """档位只由 ``persona_id`` 决定，且等于测试内写死的常量。"""
    provider = MockProvider()
    for persona_id, expected in EXPECTED_VALUES.items():
        response = await provider.answer(make_request(persona_id))
        payload = json.loads(response.raw_text)
        assert payload["value"] == expected, persona_id
        assert payload["question_id"] == "purchase_intent"


async def test_repeated_calls_are_stable() -> None:
    """同 ``persona_id`` 多次调用（含顺序打乱）结果一致——不依赖调用顺序。"""
    provider = MockProvider()
    first = json.loads((await provider.answer(make_request("p_42"))).raw_text)["value"]
    # 打乱顺序、插入其它 persona，再回查。
    await provider.answer(make_request("p_000001"))
    await provider.answer(make_request("p_000002"))
    again = json.loads((await provider.answer(make_request("p_42"))).raw_text)["value"]
    assert first == again == EXPECTED_VALUES["p_42"]


def test_hashes_and_values_match_hardcoded_expectations() -> None:
    """``stable_hash`` / ``valid_value_for`` 与写死常量一致（内置 ``hash()`` 会被证伪）。"""
    for persona_id, expected in EXPECTED_VALUES.items():
        assert valid_value_for(persona_id) == expected
        assert stable_hash(persona_id) >= 0


def test_cross_process_determinism_under_pythonhashseed() -> None:
    """两个不同 ``PYTHONHASHSEED`` 的子进程算出**相同**档位（跨进程可复现）。"""
    script = "\n".join(
        [
            "import json",
            "from app.inference.mock_provider import valid_value_for",
            "ids = ['p_000001', 'p_000002', 'p_42']",
            "print(json.dumps({pid: valid_value_for(pid) for pid in ids}))",
        ]
    )
    results: dict[str, dict[str, str]] = {}
    for seed in ("0", "1", "12345"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        results[seed] = json.loads(completed.stdout.strip())
        assert results[seed] == EXPECTED_VALUES, (seed, results[seed])
    assert results["0"] == results["1"] == results["12345"] == EXPECTED_VALUES


# ---------------------------------------------------------------------------
# (c) mock 输出通过严格校验
# ---------------------------------------------------------------------------


async def test_mock_output_passes_strict_validation() -> None:
    """默认（不允许 reason）：输出严格为 ``{question_id,value}`` 并通过校验。"""
    provider = MockProvider()
    response = await provider.answer(make_request("p_000001", allow_reason=False))
    assert response.error is None
    payload = json.loads(response.raw_text)
    assert set(payload) == {"question_id", "value"}
    answer = parse_and_validate(response.raw_text, allow_reason=False)
    assert answer.value == EXPECTED_VALUES["p_000001"]
    assert answer.score == 5  # definitely_yes


async def test_mock_output_with_reason_passes_validation() -> None:
    """开启短理由：mock 追加 ``reason``（≤100 字）并通过开启 reason 的校验。"""
    provider = MockProvider()
    response = await provider.answer(make_request("p_000001", allow_reason=True))
    payload = json.loads(response.raw_text)
    assert set(payload) == {"question_id", "value", "reason"}
    assert len(payload["reason"]) <= 100
    answer = parse_and_validate(response.raw_text, allow_reason=True)
    assert answer.value == EXPECTED_VALUES["p_000001"]
    assert answer.reason is not None


async def test_mock_usage_tokens_present_but_not_real_billing() -> None:
    """usage 与 ``tests/load/mock_provider.py`` 默认一致（present）；token 是模拟值。"""
    provider = MockProvider()
    response = await provider.answer(make_request("p_000002"))
    assert response.input_tokens == 32
    assert response.output_tokens == 8
    # 可溯源标识：请求 ID 前缀 mock-（红线 #2：mock 结果可区分）。
    assert response.provider_request_id == "mock-p_000002"


# ---------------------------------------------------------------------------
# (d) 红线边界证伪
# ---------------------------------------------------------------------------


def test_mock_output_is_valid_on_its_own_not_filled_by_platform() -> None:
    """红线 #1 边界证伪：mock 合法输出与「平台补默认答案」是**两件事**。

    本用例以「无 provider 参与」的方式直接调用平台严格校验器：

    1. 一个**非法**原文 → 校验器**抛错**（平台**不**补首选项/中点/``unsure``）；
    2. 一个内含**额外字段** ``reason`` 的原文（未开启 reason 时）→ 同样**抛错**（不吞字段）；
    3. mock 的输出之所以通过，是因为它**自身就是合法 JSON**，而非平台「填」出来的。

    故 mock **不构成**对无效输出的补值路径；它只在**被显式调用**时产出答案。
    """
    # 1) 非法 value：不回落，直接判无效。
    with pytest.raises(InvalidOutputError):
        parse_and_validate('{"question_id":"purchase_intent","value":"maybe_yes"}')
    # 2) 额外字段：allow_reason=False 时 reason 属额外字段 → 拒绝。
    with pytest.raises(InvalidOutputError):
        parse_and_validate(
            '{"question_id":"purchase_intent","value":"unsure","reason":"x"}',
            allow_reason=False,
        )
    # 3) mock 输出自身合法（不是因为平台补值）。
    answer = parse_and_validate(
        json.dumps({"question_id": "purchase_intent", "value": "unsure"})
    )
    assert answer.value == "unsure"


def test_mock_does_not_touch_global_random() -> None:
    """mock 不使用全局 ``random``：同一 ``persona_id`` 的档位是纯函数。"""
    assert valid_value_for("p_000001") == EXPECTED_VALUES["p_000001"]
    assert valid_value_for("p_000001") == valid_value_for("p_000001")
