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
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Table, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.config.settings import settings
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.drivers.database_atomic_store import (
    _TABLE as DRIVER_TABLE,
)
from sandloom.core.drivers.database_atomic_store import (
    DatabaseAtomicStoreOptions,
    DatabaseAtomicStoreProvider,
)
from sandloom.core.infra.errors import InvalidArgument, Unavailable
from sandloom.core.infra.ports.atomic_store import AtomicStoreProvider
from sandloom.core.migrations.backup import CORE_TABLES
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.atomic_store_purge import purge_expired_atomic_entries
from sandloom.core.sql_alchemy.models.atomic_store import (
    STORAGE_PARAMETERS,
    InfraAtomicStore,
)
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
    assert model.dialect_options["postgresql"]["with"] == STORAGE_PARAMETERS


def test_the_migration_creates_the_table_logged_with_the_storage_parameters() -> None:
    source = (
        Path(__file__).resolve().parents[4]
        / "migrations/core/versions/0015_infra_atomic_store.py"
    )
    if not source.exists():  # an installed wheel carries the chain elsewhere
        pytest.skip("the migration source is not in this tree")
    body = source.read_text()
    assert "prefixes" not in body  # never ``prefixes=["UNLOGGED"]``
    for name, value in STORAGE_PARAMETERS.items():
        assert f'"{name}": {value}' in body


def test_the_registry_builds_the_database_driver_as_an_option() -> None:
    registry = build_infra_registry(InMemorySchemaRegistry(), load_plugins=False)
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


# --------------------------------------------------------------- the sweeper
class _Unit:
    """The unit of work of one session on ``engine``, as the relay hands it."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.session: Any = None
        self._ctx: Any = None

    async def __aenter__(self) -> _Unit:
        from sqlalchemy.ext.asyncio import AsyncSession

        self.session = AsyncSession(self._engine)
        await self.session.begin()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc[0] is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        await self.session.close()


async def test_the_sweeper_deletes_only_rows_expired_beyond_the_grace(
    sqlite_url: str,
) -> None:
    provider = _provider(sqlite_url)
    engine = create_async_engine(sqlite_url)
    now = datetime.now(UTC)
    try:
        await provider.set("never", "a")
        await provider.set("live", "b", ttl_seconds=600)
        async with engine.begin() as conn:
            for key, delta in (("old", -3600), ("recent", -5)):
                await conn.execute(
                    text(
                        "INSERT INTO infra_atomic_store (key, value, expires_at) "
                        "VALUES (:k, 'v', :t)"
                    ),
                    {
                        "k": key,
                        "t": (now + timedelta(seconds=delta)).strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        ),
                    },
                )
        deleted = await purge_expired_atomic_entries(
            lambda: _Unit(engine), limit=100, grace_seconds=60
        )
        assert deleted == 1
        assert await provider.get("never") == "a"
        assert await provider.get("live") == "b"
        async with engine.connect() as conn:
            keys = {
                r[0]
                for r in await conn.execute(text("SELECT key FROM infra_atomic_store"))
            }
        assert keys == {"never", "live", "recent"}
        assert await purge_expired_atomic_entries(lambda: _Unit(engine), limit=0) == 0
    finally:
        await provider.aclose()
        await engine.dispose()


@_POSTGRES_ONLY
async def test_the_sweeper_on_postgresql_keeps_live_and_non_expiring_rows(
    pg_scratch_url: Callable[..., Any],
) -> None:
    url = await pg_scratch_url()
    provider = _provider(url)
    engine = create_async_engine(url)
    try:
        await provider.set("never", "a")
        await provider.set("live", "b", ttl_seconds=600)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO infra_atomic_store (key, value, expires_at) VALUES "
                    "('old', 'v', now() - interval '1 hour'), "
                    "('recent', 'v', now() - interval '5 seconds')"
                )
            )
        assert await purge_expired_atomic_entries(lambda: _Unit(engine), limit=100) == 1
        assert await provider.get("never") == "a"
        assert await provider.get("live") == "b"
        async with engine.connect() as conn:
            keys = {
                r[0]
                for r in await conn.execute(text("SELECT key FROM infra_atomic_store"))
            }
        assert keys == {"never", "live", "recent"}
    finally:
        await provider.aclose()
        await engine.dispose()


async def test_the_sweeper_skips_a_database_without_the_table(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bare.db'}")
    try:
        assert await purge_expired_atomic_entries(lambda: _Unit(engine), limit=10) == 0
    finally:
        await engine.dispose()


def test_a_row_expired_in_the_future_of_the_sweeper_is_never_a_victim() -> None:
    # The sweeper's cutoff trails this process's clock by the grace, so a row the
    # database's clock still reads as live (a skew below the grace) stays.
    from sandloom.core.sql_alchemy.atomic_store_purge import DEFAULT_GRACE_SECONDS

    assert DEFAULT_GRACE_SECONDS >= 30
