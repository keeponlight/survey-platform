# SPDX-License-Identifier: GPL-3.0-or-later
"""reports 包：统计聚合与 CSV 导出（主文档 §8.3 / architecture.md §1）。

- :mod:`app.reports.service` —— **只做 SQL 聚合**的统计服务（一致性读事务）。
- :mod:`app.reports.export`  —— 逐人 CSV 流式导出（UTF-8 BOM、公式前缀转义）。

**红线（task-list.md §8.4 #7）：ReportService 只做 SQL 聚合，禁止调用 LLM 计算百分比。**
"""

from app.reports.export import (
    CSV_BOM,
    CSV_FIELDS,
    escape_spreadsheet_prefix,
    export_csv_bytes,
    iter_export_rows,
    member_export_cells,
)
from app.reports.service import (
    AGE_GROUPS,
    CITY_GROUPS,
    BucketCount,
    GroupStat,
    ReportService,
    RunSummary,
    report_service,
)

__all__ = [
    "AGE_GROUPS",
    "CITY_GROUPS",
    "CSV_BOM",
    "CSV_FIELDS",
    "BucketCount",
    "GroupStat",
    "ReportService",
    "RunSummary",
    "escape_spreadsheet_prefix",
    "export_csv_bytes",
    "iter_export_rows",
    "member_export_cells",
    "report_service",
]
