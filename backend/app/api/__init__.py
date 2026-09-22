# SPDX-License-Identifier: GPL-3.0-or-later
"""api 包：HTTP 路由绑定与请求/响应契约（主文档 §8.2 / §9）。

**业务逻辑保留在 service 层**；本包只做参数解析、依赖注入、错误映射与序列化。
API 进程**不承载任何长任务**（worker 为独立进程）。
"""

from app.api.routes import api_router

__all__ = ["api_router"]
