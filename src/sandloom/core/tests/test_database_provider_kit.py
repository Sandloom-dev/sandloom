"""The ``database`` capability: ``DatabaseProviderConformance`` against the
``sqlalchemy`` driver on SQLite and PostgreSQL (spec 11).

PostgreSQL is ``settings.database_url`` (a scratch database of its own, created
for the run); SQLite is a file in the test's directory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core.config.settings import settings
from sandloom.core.drivers.sqlalchemy_database import SQLAlchemyDatabaseProvider
from sandloom.core.infra.config import InstanceConfig
from sandloom.driverkit.kits.database import DatabaseProviderConformance

_PG_URL = settings.database_url


def _provider(url: str) -> Callable[[], SQLAlchemyDatabaseProvider]:
    config = InstanceConfig("database", "kit", "sqlalchemy", {"url": url})
    return lambda: SQLAlchemyDatabaseProvider.from_config(config)


class _SqlalchemyKit(DatabaseProviderConformance):
    # The driver's units raise the engine's own errors, not InfraError: the
    # error-mapping groups do not apply to a relational driver (set to None, so
    # no lane reports them skipped on every run).
    test_harness_maps_backend_failures_to_infra_errors = None  # type: ignore[assignment]
    test_harness_redacts_the_health_of_a_failing_backend = None  # type: ignore[assignment]


class TestSqlalchemyOnSqlite(_SqlalchemyKit):
    @pytest.fixture
    def make_provider(self, tmp_path: Path) -> Callable[[], SQLAlchemyDatabaseProvider]:
        return _provider(f"sqlite+aiosqlite:///{tmp_path / 'kit.db'}")

    # SQLite serialises writers: a second unit cannot be held beside a writing one.
    test_a_unit_does_not_see_the_uncommitted_write_of_another = None  # type: ignore[assignment]


@pytest.mark.skipif(
    make_url(_PG_URL).get_backend_name() != "postgresql",
    reason="the database lane is not PostgreSQL (the second-db lane)",
)
class TestSqlalchemyOnPostgres(_SqlalchemyKit):
    @pytest_asyncio.fixture
    async def scratch_url(self) -> AsyncIterator[str]:
        admin_url = make_url(_PG_URL)
        scratch = f"database_kit_{uuid.uuid4().hex[:10]}"
        admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        url = admin_url.set(database=scratch).render_as_string(hide_password=False)
        try:
            yield url
        finally:
            async with admin.connect() as conn:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
                )
            await admin.dispose()

    @pytest.fixture
    def make_provider(
        self, scratch_url: str
    ) -> Callable[[], SQLAlchemyDatabaseProvider]:
        return _provider(scratch_url)


# ----------------------------------------------- the kit against broken drivers
class _NoOutbox(SQLAlchemyDatabaseProvider):
    """Declares ``transactional_outbox`` and answers no store."""

    def outbox(self) -> None:  # type: ignore[override]
        return None


class _StoreWithoutFeature(SQLAlchemyDatabaseProvider):
    """Answers an audit store while ``audit`` is not declared."""

    def features(self) -> frozenset[str]:
        return super().features() - {"audit"}


@pytest.mark.parametrize("broken", [_NoOutbox, _StoreWithoutFeature])
async def test_the_kit_fails_a_store_that_does_not_match_its_feature(
    broken: type[SQLAlchemyDatabaseProvider], tmp_path: Path
) -> None:
    parent = _provider(f"sqlite+aiosqlite:///{tmp_path / 'broken.db'}")()
    provider = broken(
        name=parent.name,
        driver=parent.driver,
        options=dict(parent.options),
        engine=parent.engine,
        session_factory=parent.session_factory,
        owns_engine=True,
    )
    try:
        with pytest.raises(AssertionError):
            await DatabaseProviderConformance().test_a_store_is_answered_exactly_when_its_feature_is_declared(
                provider
            )
    finally:
        await provider.aclose()
