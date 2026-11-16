"""The kernel side of the worker (``sandloom.core.worker``).

Pins D3: the worker boot is owned by the kernel (apps without ``identity`` still
get one), the process that runs messages is the taskqueue driver's host
(``TaskQueueProvider.worker_host()``, never constructed outside the driver), and
module tasks are discovered from the LOADED module set only. The kernel imports
no task-queue backend; the Celery host is tested in ``test_worker_port.py``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import async_runner, worker
from sandloom.core.infra.ports.taskqueue import PeriodicSpec, TaskSpec, TaskTable


class _Host:
    """A worker host that records what the kernel hands it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def run_worker(self, boot: Any, **kwargs: Any) -> int:
        self.calls.append(("worker", kwargs))
        return 7


class _Provider:
    def __init__(
        self,
        host: _Host | None = None,
        driver: str = "fake",
        features: frozenset[str] = frozenset({"delay", "periodic", "redelivery"}),
    ) -> None:
        self.host = host or _Host()
        self.driver = driver
        self._features = features
        self.options: dict[str, Any] = {}

    def worker_host(self) -> _Host:
        return self.host

    def features(self) -> frozenset[str]:
        return self._features


class _Core:
    def __init__(self, provider: Any, modules: list[str]) -> None:
        self.provider = provider
        self.loaded_modules = modules
        self.requested: list[str] = []

    def taskqueue(self, name: str = "default") -> Any:
        self.requested.append(name)
        return self.provider


def test_import_has_no_side_effects() -> None:
    """``import sandloom.core.worker`` must not boot the composition root and
    must not import a task-queue backend: the kernel runs without Celery."""
    probe = (
        "import sys, sandloom.core.worker;"
        "assert 'celery' not in sys.modules, 'celery imported';"
        "assert 'kombu' not in sys.modules, 'kombu imported';"
        "assert 'sandloom_standard.main' not in sys.modules"
    )
    env = {
        name: os.environ[name]
        for name in os.environ
        if name.startswith(("SANDLOOM_", "DATABASE_", "CACHE_", "BROKER_", "JWT_"))
        or name in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}
    }
    done = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]


def test_worker_without_identity_has_no_identity_tasks() -> None:
    """A deployment that does not load identity gets a worker without it."""
    core = _Core(_Provider(), ["sandloom_tenancy"])
    table = worker.KernelWorkerBoot(core, core.provider, "default").boot()
    # (the task table of each worker holds the loaded modules only)
    assert not any(name.startswith("sandloom_identity.") for name in table.tasks)
    assert not any(entry.id.startswith("identity.") for entry in table.periodic)


def test_worker_honours_taskqueue_instance_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(worker.TASKQUEUE_INSTANCE_ENV, "jobs")
    core = _Core(_Provider(), [])
    assert worker.run_worker(core=core) == 7
    assert core.requested == ["jobs"]


def test_a_taskqueue_without_a_worker_host_is_rejected() -> None:
    class _NoHost:
        driver = "bare"

    core = _Core(_NoHost(), [])
    with pytest.raises(worker.WorkerConfigurationError, match="worker host"):
        worker.run_worker(core=core)


def test_run_worker_hands_the_host_its_queues_and_size() -> None:
    core = _Core(_Provider(), [])
    assert (
        worker.run_worker(queues=["mail"], concurrency=3, loglevel="DEBUG", core=core)
        == 7
    )
    ((kind, kwargs),) = core.provider.host.calls
    assert kind == "worker"
    assert kwargs == {"queues": ["mail"], "concurrency": 3, "loglevel": "DEBUG"}


def test_discover_skips_modules_without_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a module of its own: a bundled module may gain tasks (an app's change)
    (tmp_path / "sandloom_probe_no_tasks").mkdir()
    (tmp_path / "sandloom_probe_no_tasks" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = worker.discover_task_modules(
        ["sandloom_probe_no_tasks", "sandloom.no_such_module"]
    )
    assert found == []


def test_composition_root_must_expose_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fake_root_no_core", SimpleNamespace())
    with pytest.raises(
        worker.WorkerConfigurationError, match="SANDLOOM_COMPOSITION_ROOT"
    ):
        worker.load_composition_core("fake_root_no_core")


# The host a taskqueue driver builds, end to end against the suite's root: the
# Celery package's ``test_celery_worker_composition.py``.


# --------------------------------------------------------------------------- #
# Container-aware sizing (split-review finding #1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cpus,expected", [(0.25, 1), (0.5, 1), (1.0, 1), (1.5, 2), (16.0, 2)]
)
def test_default_concurrency_follows_cpu_quota_and_is_capped(
    cpus: float, expected: int
) -> None:
    assert worker.default_worker_concurrency(cpus) == expected


def test_sizing_never_falls_back_to_host_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefork pool's own default is os.cpu_count() (the HOST): 10 children
    in a 384 MiB container OOM-looped. The framework always sets it."""
    from sandloom.core import cpu_quota

    monkeypatch.setattr(cpu_quota, "available_cpus", lambda: 10.0)
    assert worker.worker_concurrency({}) == worker.MAX_DEFAULT_WORKER_CONCURRENCY


def test_sizing_env_overrides() -> None:
    assert worker.worker_concurrency({"WORKER_CONCURRENCY": "3"}) == 3


@pytest.mark.parametrize("raw", ["0", "-1", "two"])
def test_sizing_rejects_bad_values(raw: str) -> None:
    with pytest.raises(worker.WorkerConfigurationError, match="WORKER_CONCURRENCY"):
        worker.worker_concurrency({"WORKER_CONCURRENCY": raw})


def test_run_worker_sizes_the_host_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_CONCURRENCY", "1")
    core = _Core(_Provider(), [])
    worker.run_worker(core=core)
    ((_, kwargs),) = core.provider.host.calls
    assert kwargs["concurrency"] == 1
    # an explicit flag wins over the environment
    worker.run_worker(concurrency=2, core=core)
    assert core.provider.host.calls[-1][1]["concurrency"] == 2


def test_the_old_healthcheck_entry_is_gone() -> None:
    """``python -m sandloom.core.worker healthcheck`` became ``sandloom worker check``
    (``test_cli_worker``); no shim keeps the old one."""
    assert not hasattr(worker, "main")


@pytest.mark.subprocess_probe
def test_the_old_probe_command_fails_and_names_the_replacement(
    tmp_path: Path,
) -> None:
    """An upgrader who kept ``python -m sandloom.core.worker healthcheck`` must not
    get a liveness probe that passes: the module run exits non-zero and says what
    replaced it (a tombstone, not an alias: it runs nothing)."""
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")
        if name in os.environ
    }
    env["WORKER_HEARTBEAT_FILE"] = str(tmp_path / "heartbeat")
    done = subprocess.run(
        [sys.executable, "-m", "sandloom.core.worker", "healthcheck"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode != 0
    assert "sandloom worker check" in done.stderr


class _RlsProvider:
    def __init__(self, features: set[str], error: Exception | None = None) -> None:
        self._features = frozenset(features)
        self._error = error
        self.verified = 0

    def features(self) -> frozenset[str]:
        return self._features

    async def verify_tenant_rls(self) -> None:
        self.verified += 1
        if self._error is not None:
            raise self._error


class _Infra:
    def __init__(self, providers: dict[str, _RlsProvider]) -> None:
        self._providers = providers

    def instances(self, capability: str) -> list[str]:
        return list(self._providers) if capability == "database" else []

    def provider(self, capability: str, name: str) -> _RlsProvider:
        return self._providers[name]


def test_worker_boot_runs_tenant_rls_role_verification() -> None:
    """The worker never enters the ASGI lifespan: it verifies RLS roles itself."""
    from sandloom.core.sql_alchemy.tenant_rls import TenantRLSConfigError

    rls = _RlsProvider({"tenant_rls"})
    plain = _RlsProvider(set())
    worker.verify_worker_infra(
        SimpleNamespace(infra=_Infra({"default": rls, "analytics": plain}))
    )
    assert (rls.verified, plain.verified) == (1, 0)

    bad = _RlsProvider({"tenant_rls"}, TenantRLSConfigError("role is a SUPERUSER"))
    with pytest.raises(TenantRLSConfigError, match="SUPERUSER"):
        worker.verify_worker_infra(SimpleNamespace(infra=_Infra({"default": bad})))
    # A core without an infra registry (tests, fakes) is a no-op.
    worker.verify_worker_infra(SimpleNamespace())


# --------------------------------------------------------------------------- #
# The table against the driver's features (SANDLOOM-B037)
# --------------------------------------------------------------------------- #
def _table(*, at_least_once: bool = False, periodic: bool = False) -> Any:
    return TaskTable(
        tasks={"m.tasks.job": TaskSpec(at_least_once=at_least_once)},
        periodic=[PeriodicSpec(id="m.job", cron="* * * * *", task="m.tasks.job")]
        if periodic
        else [],
    )


def _bare() -> _Provider:
    return _Provider(features=frozenset({"delay"}))


def test_a_table_the_driver_can_serve_boots() -> None:
    worker.check_task_table_features(
        _table(at_least_once=True, periodic=True), _Provider(), "default"
    )


@pytest.mark.parametrize(
    ("table", "feature", "name"),
    [
        (_table(at_least_once=True), "redelivery", "m.tasks.job"),
        (_table(periodic=True), "periodic", "m.job"),
    ],
)
def test_a_table_the_driver_cannot_serve_is_refused_with_b037(
    table: Any, feature: str, name: str
) -> None:
    from sandloom.core.error_codes import code_for_exception
    from sandloom.core.infra.registry import CapabilityUnavailable

    with pytest.raises(CapabilityUnavailable, match=feature) as refused:
        worker.check_task_table_features(table, _bare(), "default")
    assert name in str(refused.value)
    assert code_for_exception(refused.value) == "SANDLOOM-B037"


def test_boot_refuses_before_handing_the_host_a_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.infra.registry import CapabilityUnavailable

    monkeypatch.setattr(worker, "build_task_table", lambda names: _table(periodic=True))
    core = _Core(_bare(), [])
    boot = worker.KernelWorkerBoot(core, core.provider, "default")
    with pytest.raises(CapabilityUnavailable, match="periodic"):
        boot.boot()


# --------------------------------------------------------------------------- #
# The WorkerBoot the host calls
# --------------------------------------------------------------------------- #
def _boot() -> worker.KernelWorkerBoot:
    core = _Core(_Provider(), [])
    return worker.KernelWorkerBoot(core, core.provider, "default")


def test_boot_registers_the_disposal_of_the_process_infra() -> None:
    _boot().boot()
    assert worker.dispose_worker_infra in async_runner._cleanups


def test_run_hands_the_message_to_the_task_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        worker, "run_task", lambda name, payload: seen.append((name, payload))
    )
    _boot().run("m.tasks.job", b'{"a": 1}', task_id="t-1")
    assert seen == [("m.tasks.job", b'{"a": 1}')]


def test_process_hooks_own_the_persistent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(async_runner, "reset_loop", lambda: events.append("reset"))
    monkeypatch.setattr(async_runner, "shutdown_loop", lambda: events.append("stop"))
    boot = _boot()
    boot.on_process_start()
    boot.on_process_stop()
    assert events == ["reset", "stop"]


def test_before_fork_freezes_the_startup_heap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen: list[bool] = []
    monkeypatch.setattr(worker, "freeze_before_fork", lambda: frozen.append(True))
    _boot().before_fork()
    assert frozen == [True]


def test_run_coroutine_uses_the_persistent_loop_and_refuses_a_running_one() -> None:
    boot = _boot()
    loops: list[asyncio.AbstractEventLoop] = []

    async def where() -> int:
        loops.append(asyncio.get_running_loop())
        return 5

    assert boot.run_coroutine(where()) == 5
    assert boot.run_coroutine(where()) == 5
    assert loops[0] is loops[1]

    async def inside() -> None:
        coro = where()
        with pytest.raises(RuntimeError, match="running event loop"):
            boot.run_coroutine(coro)

    asyncio.run(inside())


def test_the_worker_module_has_no_celery_app() -> None:
    """No ``app`` attribute: ``celery -A`` moved to the driver's module."""
    assert not hasattr(worker, "app")
    assert not hasattr(worker, "get_worker_app")
