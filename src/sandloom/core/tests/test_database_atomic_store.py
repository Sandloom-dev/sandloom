"""The ``database`` atomic_store driver (PostgreSQL and SQLite) against the kit,
and what the kit cannot know: the table's pin to the model and to the backup
list, the unlogged-table refusal, the identity, the sweeper and the features.

The kit runs on SQLite (a file of its own per test) and on PostgreSQL
(``settings.database_url``; a scratch database of its own, created for the run
and dropped after). The PostgreSQL class is skipped on the second-db lane, whose
database is SQLite.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sqlite3
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Table, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.database_atomic_store import (
    _TABLE as DRIVER_TABLE,
)
from sandloom.core.drivers.database_atomic_store import (
    DatabaseAtomicStoreOptions,
    DatabaseAtomicStoreProvider,
)
from sandloom.core.infra.errors import InvalidArgument, Timeout, Unavailable
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
from sandloom.core.migrations.backup import CORE_TABLES
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.models.atomic_store import InfraAtomicStore
from sandloom.driverkit.testing import AtomicStoreConformance

# The harness resolves the URL into ``settings`` (SANDLOOM_TEST_* on a shared
# machine, DATABASE_URL on CI): never read the environment here.
_PG_URL = settings.database_url
_ON_POSTGRES = make_url(_PG_URL).get_backend_name() == "postgresql"
_VALUE_LIMIT = 8192
_POSTGRES_ONLY = pytest.mark.skipif(
    not _ON_POSTGRES, reason="the database lane is not PostgreSQL (the second-db lane)"
)


def _provider(url: str, **options: Any) -> DatabaseAtomicStoreProvider:
    options.setdefault("max_value_bytes", _VALUE_LIMIT)
    return DatabaseAtomicStoreProvider(
        name="security", driver="database", options={"url": url, **options}
    )


async def _create_table(url: str, *, unlogged: bool = False) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if unlogged:
                await conn.execute(
                    text(
                        "CREATE UNLOGGED TABLE infra_atomic_store (key varchar(1024) "
                        "PRIMARY KEY, value text NOT NULL, expires_at timestamptz)"
                    )
                )
            else:
                table: Table = InfraAtomicStore.__table__  # type: ignore[assignment]
                await conn.run_sync(table.create, checkfirst=True)
    finally:
        await engine.dispose()


# ----------------------------------------------------------------- SQLite
@pytest_asyncio.fixture
async def sqlite_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'atomic.db'}"
    await _create_table(url)
    return url


class TestDatabaseAtomicStoreOnSqlite(AtomicStoreConformance):
    value_limit_bytes = _VALUE_LIMIT

    @pytest.fixture
    def make_provider(self, sqlite_url: str) -> Callable[[], AtomicStoreProvider]:
        return lambda: _provider(sqlite_url)

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], AtomicStoreProvider]:
        url = f"sqlite+aiosqlite:////no/such/dir/{self.leak_marker}/atomic.db"
        return lambda: _provider(url)

    # A SQLite file belongs to one host: it declares ``never_evicts`` but not
    # ``shared``, and a file has no eviction (the unlogged-table refusal is a
    # PostgreSQL storage class). These groups do not apply; a ``None`` attribute
    # is not collected, where a skip would be reported on every lane
    # (tools/ci/skip_ledger.py).
    test_shared_two_providers_of_one_store_see_each_other = None  # type: ignore[assignment]
    test_never_evicts_the_driver_refuses_an_evicting_backend = None  # type: ignore[assignment]
    test_never_evicts_the_refusal_holds_when_the_operator_vouches = None  # type: ignore[assignment]


# ------------------------------------------------------------- PostgreSQL
@pytest_asyncio.fixture
async def pg_scratch_url() -> AsyncIterator[Callable[..., Any]]:
    """A factory of scratch PostgreSQL databases holding ``infra_atomic_store``
    (``unlogged=True``: as an unlogged table), all dropped after the test."""
    if not _ON_POSTGRES:
        pytest.skip("the database lane is not PostgreSQL (the second-db lane)")
    admin_url = make_url(_PG_URL)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    async def make(*, unlogged: bool = False) -> str:
        scratch = f"atomic_kit_{uuid.uuid4().hex[:10]}"
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        created.append(scratch)
        url = admin_url.set(database=scratch).render_as_string(hide_password=False)
        await _create_table(url, unlogged=unlogged)
        return url

    try:
        yield make
    finally:
        async with admin.connect() as conn:
            for scratch in created:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
                )
        await admin.dispose()


@pytest.mark.skipif(
    not _ON_POSTGRES, reason="the database lane is not PostgreSQL (the second-db lane)"
)
class TestDatabaseAtomicStoreOnPostgres(AtomicStoreConformance):
    value_limit_bytes = _VALUE_LIMIT

    @pytest_asyncio.fixture
    async def pg_url(self, pg_scratch_url: Callable[..., Any]) -> str:
        url: str = await pg_scratch_url()
        return url

    @pytest_asyncio.fixture
    async def unlogged_url(self, pg_scratch_url: Callable[..., Any]) -> str:
        url: str = await pg_scratch_url(unlogged=True)
        return url

    @pytest.fixture
    def make_provider(self, pg_url: str) -> Callable[[], AtomicStoreProvider]:
        return lambda: _provider(pg_url)

    @pytest.fixture
    def make_failing_provider(self) -> Callable[[], AtomicStoreProvider]:
        url = f"postgresql+asyncpg://user:{self.leak_marker}@127.0.0.1:1/none"
        return lambda: _provider(url)

    @pytest.fixture
    def make_evicting_provider(
        self, unlogged_url: str
    ) -> Callable[[], AtomicStoreProvider]:
        # The database's way to lose a key before its TTL: an unlogged table is
        # emptied by a crash, so the driver refuses it.
        return lambda: _provider(unlogged_url)

    # The driver has no operator override of its check.
    test_never_evicts_the_refusal_holds_when_the_operator_vouches = None  # type: ignore[assignment]


# ------------------------------------------------- what the kit cannot know
def test_the_features_are_shared_only_on_postgresql() -> None:
    pg = {"url": "postgresql+asyncpg://u:p@db/x"}
    lite = {"url": "sqlite+aiosqlite:///x.db"}
    assert DatabaseAtomicStoreProvider.features_for(pg) == {"shared", "never_evicts"}
    assert DatabaseAtomicStoreProvider.features_for(lite) == {"never_evicts"}
    assert DatabaseAtomicStoreProvider.features_for({}) == {"never_evicts"}
    provider = DatabaseAtomicStoreProvider(name="s", driver="database", options=lite)
    assert provider.features() == {"never_evicts"}
    assert provider.development_only is False


def test_only_the_one_statement_operations_are_hinted() -> None:
    assert set(DatabaseAtomicStoreProvider.performance_hints) == {
        "get",
        "compare_and_set",
    }


def test_the_driver_options_reject_unknown_keys_and_bad_bounds() -> None:
    with pytest.raises(ValueError):
        DatabaseAtomicStoreOptions(ur1="x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        DatabaseAtomicStoreOptions(max_attempts=1)


async def test_an_instance_without_a_url_is_invalid() -> None:
    provider = DatabaseAtomicStoreProvider(name="s", driver="database", options={})
    with pytest.raises(InvalidArgument):
        await provider.aopen()


async def test_another_dialect_is_refused() -> None:
    provider = _provider("mysql+aiomysql://u:p@db/x")
    with pytest.raises(InvalidArgument):
        await provider.aopen()


async def test_a_missing_table_is_unavailable_and_health_fails(tmp_path: Path) -> None:
    provider = _provider(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(Unavailable):
        await provider.aopen()
    health = await provider.health()
    assert health.status == "fail"
    assert health.details == {"error": "Unavailable"}
    await provider.aclose()


def test_the_driver_table_is_the_model_and_the_backup_list() -> None:
    model: Table = InfraAtomicStore.__table__  # type: ignore[assignment]
    assert DRIVER_TABLE.name == model.name == "infra_atomic_store"
    assert [c.name for c in DRIVER_TABLE.columns] == [c.name for c in model.columns]
    for column in DRIVER_TABLE.columns:
        modelled = model.c[column.name]
        assert type(column.type) is type(modelled.type), column.name
        assert column.nullable == modelled.nullable, column.name
        assert column.primary_key == modelled.primary_key, column.name
    assert "infra_atomic_store" in CORE_TABLES
    # The storage parameters are PostgreSQL's: the model carries none.
    assert model.dialect_options["postgresql"]["with"] is None


def test_the_migration_creates_the_table_logged_with_the_storage_parameters() -> None:
    source = (
        Path(__file__).resolve().parents[4]
        / "migrations/core/versions/0015_infra_atomic_store.py"
    )
    if not source.exists():  # an installed wheel carries the chain elsewhere
        pytest.skip("the migration source is not in this tree")
    body = source.read_text()
    assert "prefixes" not in body  # never ``prefixes=["UNLOGGED"]``
    for pinned in (
        '"fillfactor": 70',
        '"autovacuum_vacuum_scale_factor": 0.02',
        '"autovacuum_vacuum_threshold": 50',
        '"autovacuum_analyze_scale_factor": 0.05',
    ):
        assert pinned in body


def test_the_registry_builds_the_database_driver_as_an_option() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry())
    registration = registry._registration("atomic_store", "database")
    assert registration is not None
    assert not registration.delivered_by


@_POSTGRES_ONLY
async def test_the_identity_names_the_server_and_the_database(
    pg_scratch_url: Callable[..., Any],
) -> None:
    url = await pg_scratch_url()
    first, second = _provider(url), _provider(url)
    try:
        identity = await first.isolation_identity()
        assert identity is not None
        assert identity.startswith("database:")
        assert identity.endswith(f"/{make_url(url).database}")
        assert identity == await second.isolation_identity()
        assert "password" not in identity
        assert "test_password" not in identity
    finally:
        await first.aclose()
        await second.aclose()


async def test_the_sqlite_identity_names_the_file_not_its_directory(
    sqlite_url: str, tmp_path: Path
) -> None:
    provider = _provider(sqlite_url)
    other = _provider(f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    identity = await provider.isolation_identity()
    assert identity is not None and identity.startswith("database:sqlite-")
    assert identity.endswith("/atomic.db")
    assert identity != await other.isolation_identity()


@_POSTGRES_ONLY
async def test_a_refused_table_fails_health_and_every_operation(
    pg_scratch_url: Callable[..., Any],
) -> None:
    provider = _provider(await pg_scratch_url(unlogged=True))
    with pytest.raises(InvalidArgument):
        await provider.aopen()
    assert (await provider.health()).details == {"reason": "table_not_logged"}
    with pytest.raises(InvalidArgument):
        await provider.set_subject_state("k", "v1:after:1")
    await provider.aclose()


@_POSTGRES_ONLY
async def test_a_table_made_unlogged_after_open_fails_health(
    pg_scratch_url: Callable[..., Any],
) -> None:
    url = await pg_scratch_url()
    provider = _provider(url)
    await provider.aopen()
    try:
        assert (await provider.health()).status == "ok"
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text("ALTER TABLE infra_atomic_store SET UNLOGGED"))
        await engine.dispose()
        assert (await provider.health()).details == {"reason": "table_not_logged"}
        # Fail closed: the check that found the table unlogged refuses every operation.
        with pytest.raises(InvalidArgument):
            await provider.get("k")
        with pytest.raises(InvalidArgument):
            await provider.set_subject_state("k", "v1:after:1")
    finally:
        await provider.aclose()


async def test_a_non_expiring_key_survives_and_a_ttl_key_expires(
    sqlite_url: str,
) -> None:
    provider = _provider(sqlite_url)
    try:
        assert await provider.add("pending", "x") is True
        assert await provider.add("short", "y", ttl_seconds=1) is True
        await asyncio.sleep(2.1)
        assert await provider.get("pending") == "x"
        assert await provider.get("short") is None
        # Still in the table, absent for every statement, until the sweeper runs.
        assert await provider.add("short", "z", ttl_seconds=5) is True
        assert await provider.get("short") == "z"
    finally:
        await provider.aclose()


async def test_a_losing_insert_of_set_subject_state_reads_again(
    sqlite_url: str,
) -> None:
    provider = _provider(sqlite_url, max_attempts=2)
    try:
        assert await provider.set_subject_state("k", "v1:after:5") == "v1:after:5"
        assert await provider.set_subject_state("k", "v1:after:4") == "v1:after:5"
        assert await provider.set_subject_state("k", "v1:blocked:1") == "v1:blocked:1"
    finally:
        await provider.aclose()


# --------------------------------------------------- the writers' turn on SQLite
async def test_sqlite_writers_wait_for_their_turn_outside_the_statement_deadline(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten writers of 0.1 s each take 1 s together, past a 0.4 s deadline: each
    one's own statement fits it, so none may raise ``Timeout`` for the wait."""
    original = DatabaseAtomicStoreProvider._write_if_higher

    async def slow(*args: Any, **kwargs: Any) -> str | None:
        held = await original(*args, **kwargs)
        await asyncio.sleep(0.1)  # the write lock is still held
        return held

    monkeypatch.setattr(
        DatabaseAtomicStoreProvider, "_write_if_higher", staticmethod(slow)
    )
    provider = _provider(sqlite_url, statement_timeout_seconds=0.4)
    try:
        await asyncio.gather(
            *(provider.set_subject_state("k", f"v1:after:{i}") for i in range(10))
        )
        assert await provider.get("k") == "v1:after:9"
    finally:
        await provider.aclose()


async def test_a_statement_past_the_deadline_still_raises_timeout_on_sqlite(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn moves the wait out of the deadline; a statement that is itself
    slow keeps its ``Timeout``."""
    original = DatabaseAtomicStoreProvider._write_if_higher

    async def stuck(*args: Any, **kwargs: Any) -> str | None:
        held = await original(*args, **kwargs)
        await asyncio.sleep(1)
        return held

    monkeypatch.setattr(
        DatabaseAtomicStoreProvider, "_write_if_higher", staticmethod(stuck)
    )
    provider = _provider(sqlite_url, statement_timeout_seconds=0.2)
    try:
        with pytest.raises(Timeout):
            await provider.set_subject_state("k", "v1:after:1")
    finally:
        await provider.aclose()


def test_one_provider_serves_writers_from_two_event_loops(tmp_path: Path) -> None:
    """A lock binds to the loop that first waits on it: the turn is per loop."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'loops.db'}"
    asyncio.run(_create_table(url))
    provider = _provider(url)

    async def burst(tag: str) -> None:
        await asyncio.gather(
            *(provider.set_subject_state(f"{tag}", f"v1:after:{i}") for i in range(10))
        )
        assert await provider.get(tag) == "v1:after:9"

    try:
        asyncio.run(burst("first"))
        asyncio.run(burst("second"))  # the second loop meets the first one's lock
    finally:
        asyncio.run(provider.aclose())


async def test_a_forked_child_does_not_inherit_a_held_turn(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn held by the parent at ``fork`` stays held in the child for ever:
    a new process id starts the store with free turns."""
    release, entered = asyncio.Event(), asyncio.Event()
    original = DatabaseAtomicStoreProvider._write_if_higher
    calls = 0

    async def hold_the_first(*args: Any, **kwargs: Any) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()  # the turn is held, SQLite's lock is not
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        DatabaseAtomicStoreProvider, "_write_if_higher", staticmethod(hold_the_first)
    )
    provider = _provider(sqlite_url, statement_timeout_seconds=30)
    parent = asyncio.ensure_future(provider.set_subject_state("k", "v1:after:1"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        child_pid = os.getpid() + 1
        monkeypatch.setattr(os, "getpid", lambda: child_pid)
        answer = await asyncio.wait_for(
            provider.set_subject_state("k", "v1:after:2"), 3
        )
        assert answer == "v1:after:2"
    finally:
        release.set()
        await parent
        await provider.aclose()


async def test_writers_queued_behind_a_foreign_lock_fail_near_the_deadline(
    sqlite_url: str,
) -> None:
    """Another connection holds SQLite's write lock: the writer holding the turn
    waits on it and the queue behind it must not wait one deadline after the
    other (8 writers, 0.5 s deadline: about 0.5 s; one deadline after the other
    would take 4 s)."""
    path = sqlite_url.split(":///", 1)[1]
    foreign = sqlite3.connect(path, isolation_level=None)
    foreign.execute("BEGIN IMMEDIATE")
    provider = _provider(sqlite_url, statement_timeout_seconds=0.5)
    try:
        started = time.monotonic()
        results = await asyncio.gather(
            *(provider.set(f"k{i}", "v") for i in range(8)), return_exceptions=True
        )
        elapsed = time.monotonic() - started
        assert [type(r) for r in results] == [Timeout] * 8, results
        assert elapsed < 2.0, (
            f"the queue failed one deadline after the other: {elapsed}"
        )
        foreign.rollback()
        await provider.set("after", "v")  # the turn was released, not leaked
        assert await provider.get("after") == "v"
    finally:
        foreign.close()
        await provider.aclose()


async def test_a_waiter_that_keeps_waiting_keeps_its_place_in_the_queue(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers A-F start together, G arrives while they queue; each turn takes
    0.1 s and the wait is looked at every 0.25 s. A waiter that gave up its
    acquire at that look and asked again would rejoin behind G."""
    order: list[str] = []
    original = DatabaseAtomicStoreProvider._write_if_higher

    async def recording(
        conn: Any, dialect: Any, key: str, *args: Any, **kwargs: Any
    ) -> str | None:
        order.append(key)
        held = await original(conn, dialect, key, *args, **kwargs)
        await asyncio.sleep(0.1)
        return held

    monkeypatch.setattr(
        DatabaseAtomicStoreProvider, "_write_if_higher", staticmethod(recording)
    )
    provider = _provider(sqlite_url, statement_timeout_seconds=0.25)

    async def late() -> str:
        await asyncio.sleep(0.12)
        return await provider.set_subject_state("G", "v1:after:1")

    try:
        await asyncio.gather(
            *(provider.set_subject_state(key, "v1:after:1") for key in "ABCDEF"),
            late(),
        )
        assert order == list("ABCDEFG")
    finally:
        await provider.aclose()


def test_a_closed_event_loop_is_not_kept_by_the_turn(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'leak.db'}"
    asyncio.run(_create_table(url))
    provider = _provider(url)
    loops: list[weakref.ref[asyncio.AbstractEventLoop]] = []

    async def contended() -> None:
        loops.append(weakref.ref(asyncio.get_running_loop()))
        await asyncio.gather(*(provider.set(f"k{i}", "v") for i in range(5)))

    try:
        for _ in range(4):
            asyncio.run(contended())
        gc.collect()
        assert sum(ref() is not None for ref in loops) <= 1
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("database is locked", Timeout),
        ("disk I/O error", Unavailable),
    ],
)
def test_a_locked_database_is_a_timeout_and_another_sqlite_error_is_not(
    message: str, expected: type[Exception]
) -> None:
    """SQLite's busy handler giving up is the wait for the write lock running
    out; any other operational error stays ``Unavailable``."""
    provider = _provider("sqlite+aiosqlite:///unused.db")
    error = sa_exc.OperationalError("INSERT", {}, sqlite3.OperationalError(message))
    assert type(provider._failure("set", error)) is expected


async def _peak_concurrency(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[Callable[[DatabaseAtomicStoreProvider], Awaitable[object]]],
    *,
    only: str,
) -> int:
    """The most statements of one kind (``SELECT`` or a write) in flight at once."""
    original = AsyncConnection.execute
    in_flight = peak = 0

    async def watched(
        self: AsyncConnection, statement: Any, *args: Any, **kw: Any
    ) -> Any:
        nonlocal in_flight, peak
        mine = str(statement).lstrip().split(None, 1)[0].upper() == only
        if mine:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return await original(self, statement, *args, **kw)
        finally:
            if mine:
                in_flight -= 1

    monkeypatch.setattr(AsyncConnection, "execute", watched)
    provider = _provider(url)
    try:
        await asyncio.gather(*(call(provider) for call in calls))
    finally:
        await provider.aclose()
    return peak


async def test_sqlite_runs_one_writer_at_a_time_but_reads_together(
    sqlite_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sets: list[Callable[[DatabaseAtomicStoreProvider], Awaitable[object]]] = [
        partial(_set_one, f"k{i}") for i in range(8)
    ]
    states: list[Callable[[DatabaseAtomicStoreProvider], Awaitable[object]]] = [
        partial(_state_one, f"s{i}") for i in range(8)
    ]
    gets: list[Callable[[DatabaseAtomicStoreProvider], Awaitable[object]]] = [
        partial(_get_one, f"k{i}") for i in range(8)
    ]
    assert await _peak_concurrency(sqlite_url, monkeypatch, sets, only="INSERT") == 1
    assert await _peak_concurrency(sqlite_url, monkeypatch, states, only="UPDATE") == 1
    assert await _peak_concurrency(sqlite_url, monkeypatch, gets, only="SELECT") > 1


async def _set_one(key: str, provider: DatabaseAtomicStoreProvider) -> None:
    await provider.set(key, "v")


async def _state_one(key: str, provider: DatabaseAtomicStoreProvider) -> str:
    return await provider.set_subject_state(key, "v1:after:1")


async def _get_one(key: str, provider: DatabaseAtomicStoreProvider) -> str | None:
    return await provider.get(key)


# ------------------------------------------------------------------- the purge
async def _insert_expired(url: str, rows: dict[str, int]) -> None:
    """Rows expired ``seconds`` ago by the DATABASE's own clock (never ours)."""
    engine = create_async_engine(url)
    sqlite = make_url(url).get_backend_name() == "sqlite"
    try:
        async with engine.begin() as conn:
            for key, seconds in rows.items():
                expiry = (
                    f"strftime('%Y-%m-%d %H:%M:%f', 'now', '-{seconds} seconds')"
                    if sqlite
                    else f"now() - interval '{seconds} seconds'"
                )
                await conn.execute(
                    text(
                        "INSERT INTO infra_atomic_store (key, value, expires_at) "
                        f"VALUES (:k, 'v', {expiry})"
                    ),
                    {"k": key},
                )
    finally:
        await engine.dispose()


async def _keys(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT key FROM infra_atomic_store"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


async def _purge_removes_only_rows_expired_beyond_the_grace(url: str) -> None:
    provider = _provider(url)
    try:
        await provider.set("never", "a")
        await provider.set("live", "b", ttl_seconds=600)
        await _insert_expired(url, {"old": 3600, "recent": 5})
        assert await provider.purge_expired() == 1
        assert await _keys(url) == {"never", "live", "recent"}
        assert await provider.get("never") == "a"
        assert await provider.get("live") == "b"
        assert await provider.get("recent") is None  # expired: absent, not yet purged
    finally:
        await provider.aclose()


async def test_the_purge_removes_only_rows_expired_beyond_the_grace(
    sqlite_url: str,
) -> None:
    await _purge_removes_only_rows_expired_beyond_the_grace(sqlite_url)


@_POSTGRES_ONLY
async def test_the_purge_on_postgresql_removes_only_rows_expired_beyond_the_grace(
    pg_scratch_url: Callable[..., Any],
) -> None:
    await _purge_removes_only_rows_expired_beyond_the_grace(await pg_scratch_url())


async def test_the_purge_runs_bounded_batches_until_nothing_is_left(
    sqlite_url: str,
) -> None:
    provider = _provider(sqlite_url, purge_batch_size=2, purge_grace_seconds=0)
    try:
        await _insert_expired(sqlite_url, {f"old{i}": 100 + i for i in range(5)})
        assert await provider.purge_expired() == 5
        assert await _keys(sqlite_url) == set()
    finally:
        await provider.aclose()


async def test_the_purge_task_deletes_expired_rows_and_stops_on_close(
    sqlite_url: str,
) -> None:
    provider = _provider(sqlite_url, purge_interval_seconds=0.02, purge_grace_seconds=0)
    await provider.aopen()
    task = provider._purge_task
    assert task is not None and not task.done()
    try:
        await _insert_expired(sqlite_url, {"old": 100})
        for _ in range(250):
            if not await _keys(sqlite_url):
                break
            await asyncio.sleep(0.02)
        assert await _keys(sqlite_url) == set()
    finally:
        await provider.aclose()
    assert task.done()
    assert provider._purge_task is None


async def test_a_failing_purge_is_logged_counted_and_does_not_stop_the_loop(
    sqlite_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    from prometheus_client import REGISTRY

    labels = {"instance": "security", "operation": "purge"}
    name = "sandloom_atomic_store_operation_errors_total"
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    provider = _provider(sqlite_url, purge_interval_seconds=0.01)
    calls = 0

    async def failing() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionResetError("secret detail")
        raise Unavailable("down", capability="atomic_store", instance="security")

    provider.purge_expired = failing  # type: ignore[method-assign]
    caplog.set_level("WARNING")
    await provider.aopen()
    task = provider._purge_task
    assert task is not None
    try:
        for _ in range(250):
            if calls >= 3:
                break
            await asyncio.sleep(0.01)
        assert calls >= 3 and not task.done()
    finally:
        await provider.aclose()
    assert (REGISTRY.get_sample_value(name, labels) or 0.0) == before + 1
    assert "ConnectionResetError" in caplog.text
    assert "secret detail" not in caplog.text


async def test_a_purge_that_hits_a_backend_error_counts_it(
    sqlite_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    from prometheus_client import REGISTRY

    labels = {"instance": "security", "operation": "purge"}
    name = "sandloom_atomic_store_operation_errors_total"
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    provider = _provider(sqlite_url)
    await provider.aopen()
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE infra_atomic_store"))
    await engine.dispose()
    caplog.set_level("WARNING")
    try:
        with pytest.raises(Unavailable):
            await provider.purge_expired()
    finally:
        await provider.aclose()
    assert (REGISTRY.get_sample_value(name, labels) or 0.0) == before + 1
