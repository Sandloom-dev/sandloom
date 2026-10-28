"""The framework Celery worker entrypoint (``sandloom.core.worker``).

Pins D3: the worker is owned by the kernel (apps without ``identity`` still get
one), its Celery app comes from the taskqueue capability (never constructed
outside the driver), and module tasks / beat hooks are discovered from the
LOADED module set only.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery

from sandloom.core import worker


class _Provider:
    def __init__(self, backend: Any, driver: str = "celery") -> None:
        self._backend = backend
        self.driver = driver

    async def raw_backend(self) -> Any:
        return self._backend


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


def test_import_has_no_side_effects() -> None:
    """``import sandloom.core.worker`` must not boot the composition root."""
    importlib.reload(worker)
    assert worker._app is None


def test_worker_registers_loaded_module_tasks_and_beat_schedule() -> None:
    app = _celery()
    core = _Core(app, ["sandloom_identity", "sandloom_tenancy"])

    built = worker.build_worker_app(core)

    assert built is app
    assert core.requested == ["default"]
    app.finalize()
    assert "sandloom_identity.tasks.cleanup_expired_refresh_tokens" in app.tasks
    # The identity beat hook ran on finalize and filled the schedule.
    schedule = app.conf.beat_schedule
    assert "cleanup_revoked_refresh_tokens_daily" in schedule
    assert "cleanup_expired_refresh_tokens_hourly" in schedule


def test_worker_without_identity_has_no_identity_tasks() -> None:
    """A deployment that does not load identity gets a worker without it."""
    app = _celery()
    core = _Core(app, ["sandloom_tenancy"])
    worker.build_worker_app(core)
    app.finalize()
    # (@shared_task binds already-imported tasks to every app, so only the
    # beat hook — which the worker connects per loaded module — is checked.)
    assert "cleanup_revoked_refresh_tokens_daily" not in (app.conf.beat_schedule or {})


def test_worker_honours_taskqueue_instance_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(worker.TASKQUEUE_INSTANCE_ENV, "jobs")
    core = _Core(_celery(), [])
    worker.build_worker_app(core)
    assert core.requested == ["jobs"]


def test_non_celery_taskqueue_is_rejected() -> None:
    core = _Core(SimpleNamespace(), [])
    with pytest.raises(worker.WorkerConfigurationError, match="Celery"):
        worker.build_worker_app(core)


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


def test_real_composition_root_resolves_driver_built_app() -> None:
    """End to end against ``sandloom.main``: the app is the taskqueue driver's."""
    core = worker.load_composition_core("sandloom.main")
    app = worker.build_worker_app(core)
    assert hasattr(app, "send_task")
    if "sandloom_identity" in core.loaded_modules:
        app.finalize()
        assert "sandloom_identity.tasks.audit_user_activity" in app.tasks


# --------------------------------------------------------------------------- #
# Container-aware sizing + liveness heartbeat (split-review finding #1)
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
    """Celery's own default is os.cpu_count() (the HOST): 10 prefork children
    in a 384 MiB container OOM-looped. The framework always sets it."""
    from sandloom.core import cpu_quota

    monkeypatch.setattr(cpu_quota, "available_cpus", lambda: 10.0)
    app = _celery()
    worker.configure_worker_sizing(app, env={})
    assert app.conf.worker_concurrency == worker.MAX_DEFAULT_WORKER_CONCURRENCY
    assert app.conf.worker_max_memory_per_child is None


def test_sizing_env_overrides() -> None:
    app = _celery()
    worker.configure_worker_sizing(
        app,
        env={
            "WORKER_CONCURRENCY": "3",
            "WORKER_MAX_MEMORY_PER_CHILD": "200000",
            "WORKER_MAX_TASKS_PER_CHILD": "500",
        },
    )
    assert app.conf.worker_concurrency == 3
    assert app.conf.worker_max_memory_per_child == 200000
    assert app.conf.worker_max_tasks_per_child == 500


@pytest.mark.parametrize("raw", ["0", "-1", "two"])
def test_sizing_rejects_bad_values(raw: str) -> None:
    with pytest.raises(worker.WorkerConfigurationError, match="WORKER_CONCURRENCY"):
        worker.configure_worker_sizing(_celery(), env={"WORKER_CONCURRENCY": raw})


def test_build_worker_app_applies_sizing_and_heartbeat_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_CONCURRENCY", "1")
    app = _celery()
    worker.build_worker_app(_Core(app, []))
    worker.build_worker_app(_Core(app, []))
    assert app.conf.worker_concurrency == 1
    steps = [
        s for s in app.steps["worker"] if "Heartbeat" in getattr(s, "__name__", "")
    ]
    assert len(steps) == 1


def test_heartbeat_freshness(tmp_path: Path) -> None:
    beat = tmp_path / "hb"
    assert not worker.heartbeat_is_fresh(str(beat))
    beat.touch()
    assert worker.heartbeat_is_fresh(str(beat))
    assert not worker.heartbeat_is_fresh(str(beat), now=beat.stat().st_mtime + 120)


def test_healthcheck_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    beat = tmp_path / "hb"
    monkeypatch.setenv(worker.HEARTBEAT_FILE_ENV, str(beat))
    assert worker.main(["healthcheck"]) == 1
    beat.touch()
    assert worker.main(["healthcheck"]) == 0
    assert worker.main([]) == 2


def test_heartbeat_step_runs_on_the_worker_timer(tmp_path: Path) -> None:
    """The bootstep touches the file on start, re-arms on the worker's timer
    and removes the file on stop (a stopped worker is never "alive")."""
    beat = tmp_path / "hb"
    app = _celery()
    worker.install_heartbeat(app, str(beat))
    (step_cls,) = [s for s in app.steps["worker"] if "Heartbeat" in s.__name__]
    assert "celery.worker.components:Timer" in step_cls.requires

    scheduled: list[tuple[float, Any]] = []

    class _Timer:
        def call_repeatedly(
            self, secs: float, fun: Any, args: Any, priority: int
        ) -> Any:
            scheduled.append((secs, fun))
            return SimpleNamespace(cancel=lambda: scheduled.append((-1, None)))

    parent = SimpleNamespace(timer=_Timer())
    step = step_cls(parent)
    step.start(parent)
    assert worker.heartbeat_is_fresh(str(beat))
    assert scheduled[0][0] == worker.HEARTBEAT_INTERVAL_SECONDS
    beat.unlink()
    scheduled[0][1]()  # a timer tick re-touches the file
    assert beat.exists()
    step.stop(parent)
    assert not beat.exists()
    assert scheduled[-1] == (-1, None)  # timer entry cancelled
    scheduled[0][1]()  # a late tick after stop never resurrects it
    assert not beat.exists()


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
    from types import SimpleNamespace

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
