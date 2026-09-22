# SPDX-License-Identifier: GPL-3.0-or-later
"""严格答案校验（主文档 §5.2 / §6.4）。

**红线：绝不补默认答案。**

- 拒绝：非对象、缺题、错误 ``question_id``、非法 ``value``、额外字段。
- **仅**允许剥除**单层** Markdown 代码围栏。
- **不**正则猜答案、**不**补首选项、**不**改成中立/``unsure``、**不**回落中点。

任何无效输出都必须抛出 :class:`InvalidOutputError`（错误码 ``INVALID_OUTPUT``），
交由 worker 记为无效并有限重试。
"""

from __future__ import annotations

import json
import re

from app.contracts import (
    ERROR_INVALID_OUTPUT,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    ValidatedAnswer,
)

#: 单层 Markdown 代码围栏（````` ```json ... ``` ````` 或 ``` ``` ... ``` ```）。
_SINGLE_FENCE_RE = re.compile(
    r"^\s*```[A-Za-z0-9_+\-]*\s*\n(?P<body>.*?)\n?\s*```\s*$",
    re.DOTALL,
)


class InvalidOutputError(Exception):
    """无效模型输出（错误码 ``INVALID_OUTPUT``）；**不携带任何默认答案**。"""

    code = ERROR_INVALID_OUTPUT

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # 保留原始文本便于审计，但绝不据此产出答案。
        self.raw_text = raw_text


def strip_single_fence(text: str) -> str:
    """剥除**单层** Markdown 代码围栏。

    只剥一层：若剥完后内层仍是围栏内容，交给 JSON 解析处理（必然失败 → 拒绝），
    因此不存在「递归剥围栏」或「猜答案」的路径。
    """
    match = _SINGLE_FENCE_RE.match(text)
    if match is None:
        return text
    return match.group("body")


def parse_and_validate(
    raw_text: str | None,
    *,
    question_id: str = PURCHASE_INTENT_QUESTION_ID,
    allow_reason: bool = False,
) -> ValidatedAnswer:
    """把模型原文解析为 :class:`ValidatedAnswer`；任何不合法输入都抛错。

    ``allow_reason`` 为 ``False`` 时，出现 ``reason`` 视为额外字段并拒绝。
    """
    if raw_text is None:
        raise InvalidOutputError("model returned no output")

    text = raw_text.strip()
    if not text:
        raise InvalidOutputError("model returned empty output", raw_text=raw_text)

    text = strip_single_fence(text).strip()
    if not text:
        raise InvalidOutputError("model output is empty after stripping code fence")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidOutputError(
            f"model output is not valid JSON: {exc.msg}", raw_text=raw_text
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidOutputError(
            f"model output must be a JSON object, got {type(payload).__name__}",
            raw_text=raw_text,
        )

    allowed_keys = {"question_id", "value"}
    if allow_reason:
        allowed_keys.add("reason")
    extra_keys = sorted(set(payload) - allowed_keys)
    if extra_keys:
        raise InvalidOutputError(
            f"unexpected field(s) in model output: {extra_keys}", raw_text=raw_text
        )

    if "question_id" not in payload:
        raise InvalidOutputError("model output missing required field 'question_id'")
    if payload["question_id"] != question_id:
        raise InvalidOutputError(
            f"model output has wrong question_id: {payload['question_id']!r}"
        )

    if "value" not in payload:
        raise InvalidOutputError("model output missing required field 'value'")
    value = payload["value"]
    if not isinstance(value, str) or value not in OPTION_VALUES:
        # 注意：这里**不**回落到首选项/中点/unsure，直接判无效。
        raise InvalidOutputError(f"model output has illegal value: {value!r}")

    reason_raw = payload.get("reason")
    if reason_raw is not None and not isinstance(reason_raw, str):
        raise InvalidOutputError("model output field 'reason' must be a string")

    return ValidatedAnswer(question_id=payload["question_id"], value=value, reason=reason_raw)


__all__ = ["InvalidOutputError", "parse_and_validate", "strip_single_fence"]
