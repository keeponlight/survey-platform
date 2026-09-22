# SPDX-License-Identifier: GPL-3.0-or-later
"""允许的控制动作（服务端下发，前端不自行推断；主文档 §8.1 / architecture.md §3.1）。

``allowed_actions`` 由 run 当前状态决定（**纯状态机判定**）。前端按返回的列表启用按钮；
真正执行时服务端仍做一次合法转移校验（非法 → 409）。
"""

from __future__ import annotations

#: 各 run 状态允许的控制动作（顺序即展示优先级）。
#:
#: - ``start``：``ready`` 显式启动（``paused`` 须先 ``resume``，不在此列）。
#: - ``retry-failed``：仅 ``completed_with_errors`` / ``failed``。
#: - ``increase-budget``：非终态且可继续运行的状态（``ready``/``running``/``paused``）。
#: - 已达目标的重复 ``pause``/``cancel`` 由服务端返回 200（不在此表体现）。
ALLOWED_ACTIONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    "ready": ("start", "cancel", "increase-budget"),
    "running": ("pause", "cancel", "increase-budget"),
    "pausing": ("cancel",),
    "paused": ("resume", "cancel", "increase-budget"),
    "cancelling": (),
    "cancelled": (),
    "completed": (),
    "completed_with_errors": ("retry-failed",),
    "failed": ("retry-failed",),
}


def allowed_actions_for(status: str) -> list[str]:
    """返回某 run 状态下允许的动作列表（未知状态 → 空列表）。"""
    return list(ALLOWED_ACTIONS_BY_STATUS.get(status, ()))


__all__ = ["ALLOWED_ACTIONS_BY_STATUS", "allowed_actions_for"]
