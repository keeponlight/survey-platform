# SPDX-License-Identifier: GPL-3.0-or-later
"""运行时 Mock Provider（裁决 N7-b / §7.5 T6-d；主文档 §5.3 / architecture.md §5）。

**为什么需要它**：本机无真实第三方 API key（TEAM-BRIEF §4），而 ``compose.yaml`` 默认即 mock
模式。此前 mock 只存在于 ``tests/``，运行时**不可达** → ``MODEL_PROVIDER == "mock"`` 在后端
没有任何分支，compose 起来的 worker 会拿空 endpoint 发真实 HTTP 并全线失败。本模块把 mock
提升为**运行时可用的** provider。

**与 ``tests/load/mock_provider.py`` 的关系**：接口与语义**一致**——
``async def answer(request: ModelRequest) -> ModelResponse``；档位由
``stable_hash(persona_id) % 5`` 映射五档；使用同一套 ``stable_hash`` / ``valid_value_for``
定义（SHA-256，**不用**内置 ``hash()``）。差别在故障注入的**范围**：本运行时代理
**默认恒返回合法答案**（使 compose 默认链路可端到端跑通），仅支持两条**确定性**
哨兵（均只由 ``persona_id`` 前缀决定，真实用户 ID 不可能命中）：

- ``AUTH_FAULT_PERSONA_PREFIX`` → 明确 ``ERROR_AUTH``（让 ``api_auth`` 暂停链路可被
  E2E 证伪）；
- ``INVALID_FAULT_PERSONA_PREFIX`` → **非法模型原文**（``value`` 不在五档内，让
  ``INVALID_OUTPUT`` → 有限重试 → ``failed`` 路径可被 E2E 证伪，从而构造出自然的
  ``completed_with_errors`` 批次，用于核对「失败不可隐藏」与前端 normalize 兜底）。

更完整的故障矩阵（429/5xx/超时等）仍属 T7 压测脚本职责（见 ``tests/load/mock_provider.py``）。

**确定性（硬要求）**：档位等一切派生**只**由 ``persona_id`` 决定，**禁止**全局 ``random``、
**禁止**依赖调用顺序 / 并发 / 进程内计数器。故同 ``persona_id`` 在任意进程、任意
``PYTHONHASHSEED`` 下得到**相同**档位（用 SHA-256 而非内置 ``hash()``——后者受
``PYTHONHASHSEED`` 影响、跨进程不确定）。

**usage / 成本口径**：与 ``tests/load/mock_provider.py`` 的默认 ``usage_mode="present"``
保持一致（``input_tokens=32`` / ``output_tokens=8``）。**注意**：token 数是**模拟值**，
**不得**被当作真实计费对外表述；真实费用是否已知取决于是否配置了单价
（``MODEL_INPUT_PRICE_PER_MILLION`` / ``MODEL_OUTPUT_PRICE_PER_MILLION``）。未配置单价时
``compute_cost`` 返回 ``None`` ⇒ ``attempts.actual_cost`` 落 **SQL NULL（未知）**，
符合项目硬规则「``unknown`` 不得写成 ``0``」。

**红线 #2（可溯源）**：mock 输出**绝不**被表述为「真实模型模拟结果」——``provider_name``
为 ``"mock"``、``provider_request_id`` 前缀 ``mock-``、首次调用打印一条可区分 INFO 日志；
``runs.model_snapshot["provider"]`` 由 :func:`app.config.Settings.model_snapshot` 如实记录为
``mock``。

**红线 #1 边界**：本代理**仅在自身被显式调用时**产出合法答案，**不含**任何
「无效输出 → 补默认档位」逻辑（那不是它的事——有效性一律由
:func:`app.inference.validation.parse_and_validate` 判定）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.contracts import (
    ERROR_AUTH,
    ERROR_PROTOCOL,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    ModelRequest,
    ModelResponse,
    ProviderError,
)

logger = logging.getLogger("app.inference.mock")

#: 与 ``tests/load/mock_provider.py`` 默认 ``usage`` 对齐（模拟值，非真实计费）。
MOCK_INPUT_TOKENS = 32
MOCK_OUTPUT_TOKENS = 8

#: ``reason`` 字段长度上限（主文档 §5.2：不超过 100 字）。
MAX_REASON_CHARS = 100

#: 确定性**鉴权失败**哨兵前缀（触发 ``pause_reason='api_auth'`` 的 mock 路径）。
#:
#: **为什么需要**（裁决 N7-b「mock 分支必须能被测试证伪」+ T6 E2E ``test_auth_failure_hint``）：
#: 默认 mock 恒返回合法答案，**没有任何路径**能产生 401/403，故运行详情页的
#: ``pause_reason='api_auth'`` 可操作提示在 compose 默认 mock 模式下**不可达** ——
#: 这条链路既无法被 E2E 覆盖，也就无法被证伪。本哨兵使该链路**确定性可达**。
#:
#: **确定性**：只由 ``persona_id`` 前缀决定，与调用顺序/并发无关；真实用户 ID
#: 不可能以此前缀开头，故对正常数据零影响。与 ``tests/load/mock_provider.py`` 按
#: ``persona_id`` 注入故障的既有模式一致。
#:
#: **不越红线 #1**：它产出的是**明确的 provider 错误**（不产生任何答案），
#: 由 worker 映射为 ``api_auth`` 暂停，**不含**任何「无效输出 → 补默认档位」逻辑。
AUTH_FAULT_PERSONA_PREFIX = "__fault_auth__"

#: 确定性**非法输出**哨兵前缀（触发 ``INVALID_OUTPUT`` → 有限重试 → ``failed`` 的 mock 路径）。
#:
#: **为什么需要**（主文档 §6.4 红线 #1「绝不补默认答案」+ T6 §5.4 第 3/4 项）：默认 mock 恒返回
#: **合法**答案，故 ``failed`` 成员在 compose 默认 mock 模式下**不可达** ⇒ 批次永远不会收敛为
#: ``completed_with_errors``，运行详情页的「失败数 / error_summary / failed 状态原样返回 / 前端
#: normalize 兜底值」这几条**既无法验证、也无法证伪**。本哨兵使该链路**确定性可达**。
#:
#: **红线 #1（关键）**：本哨兵产出的正是**非法模型输出本身**（非「无输出」），worker 依
#: :func:`app.inference.validation.parse_and_validate` 判为 ``INVALID_OUTPUT`` 并**有限重试**，
#: **绝不**据此补默认档位 —— 即它**演示**而非绕过红线 #1。
#:
#: **确定性**：只由 ``persona_id`` 前缀决定，与调用顺序/并发无关。
INVALID_FAULT_PERSONA_PREFIX = "__fault_invalid__"


def stable_hash(value: str) -> int:
    """跨进程稳定的字符串哈希（SHA-256 前 16 个 hex）。

    **不可**用内置 ``hash()``：字符串 ``hash()`` 每个进程被 ``PYTHONHASHSEED`` 随机化，
    会破坏跨进程可复现性。
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def valid_value_for(persona_id: str) -> str:
    """由 ``persona_id`` **确定性**映射到五档之一（与 T7 mock 同定义）。"""
    return OPTION_VALUES[stable_hash(persona_id) % len(OPTION_VALUES)]


def extract_persona_id(request: ModelRequest) -> str:
    """从 ``user_message`` 反解当前 ``persona_id``（消息只含当前 persona）。

    抛 ``ValueError`` / ``KeyError`` / ``TypeError`` 表示请求负载不可解析，
    由调用方映射为 provider 协议错误（**不猜** persona）。
    """
    payload = json.loads(request.user_message)
    if not isinstance(payload, dict):
        raise TypeError("user_message is not a JSON object")
    snapshot = payload["persona_snapshot"]
    if not isinstance(snapshot, dict):
        raise TypeError("persona_snapshot is not a JSON object")
    return str(snapshot["persona_id"])


def _schema_allows_reason(output_schema: Any) -> bool:
    """``output_schema`` 是否允许 ``reason`` 字段（决定是否输出理由）。"""
    if not isinstance(output_schema, dict):
        return False
    properties = output_schema.get("properties")
    if not isinstance(properties, dict):
        return False
    return "reason" in properties


def fault_error_for(persona_id: str) -> ProviderError | None:
    """确定性故障判定：``persona_id`` 以 :data:`AUTH_FAULT_PERSONA_PREFIX` 开头 → 鉴权错误。

    返回 ``None`` 表示**无故障**（走正常合法答案路径）。故障码为 :data:`ERROR_AUTH`，
    由 worker 映射为 ``pause_reason='api_auth'``（主文档 §6.4）。**绝不**产出答案。
    """
    if persona_id.startswith(AUTH_FAULT_PERSONA_PREFIX):
        return ProviderError(
            error_code=ERROR_AUTH,
            message="mock auth failure (deterministic persona-id sentinel)",
            http_status=401,
        )
    return None


def invalid_output_for(persona_id: str) -> str | None:
    """确定性**非法输出**判定：``persona_id`` 以 :data:`INVALID_FAULT_PERSONA_PREFIX` 开头。

    命中时返回一段**非法**的模型原文（``value`` 不在五档 ``OPTION_VALUES`` 内），使
    :func:`app.inference.validation.parse_and_validate` 抛 ``InvalidOutputError`` ⇒ worker 记
    ``INVALID_OUTPUT`` 并有限重试 / 终判 ``failed``。返回 ``None`` 表示无此故障（走正常合法
    答案路径）。

    **红线 #1**：本函数**只**产出非法原文，**不含**任何「无效输出 → 补默认档位」逻辑 ——
    有效性判定与「绝不补默认答案」一律由校验层与 worker 负责。
    """
    if persona_id.startswith(INVALID_FAULT_PERSONA_PREFIX):
        return json.dumps(
            {
                "question_id": PURCHASE_INTENT_QUESTION_ID,
                "value": "__invalid_option__",
            },
            ensure_ascii=False,
        )
    return None


class MockProvider:
    """运行时可用的确定性 mock provider。

    - 实现与真实 :class:`~app.inference.provider.ProviderAdapter` **相同**的
      ``async def answer(request: ModelRequest) -> ModelResponse``。
    - 档位仅由 ``persona_id`` 派生；**不**使用全局 ``random``、**不**依赖调用顺序/并发。
    - **默认恒返回合法答案**；仅当 ``persona_id`` 命中确定性哨兵时例外：
      :func:`fault_error_for`（前缀 :data:`AUTH_FAULT_PERSONA_PREFIX`）→ 返回 provider 错误，
      使 ``api_auth`` 暂停链路可被 E2E 证伪；:func:`invalid_output_for`
      （前缀 :data:`INVALID_FAULT_PERSONA_PREFIX`）→ 返回**非法原文**，使 ``INVALID_OUTPUT``
      → ``failed`` / ``completed_with_errors`` 链路可被 E2E 证伪。
    """

    provider_name = "mock"

    def __init__(self, *, model: str = "mock-model") -> None:
        self.model = model
        self._logged_once = False

    async def answer(self, request: ModelRequest) -> ModelResponse:
        """执行一次 mock 调用。

        - 默认：恒返回**合法**的五档答案。
        - 命中 :func:`fault_error_for` 哨兵 → 返回明确的 provider 错误
          （如 ``ERROR_AUTH`` → ``api_auth`` 暂停），**不产出任何答案**。
        - 命中 :func:`invalid_output_for` 哨兵 → 返回**非法原文**（``value`` 不在五档内），
          由校验层判 ``INVALID_OUTPUT``，**绝不补默认答案**。
        """
        try:
            persona_id = extract_persona_id(request)
        except (ValueError, KeyError, TypeError) as exc:
            # 请求负载不可解析 → 映射为协议错误（与真实 provider 的 4xx 语义一致），
            # **不**猜 persona、**不**补默认答案。
            return ModelResponse(
                raw_text="",
                duration_ms=0,
                error=ProviderError(
                    error_code=ERROR_PROTOCOL,
                    message=f"mock cannot parse request persona: {exc}",
                ),
            )

        fault = fault_error_for(persona_id)
        if fault is not None:
            self._log_fault(persona_id, fault.error_code)
            return ModelResponse(raw_text="", duration_ms=0, error=fault)

        invalid_raw = invalid_output_for(persona_id)
        if invalid_raw is not None:
            self._log_invalid(persona_id)
            return ModelResponse(
                raw_text=invalid_raw,
                input_tokens=MOCK_INPUT_TOKENS,
                output_tokens=MOCK_OUTPUT_TOKENS,
                provider_request_id=f"mock-invalid-{persona_id}",
                finish_reason="stop",
                duration_ms=0,
            )

        value = valid_value_for(persona_id)
        payload: dict[str, str] = {
            "question_id": PURCHASE_INTENT_QUESTION_ID,
            "value": value,
        }
        if _schema_allows_reason(request.output_schema):
            payload["reason"] = self._reason_for(persona_id)

        self._log_call(persona_id)

        return ModelResponse(
            raw_text=json.dumps(payload, ensure_ascii=False),
            input_tokens=MOCK_INPUT_TOKENS,
            output_tokens=MOCK_OUTPUT_TOKENS,
            provider_request_id=f"mock-{persona_id}",
            finish_reason="stop",
            duration_ms=0,
        )

    @staticmethod
    def _reason_for(persona_id: str) -> str:
        """确定性、≤100 字的短理由（仅当 schema 允许 ``reason`` 时使用）。"""
        text = f"模拟依据：persona {persona_id} 的确定性档位。"
        return text[:MAX_REASON_CHARS]

    def _log_call(self, persona_id: str) -> None:
        """首次调用打印一条**可区分**的 INFO 日志（红线 #2 可溯源），其后降为 DEBUG。"""
        if not self._logged_once:
            self._logged_once = True
            logger.info(
                "mock provider serving responses (NOT a real model call): "
                "provider=%s model=%s first_persona=%s",
                self.provider_name,
                self.model,
                persona_id,
            )
        else:
            logger.debug("mock response for persona %s", persona_id)

    @staticmethod
    def _log_fault(persona_id: str, error_code: str) -> None:
        """记录一次确定性故障（可溯源：便于区分「mock 注入故障」与「真实供应商错误」）。"""
        logger.info(
            "mock injected deterministic fault: persona=%s error_code=%s (NOT a real call)",
            persona_id,
            error_code,
        )

    @staticmethod
    def _log_invalid(persona_id: str) -> None:
        """记录一次确定性非法输出（可溯源：便于区分「mock 注入非法输出」与「真实供应商异常」）。"""
        logger.info(
            "mock injected deterministic invalid output: persona=%s (NOT a real call)",
            persona_id,
        )


__all__ = [
    "AUTH_FAULT_PERSONA_PREFIX",
    "INVALID_FAULT_PERSONA_PREFIX",
    "MAX_REASON_CHARS",
    "MOCK_INPUT_TOKENS",
    "MOCK_OUTPUT_TOKENS",
    "MockProvider",
    "extract_persona_id",
    "fault_error_for",
    "invalid_output_for",
    "stable_hash",
    "valid_value_for",
]
