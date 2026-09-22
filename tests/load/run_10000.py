#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-or-later
"""T7 万级压测与验收脚本（主文档 §7 / §10 T7 / §11；architecture.md §5 / §6.7）。

**命令行契约**（照抄主文档 §10 T7 的建议入口，从 ``backend/`` 目录调用）：

    cd backend
    uv run python ../tests/load/run_10000.py --scenario normal      --size 10000
    uv run python ../tests/load/run_10000.py --scenario recoverable --size 10000
    uv run python ../tests/load/run_10000.py --scenario permanent   --size 10000
    uv run python ../tests/load/run_10000.py --scenario normal      --size 20000
    # 本轮实际交付用的小规模（证明脚本本身正确，不测容量）：
    uv run python ../tests/load/run_10000.py --scenario all         --size 200

本脚本**真的压**：
- 真实 PostgreSQL（默认隔离库 ``survey_test_t7``）；
- 真实领取 / 租约 / CAS / 收敛路径（复用 ``app.worker.main.Worker`` 与 ``app.runs.*``）；
- 真实 100 槽位并发（``Worker(concurrency=100)`` + 真实 ``ConcurrencyLimiter``）；
- 真实 mock 网络延迟（``MockProvider.fixed_latency_s`` 用 ``await asyncio.sleep`` 模拟在途）。

**不伪造结果**：所有结论均来自真实 DB 查询与真实 mock 调用日志（``MockCallLog``）。

## 场景
| 场景 | mock 配置 | 期望 |
|---|---|---|
| ``normal`` | ``fault_plan="none"`` | 每个成员 1 次调用成功；``valid == size``；run = ``completed`` |
| ``recoverable`` | **间歇性可恢复**（确定性约 1/3 子集：attempt 1 错误 / attempt 2 非法输出 / attempt≥3 合法） | 每次尝试失败都被有限重试；``valid == size``；run = ``completed`` |
| ``permanent`` | ``fault_plan="none"`` + ``invalid_personas=<失败集>`` | 失败成员每次非法输出直至额度耗尽 → ``failed``；其余成功；run = ``completed_with_errors`` |
| ``interference`` | 见下 | 杀 worker + 重启 API + 重复控制命令；核对 DB 不变量 |
| ``burst`` | **可运行时切换的全量突发供应商故障**（前 N 次恒合法，其后全部 429/503） | 触发 §6.4「连续 ≥5 次同类失败 → 暂停 ``api_unavailable``」，**保留未执行成员**；消除故障源 + ``resume`` 后收敛且**不丢已成功答案** |

### ``burst`` 场景的**「需求内部冲突」**与命名（TEAM-BRIEF §7.13 裁定）

主文档 §10 T7 期望 ``recoverable``**全量注入**后 run 仍收敛为 ``completed``；但按
``architecture.md`` §5.2 **全量**注入会让**所有**成员首调同时失败，从而触发主文档 §6.4
「连续 5 次同类供应商失败 → 暂停 ``api_unavailable``」这条**正确**的总中断保护（实测
``final_status="paused"``、``counts={"pending":126,"retry_wait":74}``）。**这是保护在正常工作，
不是缺陷**（§7.13.1）。为既让 ``recoverable`` 能收敛、又不丢掉这条保护链路的自动化证据：

- ``recoverable`` 改为**确定性间歇子集注入**（行序步长，连续同类失败恒 < 5）；
- 把「全量突发」**独立为 ``burst`` 场景**（命名理由：模拟**供应商侧瞬时全域宕机**这一**时间相位**
  事件，非按 persona 采样），用于证明 §6.4 的**暂停 + 保留未执行成员 + 修复配置后恢复**链路。
- ``burst`` 的故障由「是否处于突发窗口」决定，**不**按 ``(persona_id, attempt_no)`` 采样
  （外部宕机的固有性质）；但相位切换由**确定性调用计数**驱动，「突发前成功人数」= ``normal_call_budget``
  是确定的。该场景**不**假装可 per-member 复现。

> **为何 ``permanent`` 用「恒非法输出」而非 ``fail_personas``(PROTOCOL/400)**：按主文档 §6.4，
> 400「模型不存在 / 协议错误」属**供应商配置错误**，正确处理是**暂停** ``api_unavailable`` 并保留
> 未执行成员（不是把成员判失败）。故要让 run **收敛**为 ``completed_with_errors``，须用
> architecture.md §5.2 ``permanent`` 规则里的**另一变体**「每次非法输出直至额度耗尽」。二者都属架构
> 认可的确定性永久故障。

## R2 断言（每个场景**同时**断言两条）
- **(a) 故障按计划触发**：核对 ``MockCallLog`` 的 ``(persona_id, attempt_no, status, error_code)``。
- **(b) 无补值分支产出有效答案**：
  - mock 侧：任何 ``status != "ok"`` 的调用其 ``value`` 必须为 ``None``（错误/非法输出**不带**任何档位）；
  - DB 侧（原生 SQL 交叉核对）：``succeeded`` 成员的 ``answer_json`` **全部**是合法五档、``question_id``
    固定、非 SQL NULL；每个 ``succeeded`` 成员的值必须等于其 mock 日志中某次 ``ok`` 调用的值。

## 确定性（硬要求）
一切随机性由 ``seed`` + ``(persona_id, attempt_no)`` 派生；**不使用全局 ``random``**；mock 用 SHA-256
（``stable_hash``）而非内置 ``hash()``（后者受 ``PYTHONHASHSEED`` 影响、跨进程不确定）。

## 红线
- 严禁调用真实第三方 endpoint（本机无 key）：本脚本只用 mock。
- mock 容量测试**不等于**真实模型运行；结论**不得**表述为「真实万人模拟已完成」（-> ``docs/acceptance.md``）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import math
import os
import pathlib
import resource
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable

# ---------------------------------------------------------------------------
# sys.path：把 backend/（供 import app.*）与 tests/load/（供 import mock_provider）加入。
# mock_provider 以**顶层模块名**导入，避免与 backend/tests 这个名为 tests 的包冲突
# （见 tests/load/__init__.py 的说明）。
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve()
PROJECT_ROOT = _HERE.parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
LOAD_DIR = PROJECT_ROOT / "tests" / "load"
RESULTS_DIR = LOAD_DIR / "results"
for _path in (str(BACKEND_DIR), str(LOAD_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import httpx  # noqa: E402
from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mock_provider import (  # noqa: E402
    FAULT_PLAN_NONE,
    MockProvider,
    stable_hash,
    valid_value_for,
)
from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
    ERROR_RATE_LIMITED,
    ERROR_SERVER_ERROR,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
    ProductInput,
    ProviderError,
    QuestionInput,
    SurveyInput,
)
from app.main import create_app  # noqa: E402
from app.personas.source import create_import  # noqa: E402
from app.runs.repository import run_repository  # noqa: E402
from app.runs.service import run_service  # noqa: E402
from app.surveys.service import survey_service  # noqa: E402
from app.worker.execute import RetryPolicy  # noqa: E402
from app.worker.main import Worker  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 隔离压测库（TEAM-BRIEF §7.6.4：库名以 ``survey_test`` 开头即合法；避开 pytest 主库争用）。
DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_t7"
)

#: 各场景默认规模（主文档 §10 T7：三场景各 10,000；另有 20,000 正常场景经 ``--size`` 覆盖）。
DEFAULT_SIZES: dict[str, int] = {
    "normal": 10_000,
    "recoverable": 10_000,
    "permanent": 10_000,
    "interference": 200,
    "burst": 200,
}

#: ``answer_json`` 合法五档内联 SQL（避免数组参数绑定差异；值来自 contracts.OPTION_VALUES）。
_VALUE_SQL = "('definitely_not','probably_not','unsure','probably_yes','definitely_yes')"

#: run 活动态（与 models.ACTIVE_RUN_STATUSES_SQL 一致）。
_ACTIVE_SQL = "('running','pausing','paused','cancelling')"

#: 列映射（与 tests/fixtures/generate_personas.DEFAULT_COLUMN_MAPPING 一致；此处内联，
#: 避免把 ``backend/tests``（包名 tests）拉进 sys.path 造成命名空间冲突）。
COLUMN_MAPPING: dict[str, Any] = {
    "persona_id": "user_id",
    "profile_text_columns": ["age", "city", "gender", "note"],
    "age": "age",
    "city_tier": "city",
}

#: 快速重试（默认）：只为压测**路径**、不压真实退避时长；生产退避 2s/8s 由
#: ``tests/test_worker.py::test_retry_backoff_default_is_2s_8s`` 覆盖。
RETRY_FAST = RetryPolicy(max_attempts=3, backoff_seconds=(0.05, 0.1), jitter_ratio=0.0)
#: 真实退避（``--retry-backoff real``）：主文档 §6.4 的 2s、8s。
RETRY_REAL = RetryPolicy(max_attempts=3, backoff_seconds=(2.0, 8.0), jitter_ratio=0.0)

#: 每 10 个 persona 取 1 个为「永久失败」成员（确定性，不用随机）。
PERMANENT_FAIL_STRIDE = 10


def _persona_id(index: int) -> str:
    """与 fixture 生成器一致的 persona_id（``p_000001`` ...）。"""
    return f"p_{index:06d}"


def permanent_fail_personas(size: int) -> set[str]:
    """``permanent`` 场景的确定性失败成员集（第 10、20、... 个）。"""
    return {
        _persona_id(index)
        for index in range(PERMANENT_FAIL_STRIDE, size + 1, PERMANENT_FAIL_STRIDE)
    }


def peak_rss_mb() -> float:
    """本进程峰值常驻内存（MB）。

    使用标准库 ``resource.getrusage(RUSAGE_SELF).ru_maxrss``（**高水位**，不可回落的测法）。
    单位在 macOS 为字节、Linux 为 KB，故按 ``sys.platform`` 归一。**注意**：该值是「本脚本
    进程」的整体高水位，涵盖脚本本身 + 注入的 mock provider + 内嵌的 ``Worker`` 调度协程
    + httpx/asyncpg 缓冲区，**不等于**生产形态（API/worker 独立进程）的 RSS，仅作量级参考。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(raw / divisor, 1)


def _csv_bytes(n: int) -> bytes:
    """确定性用户表 CSV（列与 COLUMN_MAPPING 对应）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "age", "city", "gender", "note"])
    for index in range(1, n + 1):
        writer.writerow([_persona_id(index), 20 + (index % 40), "上海", "女", "备注"])
    return buffer.getvalue().encode("utf-8")


def make_settings(dsn: str, **overrides: Any) -> Settings:
    """压测用配置（高 RPM/TPM 以避免限流成为瓶颈；mock provider；配置单价以走「已知费用」路径）。

    ``model_provider="mock"`` 是本机唯一可用模式（TEAM-BRIEF §7.5 T6-d / §7.9.2），
    亦使 API ``start`` 的模型配置门禁被跳过（此处不经 API start，直接服务层启动）。
    """
    base = Settings.from_env()
    overrides.setdefault("model_provider", "mock")
    return replace(
        base,
        database_url=dsn,
        test_database_url=dsn,
        agent_concurrency=100,
        model_rpm=10_000_000,
        model_tpm=10_000_000_000,
        # 配置单价 → 有 usage 时费用「已知」（同时保留「未知费用」计数路径供核对）。
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("1"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 可控 mock（在 tests/load/mock_provider.py 的确定性 mock 上做**观测包装**，不改其故障逻辑）
# ---------------------------------------------------------------------------


class TrackingMockProvider(MockProvider):
    """``MockProvider`` + 在途/峰值计数（用于实测在途曲线与峰值断言）。

    仅包一层计数，**不改** ``(persona_id, attempt_no) → 故障`` 的确定性规则，也不产出任何默认答案。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._current = 0
        self._peak = 0

    @property
    def current(self) -> int:
        return self._current

    @property
    def peak(self) -> int:
        return self._peak

    async def answer(self, request: Any) -> Any:  # noqa: ANN401 - 与基类同接口
        self._current += 1
        self._peak = max(self._peak, self._current)
        try:
            return await super().answer(request)
        finally:
            self._current -= 1


class IntermittentRecoverableProvider(TrackingMockProvider):
    """T7 ``recoverable`` 场景专用：**确定性「间歇性可恢复」**故障注入。

    **为何不用 ``fault_plan="recoverable"`` 全量注入**（实测结论）：全量注入会让**所有**成员的
    首调同时失败，从而触发主文档 §6.4「连续 5 次同类供应商失败 → 暂停 ``api_unavailable``」这条
    **正确**的总中断保护 —— 批次会**停**而非收敛（本脚本实测：全量 recoverable 在并发 100 下
    ``final_status == "paused"``、``counts={"pending":126,"retry_wait":74}``）。真实世界的偶发
    429/5xx 是**间歇**的，故此处按**行序步长**（每 :attr:`FAULT_STRIDE` 个取 1 个）确定性选取
    子集注入「attempt 1 错误 / attempt 2 非法输出 / attempt≥3 合法」，其余恒合法，使失败流与
    成功流交错（连续同类失败恒 < 5）。故障仍**只**由 ``(persona_id, attempt_no)`` 决定，且
    **复用** mock 的 ``stable_hash`` / ``valid_value_for`` / 非法输出变体，不产出任何默认答案。
    """

    #: 故障子集步长（每 FAULT_STRIDE 个 persona 取 1 个为间歇故障成员）。
    #:
    #: 用**行序步长**而非 ``hash % k``：并发 100 时同一批成员几乎同时返回，完成顺序≈行序，
    #: 故步长保证「同类错误在完成流中被健康/合法响应间隔开」，连续同类失败恒 < 5，
    #: 不会触发 §6.4 的总中断暂停。``hash % k`` 会随机散布、可能 5 连坐而误触发暂停（已实测）。
    FAULT_STRIDE = 7

    @classmethod
    def _index(cls, persona_id: str) -> int | None:
        suffix = persona_id.rsplit("_", 1)[-1]
        return int(suffix) if suffix.isdigit() else None

    @classmethod
    def is_faulty(cls, persona_id: str) -> bool:
        """确定性判定该 persona 是否属间歇故障子集（按行序步长）。"""
        index = cls._index(persona_id)
        if index is None:
            return stable_hash(persona_id) % cls.FAULT_STRIDE == 0
        return index % cls.FAULT_STRIDE == 0

    @classmethod
    def faulty_count(cls, size: int) -> int:
        return sum(1 for index in range(1, size + 1) if index % cls.FAULT_STRIDE == 0)

    def _planned_error(self, persona_id: str, attempt_no: int) -> ProviderError | None:
        if attempt_no == 1 and self.is_faulty(persona_id):
            if stable_hash(persona_id) % 2 == 0:
                return ProviderError(
                    error_code=ERROR_RATE_LIMITED,
                    message="mock intermittent rate limited",
                    http_status=429,
                    retry_after_seconds=1.0,
                )
            return ProviderError(
                error_code=ERROR_SERVER_ERROR,
                message="mock intermittent server error",
                http_status=503,
            )
        return None

    def _planned_text(self, persona_id: str, attempt_no: int) -> str:
        if attempt_no == 2 and self.is_faulty(persona_id):
            return self._invalid_variant_for(persona_id)
        return json.dumps(
            {
                "question_id": PURCHASE_INTENT_QUESTION_ID,
                "value": valid_value_for(persona_id),
            },
            ensure_ascii=False,
        )


class GatedMockProvider(TrackingMockProvider):
    """可闸门 mock（用于「杀 worker」干扰：把在途调用**确定性地**钉在 provider.invoke 上）。

    进入 ``answer`` 时若闸门已 ``hold()``，则阻塞在只有 ``release()`` 才置位的事件上。测试在
    「杀 worker」窗口内**绝不** release，故被拦住的调用**不可能**完成 —— 其成员必停在 ``running``。

    关键前提（由 ``worker/main.py:_consumer`` 保证）：成员在 ``provider.answer()`` 被调用**之前**，
    已在其 claim 事务里提交为 ``running``。故「闸门内有调用在等待」⇔「DB 里有 ``running`` 成员」，
    且该 ``running`` 稳定存在（不随调度窗口漂移）。这是「崩溃时确有在途」的确定性替代。

    本类是**测试辅助**，只在合法输出上包一层闸门，不产出任何默认答案（红线 #1 不适用）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hold = asyncio.Event()
        self._release_event = asyncio.Event()
        self._first_gated = asyncio.Event()
        self._gated_started = 0

    def hold(self) -> None:
        self._hold.set()

    def release(self) -> None:
        self._release_event.set()

    @property
    def gated_started(self) -> int:
        return self._gated_started

    async def answer(self, request: Any) -> Any:  # noqa: ANN401 - 与基类同接口
        if self._hold.is_set():
            self._gated_started += 1
            self._first_gated.set()
            await self._release_event.wait()
        return await super().answer(request)

    async def wait_first_gated(self, timeout: float = 20.0) -> bool:
        try:
            await asyncio.wait_for(self._first_gated.wait(), timeout)
            return True
        except TimeoutError:
            return False


class BurstSwitchableProvider(TrackingMockProvider):
    """T7 ``burst`` 场景专用：**可运行时切换的「全量突发」供应商故障**（主文档 §6.4 保护链路）。

    **命名说明**：``burst``（突发）指**供应商侧瞬时全域宕机** —— 某个时刻起，**所有**（在途与后续）
    调用同时失败；它刻意复现「全量注入」的量级，用来**证明** §6.4 的
    「连续 5 次同类供应商失败 → 暂停 ``api_unavailable``」保护链路真的生效，以及
    「保留未执行成员、修复配置后恢复」这条恢复语义（主文档 §11 要求该链路有自动化测试证据）。

    **与 ``(persona_id, attempt_no)`` 确定性规则的关系（重要、如实声明）**：
    - ``normal`` / ``recoverable`` / ``permanent`` 三场景的故障**只**由 ``(persona_id, attempt_no)`` 决定；
    - ``burst`` 模拟的是**外部供应商宕机**这一**时间相位**事件，故障由「当前是否处于突发窗口」决定，
      **不**按 persona 采样。故「哪些成员恰好撞上突发」取决于调度时序，**不是** per-member 确定的
      —— 这是外部宕机场景的**固有性质**，本类**不**假装它可 per-member 复现；
    - 相位切换本身由**确定性调用计数**驱动：前 ``normal_call_budget`` 次调用**恒合法**，其后的**全部**
      调用**恒失败**，因此「突发前成功人数」= ``min(normal_call_budget, size)`` 是**确定**的；
    - 突发错误码按 ``stable_hash(persona_id) % 2`` 在 429 / 503 间**确定性**择一（两者都累计 §6.4 的
      连续失败计数）；突发**绝不**产出任何档位值（其 ``value`` 恒为 ``None``）。

    本类是**测试辅助**：不产默认答案、不改上游 ``(persona_id, attempt_no)`` 规则，只切换
    「供应商是否宕机」这一外部相位（模拟故障**不属于**红线 #1）。
    """

    def __init__(self, *, normal_call_budget: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._normal_budget = int(normal_call_budget)
        self._burst = False
        self._recovered = False

    @property
    def in_burst(self) -> bool:
        return self._burst and not self._recovered

    @property
    def normal_calls_left(self) -> int:
        return self._normal_budget

    def trigger_burst(self) -> None:
        """立即进入突发窗口（此后所有调用恒失败）。"""
        self._burst = True

    def recover(self) -> None:
        """**消除故障源**：供应商恢复，此后所有调用恒合法（模拟「修复配置后恢复」）。"""
        self._burst = False
        self._recovered = True

    def _burst_error_for(self, persona_id: str) -> ProviderError:
        if stable_hash(persona_id) % 2 == 0:
            return ProviderError(
                error_code=ERROR_RATE_LIMITED,
                message="mock burst: provider outage (429)",
                http_status=429,
                retry_after_seconds=1.0,
            )
        return ProviderError(
            error_code=ERROR_SERVER_ERROR,
            message="mock burst: provider outage (503)",
            http_status=503,
        )

    def _planned_error(self, persona_id: str, attempt_no: int) -> ProviderError | None:
        if self._recovered:
            return super()._planned_error(persona_id, attempt_no)
        if self._burst:
            return self._burst_error_for(persona_id)
        if self._normal_budget > 0:
            # 原子递减（本函数内无 await → asyncio 单线程下不可能被并发打断）。
            self._normal_budget -= 1
            return None
        # 预算耗尽 → **确定性**进入突发窗口。
        self._burst = True
        return self._burst_error_for(persona_id)


# ---------------------------------------------------------------------------
# DB 侧在途采样（真实 SQL 采样，非 mock 自报）
# ---------------------------------------------------------------------------


class RunningSampler:
    """周期采样 DB ``run_members.status='running'`` 行数，记录峰值与样本数。"""

    def __init__(self, maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, interval: float = 0.05) -> None:
        self._maker = maker
        self._run_id = run_id
        self._interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.peak = 0
        self.last = 0
        self.samples = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        sql = text(
            "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'running'"
        )
        while not self._stop.is_set():
            try:
                async with self._maker() as session:
                    value = await session.scalar(sql, {"r": self._run_id})
                self.last = int(value or 0)
                self.peak = max(self.peak, self.last)
                self.samples += 1
            except Exception:  # noqa: BLE001 - 采样失败不得中断压测
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task


class ConnectionSampler:
    """周期采样 PostgreSQL 连接数（``pg_stat_activity``），记录峰值。

    用途：10k 规模下**监控是否逼近** ``max_connections`` —— T6 真跑曾暴露「连接池打爆上限」
    （TEAM-BRIEF §7.15.1）。本采样器**只观测**，**绝不**在本脚本里偷偷调小 pool（那会改变被测语义）；
    若逼近上限，脚本**如实报告**瓶颈而非掩盖。
    """

    def __init__(self, maker: async_sessionmaker[AsyncSession], interval: float = 0.1) -> None:
        self._maker = maker
        self._interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.peak = 0
        self.last = 0
        self.samples = 0
        self.peak_active = 0
        #: 峰值「等待锁」会话数（``pg_stat_activity.wait_event_type='Lock'``）——
        #: 用于证实/证伪「领取事务在 run 行 FOR UPDATE 上串行化」导致吞吐受限的猜想。
        self.peak_lock_waiters = 0
        self.last_lock_waiters = 0
        self.max_connections: int | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        sql_all = text("SELECT count(*)::int FROM pg_stat_activity")
        sql_active = text(
            "SELECT count(*)::int FROM pg_stat_activity WHERE state IS DISTINCT FROM 'idle'"
        )
        sql_lock = text(
            "SELECT count(*)::int FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        )
        while not self._stop.is_set():
            try:
                async with self._maker() as session:
                    if self.max_connections is None:
                        raw = await session.scalar(text("SHOW max_connections"))
                        self.max_connections = int(str(raw))
                    self.last = int(await session.scalar(sql_all) or 0)
                    active = int(await session.scalar(sql_active) or 0)
                    lockers = int(await session.scalar(sql_lock) or 0)
                self.peak = max(self.peak, self.last)
                self.peak_active = max(self.peak_active, active)
                self.last_lock_waiters = lockers
                self.peak_lock_waiters = max(self.peak_lock_waiters, lockers)
                self.samples += 1
            except Exception:  # noqa: BLE001 - 采样失败不得中断压测
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task


async def db_size_pretty(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[str, int]:
    """返回 ``(pg_size_pretty, bytes)`` 形式的当前库体积。"""
    async with maker() as session:
        pretty = await session.scalar(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        )
        raw = await session.scalar(
            text("SELECT pg_database_size(current_database())")
        )
    return str(pretty), int(raw or 0)


async def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 0.02) -> bool:
    """轮询等待谓词成立；超时返回最终取值。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return bool(await predicate())


# ---------------------------------------------------------------------------
# 建批 / 查询辅助
# ---------------------------------------------------------------------------


async def build_run(
    maker: async_sessionmaker[AsyncSession],
    n: int,
    *,
    start: bool = True,
    budget_limit: Decimal | None = None,
) -> uuid.UUID:
    """建一个含 ``n`` 个成员的 run（同一事务快照 + 全成员），可选启动。返回 run_id。"""
    async with maker() as session:
        survey = await survey_service.create(
            session,
            SurveyInput(
                title="T7 万级压测问卷",
                product=ProductInput(
                    name="示例产品",
                    description="用于压测的产品",
                    price=Decimal("199"),
                    price_unit="元/件",
                ),
                question=QuestionInput(),
            ),
        )
        record, _ = await create_import(
            session,
            data=_csv_bytes(n),
            filename="t7_personas.csv",
            column_mapping=COLUMN_MAPPING,
        )
        await session.commit()
        run = await run_service.create_run(
            session,
            survey_id=survey.id,
            survey_revision=1,
            import_id=record.id,
            idempotency_key=uuid.uuid4().hex,
            budget_limit=budget_limit,
        )
        await session.commit()
        if start:
            await run_service.start_run(session, run.id)
        return run.id


async def fetch_status(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> str | None:
    async with maker() as session:
        return await session.scalar(
            text("SELECT status FROM runs WHERE id = :r"), {"r": run_id}
        )


async def member_status_counts(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> dict[str, int]:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT status, count(*) FROM run_members WHERE run_id = :r GROUP BY status"
                ),
                {"r": run_id},
            )
        ).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def succeeded_answers(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> dict[str, Any]:
    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT persona_id, answer_json FROM run_members "
                    "WHERE run_id = :r AND status = 'succeeded'"
                ),
                {"r": run_id},
            )
        ).all()
    return {str(row[0]): row[1] for row in rows}


# ---------------------------------------------------------------------------
# 不变量核对（全部走真实 DB 原生 SQL）
# ---------------------------------------------------------------------------


@dataclass
class Invariants:
    """从 DB 采集的不变量数值（供断言与报告）。"""

    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    valid: int = 0
    bucket_sum: int = 0
    succeeded_null_answers: int = 0
    succeeded_illegal_values: int = 0
    succeeded_bad_question_id: int = 0
    distinct_persona: int = 0
    distinct_row_no: int = 0
    distinct_answers: int = 0
    duplicate_attempt_no_groups: int = 0
    attempts_total: int = 0
    succeeded_attempts: int = 0
    multi_succeeded_attempt_members: int = 0
    active_runs: int = 0
    retry_distribution: dict[str, int] = field(default_factory=dict)
    failed_error_codes: dict[str, int] = field(default_factory=dict)
    failed_with_answer: int = 0
    actual_cost: str = "0"
    reserved_cost: str = "0"
    unknown_cost_count: int = 0


async def collect_invariants(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> Invariants:
    """采集全部 DB 级不变量（原生 SQL）。"""
    inv = Invariants()
    async with maker() as session:
        inv.counts = await member_status_counts(maker, run_id)
        inv.total = sum(inv.counts.values())
        inv.valid = inv.counts.get("succeeded", 0)

        async def scalar(sql: str) -> int:
            return int((await session.scalar(text(sql), {"r": run_id})) or 0)

        inv.bucket_sum = await scalar(
            f"SELECT count(*) FROM run_members WHERE run_id = :r "
            f"AND status = 'succeeded' AND answer_json->>'value' IN {_VALUE_SQL}"
        )
        inv.succeeded_null_answers = await scalar(
            "SELECT count(*) FROM run_members WHERE run_id = :r "
            "AND status = 'succeeded' AND answer_json IS NULL"
        )
        inv.succeeded_illegal_values = await scalar(
            f"SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'succeeded' "
            f"AND (answer_json IS NULL OR answer_json->>'value' NOT IN {_VALUE_SQL})"
        )
        inv.succeeded_bad_question_id = await scalar(
            "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'succeeded' "
            f"AND answer_json->>'question_id' IS DISTINCT FROM '{PURCHASE_INTENT_QUESTION_ID}'"
        )
        inv.distinct_persona = await scalar(
            "SELECT count(DISTINCT persona_id) FROM run_members WHERE run_id = :r"
        )
        inv.distinct_row_no = await scalar(
            "SELECT count(DISTINCT row_no) FROM run_members WHERE run_id = :r"
        )
        inv.distinct_answers = await scalar(
            "SELECT count(DISTINCT (persona_id, answer_json)) FROM run_members "
            "WHERE run_id = :r AND status = 'succeeded'"
        )
        inv.duplicate_attempt_no_groups = await scalar(
            "SELECT count(*) FROM ("
            "  SELECT a.member_id, a.attempt_no FROM attempts a "
            "  JOIN run_members m ON m.id = a.member_id WHERE m.run_id = :r "
            "  GROUP BY a.member_id, a.attempt_no HAVING count(*) > 1"
            ") dup"
        )
        inv.attempts_total = await scalar(
            "SELECT count(*) FROM attempts a JOIN run_members m ON m.id = a.member_id "
            "WHERE m.run_id = :r"
        )
        inv.succeeded_attempts = await scalar(
            "SELECT count(*) FROM attempts a JOIN run_members m ON m.id = a.member_id "
            "WHERE m.run_id = :r AND m.status = 'succeeded' AND a.status = 'succeeded'"
        )
        inv.multi_succeeded_attempt_members = await scalar(
            "SELECT count(*) FROM ("
            "  SELECT m.id FROM run_members m JOIN attempts a ON a.member_id = m.id "
            "  WHERE m.run_id = :r AND m.status = 'succeeded' AND a.status = 'succeeded' "
            "  GROUP BY m.id HAVING count(*) > 1"
            ") multi"
        )
        inv.failed_with_answer = await scalar(
            "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'failed' "
            "AND answer_json IS NOT NULL"
        )
        inv.active_runs = int(
            (
                await session.scalar(
                    text(f"SELECT count(*) FROM runs WHERE status IN {_ACTIVE_SQL}")
                )
            )
            or 0
        )

        retry_rows = (
            await session.execute(
                text(
                    "SELECT attempt_count, count(*) FROM run_members WHERE run_id = :r "
                    "AND status IN ('succeeded', 'failed') GROUP BY attempt_count "
                    "ORDER BY attempt_count"
                ),
                {"r": run_id},
            )
        ).all()
        inv.retry_distribution = {str(row[0]): int(row[1]) for row in retry_rows}

        failed_rows = (
            await session.execute(
                text(
                    "SELECT coalesce(last_error_code, 'UNKNOWN'), count(*) FROM run_members "
                    "WHERE run_id = :r AND status = 'failed' GROUP BY 1 ORDER BY 2 DESC"
                ),
                {"r": run_id},
            )
        ).all()
        inv.failed_error_codes = {str(row[0]): int(row[1]) for row in failed_rows}

        run_row = (
            await session.execute(
                text(
                    "SELECT actual_cost, reserved_cost, unknown_cost_count FROM runs "
                    "WHERE id = :r"
                ),
                {"r": run_id},
            )
        ).one()
        inv.actual_cost = str(run_row[0])
        inv.reserved_cost = str(run_row[1])
        inv.unknown_cost_count = int(run_row[2])
    return inv


def invariant_checks(inv: Invariants, *, expected_size: int) -> dict[str, bool]:
    """与场景无关的通用不变量断言。"""
    return {
        "N_in_N_out(成员数==规模)": inv.total == expected_size,
        "row_no↔persona_id 双射": inv.distinct_persona == expected_size
        and inv.distinct_row_no == expected_size
        and inv.distinct_persona == inv.total,
        "成功成员 answer_json 非 NULL": inv.succeeded_null_answers == 0,
        "成功成员 answer_json 全为合法五档": inv.succeeded_illegal_values == 0,
        "成功成员 question_id 固定": inv.succeeded_bad_question_id == 0,
        "五档计数之和 == valid": inv.bucket_sum == inv.valid,
        "成功成员答案互不重复": inv.distinct_answers == inv.valid,
        "无重复 (member_id, attempt_no)": inv.duplicate_attempt_no_groups == 0,
        "每个成功成员恰一条成功 attempt": inv.succeeded_attempts == inv.valid
        and inv.multi_succeeded_attempt_members == 0,
        "failed 成员不带答案": inv.failed_with_answer == 0,
        "至多一个活动批次": inv.active_runs <= 1,
    }


# ---------------------------------------------------------------------------
# R2 断言（故障按计划触发 + 无补值分支）
# ---------------------------------------------------------------------------


def _persona_call_log(provider: MockProvider) -> dict[str, list[Any]]:
    """``persona_id -> 该 persona 的调用记录（按 attempt_no 排序）``。"""
    grouped: dict[str, list[Any]] = {}
    for call in provider.log.records:
        grouped.setdefault(call.persona_id, []).append(call)
    for calls in grouped.values():
        calls.sort(key=lambda c: c.attempt_no)
    return grouped


def max_consecutive_supplier_errors(provider: MockProvider) -> int:
    """mock 调用记录（按完成顺序）中「连续同类供应商错误」的最大长度。

    用于证明间歇故障**不会**触发主文档 §6.4「连续 5 次同类失败 → 暂停 api_unavailable」。
    只统计 ``status == "error"``（429/5xx/TIMEOUT），``invalid`` 输出不计入（平台对其不累计该计数）。
    """
    longest = 0
    current = 0
    for call in provider.log.records:
        if call.status == "error":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def r2_no_default_fill_mock(provider: MockProvider) -> dict[str, Any]:
    """R2(b) mock 侧：任何非 ``ok`` 调用都**不带任何档位值**；``ok`` 调用不带错误码。"""
    non_ok_with_value = [
        (c.persona_id, c.attempt_no, c.status, c.value)
        for c in provider.log.records
        if c.status != "ok" and c.value is not None
    ]
    ok_with_error = [
        (c.persona_id, c.attempt_no, c.error_code)
        for c in provider.log.records
        if c.status == "ok" and c.error_code is not None
    ]
    return {
        "非 ok 调用携带档位值的条数(应为 0)": len(non_ok_with_value),
        "ok 调用携带错误码的条数(应为 0)": len(ok_with_error),
        "examples_non_ok_with_value": non_ok_with_value[:5],
        "examples_ok_with_error": ok_with_error[:5],
    }


async def r2_no_default_fill_db(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    provider: MockProvider,
) -> dict[str, Any]:
    """R2(b) DB 侧交叉核对：每个 ``succeeded`` 成员的值 == 其 mock 日志中某次 ``ok`` 调用的值。"""
    grouped = _persona_call_log(provider)
    answers = await succeeded_answers(maker, run_id)
    mismatched: list[dict[str, Any]] = []
    succeeded_without_ok: list[str] = []
    for persona_id, answer in answers.items():
        calls = grouped.get(persona_id, [])
        ok_values = [c.value for c in calls if c.status == "ok" and c.value is not None]
        if not ok_values:
            succeeded_without_ok.append(persona_id)
            continue
        db_value = answer.get("value") if isinstance(answer, dict) else None
        if db_value not in ok_values:
            mismatched.append(
                {"persona_id": persona_id, "db_value": db_value, "mock_ok_values": ok_values}
            )
    return {
        "succeeded 值不等于任何 ok 调用的条数(应为 0)": len(mismatched),
        "succeeded 但无任何 ok 调用的条数(应为 0)": len(succeeded_without_ok),
        "examples_mismatched": mismatched[:5],
        "examples_succeeded_without_ok": succeeded_without_ok[:5],
    }


def r2_expected_pattern(
    scenario: str,
    provider: MockProvider,
    size: int,
) -> dict[str, Any]:
    """R2(a)：故障按 ``(persona_id, attempt_no)`` 计划**按预期触发**的核对。"""
    grouped = _persona_call_log(provider)
    errors: list[dict[str, Any]] = []
    expected_personas = {_persona_id(index) for index in range(1, size + 1)}

    if scenario == "normal":
        for persona_id in sorted(expected_personas):
            calls = grouped.get(persona_id, [])
            statuses = [c.status for c in calls]
            if statuses != ["ok"] or len(calls) != 1:
                errors.append({"persona_id": persona_id, "statuses": statuses})
        expected_calls = size

    elif scenario == "recoverable":
        for persona_id in sorted(expected_personas):
            calls = grouped.get(persona_id, [])
            statuses = [c.status for c in calls]
            if IntermittentRecoverableProvider.is_faulty(persona_id):
                if statuses != ["error", "invalid", "ok"]:
                    errors.append({"persona_id": persona_id, "statuses": statuses})
            elif statuses != ["ok"]:
                errors.append({"persona_id": persona_id, "statuses": statuses})
        faulty = IntermittentRecoverableProvider.faulty_count(size)
        expected_calls = (size - faulty) * 1 + faulty * 3

    elif scenario == "permanent":
        fail_set = permanent_fail_personas(size)
        for persona_id in sorted(expected_personas):
            calls = grouped.get(persona_id, [])
            statuses = [c.status for c in calls]
            if persona_id in fail_set:
                if statuses != ["invalid", "invalid", "invalid"]:
                    errors.append({"persona_id": persona_id, "statuses": statuses})
            elif statuses != ["ok"]:
                errors.append({"persona_id": persona_id, "statuses": statuses})
        expected_calls = (size - len(fail_set)) * 1 + len(fail_set) * 3
    else:
        raise ValueError(f"unknown scenario: {scenario!r}")

    return {
        "实际调用总数": provider.log.total,
        "期望调用总数": expected_calls,
        "调用总数匹配": provider.log.total == expected_calls,
        "故障/成功序列不符的 persona 数(应为 0)": len(errors),
        "examples_mismatch": errors[:5],
        "mock_status_counts": provider.log.status_counts(),
        "mock_error_counts": provider.log.error_counts(),
    }


# ---------------------------------------------------------------------------
# 场景执行
# ---------------------------------------------------------------------------


def make_provider(scenario: str, size: int, latency: float) -> TrackingMockProvider:
    """按场景构造**确定性故障注入**的跟踪 mock。"""
    if scenario == "normal":
        return TrackingMockProvider(fixed_latency_s=latency, fault_plan=FAULT_PLAN_NONE)
    if scenario == "recoverable":
        return IntermittentRecoverableProvider(
            fixed_latency_s=latency, fault_plan=FAULT_PLAN_NONE
        )
    if scenario == "permanent":
        return TrackingMockProvider(
            fixed_latency_s=latency,
            fault_plan=FAULT_PLAN_NONE,
            invalid_personas=permanent_fail_personas(size),
        )
    raise ValueError(f"unknown scenario: {scenario!r}")


@dataclass
class ScenarioOutcome:
    """一个场景的完整结果（可 JSON 序列化）。"""

    scenario: str
    size: int
    run_id: str
    final_status: str
    duration_s: float
    rate_per_s: float
    peak_in_flight_mock: int
    peak_in_flight_db: int
    db_sample_count: int
    counts: dict[str, int]
    valid: int
    failed: int
    cancelled: int
    retry_distribution: dict[str, int]
    failed_error_codes: dict[str, int]
    attempts_total: int
    actual_cost: str
    reserved_cost: str
    unknown_cost_count: int
    rss_peak_mb: float
    peak_connections: int
    peak_active_connections: int
    peak_lock_waiters: int
    max_connections: int
    db_size: str
    db_size_bytes: int
    r2_faults: dict[str, Any]
    r2_no_default_fill_mock: dict[str, Any]
    r2_no_default_fill_db: dict[str, Any]
    invariant_checks: dict[str, bool]
    scenario_checks: dict[str, Any]
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "size": self.size,
            "run_id": self.run_id,
            "final_status": self.final_status,
            "duration_s": round(self.duration_s, 4),
            "rate_per_s": round(self.rate_per_s, 4),
            "peak_in_flight_mock": self.peak_in_flight_mock,
            "peak_in_flight_db": self.peak_in_flight_db,
            "db_sample_count": self.db_sample_count,
            "counts": self.counts,
            "valid": self.valid,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "retry_distribution": self.retry_distribution,
            "failed_error_codes": self.failed_error_codes,
            "attempts_total": self.attempts_total,
            "actual_cost": self.actual_cost,
            "reserved_cost": self.reserved_cost,
            "unknown_cost_count": self.unknown_cost_count,
            "rss_peak_mb": self.rss_peak_mb,
            "peak_connections": self.peak_connections,
            "peak_active_connections": self.peak_active_connections,
            "peak_lock_waiters": self.peak_lock_waiters,
            "max_connections": self.max_connections,
            "db_size": self.db_size,
            "db_size_bytes": self.db_size_bytes,
            "r2_faults": self.r2_faults,
            "r2_no_default_fill_mock": self.r2_no_default_fill_mock,
            "r2_no_default_fill_db": self.r2_no_default_fill_db,
            "invariant_checks": self.invariant_checks,
            "scenario_checks": self.scenario_checks,
            "passed": self.passed,
        }


async def execute_scenario(
    *,
    dsn: str,
    scenario: str,
    size: int,
    latency: float,
    retry_policy: RetryPolicy,
    timeout: float,
) -> ScenarioOutcome:
    """在真实 PostgreSQL 上跑一个压测场景并做全部断言。"""
    engine = create_async_engine(dsn, pool_size=10, max_overflow=40, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_settings(dsn)
    provider = make_provider(scenario, size, latency)
    worker = Worker(
        engine=engine,
        sessionmaker=maker,
        provider=provider,
        settings=settings,
        concurrency=100,
        lease_seconds=120.0,
        renew_interval=20.0,
        scan_interval=10.0,
        poll_interval=0.01,
        lock_check_interval=30.0,
        retry_policy=retry_policy,
    )
    try:
        assert await worker.start() is True, "worker failed to acquire advisory lock"
        run_id = await build_run(maker, size, start=True)
        sampler = RunningSampler(maker, run_id)
        sampler.start()
        conn_sampler = ConnectionSampler(maker)
        conn_sampler.start()
        started_at = time.perf_counter()
        try:
            final_status = await worker.serve(run_id, timeout=timeout)
        finally:
            duration = time.perf_counter() - started_at
            await sampler.stop()
            await conn_sampler.stop()
            await worker.stop()

        db_size, db_size_bytes = await db_size_pretty(maker)
        inv = await collect_invariants(maker, run_id)
        checks = invariant_checks(inv, expected_size=size)
        checks["在途峰值 ≤ 100(mock)"] = provider.peak <= 100
        checks["在途峰值 ≤ 100(DB)"] = sampler.peak <= 100

        if scenario == "normal":
            scenario_checks: dict[str, Any] = {
                "final_status == completed": final_status == "completed",
                "valid == size": inv.valid == size,
                "failed == 0": inv.counts.get("failed", 0) == 0,
                "每成员仅 1 次尝试": inv.retry_distribution == {"1": size},
            }
        elif scenario == "recoverable":
            faulty = IntermittentRecoverableProvider.faulty_count(size)
            scenario_checks = {
                "final_status == completed": final_status == "completed",
                "valid == size": inv.valid == size,
                "failed == 0": inv.counts.get("failed", 0) == 0,
                "尝试分布匹配(健康=1次,间歇故障=3次)": inv.retry_distribution
                == {"1": size - faulty, "3": faulty},
                "故障流为间歇(最大连续同类失败<5)": max_consecutive_supplier_errors(provider)
                < 5,
            }
        elif scenario == "permanent":
            fail_count = len(permanent_fail_personas(size))
            scenario_checks = {
                "final_status ∈ {completed_with_errors, failed}": final_status
                in ("completed_with_errors", "failed"),
                "预设失败数准确": inv.counts.get("failed", 0) == fail_count,
                "有效数 == size - 失败数": inv.valid == size - fail_count,
                "失败成员有明确 last_error_code": inv.failed_error_codes
                == {"INVALID_OUTPUT": fail_count},
            }
        else:
            raise ValueError(f"unknown scenario: {scenario!r}")

        r2_faults = r2_expected_pattern(scenario, provider, size)
        r2_mock = r2_no_default_fill_mock(provider)
        r2_db = await r2_no_default_fill_db(maker, run_id, provider)

        r2_ok = (
            r2_faults["调用总数匹配"]
            and r2_faults["故障/成功序列不符的 persona 数(应为 0)"] == 0
            and r2_mock["非 ok 调用携带档位值的条数(应为 0)"] == 0
            and r2_mock["ok 调用携带错误码的条数(应为 0)"] == 0
            and r2_db["succeeded 值不等于任何 ok 调用的条数(应为 0)"] == 0
            and r2_db["succeeded 但无任何 ok 调用的条数(应为 0)"] == 0
        )
        passed = all(checks.values()) and all(
            value for value in scenario_checks.values() if isinstance(value, bool)
        ) and r2_ok

        return ScenarioOutcome(
            scenario=scenario,
            size=size,
            run_id=str(run_id),
            final_status=final_status,
            duration_s=duration,
            rate_per_s=(size / duration) if duration > 0 else 0.0,
            peak_in_flight_mock=provider.peak,
            peak_in_flight_db=sampler.peak,
            db_sample_count=sampler.samples,
            counts=inv.counts,
            valid=inv.valid,
            failed=inv.counts.get("failed", 0),
            cancelled=inv.counts.get("cancelled", 0),
            retry_distribution=inv.retry_distribution,
            failed_error_codes=inv.failed_error_codes,
            attempts_total=inv.attempts_total,
            actual_cost=inv.actual_cost,
            reserved_cost=inv.reserved_cost,
            unknown_cost_count=inv.unknown_cost_count,
            rss_peak_mb=peak_rss_mb(),
            peak_connections=conn_sampler.peak,
            peak_active_connections=conn_sampler.peak_active,
            peak_lock_waiters=conn_sampler.peak_lock_waiters,
            max_connections=conn_sampler.max_connections or 0,
            db_size=db_size,
            db_size_bytes=db_size_bytes,
            r2_faults=r2_faults,
            r2_no_default_fill_mock=r2_mock,
            r2_no_default_fill_db=r2_db,
            invariant_checks=checks,
            scenario_checks=scenario_checks,
            passed=passed,
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 干扰场景：杀 worker + 重启 API + 重复控制命令
# ---------------------------------------------------------------------------


def _survey_payload() -> dict[str, Any]:
    return {
        "title": "T7 干扰验证问卷",
        "product": {
            "name": "示例产品",
            "description": "用于干扰验证的产品",
            "price": "199.00",
            "price_unit": "元/件",
            "time_range": "未来 30 天",
            "purchase_conditions": None,
        },
        "question": {},
    }


async def _create_run_via_api(http: httpx.AsyncClient, size: int) -> tuple[str, int]:
    """经真实 API 路由建 survey/import/run（返回 ``(run_id, revision)``）。"""
    survey_resp = await http.post("/api/v1/surveys", json=_survey_payload())
    assert survey_resp.status_code == 201, survey_resp.text
    survey = survey_resp.json()
    import_resp = await http.post(
        "/api/v1/imports",
        files={"file": ("t7.csv", _csv_bytes(size), "text/csv")},
        data={"column_mapping": json.dumps(COLUMN_MAPPING)},
    )
    assert import_resp.status_code == 201, import_resp.text
    imported = import_resp.json()
    run_resp = await http.post(
        "/api/v1/runs",
        json={
            "survey_id": survey["id"],
            "survey_revision": survey["revision"],
            "import_id": imported["import_id"],
            "model_config_id": "default",
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert run_resp.status_code == 201, run_resp.text
    body = run_resp.json()
    return body["id"], body["sample_size"]


async def execute_interference(
    *,
    dsn: str,
    size: int,
    latency: float,
    retry_policy: RetryPolicy,
    timeout: float,
) -> dict[str, Any]:
    """干扰验证：重复控制命令矩阵 + 杀 worker + 重启 API + 恢复不丢已成功答案。"""
    engine = create_async_engine(dsn, pool_size=10, max_overflow=40, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_settings(dsn)
    steps: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    conn_sampler = ConnectionSampler(maker)

    def record(name: str, **kw: Any) -> None:
        steps.append({"step": name, **kw})

    conn_sampler.start()
    try:
        # ------------------------------------------------------------------
        # 第一部分：重复控制命令矩阵（无 worker；状态機全程可判定）
        # ------------------------------------------------------------------
        app1 = create_app(sessionmaker=maker, settings=settings)
        async with app1.router.lifespan_context(app1):
            transport = httpx.ASGITransport(app=app1)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as http:
                run_a, _ = await _create_run_via_api(http, size)

                resp = await http.post(f"/api/v1/runs/{run_a}/start")
                record("run_a.start", status=resp.status_code)
                checks["start_202"] = resp.status_code == 202

                resp = await http.post(f"/api/v1/runs/{run_a}/start")
                record("run_a.start_repeat", status=resp.status_code, code=resp.json().get("code"))
                checks["start_repeat_409"] = resp.status_code == 409

                resp = await http.post(f"/api/v1/runs/{run_a}/pause")
                record("run_a.pause", status=resp.status_code)
                checks["pause_202"] = resp.status_code == 202

                resp = await http.post(f"/api/v1/runs/{run_a}/pause")
                record("run_a.pause_repeat", status=resp.status_code)
                checks["pause_repeat_200"] = resp.status_code == 200

                paused = await wait_for(
                    lambda: _api_status_is(http, run_a, "paused"), timeout=15.0
                )
                record("run_a.reached_paused", ok=paused)
                checks["reached_paused"] = paused

                resp = await http.post(f"/api/v1/runs/{run_a}/resume")
                record("run_a.resume", status=resp.status_code)
                checks["resume_202"] = resp.status_code == 202

                resp = await http.post(f"/api/v1/runs/{run_a}/resume")
                record("run_a.resume_repeat", status=resp.status_code)
                checks["resume_repeat_200"] = resp.status_code == 200

                resp = await http.post(f"/api/v1/runs/{run_a}/cancel")
                record("run_a.cancel", status=resp.status_code)
                checks["cancel_202"] = resp.status_code == 202

                resp = await http.post(f"/api/v1/runs/{run_a}/cancel")
                record("run_a.cancel_repeat", status=resp.status_code)
                checks["cancel_repeat_200"] = resp.status_code == 200

                resp = await http.post(f"/api/v1/runs/{run_a}/start")
                record("run_a.start_after_cancel", status=resp.status_code, code=resp.json().get("code"))
                checks["start_after_cancel_409"] = resp.status_code == 409

                cancelled = await wait_for(
                    lambda: _api_status_is(http, run_a, "cancelled"), timeout=15.0
                )
                checks["run_a_cancelled"] = cancelled
                record("run_a.final_status", status=await _api_status(http, run_a))

                # ----------------------------------------------------------
                # 第二部分：杀 worker + 重启 API（run_b）
                # ----------------------------------------------------------
                run_b, _ = await _create_run_via_api(http, size)
                resp = await http.post(f"/api/v1/runs/{run_b}/start")
                assert resp.status_code == 202, resp.text

                provider1 = GatedMockProvider(
                    fixed_latency_s=latency, fault_plan=FAULT_PLAN_NONE
                )
                worker1 = Worker(
                    engine=engine,
                    sessionmaker=maker,
                    provider=provider1,
                    settings=settings,
                    concurrency=100,
                    lease_seconds=120.0,
                    renew_interval=20.0,
                    scan_interval=30.0,
                    poll_interval=0.01,
                    lock_check_interval=30.0,
                    retry_policy=retry_policy,
                )
                assert await worker1.start() is True
                serve_task = asyncio.create_task(worker1.serve(uuid.UUID(run_b), timeout=timeout))
                sampler = RunningSampler(maker, uuid.UUID(run_b))
                sampler.start()

                # 先让若干成员正常成功，建立「不丢不改」基线。
                ok_some = await wait_for(
                    lambda: _count_succeeded(maker, uuid.UUID(run_b), 3), timeout=30.0
                )
                record("run_b.some_succeeded", ok=ok_some)
                checks["some_succeeded_before_kill"] = ok_some

                # 翻转闸门（此后所有调用阻塞在本用例**绝不会释放**的事件上）。
                provider1.hold()
                engaged = await provider1.wait_first_gated(timeout=30.0)
                record("run_b.gate_engaged", ok=engaged, gated=provider1.gated_started)
                checks["gate_engaged"] = engaged

                running_now = await wait_for(
                    lambda: _count_running(maker, uuid.UUID(run_b), 1), timeout=30.0
                )
                checks["running_member_at_crash"] = running_now

                # 「杀掉」worker：取消调度任务 + 释放 advisory lock。
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                await worker1.stop()
                await sampler.stop()
                # **在 worker 完全停止后**才快照已成功集合 —— 此时再无任何协程可提交成功，
                # 快照稳定，故「重启 API 读到的 succeeded」可**精确等于**该值（消除快照/提交竞态）。
                succeeded_before = await succeeded_answers(maker, uuid.UUID(run_b))
                record("run_b.succeeded_at_kill", count=len(succeeded_before))
                record(
                    "killed_worker",
                    succeeded_before=len(succeeded_before),
                    peak_in_flight_mock=provider1.peak,
                    peak_in_flight_db=sampler.peak,
                )
                checks["peak_in_flight_le_100"] = (
                    provider1.peak <= 100 and sampler.peak <= 100
                )

            # ---- API 实例 #1 已退出（lifespan 关闭）----

        # 模拟崩溃遗留：把在途成员的租约置为过期。
        await _expire_leases(maker, uuid.UUID(run_b))

        # ---- 重启 API：全新实例（无共享进程内存）----
        app2 = create_app(sessionmaker=maker, settings=settings)
        async with app2.router.lifespan_context(app2):
            transport = httpx.ASGITransport(app=app2)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as http2:
                after_restart = (await http2.get(f"/api/v1/runs/{run_b}")).json()
                record(
                    "api_restarted.read_run_b",
                    status=after_restart["status"],
                    in_flight=after_restart["in_flight"],
                    succeeded=after_restart["counts"]["succeeded"],
                )
                checks["api_restart_reads_db_progress"] = (
                    after_restart["counts"]["succeeded"] == len(succeeded_before)
                    and after_restart["status"] == "running"
                )

                provider2 = TrackingMockProvider(
                    fixed_latency_s=latency, fault_plan=FAULT_PLAN_NONE
                )
                worker2 = Worker(
                    engine=engine,
                    sessionmaker=maker,
                    provider=provider2,
                    settings=settings,
                    concurrency=100,
                    lease_seconds=120.0,
                    renew_interval=20.0,
                    scan_interval=0.05,
                    poll_interval=0.01,
                    lock_check_interval=30.0,
                    retry_policy=retry_policy,
                )
                assert await worker2.start() is True
                # 立即扫描过期租约（将 running → retry_wait），再调度至收敛。
                recovered = await worker2.scan_expired_once(uuid.UUID(run_b))
                record("run_b.expired_leases_recovered", recovered=recovered)
                recovery_started = time.perf_counter()
                final_status = await worker2.serve(uuid.UUID(run_b), timeout=timeout)
                recovery_s = time.perf_counter() - recovery_started
                await worker2.stop()

                succeeded_after = await succeeded_answers(maker, uuid.UUID(run_b))
                lost = [
                    persona_id
                    for persona_id, answer in succeeded_before.items()
                    if succeeded_after.get(persona_id) != answer
                ]
                checks["no_success_lost_after_recovery"] = len(lost) == 0
                checks["run_b_completed"] = final_status == "completed"
                checks["run_b_all_succeeded"] = len(succeeded_after) == size

                inv_b = await collect_invariants(maker, uuid.UUID(run_b))
                checks.update(invariant_checks(inv_b, expected_size=size))
                checks["no_duplicate_attempt_no_after_recovery"] = (
                    inv_b.duplicate_attempt_no_groups == 0
                )

                record(
                    "run_b.after_recovery",
                    final_status=final_status,
                    succeeded=len(succeeded_after),
                    lost=len(lost),
                    recovery_s=round(recovery_s, 4),
                    peak_in_flight_mock=provider2.peak,
                )

                # ---- API p95（状态 / 报告，10 并发）----
                p95 = await measure_api_p95(http2, run_b, concurrency=10, total=200)
                record("api_p95", **{k: round(v, 6) for k, v in p95.items()})
                checks["api_p95_under_1s_run"] = p95["run_p95"] < 1.0
                checks["api_p95_under_1s_summary"] = p95["summary_p95"] < 1.0

        await conn_sampler.stop()
        db_size, db_size_bytes = await db_size_pretty(maker)
        return {
            "scenario": "interference",
            "size": size,
            "steps": steps,
            "checks": checks,
            "api_p95": p95,
            "rss_peak_mb": peak_rss_mb(),
            "peak_connections": conn_sampler.peak,
            "peak_active_connections": conn_sampler.peak_active,
            "peak_lock_waiters": conn_sampler.peak_lock_waiters,
            "max_connections": conn_sampler.max_connections or 0,
            "db_size": db_size,
            "db_size_bytes": db_size_bytes,
        }
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 突发场景：全量突发供应商故障 → §6.4 暂停 api_unavailable → 消除故障源 + resume
# ---------------------------------------------------------------------------


async def _read_run_status_reason(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> tuple[str, str | None]:
    """**DB 原生 SQL** 读取 ``runs.status`` 与 ``runs.pause_reason`` 的**原值**。"""
    async with maker() as session:
        row = (
            await session.execute(
                text("SELECT status, pause_reason FROM runs WHERE id = :r"),
                {"r": run_id},
            )
        ).one()
    return str(row[0]), (None if row[1] is None else str(row[1]))


async def execute_burst(
    *,
    dsn: str,
    size: int,
    latency: float,
    timeout: float,
    normal_call_budget: int = 30,
) -> dict[str, Any]:
    """``burst`` 场景：全量突发供应商故障 → §6.4 暂停 ``api_unavailable`` → 消除故障源 + ``resume``。

    流程：
    1. 经真实 API 建并启动 run；
    2. 进程内跑 ``Worker``：provider 前 ``normal_call_budget`` 次调用**恒合法**，其后**全部**
       调用突发失败（429/503）→ 触发主文档 §6.4「连续 ≥5 次同类供应商失败 → 暂停 ``api_unavailable``」；
    3. 断言 run 收敛为 ``paused`` 且 ``pause_reason == "api_unavailable"``（**DB 原生 SQL 读原值**）；
    4. 断言「未执行成员被保留」：``pending``/``retry_wait`` 存在，且**无成员被误判为 ``failed``**；
    5. **消除故障源**（``provider.recover()``）+ 经真实 API ``resume`` → 再跑 worker 收敛；
    6. 断言**不丢任何已成功答案**（突发前后逐条比对）且最终 ``completed``、``valid == size``。

    刻意用**真实退避** ``RETRY_REAL``（2s/8s）：使突发失败成员停在 ``retry_wait`` 而非在保护
    生效前耗尽重试额度 —— 忠实对应 §6.4「保留未执行成员，修复配置后恢复」。
    """
    engine = create_async_engine(dsn, pool_size=10, max_overflow=40, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    settings = make_settings(dsn)
    steps: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    budget = max(1, min(normal_call_budget, size - 1))
    provider = BurstSwitchableProvider(fixed_latency_s=latency, normal_call_budget=budget)
    conn_sampler = ConnectionSampler(maker)

    def record(name: str, **kw: Any) -> None:
        steps.append({"step": name, **kw})

    conn_sampler.start()
    try:
        app = create_app(sessionmaker=maker, settings=settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as http:
                record(
                    "burst.config",
                    normal_call_budget=budget,
                    # burst **内部固定**用真实退避 2s/8s（与 CLI --retry-backoff 的横幅无关）。
                    retry_backoff="real(2s/8s)",
                )
                run_id, _ = await _create_run_via_api(http, size)
                resp = await http.post(f"/api/v1/runs/{run_id}/start")
                record("start", status=resp.status_code)
                checks["start_202"] = resp.status_code == 202

                # ---- 阶段一：全量突发 → 暂停 ----
                worker = Worker(
                    engine=engine,
                    sessionmaker=maker,
                    provider=provider,
                    settings=settings,
                    concurrency=100,
                    lease_seconds=120.0,
                    renew_interval=20.0,
                    scan_interval=30.0,
                    poll_interval=0.01,
                    lock_check_interval=30.0,
                    retry_policy=RETRY_REAL,
                )
                await worker.start()
                sampler = RunningSampler(maker, uuid.UUID(run_id))
                sampler.start()
                paused_status = await worker.serve(uuid.UUID(run_id), timeout=timeout)
                await sampler.stop()
                await worker.stop()
                record(
                    "burst.paused_status",
                    status=paused_status,
                    peak_in_flight_mock=provider.peak,
                    peak_in_flight_db=sampler.peak,
                )
                checks["converged_paused"] = paused_status == "paused"
                checks["peak_in_flight_le_100"] = (
                    provider.peak <= 100 and sampler.peak <= 100
                )

                # ---- DB 原生 SQL：读 status / pause_reason 原值 ----
                db_status, db_reason = await _read_run_status_reason(maker, uuid.UUID(run_id))
                record("burst.db_run_row", status=db_status, pause_reason=db_reason)
                checks["db_status_paused"] = db_status == "paused"
                checks["db_pause_reason_api_unavailable"] = db_reason == "api_unavailable"

                # ---- 未执行成员被保留 ----
                counts = await member_status_counts(maker, uuid.UUID(run_id))
                preserved = counts.get("pending", 0) + counts.get("retry_wait", 0)
                record("burst.member_counts", counts=counts, preserved=preserved)
                checks["preserved_non_terminal_members"] = preserved > 0
                checks["no_member_misjudged_failed"] = counts.get("failed", 0) == 0

                # ---- 突发前已成功（应恰为 budget）----
                succeeded_before = await succeeded_answers(maker, uuid.UUID(run_id))
                record("burst.succeeded_before_recovery", count=len(succeeded_before))
                checks["succeeded_before_equals_budget"] = len(succeeded_before) == budget

                # ---- 突发日志：非 ok 调用不得携带档位值 ----
                burst_mock = r2_no_default_fill_mock(provider)
                checks["burst_non_ok_with_value_zero"] = (
                    burst_mock["非 ok 调用携带档位值的条数(应为 0)"] == 0
                    and burst_mock["ok 调用携带错误码的条数(应为 0)"] == 0
                )
                status_counts = provider.log.status_counts()
                error_counts = provider.log.error_counts()
                record(
                    "burst.call_log",
                    total=provider.log.total,
                    statuses=status_counts,
                    errors=error_counts,
                )
                checks["burst_has_supplier_errors"] = (
                    error_counts.get(ERROR_RATE_LIMITED, 0)
                    + error_counts.get(ERROR_SERVER_ERROR, 0)
                    > 0
                )

                # ---- 阶段二：消除故障源 + resume → 收敛 ----
                provider.recover()
                resp = await http.post(f"/api/v1/runs/{run_id}/resume")
                record("resume", status=resp.status_code)
                checks["resume_202"] = resp.status_code == 202

                worker2 = Worker(
                    engine=engine,
                    sessionmaker=maker,
                    provider=provider,
                    settings=settings,
                    concurrency=100,
                    lease_seconds=120.0,
                    renew_interval=20.0,
                    scan_interval=0.05,
                    poll_interval=0.01,
                    lock_check_interval=30.0,
                    retry_policy=RETRY_REAL,
                )
                await worker2.start()
                started = time.perf_counter()
                final_status = await worker2.serve(uuid.UUID(run_id), timeout=timeout)
                recovery_s = time.perf_counter() - started
                await worker2.stop()

                succeeded_after = await succeeded_answers(maker, uuid.UUID(run_id))
                lost = [
                    persona_id
                    for persona_id, answer in succeeded_before.items()
                    if succeeded_after.get(persona_id) != answer
                ]
                record(
                    "recovered",
                    final_status=final_status,
                    succeeded=len(succeeded_after),
                    lost=len(lost),
                    recovery_s=round(recovery_s, 4),
                )
                checks["no_success_lost_after_resume"] = len(lost) == 0
                checks["completed_after_resume"] = final_status == "completed"
                checks["all_members_succeeded"] = len(succeeded_after) == size

                inv = await collect_invariants(maker, uuid.UUID(run_id))
                checks.update(invariant_checks(inv, expected_size=size))

                # 恢复后：succeeded 值可归因于某次 ok 调用（无补值分支）
                db_mock = await r2_no_default_fill_db(maker, uuid.UUID(run_id), provider)
                checks["no_default_fill_db"] = (
                    db_mock["succeeded 值不等于任何 ok 调用的条数(应为 0)"] == 0
                    and db_mock["succeeded 但无任何 ok 调用的条数(应为 0)"] == 0
                )

        await conn_sampler.stop()
        db_size, db_size_bytes = await db_size_pretty(maker)
        return {
            "scenario": "burst",
            "size": size,
            "normal_call_budget": budget,
            "steps": steps,
            "checks": checks,
            "rss_peak_mb": peak_rss_mb(),
            "peak_connections": conn_sampler.peak,
            "peak_active_connections": conn_sampler.peak_active,
            "peak_lock_waiters": conn_sampler.peak_lock_waiters,
            "max_connections": conn_sampler.max_connections or 0,
            "db_size": db_size,
            "db_size_bytes": db_size_bytes,
        }
    finally:
        await engine.dispose()


async def _api_status(http: httpx.AsyncClient, run_id: str) -> str:
    return (await http.get(f"/api/v1/runs/{run_id}")).json()["status"]


async def _api_status_is(http: httpx.AsyncClient, run_id: str, expected: str) -> bool:
    try:
        return await _api_status(http, run_id) == expected
    except Exception:  # noqa: BLE001
        return False


async def _count_succeeded(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, threshold: int
) -> bool:
    async with maker() as session:
        value = await session.scalar(
            text(
                "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'succeeded'"
            ),
            {"r": run_id},
        )
    return int(value or 0) >= threshold


async def _count_running(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, threshold: int
) -> bool:
    async with maker() as session:
        value = await session.scalar(
            text(
                "SELECT count(*) FROM run_members WHERE run_id = :r AND status = 'running'"
            ),
            {"r": run_id},
        )
    return int(value or 0) >= threshold


async def _expire_leases(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    async with maker() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE run_members SET lease_expires_at = now() - interval '5 seconds' "
                    "WHERE run_id = :r AND status = 'running'"
                ),
                {"r": run_id},
            )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


async def measure_api_p95(
    http: httpx.AsyncClient, run_id: str, *, concurrency: int = 10, total: int = 200
) -> dict[str, float]:
    """以 ``concurrency`` 个并发页面请求测量状态 / 报告 API 的延迟分位（目标 p95 < 1s）。"""

    async def timed(path: str) -> float:
        start = time.perf_counter()
        response = await http.get(path)
        latency = time.perf_counter() - start
        assert response.status_code == 200, response.text
        return latency

    async def burst(path: str) -> list[float]:
        semaphore = asyncio.Semaphore(concurrency)

        async def one() -> float:
            async with semaphore:
                return await timed(path)

        return list(await asyncio.gather(*(one() for _ in range(total))))

    run_latencies = await burst(f"/api/v1/runs/{run_id}")
    summary_latencies = await burst(f"/api/v1/runs/{run_id}/summary")
    return {
        "concurrency": float(concurrency),
        "requests": float(total),
        "run_p50": _percentile(run_latencies, 0.5),
        "run_p95": _percentile(run_latencies, 0.95),
        "run_p99": _percentile(run_latencies, 0.99),
        "run_max": max(run_latencies),
        "summary_p50": _percentile(summary_latencies, 0.5),
        "summary_p95": _percentile(summary_latencies, 0.95),
        "summary_p99": _percentile(summary_latencies, 0.99),
        "summary_max": max(summary_latencies),
    }


# ---------------------------------------------------------------------------
# DB 维护
# ---------------------------------------------------------------------------


def _assert_isolated_database(dsn: str) -> None:
    """硬约束（TEAM-BRIEF §7.6.4 / 红线 #8）：库名必须以 ``survey_test`` 开头。"""
    name = dsn.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not name.startswith("survey_test"):
        raise RuntimeError(
            f"refusing to run T7 load test against {name!r}: database name must start "
            f"with 'survey_test' (got dsn={dsn!r})"
        )


def _run_alembic_upgrade(dsn: str) -> None:
    """以编程方式把压测库迁移到 head（幂等）。必须在事件循环之外调用。"""
    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn)
    alembic_command.upgrade(config, "head")


async def _truncate_all(dsn: str) -> None:
    """清空业务表（隔离压测库；``CASCADE`` 处理外键）。"""
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE attempts, run_members, runs, surveys, imports RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


async def _cancel_leftover_active_runs(dsn: str) -> list[str]:
    """把遗留的活动批次一次性收敛为 ``cancelled``（防御性；避免上一场景异常残留阻塞下一场景）。

    正常情况下每场景都会收敛到终态；此函数只在上一场景**中断**（异常/非终态）时兜底。
    返回被取消的 run_id 列表（供日志）。
    """
    engine = create_async_engine(dsn)
    cancelled: list[str] = []
    try:
        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(f"SELECT id FROM runs WHERE status IN {_ACTIVE_SQL}")
                )
            ).all()
            for (run_id,) in rows:
                cancelled.append(str(run_id))
                await connection.execute(
                    text(
                        "UPDATE run_members SET status='cancelled', lease_token=NULL, "
                        "lease_expires_at=NULL WHERE run_id = :r "
                        "AND status IN ('pending','running','retry_wait')"
                    ),
                    {"r": run_id},
                )
                await connection.execute(
                    text(
                        "UPDATE runs SET status='cancelled', finished_at=now() WHERE id = :r"
                    ),
                    {"r": run_id},
                )
    finally:
        await engine.dispose()
    return cancelled


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def print_scenario_summary(outcome: ScenarioOutcome) -> None:
    """打印单个场景的结构化摘要（供报告直接引用）。"""
    data = outcome.as_dict()
    print("=" * 78)
    print(f"[scenario={data['scenario']}] size={data['size']} run_id={data['run_id']}")
    print("-" * 78)
    print(f"  final_status        : {data['final_status']}")
    print(f"  valid / failed / cancelled : {data['valid']} / {data['failed']} / {data['cancelled']}")
    print(f"  member counts       : {json.dumps(data['counts'], ensure_ascii=False)}")
    print(f"  retry distribution  : {json.dumps(data['retry_distribution'], ensure_ascii=False)}")
    print(f"  failed error codes  : {json.dumps(data['failed_error_codes'], ensure_ascii=False)}")
    print(f"  attempts total      : {data['attempts_total']}")
    print(f"  peak in-flight      : mock={data['peak_in_flight_mock']} db={data['peak_in_flight_db']} "
          f"(db samples={data['db_sample_count']})")
    print(f"  DB connections peak : {data['peak_connections']} (active={data['peak_active_connections']}, "
          f"lock_waiters={data['peak_lock_waiters']}, max_connections={data['max_connections']})")
    print(f"  DB size             : {data['db_size']}")
    print(f"  duration / rate     : {data['duration_s']}s / {data['rate_per_s']} rows/s")
    print(f"  cost known/unknown  : actual={data['actual_cost']} reserved={data['reserved_cost']} "
          f"unknown_count={data['unknown_cost_count']}")
    print(f"  peak RSS (进程高水位) : {data['rss_peak_mb']} MB")

    print("  -- R2(a) 故障按计划触发 --")
    print(f"     actual calls={data['r2_faults']['实际调用总数']} "
          f"expected={data['r2_faults']['期望调用总数']} "
          f"match={data['r2_faults']['调用总数匹配']}")
    print(f"     statuses={json.dumps(data['r2_faults']['mock_status_counts'], ensure_ascii=False)} "
          f"errors={json.dumps(data['r2_faults']['mock_error_counts'], ensure_ascii=False)}")
    print(f"     sequence mismatches={data['r2_faults']['故障/成功序列不符的 persona 数(应为 0)']}")

    print("  -- R2(b) 无补值分支产出有效答案 --")
    for key, value in data["r2_no_default_fill_mock"].items():
        if not key.startswith("examples"):
            print(f"     mock {key}: {value}")
    for key, value in data["r2_no_default_fill_db"].items():
        if not key.startswith("examples"):
            print(f"     db   {key}: {value}")

    print("  -- 通用不变量 --")
    for name, ok in data["invariant_checks"].items():
        print(f"     {'PASS' if ok else 'FAIL'}  {name}")
    print("  -- 场景不变量 --")
    for name, value in data["scenario_checks"].items():
        if isinstance(value, bool):
            print(f"     {'PASS' if value else 'FAIL'}  {name}")
    print(f"  ==> passed = {data['passed']}")
    print("=" * 78)


def print_interference_summary(result: dict[str, Any]) -> None:
    """打印干扰场景的结构化摘要。"""
    print("=" * 78)
    print(f"[scenario=interference] size={result['size']}")
    print("-" * 78)
    for step in result["steps"]:
        print(f"  step {step['step']}: {json.dumps({k: v for k, v in step.items() if k != 'step'}, ensure_ascii=False)}")
    print("  -- API p95 (10 并发) --")
    print(f"     {json.dumps({k: round(v, 6) for k, v in result['api_p95'].items()}, ensure_ascii=False)}")
    print(f"  peak RSS (进程高水位) : {result.get('rss_peak_mb')} MB")
    print(
        f"  DB connections peak : {result.get('peak_connections')} "
        f"(active={result.get('peak_active_connections')}, lock_waiters={result.get('peak_lock_waiters')}, "
        f"max_connections={result.get('max_connections')})"
    )
    print(f"  DB size             : {result.get('db_size')}")
    print("  -- 检查项 --")
    for name, value in result["checks"].items():
        print(f"     {'PASS' if value else 'FAIL'}  {name}")
    print(f"  ==> passed = {all(bool(v) for v in result['checks'].values())}")
    print("=" * 78)


def print_burst_summary(result: dict[str, Any]) -> None:
    """打印突发场景的结构化摘要（**含失败项**，不只报成功项）。"""
    print("=" * 78)
    print(
        f"[scenario=burst] size={result['size']} "
        f"normal_call_budget={result['normal_call_budget']}"
    )
    print("-" * 78)
    for step in result["steps"]:
        print(f"  step {step['step']}: {json.dumps({k: v for k, v in step.items() if k != 'step'}, ensure_ascii=False)}")
    print(f"  peak RSS (进程高水位) : {result.get('rss_peak_mb')} MB")
    print(
        f"  DB connections peak : {result.get('peak_connections')} "
        f"(active={result.get('peak_active_connections')}, lock_waiters={result.get('peak_lock_waiters')}, "
        f"max_connections={result.get('max_connections')})"
    )
    print(f"  DB size             : {result.get('db_size')}")
    print("  -- 检查项（含失败项）--")
    for name, value in result["checks"].items():
        print(f"     {'PASS' if value else 'FAIL'}  {name}")
    print(f"  ==> passed = {all(bool(v) for v in result['checks'].values())}")
    print("=" * 78)


def write_result(name: str, payload: dict[str, Any]) -> pathlib.Path:
    """把场景结果落盘到 ``tests/load/results/``（供 ``docs/acceptance.md`` 引用）。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = RESULTS_DIR / f"{name}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T7 万级压测（mock；真实第三方 API 未执行）",
    )
    parser.add_argument(
        "--scenario",
        choices=("normal", "recoverable", "permanent", "interference", "burst", "all"),
        required=True,
        help="压测场景；all = 三个数据场景 + interference + burst 顺序执行",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=None,
        help="样本规模（默认：normal/recoverable/permanent=10000，interference/burst=200）",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("T7_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="压测库 DSN（库名必须以 survey_test 开头）",
    )
    parser.add_argument(
        "--latency",
        type=float,
        default=0.2,
        help="每次 mock 调用的模拟网络延迟（秒，await asyncio.sleep；默认 0.2 = mock 默认）",
    )
    parser.add_argument(
        "--retry-backoff",
        choices=("fast", "real"),
        default="fast",
        help="重试退避：fast=0.05/0.1s（压测路径）；real=2s/8s（主文档 §6.4 生产值）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="单场景 serve 超时（秒；默认按规模自动）",
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        help="跳过 alembic upgrade head（默认执行）",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留压测库既有数据（默认开始时清空业务表）",
    )
    return parser.parse_args(argv)


async def _run_all(args: argparse.Namespace, retry_policy: RetryPolicy) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not args.keep_data:
        await _truncate_all(args.database_url)

    scenarios = (
        ["normal", "recoverable", "permanent", "interference", "burst"]
        if args.scenario == "all"
        else [args.scenario]
    )
    for scenario in scenarios:
        leftover = await _cancel_leftover_active_runs(args.database_url)
        if leftover:
            print(f"[warn] cancelled leftover active runs before {scenario}: {leftover}")
        size = args.size or DEFAULT_SIZES[scenario]
        timeout = args.timeout or max(300.0, float(size))
        if scenario == "interference":
            payload = await execute_interference(
                dsn=args.database_url,
                size=size,
                latency=args.latency,
                retry_policy=retry_policy,
                timeout=timeout,
            )
            print_interference_summary(payload)
            path = write_result(f"interference_{size}", payload)
        elif scenario == "burst":
            payload = await execute_burst(
                dsn=args.database_url,
                size=size,
                latency=args.latency,
                timeout=timeout,
            )
            print_burst_summary(payload)
            path = write_result(f"burst_{size}", payload)
        else:
            outcome = await execute_scenario(
                dsn=args.database_url,
                scenario=scenario,
                size=size,
                latency=args.latency,
                retry_policy=retry_policy,
                timeout=timeout,
            )
            print_scenario_summary(outcome)
            payload = outcome.as_dict()
            path = write_result(f"{scenario}_{size}", payload)
        payload["_result_file"] = str(path)
        results.append(payload)
        print(f"[saved] {path}")
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _assert_isolated_database(args.database_url)

    if not args.no_migrate:
        print(f"[migrate] alembic upgrade head -> {args.database_url}")
        _run_alembic_upgrade(args.database_url)

    retry_policy = RETRY_REAL if args.retry_backoff == "real" else RETRY_FAST
    print(
        f"[run] scenario={args.scenario} size={args.size} latency={args.latency}s "
        f"retry_backoff={'real(2s/8s)' if args.retry_backoff == 'real' else 'fast(0.05/0.1s)'}"
    )

    results = asyncio.run(_run_all(args, retry_policy))

    print("\n" + "=" * 78)
    print("[T7 SUMMARY]")
    all_passed = True
    for item in results:
        if item["scenario"] in ("interference", "burst"):
            passed = all(bool(v) for v in item["checks"].values())
        else:
            passed = bool(item["passed"])
        all_passed = all_passed and passed
        print(f"  {item['scenario']:<12} size={item['size']:<7} passed={passed}")
    print(f"  IS_PASS = {'YES' if all_passed else 'NO'}")
    print("=" * 78)
    print(
        "NOTE: 本脚本全部使用 mock provider（确定性故障注入）。"
        "mock 容量测试 != 真实模型运行；真实第三方 API 实测【未执行】（本机无凭据）。"
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
