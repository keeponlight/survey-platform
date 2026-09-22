# SPDX-License-Identifier: GPL-3.0-or-later
"""personas 包：用户表导入与画像快照（主文档 §3 PersonaSource / §4）。"""

from app.personas.source import (
    ImportPreview,
    InvalidUserTableError,
    ParsedTable,
    build_preview,
    build_snapshots,
    create_import,
    load_snapshots,
    parse_user_table,
)

__all__ = [
    "ImportPreview",
    "InvalidUserTableError",
    "ParsedTable",
    "build_preview",
    "build_snapshots",
    "create_import",
    "load_snapshots",
    "parse_user_table",
]
