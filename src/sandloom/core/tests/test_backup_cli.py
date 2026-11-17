"""``sandloom backup plan|verify`` and the operator guard rails (H-20).

* the plan names every database instance and every backing server the
  configured drivers say hold data (``InfraProvider.backup_hint``), flags the
  security store as critical and never prints a password;
* ``verify`` restores into a throw-away database, checks every hosted
  migration chain is at its head plus basic integrity, and drops it;
* a restore that fails, or a database behind its chain head, fails the
  verification;
* with ``SANDLOOM_TEST_PG_CLIENT`` (e.g. ``docker exec -i sandloom_postgres_test``) and
  ``SANDLOOM_TEST_PG_CLIENT_URL`` (the server as that client reaches it) the real
  ``pg_dump`` → ``pg_restore`` round trip runs too.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import Table
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sandloom.core.drivers.bootstrap as bootstrap
import sandloom.core.infra.config as infra_config
import sandloom.core.infra.tool_infra as tool_infra
import sandloom.core.migrations.backup as backup_module
from sandloom.core.cli._operator import add_operator_arguments, operator_intent
from sandloom.core.config.settings import settings
from sandloom.core.infra.ports._base import (
    BackupCriticality,
    BackupHint,
    InfraProvider,
)
from sandloom.core.infra.registry import (
    DriverConflictError,
    DriverLoadError,
    InfraRegistry,
)
from sandloom.core.infra.tool_infra import FilledInfraConfig
from sandloom.core.migrations.backup import (
    CORE_TABLES,
    CORE_VERSION_TABLE,
    LEASE_FENCE_OFFSET,
    build_backup_plan,
    command_restorer,
    dump_format,
    format_plan,
    verify_dump,
)
from sandloom.core.migrations.migrations import core_chain_dir
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.testing import reload_test_env
from sandloom.core.testing.migrations import (
    MigrationChain,
    migrate_module,
    run_sql,
    tables_created_by,
    temporary_database,
    upgrade_core_chain,
)

IDENTITY_CHAIN = (
    "sandloom_identity",
    str(Path(__file__).resolve().parents[3] / "sandloom_identity" / "migrations"),
    "alembic_version_identity",
)
ENV_TEST = Path(__file__).resolve().parents[4] / ".env.test"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    if ENV_TEST.exists():
        reload_test_env(ENV_TEST)


CHAINS = [("core", str(core_chain_dir()), CORE_VERSION_TABLE), IDENTITY_CHAIN]


def _server_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    return url


def _migrate(url: str, identity_revision: str = "head") -> None:
    upgrade_core_chain(url)
    migrate_module(url, "sandloom_identity", identity_revision)


class _KeyedModule(BaseModel):
    """A module's settings declaration with a secret to back up."""

    shop_signing_key: str | None = Field(None, alias="SHOP_SIGNING_KEY")
    backup: ClassVar[tuple[tuple[str, str], ...]] = (
        ("SHOP_SIGNING_KEY", "losing SHOP_SIGNING_KEY: orders cannot be verified"),
    )


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_covers_databases_and_security_redis_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_stub_drivers(
        monkeypatch,
        {
            "database": {
                "default": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:s3cret@db1:5432/app",
                },
                "analytics": {
                    "driver": "sqlalchemy",
                    "url": "postgresql+asyncpg://u:s3cret@db2:5432/analytics",
                },
            },
            "atomic_store": {
                "security": {"driver": "stubstore", "url": "stub://:pw@sec:6379/0"}
            },
        },
    )
    items = build_backup_plan(settings, (), [_KeyedModule])
    text = format_plan(items)
    names = {(i.kind, i.name) for i in items}
    assert ("postgres", "default") in names
    assert ("postgres", "analytics") in names
    security = [
        i for i in items if i.kind == "stubserver" and "atomic_store:security" in i.name
    ]
    assert len(security) == 1
    assert security[0].criticality == "critical"
    assert any("reconcile-security" in step for step in security[0].restore)
    assert any("never evict" in step for step in security[0].backup)
    assert "s3cret" not in text and ":pw@" not in text
    assert "pg_dump --format=custom" in text
    # the kernel's keys, and each loaded module's declared secret and loss note
    assert "JWT_SECRET" in text and "STRIPE" not in text
    assert "SHOP_SIGNING_KEY" in text
    assert "orders cannot be verified" in text


def test_plan_flags_a_database_security_store_on_a_listed_and_on_its_own_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "postgresql+asyncpg://u:s3cret@db1:5432/app"
    own = "postgresql+asyncpg://u:s3cret@db2:5432/security"
    monkeypatch.setattr(
        settings,
        "infra",
        {
            "database": {"default": {"driver": "sqlalchemy", "url": shared}},
            "atomic_store": {
                "security": {"driver": "database", "url": shared},
                "idempotency": {"driver": "database", "url": own},
            },
        },
    )
    items = build_backup_plan(settings)
    default = next(i for i in items if i.kind == "postgres" and i.name == "default")
    assert any(
        "atomic_store:security" in r and "SECURITY" in r and "infra_atomic_store" in r
        for r in default.roles
    )
    assert any(
        "infra_atomic_store" in step and "reconcile-security" in step
        for step in default.restore
    )
    separate = next(i for i in items if i.name == "atomic_store:idempotency")
    assert separate.kind == "postgres" and separate.criticality == "critical"
    assert any("IDEMPOTENCY" in r for r in separate.roles)
    assert not any(
        "atomic_store" in r
        for i in items
        if i.kind not in ("postgres", "objectstore")
        for r in i.roles
    )
    assert "s3cret" not in format_plan(items)


#: Stand-in drivers: the kernel test needs no driver package (the no-Redis lane
#: has none). Each says what ``InfraProvider.backup_hint`` lets a real driver
#: say; the real Redis and Celery hints are tested in their packages.
class _ServerDriver(InfraProvider):
    """A driver whose instance runs on the server its ``url`` names."""

    contract_version = 1
    url_option = "url"
    hint_url_option = "url"
    detail = ""
    steps: ClassVar[tuple[str, ...]] = ("persist it",)
    loss: ClassVar[str | None] = "losing it is tolerable"
    criticality: ClassVar[BackupCriticality] = "optional"

    @classmethod
    def backup_hint(cls, options: Mapping[str, Any]) -> BackupHint | None:
        url = options.get(cls.hint_url_option)
        if not isinstance(url, str):
            return None
        return BackupHint(
            url=url,
            kind="stubserver",
            detail=cls.detail,
            criticality=cls.criticality,
            backup=cls.steps,
            restore=("restore it",),
            tolerable_loss=cls.loss,
        )


class _CacheDriver(_ServerDriver):
    steps = ("persist it",)


class _StoreDriver(_ServerDriver):
    steps = ("never evict it", "persist it")
    loss = None
    criticality = "required"


class _BrokerDriver(_ServerDriver):
    url_option = "broker"
    hint_url_option = "broker"
    detail = "broker"
    loss = None


class _SharedClientDriver(_ServerDriver):
    """No url of its own: it uses the process default (the default cache's)."""

    @classmethod
    def backup_hint(cls, options: Mapping[str, Any]) -> BackupHint | None:
        return BackupHint(url=None, kind="stubserver", detail="(shared)")


class _SilentDriver(InfraProvider):
    """A driver that says nothing (the base answer): nothing to back up."""

    contract_version = 1
    url_option = "url"


_STUB_DRIVERS: dict[tuple[str, str], type[InfraProvider]] = {
    ("cache", "stubcache"): _CacheDriver,
    ("cache", "silent"): _SilentDriver,
    ("atomic_store", "stubstore"): _StoreDriver,
    ("eventbus", "stubshared"): _SharedClientDriver,
    ("taskqueue", "stubbroker"): _BrokerDriver,
}


class _StubRegistry(InfraRegistry):
    """The registry the plan asks for classes: the stand-ins, then the kernel's
    own drivers (the ones the kernel adds to every composition)."""

    def __init__(self) -> None:
        super().__init__(strict_capabilities=True)
        bootstrap.register_builtin_drivers(
            self, InMemorySchemaRegistry(), settings=settings
        )

    def driver_class(self, capability: str, driver: str) -> type[InfraProvider] | None:
        stub = _STUB_DRIVERS.get((capability, driver))
        return stub if stub is not None else super().driver_class(capability, driver)


def _use_stub_drivers(
    monkeypatch: pytest.MonkeyPatch,
    infra: dict[str, Any],
    *,
    unfilled: str | None = None,
) -> None:
    """The composition states ``infra`` and the registry knows the stand-ins."""
    monkeypatch.setattr(settings, "infra", {})
    monkeypatch.setattr(infra_config, "_APP", None)
    monkeypatch.setattr(infra_config, "_PIN", infra_config._checked(infra))
    registry = _StubRegistry()

    def filled(flat: Any) -> FilledInfraConfig:
        return FilledInfraConfig(
            infra_config.load_infra_config(flat), unfilled, registry
        )

    monkeypatch.setattr(backup_module, "filled_infra_config", filled)


_STUB_INFRA = {
    "cache": {"default": {"driver": "stubcache", "url": "stub://cachehost:6379/3"}},
    "atomic_store": {
        "security": {"driver": "stubstore", "url": "stub://cachehost:6379/3"}
    },
    "taskqueue": {
        "default": {"driver": "stubbroker", "broker": "stub://brokerhost:6379/4"}
    },
}


def test_plan_lists_the_targets_the_drivers_say_a_composition_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache, the security store that states the same url and the broker:
    one item per server, each role listed, the kernel naming no driver."""
    _use_stub_drivers(monkeypatch, _STUB_INFRA)
    items = [i for i in build_backup_plan(settings) if i.kind == "stubserver"]
    by_target = {i.target: i for i in items}
    cache = by_target["stub://cachehost:6379/3"]
    assert cache.roles[0] == "cache:default"
    assert any("atomic_store:security" in r and "SECURITY" in r for r in cache.roles)
    assert cache.criticality == "critical"
    # the keep-last merge: a step two roles state is listed once, at its latest place
    assert cache.backup == ["never evict it", "persist it"]
    # a role that must survive on the server removes the "tolerable loss" line
    assert "losing it is tolerable" not in cache.restore
    assert any("reconcile-security" in step for step in cache.restore)
    broker = by_target["stub://brokerhost:6379/4"]
    assert broker.roles == ["taskqueue:default broker"]
    assert broker.restore == ["restore it"]


def test_a_server_of_best_effort_roles_only_says_its_loss_is_tolerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_stub_drivers(
        monkeypatch,
        {"cache": {"default": {"driver": "stubcache", "url": "stub://c:1/0"}}},
    )
    (cache,) = [i for i in build_backup_plan(settings) if i.kind == "stubserver"]
    assert cache.criticality == "optional"
    assert cache.restore == ["restore it", "losing it is tolerable"]


def test_plan_lists_a_driver_with_no_url_of_its_own_on_the_default_cache_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_stub_drivers(
        monkeypatch,
        {
            "cache": {"default": {"driver": "stubcache", "url": "stub://c:1/0"}},
            "eventbus": {"extra": {"driver": "stubshared"}},
        },
    )
    (server,) = [i for i in build_backup_plan(settings) if i.kind == "stubserver"]
    assert server.roles == ["cache:default", "eventbus:extra (shared)"]


def test_plan_lists_no_target_for_an_instance_that_states_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver with no url to hint says nothing: nothing is listed, never a
    guess; a driver that says nothing at all adds no item either."""
    _use_stub_drivers(
        monkeypatch,
        {
            "cache": {"default": {"driver": "stubcache"}, "x": {"driver": "silent"}},
            "eventbus": {"extra": {"driver": "stubshared"}},
            "taskqueue": {"default": {"driver": "stubbroker"}},
        },
    )
    items = build_backup_plan(settings)
    assert not [i for i in items if i.kind == "stubserver"]
    assert not [i for i in items if i.kind == "warning"]


@pytest.mark.parametrize("error", [DriverLoadError, DriverConflictError])
def test_plan_survives_an_installed_driver_that_fails_to_load_and_says_so(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A read-only tool still plans: with a driver that fails to load the
    kernel cannot ask the drivers, so it lists no backing server, warns
    (critical) why, and the rest of the plan stands."""
    _use_stub_drivers(
        monkeypatch, _STUB_INFRA, unfilled=str(error("cannot be imported"))
    )
    items = build_backup_plan(settings)
    assert not [i for i in items if i.kind == "stubserver"]
    (warning,) = [i for i in items if i.kind == "warning"]
    assert warning.criticality == "critical"
    text = format_plan(items)
    assert "cannot be imported" in text
    assert "lists none of the cache, atomic_store" in text
    assert any(i.kind == "secrets" for i in items)  # the rest of the plan stands


def test_a_driver_that_is_not_installed_here_is_named_in_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan never goes quiet about an instance whose driver cannot say."""
    infra = {
        "cache": {"default": {"driver": "stubcache", "url": "stub://c:1/0"}},
        "atomic_store": {"security": {"driver": "uninstalled", "url": "x://s/0"}},
    }
    _use_stub_drivers(monkeypatch, infra)
    items = build_backup_plan(settings)
    assert [i for i in items if i.kind == "stubserver"]  # what is known stays
    (warning,) = [i for i in items if i.kind == "warning"]
    assert warning.criticality == "critical"
    assert "atomic_store:security (uninstalled)" in format_plan(items)


def test_plan_refuses_a_failure_that_is_not_a_driver_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(registry: object, config: object) -> None:
        raise RuntimeError("not a driver problem")

    monkeypatch.setattr(settings, "infra", {})
    monkeypatch.setattr(tool_infra, "load_selected_plugins", broken)
    with pytest.raises(RuntimeError, match="not a driver problem"):
        build_backup_plan(settings)


def test_plan_has_no_warning_when_every_driver_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_stub_drivers(monkeypatch, _STUB_INFRA)
    assert not [i for i in build_backup_plan(settings) if i.kind == "warning"]


def test_plan_names_no_module_secret_without_a_declaration() -> None:
    text = format_plan(build_backup_plan(settings))
    assert "TWOFA_ENCRYPTION_KEY" not in text and "STRIPE_" not in text


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _fake_dump(tmp_path: Path) -> Path:
    dump = tmp_path / "default.dump"
    dump.write_bytes(b"PGDMP fake custom-format header")
    return dump


def test_verify_fails_when_the_restore_fails(tmp_path: Path) -> None:
    def broken(dump: Path, url: str) -> None:
        raise subprocess.CalledProcessError(1, ["pg_restore"])

    report = verify_dump(
        _fake_dump(tmp_path), server_url=_server_url(), chains=CHAINS, restorer=broken
    )
    assert not report.ok
    assert [c.name for c in report.checks] == ["restore"]


def test_dump_format_detection(tmp_path: Path) -> None:
    custom = tmp_path / "a.dump"
    custom.write_bytes(b"PGDMP\x01\x0e")
    plain = tmp_path / "a.sql"
    plain.write_text("-- PostgreSQL database dump\n")
    tar = tmp_path / "a.tar"
    tar.write_bytes(b"\0" * 257 + b"ustar" + b"\0" * 250)
    assert dump_format(custom) == "custom"
    assert dump_format(plain) == "plain"
    assert dump_format(tar) == "tar"
    assert dump_format(tmp_path) == "directory"


@pytest.mark.slow
def test_real_pg_dump_round_trip(tmp_path: Path) -> None:
    client = os.getenv("SANDLOOM_TEST_PG_CLIENT")
    client_url = os.getenv("SANDLOOM_TEST_PG_CLIENT_URL")
    if not client or not client_url:
        pytest.skip("SANDLOOM_TEST_PG_CLIENT / SANDLOOM_TEST_PG_CLIENT_URL not set")
    with temporary_database(_server_url()) as source:
        _migrate(source)
        run_sql(
            source,
            "INSERT INTO users (id, email, full_name, is_active, is_superuser, "
            "is_verified) VALUES (gen_random_uuid(), 'dump@example.com', 'D', "
            "true, false, true)",
        )
        name = urlsplit(source).path.lstrip("/")
        parts = urlsplit(client_url)
        dsn = parts._replace(path=f"/{name}").geturl()
        dump = tmp_path / "source.dump"
        with dump.open("wb") as out:
            subprocess.run(
                [*shlex.split(client.replace(" -i", "")), "pg_dump", "-Fc", dsn],
                stdout=out,
                check=True,
            )
    report = verify_dump(
        dump,
        server_url=_server_url(),
        chains=CHAINS,
        restorer=command_restorer(
            pg_restore=f"{client} pg_restore", restore_url=client_url
        ),
    )
    assert report.ok, report.as_dict()


# --------------------------------------------------------------------------- #
# operator guard rails
# --------------------------------------------------------------------------- #
def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_operator_arguments(parser)
    return parser.parse_args(list(argv))


def test_operator_guard_rails(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.testing import set_environment

    dry = operator_intent(_args())
    assert dry.apply is False
    with pytest.raises(SystemExit, match="--reason"):
        operator_intent(_args("--apply"))
    ok: Any = operator_intent(_args("--apply", "--reason", "leak", "--operator", "al"))
    assert (ok.apply, ok.operator, ok.reason) == (True, "al", "leak")

    set_environment(monkeypatch, "prod")
    with pytest.raises(SystemExit, match="--confirm-production"):
        operator_intent(_args("--apply", "--reason", "leak"))
    confirmed = operator_intent(
        _args("--apply", "--confirm-production", "--reason", "leak")
    )
    assert confirmed.apply is True
    assert operator_intent(_args()).apply is False  # a dry-run is always allowed


# --------------------------------------------------------------------------- #
# core tables, lease fences
# --------------------------------------------------------------------------- #
def test_core_tables_equal_what_the_core_chain_creates() -> None:
    chain = MigrationChain(
        key="core",
        module=None,
        script_location=core_chain_dir(),
        version_table=CORE_VERSION_TABLE,
    )
    assert set(CORE_TABLES) == tables_created_by(chain)
    assert len(set(CORE_TABLES)) == len(CORE_TABLES)


@pytest.fixture
def lease_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A database of its own (core chain, two leases) that the CLI's ``default``
    points at: the shared test database is never written. Yields its sync URL."""
    import sandloom.core.cli.backup as cli_backup
    import sandloom.core.migrations.migrations as migrations
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
    from sandloom.core.unit_of_work import _active_core_for, open_uow

    with temporary_database(_server_url()) as url:
        upgrade_core_chain(url)
        run_sql(
            url,
            "INSERT INTO infra_lease (name, owner, token, expires_at, fence) VALUES "
            "('held', 'a', 't1', now() + interval '1 hour', 5), "
            "('free', 'b', 't2', now() - interval '1 hour', 9)",
        )
        async_url = make_url(url).set(drivername="postgresql+asyncpg")

        @asynccontextmanager
        async def _db_session(name: str = "default") -> AsyncIterator[Any]:
            engine = create_async_engine(async_url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            core, bus = _active_core_for(name)
            try:
                async with open_uow(
                    core,
                    name,
                    factory=lambda: SQLAlchemyUnitOfWork(session_factory=sessions),
                    bus=bus,
                    wait=True,
                ) as session:
                    yield session
            finally:
                await engine.dispose()

        monkeypatch.setattr(cli_backup, "db_session", _db_session)
        monkeypatch.setattr(
            migrations, "list_database_instances", lambda: {"default": url}
        )
        yield url


def _fences(url: str) -> list[tuple[str, int]]:
    rows = run_sql(url, "SELECT name, fence FROM infra_lease ORDER BY name")
    return [(str(n), int(f)) for n, f in rows]


def test_fence_leases_command_is_dry_run_by_default(
    lease_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.cli.backup import main

    assert main(["fence-leases", "--json"]) == 0
    assert '"fence_before": 5' in capsys.readouterr().out
    assert _fences(lease_database) == [("free", 9), ("held", 5)]
    assert main(["fence-leases", "--database", "nope"]) == 2
    with pytest.raises(SystemExit, match="--reason"):
        main(["fence-leases", "--apply"])
    assert _fences(lease_database) == [("free", 9), ("held", 5)]


def test_fence_leases_apply_bumps_and_audits_in_the_same_transaction(
    lease_database: str,
) -> None:
    from sandloom.core.cli.backup import main

    assert (
        main(["fence-leases", "--apply", "--reason", "restore", "--operator", "al"])
        == 0
    )
    assert _fences(lease_database) == [
        ("free", 9 + LEASE_FENCE_OFFSET),
        ("held", 5 + LEASE_FENCE_OFFSET),
    ]
    rows = run_sql(
        lease_database,
        "SELECT data FROM audit_log WHERE action = 'core.lease.fences_bumped'",
    )
    assert len(rows) == 1  # one bump, one audit row: a rerun is the operator's choice
    data = rows[0][0]
    assert data["operator"] == "al"
    assert data["reason"] == "restore"
    assert data["offset"] == LEASE_FENCE_OFFSET
    assert sorted(data["leases"]) == ["free", "held"]
    assert data["max_fence_before"] == 9
    assert data["max_fence_after"] == 9 + LEASE_FENCE_OFFSET


def test_fence_leases_and_verify_refuse_a_database_that_is_not_postgresql(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import sandloom.core.migrations.migrations as migrations
    from sandloom.core.cli.backup import main

    monkeypatch.setattr(
        migrations,
        "list_database_instances",
        lambda: {"default": "sqlite+aiosqlite:///x.db"},
    )
    assert main(["fence-leases"]) == 2
    assert "needs a migration-chain database" in capsys.readouterr().err
    dump = _fake_dump(tmp_path)
    assert main(["verify", str(dump), "--server-url", "sqlite:///x.db"]) == 2
    assert "takes its schema from the models" in capsys.readouterr().err


def test_lease_table_matches_the_driver() -> None:
    """The restore bump writes the table of the ``database`` lease driver: every
    column it names exists in the driver's table and the model, with the same
    type, nullability and key."""
    from sandloom.core.drivers.database_lease import _TABLE as driver_table
    from sandloom.core.migrations.backup import _LEASE_TABLE
    from sandloom.core.sql_alchemy.models.lease import InfraLease

    model_table: Table = InfraLease.__table__  # type: ignore[assignment]
    assert _LEASE_TABLE.name == driver_table.name == model_table.name
    for column in _LEASE_TABLE.columns:
        for other in (driver_table.c[column.name], model_table.c[column.name]):
            assert type(column.type) is type(other.type), column.name
            assert column.nullable == other.nullable, column.name
            assert column.primary_key == other.primary_key, column.name
