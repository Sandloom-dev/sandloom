"""``/ready`` and the taskqueue, the kernel's side (I-19, I-20; owner decision on
infrastructure dependencies, answers 3 and 4).

Readiness reports only TRANSIENT trouble of a configured capability, keyed on
the loaded modules' declared (and derived) requirements, never on the
presence of a ``tasks.py``:

- an optional requirement, or none: the taskqueue is never probed (resolved
  zero times, even with no driver importable);
- a REQUIRED requirement whose driver cannot open still fails readiness;
- the probe shares the readiness cache: one resolution per TTL.

What reaches a broker (an unreachable one, the reachable test one, the count of
connections, the ``periodic``-only exemption, ``/health``) needs the ``celery``
driver: ``packages/sandloom-celery/tests/test_celery_ready_taskqueue.py``, which
borrows the apps and the module descriptors below.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

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
    # Before the registry: the kernel fills the celery probe's bound from it.
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 2.0)
    # The registry ``create_app`` binds: the default taskqueue is ``celery``.
    core.set_infra(build_infra_registry(InMemorySchemaRegistry()))

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = loaded
    core.module_descriptors = [_DESCRIPTORS[name] for name in loaded]
    monkeypatch.setattr(settings, "service_bindings", {})
    monkeypatch.setattr(settings, "health_cache_ttl_seconds", ttl)
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


async def test_operational_failure_keeps_web_ready_and_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, core = _ops_app(monkeypatch, [], ttl=0)
    calls = 0

    async def unavailable() -> HealthResult:
        nonlocal calls
        calls += 1
        return HealthResult(status="fail", details={"state": "overdue"})

    core.register_health_check("scheduled_work", unavailable, operational=True)

    assert (await _get(app, "/health/simple")).status_code == 200
    assert (await _get(app, "/ready")).status_code == 200
    assert calls == 0
    response = await _get(app, "/health", internal=True)
    assert response.status_code == 503
    assert response.json()["checks"]["scheduled_work"]["details"]["state"] == "overdue"
    assert calls == 1


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
    instance = internal.json()["taskqueue"]["instances"]["default"]
    assert instance["details"] == {"error": "DriverUnavailableError"}


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
