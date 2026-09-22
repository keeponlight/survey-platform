# SPDX-License-Identifier: GPL-3.0-or-later
"""inference 包：提示词模板 / 第三方适配 / 严格校验（主文档 §5）。"""

from app.inference.mock_provider import (
    MockProvider,
    extract_persona_id,
    stable_hash,
    valid_value_for,
)
from app.inference.prompt import (
    SYSTEM_INSTRUCTION,
    PromptBundle,
    build_model_request,
    build_output_schema,
    build_prompt,
    build_user_message,
    prompt_hash,
)
from app.inference.provider import (
    OpenAICompatibleProvider,
    ProviderAdapter,
    build_provider,
    classify_http_status,
    parse_retry_after,
)
from app.inference.validation import InvalidOutputError, parse_and_validate, strip_single_fence

__all__ = [
    "SYSTEM_INSTRUCTION",
    "InvalidOutputError",
    "MockProvider",
    "OpenAICompatibleProvider",
    "PromptBundle",
    "ProviderAdapter",
    "build_model_request",
    "build_output_schema",
    "build_prompt",
    "build_provider",
    "build_user_message",
    "classify_http_status",
    "extract_persona_id",
    "parse_and_validate",
    "parse_retry_after",
    "prompt_hash",
    "stable_hash",
    "strip_single_fence",
    "valid_value_for",
]
