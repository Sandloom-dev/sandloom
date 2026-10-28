"""Statement helpers shared by the relational outbox store and its relay half.

The relay's claim lease and retry backoff, the PostgreSQL ``id = ANY(:ids)``
form, the system Unit of Work the outbox's own statements run in (no tenant
round trip) and the naive-UTC conversion of the stored timestamps.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import any_, bindparam
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.tenant_rls import non_tenant_statements

# How long a relay worker's claim on a row stays exclusive while it publishes.
# It must comfortably exceed the relay's publish timeout (so a normal publish
# settles well within the lease) yet be short enough that a CRASHED worker's
# claim is reclaimable promptly. The pending query treats a row whose
# locked_until is still in the future as in-flight and skips it.
_CLAIM_LEASE_SECONDS = 30.0
# Default retry backoff (used when the relay passes no policy): exponential in
# the attempt number, capped, with jitter so a burst of failures does not retry
# in lock-step.
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 300.0
# A broker-unavailable deferral is short and NOT counted as an attempt: the row
# is only parked briefly so the relay does not spin on it while the broker is
# down; it becomes due again soon after the broker is back.
_UNAVAILABLE_DEFER_SECONDS = 5.0


#: ``(ok, failures, unavailable, released)`` of one publish pass of a batch.
#: (delivered ids, failures (id, attempts before, error), broker-parked row,
#: released ids, held rows (id, blocking row id)).
_PublishOutcome = tuple[
    list[uuid.UUID],
    list[tuple[uuid.UUID, int, str]],
    tuple[uuid.UUID, str] | None,
    list[uuid.UUID],
    list[tuple[uuid.UUID, uuid.UUID]],
]


def _hold_or_release(
    rows: Sequence[Any], blocked: dict[str, uuid.UUID]
) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, uuid.UUID]]]:
    """Split unpublished ``rows``: held behind a blocked channel, or released."""
    released: list[uuid.UUID] = []
    held: list[tuple[uuid.UUID, uuid.UUID]] = []
    for row in rows:
        blocker = blocked.get(row.channel)
        if blocker is None:
            released.append(row.id)
        else:
            held.append((row.id, blocker))
    return released, held


def default_backoff(attempts: int) -> float:
    """Seconds to wait before retrying after the ``attempts``-th failure.

    ``base * 2**(attempts-1)`` capped at the max, then "equal jitter" (half
    fixed, half random) so retries spread out while still growing.
    """
    exp = max(0, attempts - 1)
    delay = min(
        _DEFAULT_BACKOFF_MAX_SECONDS, _DEFAULT_BACKOFF_BASE_SECONDS * (2.0**exp)
    )
    return delay / 2 + random.random() * delay / 2  # noqa: S311 - backoff jitter, not a secret


def _is_postgres(session: Any) -> bool:
    try:
        return bool(session.get_bind().dialect.name == "postgresql")
    except Exception:  # noqa: BLE001 - unknown bind: use the portable form
        return False


def _any_id(column_owner: Any, ids: Sequence[uuid.UUID]) -> Any:
    """``id = ANY(:ids)`` (PostgreSQL) — ONE array bind parameter, so the
    statement text (and asyncpg's prepared statement) is identical for every
    batch size, unlike an expanding ``IN (...)``."""

    column = column_owner.c.id if hasattr(column_owner, "c") else column_owner.id
    return column == any_(
        bindparam("outbox_ids", value=list(ids), type_=ARRAY(PG_UUID(as_uuid=True)))
    )


def _id_in(session: Any, ids: Sequence[uuid.UUID]) -> Any:
    """``id = ANY(:ids)`` on PostgreSQL, an expanding ``IN (...)`` elsewhere."""
    if _is_postgres(session):
        return _any_id(OutboxMessage, ids)
    return OutboxMessage.id.in_(list(ids))


@asynccontextmanager
async def _system_uow(
    uow_factory: Callable[[], UnitOfWorkContract],
) -> AsyncIterator[UnitOfWorkContract]:
    """A UoW for the outbox's system statements (mark-delivered, the relay's
    claim / settle / sweep).

    They touch ONLY ``outbox_messages`` (not tenant-scoped), so on a
    ``tenant_rls`` database the transaction skips the tenant-setting round trip
    when no tenant is bound (:func:`non_tenant_statements`, an explicit
    allowlist entry). Nothing else may run inside it.
    """
    with non_tenant_statements():
        async with uow_factory() as uow:
            yield uow


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
