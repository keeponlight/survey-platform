# SPDX-License-Identifier: GPL-3.0-or-later
"""环境配置与无秘密模型配置（主文档 §5.3 / §9）。

设计要点：
- 全部配置来自**后端环境变量**（或受控文件），不从前端/问卷输入读取。
- ``API key`` 通过**环境变量名**间接引用；快照只保存「key 的环境变量名 + 配置 hash」，
  **绝不**保存密钥本身（红线：密钥不进日志/快照/代码）。
- 金额用 :class:`decimal.Decimal`；``unknown`` 不写成 ``0``。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# 默认值（本机已就绪，见 TEAM-BRIEF §3 / §7 Q5）
# ---------------------------------------------------------------------------

DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey"
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test"

DEFAULT_AGENT_CONCURRENCY = 100
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_TOKENS = 256
DEFAULT_RPM = 3000
DEFAULT_TPM = 3_000_000
DEFAULT_BUDGET_CURRENCY = "CNY"
DEFAULT_PROVIDER = "openai_compatible"
DEFAULT_MODEL_NAME = "mock-model"
DEFAULT_MODEL_CONFIG_ID = "default"


def _env_str(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    return default if value is None or value == "" else value


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {key} must be an integer, got {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {key} must be a number, got {raw!r}") from exc


def _env_decimal(env: Mapping[str, str], key: str) -> Decimal | None:
    raw = env.get(key)
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"environment variable {key} must be a decimal, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """进程级配置快照（不可变）。"""

    database_url: str = DEFAULT_DATABASE_URL
    test_database_url: str = DEFAULT_TEST_DATABASE_URL

    agent_concurrency: int = DEFAULT_AGENT_CONCURRENCY

    model_config_id: str = DEFAULT_MODEL_CONFIG_ID
    model_provider: str = DEFAULT_PROVIDER
    model_name: str = DEFAULT_MODEL_NAME
    model_endpoint: str = ""
    #: 存放 API key 的**环境变量名**（不是密钥本身）。
    model_api_key_env: str = "SURVEY_MODEL_API_KEY"
    model_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    model_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    model_rpm: int = DEFAULT_RPM
    model_tpm: int = DEFAULT_TPM
    input_price_per_million: Decimal | None = None
    output_price_per_million: Decimal | None = None
    budget_currency: str = DEFAULT_BUDGET_CURRENCY

    #: 供测试覆盖的额外环境（通常为空 dict）。
    extra: Mapping[str, str] = field(default_factory=dict)

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        environ: Mapping[str, str] = os.environ if env is None else env
        return cls(
            database_url=_env_str(environ, "DATABASE_URL", DEFAULT_DATABASE_URL),
            test_database_url=_env_str(
                environ, "TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL
            ),
            agent_concurrency=_env_int(
                environ, "AGENT_CONCURRENCY", DEFAULT_AGENT_CONCURRENCY
            ),
            model_config_id=_env_str(environ, "MODEL_CONFIG_ID", DEFAULT_MODEL_CONFIG_ID),
            model_provider=_env_str(environ, "MODEL_PROVIDER", DEFAULT_PROVIDER),
            model_name=_env_str(environ, "MODEL_NAME", DEFAULT_MODEL_NAME),
            model_endpoint=_env_str(environ, "MODEL_ENDPOINT", ""),
            model_api_key_env=_env_str(
                environ, "MODEL_API_KEY_ENV", "SURVEY_MODEL_API_KEY"
            ),
            model_timeout_seconds=_env_float(
                environ, "MODEL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
            ),
            model_max_output_tokens=_env_int(
                environ, "MODEL_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS
            ),
            model_rpm=_env_int(environ, "MODEL_RPM", DEFAULT_RPM),
            model_tpm=_env_int(environ, "MODEL_TPM", DEFAULT_TPM),
            input_price_per_million=_env_decimal(environ, "MODEL_INPUT_PRICE_PER_MILLION"),
            output_price_per_million=_env_decimal(
                environ, "MODEL_OUTPUT_PRICE_PER_MILLION"
            ),
            budget_currency=_env_str(environ, "BUDGET_CURRENCY", DEFAULT_BUDGET_CURRENCY),
        )

    # -- 派生 -------------------------------------------------------------

    def api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """从环境读取密钥（只在此处读取，绝不落库/落日志）。"""
        environ: Mapping[str, str] = os.environ if env is None else env
        value = environ.get(self.model_api_key_env)
        return value or None

    def is_model_configured(self) -> bool:
        """模型配置是否可用（``start`` 前校验；``preview`` 可不需要）。"""
        return bool(self.model_endpoint and self.model_name)

    def _no_secret_params(self) -> dict[str, Any]:
        """不含秘密的模型参数（用于 model_snapshot / 配置 hash）。"""
        return {
            "model_config_id": self.model_config_id,
            "provider": self.model_provider,
            "model": self.model_name,
            "endpoint": self.model_endpoint,
            "api_key_env": self.model_api_key_env,
            "timeout_seconds": self.model_timeout_seconds,
            "max_output_tokens": self.model_max_output_tokens,
            "rpm": self.model_rpm,
            "tpm": self.model_tpm,
            "input_price_per_million": (
                str(self.input_price_per_million)
                if self.input_price_per_million is not None
                else None
            ),
            "output_price_per_million": (
                str(self.output_price_per_million)
                if self.output_price_per_million is not None
                else None
            ),
            "budget_currency": self.budget_currency,
        }

    def model_config_hash(self) -> str:
        """无秘密模型配置的确定性 hash（供 run.model_snapshot 记录）。"""
        payload = json.dumps(self._no_secret_params(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def model_snapshot(self) -> dict[str, Any]:
        """写入 ``runs.model_snapshot`` 的**不含密钥**快照。"""
        snapshot = self._no_secret_params()
        snapshot["config_hash"] = self.model_config_hash()
        return snapshot


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程级单例（可用 :func:`set_settings` 覆盖，便于测试）。"""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """覆盖/重置全局配置（测试用）。"""
    global _settings
    _settings = settings


__all__ = [
    "DEFAULT_AGENT_CONCURRENCY",
    "DEFAULT_DATABASE_URL",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TEST_DATABASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "Settings",
    "get_settings",
    "set_settings",
]
