# SPDX-License-Identifier: GPL-3.0-or-later
"""tests/load —— 万级 mock 压测与验收脚本（主文档 §10 T3/T7）。

本目录位于**项目根**（非 ``backend/tests``）。``mock_provider`` 通过把本目录加入
``sys.path`` 的方式被后台测试以顶层模块名 ``mock_provider`` 导入，从而避免与
``backend/tests`` 这个名为 ``tests`` 的包发生命名冲突。
"""

from __future__ import annotations

__all__: list[str] = []
