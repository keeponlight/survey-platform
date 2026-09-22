#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""真实第三方 API 50 人全流程 + 网络/并发全链路插桩（任务 #16）。

**这一步要证明什么（用户核心诉求）**
1. 用**真实** provider（``deploy/real.env``：``openai_compatible`` + DeepSeek 官方 endpoint）跑通
   50 人全流程：``import → preview → create run（带 Idempotency-Key）→ start → worker 收敛 →
   results / summary / export.csv``；每步记录状态与耗时；**preview 不得调用模型**（有断言）。
2. **网络插桩**：逐请求明细（persona_id、attempt_no、URL（脱敏）、HTTP 状态、耗时、是否重试、
   Retry-After、usage tokens、finish_reason、错误分类）+ 汇总（成功率、429/5xx/超时/重试、
   延迟 p50/p95/p99/max、token 总量、实测 RPM/TPM vs 配置、HTTP 连接数）。
3. **并发插桩**：运行期间**双路采样**（① ``ConcurrencyLimiter`` 内部在途计数；② DB 侧
   ``run_members.status='running'`` 与 ``attempts.status='running'`` 行数），落盘时间序列，
   记录峰值、是否恒 ≤ 上限、滚动补位证据。
4. **两轮对比**：第 1 轮全 50 行（``AGENT_CONCURRENCY=50``）；第 2 轮 **10 行切片**
   （``AGENT_CONCURRENCY=8``），证明限流器真的收紧且峰值 ≤ 8。两轮用**独立 run**。

**红线（本脚本遵守）**
- 红线 #1：脚本**不产出任何默认答案**；无效输出一律 ``INVALID_OUTPUT`` → 重试 → 失败。
  本脚本只做「观测包装」，不改任何业务逻辑。
- 红线 #2：本脚本只跑 **50 人**（+10 行切片），**不得**表述为「万级真实模拟已完成」。
- 红线 #3：密钥**只**从 ``deploy/real.env`` 读取并注入**进程环境**；**绝不**写进任何产出物。
  一切输出文本里的 ``Authorization`` 头与密钥子串一律脱敏为 ``Bearer <redacted>``。

**插桩不改业务代码**：per-request 归因用 ``contextvars`` 传递当前 ``(persona_id, attempt_no)``
（在 :class:`InstrumentedExecutor` 子类里 set/reset），由 :class:`RecordingProvider` 与 httpx
事件钩子读取；``ConcurrencyLimiter`` 直接复用 worker 自己的实例，不另造计数器。

**未验证项**（如实声明，见 ``docs/real-run-50.md``）：``MODEL_RPM/TPM`` 为客户给出的基线，未经
供应商配额接口核实；无单价 → 费用未知（不估具体金额）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import csv
import io
import json
import os
import pathlib
import re
import sys
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# sys.path：backend/（app.*）与 tests/load/（run_10000 顶层模块）。
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve()
PROJECT_ROOT = _HERE.parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
LOAD_DIR = PROJECT_ROOT / "tests" / "load"
RESULTS_DIR = LOAD_DIR / "results"
FIXTURES_DIR = LOAD_DIR / "fixtures"
for _path in (str(BACKEND_DIR), str(LOAD_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 说明：以下 import 必须位于 sys.path 注入之后，故整体加 E402；导入分组顺序不与 isort
# 强求一致（I001），因为顺序受「先注入 sys.path」这一硬约束支配。
import httpx  # noqa: E402, I001
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from run_10000 import (  # noqa: E402
    ConnectionSampler,
    _percentile,
    _run_alembic_upgrade,
    collect_invariants,
    db_size_pretty,
    invariant_checks,
    member_status_counts,
    peak_rss_mb,
    succeeded_answers,
)
from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
    ERROR_AUTH,
    ERROR_PROTOCOL,
    ERROR_RATE_LIMITED,
    ERROR_SERVER_ERROR,
    ERROR_TIMEOUT,
)
from app.inference.provider import OpenAICompatibleProvider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.runs.repository import run_repository  # noqa: E402
from app.worker.execute import AttemptExecutor, RetryPolicy  # noqa: E402
from app.worker.limits import ConcurrencyLimiter, SmoothRateLimiter  # noqa: E402
from app.worker.main import Worker  # noqa: E402

# ---------------------------------------------------------------------------
# 常量：输入口径（用户已定）
# ---------------------------------------------------------------------------

#: 样本文件（只读；**不写回 Desktop**）。
SAMPLE_CSV = pathlib.Path("/Users/zhao/Desktop/User_360_Analysis_sample50.csv")

#: persona 唯一 ID 列（用户指定）。
PERSONA_ID_COLUMN = "user_id"

#: **L1 画像列**（用户口径：只取 L1，`age_band` … `brand_affinity` 一带，共 25 列）。
#: 已排除：元数据/标识（`_sample_group`/`_sample_note`/`respondent_id`/`data_source`/
#: `has_l2_survey`/`has_l3_review`/`has_l2_behavior`）、全部 `Q1_*`…`Q8_*`（含
#: `Q4_purchase_intent`）、全部 L3 评论正文列（`review_*`）、L2 交易/行为列
#: （`order_*`/`beh_*`/`total_gmv_cny`…）、数据质量标记（`dq_*`）。
L1_PROFILE_COLUMNS: list[str] = [
    "age_band",
    "gender",
    "city_tier",
    "city",
    "education",
    "occupation",
    "marital_status",
    "monthly_disposable_income",
    "beauty_monthly_spend",
    "consumption_style",
    "preferred_categories",
    "price_band_pref",
    "ingredient_focus",
    "ingredient_tags",
    "purchase_channels",
    "media_platforms",
    "content_preferences",
    "kol_types",
    "big5_openness",
    "big5_conscientiousness",
    "big5_extraversion",
    "big5_agreeableness",
    "big5_neuroticism",
    "decision_style",
    "brand_affinity",
]

#: 列映射（用户已定）：persona_id + profile_text_columns（只取 L1，不做任何补全）。
COLUMN_MAPPING: dict[str, Any] = {
    "persona_id": PERSONA_ID_COLUMN,
    "profile_text_columns": L1_PROFILE_COLUMNS,
}

#: 产品设定值（用户已定）——**中性客观描述，不编造功效**。
#: ⚠️ ``price=328.00`` / ``price_unit=30ml 精华`` 是**本轮设定值**，非从库存/官方价核实。
PRODUCT: dict[str, Any] = {
    "name": "怡兰葆 白茶赤芝系列",
    "description": "怡兰葆品牌推出以白茶与赤芝为核心成分的护肤系列。",
    "price": "328.00",
    "price_unit": "30ml 精华",
    "time_range": "未来 30 天",
    "purchase_conditions": None,
}

SURVEY_TITLE = "怡兰葆 白茶赤芝系列 · 购买意向模拟（真实 API 50 人）"

#: 预算（名义软上限）：无单价 → 费用未知、预留为 0，故预算实际不绑定；见报告口径。
BUDGET_LIMIT = Decimal("50.00")
BUDGET_CURRENCY = "CNY"

#: 开发库（真实运行落库到应用库；**不占用 survey_test**）。
DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey"

#: 两轮配置（用户已定）。
ROUNDS: dict[str, dict[str, Any]] = {
    "cap50": {
        "label": "第 1 轮：全 50 行",
        "cap": 50,
        "slice_rows": None,
        "result_file": "real_50_maxtok4096.json",
    },
    "cap8": {
        "label": "第 2 轮：10 行切片",
        "cap": 8,
        "slice_rows": 10,
        "result_file": "real_50_cap8_maxtok4096.json",
    },
}

#: 并发采样间隔（秒）。
CONCURRENCY_SAMPLE_INTERVAL = 0.1

#: 重试策略：真实退避 2s / 8s（主文档 §6.4），抖动为 0 以便时序可复现。
RETRY_REAL = RetryPolicy(max_attempts=3, backoff_seconds=(2.0, 8.0), jitter_ratio=0.0)

#: 单轮 serve 超时（秒）。
SERVE_TIMEOUT = 900.0

#: 批量前输出上限：`deepseek-flash` 带思维链，`max_tokens` 会被 reasoning 吃满。
#: 历程：`real.env`=256（能力检查基线）→ §7.7 O1 上调 **2048** → 实测 3/53 首调触顶
#: `finish_reason=length`（截断率 3/53=5.7% > §10 阈值 2%）→ 团队裁定 R-1 再上调 **4096**。
#: `max_tokens` 只是上限、按实际 token 计费；仅思维链失控时成本上升，故不过度放大。
#: 本脚本在**进程环境**覆盖 ``MODEL_MAX_OUTPUT_TOKENS``（**不改** ``deploy/real.env``）。
MAX_OUTPUT_TOKENS_EFFECTIVE = 4096

#: 当前调用上下文：``(persona_id, attempt_no, attempt_limit)``。
_CURRENT_CALL: contextvars.ContextVar[tuple[str, int, int] | None] = contextvars.ContextVar(
    "current_call", default=None
)


# ---------------------------------------------------------------------------
# 脱敏（红线 #3）
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)
_SK_RE = re.compile(r"\b(sk-)[A-Za-z0-9]{4,}")


def redact(value: Any, secret: str | None) -> str:
    """把文本里的密钥本体与 ``Bearer`` 头脱敏。"""
    if value is None:
        return ""
    out = str(value)
    if secret:
        out = out.replace(secret, "<redacted>")
    out = _BEARER_RE.sub("Bearer <redacted>", out)
    out = _SK_RE.sub(r"\g<1><redacted>", out)
    return out


# ---------------------------------------------------------------------------
# env 文件加载（真实配置来源；密钥**只**存在于此文件）
# ---------------------------------------------------------------------------


def load_env_file(path: pathlib.Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` env 文件（忽略空行与 ``#`` 注释）。"""
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


# ---------------------------------------------------------------------------
# 网络插桩：httpx 事件钩子（真实 wire 证据；不改 provider 行为）
# ---------------------------------------------------------------------------


class WireProbe:
    """httpx 事件钩子：记录**真实发出**的请求 URL/状态，并按上下文归因到 persona/attempt。

    - 事件钩子与 provider 的 ``await client.post(...)`` 在**同一 asyncio 任务**内执行，
      因此 :data:`_CURRENT_CALL` 上下文在钩子里可见 → 可把 wire 事件精确归因到某次调用。
    - ``response.extensions["network_stream"]`` 是底层连接对象；对其 ``id()`` 去重即得
      **去重后的 TCP 连接数**（keep-alive 复用会命中同一对象）。属**尽力而为**证据，
      见报告「未验证项」。
    """

    def __init__(self, secret: str | None) -> None:
        self._secret = secret
        self.n_request = 0
        self.n_response = 0
        self.events: list[dict[str, Any]] = []
        self.conn_ids: set[int] = set()
        self.wire: dict[tuple[str, int], dict[str, Any]] = {}
        #: **真实发出**的请求体里出现过的 ``max_tokens`` 取值集合（证明 §7.7 O1 覆盖确已生效）。
        self.observed_max_tokens: set[int] = set()

    async def on_request(self, request: httpx.Request) -> None:
        self.n_request += 1
        ctx = _CURRENT_CALL.get()
        key = (ctx[0], ctx[1]) if ctx else None
        moment = time.perf_counter()
        # 取证：抓取**真实发出**的请求体里的 max_tokens，证明输出上限覆盖（2048）确已生效。
        req_max_tokens: int | None = None
        req_has_response_format: bool | None = None
        try:
            body = json.loads(request.content.decode("utf-8"))
            if isinstance(body, dict):
                mt = body.get("max_tokens")
                req_max_tokens = int(mt) if isinstance(mt, (int, float)) else None
                req_has_response_format = "response_format" in body
                if req_max_tokens is not None:
                    self.observed_max_tokens.add(req_max_tokens)
        except Exception:  # noqa: BLE001 - 观测量缺失不得影响调用
            pass
        self.events.append(
            {
                "phase": "request",
                "t": moment,
                "method": request.method,
                "url": redact(str(request.url), self._secret),
                "key": key,
                "max_tokens": req_max_tokens,
            }
        )
        if key is not None:
            entry = self.wire.setdefault(key, {})
            entry["t_req"] = moment
            entry["req_max_tokens"] = req_max_tokens
            entry["req_has_response_format"] = req_has_response_format

    async def on_response(self, response: httpx.Response) -> None:
        self.n_response += 1
        ctx = _CURRENT_CALL.get()
        key = (ctx[0], ctx[1]) if ctx else None
        moment = time.perf_counter()
        conn_seq: int | None = None
        try:
            stream = response.extensions.get("network_stream")
            if stream is not None:
                self.conn_ids.add(id(stream))
                conn_seq = len(self.conn_ids)
        except Exception:  # noqa: BLE001 - 观测量缺失不得影响调用
            conn_seq = None
        # 注意：usage（含 reasoning_tokens）在 :class:`ProbeTransport` 层抓取；此处**不**解析
        # body——真实 transport 下钩子内 body 尚不可读，且会把已抓到的值覆写成 None。
        self.events.append(
            {
                "phase": "response",
                "t": moment,
                "status": response.status_code,
                "key": key,
                "conn_seq": conn_seq,
            }
        )
        if key is not None:
            entry = self.wire.setdefault(key, {})
            entry["t_resp"] = moment
            entry["status"] = response.status_code
            entry["conn_seq"] = conn_seq

    @property
    def distinct_connections(self) -> int:
        return len(self.conn_ids)

    def take(self, persona_id: str, attempt_no: int) -> dict[str, Any] | None:
        return self.wire.get((persona_id, attempt_no))

    def capture_usage(self, data: Any) -> None:
        """从**响应 JSON** 抓取 usage 明细（``reasoning_tokens`` 适配器不暴露）。

        由 :class:`ProbeTransport` 在**响应体已读**处调用（httpx 的 response 事件钩子
        在真实 transport 下**响应体尚未读取**，故不能在钩子里解析 body）。
        按当前 :data:`_CURRENT_CALL` 归因写入对应 ``wire`` 记录。
        """
        ctx = _CURRENT_CALL.get()
        if ctx is None or not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details")
        rt = details.get("reasoning_tokens") if isinstance(details, dict) else None
        entry = self.wire.setdefault((ctx[0], ctx[1]), {})
        entry["wire_prompt_tokens"] = int(pt) if isinstance(pt, (int, float)) else None
        entry["wire_completion_tokens"] = int(ct) if isinstance(ct, (int, float)) else None
        entry["reasoning_tokens"] = int(rt) if isinstance(rt, (int, float)) else None


class ProbeTransport(httpx.AsyncHTTPTransport):
    """真实传输层：在**响应体可读处**抓取 usage（含 ``reasoning_tokens``）。

    为什么不在事件钩子里读 body：httpx 的 response 事件钩子**在响应体读取之前**触发
    （真实 transport 下钩子内 ``response.json()`` 会抛 ``ResponseNotRead``，被静默吞掉，
    导致 reasoning_tokens 恒为 None）。因此把 usage 抓取下沉到 transport 层。

    ``await response.aread()`` 会把正文**缓存**在 response 上，httpx 后续读取命中缓存，
    因此 provider 行为不变（仍拿到同一正文）；不影响 ``duration_ms``（非流式本就需读全量）。
    """

    def __init__(self, probe: WireProbe, secret: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._probe = probe
        self._secret = secret

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        try:
            if response.status_code == 200 and str(request.url).endswith("/chat/completions"):
                await response.aread()
                self._probe.capture_usage(json.loads(response.content.decode("utf-8")))
        except Exception:  # noqa: BLE001 - 观测不得影响调用
            pass
        return response


# ---------------------------------------------------------------------------
# provider 观测包装（不改内层行为）
# ---------------------------------------------------------------------------


class RecordingProvider:
    """包装真实 :class:`OpenAICompatibleProvider`，逐请求记录网络明细（不改其行为）。

    ``answer`` 内部：置计时 → 调内层 →（内层作为唯一 HTTP 路径，每次 = 1 次真实 HTTP POST）→
    从 :class:`ModelResponse` 与 wire 钩子汇总一条记录。**不产出任何答案**（红线 #1）。
    """

    def __init__(
        self,
        inner: OpenAICompatibleProvider,
        *,
        settings: Any,
        probe: WireProbe,
        secret: str | None,
        t0: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._inner = inner
        self._settings = settings
        self._probe = probe
        self._secret = secret
        self._t0_ref = t0
        #: 注入的 httpx client（``OpenAICompatibleProvider`` 因 ``client`` 非空而不拥有它，
        #: 故由本包装负责关闭，避免连接泄漏）。
        self._client = client
        self.records: list[dict[str, Any]] = []

    @property
    def endpoint_redacted(self) -> str:
        return redact(self._settings.model_endpoint, self._secret)

    @property
    def request_url(self) -> str:
        base = str(self._settings.model_endpoint).rstrip("/")
        return redact(f"{base}/chat/completions", self._secret)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()

    async def answer(self, request: Any) -> Any:  # noqa: ANN401 - 与内层同接口
        ctx = _CURRENT_CALL.get()
        persona_id = ctx[0] if ctx else None
        attempt_no = ctx[1] if ctx else None
        started = time.perf_counter()
        response = await self._inner.answer(request)
        finished = time.perf_counter()

        error_code = response.error.error_code if response.error is not None else None
        http_status = response.error.http_status if response.error is not None else 200
        wire = (
            self._probe.take(persona_id, attempt_no)
            if persona_id is not None and attempt_no is not None
            else None
        )
        wire_status = wire.get("status") if wire else None
        wire_ms = (
            int((wire["t_resp"] - wire["t_req"]) * 1000)
            if wire and "t_req" in wire and "t_resp" in wire
            else None
        )
        retry_after = response.error.retry_after_seconds if response.error else None
        record = {
            "seq": len(self.records) + 1,
            "persona_id": persona_id,
            "attempt_no": attempt_no,
            "method": "POST",
            "url": self.request_url,
            "http_status": http_status,
            "wire_status": wire_status,
            "duration_ms": int(response.duration_ms),
            "wall_ms": int((finished - started) * 1000),
            "wire_ms": wire_ms,
            "retry": bool(attempt_no is not None and attempt_no > 1),
            "retry_no": (attempt_no - 1) if attempt_no and attempt_no > 1 else 0,
            "retry_after_s": retry_after,
            "prompt_tokens": response.input_tokens,
            "completion_tokens": response.output_tokens,
            # 来自响应原文（适配器不暴露）——与 input/output 分列，不并入总数。
            "reasoning_tokens": (wire.get("reasoning_tokens") if wire else None),
            "wire_prompt_tokens": (wire.get("wire_prompt_tokens") if wire else None),
            "wire_completion_tokens": (wire.get("wire_completion_tokens") if wire else None),
            # 真实发出的请求体里的 max_tokens（证明 §7.7 O1 覆盖生效）。
            "req_max_tokens": (wire.get("req_max_tokens") if wire else None),
            "req_has_response_format": (wire.get("req_has_response_format") if wire else None),
            "finish_reason": response.finish_reason,
            "provider_request_id": response.provider_request_id,
            "error_code": error_code,
            "error_message": (
                redact(response.error.message, self._secret) if response.error else None
            ),
            "t_start_offset_s": round(started - self._t0_ref, 6),
            "t_end_offset_s": round(finished - self._t0_ref, 6),
        }
        self.records.append(record)
        return response


# ---------------------------------------------------------------------------
# 执行器子类：把当前 (persona_id, attempt_no) 放进 contextvar（不改业务逻辑）
# ---------------------------------------------------------------------------


class InstrumentedExecutor(AttemptExecutor):
    """仅在执行前后 set/reset :data:`_CURRENT_CALL`，其余完全委托父类。"""

    async def execute(  # type: ignore[override]
        self,
        *,
        run: Any,
        claim: Any,
        survey: Any,
        now: Any = None,
    ) -> Any:
        token = _CURRENT_CALL.set(
            (claim.persona_id, int(claim.attempt_no), int(claim.attempt_limit))
        )
        try:
            return await super().execute(run=run, claim=claim, survey=survey, now=now)
        finally:
            _CURRENT_CALL.reset(token)


# ---------------------------------------------------------------------------
# 并发双路采样
# ---------------------------------------------------------------------------


class DualConcurrencySampler:
    """按固定间隔双路采样：① 限流器在途；② DB 侧 running 行数。

    DB 侧按任务要求采样 ``attempts.status='running'``；``attempts`` **无** ``run_id`` 列，
    故经 ``run_members`` JOIN（原始 SQL 见类常量）。同时采样
    ``run_members.status='running'`` 作为第二路交叉参考。
    """

    _SQL_MEMBERS = text(
        "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'running'"
    )
    _SQL_ATTEMPTS = text(
        "SELECT count(*) FROM attempts a JOIN run_members m ON m.id = a.member_id "
        "WHERE m.run_id = :r AND a.status = 'running'"
    )

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        run_id: uuid.UUID,
        limiter: ConcurrencyLimiter,
        *,
        interval: float = CONCURRENCY_SAMPLE_INTERVAL,
        t0: float | None = None,
    ) -> None:
        self._maker = maker
        self._run_id = run_id
        self._limiter = limiter
        self._interval = interval
        self._t0 = t0 if t0 is not None else time.perf_counter()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.series: list[list[float]] = []
        self.samples = 0
        self.peak_in_flight = 0
        self.peak_db_members = 0
        self.peak_db_attempts = 0
        self.last_in_flight = 0
        self.last_db_members = 0
        self.last_db_attempts = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stop.is_set():
            moment = time.perf_counter()
            in_flight = int(self._limiter.in_flight)
            db_members = 0
            db_attempts = 0
            try:
                async with self._maker() as session:
                    db_members = int(
                        await session.scalar(self._SQL_MEMBERS, {"r": self._run_id}) or 0
                    )
                    db_attempts = int(
                        await session.scalar(self._SQL_ATTEMPTS, {"r": self._run_id}) or 0
                    )
            except Exception:  # noqa: BLE001 - 采样失败不得中断压测
                pass
            self.series.append([round(moment - self._t0, 4), in_flight, db_members, db_attempts])
            self.samples += 1
            self.last_in_flight = in_flight
            self.last_db_members = db_members
            self.last_db_attempts = db_attempts
            self.peak_in_flight = max(self.peak_in_flight, in_flight)
            self.peak_db_members = max(self.peak_db_members, db_members)
            self.peak_db_attempts = max(self.peak_db_attempts, db_attempts)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task


# ---------------------------------------------------------------------------
# 输入准备
# ---------------------------------------------------------------------------


def read_sample_bytes() -> bytes:
    """读样本 CSV 原始字节（含 BOM；只读，不写回 Desktop）。"""
    if not SAMPLE_CSV.exists():
        raise FileNotFoundError(f"sample csv not found: {SAMPLE_CSV}")
    return SAMPLE_CSV.read_bytes()


def write_slice_csv(rows: int) -> pathlib.Path:
    """把样本**前 ``rows`` 个数据行**写成项目内临时切片 CSV（保留表头与 BOM）。

    ⚠️ 这是**刻意的插桩手段**（第 2 轮），非全样本；报告须写明「该 run 使用 10 行切片」。
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    text_in = SAMPLE_CSV.read_text(encoding="utf-8-sig")
    all_rows = [list(row) for row in csv.reader(io.StringIO(text_in))]
    header, data = all_rows[0], all_rows[1:]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(data[:rows])
    target = FIXTURES_DIR / f"sample50_head{rows}.csv"
    target.write_text(buffer.getvalue(), encoding="utf-8-sig")  # utf-8-sig 保留 BOM
    return target


def survey_payload() -> dict[str, Any]:
    """问卷创建体（产品 + 默认单题；题目快照由服务端冻结）。"""
    return {"title": SURVEY_TITLE, "product": PRODUCT, "question": {}}


# ---------------------------------------------------------------------------
# 环境 / DB 守卫
# ---------------------------------------------------------------------------


def assert_dev_database(dsn: str) -> None:
    """硬守卫：只允许写开发库 ``survey``（**绝不**占用 ``survey_test*``）。"""
    name = dsn.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if name != "survey":
        raise RuntimeError(
            f"refusing to run real 50-person load test against {name!r}: "
            f"this task requires the dev database 'survey' (dsn={dsn!r})"
        )


async def count_active_runs(dsn: str) -> list[str]:
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id FROM runs WHERE status IN "
                        "('running','pausing','paused','cancelling')"
                    )
                )
            ).all()
        return [str(row[0]) for row in rows]
    finally:
        await engine.dispose()


async def cancel_active_runs(dsn: str) -> list[str]:
    """把遗留活动批次一次性收敛为 ``cancelled``（仅在显式 ``--cancel-active`` 时调用）。"""
    engine = create_async_engine(dsn)
    cancelled: list[str] = []
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id FROM runs WHERE status IN "
                        "('running','pausing','paused','cancelling')"
                    )
                )
            ).all()
            for (run_id,) in rows:
                cancelled.append(str(run_id))
                await conn.execute(
                    text(
                        "UPDATE run_members SET status='cancelled', lease_token=NULL, "
                        "lease_expires_at=NULL WHERE run_id = :r "
                        "AND status IN ('pending','running','retry_wait')"
                    ),
                    {"r": run_id},
                )
                await conn.execute(
                    text("UPDATE runs SET status='cancelled', finished_at=now() WHERE id = :r"),
                    {"r": run_id},
                )
    finally:
        await engine.dispose()
    return cancelled


# ---------------------------------------------------------------------------
# 汇总计算
# ---------------------------------------------------------------------------


def summarize_network(
    records: list[dict[str, Any]],
    probe: WireProbe,
    settings: Any,
    *,
    serve_duration_s: float,
) -> dict[str, Any]:
    """逐请求记录的汇总（成功率/错误分类/延迟分位/token/实测 RPM·TPM/连接数）。"""
    total = len(records)
    ok = [r for r in records if r["error_code"] is None]
    errored = [r for r in records if r["error_code"] is not None]
    durations = [float(r["duration_ms"]) for r in records]
    ok_durations = [float(r["duration_ms"]) for r in ok]
    wire_durations = [float(r["wire_ms"]) for r in records if r["wire_ms"] is not None]

    def count_status(pred: Any) -> int:
        return sum(1 for r in records if pred(r.get("http_status")))

    def count_code(code: str) -> int:
        return sum(1 for r in records if r["error_code"] == code)

    prompt_total = sum(int(r["prompt_tokens"]) for r in records if r["prompt_tokens"] is not None)
    completion_total = sum(
        int(r["completion_tokens"]) for r in records if r["completion_tokens"] is not None
    )
    with_usage = sum(
        1 for r in records if r["prompt_tokens"] is not None or r["completion_tokens"] is not None
    )
    starts = [r["t_start_offset_s"] for r in records]
    ends = [r["t_end_offset_s"] for r in records]
    window_s = (max(ends) - min(starts)) if records else 0.0
    retries = sum(int(r["retry_no"]) for r in records)
    retry_requests = [r for r in records if r["retry"]]

    def rp(values: list[float]) -> dict[str, float]:
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "min": 0.0}
        return {
            "p50": round(_percentile(values, 0.5), 2),
            "p95": round(_percentile(values, 0.95), 2),
            "p99": round(_percentile(values, 0.99), 2),
            "max": round(max(values), 2),
            "min": round(min(values), 2),
        }

    def rate(count: int, seconds: float) -> float:
        return round(count / (seconds / 60.0), 3) if seconds > 0 else 0.0

    return {
        "total_requests": total,
        "ok_count": len(ok),
        "error_count": len(errored),
        "success_rate": round(len(ok) / total, 6) if total else 0.0,
        "http_429_count": count_status(lambda s: s == 429),
        "http_5xx_count": count_status(lambda s: isinstance(s, int) and 500 <= s <= 599),
        "http_4xx_other_count": count_status(
            lambda s: isinstance(s, int) and 400 <= s <= 499 and s != 429
        ),
        "timeout_count": count_code(ERROR_TIMEOUT),
        "rate_limited_count": count_code(ERROR_RATE_LIMITED),
        "auth_count": count_code(ERROR_AUTH),
        "server_error_count": count_code(ERROR_SERVER_ERROR),
        "protocol_count": count_code(ERROR_PROTOCOL),
        "retry_requests": len(retry_requests),
        "retry_attempts_total": retries,
        "error_code_breakdown": _tally(r["error_code"] for r in records if r["error_code"]),
        "finish_reason_breakdown": _tally(r["finish_reason"] for r in records),
        "latency_ms_all": rp(durations),
        "latency_ms_ok": rp(ok_durations),
        "latency_ms_wire": rp(wire_durations),
        "duration_ms_sum": int(sum(durations)),
        "prompt_tokens_total": prompt_total,
        "completion_tokens_total": completion_total,
        "tokens_total": prompt_total + completion_total,
        "requests_with_usage": with_usage,
        "requests_without_usage": total - with_usage,
        # reasoning_tokens 单列（不并入 tokens_total）——供后续 max_tokens/成本口径判断。
        "reasoning_tokens_total": sum(
            int(r["reasoning_tokens"]) for r in records if r["reasoning_tokens"] is not None
        ),
        "requests_with_reasoning_tokens": sum(
            1 for r in records if r["reasoning_tokens"] is not None
        ),
        # 真实请求体中观测到的 max_tokens 取值（期望恰为 [2048]，即 O1 覆盖生效）。
        "observed_request_max_tokens_values": sorted(probe.observed_max_tokens),
        "requests_with_response_format": sum(
            1 for r in records if r.get("req_has_response_format") is True
        ),
        "network_window_s": round(window_s, 4),
        "serve_duration_s": round(serve_duration_s, 4),
        "measured_rpm": rate(total, window_s),
        "measured_tpm": rate(prompt_total + completion_total, window_s),
        "configured_rpm": int(settings.model_rpm),
        "configured_tpm": int(settings.model_tpm),
        "http_requests_on_wire": probe.n_request,
        "http_responses_on_wire": probe.n_response,
        "distinct_http_connections": probe.distinct_connections,
        "wire_status_matched_all": all(
            r["wire_status"] is None or r["wire_status"] == r["http_status"] for r in records
        ),
        "model_max_output_tokens": int(settings.model_max_output_tokens),
        "model_timeout_seconds": float(settings.model_timeout_seconds),
    }


def _tally(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = "null" if value is None else str(value)
        out[key] = out.get(key, 0) + 1
    return out


def summarize_concurrency(
    sampler: DualConcurrencySampler,
    records: list[dict[str, Any]],
    *,
    cap: int,
) -> dict[str, Any]:
    """并发汇总 + 由请求时间区间**独立重建**的在途曲线（滚动补位证据）。"""
    series = sampler.series
    # 独立重建：+1 于 t_start，-1 于 t_end。
    events: list[tuple[float, int]] = []
    for r in records:
        events.append((float(r["t_start_offset_s"]), 1))
        events.append((float(r["t_end_offset_s"]), -1))
    events.sort(key=lambda item: (item[0], item[1]))
    running = 0
    rebuilt_peak = 0
    for _, delta in events:
        running += delta
        rebuilt_peak = max(rebuilt_peak, running)

    starts = sorted(float(r["t_start_offset_s"]) for r in records)
    ends = sorted(float(r["t_end_offset_s"]) for r in records)
    first_end = ends[0] if ends else None
    starts_after_first_end = (
        sum(1 for s in starts if s > first_end) if first_end is not None else 0
    )
    # 从「某次完成」到「下一次开始」的最大等待（越小越像滚动补位）。
    max_wait_ms = None
    if records:
        for end in ends:
            later = [s for s in starts if s > end]
            if later:
                delta = (min(later) - end) * 1000.0
                max_wait_ms = delta if max_wait_ms is None else max(max_wait_ms, delta)

    return {
        "interval_s": CONCURRENCY_SAMPLE_INTERVAL,
        "cap": cap,
        "samples": sampler.samples,
        "peak_in_flight_limiter": sampler.peak_in_flight,
        "peak_db_running_members": sampler.peak_db_members,
        "peak_db_running_attempts": sampler.peak_db_attempts,
        "last_in_flight_limiter": sampler.last_in_flight,
        "always_le_cap": (
            sampler.peak_in_flight <= cap
            and sampler.peak_db_members <= cap
            and sampler.peak_db_attempts <= cap
        ),
        "rebuilt_peak_in_flight_from_requests": rebuilt_peak,
        "rolling_refill": {
            "first_completion_offset_s": round(first_end, 4) if first_end is not None else None,
            "starts_strictly_after_first_completion": starts_after_first_end,
            "max_wait_from_a_completion_to_next_start_ms": (
                round(max_wait_ms, 2) if max_wait_ms is not None else None
            ),
            "total_calls": len(records),
        },
        "series": series,
        "series_columns": ["t_offset_s", "in_flight_limiter", "db_running_members",
                           "db_running_attempts"],
    }


async def _read_run_budget(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> dict[str, Any]:
    async with maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, pause_reason, actual_cost, reserved_cost, "
                    "unknown_cost_count, request_limit, requests_reserved, budget_limit "
                    "FROM runs WHERE id = :r"
                ),
                {"r": run_id},
            )
        ).one()
    return {
        "final_status_db": str(row[0]),
        "pause_reason_db": None if row[1] is None else str(row[1]),
        "actual_cost": None if row[2] is None else str(row[2]),
        "reserved_cost": None if row[3] is None else str(row[3]),
        "unknown_cost_count": int(row[4]),
        "request_limit": int(row[5]),
        "requests_reserved": int(row[6]),
        "budget_limit": None if row[7] is None else str(row[7]),
    }


async def _read_pause_reason_native(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> str | None:
    """在**暂停点**用原生 SQL 即时读 ``pause_reason``。

    必须即时读：``resume_run()`` 会把 ``pause_reason`` 清成 NULL，事后只能读到 NULL
    （先例已踩过此坑）。
    """
    async with maker() as session:
        row = (
            await session.execute(
                text("SELECT pause_reason FROM runs WHERE id = :r"), {"r": run_id}
            )
        ).one()
    return None if row[0] is None else str(row[0])


# ---------------------------------------------------------------------------
# 单轮执行
# ---------------------------------------------------------------------------


def build_settings(env_map: dict[str, str], *, dsn: str, cap: int) -> Settings:
    """从 ``deploy/real.env`` + 进程环境构造 Settings（并发/DB/输出上限用本轮覆盖值）。"""
    merged = dict(os.environ)
    merged.update(env_map)
    merged["DATABASE_URL"] = dsn
    merged["TEST_DATABASE_URL"] = dsn
    merged["AGENT_CONCURRENCY"] = str(cap)
    # §7.7 裁定 O1：进程环境覆盖输出上限（不改 real.env）。
    merged["MODEL_MAX_OUTPUT_TOKENS"] = str(MAX_OUTPUT_TOKENS_EFFECTIVE)
    return replace(Settings.from_env(merged), database_url=dsn, test_database_url=dsn)


def build_recording_provider(
    settings: Settings, probe: WireProbe, secret: str | None, t0: float
) -> RecordingProvider:
    """镜像 ``build_provider`` 的真实分支，但注入带事件钩子的 httpx client（同参数）。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = settings.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    client = httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(settings.model_timeout_seconds),
        # 自定义 transport：在响应体可读处抓 usage（reasoning_tokens）。
        transport=ProbeTransport(probe, secret, retries=0),
        limits=httpx.Limits(
            max_connections=settings.agent_concurrency,
            max_keepalive_connections=settings.agent_concurrency,
        ),
        event_hooks={"request": [probe.on_request], "response": [probe.on_response]},
    )
    inner = OpenAICompatibleProvider(
        endpoint=settings.model_endpoint,
        api_key=key,
        model=settings.model_name,
        provider=settings.model_provider,
        timeout_seconds=settings.model_timeout_seconds,
        max_output_tokens=settings.model_max_output_tokens,
        max_connections=settings.agent_concurrency,
        client=client,
    )
    return RecordingProvider(
        inner, settings=settings, probe=probe, secret=secret, t0=t0, client=client
    )


async def run_round(
    *,
    round_key: str,
    env_map: dict[str, str],
    dsn: str,
) -> dict[str, Any]:
    """执行一轮：全流程步骤 + 网络明细 + 并发双路采样 + 预算/不变量。"""
    cfg = ROUNDS[round_key]
    cap = int(cfg["cap"])
    slice_rows = cfg["slice_rows"]

    settings = build_settings(env_map, dsn=dsn, cap=cap)
    secret = settings.api_key()

    data = read_sample_bytes() if slice_rows is None else write_slice_csv(slice_rows).read_bytes()
    source_filename = (
        "User_360_Analysis_sample50.csv"
        if slice_rows is None
        else f"sample50_head{slice_rows}.csv"
    )

    engine = create_async_engine(dsn, pool_size=10, max_overflow=20, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    probe = WireProbe(secret)
    t0 = time.perf_counter()
    provider = build_recording_provider(settings, probe, secret, t0)
    rate_limiter = SmoothRateLimiter(rpm=settings.model_rpm, tpm=settings.model_tpm)
    limiter = ConcurrencyLimiter(capacity=cap)
    executor = InstrumentedExecutor(
        sessionmaker=maker,
        provider=provider,
        settings=settings,
        repository=run_repository,
        retry_policy=RETRY_REAL,
        rate_limiter=rate_limiter,
        allow_reason=True,
    )
    worker = Worker(
        engine=engine,
        sessionmaker=maker,
        provider=provider,
        settings=settings,
        concurrency=cap,
        lease_seconds=120.0,
        renew_interval=20.0,
        scan_interval=10.0,
        poll_interval=0.01,
        lock_check_interval=30.0,
        retry_policy=RETRY_REAL,
        rate_limiter=rate_limiter,
        limiter=limiter,
        executor=executor,
    )

    steps: list[dict[str, Any]] = []
    notes: list[str] = []
    resume_events: list[dict[str, Any]] = []
    if slice_rows is not None:
        notes.append(
            f"该 run 使用 {slice_rows} 行切片（{source_filename}），非全样本；"
            "切片是刻意的插桩手段（验证限流器收紧）。"
        )

    def record(step: str, **extra: Any) -> None:
        steps.append({"step": step, **extra})

    app = create_app(sessionmaker=maker, settings=settings)
    sampler: DualConcurrencySampler | None = None
    conn_sampler = ConnectionSampler(maker)
    summary: dict[str, Any] | None = None
    concurrency_summary: dict[str, Any] | None = None
    network_summary: dict[str, Any] | None = None
    budget: dict[str, Any] = {}
    inv_checks: dict[str, bool] = {}
    final_counts: dict[str, int] = {}
    run_id_str = ""
    final_status = "unknown"
    serve_ms = 0.0

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://survey.local", timeout=180.0
            ) as http:
                # -- 1) 建问卷（配置，不调模型）---------------------------------
                started = time.perf_counter()
                resp = await http.post("/api/v1/surveys", json=survey_payload())
                record(
                    "create_survey",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    model_calls=probe.n_request,
                )
                assert resp.status_code == 201, resp.text
                survey = resp.json()
                survey_id = survey["id"]
                revision = survey["revision"]

                # -- 2) import ----------------------------------------------------
                started = time.perf_counter()
                resp = await http.post(
                    "/api/v1/imports",
                    files={"file": (source_filename, data, "text/csv")},
                    data={"column_mapping": json.dumps(COLUMN_MAPPING)},
                )
                body = resp.json()
                record(
                    "import",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    import_id=body.get("import_id"),
                    row_count=body.get("row_count"),
                    model_calls=probe.n_request,
                )
                assert resp.status_code == 201, resp.text
                import_id = body["import_id"]
                row_count = int(body["row_count"])

                # -- 3) preview（**必须零模型调用**）------------------------------
                started = time.perf_counter()
                resp = await http.post(
                    "/api/v1/runs/preview",
                    json={
                        "survey_id": survey_id,
                        "survey_revision": revision,
                        "import_id": import_id,
                        "model_config_id": settings.model_config_id,
                    },
                )
                preview = resp.json()
                record(
                    "preview",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    sample_size=preview.get("sample_size"),
                    estimated_cost=preview.get("estimated_cost"),
                    cost_is_unknown=preview.get("cost_is_unknown"),
                    prompt_previews=len(preview.get("prompt_previews") or []),
                    model_calls_after_preview=probe.n_request,
                )
                assert resp.status_code == 200, resp.text
                assert probe.n_request == 0, (
                    "preview 不得调用模型，但观测到 HTTP 模型调用："
                    f"{probe.n_request}"
                )

                # -- 4) create run（带 Idempotency-Key）---------------------------
                request_limit = 3 * row_count
                idem_key = uuid.uuid4().hex
                started = time.perf_counter()
                resp = await http.post(
                    "/api/v1/runs",
                    json={
                        "survey_id": survey_id,
                        "survey_revision": revision,
                        "import_id": import_id,
                        "model_config_id": settings.model_config_id,
                        "budget_limit": str(BUDGET_LIMIT),
                        "budget_currency": BUDGET_CURRENCY,
                        "request_limit": request_limit,
                    },
                    headers={"Idempotency-Key": idem_key},
                )
                run_body = resp.json()
                record(
                    "create_run",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    run_id=run_body.get("id"),
                    sample_size=run_body.get("sample_size"),
                    request_limit=run_body.get("request_limit"),
                    idempotency_key=idem_key,
                    model_calls=probe.n_request,
                )
                assert resp.status_code == 201, resp.text
                run_id = run_body["id"]
                run_id_str = run_id
                uuid.UUID(run_id_str)

                # -- 5) start（真实 provider 门禁在此生效）------------------------
                started = time.perf_counter()
                resp = await http.post(f"/api/v1/runs/{run_id}/start")
                record(
                    "start",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    model_calls=probe.n_request,
                )
                if resp.status_code != 202:
                    notes.append(f"start 未返回 202：{resp.status_code} {resp.text[:200]}")
                assert resp.status_code == 202, resp.text

                # -- 6) worker 执行至收敛（并发双路采样在此窗口内）----------------
                assert await worker.start() is True, "worker failed to acquire advisory lock"
                sampler = DualConcurrencySampler(
                    maker, uuid.UUID(run_id_str), limiter, t0=t0
                )
                sampler.start()
                conn_sampler.start()
                started = time.perf_counter()
                try:
                    final_status = await worker.serve(
                        uuid.UUID(run_id_str), timeout=SERVE_TIMEOUT
                    )
                    # 若触发自动暂停：允许**恰好一次** resume（团队硬要求）。
                    # pause_reason 必须在暂停点即时用原生 SQL 读（resume 会把它清成 NULL）。
                    if final_status == "paused":
                        reason_at_pause = await _read_pause_reason_native(
                            maker, uuid.UUID(run_id_str)
                        )
                        resume_events.append(
                            {
                                "detected_status": "paused",
                                "pause_reason_at_pause": reason_at_pause,
                                "t_offset_s": round(time.perf_counter() - t0, 4),
                            }
                        )
                        resp = await http.post(f"/api/v1/runs/{run_id}/resume")
                        try:
                            resume_body_status = (resp.json() or {}).get("status")
                        except Exception:  # noqa: BLE001 - 观测兜底
                            resume_body_status = None
                        resume_events.append(
                            {
                                "resume_http_status": resp.status_code,
                                "resume_body_status": resume_body_status,
                                "t_offset_s": round(time.perf_counter() - t0, 4),
                            }
                        )
                        notes.append(
                            "触发自动暂停一次：pause_reason(暂停点)="
                            f"{reason_at_pause}；已执行**恰好一次** resume"
                            f"（HTTP {resp.status_code}）。"
                        )
                        if resp.status_code == 202:
                            final_status = await worker.serve(
                                uuid.UUID(run_id_str), timeout=SERVE_TIMEOUT
                            )
                            record(
                                "worker_serve_after_resume",
                                status="converged",
                                duration_ms=round(
                                    (time.perf_counter() - started) * 1000, 2
                                ),
                                final_status=final_status,
                                model_calls=probe.n_request,
                            )
                finally:
                    serve_ms = (time.perf_counter() - started) * 1000
                    await sampler.stop()
                    await conn_sampler.stop()
                    await worker.stop()
                record(
                    "worker_serve",
                    status="converged",
                    duration_ms=round(serve_ms, 2),
                    final_status=final_status,
                    model_calls=probe.n_request,
                    in_flight_last=sampler.last_in_flight,
                )

                # -- 7) results / summary / export -------------------------------
                started = time.perf_counter()
                resp = await http.get(
                    f"/api/v1/runs/{run_id}/results",
                    params={"page": 1, "page_size": 200},
                )
                results_body = resp.json()
                record(
                    "results",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    total=results_body.get("total"),
                    items=len(results_body.get("items") or []),
                )
                assert resp.status_code == 200, resp.text

                started = time.perf_counter()
                resp = await http.get(f"/api/v1/runs/{run_id}/summary")
                summary = resp.json()
                record(
                    "summary",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    valid_count=summary.get("valid_count"),
                    planned=summary.get("planned"),
                )
                assert resp.status_code == 200, resp.text

                started = time.perf_counter()
                resp = await http.get(f"/api/v1/runs/{run_id}/export.csv")
                content = resp.content
                csv_rows = _count_csv_rows(content)
                record(
                    "export_csv",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    content_type=resp.headers.get("content-type"),
                    bytes=len(content),
                    has_utf8_bom=content[:3] == b"\xef\xbb\xbf",
                    csv_data_rows=csv_rows,
                )
                assert resp.status_code == 200, resp.text

                started = time.perf_counter()
                resp = await http.get(f"/api/v1/runs/{run_id}")
                run_view = resp.json()
                record(
                    "get_run",
                    status=resp.status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    in_flight=run_view.get("in_flight"),
                    throttled=run_view.get("throttled"),
                    target_concurrency=run_view.get("target_concurrency"),
                )

        # -- 聚集：不变量 / 预算 / 计数 ------------------------------------------
        inv = await collect_invariants(maker, uuid.UUID(run_id_str))
        inv_checks = invariant_checks(inv, expected_size=row_count)
        final_counts = await member_status_counts(maker, uuid.UUID(run_id_str))
        budget = await _read_run_budget(maker, uuid.UUID(run_id_str))
        answer_count = len(await succeeded_answers(maker, uuid.UUID(run_id_str)))
        network_summary = summarize_network(
            provider.records, probe, settings, serve_duration_s=serve_ms / 1000.0
        )
        assert sampler is not None
        concurrency_summary = summarize_concurrency(sampler, provider.records, cap=cap)

        payload: dict[str, Any] = {
            "round": round_key,
            "round_label": cfg["label"],
            "result_file": cfg["result_file"],
            "generated_at": _utc_now(),
            "provider_config": {
                "model_provider": settings.model_provider,
                "model_name": settings.model_name,
                "endpoint": provider.endpoint_redacted,
                "request_url": provider.request_url,
                "model_config_id": settings.model_config_id,
                "model_timeout_seconds": float(settings.model_timeout_seconds),
                "model_max_output_tokens": int(settings.model_max_output_tokens),
                "model_rpm_configured": int(settings.model_rpm),
                "model_tpm_configured": int(settings.model_tpm),
                "agent_concurrency": cap,
                "price_configured": (
                    settings.input_price_per_million is not None
                    and settings.output_price_per_million is not None
                ),
                "api_key_present": bool(secret),
            },
            # 团队硬要求：显式确认「执行真实调用的 Worker 是否在同一进程内」+ 覆盖如何施加。
            "execution_env": {
                "worker_in_same_process": True,
                "evidence": (
                    "Worker / InstrumentedExecutor / RecordingProvider 均由本脚本**进程内**构造；"
                    "worker 经 worker.serve() 在本进程 asyncio loop 内执行真实模型调用；"
                    "控制面 HTTP 经 httpx.ASGITransport 调用同一 app 对象，"
                    "无容器/子进程/独立网络栈。"
                ),
                "override_mechanism": (
                    f"进程环境覆盖：build_settings() 把 MODEL_MAX_OUTPUT_TOKENS="
                    f"{MAX_OUTPUT_TOKENS_EFFECTIVE} 写入 merged env 再 Settings.from_env()；"
                    "deploy/real.env 未改。"
                ),
                "model_max_output_tokens_effective": int(settings.model_max_output_tokens),
                "real_env_model_max_output_tokens": env_map.get("MODEL_MAX_OUTPUT_TOKENS"),
                "on_wire_max_tokens_observed": sorted(probe.observed_max_tokens),
                "python_pid": os.getpid(),
            },
            "sample": {
                "source_file": str(SAMPLE_CSV),
                "row_count": row_count,
                "is_slice": slice_rows is not None,
                "slice_rows": slice_rows,
                "persona_id_column": PERSONA_ID_COLUMN,
                "profile_text_columns": L1_PROFILE_COLUMNS,
                "n_profile_columns": len(L1_PROFILE_COLUMNS),
                "sample_groups_note": (
                    "样本按 _sample_group 分 G1–G11 数据完整度层（来源 synthetic_calibrated）；"
                    "本轮只取 L1 画像列，不补全画像。"
                ),
            },
            "product": PRODUCT,
            "budget": {
                "budget_limit": str(BUDGET_LIMIT),
                "budget_currency": BUDGET_CURRENCY,
                "request_limit": request_limit,
                "request_limit_formula": "3 * sample_size",
                **budget,
            },
            "steps": steps,
            "resume_events": resume_events,
            "network": {
                "summary": network_summary,
                "requests": provider.records,
            },
            "concurrency": concurrency_summary,
            "invariants": {
                "checks": inv_checks,
                "all_pass": all(inv_checks.values()),
                "raw": {
                    "total_members": inv.total,
                    "counts": inv.counts,
                    "valid": inv.valid,
                    "bucket_sum": inv.bucket_sum,
                    "succeeded_null_answers": inv.succeeded_null_answers,
                    "succeeded_illegal_values": inv.succeeded_illegal_values,
                    "succeeded_bad_question_id": inv.succeeded_bad_question_id,
                    "distinct_persona": inv.distinct_persona,
                    "distinct_row_no": inv.distinct_row_no,
                    "attempts_total": inv.attempts_total,
                    "retry_distribution": inv.retry_distribution,
                    "failed_error_codes": inv.failed_error_codes,
                },
            },
            "final": {
                "run_id": run_id_str,
                "final_status": final_status,
                "member_status_counts": final_counts,
                "succeeded_answers": answer_count,
            },
            "runtime": {
                "rss_peak_mb": peak_rss_mb(),
                "peak_connections": conn_sampler.peak,
                "peak_active_connections": conn_sampler.peak_active,
                "peak_lock_waiters": conn_sampler.peak_lock_waiters,
                "max_connections": conn_sampler.max_connections or 0,
                "db_size": (await db_size_pretty(maker))[0],
            },
            "notes": notes,
            "summary_endpoint": summary,
        }
        return payload
    finally:
        with contextlib.suppress(Exception):
            await provider.aclose()
        await engine.dispose()


def _count_csv_rows(content: bytes) -> int:
    """统计导出 CSV 的数据行数（不含表头，含 BOM 处理）。"""
    text_out = content.decode("utf-8-sig", errors="replace")
    rows = [line for line in text_out.splitlines() if line.strip()]
    return max(0, len(rows) - 1)


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# --check：零模型调用的就绪自检（不建 run、不发请求）
# ---------------------------------------------------------------------------


async def check_mode(env_map: dict[str, str], dsn: str) -> int:
    """校验环境/DB/样本/口径，**不做任何模型调用、不创建任何记录**。"""
    from app.personas.source import parse_user_table

    print("=" * 78)
    print("[check] 零模型调用就绪自检")
    print("-" * 78)
    assert_dev_database(dsn)
    print(f"  dev database         : {dsn.rsplit('/', 1)[-1]}")

    secret = env_map.get(env_map.get("MODEL_API_KEY_ENV", "SURVEY_MODEL_API_KEY"))
    print(f"  MODEL_PROVIDER       : {env_map.get('MODEL_PROVIDER')}")
    print(f"  MODEL_NAME           : {env_map.get('MODEL_NAME')}")
    print(f"  MODEL_ENDPOINT       : {env_map.get('MODEL_ENDPOINT')}")
    print(f"  MODEL_API_KEY_ENV    : {env_map.get('MODEL_API_KEY_ENV')}")
    print(f"  api_key_present      : {bool(secret)}")
    print(f"  AGENT_CONCURRENCY    : {env_map.get('AGENT_CONCURRENCY')}")
    print(f"  MODEL_RPM/TPM        : {env_map.get('MODEL_RPM')} / {env_map.get('MODEL_TPM')}")
    print(
        f"  MAX_OUTPUT_TOKENS    : {MAX_OUTPUT_TOKENS_EFFECTIVE} "
        f"(§7.7 进程覆盖；real.env={env_map.get('MODEL_MAX_OUTPUT_TOKENS')})"
    )

    data = read_sample_bytes()
    parsed = parse_user_table(data, filename="User_360_Analysis_sample50.csv",
                              column_mapping=COLUMN_MAPPING)
    print(f"  sample rows parsed   : {len(parsed.snapshots)}")
    print(f"  L1 columns ({len(L1_PROFILE_COLUMNS)})     : {', '.join(L1_PROFILE_COLUMNS)}")
    print(f"  persona_id column    : {PERSONA_ID_COLUMN}")
    print(f"  first persona_id     : {parsed.snapshots[0].persona_id}")
    print(f"  profile_text head    : {parsed.snapshots[0].profile_text[:80]}...")
    slice_path = write_slice_csv(10)
    slice_rows = len(parse_user_table(slice_path.read_bytes(),
                                      filename=slice_path.name,
                                      column_mapping=COLUMN_MAPPING).snapshots)
    print(f"  slice file           : {slice_path} ({slice_rows} rows)")
    print(f"  budget_limit/currency: {BUDGET_LIMIT} {BUDGET_CURRENCY}")
    print("  request_limit formula: 3 * sample_size -> 50:150 / 10:30")
    print("  model calls made     : 0 (no run created)")
    print("=" * 78)
    return 0


# ---------------------------------------------------------------------------
# --preflight-nonmodel：真实 env + 开发库上跑通「不调模型」的全流程步骤
# ---------------------------------------------------------------------------


async def preflight_nonmodel(env_map: dict[str, str], dsn: str) -> int:
    """真实 env / 开发库上验证 ``create survey → import → preview → create run → cancel``。

    **结构性零模型调用**：本模式根本不构造 provider，也不会 start/worker，因此不可能发出
    任何付费模型请求。preview 后再以 wire 计数二次确认（应为 0）。运行后把 run 取消，
    不残留活动批次（不占用 ``uq_runs_single_active``）。
    """
    print("=" * 78)
    print("[preflight-nonmodel] 真实 env + 开发库：不调模型的全流程步骤")
    print("-" * 78)
    assert_dev_database(dsn)
    settings = build_settings(env_map, dsn=dsn, cap=8)
    secret = settings.api_key()
    probe = WireProbe(secret)
    data = write_slice_csv(10).read_bytes()

    engine = create_async_engine(dsn, pool_size=5, max_overflow=5, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(sessionmaker=maker, settings=settings)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://survey.local", timeout=120.0
            ) as http:
                resp = await http.post("/api/v1/surveys", json=survey_payload())
                assert resp.status_code == 201, resp.text
                survey = resp.json()
                print(f"  create_survey        : {resp.status_code} revision={survey['revision']}")

                resp = await http.post(
                    "/api/v1/imports",
                    files={"file": ("sample50_head10.csv", data, "text/csv")},
                    data={"column_mapping": json.dumps(COLUMN_MAPPING)},
                )
                assert resp.status_code == 201, resp.text
                imported = resp.json()
                print(
                    f"  import               : {resp.status_code} "
                    f"row_count={imported['row_count']} import_id={imported['import_id']}"
                )

                resp = await http.post(
                    "/api/v1/runs/preview",
                    json={
                        "survey_id": survey["id"],
                        "survey_revision": survey["revision"],
                        "import_id": imported["import_id"],
                        "model_config_id": settings.model_config_id,
                    },
                )
                preview = resp.json()
                assert resp.status_code == 200, resp.text
                print(
                    f"  preview              : {resp.status_code} "
                    f"sample_size={preview['sample_size']} "
                    f"target_concurrency={preview['target_concurrency']} "
                    f"cost_is_unknown={preview['cost_is_unknown']} "
                    f"estimated_cost={preview['estimated_cost']} "
                    f"prompt_previews={len(preview['prompt_previews'])}"
                )
                assert probe.n_request == 0, f"preview 期间出现模型调用：{probe.n_request}"

                idem = uuid.uuid4().hex
                resp = await http.post(
                    "/api/v1/runs",
                    json={
                        "survey_id": survey["id"],
                        "survey_revision": survey["revision"],
                        "import_id": imported["import_id"],
                        "model_config_id": settings.model_config_id,
                        "budget_limit": str(BUDGET_LIMIT),
                        "budget_currency": BUDGET_CURRENCY,
                        "request_limit": 3 * imported["row_count"],
                    },
                    headers={"Idempotency-Key": idem},
                )
                assert resp.status_code == 201, resp.text
                run = resp.json()
                print(
                    f"  create_run           : {resp.status_code} run_id={run['id']} "
                    f"status={run['status']} sample_size={run['sample_size']} "
                    f"request_limit={run['request_limit']} "
                    f"allowed_actions={run.get('allowed_actions')}"
                )
                assert run["request_limit"] == 3 * imported["row_count"]
                assert run["status"] == "ready"

                # start 门禁（真实 provider + 真实密钥）：证明 Q6 不会在真实运行前拦下我们。
                # start 只把 run 置 running，**不启动 worker** → 不产生任何模型调用。
                resp = await http.post(f"/api/v1/runs/{run['id']}/start")
                print(
                    f"  start (gate check)   : {resp.status_code} "
                    f"status={resp.json().get('status')}"
                )
                assert resp.status_code == 202, resp.text
                assert probe.n_request == 0, "start 不应触发模型调用"

                # 无 worker → 用强制收敛清理（不残留活动批次）。
                cancelled = await cancel_active_runs(dsn)
                print(f"  force cleanup        : cancelled={cancelled}")

                active = await count_active_runs(dsn)
                print(f"  active runs remaining: {active}")
                print(f"  model calls made     : {probe.n_request} (expected 0)")
                assert probe.n_request == 0
                assert active == [], f"活动批次未清空：{active}"
    finally:
        await engine.dispose()
    print("=" * 78)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实第三方 API 50 人全流程 + 网络/并发插桩（红线 #2：非万级）",
    )
    parser.add_argument(
        "--round",
        choices=("cap50", "cap8", "all"),
        default="all",
        help="cap50=全 50 行(cap=50)；cap8=10 行切片(cap=8)；all=两轮顺序执行",
    )
    parser.add_argument(
        "--env-file",
        default=str(PROJECT_ROOT / "deploy" / "real.env"),
        help="真实 provider env（含密钥；gitignored）",
    )
    parser.add_argument(
        "--database-url",
        default=DEV_DATABASE_URL,
        help="落库 DSN（必须是开发库 survey）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="零模型调用就绪自检（不建 run、不发请求）",
    )
    parser.add_argument(
        "--preflight-nonmodel",
        action="store_true",
        help=(
            "在真实 env/开发库上跑通 create/import/preview/create-run + start 门禁"
            "（不调模型、不启动 worker；随后强制收敛）"
        ),
    )
    parser.add_argument(
        "--confirm-real",
        action="store_true",
        help="确认发起**真实付费**模型调用（无此标志则拒绝执行）",
    )
    parser.add_argument(
        "--cancel-active",
        action="store_true",
        help="执行前把遗留活动批次收敛为 cancelled（默认：发现活动批次则失败退出）",
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        help="跳过 alembic upgrade head",
    )
    return parser.parse_args(argv)


def write_result(name: str, payload: dict[str, Any]) -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = RESULTS_DIR / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def print_round_summary(payload: dict[str, Any]) -> None:
    net = payload["network"]["summary"]
    con = payload["concurrency"]
    budget = payload["budget"]
    final = payload["final"]
    print("=" * 78)
    print(f"[round={payload['round']}] {payload['round_label']}")
    print("-" * 78)
    print(f"  run_id / final_status : {final['run_id']} / {final['final_status']}")
    counts_json = json.dumps(final["member_status_counts"], ensure_ascii=False)
    print(f"  member counts         : {counts_json}")
    print(f"  requests ok/error     : {net['ok_count']} / {net['error_count']}")
    print(f"  429 / 5xx / timeout   : {net['http_429_count']} / {net['http_5xx_count']} / "
          f"{net['timeout_count']}")
    print(f"  retry requests        : {net['retry_requests']} "
          f"(attempts={net['retry_attempts_total']})")
    print(f"  latency p50/p95/p99/max(ms,all) : {net['latency_ms_all']['p50']} / "
          f"{net['latency_ms_all']['p95']} / {net['latency_ms_all']['p99']} / "
          f"{net['latency_ms_all']['max']}")
    print(f"  tokens in/out/total   : {net['prompt_tokens_total']} / "
          f"{net['completion_tokens_total']} / {net['tokens_total']}")
    print(f"  reasoning_tokens      : {net['reasoning_tokens_total']} "
          f"(requests_with_reasoning={net['requests_with_reasoning_tokens']})")
    print(f"  on-wire max_tokens    : {net['observed_request_max_tokens_values']} "
          f"(response_format on {net['requests_with_response_format']} reqs)")
    print(f"  measured RPM/TPM      : {net['measured_rpm']} / {net['measured_tpm']} "
          f"(configured {net['configured_rpm']} / {net['configured_tpm']})")
    print(f"  distinct HTTP conns   : {net['distinct_http_connections']} "
          f"(wire req/resp {net['http_requests_on_wire']}/{net['http_responses_on_wire']})")
    print(f"  concurrency peak      : limiter={con['peak_in_flight_limiter']} "
          f"db_members={con['peak_db_running_members']} "
          f"db_attempts={con['peak_db_running_attempts']} "
          f"(cap={con['cap']}, always_le_cap={con['always_le_cap']}, samples={con['samples']})")
    print(f"  rebuilt peak (reqs)   : {con['rebuilt_peak_in_flight_from_requests']} "
          f"| rolling_refill={json.dumps(con['rolling_refill'], ensure_ascii=False)}")
    print(f"  cost                  : actual={budget['actual_cost']} "
          f"reserved={budget['reserved_cost']} "
          f"unknown_count={budget['unknown_cost_count']} "
          f"requests_reserved/limit={budget['requests_reserved']}/{budget['request_limit']}")
    print(f"  invariants all_pass   : {payload['invariants']['all_pass']}")
    print("=" * 78)


async def _run_all(args: argparse.Namespace, env_map: dict[str, str]) -> int:
    rounds = ["cap50", "cap8"] if args.round == "all" else [args.round]
    overall = 0
    for round_key in rounds:
        leftover = await count_active_runs(args.database_url)
        if leftover:
            if args.cancel_active:
                cancelled = await cancel_active_runs(args.database_url)
                print(f"[warn] cancelled leftover active runs: {cancelled}")
            else:
                print(
                    f"[abort] active run(s) present in {args.database_url}: {leftover}; "
                    "refusing to clobber them. Re-run with --cancel-active if they are stale."
                )
                return 3
        payload = await run_round(
            round_key=round_key, env_map=env_map, dsn=args.database_url
        )
        print_round_summary(payload)
        path = write_result(payload["result_file"], payload)
        print(f"[saved] {path}")
        if not payload["invariants"]["all_pass"]:
            overall = 1
        terminal = {"completed", "completed_with_errors", "failed", "cancelled"}
        final_db = payload["budget"].get("final_status_db")
        if final_db not in terminal:
            leftover = await cancel_active_runs(args.database_url)
            print(
                f"[warn] run ended non-terminal ({final_db}); force-cancelled before next "
                f"round: {leftover}"
            )
    return overall


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    assert_dev_database(args.database_url)

    env_path = pathlib.Path(args.env_file)
    if not env_path.exists():
        print(f"[abort] env file not found: {env_path}")
        return 2
    env_map = load_env_file(env_path)

    # 把 real.env 注入**本进程环境**（等价于 compose 的 env_file 语义）。原因：
    # `Settings.api_key()` 在 API 层（Q6 start 门禁）**按 os.environ 读取**密钥，
    # 若只放进 settings 快照，门禁会看不到密钥而误判 422。密钥只驻留进程内存，
    # **绝不**写入任何文件/日志/快照（红线 #3）。
    os.environ.update(env_map)

    if env_map.get("MODEL_PROVIDER", "mock") == "mock":
        print("[abort] MODEL_PROVIDER is 'mock'; this script requires a real provider.")
        return 2

    if args.check:
        return asyncio.run(check_mode(env_map, args.database_url))

    if args.preflight_nonmodel:
        if not args.no_migrate:
            print(f"[migrate] alembic upgrade head -> {args.database_url}")
            _run_alembic_upgrade(args.database_url)
        return asyncio.run(preflight_nonmodel(env_map, args.database_url))

    if not args.confirm_real:
        print(
            "[abort] refusing to make real paid model calls without --confirm-real "
            "(this will send up to 160 real HTTP requests)."
        )
        return 2

    if not args.no_migrate:
        print(f"[migrate] alembic upgrade head -> {args.database_url}")
        _run_alembic_upgrade(args.database_url)

    print(
        f"[run] round={args.round} env={env_path} db={args.database_url.rsplit('/', 1)[-1]} "
        f"provider={env_map.get('MODEL_PROVIDER')} model={env_map.get('MODEL_NAME')}"
    )
    # 说明：**不打印密钥**（红线 #3）。
    return asyncio.run(_run_all(args, env_map))


if __name__ == "__main__":
    raise SystemExit(main())
