"""An application boots on SQLite (design/infra-agnostic/spec.md, slice 8b).

The settings accept any ``<dialect>+<dbapi>://`` URL and startup asks the
drivers (SANDLOOM-B024); a SQLite file takes its schema from the models at
startup (``create_all``), never altered: a file whose tables differ from the
models refuses to start, or is recreated in development (SANDLOOM-B028);
``sandloom migrate`` refuses it (SANDLOOM-B028); a production process refuses
it without its accepted risk, and always under the strict profile
(SANDLOOM-B029); ``DATABASE_SYSTEM_URL`` on it is refused (SANDLOOM-B026).

The boot itself runs the platform root (identity, tenancy, billing, admin)
in a child process on a file in ``tmp_path``: ``/health``, a public route
(registration) and authenticated ones (create and list an organization).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select

import sandloom.core.migrations.migrations as _migrations
from sandloom.core import app_factory, worker
from sandloom.core.cli import migrate as migrate_cli
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import Settings, database_url_problem, settings
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.dbapi import async_dbapi_problem
from sandloom.core.sql_alchemy.model_schema import (
    ModelSchemaDrift,
    ensure_model_schema,
    schema_drift,
)
from sandloom.core.sql_alchemy.portable import development_only, schema_from_models
from sandloom.core.sql_alchemy.tenant_rls import row_security_problem

# --- settings: the URL's shape only; startup asks the drivers (B024) ------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@h:5432/d",
        "sqlite+aiosqlite:///./app.db",
        "sqlite+aiosqlite:////abs/path/app.db",
        "mysql+aiomysql://u:p@h/d",  # no renderer: B024 at boot, not here
    ],
)
def test_settings_accept_a_dialect_and_dbapi_url(url: str) -> None:
    assert database_url_problem("DATABASE_URL", url) is None
    assert Settings._db_dsn_ok(url) == url
    assert Settings._system_dsn_ok(url) == url


def test_settings_accept_a_path_with_spaces_and_name_an_upper_case_scheme() -> None:
    assert (
        database_url_problem("DATABASE_URL", "sqlite+aiosqlite:///My Apps/a.db") is None
    )
    problem = database_url_problem("DATABASE_URL", "SQLite+AIOSQLite:///a.db")
    assert problem is not None and "lower case: sqlite+aiosqlite://" in problem


@pytest.mark.parametrize(
    "url", ["postgresql://u:p@h/d", "sqlite:///app.db", "app.db", "", "x+y:/no"]
)
def test_settings_refuse_a_url_without_its_dbapi(url: str) -> None:
    problem = database_url_problem("DATABASE_URL", url)
    assert problem is not None and "<dialect>+<dbapi>" in problem
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings._db_dsn_ok(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+asyncpg://u:p@h/d", None),
        ("sqlite+aiosqlite:///app.db", None),
        ("postgresql+psycopg2://u:secret@h/d", "synchronous DBAPI 'psycopg2'"),
        ("postgresql+nosuch://u@h/d", "no DBAPI 'nosuch'"),
        ("mysql+aiomysql://u@h/d", None),  # no renderer: sql_dialect_problem's
    ],
)
def test_boot_refuses_an_unknown_or_synchronous_dbapi(
    url: str, expected: str | None
) -> None:
    problem = async_dbapi_problem(url)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem
        assert "SANDLOOM-B024" in problem and "secret" not in problem


def test_the_renderers_state_where_the_schema_comes_from() -> None:
    assert schema_from_models("sqlite") and development_only("sqlite")
    assert not schema_from_models("postgresql")
    assert not development_only("postgresql")
    assert not schema_from_models("nosuch")  # no renderer: B024's refusal


# --- B029: a development database in production ---------------------------


def _cfg(url: str, **update: Any) -> Any:
    return settings.model_copy(update={"database_url": url, "infra": {}, **update})


SQLITE = "sqlite+aiosqlite:///app.db"


def test_b029_refuses_sqlite_in_prod_without_the_accepted_risk() -> None:
    problem = app_factory.development_database_config_problem(
        _cfg(SQLITE, environment=Environment.prod, sandloom_accepted_risks=[])
    )
    assert problem is not None and "SANDLOOM-B029" in problem
    assert "database/default (sqlite)" in problem
    assert "development_database_in_prod" in problem
    assert any(
        "SANDLOOM-B029" in p
        for p in app_factory.configuration_problems(
            _cfg(SQLITE, environment=Environment.prod), include_profile=False
        )
    )


def test_b029_admits_the_accepted_risk_outside_the_strict_profile() -> None:
    accepted = ["development_database_in_prod"]
    cfg = _cfg(SQLITE, environment=Environment.prod, sandloom_accepted_risks=accepted)
    assert app_factory.development_database_config_problem(cfg) is None
    strict = _cfg(
        SQLITE,
        environment=Environment.prod,
        sandloom_accepted_risks=accepted,
        production_profile="multi_tenant_strict",
    )
    problem = app_factory.development_database_config_problem(strict)
    assert problem is not None and "SANDLOOM-B029" in problem
    assert "refuses it even when accepted" in problem


def test_b029_leaves_development_tests_and_postgresql_alone() -> None:
    for environment in (Environment.dev, Environment.test):
        cfg = _cfg(SQLITE, environment=environment)
        assert app_factory.development_database_config_problem(cfg) is None
    pg = _cfg("postgresql+asyncpg://u:p@h/d", environment=Environment.prod)
    assert app_factory.development_database_config_problem(pg) is None


# --- B026: no system channel on a database without roles ------------------


def test_a_system_url_on_sqlite_is_refused_not_ignored() -> None:
    problem = row_security_problem(
        "default", SQLITE, {}, system_url="sqlite+aiosqlite:///system.db"
    )
    assert problem is not None and "SANDLOOM-B026" in problem
    assert "DATABASE_SYSTEM_URL" in problem
    # PostgreSQL without tenant_rls keeps today's answer.
    pg = "postgresql+asyncpg://u:p@h/d"
    assert row_security_problem("default", pg, {}, system_url=pg) is None


def test_postgresql_with_a_system_url_and_no_tenant_rls_still_boots() -> None:
    pg = "postgresql+asyncpg://u:p@h/d"
    cfg = _cfg(pg, database_system_url="postgresql+asyncpg://sys:p@h/d")
    assert app_factory.row_security_config_problem(cfg) is None
    assert app_factory.configuration_problems(cfg, include_profile=False) == []


@pytest.mark.parametrize(
    ("system_url", "expected"),
    [
        ("postgresql+psycopg2://sys:p@h/d", "synchronous DBAPI 'psycopg2'"),
        ("postgresql+asyncgp://sys:p@h/d", "no DBAPI 'asyncgp'"),
        ("mysql+aiomysql://sys:p@h/d", "no SQL dialect renderer"),
    ],
)
def test_the_system_urls_get_the_boot_checks_too(
    system_url: str, expected: str
) -> None:
    pg = "postgresql+asyncpg://u:p@h/d"
    for cfg in (
        _cfg(pg, database_system_url=system_url),
        _cfg(
            pg,
            infra={
                "database": {
                    "reports": {
                        "driver": "sqlalchemy",
                        "url": pg,
                        "tenant_rls": True,
                        "system_url": system_url,
                    }
                }
            },
        ),
    ):
        problems = app_factory.configuration_problems(cfg, include_profile=False)
        assert any(expected in p and "SANDLOOM-B024" in p for p in problems), problems
        assert any("system channel" in p for p in problems), problems


# --- B028: sandloom migrate refuses a schema-from-models database ------------


def test_b028_sandloom_migrate_refuses_sqlite(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        _migrations,
        "list_database_instances",
        lambda: {"default": SQLITE, "reports": "postgresql+psycopg2://u@h/r"},
    )
    assert migrate_cli.main(["upgrade-all", "--apply"]) == 2
    err = capsys.readouterr().err
    assert "SANDLOOM-B028" in err and "database/default (sqlite)" in err
    assert "reports" not in err


def test_b028_spares_migrate_drift_which_skips_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sandloom test``'s "migration drift" step on an app whose tests run on
    SQLite: not refused (exit 2), the step says it skips."""
    monkeypatch.setattr(
        _migrations, "list_database_instances", lambda: {"default": SQLITE}
    )
    monkeypatch.setattr(
        migrate_cli,
        "_load_core",
        lambda: SimpleNamespace(module_descriptors=[]),
    )
    monkeypatch.setattr(
        migrate_cli._migrate_drift,
        "drift_supported",
        lambda: "the default database is sqlite, not PostgreSQL",
    )
    assert migrate_cli.main(["drift"]) == 0
    captured = capsys.readouterr()
    assert "SANDLOOM-B028" not in captured.err
    assert "migration drift: skipped" in captured.out


def test_b028_sandloom_migrate_runs_on_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _migrations,
        "list_database_instances",
        lambda: {"default": "postgresql+psycopg2://u@h/d"},
    )
    assert _migrations.migration_path_problem() is None


# --- B028: create_all never alters; drift refuses or is set aside ---------


def _items(*extra: str, unique: bool = False, index: bool = False) -> MetaData:
    metadata = MetaData()
    Table(
        "lite_items",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(20), nullable=True, unique=unique, index=index),
        *(Column(name, String(20)) for name in extra),
    )
    return metadata


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _built(path: Path, metadata: MetaData) -> None:
    engine = driver._build_engine(_url(path))
    try:
        await ensure_model_schema(engine, metadata, where="database/default")
    finally:
        await engine.dispose()


async def _drift(path: Path, metadata: MetaData) -> list[str]:
    engine = driver._build_engine(_url(path))
    try:
        async with engine.connect() as conn:
            return list(await conn.run_sync(lambda c: schema_drift(c, metadata)))
    finally:
        await engine.dispose()


async def test_create_all_builds_then_refuses_a_file_behind_its_models(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    engine = driver._build_engine(_url(path))
    try:
        assert await ensure_model_schema(engine, _items(), where="db") == []
        async with engine.begin() as conn:
            await conn.execute(insert(_items().tables["lite_items"]).values(id=1))
        # Built again unchanged: nothing to do, the row stays.
        assert await ensure_model_schema(engine, _items(), where="db") == []
        with pytest.raises(ModelSchemaDrift) as refused:
            await ensure_model_schema(engine, _items("label"), where="database/x")
        message = str(refused.value)
        assert "SANDLOOM-B028" in message and "database/x" in message
        assert "lite_items.label (not in the database)" in message
        async with engine.connect() as conn:
            table = _items().tables["lite_items"]
            assert (await conn.execute(select(table.c.id))).all() == [(1,)]
    finally:
        await engine.dispose()
    assert sorted(p.name for p in tmp_path.iterdir() if "stale" in p.name) == []


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (_items(unique=True), "lite_items unique (code) (not in the database)"),
        (
            _items(index=True),
            "lite_items index ix_lite_items_code (not in the database)",
        ),
    ],
)
async def test_drift_sees_a_missing_unique_constraint_or_index(
    tmp_path: Path, models: MetaData, expected: str
) -> None:
    path = tmp_path / "app.db"
    await _built(path, _items())
    assert await _drift(path, models) == [expected]
    # And the other way round: the file holds one the models dropped.
    other = tmp_path / "other.db"
    await _built(other, models)
    assert await _drift(other, _items()) == [
        expected.replace("not in the database", "not in the models")
    ]


async def test_drift_sees_nullability_and_a_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    await _built(path, _items())
    strict = MetaData()
    Table(
        "lite_items",
        strict,
        Column("id", Integer, primary_key=True),
        Column("code", String(20), nullable=False),
    )
    assert await _drift(path, strict) == ["lite_items.code (the models say NOT NULL)"]
    elsewhere = MetaData()
    Table(
        "audit_rows", elsewhere, Column("id", Integer, primary_key=True), schema="ops"
    )
    assert await _drift(path, elsewhere) == [
        "ops.audit_rows (a schema this database lacks)"
    ]


async def test_the_platform_models_build_without_drift(tmp_path: Path) -> None:
    path = tmp_path / "platform.db"
    await _built(path, Base.metadata)
    assert await _drift(path, Base.metadata) == []


async def test_the_recreate_signal_sets_the_file_aside_and_builds_fresh(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "app.db"
    await _built(path, _items("gone"))
    provider = driver.SQLAlchemyDatabaseProvider(
        name="default",
        driver="sqlalchemy",
        options={"url": _url(path)},
        engine=driver._build_engine(_url(path)),
        session_factory=driver.get_default_sessionmaker(),
        owns_engine=True,
    )
    async with provider.engine.begin() as conn:
        await conn.execute(insert(_items("gone").tables["lite_items"]).values(id=7))
    try:
        assert await provider.ensure_model_schema(_items("label"), recreate=True)
    finally:
        await provider.aclose()
    stale = sorted(p for p in tmp_path.iterdir() if ".stale-" in p.name)
    assert stale and stale[0].name.startswith("app.db.stale-")
    # Nothing dropped: the old rows are in the stale file, which still opens.
    with sqlite3.connect(stale[0]) as conn:
        assert conn.execute("SELECT id FROM lite_items").fetchall() == [(7,)]
    assert await _drift(path, _items("label")) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM lite_items").fetchone() == (0,)
    assert str(stale[0]) in caplog.text and "SANDLOOM-B028" in caplog.text


async def test_without_the_signal_drift_refuses_and_leaves_the_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    await _built(path, _items("gone"))
    before = path.read_bytes()
    provider = driver.SQLAlchemyDatabaseProvider(
        name="default",
        driver="sqlalchemy",
        options={"url": _url(path)},
        engine=driver._build_engine(_url(path)),
        session_factory=driver.get_default_sessionmaker(),
        owns_engine=True,
    )
    try:
        with pytest.raises(ModelSchemaDrift, match="SANDLOOM-B028"):
            await provider.ensure_model_schema(_items(), recreate=False)
    finally:
        await provider.aclose()
    assert [p.name for p in tmp_path.iterdir() if ".stale-" in p.name] == []
    assert await _drift(path, _items("gone")) == []
    assert path.read_bytes()[:100] == before[:100]


def test_a_worker_checks_the_schema_and_never_recreates() -> None:
    calls: list[tuple[Any, bool]] = []

    class _Provider:
        def features(self) -> frozenset[str]:
            return frozenset()

        async def ensure_model_schema(
            self, metadata: Any = None, *, recreate: bool
        ) -> bool:
            calls.append((metadata, recreate))
            raise ModelSchemaDrift("drift [SANDLOOM-B028]")

    infra = SimpleNamespace(
        instances=lambda capability: ["default"],
        provider=lambda capability, name: _Provider(),
    )
    with pytest.raises(ModelSchemaDrift):
        worker.verify_worker_infra(SimpleNamespace(infra=infra, settings=None))
    assert calls == [(None, False)]


async def test_a_postgresql_provider_leaves_its_schema_to_the_chains() -> None:
    if driver.get_default_engine().dialect.name != "postgresql":
        pytest.skip("the default test database is not PostgreSQL")
    provider = driver.SQLAlchemyDatabaseProvider(
        name="default",
        driver="sqlalchemy",
        options={},
        engine=driver.get_default_engine(),
        session_factory=driver.get_default_sessionmaker(),
        owns_engine=False,
    )
    assert await provider.ensure_model_schema(_items(), recreate=True) is False


# --- the platform root boots on a SQLite file -----------------------------

_BOOT = """
import asyncio, json
import httpx
from sandloom.core.composition import load_composition_app

app = load_composition_app()
from sandloom.sdk.testing import as_user

async def main():
    out = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            out["health"] = (await c.get("/health")).status_code
            r = await c.post("/auth/register", json={
                "email": "lite@example.com", "password": "Lite-pass-0123456789!",
                "full_name": "Lite"})
            out["register"] = r.status_code
            _, headers = as_user(tenant=False)
            r = await c.post("/api/v1/orgs", json={"name": "Acme", "slug": "acme"},
                             headers=headers)
            out["create_org"] = r.status_code
            r = await c.get("/api/v1/orgs", headers=headers)
            out["orgs"] = [o["slug"] for o in r.json()]
    print("RESULT " + json.dumps(out))

asyncio.run(main())
"""
