"""Regression coverage for the infrastructure review of 22 October."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sandloom.core.config.settings import Settings
from sandloom.core.drivers.smtp_notifier import create_smtp_notifier
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.observability.guardrails import scrub_text
from sandloom.core.tenant import (
    TenantScoped,
    UnsafeTenantOperationError,
    bind_tenant,
    install_tenant_guard,
    unscoped,
)


def _test_database_url() -> str:
    from sandloom.core.testing.workers import worker_env_values

    values = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    return os.environ.get("SANDLOOM_TEST_DATABASE_URL") or values["DATABASE_URL"]


class _Base(DeclarativeBase):
    pass


class _Document(TenantScoped, _Base):
    __tablename__ = "review_tenant_documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str]


@pytest.mark.parametrize("operation", ["select", "update", "delete", "insert"])
def test_core_tenant_table_statements_cannot_escape_bound_scope(operation: str) -> None:
    install_tenant_guard()
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    try:
        with Session(engine) as session:
            with unscoped():
                session.add_all(
                    [
                        _Document(tenant_id=tenant_a, title="a"),
                        _Document(tenant_id=tenant_b, title="b"),
                    ]
                )
                session.commit()
            table = _Document.__table__
            statement = {
                "select": select(table),
                "update": update(table).values(title="changed"),
                "delete": delete(table),
                "insert": insert(table).values(
                    id=uuid.uuid4(), tenant_id=tenant_b, title="foreign"
                ),
            }[operation]
            with bind_tenant(tenant_a):
                assert len(session.scalars(select(_Document)).all()) == 1
                with pytest.raises(UnsafeTenantOperationError, match="Core"):
                    session.execute(statement)
            session.rollback()
            with unscoped():
                assert len(session.execute(select(table)).all()) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "secret",
    [
        "redis://:s3cret@host:6379/0",
        "rediss://:s3cret@host:6380/0",
        "sk_live_0123456789abcdefgh",
        "whsec_0123456789abcdefgh",
    ],
)
def test_free_text_redacts_provider_secrets(secret: str) -> None:
    assert "s3cret" not in scrub_text(secret)
    assert "0123456789abcdefgh" not in scrub_text(secret)


def test_smtp_missing_host_is_refused_before_network_io() -> None:
    with pytest.raises(ValueError, match="host"):
        create_smtp_notifier(
            InstanceConfig(
                capability="notification",
                name="mail",
                driver="smtp",
                options={"host": None},
            )
        )


def test_settings_errors_do_not_include_input_credentials() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, DATABASE_URL="postgresql://user:unique-secret@host/db")
    assert "unique-secret" not in str(exc.value)


def test_prod_setting_typo_is_actionable_without_dumping_values() -> None:
    with pytest.raises(ValidationError, match="AUTH_REVOCATION_FAIL_CLOSED") as exc:
        Settings(
            _env_file=None,
            ENVIRONMENT="prod",
            AUTH_REVOCATION_FAIL_CLOSE="sensitive-value",
        )
    assert "sensitive-value" not in str(exc.value)


@pytest.mark.parametrize("level", ["warn", "FATAL", "debug"])
def test_log_levels_are_normalized(level: str) -> None:
    from sandloom.core.config.settings import settings

    cfg = Settings(
        _env_file=None,
        DATABASE_URL=settings.database_url,
        CACHE_URL=settings.cache_url,
        BROKER_URL=settings.broker_url,
        JWT_SECRET=settings.jwt_secret,
        LOG_LEVEL=level,
    )
    assert cfg.log_level in {"WARNING", "CRITICAL", "DEBUG"}


def test_migration_percent_encoded_dsn_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic import command

    from sandloom.core.descriptors import ModuleDescriptor
    from sandloom.core.migrations import migrations

    expected = "postgresql+psycopg2://user:p%40ss%25word@host/database"
    monkeypatch.setattr(migrations, "_is_migration_dir", lambda path: True)
    monkeypatch.setattr(migrations, "_has_env_script", lambda path: True)
    monkeypatch.setattr(migrations, "resolve_database_sync_url", lambda name: expected)
    observed = []
    monkeypatch.setattr(
        command,
        "upgrade",
        lambda cfg, revision: observed.append(cfg.get_main_option("sqlalchemy.url")),
    )
    migrations.upgrade_module(
        ModuleDescriptor(name="sample", version="1.0", migrations_path="unused"),
        dry_run=False,
    )
    assert observed == [expected]


async def test_reconfigured_providers_close_instead_of_being_orphaned() -> None:
    from sandloom.core.tests.test_infra_registry import _registry_with_fake

    registry = _registry_with_fake()
    original = registry.provider("cache", "default")
    registry.configure_instance(InstanceConfig("cache", "default", "fake", {}))
    replacement = registry.provider("cache", "default")
    assert replacement is not original
    await registry.aclose_all()
    assert original.closed and replacement.closed


async def test_health_probes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.tests.test_infra_registry import _registry_with_fake

    registry = _registry_with_fake()
    provider = registry.provider("cache", "default")

    async def hang():
        await asyncio.sleep(100)

    monkeypatch.setattr(provider, "health", hang)
    monkeypatch.setattr(settings, "health_check_timeout_seconds", 0.01)
    result = await asyncio.wait_for(registry.health_all(), timeout=0.5)
    assert result["cache:default"].status == "fail"
    assert result["cache:sessions"].status == "ok"
    await registry.aclose_all()


async def test_broker_unavailability_is_reported_as_health_failure() -> None:
    from sandloom.core.drivers.event_bus_driver import WrappedEventBusProvider

    async def client():
        raise ConnectionError("unavailable")

    provider = WrappedEventBusProvider(
        name="default",
        driver="streams",
        options={},
        bus_instance=SimpleNamespace(),
        client_factory=client,
    )
    assert (await provider.health()).status == "fail"


async def test_strict_profile_checks_physical_redis_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core import production_profile
    from sandloom.core.tests.test_production_profile import _settings

    cfg = _settings()
    provider = SimpleNamespace(
        deployment_identity=AsyncMock(return_value="same-process")
    )
    core = SimpleNamespace(
        settings=cfg, infra=SimpleNamespace(provider=lambda *args: provider)
    )
    monkeypatch.setattr(
        production_profile, "verify_tenant_rls_coverage", AsyncMock(return_value=[])
    )
    with pytest.raises(
        production_profile.ProductionProfileError, match="same Redis process"
    ):
        await production_profile.enforce_runtime(core)


def test_rls_target_role_checked_before_target_tables_exist() -> None:
    from sandloom.core.cli.extract import _target_rls_problem

    source = SimpleNamespace(all=lambda *args: [("tenant_documents",)])
    target = SimpleNamespace(all=lambda *args: [], one=lambda *args: (False,))
    assert "target role" in _target_rls_problem(source, target, ["tenant_documents"])


def test_cursor_mac_never_reuses_rpc_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core import planner

    for name in ("planner_cursor_secret", "internal_api_token", "jwt_secret"):
        monkeypatch.setattr(planner.settings, name, None)
    monkeypatch.setattr(planner.settings, "rpc_private_key", "private-key-material")
    with pytest.raises(RuntimeError, match="PLANNER_CURSOR_SECRET"):
        planner._cursor_key()


async def test_database_health_does_not_wait_for_exhausted_request_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers import sqlalchemy_database as driver

    monkeypatch.setattr(settings, "db_pool_size", 1)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            "database", "health-capacity", "sqlalchemy", {"url": _test_database_url()}
        )
    )
    try:
        async with provider.engine.connect():
            result = await asyncio.wait_for(provider.health(), timeout=1.0)
            assert result.status == "ok"
    finally:
        await provider.aclose()


async def test_read_only_session_refuses_select_writing_function() -> None:
    from sqlalchemy import func, text
    from sqlalchemy.exc import DBAPIError

    from sandloom.core.di import _read_only_uow_factory
    from sandloom.core.drivers.sqlalchemy_database import build_disposable_engine

    engine = build_disposable_engine(_test_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE IF NOT EXISTS review_readonly_effects (n integer)")
            )
            await connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION review_readonly_write() RETURNS integer LANGUAGE plpgsql AS $$ BEGIN INSERT INTO review_readonly_effects VALUES (1); RETURN 1; END $$"
                )
            )
        core = SimpleNamespace(database=lambda name: SimpleNamespace(engine=engine))
        factory = _read_only_uow_factory(core)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            async with factory() as unit:
                await unit.session.execute(select(func.review_readonly_write()))
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM review_readonly_effects")
                )
            ).scalar_one() == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP FUNCTION IF EXISTS review_readonly_write()")
            )
            await connection.execute(
                text("DROP TABLE IF EXISTS review_readonly_effects")
            )
        await engine.dispose()


def test_migration_lock_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import create_engine, text

    from sandloom.core.config.settings import settings
    from sandloom.core.migrations.migrations import (
        MIGRATION_LOCK_KEY,
        MigrationDependencyError,
        migration_lock,
    )
    from sandloom.core.migrations.urls import to_sync_url

    url = to_sync_url(_test_database_url())
    engine = create_engine(url)
    monkeypatch.setattr(settings, "migration_lock_timeout_seconds", 0.02)
    try:
        with engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            try:
                with pytest.raises(MigrationDependencyError, match="Timed out"):
                    with migration_lock([url]):
                        pytest.fail("another process already holds the migration lock")
            finally:
                holder.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
def test_runtime_deadlines_are_finite(value: str) -> None:
    with pytest.raises(ValidationError, match="finite"):
        Settings(_env_file=None, SHUTDOWN_BUDGET_SECONDS=value)


def test_mixed_orm_and_core_alias_cannot_read_another_tenant() -> None:
    from sqlalchemy import true
    from sqlalchemy.orm import aliased

    install_tenant_guard()
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    try:
        with Session(engine) as session:
            with unscoped():
                session.add_all(
                    [
                        _Document(tenant_id=tenant_a, title="a"),
                        _Document(tenant_id=tenant_b, title="b-secret"),
                    ]
                )
                session.commit()
            raw_alias = _Document.__table__.alias("raw_alias")
            statement = (
                select(_Document.title, raw_alias.c.title)
                .select_from(_Document)
                .join(raw_alias, true())
            )
            with bind_tenant(tenant_a):
                with pytest.raises(UnsafeTenantOperationError, match="Core"):
                    session.execute(statement)
                orm_alias = aliased(_Document)
                rows = session.execute(
                    select(_Document.title, orm_alias.title)
                    .select_from(_Document)
                    .join(orm_alias, true())
                ).all()
                assert rows == [("a", "a")]
    finally:
        engine.dispose()


def test_missing_smtp_host_fails_build_free_registry_validation() -> None:
    from sandloom.core.drivers.bootstrap import build_infra_registry
    from sandloom.core.infra.registry import ConfigSchemaError
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    config = {
        "notification": {
            "mail": InstanceConfig("notification", "mail", "smtp", {"host": None})
        }
    }
    with pytest.raises(ConfigSchemaError, match="host"):
        build_infra_registry(
            InMemorySchemaRegistry(), config=config, load_plugins=False
        )


def test_production_local_storage_requires_explicit_shared_volume_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sandloom.core.config.common import Environment
    from sandloom.core.config.settings import settings
    from sandloom.core.drivers.local_objectstore import create_local_objectstore

    monkeypatch.setattr(settings, "environment", Environment.prod)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", [])
    config = InstanceConfig("objectstore", "review", "local", {"path": str(tmp_path)})
    with pytest.raises(ValueError, match="local_storage_in_prod"):
        create_local_objectstore(config)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["local_storage_in_prod"])
    assert create_local_objectstore(config).driver == "local"


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan")])
def test_rpc_binding_deadlines_are_finite(value: float) -> None:
    from sandloom.core.config.common import validate_service_binding

    with pytest.raises(ValueError, match="positive finite"):
        validate_service_binding(
            "example.read.v1",
            {"base_url": "http://provider:8000", "timeout_seconds": value},
        )
