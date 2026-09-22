"""启动前能力检查（主文档 §5.3）——一次性脚本，用项目自身适配器发真实请求。

规则（红线 #3）：
- 密钥**只**从受控文件 ``deploy/real.env`` 载入进程环境（与生产 compose 的 env_file 一致）；
  本脚本**不含**任何密钥字面量。
- 所有记录的请求/响应都把 ``Authorization`` 头脱敏为 ``Bearer <redacted>``；
  并对输出做一次全局红线兜底（把密钥本体替换为 ``<redacted-key>``）。
- 不打印、不落盘任何未脱敏的密钥。

用法：``cd backend && uv run python scripts/capability_check.py``
总真实请求数受 ``--max-requests`` 约束（默认 3，硬上限 6）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

# 让 ``import app.*`` 在直接运行时可用
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
    PersonaSnapshot,
    ProductInput,
    QuestionInput,
    SurveyInput,
)
from app.inference.prompt import build_model_request  # noqa: E402
from app.inference.provider import build_provider  # noqa: E402
from app.inference.validation import InvalidOutputError, parse_and_validate  # noqa: E402

PROJECT_ROOT = _BACKEND_DIR.parent
REAL_ENV_PATH = PROJECT_ROOT / "deploy" / "real.env"
HARD_MAX_REQUESTS = 6

# 故意错 key（明显非真实格式，仅用于验证 401 分类）
WRONG_KEY = "sk-wrong-key-capability-check-0000"
# 故意不存在的模型名（用于验证模型名校验路径）
MISSING_MODEL = "deepseek-does-not-exist-xyz-2026"


def load_env_file(path: Path) -> dict[str, str]:
    """极简 KEY=VALUE 解析（足够解析本项目 env 模板；忽略注释与空行）。"""
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """脱敏：任何携带凭据的头一律替换为占位符。"""
    masked = dict(headers)
    for name in list(masked):
        if name.lower() in {"authorization", "x-api-key", "api-key"}:
            masked[name] = "Bearer <redacted>"
    return masked


def redact(text: str, secrets: list[str]) -> str:
    """把输出里任何残留的密钥本体替换掉（红线兜底）。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted-key>")
    return text


class Recorder:
    """记录器：把请求/响应（脱敏后）追加进 ``events``。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def on_request(self, request: httpx.Request) -> None:
        body: Any
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except Exception as exc:  # pragma: no cover - 仅防御
            body = f"<unparsed request body: {exc}>"
        self.events.append(
            {
                "phase": "request",
                "method": request.method,
                "url": str(request.url),
                "headers": mask_headers(dict(request.headers)),
                "body": body,
            }
        )

    async def on_response(self, response: httpx.Response) -> None:
        try:
            await response.aread()
        except Exception:  # pragma: no cover - 仅防御
            pass
        try:
            body_text: str = response.text
        except Exception as exc:  # pragma: no cover - 仅防御
            body_text = f"<unreadable response body: {exc}>"
        self.events.append(
            {
                "phase": "response",
                "status": response.status_code,
                "headers": dict(response.headers),
                "body_text": body_text,
            }
        )


async def run_one(
    label: str,
    provider: Any,
    request: Any,
    recorder: Recorder,
) -> dict[str, Any]:
    """执行一次调用并收集证据（不抛异常，错误经返回值表达）。"""
    before = len(recorder.events)
    result: dict[str, Any] = {"label": label, "request_model": request.model}
    try:
        response = await provider.answer(request)
    except Exception as exc:  # 适配器本应不抛业务异常，捕获以防万一
        result["adapter_raised"] = f"{type(exc).__name__}: {exc}"
        result["events"] = recorder.events[before:]
        return result

    result["response"] = response.model_dump()
    err = response.error
    result["error_code"] = err.error_code if err else None
    result["http_status"] = err.http_status if err else None
    result["retry_after_seconds"] = err.retry_after_seconds if err else None
    result["events"] = recorder.events[before:]

    if err is None:
        # 严格校验器（真实 provider 路径的必备环节）
        try:
            validated = parse_and_validate(response.raw_text)
            result["validator"] = {
                "ok": True,
                "validated": validated.model_dump(),
            }
        except InvalidOutputError as exc:
            result["validator"] = {"ok": False, "error": str(exc)}
    return result


async def attach_recorder(provider: Any, recorder: Recorder) -> None:
    """给 provider 惰性创建的 httpx 客户端挂上事件钩子（不改适配器代码）。"""
    client = await provider._get_client()  # noqa: SLF001 - 只读挂钩子，不改行为
    client.event_hooks = {
        "request": [recorder.on_request],
        "response": [recorder.on_response],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=3)
    parser.add_argument("--out", type=str, default="", help="将脱敏后的证据 JSON 写入该路径")
    args = parser.parse_args()
    if args.max_requests > HARD_MAX_REQUESTS:
        print(f"refuse: max_requests>{HARD_MAX_REQUESTS}")
        return 2

    file_env = load_env_file(REAL_ENV_PATH)
    # 与生产一致：把 env 文件注入进程环境（config.get_settings/api_key 读 os.environ）
    os.environ.update(file_env)
    real_key = os.environ.get(file_env.get("MODEL_API_KEY_ENV", "SURVEY_MODEL_API_KEY"), "")
    secrets = [real_key]

    settings = Settings.from_env()
    report: dict[str, Any] = {
        "env_file": str(REAL_ENV_PATH.relative_to(PROJECT_ROOT)),
        "settings": {
            "provider": settings.model_provider,
            "model": settings.model_name,
            "endpoint": settings.model_endpoint,
            "model_config_id": settings.model_config_id,
            "api_key_env": settings.model_api_key_env,
            "api_key_present": bool(settings.api_key()),
            "api_key_len": len(settings.api_key() or ""),
            "timeout_seconds": settings.model_timeout_seconds,
            "max_output_tokens": settings.model_max_output_tokens,
            "rpm": settings.model_rpm,
            "tpm": settings.model_tpm,
            "agent_concurrency": settings.agent_concurrency,
        },
        "provider_class": type(build_provider(settings)).__name__,
        "is_model_configured": settings.is_model_configured(),
        "requests": [],
    }

    # 构造最小问卷 + persona（output_schema 非空 ⇒ 适配器带 response_format=json_object）
    survey = SurveyInput(
        title="启动前能力检查",
        product=ProductInput(
            name="示例智能音箱",
            description="用于能力检查的最小产品描述。",
            price=Decimal("299"),
            price_unit="台",
        ),
        question=QuestionInput(),
    )
    persona = PersonaSnapshot(
        row_no=1,
        persona_id="capability-check-persona-1",
        profile_text="30 岁一线城市白领，注重性价比，家中已有一台旧音箱。",
    )
    base_request = build_model_request(
        survey=survey,
        persona=persona,
        model=settings.model_name,
        provider=settings.model_provider,
        max_output_tokens=settings.model_max_output_tokens,
        timeout_seconds=settings.model_timeout_seconds,
    )
    report["base_request"] = {
        "model": base_request.model,
        "output_schema_present": bool(base_request.output_schema),
        "max_output_tokens": base_request.max_output_tokens,
        "timeout_seconds": base_request.timeout_seconds,
    }

    counter = {"n": 0}

    # ---- 检查 1：正常鉴权 + 模型名有效性 + 响应结构 + JSON 模式 + usage ----
    if counter["n"] < args.max_requests:
        counter["n"] += 1
        recorder = Recorder()
        provider = build_provider(settings)
        await attach_recorder(provider, recorder)
        result = await run_one("A_normal_deepseek_flash", provider, base_request, recorder)
        await provider.aclose()
        report["requests"].append(result)

    # ---- 检查 2：故意错 key → 期望 401/AUTH ----
    if counter["n"] < args.max_requests:
        counter["n"] += 1
        recorder = Recorder()
        os.environ[settings.model_api_key_env] = WRONG_KEY
        provider_bad = build_provider(settings)
        await attach_recorder(provider_bad, recorder)
        result = await run_one("B_wrong_key", provider_bad, base_request, recorder)
        await provider_bad.aclose()
        os.environ[settings.model_api_key_env] = real_key
        report["requests"].append(result)

    # ---- 检查 3：故意不存在的模型名 → 记录状态码与分类 ----
    if counter["n"] < args.max_requests:
        counter["n"] += 1
        recorder = Recorder()
        provider = build_provider(settings)
        await attach_recorder(provider, recorder)
        missing_request = base_request.model_copy(update={"model": MISSING_MODEL})
        result = await run_one("C_missing_model", provider, missing_request, recorder)
        await provider.aclose()
        report["requests"].append(result)

    report["real_request_count"] = counter["n"]

    # ---- 汇总 token usage ----
    total_in = 0
    total_out = 0
    usage_readable = False
    for req in report["requests"]:
        resp = req.get("response") or {}
        if resp.get("input_tokens") is not None or resp.get("output_tokens") is not None:
            usage_readable = True
        total_in += resp.get("input_tokens") or 0
        total_out += resp.get("output_tokens") or 0
    report["usage_summary"] = {
        "usage_readable": usage_readable,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
    }

    payload = redact(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False), secrets)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
