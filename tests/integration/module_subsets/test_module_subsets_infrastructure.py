"""Module subsets against real infrastructure and absent drivers (KB-IC-4, I-19).

The stubbed matrix (``test_module_subsets_repository.py``) replaces every health
probe by a passing one, so it cannot see a missing Python driver or a broker
nobody listens on. These lanes boot the subsets in the repository environment (Celery and
redis-py installed; all sixteen on real infrastructure, ``FAILURE_SUBSETS``,
one per class of subset, on a failure) and leave the checks under test real:

- **real infrastructure**: Postgres, Redis and the broker the suite runs on,
  no stub at all: web readiness stays green and Billing reports missing
  reconciler progress when no worker or beat is running;
- **broker unreachable**: real database and cache, a broker nobody listens on:
  billing needs only ``periodic`` (the worker and beat run it), so the web
  process stays ready and the broker is not hidden by a stub;
- **a Python driver missing**: ``redis`` (identity requires the cache) or
  ``celery`` (billing requires the taskqueue with ``periodic``) made
  unimportable, which ``find_spec`` also reports missing, as in a clean venv:
  the module is refused with SANDLOOM-B037 naming the module, the capability
  and the missing feature; without redis-py the kernel's own cache check fails
  (no stub hides it: the health detail names ``missing_module: redis``);
- **a taskqueue driver without ``periodic``**: a driver a package publishes
  through the ``sandloom.drivers`` entry point, advertising ``delay`` only,
  selected with ``INFRA``: billing is refused naming ``periodic``, identity
  (token cleanup is optional) boots.

The clean-venv version of the missing-driver lane, on plain ``sandloom``, is in
``test_module_subsets_wheels.py`` (slow).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from _module_subset_matrix import (
    MODULES,
    REFUSAL_NAMES,
    SUBSETS,
    Outcome,
    SubsetRun,
    check_run,
    expected_unstubbed,
    probe_environment,
    run_matrix,
    run_subset,
    subset_id,
)

from sandloom.core.testing.database import open_test_engine

pytestmark = pytest.mark.subprocess_probe

ADMIN_SOURCE = (
    Path(__file__).resolve().parents[3] / "packages" / "editable" / "sandloom-admin"
)

#: Nobody listens on port 1: a broker that cannot be reached.
DEAD_BROKER = "redis://127.0.0.1:1/1"

FAKE_DRIVER = "fakeq"
FAKE_DRIVER_MODULE = '''\
"""A taskqueue driver that sends and nothing else: no ``periodic``."""

from datetime import timedelta

from sandloom.core.infra.contracts import TaskQueueProvider


class NoPeriodicQueue(TaskQueueProvider):
    contract_version = 2
    supported_features = frozenset({"delay", "redelivery"})

    def worker_host(self) -> object:
        raise NotImplementedError

    async def send(
        self,
        task: str,
        payload: bytes,
        *,
        task_id: str,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> str:
        return task_id
'''


def _real(name: str) -> str:
    """The suite's own service URL (``.env.test``, an xdist worker's, the CI's)."""
    return os.environ[name]


def _live_infra(taskqueue: dict[str, object] | None, *, state_taskqueue: bool) -> str:
    """The suite's own ``INFRA`` (every instance's url, as ``.env.test``, the
    worker rewrite and the run's overrides state them), with the default task
    queue replaced by ``taskqueue`` when given and left out with
    ``state_taskqueue=False`` (the composition's own, implicit instance then
    reports a missing driver through the module that needs it). No flat url
    exists."""
    infra = json.loads(_real("INFRA"))
    if not state_taskqueue:
        infra.pop("taskqueue", None)
    elif taskqueue is not None:
        infra["taskqueue"] = {"default": taskqueue}
    return json.dumps(infra)


def _live_env(
    extra: dict[str, str] | None = None,
    *,
    taskqueue: dict[str, object] | None = None,
    state_taskqueue: bool = True,
) -> dict:
    return probe_environment(
        {
            "PYTHONPATH": str(ADMIN_SOURCE),
            "DATABASE_URL": _real("DATABASE_URL"),
            "INFRA": _live_infra(taskqueue, state_taskqueue=state_taskqueue),
            **(extra or {}),
        }
    )


def _root(subset: tuple[str, ...]) -> str:
    # The lean root selects no driver (SANDLOOM-B037): the standard one does.
    return "sandloom_standard.main" if subset else "kernel"


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


#: The subsets the four failure lanes boot (the live lane boots all sixteen).
#: A failure outcome depends on the kernel root against a module composition
#: (``_root``), on billing (it alone requires ``periodic`` and runs the
#: reconciler in the background), on identity (its queue is optional: it boots
#: where billing is refused) and on neither (tenancy, admin). The modules
#: act independently in ``check_run`` and ``expected_unstubbed``, so one subset
#: per class covers every path (``test_the_failure_subsets_cover_every_failure_path``
#: pins it, and that it bites on a narrower choice):
#:
#: - ``none``: the kernel root with the standard composition's driver defaults:
#:   a process with no module at all;
#: - ``identity``: refused for a missing redis driver, boots with a missing celery
#:   or a queue without ``periodic``;
#: - ``billing``: refused for ``periodic``, reports the reconciler in the background;
#: - ``identity`` + ``billing``: one real combination, because the proof above
#:   assumes the modules act independently and this boots the assumption once;
#: - ``tenancy`` + ``admin``: the modules that require none of what a lane breaks.
FAILURE_SUBSETS: tuple[tuple[str, ...], ...] = (
    (),
    ("identity",),
    ("billing",),
    ("identity", "billing"),
    ("tenancy", "admin"),
)


def _uncovered_failure_paths(chosen: Sequence[tuple[str, ...]]) -> list[str]:
    """What ``chosen`` leaves out of what the full matrix (``SUBSETS``) covers."""
    gaps: list[str] = []
    if () not in chosen:
        gaps.append("the kernel root (no module)")
    for module in MODULES:
        if not any(module in subset for subset in chosen):
            gaps.append(f"module {module}")
    for module in REFUSAL_NAMES:
        if not any(set(subset) & set(REFUSAL_NAMES) == {module} for subset in chosen):
            gaps.append(f"{module} as the only module that names a refusal")
    if not any(subset and not set(subset) & set(REFUSAL_NAMES) for subset in chosen):
        gaps.append("modules that require none of what a lane breaks")
    for celery, periodic in itertools.product((True, False), repeat=2):
        outcomes = {
            expected_unstubbed(s, celery=celery, periodic=periodic) for s in chosen
        }
        every = {
            expected_unstubbed(s, celery=celery, periodic=periodic) for s in SUBSETS
        }
        if outcomes != every:
            gaps.append(f"outcomes with celery={celery} periodic={periodic}")
    return gaps


def test_the_failure_subsets_cover_every_failure_path() -> None:
    assert set(FAILURE_SUBSETS) < set(SUBSETS)
    assert _uncovered_failure_paths(SUBSETS) == []
    assert _uncovered_failure_paths(FAILURE_SUBSETS) == []


@pytest.mark.parametrize(
    ("narrower", "missing"),
    [
        # no billing: its refusal and its background reconciler are never run
        (((), ("identity",), ("tenancy", "admin")), "outcomes"),
        (((), ("identity",), ("tenancy", "admin")), "billing"),
        # no identity: the module whose queue is optional is never run
        (((), ("billing",), ("tenancy", "admin")), "identity"),
        # no other modules: tenancy and admin are never run
        (((), ("identity",), ("billing",)), "tenancy"),
        # no kernel root
        ((("identity",), ("billing",), ("tenancy", "admin")), "kernel root"),
    ],
)
def test_a_narrower_failure_choice_is_reported(
    narrower: tuple[tuple[str, ...], ...], missing: str
) -> None:
    assert any(missing in gap for gap in _uncovered_failure_paths(narrower))


def _only_expected_health_failures(run: SubsetRun) -> None:
    checks = run.probe["health_checks"]
    assert checks, run.probe
    bad = {n: c for n, c in checks.items() if c["status"] != "ok"}
    assert set(bad) == ({"billing_reconciler"} if "billing" in run.subset else set()), (
        f"{run.subset}: {bad}"
    )


def _billing_runs_in_the_background(run: SubsetRun) -> None:
    if "billing" in run.subset:
        block = run.probe["ready_body"]["taskqueue"]
        assert "sandloom_billing" in block["background_modules"], block
        assert "billing_reconciler" not in run.probe["ready_body"]["health_checks"]
        check = run.probe["health_checks"]["billing_reconciler"]
        assert check["details"]["state"] == "never_ran", check


@pytest.fixture(scope="module", autouse=True)
def _operational_probe_schema() -> None:
    """Start with the normal test schema even when this matrix runs alone."""
    from sandloom_standard.main import core

    assert "sandloom_billing" in core.loaded_modules

    async def create() -> None:
        async with open_test_engine():
            pass

    asyncio.run(create())


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
        subsets=FAILURE_SUBSETS,
        env=_live_env(taskqueue={"broker": DEAD_BROKER}),
    )


@pytest.fixture(scope="module")
def no_redis_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(
        tmp_path_factory.mktemp("subsets-no-redis"),
        subsets=FAILURE_SUBSETS,
        env=_live_env(),
        probe_config={"stub": [], "block": ["redis"]},
    )


@pytest.fixture(scope="module")
def no_celery_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    return _lane(
        tmp_path_factory.mktemp("subsets-no-celery"),
        subsets=FAILURE_SUBSETS,
        env=_live_env(state_taskqueue=False),
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
    work = root / "work"
    work.mkdir()
    return _lane(
        work,
        subsets=FAILURE_SUBSETS,
        env=_live_env(
            {"PYTHONPATH": os.pathsep.join([str(ADMIN_SOURCE), str(site)])},
            taskqueue={"driver": FAKE_DRIVER},
        ),
    )


@pytest.fixture(scope="module")
def dev_billing_run(tmp_path_factory: pytest.TempPathFactory) -> SubsetRun:
    """Billing alone at the ``dev`` tier, as ``sandloom dev run`` serves it: no
    worker and no beat."""
    runs = _lane(
        tmp_path_factory.mktemp("subsets-dev"),
        subsets=(("billing",),),
        env=_live_env({"ENVIRONMENT": "dev"}),
    )
    return runs[("billing",)]


def test_in_development_a_billing_reconciler_that_never_ran_keeps_the_app_healthy(
    dev_billing_run: SubsetRun,
) -> None:
    """The matrix lanes run at ``test``, where the same boot is
    ``ready_ops_degraded``: here ``/health`` is 200 and says what to start."""
    probe = dev_billing_run.probe
    assert probe["outcome"] == "booted", probe
    assert probe["health"] == 200 and probe["ready"] == 200, probe
    check = probe["health_checks"]["billing_reconciler"]
    assert check["status"] == "ok", check
    assert check["details"]["state"] == "never_ran", check
    assert "uv run sandloom beat" in check["details"]["note"], check
    failing = {n for n, c in probe["health_checks"].items() if c["status"] != "ok"}
    assert not failing, probe["health_checks"]


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_every_subset_is_ready_on_real_infrastructure(
    live_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    run = live_runs[subset]
    check_run(run, expected_unstubbed(subset), celery=True)
    assert run.probe["ready"] == 200
    _only_expected_health_failures(run)
    _billing_runs_in_the_background(run)


@pytest.mark.parametrize("subset", FAILURE_SUBSETS, ids=subset_id)
def test_an_unreachable_broker_does_not_unready_the_web_process(
    broker_down_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """No bundled module enqueues on the request path: ``periodic`` is run by the
    worker and beat, so the web process is ready with the broker down (KB-IC-3)."""
    run = broker_down_runs[subset]
    check_run(run, expected_unstubbed(subset), celery=True)
    _only_expected_health_failures(run)
    assert run.probe["ready_body"]["taskqueue_status"] == "unused"
    _billing_runs_in_the_background(run)


@pytest.mark.parametrize("subset", FAILURE_SUBSETS, ids=subset_id)
def test_a_missing_redis_driver_is_refused_or_visible(
    no_redis_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """The standard composition selects the ``redis`` cache and the security
    store, which every process starts with (SANDLOOM-B037): whatever the
    subset, a missing redis-py is refused at startup naming the instance and
    the package, never a boot that is not ready."""
    run = no_redis_runs[subset]
    check_run(
        run,
        Outcome("refused", refusal="SANDLOOM-B037"),
        celery=True,
        names={},
    )
    message = run.probe["message"]
    for token in ("cache/default", "atomic_store/security", "redis"):
        assert token in message, (token, message)


@pytest.mark.parametrize("subset", FAILURE_SUBSETS, ids=subset_id)
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
        _only_expected_health_failures(run)


@pytest.mark.parametrize("subset", FAILURE_SUBSETS, ids=subset_id)
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
        _only_expected_health_failures(run)
