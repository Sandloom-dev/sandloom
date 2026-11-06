"""Database row security for TenantScoped tables (defense in depth).

The ORM tenant guard (:mod:`sandloom.core.tenant`) scopes ORM statements, but raw
``text()`` / Core statements bypass it. Row security moves the same
invariant INTO the database, where the database has it (its SQL dialect
renderer's :class:`~sandloom.core.sql_alchemy.portable.RowSecurity`;
PostgreSQL's Row-Level Security): a policy compares each row's ``tenant_id`` with the
transaction-local setting ``sandloom.tenant_id`` that the framework keeps in sync
with the bound tenant when the database instance runs with ``tenant_rls``
enabled (``INFRA={"database": {"default": {"tenant_rls": true}}}``).

Use from a module's Alembic migration::

    from sandloom.core.orm import disable_tenant_rls, enable_tenant_rls

    def upgrade() -> None:
        op.create_table("notes_note", ...)  # has a tenant_id uuid column
        enable_tenant_rls(op, "notes_note")
        # tables that privileged system jobs must reach across tenants:
        # enable_tenant_rls(op, "notes_note", system_access=True)

    def downgrade() -> None:
        disable_tenant_rls(op, "notes_note")
        op.drop_table("notes_note")

The tenant policy has NO bypass: the ordinary application role sees and writes
only rows whose ``tenant_id`` equals a well-formed ``sandloom.tenant_id``. An unset,
empty or malformed setting yields zero rows and refused writes (never a cast
error). ``unscoped()`` no longer lifts RLS — cross-tenant work goes through the
framework's *system channel*: a separate login role (default ``sandloom_system``)
granted, per table, by ``system_access=True`` (an explicit permissive policy
``TO sandloom_system`` + the DML grants). The app role must not be that role nor a
member of it.

PostgreSQL superusers, ``BYPASSRLS`` roles and table OWNERS (who can simply
``ALTER TABLE ... DISABLE ROW LEVEL SECURITY``) are outside any policy, so the
application must connect as a role that is none of those; the driver verifies
this at startup (:func:`sandloom.core.sql_alchemy.tenant_rls.verify_tenant_rls_roles`).

Threat model (see ``docs/advanced/security-model.md``): RLS guards against
forgotten tenant filters and removes every *privileged* bypass from the app
role. It does NOT authenticate the tenant context: any code that can send
arbitrary SQL as the app role can still ``set_config('sandloom.tenant_id', <other
tenant>, true)`` and act as that tenant.

The SQL is the renderer's (``row_security()``): this module names no
database. ``op`` is the Alembic operations object; its migration context
names the database. A database without row security refuses
(SANDLOOM-R015); configuring ``tenant_rls`` on one stops startup
(SANDLOOM-B026).
"""

from __future__ import annotations

from typing import Any, Protocol

from sandloom.core.sql_alchemy.portable import (
    TENANT_SETTING,
    RowSecurity,
    UnsupportedSqlForm,
    dialect_name,
    sql_dialect,
)

#: Default name of the dedicated system-channel login role.
DEFAULT_SYSTEM_ROLE = "sandloom_system"


class _Executes(Protocol):
    def execute(self, sql: Any, /) -> Any: ...

    def get_context(self) -> Any: ...


def row_security(bind: Any) -> RowSecurity:
    """The row security of the database of ``bind`` (a dialect name, a
    dialect, a connection, an engine or a session); refuses a database
    without it (:class:`UnsupportedSqlForm`, SANDLOOM-R015)."""
    dialect = dialect_name(bind)
    renderer = sql_dialect(dialect)
    if renderer is None:
        raise UnsupportedSqlForm("row_security", dialect, renderer=False)
    security = renderer.row_security()
    if security is None:
        raise UnsupportedSqlForm("row_security", dialect)
    return security


def enable_tenant_rls_statements(
    table: str,
    *,
    dialect: Any,
    column: str = "tenant_id",
    schema: str | None = None,
    system_access: bool = False,
    system_role: str = DEFAULT_SYSTEM_ROLE,
) -> list[str]:
    """SQL statements enabling + forcing tenant row security on ``table``
    (idempotent) on ``dialect`` (anything :func:`row_security` takes).

    With ``system_access=True`` the dedicated ``system_role`` (which must
    exist) gets an explicit cross-tenant policy and the DML grants on this
    table only; without it any such policy is dropped (least privilege).
    """
    return row_security(dialect).enable_statements(
        table,
        column=column,
        schema=schema,
        system_access=system_access,
        system_role=system_role,
    )


def disable_tenant_rls_statements(
    table: str, *, dialect: Any, schema: str | None = None
) -> list[str]:
    """SQL statements reverting :func:`enable_tenant_rls_statements`."""
    return row_security(dialect).disable_statements(table, schema=schema)


def _op_dialect(op: _Executes) -> Any:
    """The dialect of the database the migration runs on (online or ``--sql``)."""
    return op.get_context().dialect


def enable_tenant_rls(
    op: _Executes,
    table: str,
    *,
    column: str = "tenant_id",
    schema: str | None = None,
    system_access: bool = False,
    system_role: str = DEFAULT_SYSTEM_ROLE,
) -> None:
    """Enable + FORCE the database's row security with the Sandloom tenant
    policy on ``table``.

    ``op`` is the Alembic ``op`` module; its migration context names the
    database, whose renderer writes the SQL. ``column`` must be the table's
    ``uuid`` tenant column (``TenantScoped`` declares ``tenant_id``).
    ``system_access=True`` lets the system-channel role (``system_role``,
    default ``sandloom_system``; it must already exist) read and write every
    tenant's rows of THIS table. Re-running is safe (policies are replaced).
    """
    for sql in enable_tenant_rls_statements(
        table,
        dialect=_op_dialect(op),
        column=column,
        schema=schema,
        system_access=system_access,
        system_role=system_role,
    ):
        op.execute(sql)


def disable_tenant_rls(op: _Executes, table: str, *, schema: str | None = None) -> None:
    """Drop the Sandloom tenant policies and disable row security on
    ``table`` (for downgrade).
    """
    for sql in disable_tenant_rls_statements(
        table, dialect=_op_dialect(op), schema=schema
    ):
        op.execute(sql)


__all__ = [
    "DEFAULT_SYSTEM_ROLE",
    "TENANT_SETTING",
    "disable_tenant_rls",
    "disable_tenant_rls_statements",
    "enable_tenant_rls",
    "enable_tenant_rls_statements",
    "row_security",
]
