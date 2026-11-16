"""The sixteen module subsets boot from built wheels in clean venvs (slow, KB-04).

The wheels of ``sandloom``, ``sandloom-standard``, the four module
distributions and the driver packages of the standard set (``sandloom-redis``,
``sandloom-celery``)
are built from this tree once, then:

- **P2** (I-19): one clean venv per subset with plain ``sandloom`` and exactly
  the subset's module wheels, each with only the dependencies its published
  metadata declares (no Celery, no Redis). The composition root
  ``sandloom.main`` composes what is installed, by entry point, and selects
  no driver: every subset is refused (SANDLOOM-B037). A subset with identity
  (a token issuer) adds the ``sandloom-standard`` wheel and boots
  ``sandloom_standard.main``, since the lean root assigns no claim
  authorities and refuses an issuer (SANDLOOM-B035); its ``DEFAULT_INFRA``
  names the Redis drivers the venv lacks, so it is refused (B037) too.
- **P3 extras**: one clean venv with ``sandloom[standard,admin]`` (every module
  wheel, Celery and Redis); each subset is chosen with ``SANDLOOM_MODULES``
  through the standard root ``sandloom_standard.main``, what the image and
  the deploy profiles run. The empty subset is the kernel factory with no
  default modules.

The expected outcomes are :func:`_module_subset_matrix.expected`. The P2 venvs
are booted a second time with the health probes of the cache and the taskqueue
real (``test_subset_on_plain_sandloom_p2_unstubbed``, KB-IC-4): a stub for them
hid that plain ``sandloom`` has no redis-py. Only the database probe is
replaced (no database runs). The CI
``packages`` job runs this file; the repository profile is
``test_module_subsets_repository.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _module_subset_matrix import (
    MODULES,
    REDIS_INFRA,
    REPO,
    SUBSETS,
    SubsetRun,
    check_run,
    expected,
    probe_environment,
    run_matrix,
    run_subset,
    subset_id,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.subprocess_probe,
    pytest.mark.skipif(
        not (REPO / "packages").is_dir() or shutil.which("uv") is None,
        reason="needs the source tree's packages/ and uv",
    ),
]

sys.path.insert(0, str(REPO / "tools" / "packaging"))
import release_packages  # noqa: E402  (stdlib only; the one package list)

#: The kernel, the root and the module distributions, from the one list, in its
#: release order, and the driver packages of the standard set (a subset is a set
#: of modules, but the ``sandloom[standard]`` extra requires those drivers, so
#: the extras install resolves them from these wheels).
PACKAGES = tuple(
    p.name
    for p in release_packages.PACKAGES
    if p.kind in ("kernel", "root", "module") or (p.kind == "driver" and p.standard)
)
assert set(PACKAGES) >= {
    "sandloom",
    "sandloom-standard",
    *(d for _, d in MODULES.values()),
}


def _bin(venv: Path, name: str) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / name


def _uv(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    done = subprocess.run(
        ["uv", *args], capture_output=True, text=True, check=False, env=env, timeout=900
    )
    assert done.returncode == 0, f"uv {' '.join(args)}\n{done.stdout}{done.stderr}"[
        -4000:
    ]


def _venv(path: Path, *requirements: str) -> Path:
    """A clean venv on this interpreter's Python with ``requirements`` only."""
    _uv("venv", "--quiet", "--python", sys.executable, str(path))
    python = _bin(path, "python")
    _uv("pip", "install", "--quiet", "--python", str(python), *requirements)
    return python


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("subset-wheels")

    def build(package: str) -> Path:
        target = out / package
        _uv(
            "build",
            "--quiet",
            "--wheel",
            "--out-dir",
            str(target),
            str(REPO / "packages" / package),
        )
        return next(target.glob("*.whl"))

    with ThreadPoolExecutor(max_workers=len(PACKAGES)) as pool:
        return dict(zip(PACKAGES, pool.map(build, PACKAGES), strict=True))


@pytest.fixture(scope="module")
def p2_venvs(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> dict[tuple[str, ...], Path]:
    """One clean venv per subset: ``sandloom`` with its ``postgres`` extra (the
    probe environment names a PostgreSQL database; no Redis, no Celery) and the
    subset's wheels."""
    root = tmp_path_factory.mktemp("subsets-p2")

    def make(subset: tuple[str, ...]) -> Path:
        return _venv(
            root / subset_id(subset),
            f"sandloom[postgres] @ {wheels['sandloom'].resolve().as_uri()}",
            *([str(wheels["sandloom-standard"])] if "identity" in subset else []),
            *(str(wheels[MODULES[m][1]]) for m in subset),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(SUBSETS, pool.map(make, SUBSETS), strict=True))


def _p2_runs(
    cwd: Path,
    venvs: dict[tuple[str, ...], Path],
    probe_config: dict[str, object] | None,
) -> dict[tuple[str, ...], SubsetRun]:
    def run(subset: tuple[str, ...]) -> SubsetRun:
        python = venvs[subset]
        return run_subset(
            subset,
            python=python,
            sandloom=python.parent / "sandloom",
            root="sandloom_standard.main" if "identity" in subset else "sandloom.main",
            cwd=cwd,
            env=probe_environment(),
            probe_config=probe_config,
        )

    return run_matrix(run)


@pytest.fixture(scope="module")
def p2_runs(
    tmp_path_factory: pytest.TempPathFactory, p2_venvs: dict[tuple[str, ...], Path]
) -> dict[tuple[str, ...], SubsetRun]:
    return _p2_runs(tmp_path_factory.mktemp("work-p2"), p2_venvs, None)


@pytest.fixture(scope="module")
def p2_unstubbed_runs(
    tmp_path_factory: pytest.TempPathFactory, p2_venvs: dict[tuple[str, ...], Path]
) -> dict[tuple[str, ...], SubsetRun]:
    """The same venvs, the cache and taskqueue probes real (only ``db`` stubbed)."""
    return _p2_runs(tmp_path_factory.mktemp("work-p2-live"), p2_venvs, {"stub": ["db"]})


@pytest.fixture(scope="module")
def p3_runs(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> dict[tuple[str, ...], SubsetRun]:
    root = tmp_path_factory.mktemp("subsets-p3")
    cwd = root / "work"
    cwd.mkdir()
    venv = root / "extras"
    python = _venv(
        venv,
        f"sandloom[standard,admin] @ {wheels['sandloom'].resolve().as_uri()}",
        *(str(w) for p, w in wheels.items() if p != "sandloom"),
    )

    def run(subset: tuple[str, ...]) -> SubsetRun:
        # The extras venv has the Redis and Celery drivers: their instances
        # state their urls (the probes are stubbed, nothing connects).
        extra = {"INFRA": REDIS_INFRA}
        if subset:
            extra["SANDLOOM_MODULES"] = json.dumps([MODULES[m][0] for m in subset])
        return run_subset(
            subset,
            python=python,
            sandloom=_bin(venv, "sandloom"),
            root="sandloom_standard.main" if subset else "kernel",
            cwd=cwd,
            env=probe_environment(extra),
        )

    return run_matrix(run)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_subset_boots_on_plain_sandloom_p2(
    p2_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """Plain ``sandloom`` selects no driver: every subset is refused (B037)."""
    check_run(p2_runs[subset], expected(subset, celery=False), celery=False, names={})


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_subset_on_plain_sandloom_p2_unstubbed(
    p2_unstubbed_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    """No driver package (``sandloom-redis``, ``sandloom-celery``), no stub (I-19, KB-IC-4).

    The kernel names no driver and never ``memory``, and the venv has no
    composition that selects one (the identity subsets bring
    ``sandloom-standard``, whose ``DEFAULT_INFRA`` names the Redis drivers
    this venv lacks): every subset is refused with SANDLOOM-B037, naming the
    required instances, at startup instead of booting without a cache.
    A missing redis-py behind an installed package is the infrastructure
    lane's (``test_a_missing_redis_driver_is_refused_or_visible``).
    """
    run = p2_unstubbed_runs[subset]
    check_run(run, expected(subset, celery=False), celery=False, names={})
    message = run.probe["message"]
    for token in ("cache/default", "atomic_store/security"):
        assert token in message, (token, message)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_subset_boots_through_the_extras_p3(
    p3_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    check_run(p3_runs[subset], expected(subset, celery=True), celery=True)
