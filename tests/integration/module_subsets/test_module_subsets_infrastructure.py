"""Module subsets against real infrastructure and absent drivers (KB-IC-4, I-19).

The stubbed matrix (``test_module_subsets_repository.py``) replaces every health
probe by a passing one, so it cannot see a missing Python driver or a broker
nobody listens on. These lanes boot the same sixteen subsets in the repository
environment (Celery and redis-py installed) and leave the checks under test
real:

- **real infrastructure**: Postgres, Redis and the broker the suite runs on,
  no stub at all: every subset is ready and every health check is ``ok``;
- **broker unreachable**: real database and cache, a broker nobody listens on:
  billing needs only ``periodic`` (the worker and beat run it), so the web
  process stays ready and the broker is not hidden by a stub;
- **a Python driver missing**: ``redis`` (identity requires the cache) or
  ``celery`` (billing requires the taskqueue with ``periodic``) made
  unimportable, which ``find_spec`` also reports missing, as in a clean venv:
  the module is refused with SANDLOOM-B037 naming the module, the capability
  and the missing feature; without redis-py the kernel's own cache check fails
  (no stub hides ``No module named 'redis'``);
- **a taskqueue driver without ``periodic``**: a driver a package publishes
  through the ``sandloom.drivers`` entry point, advertising ``delay`` only,
  selected with ``INFRA``: billing is refused naming ``periodic``, identity
  (token cleanup is optional) boots.

The clean-venv version of the missing-driver lane, on plain ``sandloom``, is in
``test_module_subsets_wheels.py`` (slow).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from _module_subset_matrix import (
    MODULES,
    REFUSAL_NAMES,
    SUBSETS,
    SubsetRun,
    check_run,
    expected_unstubbed,
    probe_environment,
    run_matrix,
    run_subset,
    subset_id,
)

pytestmark = pytest.mark.subprocess_probe

ADMIN_SOURCE = (
    Path(__file__).resolve().parents[3] / "packages" / "editable" / "sandloom-admin"
)

#: Nobody listens on port 1: a broker that cannot be reached.
DEAD_BROKER = "redis://127.0.0.1:1/1"

FAKE_DRIVER = "fakeq"
FAKE_DRIVER_MODULE = '''\
"""A taskqueue driver that enqueues and nothing else: no ``periodic``."""

from collections.abc import Mapping
from typing import Any

from sandloom.core.infra.contracts import TaskQueueProvider


class NoPeriodicQueue(TaskQueueProvider):
    contract_version = "v1"
    supported_features = frozenset({"delay"})

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return "fakeq-1"
'''


def _real(name: str) -> str:
    """The suite's own service URL (``.env.test``, an xdist worker's, the CI's)."""
    return os.environ[name]


def _live_env(
    extra: dict[str, str] | None = None, *, broker: str | None = None
) -> dict:
    return probe_environment(
        {
            "PYTHONPATH": str(ADMIN_SOURCE),
            "DATABASE_URL": _real("DATABASE_URL"),
            "CACHE_URL": _real("CACHE_URL"),
            "BROKER_URL": broker or _real("BROKER_URL"),
            **(extra or {}),
        }
    )


def _root(subset: tuple[str, ...]) -> str:
    if not subset:
        return "kernel"
    return "sandloom_standard.main" if "identity" in subset else "sandloom.main"


def _lane(
    cwd: Path,
    *,
    subsets: tuple[tuple[str, ...], ...] = SUBSETS,
    env: dict[str, str],
    probe_config: dict[str, object] | None = None,
) -> dict[tuple[str, ...], SubsetRun]:
    python = Path(sys.executable)

    def run(subset: tuple[str, ...]) -> SubsetRun:
        extra = dict(env)
        if subset:
            extra["SANDLOOM_MODULES"] = json.dumps([MODULES[m][0] for m in subset])
        return run_subset(
            subset,
            python=python,
            sandloom=python.parent / "sandloom",
            root=_root(subset),
            cwd=cwd,
            env=extra,
            probe_config=probe_config or {"stub": []},
        )

    return run_matrix(run, subsets)


def _all_checks_ok(run: SubsetRun) -> None:
    checks = run.probe["health_checks"]
    assert checks, run.probe
    bad = {n: c for n, c in checks.items() if c["status"] != "ok"}
    assert not bad, f"{run.subset}: {bad}"


def _billing_runs_in_the_background(run: SubsetRun) -> None:
    if "billing" in run.subset:
        block = run.probe["ready_body"]["taskqueue"]
        assert "sandloom_billing" in block["background_modules"], block
        assert "billing_reconciler" in run.probe["health_checks"], run.probe


@pytest.fixture(scope="module")
def live_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(tmp_path_factory.mktemp("subsets-live"), env=_live_env())


@pytest.fixture(scope="module")
def broker_down_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(
        tmp_path_factory.mktemp("subsets-broker-down"),
        env=_live_env(broker=DEAD_BROKER),
    )


@pytest.fixture(scope="module")
def no_redis_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(
        tmp_path_factory.mktemp("subsets-no-redis"),
        env=_live_env(),
        probe_config={"stub": [], "block": ["redis"]},
    )


@pytest.fixture(scope="module")
def no_celery_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(
        tmp_path_factory.mktemp("subsets-no-celery"),
        env=_live_env(),
        probe_config={"stub": [], "block": ["celery"]},
    )


@pytest.fixture(scope="module")
def no_periodic_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    root = tmp_path_factory.mktemp("subsets-no-periodic")
    site = root / "site"
    dist = site / "sandloom_fakeq-0.0.0.dist-info"
    dist.mkdir(parents=True)
    (site / "fakeq_driver.py").write_text(FAKE_DRIVER_MODULE)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sandloom-fakeq\nVersion: 0.0.0\n"
    )
    (dist / "entry_points.txt").write_text(
        f"[sandloom.drivers]\ntaskqueue.{FAKE_DRIVER} = fakeq_driver:NoPeriodicQueue\n"
    )
    infra = {"taskqueue": {"default": {"driver": FAKE_DRIVER}}}
    work = root / "work"
    work.mkdir()
    return _lane(
        work,
        env=_live_env(
            {
                "PYTHONPATH": os.pathsep.join([str(ADMIN_SOURCE), str(site)]),
                "INFRA": json.dumps(infra),
            }
        ),
    )


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_every_subset_is_ready_on_real_infrastructure(
    live_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    run = live_runs[subset]
    check_run(run, expected_unstubbed(subset), celery=True)
    assert run.probe["health"] == 200 and run.probe["ready"] == 200
    _all_checks_ok(run)
    _billing_runs_in_the_background(run)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_an_unreachable_broker_does_not_unready_the_web_process(
    broker_down_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """No bundled module enqueues on the request path: ``periodic`` is run by the
    worker and beat, so the web process is ready with the broker down (KB-IC-3)."""
    run = broker_down_runs[subset]
    check_run(run, expected_unstubbed(subset), celery=True)
    _all_checks_ok(run)
    assert run.probe["ready_body"]["taskqueue_status"] == "unused"
    _billing_runs_in_the_background(run)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_a_missing_redis_driver_is_refused_or_visible(
    no_redis_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """identity requires the cache: refused naming it. Any other subset boots,
    and the kernel's own cache check fails, un-hidden by a stub."""
    run = no_redis_runs[subset]
    want = expected_unstubbed(subset, redis=False)
    check_run(run, want, celery=True, names={"identity": REFUSAL_NAMES["identity"]})
    if want.kind == "not_ready_cache":
        checks = run.probe["health_checks"]
        assert "redis" in checks["cache"]["details"]["error"], checks
        assert {n for n, c in checks.items() if c["status"] != "ok"} == {"cache"}, (
            checks
        )


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_a_missing_celery_driver_refuses_billing_naming_periodic(
    no_celery_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """billing requires ``periodic``: refused naming module, capability, feature
    and the extra to install. identity's queue is optional: it boots."""
    run = no_celery_runs[subset]
    check_run(
        run,
        expected_unstubbed(subset, celery=False),
        celery=False,
        names={"billing": REFUSAL_NAMES["billing"]},
    )
    if "billing" in subset:
        assert "sandloom[celery]" in run.probe["message"], run.probe["message"]
    else:
        assert run.probe["health"] == 200 and run.probe["ready"] == 200
        _all_checks_ok(run)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_a_taskqueue_driver_without_periodic_refuses_billing(
    no_periodic_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """A driver that supports ``enqueue`` is not granted ``periodic``: billing is
    refused naming the feature and the driver; the other subsets are ready."""
    run = no_periodic_runs[subset]
    names = {"billing": ("sandloom_billing", "taskqueue", "periodic", FAKE_DRIVER)}
    check_run(run, expected_unstubbed(subset, periodic=False), celery=True, names=names)
    if "billing" not in subset:
        assert run.probe["health"] == 200 and run.probe["ready"] == 200
        _all_checks_ok(run)
