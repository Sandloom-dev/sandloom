"""The framework worker boot (``sandloom.core.worker``) with the bundled modules.

Pins D3: the worker is owned by the kernel (apps without ``identity`` still get
one), its host comes from the taskqueue capability (never constructed outside
the driver), and module tasks / periodic tasks are discovered from the LOADED
module set only.

Moved from ``sandloom/core/tests/test_worker.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from sandloom_celery.worker import CeleryWorkerHost, register_task_table

from sandloom.core import worker


class _Provider:
    def __init__(self, app: Any, driver: str = "celery") -> None:
        self._app = app
        self.driver = driver
        self.options: dict[str, Any] = {}

    def worker_host(self) -> CeleryWorkerHost:
        return CeleryWorkerHost(lambda: self._app)

    def features(self) -> frozenset[str]:
        return frozenset({"delay", "periodic", "redelivery"})


class _Core:
    def __init__(self, app: Any, modules: list[str]) -> None:
        self._provider = _Provider(app)
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

    provider, host = worker.resolve_worker_host(core, "default")
    boot = worker.KernelWorkerBoot(core, provider, "default")
    assert isinstance(host, CeleryWorkerHost)
    register_task_table(host._app, boot.boot(), boot.run)

    assert host._app is app
    assert core.requested == ["default"]
    app.finalize()
    assert "sandloom_identity.tasks.cleanup_expired_refresh_tokens" in app.tasks
    # The identity periodic tasks entered the schedule on finalize.
    schedule = app.conf.beat_schedule
    assert "identity.cleanup-revoked-refresh-tokens" in schedule
    assert "identity.cleanup-expired-refresh-tokens" in schedule
