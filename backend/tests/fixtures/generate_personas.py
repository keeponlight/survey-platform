# SPDX-License-Identifier: GPL-3.0-or-later
"""可复现的用户表 fixture 生成器（开发 + 测试共用）。

生成确定性数据（不依赖全局 ``random``），用于：
- 20,000 行开发 fixture（主文档 T1：「另提供 20,000 人开发 fixture」）。
- 测试中构造 CSV / XLSX 表以验证导入、行号保持、无静默丢行。

列结构：``user_id, age, city, gender, note``。
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any

#: 生成器的列名（与 tests 中使用的 column_mapping 对应）。
COLUMNS: tuple[str, ...] = ("user_id", "age", "city", "gender", "note")

#: 默认列映射（供 import 使用）。
DEFAULT_COLUMN_MAPPING: dict[str, Any] = {
    "persona_id": "user_id",
    "profile_text_columns": ["age", "city", "gender", "note"],
    "age": "age",
    "city_tier": "city",
}

_CITIES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "成都",
    "杭州",
    "武汉",
    "西安",
    "南京",
    "重庆",
)
_GENDERS = ("女", "男")
_NOTES = (
    "偏好性价比，预算有限",
    "关注品质与售后",
    "已有同类替代品",
    "首次尝试该品类",
    "对价格敏感",
    "重视品牌口碑",
)


def generate_persona_rows(n: int, *, seed: int = 20260920) -> list[dict[str, Any]]:
    """生成 ``n`` 行确定性用户数据。

    ``row_no`` = 1..n（数据行序号，与主文档 §4 示例一致）。
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for index in range(1, n + 1):
        age = rng.randint(18, 70)
        rows.append(
            {
                "user_id": f"p_{index:06d}",
                "age": age,
                "city": _CITIES[index % len(_CITIES)],
                "gender": _GENDERS[index % len(_GENDERS)],
                "note": _NOTES[index % len(_NOTES)],
            }
        )
    return rows


def write_personas_csv(
    path: str | Path,
    n: int = 20_000,
    *,
    seed: int = 20260920,
    with_bom: bool = False,
) -> Path:
    """写出 CSV（UTF-8，可选带 BOM）。返回路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if with_bom else "utf-8"
    with target.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in generate_persona_rows(n, seed=seed):
            writer.writerow(row)
    return target


def write_personas_xlsx(
    path: str | Path,
    n: int = 20_000,
    *,
    seed: int = 20260920,
    sheet_name: str = "personas",
) -> Path:
    """写出 XLSX（单工作表）。返回路径。"""
    from openpyxl import Workbook

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    worksheet.append(list(COLUMNS))
    for row in generate_persona_rows(n, seed=seed):
        worksheet.append([row[column] for column in COLUMNS])
    workbook.save(target)
    return target


def _main() -> None:  # pragma: no cover - 手工开发入口
    """``python -m tests.fixtures.generate_personas`` 生成 20,000 行开发 fixture。"""
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic persona fixtures")
    parser.add_argument("--size", type=int, default=20_000)
    parser.add_argument("--out", default="fixtures/personas_20000.csv")
    parser.add_argument("--format", choices=("csv", "xlsx"), default="csv")
    parser.add_argument("--bom", action="store_true")
    args = parser.parse_args()

    if args.format == "xlsx":
        path = write_personas_xlsx(args.out, args.size)
    else:
        path = write_personas_csv(args.out, args.size, with_bom=args.bom)
    print(f"wrote {args.size} rows to {path}")


if __name__ == "__main__":  # pragma: no cover
    _main()
