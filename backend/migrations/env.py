"""Alembic 环境（异步 asyncpg 引擎）。

- URL 解析优先级：``config.get_main_option("sqlalchemy.url")`` > ``Settings.database_url``。
- 测试通过 ``cfg.set_main_option("sqlalchemy.url", TEST_DSN)`` 覆盖，从而迁移到 ``survey_test``。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False（P2-1 修复）：fileConfig 默认值为 True，会把所有
    # **未在 alembic.ini 中声明**的既有 logger（含 ``app.worker`` / ``app.api``）置
    # ``disabled=True``，导致「第二个 worker 必须留明确日志」等验收证据被**静默吞掉**。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # noqa: ANN001 - Alembic 回调签名
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), future=True)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
