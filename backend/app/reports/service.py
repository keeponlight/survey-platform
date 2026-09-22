# SPDX-License-Identifier: GPL-3.0-or-later
"""统计聚合服务（主文档 §8.3 / PRD-v1 §5 / architecture.md §1）。

**只做 SQL 聚合，禁止调用 LLM 计算百分比（task-list.md §8.4 红线 #7）。**

口径（原样固化主文档 §8.3，禁止自创）::

    planned   = run.sample_size
    valid     = succeeded 成员数
    option_rate[v] = count(value=v) / valid
    top2box   = (count(probably_yes) + count(definitely_yes)) / valid
    mean_score = sum(score(value)) / valid
    coverage  = valid / planned
    progress  = (succeeded + failed + cancelled) / planned

规则：

- ``valid == 0`` 时**比率与均分返回 ``null``**（不得显示 0%）；
  ``coverage`` 为「覆盖率」指标，仍按 ``valid/planned`` 计算（``valid=0`` 时为 ``0.0``）。
- 五档计数之和恒等于 ``valid``。
- ``failed`` **不包括**仍处于 ``retry_wait`` 的成员。
- 分组按 ``run_members.persona_snapshot`` 快照字段；**缺列隐藏**该分组。
- summary 使用**一致性读事务**（``REPEATABLE READ``），附 ``as_of`` 与 ``report_revision``。
- 首版不缓存聚合结果。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import (
    OPTION_LABELS_ZH,
    OPTION_SCORES,
    OPTION_VALUES,
    PURCHASE_INTENT_QUESTION_ID,
)

#: 年龄分组标签（顺序即展示顺序）。
AGE_GROUPS: tuple[str, ...] = ("18-24", "25-29", "30-35", "其他", "未知")

#: 城市分组标签（顺序即展示顺序）。
CITY_GROUPS: tuple[str, ...] = ("一线", "二线", "其他", "未知")

#: 非终态（仍会变化 → summary 标注「部分结果」）。
_NON_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"ready", "running", "pausing", "paused", "cancelling"}
)

#: 成员状态全集（顺序固定）。
_MEMBER_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "succeeded",
    "retry_wait",
    "failed",
    "cancelled",
)

#: 「模拟」标识（UI / 报表 / CSV 三处可核对；不使用「真实购买转化率」措辞）。
SIMULATION_LABEL = "模拟购买意向"

# ---------------------------------------------------------------------------
# SQL 片段（CASE 表达式由快照字段派生分组键）
# ---------------------------------------------------------------------------

_AGE_CASE = """
CASE
  WHEN m.persona_snapshot->>'age' IS NULL
       OR btrim(m.persona_snapshot->>'age') = '' THEN '未知'
  WHEN btrim(m.persona_snapshot->>'age') ~ '^[0-9]+$'
       AND (m.persona_snapshot->>'age')::int BETWEEN 18 AND 24 THEN '18-24'
  WHEN btrim(m.persona_snapshot->>'age') ~ '^[0-9]+$'
       AND (m.persona_snapshot->>'age')::int BETWEEN 25 AND 29 THEN '25-29'
  WHEN btrim(m.persona_snapshot->>'age') ~ '^[0-9]+$'
       AND (m.persona_snapshot->>'age')::int BETWEEN 30 AND 35 THEN '30-35'
  ELSE '其他'
END
"""

_CITY_CASE = """
CASE
  WHEN m.persona_snapshot->>'city_tier' IS NULL
       OR btrim(m.persona_snapshot->>'city_tier') = '' THEN '未知'
  WHEN lower(btrim(m.persona_snapshot->>'city_tier'))
       IN ('tier_1', '一线', '1线', 't1', '一线城市') THEN '一线'
  WHEN lower(btrim(m.persona_snapshot->>'city_tier'))
       IN ('tier_2', '二线', '2线', 't2', '二线城市') THEN '二线'
  ELSE '其他'
END
"""

_VALUE_FILTERS = ",\n".join(
    f"count(*) FILTER (WHERE m.status = 'succeeded' "
    f"AND m.answer_json->>'value' = '{value}') AS c_{value}"
    for value in OPTION_VALUES
)

_GROUP_QUERY = """
SELECT
  {case_expr} AS grp,
  count(*) AS planned,
  count(*) FILTER (WHERE m.status = 'succeeded') AS valid,
  {value_filters}
FROM run_members m
WHERE m.run_id = :run_id
GROUP BY grp
"""


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class BucketCount(BaseModel):
    """单个意向档位的计数与比率。"""

    value: str
    label: str
    score: int
    count: int
    #: ``valid == 0`` 时为 ``None``（不得显示 0%）。
    rate: float | None


class GroupStat(BaseModel):
    """一个分组（年龄/城市）的统计。"""

    label: str
    planned: int
    valid: int
    coverage: float
    #: 该组内 五档 value → count（仅 succeeded）。
    distribution: dict[str, int]


class RunSummary(BaseModel):
    """``GET /runs/{id}/summary`` 出参（一致性快照）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: str
    question_id: str
    prompt_version: str

    sample_size: int
    valid_count: int
    failed_count: int
    cancelled_count: int
    pending_count: int
    running_count: int
    retry_wait_count: int
    succeeded_count: int

    coverage: float
    progress: float
    top2box: float | None
    mean_score: float | None

    buckets: list[BucketCount]
    age_groups: list[GroupStat] | None
    city_groups: list[GroupStat] | None

    report_revision: str
    as_of: datetime
    is_partial: bool

    simulated: bool = True
    simulation_label: str = SIMULATION_LABEL


class ReportNotFoundError(Exception):
    """run 不存在（→ 404）。"""

    code = "NOT_FOUND"


@dataclass(frozen=True)
class _RunMeta:
    """聚合所需的 run 元数据（只读最小字段）。"""

    id: UUID
    status: str
    sample_size: int
    prompt_version: str


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class ReportService:
    """统计聚合服务（无状态，可安全共享）。"""

    async def summary(
        self, session: AsyncSession, run_id: UUID, *, as_of: datetime | None = None
    ) -> RunSummary:
        """在**一致性读事务**内做 SQL 聚合，返回 :class:`RunSummary`。

        调用方须传入一个「尚未执行任何语句」的会话（本方法会以
        ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`` 开启一致性快照）。
        """
        moment = as_of or datetime.now(UTC)
        await self._begin_consistency_read(session)

        run = await self._get_run(session, run_id)
        status_counts = await self._status_counts(session, run_id)
        option_counts = await self._option_counts(session, run_id)
        age_groups, age_present = await self._groups(
            session, run_id, case_expr=_AGE_CASE, order=AGE_GROUPS
        )
        city_groups, city_present = await self._groups(
            session, run_id, case_expr=_CITY_CASE, order=CITY_GROUPS
        )

        sample_size = int(run.sample_size)
        succeeded = status_counts.get("succeeded", 0)
        failed = status_counts.get("failed", 0)
        cancelled = status_counts.get("cancelled", 0)
        valid = succeeded

        buckets = [
            BucketCount(
                value=value,
                label=OPTION_LABELS_ZH[value],
                score=OPTION_SCORES[value],
                count=option_counts.get(value, 0),
                rate=(option_counts.get(value, 0) / valid) if valid > 0 else None,
            )
            for value in OPTION_VALUES
        ]

        top2_count = option_counts.get("probably_yes", 0) + option_counts.get("definitely_yes", 0)
        score_sum = sum(
            option_counts.get(value, 0) * OPTION_SCORES[value] for value in OPTION_VALUES
        )

        top2box = (top2_count / valid) if valid > 0 else None
        mean_score = (score_sum / valid) if valid > 0 else None
        coverage = (valid / sample_size) if sample_size > 0 else 0.0
        progress = ((succeeded + failed + cancelled) / sample_size) if sample_size > 0 else 0.0

        revision = self._report_revision(
            run=run,
            status_counts=status_counts,
            option_counts=option_counts,
            age_groups=age_groups,
            city_groups=city_groups,
        )

        return RunSummary(
            run_id=run.id,
            status=run.status,
            question_id=PURCHASE_INTENT_QUESTION_ID,
            prompt_version=run.prompt_version,
            sample_size=sample_size,
            valid_count=valid,
            failed_count=failed,
            cancelled_count=cancelled,
            pending_count=status_counts.get("pending", 0),
            running_count=status_counts.get("running", 0),
            retry_wait_count=status_counts.get("retry_wait", 0),
            succeeded_count=succeeded,
            coverage=coverage,
            progress=progress,
            top2box=top2box,
            mean_score=mean_score,
            buckets=buckets,
            age_groups=age_groups if age_present else None,
            city_groups=city_groups if city_present else None,
            report_revision=revision,
            as_of=moment,
            is_partial=run.status in _NON_TERMINAL_STATUSES,
        )

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    async def _begin_consistency_read(session: AsyncSession) -> None:
        """开启一致性读事务（``REPEATABLE READ``，须为本事务首条语句）。"""
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

    @staticmethod
    async def _get_run(session: AsyncSession, run_id: UUID) -> _RunMeta:
        result = await session.execute(
            text(
                "SELECT id, status, sample_size, prompt_version FROM runs WHERE id = :run_id"
            ),
            {"run_id": run_id},
        )
        row = result.one_or_none()
        if row is None:
            raise ReportNotFoundError(f"run not found: {run_id}")
        return _RunMeta(
            id=row[0],
            status=str(row[1]),
            sample_size=int(row[2]),
            prompt_version=str(row[3]),
        )

    @staticmethod
    async def _status_counts(session: AsyncSession, run_id: UUID) -> dict[str, int]:
        result = await session.execute(
            text(
                "SELECT status, count(*) FROM run_members "
                "WHERE run_id = :run_id GROUP BY status"
            ),
            {"run_id": run_id},
        )
        counts = {str(status): int(count) for status, count in result.all()}
        return {status: counts.get(status, 0) for status in _MEMBER_STATUSES}

    @staticmethod
    async def _option_counts(session: AsyncSession, run_id: UUID) -> dict[str, int]:
        result = await session.execute(
            text(
                "SELECT m.answer_json->>'value' AS value, count(*) "
                "FROM run_members m "
                "WHERE m.run_id = :run_id AND m.status = 'succeeded' "
                "AND m.answer_json IS NOT NULL "
                "GROUP BY value"
            ),
            {"run_id": run_id},
        )
        raw = {value: int(count) for value, count in result.all() if value is not None}
        return {value: raw.get(value, 0) for value in OPTION_VALUES}

    @staticmethod
    async def _groups(
        session: AsyncSession,
        run_id: UUID,
        *,
        case_expr: str,
        order: tuple[str, ...],
    ) -> tuple[list[GroupStat], bool]:
        """按分组键聚合；返回 ``(分组列表, 该维度是否存在)``。

        「该维度是否存在」= 至少有一个非「未知」分组（缺列/全空 → 隐藏该分组）。
        """
        query = _GROUP_QUERY.format(case_expr=case_expr, value_filters=_VALUE_FILTERS)
        result = await session.execute(text(query), {"run_id": run_id})
        rows = result.all()

        defined = [row for row in rows if row[0] != "未知"]
        present = len(defined) > 0
        if not present:
            return [], False

        by_label: dict[str, GroupStat] = {}
        for row in rows:
            label = str(row[0])
            planned = int(row[1])
            valid = int(row[2])
            distribution = {
                value: int(row[3 + index])
                for index, value in enumerate(OPTION_VALUES)
            }
            by_label[label] = GroupStat(
                label=label,
                planned=planned,
                valid=valid,
                coverage=(valid / planned) if planned > 0 else 0.0,
                distribution=distribution,
            )

        ordered = [by_label[label] for label in order if label in by_label]
        return ordered, True

    @staticmethod
    def _report_revision(
        *,
        run: _RunMeta,
        status_counts: Mapping[str, int],
        option_counts: Mapping[str, int],
        age_groups: list[GroupStat],
        city_groups: list[GroupStat],
    ) -> str:
        """报告内容修订号（对聚合内容取 SHA-256，前缀 16 位十六进制）。

        同一份数据 → 同一修订号；数据变化 → 修订号变化，供「按相同 report_revision 比较」。
        """
        signature: dict[str, Any] = {
            "run_id": str(run.id),
            "status": run.status,
            "sample_size": int(run.sample_size),
            "status_counts": dict(sorted(status_counts.items())),
            "option_counts": dict(sorted(option_counts.items())),
            "age_groups": [group.model_dump() for group in age_groups],
            "city_groups": [group.model_dump() for group in city_groups],
        }
        blob = json.dumps(signature, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


#: 默认服务实例（无状态）。
report_service = ReportService()


__all__ = [
    "AGE_GROUPS",
    "CITY_GROUPS",
    "SIMULATION_LABEL",
    "BucketCount",
    "GroupStat",
    "ReportNotFoundError",
    "ReportService",
    "RunSummary",
    "report_service",
]
