"""Per-worker-process event loop for synchronous task bodies (``run_async``).

Celery task bodies used ``asyncio.run`` per task, while the async engine pools
are process-wide and loop-bound: the 2nd task in a worker child got a pooled
connection whose loop ``asyncio.run`` had already closed. ``run_async`` keeps
ONE loop per process (fresh after fork via ``worker_process_init``, engines
disposed on it at ``worker_process_shutdown``).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from celery import Celery, signals

from sandloom.core import async_runner, worker
from sandloom.core.di import run_async as di_run_async
from sandloom.core.testing import worker_env_values

_BACKEND = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _fresh_runner() -> Any:
    async_runner.shutdown_loop()
    saved = list(async_runner._cleanups)
    async_runner._cleanups.clear()
    yield
    async_runner.shutdown_loop()
    async_runner._cleanups[:] = saved


async def _loop_id() -> int:
    return id(asyncio.get_running_loop())


def test_di_reexports_the_runner() -> None:
    assert di_run_async is async_runner.run_async


def test_sequential_calls_share_one_persistent_loop() -> None:
    first = async_runner.run_async(_loop_id())
    second = async_runner.run_async(_loop_id())
    assert first == second
    assert async_runner._loop is not None and not async_runner._loop.is_closed()


def test_inherited_loop_is_replaced_after_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    parent_loop = async_runner.run_async(_loop_id())
    child_pid = os.getpid() + 1
    monkeypatch.setattr(async_runner.os, "getpid", lambda: child_pid)
    assert async_runner.run_async(_loop_id()) != parent_loop


def test_worker_process_init_resets_the_loop() -> None:
    async_runner.install_celery_hooks()
    before = async_runner.run_async(_loop_id())
    signals.worker_process_init.send(sender=None)
    assert async_runner.run_async(_loop_id()) != before


def test_shutdown_runs_cleanups_on_the_worker_loop_then_closes_it() -> None:
    seen: list[int] = []

    async def cleanup() -> None:
        seen.append(id(asyncio.get_running_loop()))

    async_runner.add_loop_cleanup(cleanup)
    async_runner.add_loop_cleanup(cleanup)  # idempotent registration
    loop_id = async_runner.run_async(_loop_id())
    loop = async_runner._loop
    async_runner.install_celery_hooks()

    signals.worker_process_shutdown.send(sender=None)

    assert seen == [loop_id]
    assert loop is not None and loop.is_closed()
    async_runner.shutdown_loop()  # idempotent


def test_dispatcher_is_drained_before_worker_returns_and_disposes_infra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.unit_of_work import dispatch as unit_of_work_dispatch

    calls: list[str] = []

    async def drain(timeout: float | None = None) -> None:
        assert timeout is not None and timeout > 0
        calls.append("drain")

    async def work() -> str:
        calls.append("work")
        return "result"

    def dispose() -> None:
        calls.append("dispose")

    monkeypatch.setattr(unit_of_work_dispatch, "drain_post_commit", drain)
    async_runner.add_loop_cleanup(dispose)
    assert async_runner.run_async(work()) == "result"
    assert calls == ["work", "drain"]
    async_runner.shutdown_loop()
    assert calls == ["work", "drain", "drain", "dispose"]


def test_failing_cleanup_does_not_block_the_others() -> None:
    ran: list[str] = []

    async def boom() -> None:
        raise RuntimeError("unreachable backend")

    def sync_cleanup() -> None:
        ran.append("sync")

    async_runner.add_loop_cleanup(sync_cleanup)
    async_runner.add_loop_cleanup(boom)
    async_runner.run_async(_loop_id())
    async_runner.shutdown_loop()
    assert ran == ["sync"]


async def test_run_async_inside_a_running_loop_is_rejected() -> None:
    coro = _loop_id()
    with pytest.raises(RuntimeError, match="await"):
        async_runner.run_async(coro)


def test_build_worker_app_wires_loop_hooks_and_engine_disposal() -> None:
    class _Provider:
        driver = "celery"

        def __init__(self, app: Celery) -> None:
            self._app = app

        def raw_backend(self) -> Celery:
            return self._app

    class _Core:
        loaded_modules: list[str] = []

        def __init__(self, app: Celery) -> None:
            self._p = _Provider(app)

        def taskqueue(self, name: str = "default") -> _Provider:
            return self._p

    app = Celery("sandloom-runner-test", broker="memory://")
    worker.build_worker_app(_Core(app))

    assert worker.dispose_worker_infra in async_runner._cleanups
    assert async_runner._hooks_installed


def test_dispose_worker_infra_runs_the_core_shutdown_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown reuses the composition root's shutdown tasks, on the loop."""
    ran: list[int] = []

    async def close_engine() -> None:
        ran.append(id(asyncio.get_running_loop()))

    class _Core:
        shutdown_tasks = [close_engine]

    monkeypatch.setattr(worker, "_teardown_core", _Core())
    async_runner.add_loop_cleanup(worker.dispose_worker_infra)
    loop_id = async_runner.run_async(_loop_id())
    async_runner.shutdown_loop()
    assert ran == [loop_id]


# --------------------------------------------------------------------------- #
# Real PostgreSQL: two task bodies, one worker process
# --------------------------------------------------------------------------- #

_TASK_SCRIPT = """
import asyncio, sys
from sqlalchemy import text

import sandloom.main  # composition root: create_app() binds the active core
from sandloom.core import async_runner
from sandloom.core.di import db_session, run_async


async def task_body():
    async with db_session() as session:
        return (await session.execute(text("select pg_backend_pid()"))).scalar_one()


runner = run_async if sys.argv[1] == "run_async" else asyncio.run
first = runner(task_body())
second = runner(task_body())
print("PIDS", first, second)
if sys.argv[1] == "run_async":
    # worker_process_shutdown: dispose the pools on the loop that opened them
    from sandloom.core import worker
    worker._teardown_core = worker.load_composition_core("sandloom.main")
    async_runner.add_loop_cleanup(worker.dispose_worker_infra)
    async_runner.shutdown_loop()
    print("CLOSED")
"""


def _run_task_script(mode: str) -> subprocess.CompletedProcess[str]:
    env_test = worker_env_values(_BACKEND / ".env.test")
    env = {**os.environ, **env_test}
    return subprocess.run(
        [sys.executable, "-c", _TASK_SCRIPT, mode],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_two_sequential_task_bodies_share_the_pool_with_run_async() -> None:
    proc = _run_task_script("run_async")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("PIDS"))
    _, first, second = line.split()
    # Same backend pid -> the 2nd task reused the 1st task's pooled connection
    # on the same loop (no reconnect, no cross-loop failure).
    assert first == second
    assert "CLOSED" in proc.stdout
    assert "Shutdown task failed" not in proc.stderr, proc.stderr
    assert "CLOSED" in proc.stdout
    assert "Shutdown task failed" not in proc.stderr, proc.stderr


def test_asyncio_run_per_task_breaks_the_loop_bound_pool() -> None:
    """Control: the pattern ``run_async`` replaces fails on the 2nd task."""
    proc = _run_task_script("asyncio_run")
    assert proc.returncode != 0, proc.stdout
    assert "loop" in proc.stderr.lower(), proc.stderr
