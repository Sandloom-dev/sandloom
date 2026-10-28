"""pytest plugin: the whole per-module test harness, owned by the framework.

Activate it ONCE per application (``sandloom new`` does this for you)::

    [tool.pytest.ini_options]
    addopts = ["-p", "sandloom.core.testing.plugin"]
    # optional, instead of SANDLOOM_COMPOSITION_ROOT in .env.test:
    # sandloom_composition_root = "myapp.asgi"

Then a business module needs NO ``tests/conftest.py``: its tests just ask for
``client`` / ``db`` / ``user_headers`` / ``make_headers`` (see
:mod:`sandloom.core.testing.fixtures` for the full list).

Deliberately NOT registered as a ``pytest11`` entry point — installing the
framework must never change the behaviour of an unrelated test suite (nor of
the framework's own).

Ordering: ``sandloom.core.config.settings`` snapshots the environment when first
imported,
so ``.env.test`` is loaded in :func:`pytest_load_initial_conftests` — before any
conftest or test module can import the framework. This module (and the
``sandloom.core.testing`` package) imports nothing that reads settings.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from sandloom.core.testing.fixtures import (  # noqa: F401 - re-exported as fixtures
    COMPOSITION_ROOT_INI,
    _sandloom_isolate_module_globals,
    _sandloom_isolate_process_globals,
    _sandloom_release_default_pools,
    ambient_uow,
    async_client,
    client,
    db,
    db_engine,
    db_uow,
    load_test_env,
    make_headers,
    real_uow_client,
    revoked_tokens,
    saga_harness,
    sandloom_app,
    sandloom_cache,
    sandloom_core,
    sandloom_test_env,
    scratch_database,
    test_db,
    test_engine,
    user_headers,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "sandloom_allow_server_errors: the client returns a masked 500 instead of "
        "re-raising the endpoint's exception (SANDLOOM-R006)",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        COMPOSITION_ROOT_INI,
        help="composition root module the sandloom_app fixture boots "
        "(e.g. myapp.asgi); SANDLOOM_COMPOSITION_ROOT wins when set",
        default="",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse another run's database / Redis and a server without room for
    this run's connections — before any test resets anything."""
    from sandloom.core.testing.session_checks import check_run_resources  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it

    check_run_resources(session.config)


@pytest.hookimpl(wrapper=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> Generator[None, int, int]:
    """``-n auto``: no more workers than the PostgreSQL server has room for."""
    from sandloom.core.testing.session_checks import cap_auto_workers  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the hook / call that needs it

    requested = yield
    return int(cap_auto_workers(config, requested))


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> None:
    """Load ``.env.test`` before any conftest imports the framework."""
    starts = [Path(str(early_config.rootpath))]
    for arg in args:
        candidate = Path(str(arg).split("::", 1)[0])
        if not str(arg).startswith("-") and candidate.exists():
            starts.append(candidate)
    load_test_env(starts, warn_if_late=True)
