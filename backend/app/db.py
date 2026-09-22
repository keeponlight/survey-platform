# SPDX-License-Identifier: GPL-3.0-or-later
"""数据库会话、事务与连接池（SQLAlchemy 2.x async + asyncpg）。

- 任务唯一事实源 = PostgreSQL（不使用 SQLite，测试亦如此）。
- **连接池上限必须低于 PostgreSQL 的 ``max_connections``**（默认 100）。api 与 worker
  各建一个引擎；100 路**模型**并发并不需要在途 100 条 DB 连接——真正的长耗时在模型调用，
  而它**不在任何事务/连接内**（仅领取与落库是短事务）。故每引擎 30 条（10+20）足够，
  api+worker 合计 60 < 100，留出余量。若沿用旧的 20+100=120/引擎，两引擎峰值会打爆
  ``max_connections`` → ``asyncpg TooManyConnectionsError`` → worker 领取/落库全线失败
  （N7 实测发现：100 成员批次跑到一半即卡死）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

#: 连接池规模：与 ``tests/`` 内的既有取值（pool_size=10）对齐；两个引擎合计 60 条，
#: 低于 PostgreSQL ``max_connections`` 默认值 100，避免 ``TooManyConnectionsError``。
DEFAULT_POOL_SIZE = 10
#: 溢出连接上限：10+20=30/引擎；模型调用**不占**连接，故 30 条足以支撑 100 路并发领取/落库。
DEFAULT_MAX_OVERFLOW = 20


def create_engine(
    url: str | None = None,
    *,
    echo: bool = False,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
) -> AsyncEngine:
    """创建异步引擎（``url`` 为空时用 :func:`app.config.get_settings` 的开发 DSN）。"""
    dsn = url or get_settings().database_url
    return create_async_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        future=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """构造会话工厂（``expire_on_commit=False``，便于读回已提交对象）。"""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """事务作用域：正常提交，异常回滚。"""
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def current_database(session: AsyncSession) -> str:
    """返回当前连接所在数据库名（测试库隔离断言用）。"""
    result = await session.execute(text("SELECT current_database()"))
    return str(result.scalar_one())


__all__ = [
    "DEFAULT_MAX_OVERFLOW",
    "DEFAULT_POOL_SIZE",
    "create_engine",
    "create_sessionmaker",
    "current_database",
    "session_scope",
]
