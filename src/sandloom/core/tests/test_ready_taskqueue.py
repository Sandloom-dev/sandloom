"""``/ready`` and the taskqueue (I-19, I-20; owner decision on infrastructure
dependencies, answers 3 and 4).

Readiness reports only TRANSIENT trouble of a configured capability, keyed on
the loaded modules' declared (and derived) requirements, never on the
presence of a ``tasks.py``:

- a REQUIRED taskqueue requirement the process uses on the request path (no
  feature named, or any beyond ``periodic``): an unreachable broker makes
  ``/ready`` 503 naming the capability and the instance;
- a requirement for ``periodic`` only: the web process does not use the
  broker to serve, so a broker outage keeps it ready (the worker and beat
  run that work; the module's own check watches it);
- an optional requirement, or none: the taskqueue is never probed.

``/health`` never carries the taskqueue. The probe shares the readiness
cache: one broker connection per TTL, however many ``/ready`` hits arrive
(counted on a real Redis broker).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import sandloom.core.drivers.celery_taskqueue as _celery_taskqueue
from sandloom.core.config.settings import settings
from sandloom.core.descriptors import (
    CapabilityRequirement,
    HealthResult,
    ModuleDescriptor,
)
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry

_TOKEN = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
#: Nothing listens here: a broker outage.
_DOWN_BROKER = "redis://127.0.0.1:1/0"

#: Uses the queue while serving (enqueue on the request path).
_ENQUEUES = "kbic3_enqueues"
#: Needs only periodic jobs (the beat schedules them, a worker runs them).
_PERIODIC = "kbic3_periodic_only"
#: Enqueues, but declares the queue optional (degrades without it).
_OPTIONAL = "kbic3_optional_queue"
#: Has a ``tasks.py`` and declares nothing about the queue.
_UNDECLARED = "kbic3_undeclared_tasks"


def _descriptor(name: str, *requirements: CapabilityRequirement) -> ModuleDescriptor:
    return ModuleDescriptor(name=name, version="1", capabilities=list(requirements))


_DESCRIPTORS = {
    _ENQUEUES: _descriptor(_ENQUEUES, CapabilityRequirement("taskqueue")),
    _PERIODIC: _descriptor(
        _PERIODIC, CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))
    ),
    _OPTIONAL: _descriptor(
        _OPTIONAL,
        CapabilityRequirement(
            "taskqueue", features=frozenset({"delay"}), required=False
        ),
    ),
    _UNDECLARED: _descriptor(_UNDECLARED),
}


@pytest.fixture(autouse=True)
def undeclared_has_a_tasks_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_UNDECLARED`` is an importable package with a plain ``tasks.py``
    (no beat hook): KB-02 probed the queue for it; the requirement decides
    now."""
    package = tmp_path / _UNDECLARED
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "tasks.py").write_text("def job():\n    return None\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in (_UNDECLARED, f"{_UNDECLARED}.tasks"):
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.fixture(autouse=True)
def fresh_default_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test builds its own default Celery app from the settings it sets."""
    monkeypatch.setattr(_celery_taskqueue, "_default_app", None)


@pytest.fixture
def celery_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Celery cannot be imported (a plain ``sandloom`` without the extra)."""
    monkeypatch.setitem(sys.modules, "celery", None)


def _ops_app(
    monkeypatch: pytest.MonkeyPatch,
    loaded: list[str],
    *,
    ttl: float = 0.0,
    broker: str | None = None,
) -> tuple[FastAPI, CoreFacade]:
    """The kernel's ops endpoints over a core whose ``loaded`` modules are
    given (with their descriptors); every other readiness input is green."""
    from sandloom.core.app_factory import _install_ops_endpoints

    if broker is not None:
        monkeypatch.setattr(settings, "broker_url", broker)
    app = FastAPI()
    core = CoreFacade(app=app)
    # The registry ``create_app`` binds: the default taskqueue is ``celery``.
    core.set_infra(build_infra_registry(InMemorySchemaRegistry(), load_plugins=False))

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = loaded
    core.module_descriptors = [_DESCRIPTORS[name] for name in loaded]
    monkeypatch.setattr(settings, "service_bindings", {})
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", ttl)
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "sandloom_modules", loaded)
    monkeypatch.setattr(settings, "internal_api_token", _TOKEN)
    _install_ops_endpoints(app, core)
    return app, core


def _count_taskqueue_resolutions(
    monkeypatch: pytest.MonkeyPatch, core: CoreFacade
) -> list[str]:
    """Record every ``taskqueue`` capability resolution on ``core``."""
    calls: list[str] = []
    resolve = core.capability

    def counting(capability: str, name: str = "default", **kwargs: Any) -> Any:
        if capability == "taskqueue":
            calls.append(name)
        return resolve(capability, name, **kwargs)

    monkeypatch.setattr(core, "capability", counting)
    return calls


async def _get(app: FastAPI, path: str, *, internal: bool = False) -> httpx.Response:
    headers = {"X-Internal-Token": _TOKEN} if internal else {}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path, headers=headers)


# --------------------------------------------------- request-path use ----


async def test_ready_fails_naming_the_taskqueue_when_its_broker_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that enqueues while serving × an unreachable broker: ``/ready``
    503 naming the capability and the instance (publicly by status, details
    behind the internal token). Building the provider succeeds; the driver's
    broker probe is what fails."""
    pytest.importorskip("celery")
    app, _ = _ops_app(monkeypatch, [_ENQUEUES], broker=_DOWN_BROKER)

    public = await _get(app, "/ready")
    internal = await _get(app, "/ready", internal=True)

    assert public.status_code == 503, public.json()
    assert public.json()["status"] == "degraded"
    assert public.json()["taskqueue_status"] == "fail"
    assert "taskqueue" not in public.json()  # details stay internal
    block = internal.json()["taskqueue"]
    assert block["capability"] == "taskqueue"
    assert block["status"] == "fail"
    assert block["task_modules"] == [_ENQUEUES]
    default = block["instances"]["default"]
    assert default["status"] == "fail"
    assert default["task_modules"] == [_ENQUEUES]
    assert "broker redis://127.0.0.1:1 unreachable" in default["details"]["error"]


async def test_ready_stays_green_with_a_reachable_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that enqueues while serving × the test broker: ``ok``."""
    pytest.importorskip("celery")
    app, _ = _ops_app(monkeypatch, [_ENQUEUES, _UNDECLARED])

    response = await _get(app, "/ready", internal=True)

    assert response.status_code == 200, response.json()
    assert response.json()["taskqueue_status"] == "ok"
    assert response.json()["taskqueue"]["task_modules"] == [_ENQUEUES]


@pytest.mark.usefixtures("celery_absent")
async def test_ready_fails_when_the_driver_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last line of defence: a request-path requirement whose driver
    cannot open (startup refuses this composition first, kb-ic-1) still
    makes readiness fail rather than pass."""
    app, _ = _ops_app(monkeypatch, [_ENQUEUES])

    internal = await _get(app, "/ready", internal=True)

    assert internal.status_code == 503
    assert (
        "celery"
        in internal.json()["taskqueue"]["instances"]["default"]["details"]["error"]
    )


# ------------------------------------------------ background-only use ----


async def test_a_periodic_only_module_keeps_the_web_process_ready_on_a_broker_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner answer 3: a short broker outage does not drop the readiness of a
    process that does not use the queue to serve. The queue is not probed;
    the internal block names the module as background work."""
    pytest.importorskip("celery")
    app, core = _ops_app(monkeypatch, [_PERIODIC], broker=_DOWN_BROKER)
    calls = _count_taskqueue_resolutions(monkeypatch, core)

    public = await _get(app, "/ready")
    internal = await _get(app, "/ready", internal=True)

    assert public.status_code == 200, public.json()
    assert public.json()["taskqueue_status"] == "unused"
    block = internal.json()["taskqueue"]
    assert block["status"] == "unused"
    assert block["background_modules"] == [_PERIODIC]
    assert block["task_modules"] == []
    assert calls == []


async def test_a_request_path_module_beside_a_periodic_one_still_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The background exemption is per requirement, not per process."""
    pytest.importorskip("celery")
    app, _ = _ops_app(monkeypatch, [_PERIODIC, _ENQUEUES], broker=_DOWN_BROKER)

    internal = await _get(app, "/ready", internal=True)

    assert internal.status_code == 503
    assert internal.json()["taskqueue"]["task_modules"] == [_ENQUEUES]
    assert internal.json()["taskqueue"]["background_modules"] == [_PERIODIC]


# ---------------------------------------- no requirement, or optional ----


@pytest.mark.usefixtures("celery_absent")
@pytest.mark.parametrize("loaded", [[_UNDECLARED], [_OPTIONAL], []])
async def test_the_taskqueue_is_not_probed_without_a_required_requirement(
    monkeypatch: pytest.MonkeyPatch, loaded: list[str]
) -> None:
    """A ``tasks.py`` alone (KB-02's ``find_spec`` rule), an optional
    requirement, or no module: readiness unchanged and the taskqueue never
    resolved, even with Celery absent."""
    app, core = _ops_app(monkeypatch, loaded)
    calls = _count_taskqueue_resolutions(monkeypatch, core)

    public = await _get(app, "/ready")
    internal = await _get(app, "/ready", internal=True)

    assert public.status_code == 200, public.json()
    assert public.json()["taskqueue_status"] == "unused"
    assert internal.json()["taskqueue"] is None
    assert calls == []


async def test_health_never_fails_on_the_taskqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` is dependency diagnostics that never carries the
    taskqueue: only readiness does."""
    pytest.importorskip("celery")
    app, _ = _ops_app(monkeypatch, [_ENQUEUES], broker=_DOWN_BROKER)

    health = await _get(app, "/health", internal=True)

    assert health.status_code == 200, health.json()
    assert "taskqueue" not in health.json()["checks"]


# ------------------------------------------------------ no extra I/O ----


@pytest.mark.usefixtures("celery_absent")
async def test_the_taskqueue_probe_is_cached_like_the_other_readiness_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the health TTL, many ``/ready`` hits resolve the taskqueue once
    (the single-flight cache of the infrastructure and peer probes)."""
    app, core = _ops_app(monkeypatch, [_ENQUEUES], ttl=60.0)
    calls = _count_taskqueue_resolutions(monkeypatch, core)

    statuses = [(await _get(app, "/ready")).status_code for _ in range(5)]

    assert statuses == [503] * 5
    assert calls == ["default"]


def _count_broker_connections(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every broker connection the celery driver's probe opens (the
    real connect still runs)."""
    import sandloom.core.drivers.celery_broker_probe as probe

    opened: list[float] = []
    connect = probe._connect_once

    def counting(app: Any, timeout: float) -> None:
        opened.append(timeout)
        connect(app, timeout)

    monkeypatch.setattr(probe, "_connect_once", counting)
    return opened


async def test_one_broker_connection_per_ttl_however_many_ready_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The I/O count on the real test broker: 25 ``/ready`` hits within the
    TTL open exactly ONE broker connection; a periodic-only process opens
    none. (The connection count of the broker's own server is measured in
    the PR; under a parallel run the server is shared, so the suite counts
    in-process.)"""
    pytest.importorskip("celery")
    opened = _count_broker_connections(monkeypatch)
    app, _ = _ops_app(monkeypatch, [_ENQUEUES], ttl=60.0)

    statuses = [(await _get(app, "/ready")).status_code for _ in range(25)]

    assert statuses == [200] * 25
    assert opened == [2.0]  # one connection, bounded by the health timeout

    background, _ = _ops_app(monkeypatch, [_PERIODIC], ttl=60.0)
    for _ in range(25):
        assert (await _get(background, "/ready")).status_code == 200
    assert opened == [2.0]


async def test_the_health_infra_check_never_probes_a_taskqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health``'s check of the named instances skips the taskqueue: no
    broker connection per TTL, and a named instance on a broker that is down
    does not fail ``/health`` (readiness owns the taskqueue)."""
    pytest.importorskip("celery")
    from sandloom.core.app_factory.lifecycle import _register_health_checks
    from sandloom.core.infra.config import InstanceConfig

    opened = _count_broker_connections(monkeypatch)
    _, core = _ops_app(monkeypatch, [_ENQUEUES], broker=_DOWN_BROKER)
    core.infra.configure_instance(
        InstanceConfig("taskqueue", "jobs", "celery", {"broker": _DOWN_BROKER})
    )
    _register_health_checks(core, core.infra)

    result = await core.health_checks["infra"]()

    assert result.status == "ok", result.details
    assert opened == []
