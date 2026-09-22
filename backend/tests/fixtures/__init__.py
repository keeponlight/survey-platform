# SPDX-License-Identifier: GPL-3.0-or-later
"""测试 fixture 生成器包。"""

from tests.fixtures.generate_personas import (
    COLUMNS,
    DEFAULT_COLUMN_MAPPING,
    generate_persona_rows,
    write_personas_csv,
    write_personas_xlsx,
)

__all__ = [
    "COLUMNS",
    "DEFAULT_COLUMN_MAPPING",
    "generate_persona_rows",
    "write_personas_csv",
    "write_personas_xlsx",
]
