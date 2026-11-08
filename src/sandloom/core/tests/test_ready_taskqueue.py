"""``/ready`` and an unusable taskqueue (KB-02, I-20: no silent degradation).

A loaded module that registers tasks or periodic tasks (it has a
``<pkg>.tasks`` submodule, the worker's own test) cannot run them when the
configured taskqueue cannot open: readiness fails naming the capability. With
no such module the taskqueue is never probed and readiness is unchanged.
``/health`` never fails on it.

The matrix uses the real ``celery`` driver with Celery made unimportable
(``sys.modules["celery"] = None``), the way a plain ``sandloom`` install
without the ``celery`` extra fails to open its default taskqueue.
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
from sandloom.core.descriptors import HealthResult
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry

_TOKEN = "9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b"
#: A module package with a ``tasks.py`` (tasks and the beat hook live there).
_WITH_TASKS = "kb02_module_with_tasks"
#: A module package without one.
_WITHOUT_TASKS = "kb02_module_without_tasks"


@pytest.fixture
def module_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two importable module packages, one with ``tasks.py``, one without."""
    with_tasks = tmp_path / _WITH_TASKS
    with_tasks.mkdir()
    (with_tasks / "__init__.py").write_text("")
    (with_tasks / "tasks.py").write_text(
        "def setup_periodic_tasks(sender, **kwargs):\n    pass\n"
    )
    without_tasks = tmp_path / _WITHOUT_TASKS
    without_tasks.mkdir()
    (without_tasks / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in (_WITH_TASKS, _WITHOUT_TASKS, f"{_WITH_TASKS}.tasks"):
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.fixture
def celery_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Celery cannot be imported and no default Celery app was built yet."""
    monkeypatch.setitem(sys.modules, "celery", None)
    monkeypatch.setattr(_celery_taskqueue, "_default_app", None)


def _ops_app(
    monkeypatch: pytest.MonkeyPatch, loaded: list[str], *, ttl: float = 0.0
) -> tuple[FastAPI, CoreFacade]:
    """The kernel's ops endpoints over a core whose ``loaded`` modules are
    given; every other readiness input is green."""
    from sandloom.core.app_factory import _install_ops_endpoints

    app = FastAPI()
    core = CoreFacade(app=app)
    # The registry ``create_app`` binds: the default taskqueue is ``celery``.
    core.set_infra(build_infra_registry(InMemorySchemaRegistry(), load_plugins=False))

    async def ok() -> HealthResult:
        return HealthResult(status="ok")

    core.register_health_check("db", ok)
    core.loaded_modules = loaded
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


@pytest.mark.usefixtures("module_packages", "celery_absent")
async def test_ready_fails_naming_the_taskqueue_when_a_task_module_cannot_use_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Celery absent × a module with tasks: ``/ready`` 503 naming the
    capability (publicly by status, with details behind the internal token)."""
    app, _ = _ops_app(monkeypatch, [_WITH_TASKS])

    public = await _get(app, "/ready")
    internal = await _get(app, "/ready", internal=True)

    assert public.status_code == 503, public.json()
    assert public.json()["status"] == "degraded"
    assert public.json()["taskqueue_status"] == "fail"
    assert "taskqueue" not in public.json()  # details stay internal
    assert internal.status_code == 503
    block = internal.json()["taskqueue"]
    assert block["capability"] == "taskqueue"
    assert block["instance"] == "default"
    assert block["status"] == "fail"
    assert block["task_modules"] == [_WITH_TASKS]
    assert "celery" in block["details"]["error"]


@pytest.mark.usefixtures("module_packages", "celery_absent")
async def test_ready_unchanged_and_taskqueue_not_probed_without_task_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Celery absent × no module with tasks: readiness unchanged (200) and the
    taskqueue is never resolved."""
    app, core = _ops_app(monkeypatch, [_WITHOUT_TASKS])
    calls = _count_taskqueue_resolutions(monkeypatch, core)

    public = await _get(app, "/ready")
    internal = await _get(app, "/ready", internal=True)

    assert public.status_code == 200, public.json()
    assert public.json()["taskqueue_status"] == "unused"
    assert internal.json()["taskqueue"] is None
    assert calls == []


@pytest.mark.usefixtures("module_packages")
async def test_ready_stays_green_with_a_usable_taskqueue_and_task_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Celery present × a module with tasks: the taskqueue opens and the
    process is ready (building the provider sends nothing to the broker)."""
    pytest.importorskip("celery")
    monkeypatch.setattr(_celery_taskqueue, "_default_app", None)
    app, _ = _ops_app(monkeypatch, [_WITH_TASKS, _WITHOUT_TASKS])

    response = await _get(app, "/ready", internal=True)

    assert response.status_code == 200, response.json()
    assert response.json()["taskqueue_status"] == "ok"
    assert response.json()["taskqueue"]["task_modules"] == [_WITH_TASKS]


@pytest.mark.usefixtures("module_packages", "celery_absent")
async def test_health_never_fails_on_an_unusable_taskqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` is dependency diagnostics that never fails on the
    taskqueue: only readiness carries the new rule."""
    app, _ = _ops_app(monkeypatch, [_WITH_TASKS])

    health = await _get(app, "/health", internal=True)

    assert health.status_code == 200, health.json()
    assert "taskqueue" not in health.json()["checks"]


@pytest.mark.usefixtures("module_packages", "celery_absent")
async def test_the_taskqueue_probe_is_cached_like_the_other_readiness_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No extra I/O per request: within the health TTL, many ``/ready`` hits
    resolve the taskqueue once (the same single-flight cache as the
    infrastructure and peer probes)."""
    app, core = _ops_app(monkeypatch, [_WITH_TASKS], ttl=60.0)
    calls = _count_taskqueue_resolutions(monkeypatch, core)

    statuses = [(await _get(app, "/ready")).status_code for _ in range(5)]

    assert statuses == [503] * 5
    assert calls == ["default"]
