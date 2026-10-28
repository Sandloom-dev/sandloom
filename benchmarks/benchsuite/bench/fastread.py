"""S5 secure read fast paths (PROTOTYPES, benchmark-only — not framework code).

Both bypass the Session/UoW and talk to the asyncpg connection underneath
Sandloom's own pool, yet keep BOTH tenant barriers:

* an explicit ``tenant_id = $n`` predicate (the ORM guard's twin), and
* PostgreSQL RLS: the transaction-local ``sandloom.tenant_id`` GUC is set by the
  same mechanism the policy reads, in the SAME transaction as the read.

``fetch_tx`` (3 round trips): ``BEGIN READ ONLY; SELECT set_config(...)`` in
ONE simple-protocol message, the SELECT, ``COMMIT``. The tenant id is inlined
as a literal only after ``uuid.UUID`` canonicalisation (cannot carry SQL);
``READ ONLY`` makes any write on this path an error.

``fetch_fn`` (1 round trip, autocommit): ``SELECT ... FROM bench_s5_get($1,$2)``
— a plpgsql function that runs ``set_config(..., true)`` as its own statement
BEFORE its query (sequential; no evaluation-order games) and whose GUC dies with
the implicit single-statement transaction.

NOT used, on purpose: a CTE ``WITH cfg AS (SELECT set_config(...)) SELECT ...``
in one statement. The planner may scan the table BEFORE the CTE, so the policy
reads the PREVIOUS value of the GUC: empty in autocommit (wrong 404 — fail
closed) but a stale tenant inside an open transaction (fail OPEN). The S5 tests
pin both outcomes.

Connection safety: a raw ``BEGIN`` is invisible to SQLAlchemy's adapter, whose
reset-on-return ROLLBACK is then a no-op. So any error / cancellation / open
transaction INVALIDATES the connection (closing it kills the transaction and
its GUC) instead of returning it to the pool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sandloom.core.sql_alchemy.rls import TENANT_SETTING

#: Read with an explicit tenant predicate (the RLS policy applies on top).
SQL_BY_ID = (
    "SELECT id, name, value FROM bench_tenant_items WHERE id = $1 AND tenant_id = $2"
)
#: One-round-trip read through the plpgsql function (see ``setup_db``).
SQL_FN = "SELECT id, name, value FROM bench_s5_get($1, $2)"


class FastPathError(RuntimeError):
    """The fast path refused to run or left an unsafe connection state."""


def begin_sql(tenant: uuid.UUID) -> str:
    """``BEGIN READ ONLY`` + the RLS GUCs in ONE simple-protocol message."""
    if tenant is None:
        raise FastPathError("no tenant bound: the fast path fails closed")
    tid = str(uuid.UUID(str(tenant)))  # canonical form: hex digits and dashes only
    return (
        "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY; "
        f"SELECT set_config('{TENANT_SETTING}', '{tid}', true)"
    )


@asynccontextmanager
async def driver_connection(engine: Any) -> AsyncIterator[Any]:
    """The raw asyncpg connection of one pooled connection, returned safely."""
    async with engine.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        if raw.is_in_transaction():  # never build on someone else's transaction
            await conn.invalidate()
            raise FastPathError("pooled connection was handed out inside a transaction")
        try:
            yield raw
        except BaseException:
            # Error or cancellation: the raw transaction (and its GUC) may be
            # open, or a query may still be in flight. Never pool it.
            await conn.invalidate()
            raise
        if raw.is_in_transaction():
            await conn.invalidate()
            raise FastPathError("fast path left a transaction open")


async def fetch_tx(engine: Any, tenant: uuid.UUID, sql: str, *args: Any) -> Any:
    """BEGIN READ ONLY + set_config | SELECT | COMMIT — 3 round trips."""
    begin = begin_sql(tenant)
    async with driver_connection(engine) as raw:
        await raw.execute(begin)
        row = await raw.fetchrow(sql, *args)
        await raw.execute("COMMIT")
    return row


async def fetch_fn(engine: Any, tenant: uuid.UUID, item_id: int) -> Any:
    """One round trip in autocommit through ``bench_s5_get``."""
    if tenant is None:
        raise FastPathError("no tenant bound: the fast path fails closed")
    async with driver_connection(engine) as raw:
        return await raw.fetchrow(SQL_FN, tenant, item_id)


__all__ = [
    "SQL_BY_ID",
    "SQL_FN",
    "FastPathError",
    "begin_sql",
    "driver_connection",
    "fetch_fn",
    "fetch_tx",
]
