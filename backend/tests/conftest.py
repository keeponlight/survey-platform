# SPDX-License-Identifier: GPL-3.0-or-later
"""pytest 全局配置与 fixture。

**硬约束（R3 / task-list.md §8.4 红线 #8 + 裁定 §7.6.4）：**
测试启动时在建立任何连接之后、跑任何用例之前，必须断言当前数据库名**以 ``survey_test`` 开头**，
否则以 :class:`RuntimeError` **fail fast**（挡住误连开发库/生产库）。

- 放宽为「前缀匹配」是为支持**有意隔离**的库（`survey_test_qa`、`survey_test_t7`），
  使并行复核 / T7 压测不必抢占主库；dev 库 `survey` 不以 `survey_test` 开头，仍被拒绝。
- §7.6.1 的独占规则继续有效：隔离是**显式申请**的例外。

测试始终打**真实 PostgreSQL**（不使用 SQLite）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import shutil
import sys
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_LOGGER = logging.getLogger(__name__)

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, set_settings  # noqa: E402

#: 测试库 DSN：显式环境变量优先，否则用配置默认（同实例不同 database）。
TEST_DATABASE_URL: str = (
    os.environ.get("TEST_DATABASE_URL") or Settings.from_env().test_database_url
)

#: 期望的测试数据库名**前缀**（裁定 §7.6.4：以 ``survey_test`` 开头即为合法测试库）。
EXPECTED_TEST_DATABASE = "survey_test"

#: 清库顺序（CASCADE 处理外键）。
_ALL_TABLES = "attempts, run_members, runs, surveys, imports"

#: 用例临时目录根（放在项目根之下，沙箱只允许写项目根）。
_PROJECT_TMP_ROOT = BACKEND_DIR / ".pytest_tmp"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _remove_tree_quietly(path: pathlib.Path) -> None:
    """删除用例临时目录，**绝不向外抛异常**。

    本机沙箱的 ``safe-delete`` bulk-guard（实现见 ``cli/vendor/shim/sitecustomize.py``）在
    删除数超阈值时会抛 :class:`SystemExit`。**``SystemExit`` 继承自 ``BaseException``，不是
    ``Exception``**，因此 ``shutil.rmtree(path, ignore_errors=True)`` 的内建 ``onerror``
    （只处理 ``OSError``）**吞不掉它**。

    若任其逃逸，会**中断 pytest 的 teardown 链**：同一用例后续 fixture 的 finalizer 被跳过
    （``db_session`` 的清库即被跳过）→ DB 残留数据/残留活动批次 → **级联污染无关用例**
    （裁定 §7.10 已实测）。

    故此处显式吞掉 ``BaseException``，保证清理失败**不影响测试正确性**；仅记一条警告日志。
    **不改变清理目标路径、不依赖任何环境变量**（红线：不许用 TMPDIR 走守卫豁免换全绿）。
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException:  # noqa: BLE001 - 必须吞掉 SystemExit 等 BaseException，见上 docstring
        _LOGGER.warning("tmp_path cleanup failed for %s (ignored)", path, exc_info=True)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> pathlib.Path:
    """把 pytest 的 ``tmp_path`` 重定向到项目根下的 ``backend/.pytest_tmp``。

    本机沙箱只允许写项目根之下，系统临时目录会被拒绝；此覆盖保证测试可复现。
    清理走 :func:`_remove_tree_quietly`，**绝不抛异常**，以免中断 teardown 链（§7.10）。
    """
    safe = _SAFE_NAME.sub("_", request.node.name)[:80]
    path = _PROJECT_TMP_ROOT / f"{safe}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        _remove_tree_quietly(path)


async def _fetch_database_name(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT current_database()"))
            return str(result.scalar_one())
    finally:
        await engine.dispose()


def _database_name_from_url(url: str) -> str:
    """从 DSN 解析数据库名（``…/<dbname>?params`` → ``<dbname>``）。"""
    path = url.split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1]


def _run_alembic_upgrade(url: str) -> None:
    """以编程方式把测试库升级到 head（幂等）。"""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def prepared_test_db() -> None:
    """整个测试会话的前置：校验测试库 + 迁移到 head。"""
    if not _database_name_from_url(TEST_DATABASE_URL).startswith(EXPECTED_TEST_DATABASE):
        raise RuntimeError(
            f"TEST_DATABASE_URL must point at a database whose name starts with "
            f"{EXPECTED_TEST_DATABASE!r}, got {TEST_DATABASE_URL!r}"
        )

    database_name = asyncio.run(_fetch_database_name(TEST_DATABASE_URL))
    if not database_name.startswith(EXPECTED_TEST_DATABASE):
        raise RuntimeError(
            "tests must run against an isolated test database whose name starts with "
            f"{EXPECTED_TEST_DATABASE!r}, but current_database() = {database_name!r} "
            f"(TEST_DATABASE_URL={TEST_DATABASE_URL!r})"
        )

    _run_alembic_upgrade(TEST_DATABASE_URL)

    # 确保应用层的默认 DSN 也指向测试库，避免测试误连开发库。
    set_settings(
        replace(
            Settings.from_env(),
            database_url=TEST_DATABASE_URL,
            test_database_url=TEST_DATABASE_URL,
        )
    )
    yield


@pytest_asyncio.fixture
async def engine(prepared_test_db: None) -> AsyncEngine:
    """每个用例一个独立引擎（用完释放）。"""
    test_engine = create_async_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=10)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


async def _truncate_all(engine: AsyncEngine) -> None:
    """清空全部业务表（``CASCADE`` 处理外键、``RESTART IDENTITY`` 复位序列）。

    用例隔离的基础（§7.6），**绝不删除**。任何清库失败都应如实抛出（那是真实的库层问题）。
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(f"TRUNCATE TABLE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncSession:
    """每用例一个会话；**起点**与**终点**都清库，保证用例隔离（裁定 §7.10）。

    为什么两端都清：
    - pytest 的 fixture teardown 按 setup 逆序执行；**任一** finalizer 抛出 ``BaseException``
      都会中断整条 teardown 链，使排在更早 setup 的 fixture 的 finalizer 被**跳过**。
      ``db_session`` 通常排在用例签名里 ``tmp_path`` 之前 → 它 setup 更早、teardown 更晚
      → 一旦 ``tmp_path`` 的清理抛 ``SystemExit``（沙箱 bulk-guard），本 fixture 的终点清库
      就会被跳过、DB 残留。**终点清库无法独自抵御这种兄弟 fixture 中断。**
    - 因此**起点也清一次**：即便上一用例的 teardown 曾被中断，本用例仍在干净 DB 上开始
      ——这是「无论前面发生什么，每个用例都从干净库开始」的**结构性保证**。
    - 终点清库包在本 fixture **最外层** ``try/finally``：即使 ``rollback``/``close`` 出错，
      清库仍会执行（两步各自独立兜底，见下）。
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()
    # 起点清库：兜底抵御「上一用例 teardown 被兄弟 fixture 的 BaseException 中断」的情形。
    await _truncate_all(engine)
    try:
        yield session
    finally:
        try:
            await session.rollback()
        except BaseException:  # 清理失败绝不中断后续步骤
            _LOGGER.warning("db_session rollback failed (ignored)", exc_info=True)
        finally:
            try:
                await session.close()
            except BaseException:
                _LOGGER.warning("db_session close failed (ignored)", exc_info=True)
            finally:
                # 最外层 finally：清库永远执行（§7.10 的核心不变量）。
                await _truncate_all(engine)
