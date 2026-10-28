"""The fixtures behind the ``sandloom.core.testing`` pytest plugin.

An application activates them all at once with
``addopts = ["-p", "sandloom.core.testing.plugin"]`` (see :mod:`.plugin`). A module
scaffolded INTO the framework tree (whose own suite does not load the plugin)
pulls them in from a one-line ``conftest.py``::

    from sandloom.core.testing.fixtures import *  # noqa: F403

Fixtures
--------
``sandloom_app`` (session)
    The composition-root ASGI app (``SANDLOOM_COMPOSITION_ROOT``, else the
    ``sandloom_composition_root`` ini option, else ``sandloom.main``). Building it
    registers every loaded module's models on the shared metadata.
``db_engine``
    An engine on the TEST ``DATABASE_URL`` (refused unless the database name
    contains ``test``, even with ``ENVIRONMENT=test``) with every table present and
    empty (built once per process, TRUNCATEd afterwards).
``db``
    An ``AsyncSession`` on it, rolled back after the test.
``client``
    ``httpx.AsyncClient`` on ``sandloom_app`` whose requests share ``db`` (bound as
    the UoW, so ``publish_event`` / ``record_audit(required=True)`` land in the
    test transaction; ``on_commit`` callbacks run after a successful request).
``real_uow_client``
    ``httpx.AsyncClient`` on ``sandloom_app`` WITHOUT the ``db`` override: every
    request runs through the real request UoW (commit on success, rollback on
    error) on the test database; unhandled errors become 500 responses.
``make_headers``
    Factory ``(user_id=None, tenant_id=None, **claims) -> (user_id, headers)``
    minting a real access token (:func:`sandloom.core.testing.make_user_headers`;
    ``email=`` / ``email_verified=`` / ``tenant=False`` shape the claims).
``user_headers``
    Headers of one default user (random user + tenant).
``sandloom_core``
    The app's ``CoreFacade`` (session-scoped).
``db_uow``
    ``db`` bound as the Unit of Work for the whole test: services called
    directly on it (not through ``client``) ``publish_event`` into its outbox
    and ``record_audit`` into its ``audit_log`` — rows that join the test's
    transaction and roll back with it. Since 0.7 both raise outside a UoW.
``ambient_uow``
    An in-memory recording UoW bound for the whole test (a
    :class:`~sandloom.core.testing.RecordingSession`): services called directly
    (not through ``client``) can ``record_audit(required=True)`` /
    ``publish_event`` and the test asserts ``.audits`` / ``.events``.
``revoked_tokens``
    Records ``revoke_user_tokens`` calls (the user ids) instead of writing
    the revocation cache; ``revoked_tokens.available = False`` simulates an
    unreachable revocation store (the mark is reported as not written).
``sandloom_cache``
    The app's ``default`` cache pointed at ``.env.test``'s ``CACHE_URL`` for
    the test, FLUSHDB'd before it (yields the ``CacheProvider``; the previous
    instance is restored afterwards).
``scratch_database``
    A throw-away database on the test server (sync URL), dropped after the
    test — for a module's migration tests (:mod:`sandloom.core.testing.migrations`).
``test_db`` / ``async_client`` / ``test_engine``
    Back-compat aliases of ``db`` / ``client`` / ``db_engine``.

Every test also returns the ``default`` engines' pooled connections on its own
event loop afterwards, so loop-bound pools never leak into the next test, and
gets the process's runtime bindings (active core / infra / event bus, the
settings' module lists) restored, so an app a test built never leaks into the
next test (:mod:`sandloom.core.testing.isolation`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import sandloom.core.testing as _testing
import sandloom.core.testing.env as _env
from sandloom.core.testing import isolation
from sandloom.core.testing.auth import make_user_headers
from sandloom.core.testing.infra import override_instance
from sandloom.core.testing.server_errors import ALLOW_MARKER, raise_server_errors

#: Test-environment file the harness loads (walking up the directory tree).
ENV_FILE = _env.ENV_FILE
find_env_file = _env.find_env_file
#: ini option naming the composition root (alternative to the env var).
COMPOSITION_ROOT_INI = "sandloom_composition_root"


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def load_test_env(starts: Iterable[Path], *, warn_if_late: bool = False) -> Path:
    """Load the nearest ``.env.test`` (:func:`sandloom.core.testing.env.load_test_env`);
    a missing file is a pytest usage error."""
    try:
        return _env.load_test_env(starts, warn_if_late=warn_if_late)
    except _env.MissingTestEnvError as exc:
        raise pytest.UsageError(str(exc)) from None


def env_starts(config: pytest.Config) -> list[Path]:
    """Where to look for ``.env.test``: the rootdir, then each test path."""
    starts = [Path(str(config.rootpath))]
    for arg in config.invocation_params.args:
        candidate = Path(str(arg).split("::", 1)[0])
        if not str(arg).startswith("-") and candidate.exists():
            starts.append(candidate)
    return starts


@pytest.fixture(scope="session", autouse=True)
def sandloom_test_env(request: pytest.FixtureRequest) -> Path:
    """Load the nearest ``.env.test`` (rootdir first, then the test paths)."""
    return load_test_env(env_starts(request.config))


# --------------------------------------------------------------------------- #
# Application / database
# --------------------------------------------------------------------------- #


def _composition_root(config: pytest.Config) -> str | None:
    from sandloom.core.composition import COMPOSITION_ROOT_ENV  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    if os.getenv(COMPOSITION_ROOT_ENV):
        return None  # load_composition_app reads the env var itself
    try:
        value = config.getini(COMPOSITION_ROOT_INI)
    except ValueError:  # ini option not registered (fixtures used w/o plugin)
        return None
    return str(value).strip() or None


def harness_app(config: pytest.Config) -> Any:
    """The composition-root app, built once per process, and its runtime as
    the isolation baseline.

    The composition root's module is imported once, so every caller gets the
    same app. Building it binds it as the process's active runtime: that is
    the state EVERY test module starts from (see ``_sandloom_isolate_*``), whether
    or not an earlier test asked for ``sandloom_app`` first.
    """
    from sandloom.core.composition import load_composition_app  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    app = load_composition_app(_composition_root(config))
    if not isolation.baseline_is(app):
        isolation.set_baseline(app=app)
    return app


@pytest.fixture(scope="session")
def sandloom_app(pytestconfig: pytest.Config, sandloom_test_env: Path) -> Any:
    """The application's ASGI app, built by its composition root."""
    return harness_app(pytestconfig)


@pytest_asyncio.fixture
async def db_engine(sandloom_app: Any) -> AsyncIterator[Any]:
    """Engine on the test database; every table present and empty."""
    from sandloom.core.testing.database import open_test_engine  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

    async with open_test_engine() as engine:
        yield engine


@pytest_asyncio.fixture
async def db(db_engine: Any) -> AsyncIterator[Any]:
    """A session on the test database, rolled back after the test."""
    from sandloom.core.testing.database import open_test_session  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

    async with open_test_session(db_engine) as session:
        yield session


def _surface_server_errors(request: Any, http: Any) -> Any:
    """Re-raise a masked 500's exception unless the test opts out (SANDLOOM-R006)."""
    if request.node.get_closest_marker(ALLOW_MARKER) is not None:
        return nullcontext(http)
    return raise_server_errors(http)


@pytest_asyncio.fixture
async def client(
    sandloom_app: Any, db: Any, request: pytest.FixtureRequest
) -> AsyncIterator[Any]:
    """HTTP client on the app; every request runs on the test's ``db``.

    Shared-session semantics: every request uses the test's ONE ``db``
    session (bound as the UoW), through ``DbSession`` and through ``db()``
    alike (:class:`sandloom.core.testing._uow.SharedRequestUnit`), so nothing commits
    and a
    request that fails with a 4xx does NOT roll its writes back: the rows a
    failed request flushed stay visible to the test (and to later requests)
    until the test ends. Assert rollback behaviour with ``real_uow_client``,
    whose requests commit or roll back exactly as in production (both paths).

    An endpoint that raises an unhandled exception re-raises it here
    (:class:`~sandloom.core.testing.server_errors.UnhandledServerError`: type,
    message, fix) instead of returning a bare ``500 internal_error``; mark a
    test ``@pytest.mark.sandloom_allow_server_errors`` to get the 500 response.
    """
    import httpx  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

    from sandloom.core._internal.db_provider import get_db  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded
    from sandloom.core.testing._uow import (  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded
        bind_test_uow,
        shared_request_unit,
    )

    async def _override_get_db() -> AsyncIterator[Any]:
        # Bound as the UoW: outbox / required-audit rows join the test's
        # transaction. The framework runs on_commit callbacks after a
        # successful request on this override path (never after a failure).
        with bind_test_uow(db):
            yield db

    sandloom_app.dependency_overrides[get_db] = _override_get_db
    try:
        # db() in a request is the same shared session (not the lazy unit's
        # own connection): how a route obtains its session never changes the
        # test's semantics
        with shared_request_unit(sandloom_app.state.core, db):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sandloom_app), base_url="http://test"
            ) as http:
                with _surface_server_errors(request, http):
                    yield http
    finally:
        sandloom_app.dependency_overrides.pop(get_db, None)


def _configured_driver(core: Any, capability: str, fallback: str) -> str:
    config = core.infra.instance_config(capability)
    return str(getattr(config, "driver", None) or fallback)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set: add it to {ENV_FILE}")
    return value


@pytest.fixture(scope="session")
def sandloom_core(sandloom_app: Any) -> Any:
    """The application's ``CoreFacade``."""
    return sandloom_app.state.core


@pytest_asyncio.fixture
async def sandloom_cache(sandloom_core: Any) -> AsyncIterator[Any]:
    """The app's ``default`` cache on the test ``CACHE_URL``, emptied first.

    Yields the ``CacheProvider`` the app itself resolves (``core.cache()``,
    idempotency, rate limits, token revocation). The Redis database is
    FLUSHDB'd before the test so keys never leak between tests — so
    ``CACHE_URL`` must name a disposable test database. Also needed where the
    app was imported before ``.env.test`` was loaded (its settings snapshot
    names another cache), as in the framework's own suite.
    """
    driver = _configured_driver(sandloom_core, "cache", "redis")
    async with override_instance(
        sandloom_core, "cache", driver=driver, url=_required_env("CACHE_URL")
    ) as registry:
        provider = registry.provider("cache")
        raw = getattr(provider, "raw_client", None)
        if raw is not None:
            from sandloom.core.testing.ownership import flush_owned_redis  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

            client = raw()
            if hasattr(client, "__await__"):
                client = await client
            await flush_owned_redis(client)
        yield provider


@pytest_asyncio.fixture
async def real_uow_client(
    sandloom_app: Any,
    sandloom_core: Any,
    db_engine: Any,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Any]:
    """HTTP client whose requests run through the REAL request UoW.

    No ``get_db`` override: the app's ``default`` database (and cache) are
    pointed at the test ``DATABASE_URL`` / ``CACHE_URL``, so every request
    commits or rolls back exactly as in production — a 4xx rolls its writes
    back (committed rows are emptied by the next test's ``db_engine``). An
    unhandled endpoint error re-raises like with ``client``
    (``@pytest.mark.sandloom_allow_server_errors`` keeps the 500 response).
    """
    import httpx  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

    from sandloom.core._internal.db_provider import get_db  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    if get_db in sandloom_app.dependency_overrides:
        raise RuntimeError(
            "real_uow_client cannot be combined with `client` in one test: "
            "`client` overrides the request session"
        )
    async with AsyncExitStack() as stack:
        for capability, env, fallback in (
            ("database", "DATABASE_URL", "sqlalchemy"),
            ("cache", "CACHE_URL", "redis"),
        ):
            await stack.enter_async_context(
                override_instance(
                    sandloom_core,
                    capability,
                    driver=_configured_driver(sandloom_core, capability, fallback),
                    url=_required_env(env),
                )
            )
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=sandloom_app, raise_app_exceptions=False
                ),
                base_url="http://test",
            )
        )
        stack.enter_context(_surface_server_errors(request, http))
        yield http


@pytest_asyncio.fixture
async def saga_harness(sandloom_core: Any, db_engine: Any) -> AsyncIterator[Any]:
    """Drive the REAL saga protocol synchronously (``sandloom.core.testing.sagas``).

    The app's ``default`` database points at the test database; every
    transition commits for real (rows are emptied by the next test's
    ``db_engine``). ``run()`` / ``fail_step`` / ``crash_after`` /
    ``deliver_twice`` / ``clock.advance`` / ``fake_contract``.
    """
    async with _testing.open_saga_harness(sandloom_core) as harness:
        yield harness


@pytest_asyncio.fixture
async def db_uow(db: Any) -> AsyncIterator[Any]:
    """``db`` bound as the UoW for the whole test (outbox / audit rows join it)."""
    from sandloom.core.testing._uow import bind_test_uow  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    with bind_test_uow(db):
        yield db


@pytest_asyncio.fixture
async def ambient_uow() -> AsyncIterator[Any]:
    """Bind an in-memory recording UoW for the whole test."""
    from sandloom.core.testing._uow import recording_uow  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    with recording_uow() as session:
        yield session


class RevokedTokens(list[Any]):
    """The user ids ``revoke_user_tokens`` was called for (``revoked_tokens``).

    Set ``available = False`` to simulate an unreachable revocation store:
    every later call records nothing and reports the mark as NOT written, so
    an immediate revocation (``require_immediate=True``) refuses the change
    (503 / ``RevocationUnavailableError``).
    """

    available: bool = True


@pytest.fixture
def revoked_tokens(monkeypatch: pytest.MonkeyPatch) -> RevokedTokens:
    """Record ``revoke_user_tokens(user_id)`` calls instead of running them.

    Keeps API tests deterministic (a token minted in the same second as a
    revocation would otherwise flip to 401) and lets the test assert who was
    revoked; enforcement itself is covered by the core auth tests. A
    :class:`RevokedTokens` list: ``.available = False`` simulates an outage.
    """
    from sandloom.core.auth import revocation  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    calls = RevokedTokens()

    async def _record(user_id: Any, *, valid_after: int | None = None) -> bool:
        if not calls.available:
            return False  # store unreachable: the mark was not written
        calls.append(user_id)
        return True

    monkeypatch.setattr(revocation, "revoke_user_tokens", _record)
    return calls


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


@pytest.fixture
def scratch_database(sandloom_test_env: Path) -> Iterator[str]:
    """A throw-away database on the test server (sync URL), dropped after.

    For a module's migration tests (``sandloom.core.testing.migrations``): walk its
    chain up and down without touching the shared test database.
    """
    from sandloom.core.testing.migrations import temporary_database  # noqa: PLC0415 - cold: plugin entry - pytest imports it on every run; the heavy harness part loads with the fixture that needs it

    with temporary_database() as url:
        yield url


@pytest.fixture
def make_headers(sandloom_test_env: Path) -> Callable[..., tuple[Any, dict[str, str]]]:
    """Factory: ``make_headers(user_id=None, tenant_id=None, **claims)``."""
    return make_user_headers


@pytest.fixture
def user_headers(
    make_headers: Callable[..., tuple[Any, dict[str, str]]],
) -> dict[str, str]:
    """Headers of a default user (random user id and tenant)."""
    return make_headers()[1]


# --------------------------------------------------------------------------- #
# Back-compat aliases (the names module conftests used to define)
# --------------------------------------------------------------------------- #


@pytest.fixture
def test_engine(db_engine: Any) -> Any:
    return db_engine


@pytest.fixture
def test_db(db: Any) -> Any:
    return db


@pytest.fixture
def async_client(client: Any) -> Any:
    return client


# --------------------------------------------------------------------------- #
# Process-global runtime isolation
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module", autouse=True)
def _sandloom_isolate_module_globals(request: pytest.FixtureRequest) -> Iterator[None]:
    """Start every test module from the session baseline (the harness app's
    runtime) and put the process's runtime bindings back to it afterwards.

    The baseline is the same whatever ran before: the harness app is built
    before the first module (:func:`harness_app`), so a test that resolves the
    process's active core / infra / event bus without asking for ``sandloom_app``
    passes alone, first or after any other file. A module-scoped fixture that
    built its own app (``create_app`` binds it as the process's active core /
    infra / event bus) keeps it for its own tests only; the next module never
    resolves ``db_session()`` through it.
    """
    harness_app(request.config)
    isolation.restore(isolation.baseline())
    yield
    isolation.restore(isolation.baseline())


@pytest.fixture(autouse=True)
def _sandloom_isolate_process_globals() -> Iterator[None]:
    """Restore the process's runtime bindings after every test.

    ``create_app`` rebinds the active core, infrastructure registry, event bus
    and the settings' module lists (:mod:`sandloom.core.testing.isolation`); a test
    that builds an app must not hand that state to the next test. What a test
    registers on the bound core (a channel, a service, a startup task, a
    declared operation, ...) goes too (``CoreFacade.restore_registrations``),
    and the process caches it warmed are emptied (``PROCESS_CACHES``).
    """
    snapshot = isolation.capture()
    registrations = isolation.capture_registrations()
    yield
    isolation.restore(snapshot)
    isolation.restore_registrations(registrations)
    isolation.clear_process_caches()


# --------------------------------------------------------------------------- #
# Per-test pool hygiene
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(autouse=True)
async def _sandloom_release_default_pools() -> AsyncIterator[None]:
    """Return the ``default`` engines' pooled connections after every test.

    Pooled asyncpg connections belong to the loop of the test that opened
    them; pytest-asyncio gives each test its own loop, so a later test reusing
    one fails with "attached to a different loop". Disposing on the test's own
    loop closes them cleanly; the engines survive and reconnect next time.
    """
    yield
    if "sandloom.core.unit_of_work" in sys.modules:
        # Background post-commit dispatch of this test's requests runs on the
        # test's loop: finish it (it may mark rows on the pools) before the
        # pools are released and the loop closes.
        from sandloom.core.unit_of_work.dispatch import drain_post_commit  # noqa: PLC0415 - cold: only when a test already loaded the UoW / default database

        try:
            await drain_post_commit(timeout=10)
        except Exception:  # noqa: BLE001, S110 - never fail teardown on it
            pass
    if "sandloom.core._internal.db_provider" not in sys.modules:
        return  # nothing touched the default database
    from sandloom.core._internal.db_provider import (  # noqa: PLC0415 - cold: only when a test already loaded the UoW / default database
        release_default_pools,
    )

    await release_default_pools()


__all__ = [
    "COMPOSITION_ROOT_INI",
    "ENV_FILE",
    "sandloom_app",
    "sandloom_core",
    "sandloom_cache",
    "sandloom_test_env",
    "ambient_uow",
    "async_client",
    "client",
    "db",
    "db_engine",
    "db_uow",
    "env_starts",
    "find_env_file",
    "load_test_env",
    "make_headers",
    "RevokedTokens",
    "real_uow_client",
    "revoked_tokens",
    "saga_harness",
    "scratch_database",
    "test_db",
    "test_engine",
    "user_headers",
    "_sandloom_isolate_module_globals",
    "_sandloom_isolate_process_globals",
    "_sandloom_release_default_pools",
]
