"""The sixteen module subsets boot from built wheels in clean venvs (slow, KB-04).

The wheels of ``sandloom``, ``sandloom-standard`` and the four module
distributions are built from this tree once, then:

- **P2** (I-19): one clean venv per subset with plain ``sandloom`` and exactly
  the subset's module wheels, each with only the dependencies its published
  metadata declares (no Celery, no Redis). The composition root
  ``sandloom.main`` composes what is installed, by entry point; a subset with
  identity (a token issuer) adds the ``sandloom-standard`` wheel and boots
  ``sandloom_standard.main``, since the lean root assigns no claim
  authorities and refuses an issuer (SANDLOOM-B035).
- **P3 extras**: one clean venv with ``sandloom[standard,admin]`` (every module
  wheel, Celery and Redis); each subset is chosen with ``SANDLOOM_MODULES``
  through the standard root ``sandloom_standard.main``, what the image and
  the deploy profiles run. The empty subset is the kernel factory with no
  default modules.

The expected outcomes are :func:`_module_subset_matrix.expected`. The CI
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

PACKAGES = ("sandloom", "sandloom-standard", *(d for _, d in MODULES.values()))


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
def p2_runs(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> dict[tuple[str, ...], SubsetRun]:
    root = tmp_path_factory.mktemp("subsets-p2")
    cwd = root / "work"
    cwd.mkdir()

    def run(subset: tuple[str, ...]) -> SubsetRun:
        venv = root / subset_id(subset)
        issuer = "identity" in subset
        python = _venv(
            venv,
            str(wheels["sandloom"]),
            *([str(wheels["sandloom-standard"])] if issuer else []),
            *(str(wheels[MODULES[m][1]]) for m in subset),
        )
        return run_subset(
            subset,
            python=python,
            sandloom=_bin(venv, "sandloom"),
            root="sandloom_standard.main" if issuer else "sandloom.main",
            cwd=cwd,
            env=probe_environment(),
        )

    return run_matrix(run)


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
        extra = (
            {"SANDLOOM_MODULES": json.dumps([MODULES[m][0] for m in subset])}
            if subset
            else {}
        )
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
    check_run(p2_runs[subset], expected(subset, celery=False), celery=False)


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_subset_boots_through_the_extras_p3(
    p3_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    check_run(p3_runs[subset], expected(subset, celery=True), celery=True)
