"""``/ready`` and the taskqueue over the real ``celery`` driver (I-19, I-20; owner
decision on infrastructure dependencies, answers 3 and 4).

The kernel's half (a process that never probes the queue, a driver that cannot
open, the cached probe, operational checks) is
``sandloom.core.tests.test_ready_taskqueue``, which holds the apps and the
module descriptors these tests borrow. Here the probe reaches a broker:

- a REQUIRED taskqueue requirement used on the request path: an unreachable
  broker makes ``/ready`` 503 naming the capability and the instance;
- a requirement for ``periodic`` only: a broker outage keeps the web process
  ready (the worker and beat run that work);
- ``/health`` never carries the taskqueue;
- the probe shares the readiness cache: one broker connection per TTL, however
  many ``/ready`` hits arrive (counted on a real Redis broker).
"""

from __future__ import annotations

from typing import Any

import pytest
import sandloom_celery.worker as _celery_worker

from sandloom.core.tests.test_ready_taskqueue import (
    _ENQUEUES,
    _PERIODIC,
    _UNDECLARED,
    _count_taskqueue_resolutions,
    _get,
    _ops_app,
    undeclared_has_a_tasks_module,  # noqa: F401  (the autouse fixture of the kernel file)
)

#: Nothing listens here: a broker outage.
_DOWN_BROKER = "redis://127.0.0.1:1/0"


@pytest.fixture(autouse=True)
def fresh_default_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test builds its own default Celery app from the settings it sets."""
    monkeypatch.setattr(_celery_worker._default, "app", None)


async def test_ready_fails_naming_the_taskqueue_when_its_broker_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that enqueues while serving × an unreachable broker: ``/ready``
    503 naming the capability and the instance (publicly by status, details
    behind the internal token). Building the provider succeeds; the driver's
    broker probe is what fails."""
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
    # The type only (spec 2.4): an exception text can carry a URL.
    assert default["details"]["error"] == "BrokerUnreachableError"


async def test_ready_stays_green_with_a_reachable_broker(
    monkeypatch: pytest.MonkeyPatch, real_broker: str
) -> None:
    """A module that enqueues while serving × the test broker: ``ok``."""
    app, _ = _ops_app(monkeypatch, [_ENQUEUES, _UNDECLARED])

    response = await _get(app, "/ready", internal=True)

    assert response.status_code == 200, response.json()
    assert response.json()["taskqueue_status"] == "ok"
    assert response.json()["taskqueue"]["task_modules"] == [_ENQUEUES]


async def test_a_periodic_only_module_keeps_the_web_process_ready_on_a_broker_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner answer 3: a short broker outage does not drop the readiness of a
    process that does not use the queue to serve. The queue is not probed;
    the internal block names the module as background work."""
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
    app, _ = _ops_app(monkeypatch, [_PERIODIC, _ENQUEUES], broker=_DOWN_BROKER)

    internal = await _get(app, "/ready", internal=True)

    assert internal.status_code == 503
    assert internal.json()["taskqueue"]["task_modules"] == [_ENQUEUES]
    assert internal.json()["taskqueue"]["background_modules"] == [_PERIODIC]


async def test_health_never_fails_on_the_taskqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` is dependency diagnostics that never carries the
    taskqueue: only readiness does."""
    app, _ = _ops_app(monkeypatch, [_ENQUEUES], broker=_DOWN_BROKER)

    health = await _get(app, "/health", internal=True)

    assert health.status_code == 200, health.json()
    assert "taskqueue" not in health.json()["checks"]


def _count_broker_connections(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every broker connection the celery driver's probe opens (the
    real connect still runs)."""
    import sandloom_celery.broker_probe as probe

    opened: list[float] = []
    connect = probe._connect_once

    def counting(app: Any, timeout: float) -> None:
        opened.append(timeout)
        connect(app, timeout)

    monkeypatch.setattr(probe, "_connect_once", counting)
    return opened


async def test_one_broker_connection_per_ttl_however_many_ready_hits(
    monkeypatch: pytest.MonkeyPatch, real_broker: str
) -> None:
    """The I/O count on the real test broker: 25 ``/ready`` hits within the
    TTL open exactly ONE broker connection; a periodic-only process opens
    none. (The connection count of the broker's own server is measured in
    the PR; under a parallel run the server is shared, so the suite counts
    in-process.)"""
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
