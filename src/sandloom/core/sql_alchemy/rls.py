"""PostgreSQL Row-Level Security for TenantScoped tables (defense in depth).

The ORM tenant guard (:mod:`sandloom.core.tenant`) scopes ORM statements, but raw
``text()`` / Core statements bypass it. Row-Level Security moves the same
invariant INTO the database: a policy compares each row's ``tenant_id`` with the
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

Threat model (see ``docs/reference/security-model.md``): RLS guards against
forgotten tenant filters and removes every *privileged* bypass from the app
role. It does NOT authenticate the tenant context: any code that can send
arbitrary SQL as the app role can still ``set_config('sandloom.tenant_id', <other
tenant>, true)`` and act as that tenant.

This module is plain SQL text (no SQLAlchemy / Alembic import); ``op`` is the
Alembic operations object — anything with ``execute(sql: str)`` works.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

#: Transaction-local setting maintained by the UoW engine hook.
TENANT_SETTING = "sandloom.tenant_id"
#: Default name of the dedicated system-channel login role.
DEFAULT_SYSTEM_ROLE = "sandloom_system"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
#: Canonical lowercase UUID text (what the engine hook writes).
_UUID_RE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class _Executes(Protocol):
    def execute(self, sql: Any, /) -> Any: ...


def _quote(ident: str, what: str) -> str:
    if not _IDENT.match(ident):
        raise ValueError(f"invalid {what} identifier for RLS: {ident!r}")
    return f'"{ident}"'


def _qualified(table: str, schema: str | None) -> str:
    quoted = _quote(table, "table")
    return f"{_quote(schema, 'schema')}.{quoted}" if schema else quoted


def tenant_rls_policy_name(table: str) -> str:
    """Name of the tenant-isolation policy created for ``table``."""
    return f"{table}_tenant_isolation"[:63]


def system_rls_policy_name(table: str) -> str:
    """Name of the system-channel policy created for ``table``."""
    name = f"{table}_system_access"[:63]
    if name == tenant_rls_policy_name(table):  # pragma: no cover - 63-char names
        raise ValueError(f"table name too long for RLS policies: {table!r}")
    return name


def tenant_context_expression() -> str:
    """SQL expression: the bound tenant as ``uuid``, or NULL (fail closed).

    ``CASE`` guarantees the cast runs only on a canonical UUID string, so an
    unset / empty / malformed setting yields NULL (zero rows, refused writes)
    instead of an ``invalid input syntax for type uuid`` error.
    """
    setting = f"current_setting('{TENANT_SETTING}', true)"
    return f"CASE WHEN {setting} ~ '{_UUID_RE}' THEN {setting}::uuid END"


def tenant_rls_predicate(column: str = "tenant_id") -> str:
    """SQL predicate the tenant policy uses for both USING and WITH CHECK.

    It has no bypass branch: nothing the application role can set makes a row
    of a tenant other than ``sandloom.tenant_id`` visible or writable.
    """
    col = _quote(column, "column")
    return f"({col} = ({tenant_context_expression()}))"


def enable_tenant_rls_statements(
    table: str,
    *,
    column: str = "tenant_id",
    schema: str | None = None,
    system_access: bool = False,
    system_role: str = DEFAULT_SYSTEM_ROLE,
) -> list[str]:
    """SQL statements enabling + forcing tenant RLS on ``table`` (idempotent).

    With ``system_access=True`` the dedicated ``system_role`` (which must
    exist) gets an explicit cross-tenant policy and the DML grants on this
    table only; without it any such policy is dropped (least privilege).
    """
    target = _qualified(table, schema)
    policy = _quote(tenant_rls_policy_name(table), "policy")
    sys_policy = _quote(system_rls_policy_name(table), "policy")
    predicate = tenant_rls_predicate(column)
    statements = [
        f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {policy} ON {target}",
        f"CREATE POLICY {policy} ON {target} AS PERMISSIVE FOR ALL "
        f"USING {predicate} WITH CHECK {predicate}",
        f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
    ]
    if system_access:
        role = _quote(system_role, "role")
        statements += [
            f"CREATE POLICY {sys_policy} ON {target} AS PERMISSIVE FOR ALL "
            f"TO {role} USING (true) WITH CHECK (true)",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO {role}",
        ]
    return statements


def disable_tenant_rls_statements(
    table: str, *, schema: str | None = None
) -> list[str]:
    """SQL statements reverting :func:`enable_tenant_rls_statements`."""
    target = _qualified(table, schema)
    policy = _quote(tenant_rls_policy_name(table), "policy")
    sys_policy = _quote(system_rls_policy_name(table), "policy")
    return [
        f"DROP POLICY IF EXISTS {sys_policy} ON {target}",
        f"DROP POLICY IF EXISTS {policy} ON {target}",
        f"ALTER TABLE {target} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {target} DISABLE ROW LEVEL SECURITY",
    ]


def enable_tenant_rls(
    op: _Executes,
    table: str,
    *,
    column: str = "tenant_id",
    schema: str | None = None,
    system_access: bool = False,
    system_role: str = DEFAULT_SYSTEM_ROLE,
) -> None:
    """Enable + FORCE PostgreSQL RLS with the Sandloom tenant policy on ``table``.

    ``op`` is the Alembic ``op`` module (or any object with ``execute(str)``).
    ``column`` must be the table's ``uuid`` tenant column (``TenantScoped``
    declares ``tenant_id``). ``system_access=True`` lets the system-channel
    role (``system_role``, default ``sandloom_system``; it must already exist) read
    and write every tenant's rows of THIS table. Re-running is safe (policies
    are replaced).
    """
    for sql in enable_tenant_rls_statements(
        table,
        column=column,
        schema=schema,
        system_access=system_access,
        system_role=system_role,
    ):
        op.execute(sql)


def disable_tenant_rls(op: _Executes, table: str, *, schema: str | None = None) -> None:
    """Drop the Sandloom tenant policies and disable RLS on
    ``table`` (for downgrade).
    """
    for sql in disable_tenant_rls_statements(table, schema=schema):
        op.execute(sql)


__all__ = [
    "DEFAULT_SYSTEM_ROLE",
    "TENANT_SETTING",
    "disable_tenant_rls",
    "disable_tenant_rls_statements",
    "enable_tenant_rls",
    "enable_tenant_rls_statements",
    "system_rls_policy_name",
    "tenant_context_expression",
    "tenant_rls_policy_name",
    "tenant_rls_predicate",
]
