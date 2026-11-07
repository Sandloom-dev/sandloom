"""Row security through the SQL dialect renderer (infra-agnostic slice 6).

Every SQL text the kernel sends to PostgreSQL for tenant isolation moved
from ``rls.py``, ``tenant_rls.py`` and the planner adapter into the
renderer's :class:`~sandloom.core.sql_alchemy.portable.RowSecurity`. These
tests pin each text byte for byte against the literals the kernel wrote
before the move (copied below as the oracle), and check SQLite's answer
(no row security), the B026 refusals and the application-enforced label.
"""

from __future__ import annotations

import itertools
import logging
import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core import app_factory, error_codes, production_profile
from sandloom.core.drivers.dbapi_asyncpg import _merged_begin_sql, begin_texts
from sandloom.core.drivers.sql_dialects import POSTGRESQL, SQLITE
from sandloom.core.guarantee_lattice import (
    APPLICATION_ENFORCED_ISOLATION,
    Mechanism,
    tenant_isolation_label,
)
from sandloom.core.sql_alchemy import portable, rls, tenant_rls
from sandloom.core.sql_alchemy.portable import UnsupportedSqlForm
from sandloom.core.sql_alchemy.tenant_rls import (
    RowSecurityUnavailable,
    TenantRLSConfigError,
    _Binding,
    _set_config_sql,
    install_tenant_rls,
    row_security_problem,
    tenant_rls_coverage,
    verify_tenant_rls_roles,
)

PG = POSTGRESQL.row_security()
TENANT = "00000000-0000-0000-0000-00000000000a"

# --- the oracle: the kernel's literals before slice 6, verbatim -------------

_OLD_SETTING = "sandloom.tenant_id"
_OLD_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_OLD_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _old_quote(ident: str, what: str) -> str:
    if not _OLD_IDENT.match(ident):
        raise ValueError(f"invalid {what} identifier for RLS: {ident!r}")
    return f'"{ident}"'


def _old_qualified(table: str, schema: str | None) -> str:
    quoted = _old_quote(table, "table")
    return f"{_old_quote(schema, 'schema')}.{quoted}" if schema else quoted


def _old_predicate(column: str) -> str:
    setting = f"current_setting('{_OLD_SETTING}', true)"
    expression = f"CASE WHEN {setting} ~ '{_OLD_UUID_RE}' THEN {setting}::uuid END"
    return f"({_old_quote(column, 'column')} = ({expression}))"


def _old_enable(
    table: str,
    *,
    column: str = "tenant_id",
    schema: str | None = None,
    system_access: bool = False,
    system_role: str = "sandloom_system",
) -> list[str]:
    target = _old_qualified(table, schema)
    policy = _old_quote(f"{table}_tenant_isolation"[:63], "policy")
    sys_policy = _old_quote(f"{table}_system_access"[:63], "policy")
    predicate = _old_predicate(column)
    statements = [
        f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {policy} ON {target}",
        f"CREATE POLICY {policy} ON {target} AS PERMISSIVE FOR ALL "
        f"USING {predicate} WITH CHECK {predicate}",
        f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
    ]
    if system_access:
        role = _old_quote(system_role, "role")
        statements += [
            f"CREATE POLICY {sys_policy} ON {target} AS PERMISSIVE FOR ALL "
            f"TO {role} USING (true) WITH CHECK (true)",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO {role}",
        ]
    return statements


def _old_disable(table: str, *, schema: str | None = None) -> list[str]:
    target = _old_qualified(table, schema)
    policy = _old_quote(f"{table}_tenant_isolation"[:63], "policy")
    sys_policy = _old_quote(f"{table}_system_access"[:63], "policy")
    return [
        f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
        f"DROP POLICY IF EXISTS {policy} ON {target}",
        f"ALTER TABLE {target} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {target} DISABLE ROW LEVEL SECURITY",
    ]


def _old_set_config(paramstyle: str, tenant: str) -> tuple[str, Any]:
    if paramstyle == "pyformat":
        mark: str = "%(t)s"
        params: Any = {"t": tenant}
    elif paramstyle == "named":
        mark, params = ":t", {"t": tenant}
    else:
        mark = {
            "numeric_dollar": "$1",
            "numeric": ":1",
            "qmark": "?",
            "format": "%s",
        }.get(paramstyle, "%s")
        params = (tenant,)
    return f"SELECT set_config('{_OLD_SETTING}', {mark}, true)", params


_OLD_QUERIES = {
    "role_attributes": (
        "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname = current_user"
    ),
    "privileged_memberships": (
        "SELECT rolname FROM pg_roles WHERE (rolsuper OR rolbypassrls) "
        "AND rolname <> current_user AND pg_has_role(current_user, oid, 'MEMBER') "
        "ORDER BY rolname"
    ),
    "owned_secured_tables": (
        "SELECT n.nspname || '.' || c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relrowsecurity AND pg_has_role(current_user, c.relowner, 'MEMBER') "
        "ORDER BY 1"
    ),
    "unconditional_policies": (
        "SELECT DISTINCT p.schemaname || '.' || p.tablename || ':' || p.policyname "
        "FROM pg_policies p, unnest(p.roles) AS r(role) "
        "WHERE r.role <> 'public' AND pg_has_role(current_user, r.role, 'MEMBER') "
        "AND (p.qual = 'true' OR p.with_check = 'true') ORDER BY 1"
    ),
    "secured_tables": (
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND c.relname = ANY(:names) "
        "AND n.nspname = ANY(current_schemas(false))"
    ),
    "table_policies": (
        "SELECT tablename, policyname FROM pg_policies "
        "WHERE tablename = ANY(:names) AND schemaname = ANY(current_schemas(false))"
    ),
}


# --- byte identity on PostgreSQL --------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "schema", "system_access", "system_role"),
    [
        ("notes_note", "tenant_id", None, False, "sandloom_system"),
        ("notes_note", "tenant_id", None, True, "sandloom_system"),
        ("saga_instances", "org_id", "app", True, "ops_system"),
        ("t" * 50, "tenant_id", None, False, "sandloom_system"),
    ],
)
def test_policy_ddl_is_byte_identical(
    table: str, column: str, schema: str | None, system_access: bool, system_role: str
) -> None:
    new = rls.enable_tenant_rls_statements(
        table,
        dialect="postgresql",
        column=column,
        schema=schema,
        system_access=system_access,
        system_role=system_role,
    )
    assert new == _old_enable(
        table,
        column=column,
        schema=schema,
        system_access=system_access,
        system_role=system_role,
    )
    assert rls.disable_tenant_rls_statements(
        table, dialect="postgresql", schema=schema
    ) == _old_disable(table, schema=schema)
    assert PG.policy_name(table) == f"{table}_tenant_isolation"[:63]


def test_the_migration_helper_sends_the_same_ddl_through_op() -> None:
    sent: list[str] = []
    op = SimpleNamespace(
        execute=sent.append,
        get_context=lambda: SimpleNamespace(dialect=postgresql.dialect()),
    )
    rls.enable_tenant_rls(op, "notes_note", system_access=True)
    rls.disable_tenant_rls(op, "notes_note", schema="app")
    assert sent == _old_enable("notes_note", system_access=True) + _old_disable(
        "notes_note", schema="app"
    )


@pytest.mark.parametrize(
    "paramstyle", ["numeric_dollar", "pyformat", "named", "numeric", "qmark", "format"]
)
@pytest.mark.parametrize("tenant", [TENANT, ""])
def test_binding_statement_is_byte_identical(paramstyle: str, tenant: str) -> None:
    assert _set_config_sql(_Binding(PG, paramstyle), tenant) == _old_set_config(
        paramstyle, tenant
    )


#: driver_compat.begin_sql before slice 7b (asyncpg's Transaction.start text).
_OLD_ISOLATION_SQL = {
    "read_committed": " ISOLATION LEVEL READ COMMITTED",
    "read_uncommitted": " ISOLATION LEVEL READ UNCOMMITTED",
    "repeatable_read": " ISOLATION LEVEL REPEATABLE READ",
    "serializable": " ISOLATION LEVEL SERIALIZABLE",
}


def begin_sql(isolation: str | None, readonly: bool, deferrable: bool) -> str:
    sql = "BEGIN" + _OLD_ISOLATION_SQL.get(isolation or "", "")
    if readonly:
        sql += " READ ONLY"
    if deferrable:
        sql += " DEFERRABLE"
    return sql


@pytest.mark.parametrize(
    ("isolation", "readonly", "deferrable"),
    list(
        itertools.product(
            [
                None,
                "read_committed",
                "read_uncommitted",
                "repeatable_read",
                "serializable",
            ],
            [False, True],
            [False, True],
        )
    ),
)
@pytest.mark.parametrize("tenant", [TENANT, TENANT.upper(), ""])
def test_merged_begin_is_byte_identical(
    isolation: str | None, readonly: bool, deferrable: bool, tenant: str
) -> None:
    adapter = SimpleNamespace(
        isolation_level=isolation, readonly=readonly, deferrable=deferrable
    )
    value = str(uuid.UUID(tenant)) if tenant else ""
    old = (
        f"{begin_sql(isolation, readonly, deferrable)}; "
        f"SELECT set_config('{_OLD_SETTING}', '{value}', true)"
    )
    # The BEGIN texts are the renderer's (slice 7b) for the units it writes,
    # asyncpg's for the rest: on PostgreSQL every one is the old text.
    assert _merged_begin_sql(adapter, tenant, PG, begin_texts(POSTGRESQL)) == old


def test_merged_begin_refuses_a_non_uuid_value() -> None:
    adapter = SimpleNamespace(isolation_level=None, readonly=False, deferrable=False)
    with pytest.raises(ValueError):
        _merged_begin_sql(adapter, "x', true); --", PG, begin_texts(POSTGRESQL))


@pytest.mark.parametrize("name", sorted(_OLD_QUERIES))
def test_role_and_coverage_queries_are_byte_identical(name: str) -> None:
    statement = getattr(PG, name)()
    assert statement.text == _OLD_QUERIES[name]


def test_the_renderer_answers_once_per_dialect() -> None:
    # Built once: the hook never asks the registry per statement.
    assert POSTGRESQL.row_security() is POSTGRESQL.row_security()
    assert PG.role_attributes() is PG.role_attributes()


def test_install_builds_the_binding_once_per_engine() -> None:
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    try:
        install_tenant_rls(engine, merged_begin=False)
        binding = getattr(engine.sync_engine.dialect, tenant_rls._BINDING_ATTR)
        assert binding.sql == "SELECT set_config('sandloom.tenant_id', $1, true)"
        assert binding.security is PG
    finally:
        engine.sync_engine.dispose()


# --- SQLite: no row security ------------------------------------------------


def test_sqlite_has_no_row_security_and_the_helpers_refuse() -> None:
    assert SQLITE.row_security() is None
    assert portable.row_security_for("sqlite") is None
    assert portable.row_security_of_url("sqlite+aiosqlite:///x.db") is None
    assert portable.row_security_of_url("postgresql+asyncpg://h/db") is PG
    with pytest.raises(UnsupportedSqlForm, match="SANDLOOM-R015"):
        rls.enable_tenant_rls_statements("notes_note", dialect="sqlite")


def test_install_on_a_database_without_row_security_is_b026() -> None:
    engine = create_engine("sqlite://")  # the sync driver: no extra needed
    try:
        with pytest.raises(RowSecurityUnavailable, match="SANDLOOM-B026"):
            install_tenant_rls(engine)
    finally:
        engine.dispose()
    assert issubclass(RowSecurityUnavailable, TenantRLSConfigError)
    assert error_codes.code_for_exception(RowSecurityUnavailable("x")) == (
        "SANDLOOM-B026"
    )
    assert error_codes.get("B026").slug == "row-security-missing"


# --- B026 at boot -----------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"tenant_rls": True},
        {"system_url": "sqlite+aiosqlite:///sys.db"},
        {"require_system_channel": True},
    ],
)
def test_row_security_options_without_row_security_are_b026(
    options: dict[str, Any],
) -> None:
    problem = row_security_problem("default", "sqlite+aiosqlite:///x.db", options)
    assert problem is not None and "SANDLOOM-B026" in problem
    assert next(iter(options)) in problem
    # PostgreSQL has row security (its system channel too); no options, no problem.
    pg = "postgresql+asyncpg://h/db"
    on_pg = {k: (pg if k == "system_url" else v) for k, v in options.items()}
    assert row_security_problem("default", pg, on_pg) is None
    assert row_security_problem("default", "sqlite+aiosqlite:///x.db", {}) is None


def test_the_system_channel_url_is_checked_too() -> None:
    pg = "postgresql+asyncpg://h/db"
    lite = "sqlite+aiosqlite:///sys.db"
    # the instance option
    problem = row_security_problem("default", pg, {"system_url": lite})
    assert problem is not None and "system channel" in problem
    assert "SANDLOOM-B026" in problem
    # the default instance's DATABASE_SYSTEM_URL, with tenant_rls on
    problem = row_security_problem("default", pg, {"tenant_rls": True}, system_url=lite)
    assert problem is not None and "SANDLOOM-B026" in problem
    problem = row_security_problem(
        "default", pg, {"require_system_channel": True}, system_url=lite
    )
    assert problem is not None
    # a PostgreSQL system channel, or no system channel at all: fine
    assert (
        row_security_problem("default", pg, {"tenant_rls": True}, system_url=pg) is None
    )
    assert row_security_problem("default", pg, {}) is None
    assert row_security_problem("default", pg, {}, system_url=pg) is None


def test_a_system_channel_on_another_database_is_b026_without_any_option() -> None:
    """DATABASE_SYSTEM_URL naming a SQLite file next to a PostgreSQL instance
    is refused even with no row-security option set."""
    pg = "postgresql+asyncpg://h/db"
    lite = "sqlite+aiosqlite:///sys.db"
    problem = row_security_problem("default", pg, {}, system_url=lite)
    assert problem is not None and "SANDLOOM-B026" in problem
    assert "sqlite" in problem and "postgresql" in problem
    problem = row_security_problem("default", pg, {"system_url": lite})
    assert problem is not None and "SANDLOOM-B026" in problem


def test_a_system_channel_on_another_database_of_the_same_server_is_b026() -> None:
    """The same dialect but another database name is refused; the system
    role's own credentials and address are not (the password is never shown)."""
    pg = "postgresql+asyncpg://app:pw@h:5432/db"
    other = "postgresql+asyncpg://system:secret@h:5432/other?password=qsecret"
    problem = row_security_problem("default", pg, {}, system_url=other)
    assert problem is not None and "SANDLOOM-B026" in problem
    assert "secret" not in problem and "qsecret" not in problem
    # the same database through another address (a pooler) and role
    same = "postgresql+asyncpg://system:secret@pooler:6432/db"
    assert (
        row_security_problem("default", pg, {"tenant_rls": True}, system_url=same)
        is None
    )


async def test_startup_checks_on_a_database_without_row_security_are_fatal() -> None:
    conn: Any = SimpleNamespace(dialect=sqlite.dialect())
    with pytest.raises(TenantRLSConfigError, match="SANDLOOM-B026"):
        await verify_tenant_rls_roles(conn)
    with pytest.raises(TenantRLSConfigError, match="SANDLOOM-B026"):
        await tenant_rls_coverage(conn, ["notes_note"])


def _cfg(infra: dict[str, Any], **extra: Any) -> Any:
    return SimpleNamespace(
        infra=infra, database_url="postgresql+asyncpg://u:p@h/db", **extra
    )


def _load_infra(monkeypatch: pytest.MonkeyPatch, databases: dict[str, Any]) -> None:
    """``load_infra_config`` answering ``databases`` (name -> options)."""
    instances = {
        name: SimpleNamespace(options=options) for name, options in databases.items()
    }
    monkeypatch.setattr(
        app_factory.config, "load_infra_config", lambda _cfg: {"database": instances}
    )


def test_configuration_problems_refuse_b026_build_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg({})
    _load_infra(
        monkeypatch,
        {
            "default": {"tenant_rls": True},  # DATABASE_URL: PostgreSQL
            "lite": {"url": "sqlite+aiosqlite:///lite.db", "tenant_rls": True},
        },
    )
    problem = app_factory.row_security_config_problem(cfg)
    assert problem is not None
    assert "database/lite" in problem and "SANDLOOM-B026" in problem
    assert "database/default" not in problem
    # Every entry point runs it (enforce_configuration).
    found = app_factory.configuration_problems(app_factory.settings)
    assert any("database/lite" in p and "SANDLOOM-B026" in p for p in found)
    _load_infra(monkeypatch, {"default": {"tenant_rls": True}})
    assert app_factory.row_security_config_problem(cfg) is None


def test_the_driver_factory_refuses_b026() -> None:
    from sandloom.core.drivers.sqlalchemy_database import create_sqlalchemy_database
    from sandloom.core.infra.config import InstanceConfig

    with pytest.raises(RowSecurityUnavailable, match="SANDLOOM-B026"):
        create_sqlalchemy_database(
            InstanceConfig(
                capability="database",
                name="lite",
                driver="sqlalchemy",
                options={"url": "sqlite+aiosqlite:///lite.db", "tenant_rls": True},
            )
        )


# --- application-enforced isolation: the label and the warning ---------------


def test_the_lattice_labels_isolation_from_the_instance_features() -> None:
    enforced = Mechanism.from_features({"transactions", "tenant_rls"})
    assert enforced.row_security and tenant_isolation_label(enforced) is None
    applied = Mechanism.from_features({"transactions", "command_idempotency"})
    assert applied.command_log and not applied.row_security
    assert tenant_isolation_label(applied) == APPLICATION_ENFORCED_ISOLATION
    assert tenant_isolation_label() == APPLICATION_ENFORCED_ISOLATION  # unsafe default


#: Metadata mapping one table that :func:`tenant_scoped_tables` names (the
#: tests patch it rather than declare a TenantScoped model process-wide).
_TENANT_TABLE = "test_row_security_tenant_rows"
_TENANT_METADATA = MetaData()
Table(_TENANT_TABLE, _TENANT_METADATA, Column("id", Integer, primary_key=True))


@pytest.fixture(autouse=True)
def _tenant_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_factory.config, "tenant_scoped_tables", lambda: [_TENANT_TABLE])


class _Infra:
    """The registry surface :func:`application_enforced_isolation` reads."""

    def __init__(self, instances: dict[str, dict[str, Any]]) -> None:
        self._instances = instances

    def instances(self, capability: str) -> list[str]:
        return sorted(self._instances)

    def sql_dialects(self, capability: str, name: str) -> Any:
        return ("sqlalchemy", frozenset({"postgresql", "sqlite"}))

    def instance_config(self, capability: str, name: str) -> Any:
        return SimpleNamespace(options=self._instances[name])

    def advertised_features(self, capability: str, name: str) -> frozenset[str]:
        features = {"transactions"}
        if self._instances[name].get("tenant_rls"):
            features.add("tenant_rls")
        return frozenset(features)


def test_only_instances_without_row_security_are_application_enforced() -> None:
    infra = _Infra(
        {
            "default": {"url": "postgresql+asyncpg://h/db"},
            "lite": {"url": "sqlite+aiosqlite:///lite.db"},
        }
    )
    assert app_factory.application_enforced_isolation(infra, _TENANT_METADATA) == [  # type: ignore[arg-type]
        ("lite", APPLICATION_ENFORCED_ISOLATION)
    ]
    # No TenantScoped table mapped: nothing to isolate, nothing reported.
    assert app_factory.application_enforced_isolation(infra, MetaData()) == []  # type: ignore[arg-type]


def test_startup_warns_once_per_application_enforced_instance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    infra = _Infra(
        {
            "default": {"url": "postgresql+asyncpg://h/db"},
            "lite": {"url": "sqlite+aiosqlite:///lite.db"},
        }
    )
    log = logging.getLogger("sandloom.app")
    with caplog.at_level(logging.WARNING, logger="sandloom.app"):
        app_factory.warn_application_enforced_isolation(infra, _TENANT_METADATA, log)  # type: ignore[arg-type]
    records = [r for r in caplog.records if r.name == "sandloom.app"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "database/lite" in message
    assert "application-enforced tenant isolation" in message


def test_the_strict_profile_refuses_an_instance_without_row_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No tenant_rls (it would be B026): the profile's own refusal, as today.
    cfg = _cfg(
        {
            "database": {
                "default": {
                    "driver": "sqlalchemy",
                    "url": "sqlite+aiosqlite:///lite.db",
                }
            }
        },
        tenancy_mode="multi",
    )
    errors, _warnings = production_profile.config_problems(cfg)
    assert any("database/default: tenant_rls is off" in e for e in errors)
    # And asking for it on that database is refused before the profile runs.
    _load_infra(
        monkeypatch,
        {"default": {"url": "sqlite+aiosqlite:///lite.db", "tenant_rls": True}},
    )
    problem = app_factory.row_security_config_problem(_cfg({}))
    assert problem is not None and "SANDLOOM-B026" in problem
