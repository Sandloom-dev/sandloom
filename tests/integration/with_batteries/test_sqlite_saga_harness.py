"""Nested write units on a database whose writers are serial (SQLite).

Spec ``design/infra-agnostic/spec.md``, "Slice 8c, as built", option (a): a
local contract call, a saga step's marker unit or a child ``open_uow()``
inside a unit of the same task, on the same instance, runs as a SAVEPOINT on
the caller's connection. It commits with the caller: an outer rollback undoes
its rows, its outbox rows and its command-log claim, and its post-commit
stages (outbox dispatch, ``on_commit``) run only after the outer COMMIT. Its
own failure rolls back to the savepoint, and the caller continues. PostgreSQL
(no ``serial_writers``) still opens a fresh, independent unit.

Moved from ``sandloom/core/tests/test_sqlite_nested_units.py`` (KB-05): these tests need
the bundled modules, so they run where the batteries are installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]

#: Saga protocol tests whose steps (the transition unit, the step-started
#: marker unit, the provider's command unit) nest on SQLite.
_SAGA_TESTS = (
    "src/sandloom/core/tests/test_sagas_protocol.py::"
    "test_happy_path_runs_every_step_once_with_derived_keys",
    "src/sandloom/core/tests/test_sagas_protocol.py::"
    "test_duplicate_deliveries_never_run_a_step_twice",
    "src/sandloom/core/tests/test_sagas_recovery.py::"
    "test_refused_before_the_pivot_compensates_the_applied_steps",
    # the harness beside the client fixture: no mark waits for db's lock
    "tests/integration/with_batteries/test_saga_harness_with_client.py::"
    "test_a_saga_run_between_requests_waits_for_no_lock",
)


@pytest.mark.subprocess_probe
def test_saga_steps_run_on_the_sqlite_test_harness(tmp_path: Path) -> None:
    """The real saga protocol (``saga_harness``) on a SQLite test database:
    each step's units nest in the transition's unit and commit with it."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'saga_test.db'}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
        "SANDLOOM_TEST_DATABASE_URL": url,
        "DATABASE_URL": url,
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:xdist",
            "-p",
            "no:randomly",
            *_SAGA_TESTS,
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert f"{len(_SAGA_TESTS)} passed" in proc.stdout, proc.stdout[-2000:]
