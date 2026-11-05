"""Public ORM surface for modules.

Expose only the SQLAlchemy Declarative Base type for model definitions, the
``TenantScoped`` mixin, the id default and timestamp columns (``new_id``,
``CreatedAt``, ``UpdatedAt``), the portable column types, indexes and
statements (``sandloom.core.sql_alchemy.portable``), and the PostgreSQL
Row-Level Security migration helpers (``enable_tenant_rls`` /
``disable_tenant_rls``, for module Alembic migrations).
Internals (engine, sessions, etc.) remain hidden behind DI/facade.
"""

from typing import Any

import sandloom.core.sql_alchemy.planner_adapter as _planner_adapter

# Execution planner: declared reads with DTO results.
from sandloom.core.planner import (
    InvalidCursor,
    JoinRead,
    ListOperation,
    Page,
    PageLimitError,
    ReadOperation,
    Reads,
    make_cursor,
)
from sandloom.core.sql_alchemy.base import Base
from sandloom.core.sql_alchemy.columns import CreatedAt, UpdatedAt, new_id
from sandloom.core.sql_alchemy.money_ledger import MoneyLedger
from sandloom.core.sql_alchemy.portable import (
    Array,
    CaseInsensitiveText,
    ConflictInsert,
    FeatureUse,
    JsonPathKey,
    UtcDateTime,
    WallClock,
    conflict_insert,
    fulltext_index,
    json_contains,
    json_path,
    matches,
    partial_index,
    search_index,
    table_feature_uses,
    transaction_lock,
)
from sandloom.core.sql_alchemy.rls import (
    disable_tenant_rls,
    enable_tenant_rls,
)

# Re-exported here so a model file declares ``class Foo(TenantScoped, Base)``
# from one import; importing it also installs the automatic tenant guard.
from sandloom.core.tenant import TenantScoped


def module_feature_uses(package: str) -> list[FeatureUse]:
    """The feature forms the tables of ``package``'s mapped models are built
    with (``Array`` columns, ``fulltext_index`` indexes), each once; see
    :func:`~sandloom.core.sql_alchemy.portable.table_feature_uses`."""
    tables: dict[str, Any] = {}
    for mapper in Base.registry.mappers:
        owner = getattr(mapper.class_, "__module__", "") or ""
        if owner == package or owner.startswith(f"{package}."):
            for table in mapper.tables:
                tables.setdefault(str(table.fullname), table)
    return [use for name in sorted(tables) for use in table_feature_uses(tables[name])]


def session_reads(session: Any) -> Reads:
    """Run declared operations ON ``session`` (an open Unit of Work).

    Inside a ``DbSession`` route, ``db_session()`` or ``contract_session()``:
    the reads share the unit's transaction, connection and tenant context
    (read-your-own-writes; the general executor). Routes use
    ``sandloom.core.di.UowReads`` instead.
    """

    return _planner_adapter.session_reads(session)


__all__ = [
    "Array",
    "Base",
    "CaseInsensitiveText",
    "ConflictInsert",
    "CreatedAt",
    "InvalidCursor",
    "JsonPathKey",
    "JoinRead",
    "ListOperation",
    "MoneyLedger",
    "Page",
    "PageLimitError",
    "ReadOperation",
    "Reads",
    "TenantScoped",
    "UpdatedAt",
    "UtcDateTime",
    "WallClock",
    "conflict_insert",
    "disable_tenant_rls",
    "enable_tenant_rls",
    "fulltext_index",
    "json_contains",
    "json_path",
    "make_cursor",
    "matches",
    "new_id",
    "partial_index",
    "search_index",
    "session_reads",
    "transaction_lock",
]
