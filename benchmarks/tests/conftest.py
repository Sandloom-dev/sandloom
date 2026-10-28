"""S5 functional/security tests run on the FRAMEWORK's test harness.

Run from the repository root with its venv (test Postgres + Redis up)::

    docker compose -f deploy/compose/test.yml up -d --wait
    PYTHONPATH=benchmarks:src uv run pytest -c pyproject.toml \\
        benchmarks/tests -q --no-cov

``src`` on the path gives the wire-protocol counting proxy of the framework's
perf-budget suite (``tests.perf_budgets.pgcount``).

The suite runs under pytest-xdist (``-n auto --dist loadfile``, as CI does):
like ``conftest.py`` — which is not on this path — every worker is
moved onto its own database / Redis DBs before a test module imports the
framework, and the app role is per worker (``test_s5_secure_read.ROLE``).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.fixtures import load_test_env

# Before any test module imports sandloom.core.config.settings (it snapshots the
# environment), as the sandloom.core.testing plugin does: .env.test (what
# the sandloom_test_env fixture loads anyway; the xdist controller allocates the
# workers' Redis indexes from it), the SANDLOOM_TEST_* overrides, the small pools
# of a test process, and this xdist worker's own database / Redis DBs.
load_test_env([Path(__file__).resolve().parents[2]])


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "s5_overflow(n): the s5 fixture's pool of one gets n overflow connections",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Ownership of the test database / Redis, the connection budget and the
    workers' Redis indexes (``SANDLOOM_TEST_REDIS_DB_STRIDE=auto``), once, before
    any test resets anything."""
    from sandloom.core.testing.session_checks import check_run_resources  # noqa: PLC0415

    check_run_resources(session.config)


@pytest.hookimpl(wrapper=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> Generator[None, int, int]:
    """``-n auto``: no more workers than the PostgreSQL server has room for."""
    from sandloom.core.testing.session_checks import cap_auto_workers  # noqa: PLC0415

    requested = yield
    return int(cap_auto_workers(config, requested))
