# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP 服务装配（主文档 §3 / §9；节点 N5）。

- 统一前缀 ``/api/v1``；错误体 ``{"code","message","details"}``。
- 引擎/会话工厂在 **lifespan** 中创建（导入本模块**不**触碰数据库）。
- **本进程只承载 HTTP**；100 槽位长任务由独立 worker 进程承担
  （``python -m app.worker.main``），二者复用同一镜像、启动命令不同（裁决 C2）。

启动：``uvicorn app.main:app``。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import register_exception_handlers
from app.api.routes import api_router
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker

#: API 统一前缀（写死的契约，照挂）。
API_PREFIX = "/api/v1"


def create_app(
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """装配 FastAPI 应用。

    - ``sessionmaker`` 为空时在 lifespan 中按 ``settings.database_url`` 自建引擎；
      测试可注入指向 ``survey_test`` 的会话工厂。
    - ``settings`` 为空时取进程级单例（:func:`app.config.get_settings`）。
    """
    active_settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if sessionmaker is not None:
            app.state.sessionmaker = sessionmaker
            app.state.engine = None
        else:
            engine = create_engine(active_settings.database_url)
            app.state.engine = engine
            app.state.sessionmaker = create_sessionmaker(engine)
        app.state.settings = active_settings
        try:
            yield
        finally:
            owned_engine = getattr(app.state, "engine", None)
            if owned_engine is not None:
                await owned_engine.dispose()

    app = FastAPI(
        title="万级智能体模拟问卷平台 API",
        version="0.1.0",
        description=(
            "输入现有用户表 → 100 个 Agent 并行槽位逐人调用第三方模型 → "
            "输出逐人模拟购买意向表。**结果为模拟购买意向，不是真实转化率。**\n\n"
            "**口径提示（row_no）**：`row_no` 是**数据行序号**（从 1 起、**不含表头**），"
            "比 Excel 工作表物理行号小 1（物理行号 = `row_no + 1`）。"
        ),
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)
    return app


#: 进程级应用（``uvicorn app.main:app``）。
app = create_app()


__all__ = ["API_PREFIX", "app", "create_app"]
