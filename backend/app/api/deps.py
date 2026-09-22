# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI 依赖注入（会话 / 配置）。

引擎与会话工厂由 :func:`app.main.create_app` 在 lifespan 中挂到 ``app.state``，
测试可通过传参注入测试库会话工厂。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    """返回应用级会话工厂。"""
    return request.app.state.sessionmaker


def get_app_settings(request: Request) -> Settings:
    """返回应用级配置快照。"""
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """每请求一个会话；退出时回滚并关闭（写路径在路由内显式 ``commit``）。"""
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    session = maker()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


__all__ = ["get_app_settings", "get_session", "get_sessionmaker"]
