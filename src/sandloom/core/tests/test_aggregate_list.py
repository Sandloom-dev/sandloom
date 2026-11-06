"""Slice 10: ``aggregate_list``, the values of a group as one Python list.

A feature form (``list_aggregates``) that every built-in renderer declares
(PostgreSQL ``array_agg``, SQLite ``json_group_array`` read back through the
item type). A renderer without it refuses the form when compiled
(SANDLOOM-R015), a module declaring it is refused at startup on such a
database (SANDLOOM-B033), and a renderer declaring it without its members is
refused at load (SANDLOOM-B025) and fails the conformance kit. Run on the
test PostgreSQL and a SQLite file with the same answers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine

import sandloom.core.sql_alchemy.portable as portable
from sandloom.core.descriptors import CapabilityRequirement, ModuleDescriptor
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.sql_dialects import POSTGRESQL, SQLITE, SqliteSql
from sandloom.core.drivers.sqlalchemy_database import (
    SQLALCHEMY_DB_FEATURES,
    SQLAlchemyDatabaseOptions,
    create_sqlalchemy_database,
    sqlalchemy_db_features,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.capabilities import (
    DATABASE_CAPABILITY,
    DATABASE_FORM_FEATURES,
)
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.registry import DatabaseFeatureMissing, InfraRegistry
from sandloom.core.loader.plugin_loader import _preflight_capabilities
from sandloom.core.sql_alchemy.portable import (
    IncompatibleSqlDialect,
    UnsupportedSqlForm,
    UtcDateTime,
    register_sql_dialect,
)
from sandloom.core.testing.workers import worker_env_values
from sandloom.driverkit.testing import SqlDialectConformance
from sandloom.sdk.data import Array, aggregate_list

PG = postgresql.dialect()  # type: ignore[no-untyped-call]
LITE = sqlite.dialect()
REPO = Path(__file__).resolve().parents[4]
AT = datetime(2026, 11, 5, 12, 30, tzinfo=UTC)
ID = uuid.UUID("0190f5c2-0000-7000-8000-000000000001")


def _sql(element: Any, dialect: Any) -> str:
    return " ".join(str(element.compile(dialect=dialect)).split())


def _tables() -> tuple[sa.Table, sa.Table]:
    metadata = sa.MetaData()
    owners = sa.Table(
        "slice10_owners",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    )
    items = sa.Table(
        "slice10_items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("owner_id", sa.Integer, sa.ForeignKey("slice10_owners.id")),
        sa.Column("code", sa.String(40)),
        sa.Column("ref", sa.Uuid),
        sa.Column("flag", sa.Boolean),
        sa.Column("amount", sa.Integer),
        sa.Column("at", UtcDateTime()),
    )
    return owners, items


def test_every_built_in_renderer_declares_its_feature() -> None:
    assert DATABASE_FORM_FEATURES["aggregate_list"] == "list_aggregates"
    assert portable.FEATURE_FORM_MEMBERS["list_aggregates"] == (
        "aggregate_list",
        "aggregate_list_type",
    )
    assert "list_aggregates" in POSTGRESQL.features()
    assert "list_aggregates" in SQLITE.features()
    assert "list_aggregates" in DATABASE_CAPABILITY.features
    assert "list_aggregates" in sqlalchemy_db_features({"url": "sqlite+aiosqlite://"})


def test_each_built_in_renderer_writes_it() -> None:
    _, items = _tables()
    query = sa.select(aggregate_list(items.c.code))
    assert (
        _sql(query, PG)
        == "SELECT array_agg(slice10_items.code) AS anon_1 FROM slice10_items"
    )
    assert _sql(query, LITE) == (
        "SELECT json_group_array(slice10_items.code) AS anon_1 FROM slice10_items"
    )


def test_a_scalar_subquery_of_it_correlates_to_the_outer_row() -> None:
    owners, items = _tables()
    codes = (
        sa.select(aggregate_list(items.c.code))
        .where(items.c.owner_id == owners.c.id)
        .scalar_subquery()
    )
    sql = _sql(sa.select(owners.c.id, codes), PG)
    assert sql == (
        "SELECT slice10_owners.id, (SELECT array_agg(slice10_items.code) AS "
        "anon_2 FROM slice10_items WHERE slice10_items.owner_id = "
        "slice10_owners.id) AS anon_1 FROM slice10_owners"
    )


def test_its_cache_key_follows_the_value() -> None:
    _, items = _tables()
    code = aggregate_list(items.c.code)._generate_cache_key()
    assert code == aggregate_list(items.c.code)._generate_cache_key()
    assert code != aggregate_list(items.c.ref)._generate_cache_key()


@pytest.mark.parametrize(
    "item",
    [sa.JSON(), Array(sa.String(10)), sa.ARRAY(sa.Integer), sa.LargeBinary()],
)
def test_it_collects_scalars_only(item: Any) -> None:
    with pytest.raises(TypeError, match="scalar values"):
        aggregate_list(sa.column("x", item))


class _Older(SqliteSql):
    """A contract-2 renderer written before list_aggregates."""

    aggregate_list = None  # type: ignore[assignment]
    aggregate_list_type = None  # type: ignore[assignment]

    def features(self) -> frozenset[str]:
        return super().features() - {"list_aggregates"}


def test_a_renderer_without_it_refuses_it_naming_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portable, "sql_dialect", lambda _name: _Older())
    _, items = _tables()
    with pytest.raises(UnsupportedSqlForm, match="list_aggregates") as refused:
        _sql(sa.select(aggregate_list(items.c.code)), LITE)
    assert refused.value.code == "SANDLOOM-R015"
    assert refused.value.feature == "list_aggregates"


def _core(url: str) -> CoreFacade:
    reg = InfraRegistry(
        config={
            "database": {
                "default": InstanceConfig(
                    capability="database",
                    name="default",
                    driver="sqlalchemy",
                    options={"url": url},
                )
            }
        },
        strict_capabilities=True,
    )
    reg.register_capability(DATABASE_CAPABILITY)
    reg.register_driver(
        "database",
        "sqlalchemy",
        create_sqlalchemy_database,
        features=SQLALCHEMY_DB_FEATURES,
        config_schema=SQLAlchemyDatabaseOptions,
        instance_features=sqlalchemy_db_features,
    )
    core = CoreFacade(app=FastAPI())
    core.set_infra(reg)
    return core


def test_a_module_needing_it_is_refused_at_startup_where_the_renderer_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity's case: the module declares list_aggregates, so a database
    whose renderer predates the form refuses it at load, not at first login."""
    desc = ModuleDescriptor(
        name="slice10mod",
        version="1",
        capabilities=[
            CapabilityRequirement("database", features=frozenset({"list_aggregates"}))
        ],
    )
    url = "sqlite+aiosqlite:///never-opened-slice10.db"
    _preflight_capabilities(_core(url), desc)  # the built-in renderer has it
    monkeypatch.setattr(portable, "sql_dialect", lambda _name: _Older())
    with pytest.raises(DatabaseFeatureMissing, match="list_aggregates"):
        _preflight_capabilities(_core(url), desc)


def test_a_renderer_declaring_it_must_write_it() -> None:
    class HalfSql(SqliteSql):
        aggregate_list_type = None  # type: ignore[assignment]

    with pytest.raises(
        IncompatibleSqlDialect, match="list_aggregates: aggregate_list_type"
    ):
        register_sql_dialect("slice10_half", HalfSql())


class _NoList(SqliteSql):
    def aggregate_list_type(self, item_type: Any) -> Any:
        return sa.Text()  # reads text, not a list


class _NoAggregate(SqliteSql):
    def aggregate_list(self, value: Any) -> Any:
        return None


@pytest.mark.parametrize("renderer", [_NoList(), _NoAggregate()])
def test_the_conformance_kit_fails_a_broken_aggregate(renderer: Any) -> None:
    class Broken(SqlDialectConformance):
        pass

    Broken.renderer = renderer
    Broken.dialect = sqlite.dialect()
    with pytest.raises(AssertionError):
        Broken().test_list_aggregates_read_a_list_or_are_absent()


@pytest.mark.parametrize(
    ("renderer", "dialect"), [(POSTGRESQL, PG), (SQLITE, LITE), (_Older(), LITE)]
)
def test_the_conformance_kit_passes_the_built_in_renderers(
    renderer: Any, dialect: Any
) -> None:
    class Kit(SqlDialectConformance):
        pass

    Kit.renderer = renderer
    Kit.dialect = dialect
    Kit().test_list_aggregates_read_a_list_or_are_absent()


def _pg_url() -> str:
    return worker_env_values(REPO / ".env.test")["DATABASE_URL"]


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[AsyncEngine, None]:
    engine = driver.build_disposable_engine(_pg_url())
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'slice10.db'}")
    yield engine
    await engine.dispose()


async def _answers(engine: AsyncEngine) -> dict[str, Any]:
    owners, items = _tables()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(owners.metadata.create_all)
            await connection.execute(sa.insert(owners), [{"id": 1}, {"id": 2}])
            # amount and at: 7 and AT on the first item, NULL on the second.
            await connection.execute(
                sa.insert(items),
                [
                    {"id": 1, "owner_id": 1, "code": "b", "ref": ID, "flag": True},
                    {"id": 2, "owner_id": 1, "code": "a", "ref": None, "flag": False},
                ],
            )
            await connection.execute(
                sa.update(items).where(items.c.id == 1).values(amount=7, at=AT)
            )
            group = sa.select(
                aggregate_list(items.c.code),
                aggregate_list(items.c.ref),
                aggregate_list(items.c.flag),
                aggregate_list(items.c.amount),
                aggregate_list(items.c.at),
            ).where(items.c.owner_id == 1)
            row = (await connection.execute(group)).one()
            # The aggregate alone reads FROM its value's table.
            alone = (
                await connection.execute(sa.select(aggregate_list(items.c.code)))
            ).scalar_one()
            # A correlated scalar subquery: one list per outer row, in the
            # same statement, [] for an owner without items.
            codes = (
                sa.select(aggregate_list(items.c.code))
                .where(items.c.owner_id == owners.c.id)
                .scalar_subquery()
            )
            per_owner = (
                await connection.execute(
                    sa.select(owners.c.id, codes).order_by(owners.c.id)
                )
            ).all()
            return {
                "code": sorted(row[0], key=str),
                "ref": sorted(row[1], key=str),
                "flag": sorted(row[2], key=str),
                "amount": sorted(row[3], key=str),
                "at": sorted(row[4], key=str),
                "per_owner": [(owner, sorted(c)) for owner, c in per_owner],
                "alone": sorted(alone),
            }
        finally:
            await transaction.rollback()


EXPECTED = {
    "code": ["a", "b"],
    "ref": [ID, None],
    "flag": [False, True],
    "amount": [7, None],
    "at": [AT, None],
    "per_owner": [(1, ["a", "b"]), (2, [])],
    "alone": ["a", "b"],
}


@pytest.mark.asyncio
async def test_postgresql_reads_typed_lists(pg: AsyncEngine) -> None:
    assert await _answers(pg) == EXPECTED


@pytest.mark.asyncio
async def test_sqlite_reads_the_same_lists(lite: AsyncEngine) -> None:
    answers = await _answers(lite)
    assert answers == EXPECTED
    # Read through the item types, not left as JSON text.
    assert isinstance(answers["ref"][0], uuid.UUID)
    assert answers["at"][0].tzinfo is UTC
