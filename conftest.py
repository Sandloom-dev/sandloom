"""Suite-wide fixtures shared by every test path under ``src/``."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values

from sandloom.core.testing.workers import seed_test_environ

#: Settings ``.env.test`` must not leak into the framework suite's snapshot:
#: the suite runs as ``ENVIRONMENT=dev`` (it gates on ``is_prod``, never
#: ``is_test``).
_SEED_SKIP = frozenset({"ENVIRONMENT"})


def _seed_test_env() -> None:
    """Seed the settings snapshot from ``.env.test``, not a developer's ``.env``.

    ``sandloom.core.config.settings`` snapshots the process env and ``.env``
    (relative to the cwd) at import. A fresh checkout has no ``.env``, so every
    run died with ``DATABASE_URL Field required``; a ``.env`` copied from
    ``.env.example`` made the suite depend on dev values. Now ``.env.test``
    fills the process env (it beats ``.env`` in pydantic-settings) for every
    key the shell leaves unset, an explicit ``SANDLOOM_TEST_DATABASE_URL`` /
    ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL`` wins over the
    file and over a shell-exported ``DATABASE_URL`` / ``CACHE_URL`` /
    ``BROKER_URL``, and ``ENVIRONMENT`` stays the dev default. A shell that
    exports ``DATABASE_URL`` itself without an override (CI) is left alone
    (:func:`~sandloom.core.testing.workers.seed_test_environ`).
    """
    env_test = Path(__file__).resolve().parents[0] / ".env.test"
    if not env_test.is_file():
        return
    seed_test_environ(dotenv_values(env_test), skip=_SEED_SKIP)
    if not (Path.cwd() / ".env").is_file():
        print(
            f"sandloom tests: settings from {env_test.name} (no .env needed). Start "
            "the test services first: docker compose -f deploy/compose/test.yml "
            "up -d --wait (docs/modules/testing.md)",
            file=sys.stderr,
        )


_seed_test_env()

from sandloom.core.testing.connections import apply_test_pool_profile  # noqa: E402
from sandloom.core.testing.fixtures import (  # noqa: E402, F401 - autouse fixtures
    _sandloom_cancel_leftover_tasks,
    _sandloom_isolate_module_globals,
    _sandloom_isolate_process_globals,
    end_post_commit_dispatch,
    reset_kept_pools,
    runs_on_session_loop,
)
from sandloom.core.testing.workers import apply_worker_env  # noqa: E402

# pytest-xdist: move this worker onto its own database / Redis DBs BEFORE any
# test module imports the framework (sandloom.core.config.settings snapshots the
# environment at import — e.g. CI's exported DATABASE_URL). Every later
# `.env.test` reload goes through sandloom.core.testing.reload_test_env, which
# re-applies the same rewrite. A no-op without xdist.
apply_worker_env()
# Small pools for a test process (explicit environment values win): one run
# must stay well below the shared server's max_connections
# (sandloom.core.testing.connections). Before sandloom.core.config.settings is imported.
apply_test_pool_profile()


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Ownership of the test database / Redis + the connection budget, once,
    before any test resets anything (sandloom.core.testing.session_checks)."""
    from sandloom.core.testing.session_checks import check_run_resources

    check_run_resources(session.config)


@pytest.hookimpl(wrapper=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> Generator[None, int, int]:
    """``-n auto``: no more workers than the PostgreSQL server has room for."""
    from sandloom.core.testing.session_checks import cap_auto_workers

    requested = yield
    return int(cap_auto_workers(config, requested))


@pytest_asyncio.fixture(autouse=True)
async def _release_default_engine_pool(
    request: pytest.FixtureRequest,
) -> AsyncIterator[None]:
    """End the test's post-commit dispatch; return the process-wide
    ``default`` engine's pool after a sync test.

    The suite runs every async test on one event loop per process
    (``asyncio_default_test_loop_scope = "session"``), so the pooled asyncpg
    connections an async test opened stay usable by the next one (and get
    ``db_engine``'s per-test reset: ``reset_kept_pools``). A sync test
    may have opened some on a loop of its own (``asyncio.run``), which is gone
    when it ends: those are closed here, on this loop, so the next test never
    checks out a connection "attached to a different loop". The engine object
    is kept; providers holding it reconnect.
    """
    yield
    await end_post_commit_dispatch()
    if runs_on_session_loop(request.node):
        reset_kept_pools()
        return
    from sandloom.core.drivers import sqlalchemy_database

    for engine in (
        sqlalchemy_database._default_engine,  # test seam
        sqlalchemy_database._default_contract_engine,
    ):
        if engine is not None:
            await engine.dispose()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark the guarantee ledger's enforcing tests ``guarantees``.

    ``uv run pytest -m guarantees src`` then runs exactly the tests that prove
    the ledger (``guarantees.yml``) — what a simplification wave runs
    first. The ledger is read only when ``-m`` mentions the marker.
    """
    if "guarantees" not in (config.getoption("markexpr", "") or ""):
        return

    script = Path(__file__).resolve().parents[0] / "tools" / "checks" / "guarantees.py"
    spec = importlib.util.spec_from_file_location("sandloom_guarantee_ledger", script)
    if spec is None or spec.loader is None:
        return
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    ids = module.ledger_nodeids_for_marker()
    marker = pytest.mark.guarantees
    for item in items:
        if module.item_matches(item.nodeid, ids):
            item.add_marker(marker)
