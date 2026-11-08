"""The sixteen module subsets boot in the repository environment (P3, KB-04).

The repository's environment is the extras' install: ``sandloom[standard]``
(Celery and Redis included) with every module importable, admin from
``packages/editable/sandloom-admin`` (the pytest ``pythonpath``; installed it
would join every composition). Each subset runs in a fresh interpreter with
``SANDLOOM_MODULES`` naming it through the lean root ``sandloom.main``; the
empty subset is the kernel factory with no default modules, since an empty
``SANDLOOM_MODULES`` means the root's defaults. The clean-venv profiles (P2
wheels, P3 extras) are ``test_module_subsets_wheels.py`` (slow).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from _module_subset_matrix import (
    MODULES,
    REPO,
    SUBSETS,
    TASK_MODULES,
    SubsetRun,
    check_run,
    expected,
    probe_environment,
    run_matrix,
    run_subset,
    subset_id,
)

pytestmark = pytest.mark.subprocess_probe

ADMIN_SOURCE = REPO / "packages" / "editable" / "sandloom-admin"


@pytest.fixture(scope="module")
def repository_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, ...], SubsetRun]:
    cwd = tmp_path_factory.mktemp("subsets-repository")
    python = Path(sys.executable)

    def run(subset: tuple[str, ...]) -> SubsetRun:
        extra = {"PYTHONPATH": str(ADMIN_SOURCE)}
        if subset:
            extra["SANDLOOM_MODULES"] = json.dumps([MODULES[m][0] for m in subset])
        return run_subset(
            subset,
            python=python,
            sandloom=python.parent / "sandloom",
            root="sandloom.main" if subset else "kernel",
            cwd=cwd,
            env=probe_environment(extra),
        )

    return run_matrix(run)


def test_the_matrix_has_sixteen_distinct_subsets() -> None:
    assert len(SUBSETS) == 16
    assert len({frozenset(s) for s in SUBSETS}) == 16


def test_task_modules_are_the_modules_with_tasks() -> None:
    """:data:`TASK_MODULES` (what P2 readiness expects) follows the source."""
    with_tasks = {
        name
        for name, (path, _) in MODULES.items()
        if importlib.util.find_spec(f"{path}.tasks") is not None
    }
    assert with_tasks == TASK_MODULES


@pytest.mark.parametrize("subset", SUBSETS, ids=subset_id)
def test_subset_boots_in_the_repository(
    repository_runs: dict[tuple[str, ...], SubsetRun], subset: tuple[str, ...]
) -> None:
    check_run(repository_runs[subset], expected(subset, celery=True), celery=True)
