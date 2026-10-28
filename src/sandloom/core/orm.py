"""Public ORM surface for modules.

Expose only the SQLAlchemy Declarative Base type for model definitions, the
``TenantScoped`` mixin, the id default and timestamp columns (``new_id``,
``CreatedAt``, ``UpdatedAt``), and the PostgreSQL Row-Level Security migration helpers
(``enable_tenant_rls`` / ``disable_tenant_rls``, for
module Alembic migrations).
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
from sandloom.core.sql_alchemy.rls import (
    disable_tenant_rls,
    enable_tenant_rls,
)

# Re-exported here so a model file declares ``class Foo(TenantScoped, Base)``
# from one import; importing it also installs the automatic tenant guard.
from sandloom.core.tenant import TenantScoped


def session_reads(session: Any) -> Reads:
    """Run declared operations ON ``session`` (an open Unit of Work).

    Inside a ``DbSession`` route, ``db_session()`` or ``contract_session()``:
    the reads share the unit's transaction, connection and tenant context
    (read-your-own-writes; the general executor). Routes use
    ``sandloom.core.di.UowReads`` instead.
    """

    return _planner_adapter.session_reads(session)


__all__ = [
    "Base",
    "CreatedAt",
    "InvalidCursor",
    "JoinRead",
    "ListOperation",
    "MoneyLedger",
    "Page",
    "PageLimitError",
    "ReadOperation",
    "Reads",
    "TenantScoped",
    "UpdatedAt",
    "make_cursor",
    "new_id",
    "session_reads",
    "disable_tenant_rls",
    "enable_tenant_rls",
]
