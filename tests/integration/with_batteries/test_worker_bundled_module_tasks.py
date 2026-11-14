"""The framework Celery worker entrypoint (``sandloom.core.worker``).

Pins D3: the worker is owned by the kernel (apps without ``identity`` still get
one), its Celery app comes from the taskqueue capability (never constructed
outside the driver), and module tasks / periodic tasks are discovered from the
LOADED module set only.

Moved from ``sandloom/core/tests/test_worker.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from sandloom.core import worker


class _Provider:
    def __init__(self, backend: Any, driver: str = "celery") -> None:
        self._backend = backend
        self.driver = driver

    async def raw_backend(self) -> Any:
        return self._backend

    def features(self) -> frozenset[str]:
        return frozenset({"delay", "periodic", "redelivery"})

    def register_task_table(self, table: Any, run: Any) -> None:
        from sandloom.core.drivers.celery_taskqueue import register_task_table

        register_task_table(self._backend, table, run)


class _Core:
    def __init__(self, backend: Any, modules: list[str]) -> None:
        self._provider = _Provider(backend)
        self.loaded_modules = modules
        self.requested: list[str] = []

    def taskqueue(self, name: str = "default") -> _Provider:
        self.requested.append(name)
        return self._provider


def _celery() -> Celery:
    return Celery("sandloom-worker-test", broker="memory://", backend="cache+memory://")


def test_worker_registers_loaded_module_tasks_and_beat_schedule() -> None:
    app = _celery()
    core = _Core(app, ["sandloom_identity", "sandloom_tenancy"])

    built = worker.build_worker_app(core)

    assert built is app
    assert core.requested == ["default"]
    app.finalize()
    assert "sandloom_identity.tasks.cleanup_expired_refresh_tokens" in app.tasks
    # The identity periodic tasks entered the schedule on finalize.
    schedule = app.conf.beat_schedule
    assert "identity.cleanup-revoked-refresh-tokens" in schedule
    assert "identity.cleanup-expired-refresh-tokens" in schedule
