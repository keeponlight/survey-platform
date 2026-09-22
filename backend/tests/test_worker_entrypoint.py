# SPDX-License-Identifier: GPL-3.0-or-later
"""裁决 N7-d 验收测试：worker 进程入口（``python -m app.worker.main``）。

背景（主理人实证）：``app/worker/main.py`` 此前**无任何入口**，``python -m app.worker.main``
只导入模块后立刻退出（``exit_code=0``，无 worker 日志）→ 核心执行引擎根本无法启动。
本文件以**子进程**方式真跑该命令，断言它**真的在跑**、第二个 worker 抢锁失败、
SIGTERM 后**干净退出并释放 advisory lock**。

隔离：用 ``TEST_DATABASE_URL`` 指向**隔离库**（``survey_test_qa``），受 §7.6.1 独占规则与
§7.6.4 前缀放宽约束。**不**触碰 ``survey_test``（属他人独占窗口）。

验收命令：
``TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/survey_test_qa \\
  uv run pytest tests/test_worker_entrypoint.py -q``
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.worker.main import ADVISORY_LOCK_KEY

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

#: 测试库 DSN（与 conftest 同优先级：显式环境变量优先，否则配置默认）。
TEST_DATABASE_URL: str = (
    os.environ.get("TEST_DATABASE_URL") or Settings.from_env().test_database_url
)

#: 启动日志标记（``run_worker`` 抢锁成功后打印）。
STARTUP_MARKER = "worker started:"

#: 抢锁失败日志标记。
LOCK_FAILURE_MARKER = "another worker already holds advisory lock"

#: 子进程整体超时（秒）。
_STARTUP_TIMEOUT = 25.0
_EXIT_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# 子进程封装（后台线程读取合并后的 stdout/stderr）
# ---------------------------------------------------------------------------


class WorkerProcess:
    """一个以 ``python -m app.worker.main`` 启动的 worker 子进程。"""

    def __init__(self) -> None:
        env = dict(os.environ)
        env["DATABASE_URL"] = TEST_DATABASE_URL
        env["TEST_DATABASE_URL"] = TEST_DATABASE_URL
        env["MODEL_PROVIDER"] = "mock"
        env["AGENT_CONCURRENCY"] = "100"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("MODEL_RPM", "100000")
        env.setdefault("MODEL_TPM", "100000000")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker.main"],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line.rstrip("\n"))

    @property
    def output(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def wait_for(self, needle: str, timeout: float = _STARTUP_TIMEOUT) -> bool:
        """等待输出中出现 ``needle``；进程提前退出则立即返回最终判定。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.output:
                return True
            if self.proc.poll() is not None:
                return needle in self.output
            time.sleep(0.05)
        return needle in self.output

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=_EXIT_TIMEOUT)


@pytest.fixture
def worker_factory():
    """启动 worker 子进程的工厂；测试结束**务必**清理，避免泄漏进程污染后续用例。"""
    procs: list[WorkerProcess] = []

    def start() -> WorkerProcess:
        process = WorkerProcess()
        procs.append(process)
        return process

    yield start

    for process in procs:
        process.terminate()


# ---------------------------------------------------------------------------
# (a) 子进程真的在跑，且打印启动日志
# ---------------------------------------------------------------------------


def test_worker_entrypoint_stays_alive_and_logs_startup(worker_factory) -> None:
    """``python -m app.worker.main`` 数秒内**不退出**（真的在跑，而非 exit 0 空转）。"""
    worker = worker_factory()
    assert worker.wait_for(STARTUP_MARKER), f"startup log not found:\n{worker.output}"

    # 再观察一段时间，证明确实在持续运行（不是导入后立刻退出）。
    time.sleep(3.0)
    assert worker.is_alive(), f"worker exited unexpectedly:\n{worker.output}"

    output = worker.output
    assert "advisory_lock_held=True" in output, output
    assert "concurrency=100" in output, output
    # 限流参数应出现在启动日志中。
    assert "rpm=" in output and "tpm=" in output, output


# ---------------------------------------------------------------------------
# (b) 第二个并发 worker 抢锁失败 → 非 0 退出 + 明确日志
# ---------------------------------------------------------------------------


def test_second_worker_fails_to_acquire_lock(worker_factory) -> None:
    """第二个 worker 进程抢不到 advisory lock → 明确日志 + 非 0 退出码。"""
    first = worker_factory()
    assert first.wait_for(STARTUP_MARKER), f"first worker did not start:\n{first.output}"

    second = worker_factory()
    exit_code = second.proc.wait(timeout=_EXIT_TIMEOUT)

    assert exit_code != 0, f"second worker should exit non-zero, got {exit_code}"
    assert LOCK_FAILURE_MARKER in second.output, second.output

    # 第一个 worker 仍持有锁并继续运行。
    assert first.is_alive(), first.output


# ---------------------------------------------------------------------------
# (c) SIGTERM → 干净退出（0）且释放 advisory lock
# ---------------------------------------------------------------------------


async def test_sigterm_exits_zero_and_releases_lock(worker_factory) -> None:
    """SIGTERM → 退出码 0，且 advisory lock 被释放（另一会话能重新拿到）。"""
    worker = worker_factory()
    assert worker.wait_for(STARTUP_MARKER), f"worker did not start:\n{worker.output}"

    worker.proc.send_signal(signal.SIGTERM)
    exit_code = worker.proc.wait(timeout=_EXIT_TIMEOUT)
    assert exit_code == 0, f"expected clean exit 0, got {exit_code}:\n{worker.output}"

    # 锁已释放：一个全新会话能拿到同一 advisory lock。
    probe = create_async_engine(TEST_DATABASE_URL)
    try:
        async with probe.connect() as connection:
            acquired = (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
                )
            ).scalar()
            assert acquired is True, "advisory lock still held after SIGTERM"
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
    finally:
        await probe.dispose()
