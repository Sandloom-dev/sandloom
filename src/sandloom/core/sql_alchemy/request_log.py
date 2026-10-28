"""Relational durable HTTP idempotency log (``idempotency_guard(durable=True)``).

The relational implementation behind ``DatabaseProvider.request_log()``: the
third adapter of the durable dedupe record (ADR-0005,
:mod:`sandloom.core.sql_alchemy.dedupe_log`), on the ``http_idempotency_log`` table
— key = digest of (route scope, caller, tenant, ``Idempotency-Key``), args
digest = the request fingerprint, status + stored response + TTL. The guard
claims the key IN the request's own Unit of Work before the endpoint runs and
stores the response on the same row right before ``COMMIT``
(:mod:`sandloom.core.idempotency`), so the record and the endpoint's effect are one
transaction. See :mod:`sandloom.core.sql_alchemy.models.http_idempotency`.

Claim protocol (two statements on the happy path)::

    SELECT pg_try_advisory_xact_lock(<namespace>, <key hash>)   -- never waits
    INSERT INTO http_idempotency_log (...) VALUES (...)
    ON CONFLICT (dedupe_key) DO UPDATE SET ...  -- take over an EXPIRED row only
        WHERE http_idempotency_log.expires_at <= :now
    RETURNING dedupe_key

* the advisory lock is transaction-scoped and taken WITHOUT waiting: a second
  request with the same key while the first is in flight (its claim
  uncommitted) is refused at once (:class:`RequestLogBusy` -> ``409``) instead
  of blocking a pooled connection on the first one's unique-index entry for
  as long as the first request runs. Holding the lock, the claim never waits:
  the only other writer of the key is a transaction that already ended;
* a row comes back -> claimed; no row -> the record is committed: replay it
  (same fingerprint) or report a mismatch (``422``);
* a hash collision of two different keys (32-bit lock key) can only refuse a
  request with ``409`` while the other is in flight — never replay or merge
  them (the record key is the full digest).

Lock namespace: the two-``int4`` advisory form (``objsubid = 2``) with
:data:`ADVISORY_NAMESPACE`, disjoint from the ``bigint`` form the migration
lock uses.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import Table, text

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.effects import PENDING, DedupeBusy, DedupeRecordSpec
from sandloom.core.infra.contracts import CommandClaim, RequestLogBusy, RequestLogStore
from sandloom.core.sql_alchemy.dedupe_log import RelationalDedupeLog
from sandloom.core.sql_alchemy.models.http_idempotency import (
    HttpIdempotencyRecord,
    _utc_naive_now,
)

#: ``classid`` of the guard's transaction-scoped advisory locks ("SANDLOOMH").
ADVISORY_NAMESPACE = 0x414A4148

REQUEST_RECORD = DedupeRecordSpec(
    table="http_idempotency_log",
    conflict=("dedupe_key",),
    columns=(
        "dedupe_key",
        "scope",
        "tenant_id",
        "args_digest",
        "status",
        "result",
        "created_at",
        "expires_at",
    ),
    returning="dedupe_key",
    purge_column="expires_at",
    expires_at="expires_at",
    created_at="created_at",
    args_digest="args_digest",
    status="status",
    result="result",
    busy_on_lock_timeout=True,
)

#: The HTTP idempotency log's durable dedupe log (one per process).
REQUEST_LOG = RelationalDedupeLog(
    REQUEST_RECORD, lambda: cast(Table, HttpIdempotencyRecord.__table__)
)

_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(:ns, :k)")


def advisory_key(dedupe_key: str) -> int:
    """Signed ``int4`` lock key from the record's hex digest."""
    value = int(dedupe_key[:8], 16)
    return value - (1 << 32) if value >= (1 << 31) else value


class SQLAlchemyRequestLogStore(RequestLogStore):
    """Durable HTTP idempotency log over ``http_idempotency_log``."""

    async def claim(
        self,
        session: Any,
        *,
        dedupe_key: str,
        scope: str,
        tenant: str | None,
        fingerprint: str,
        ttl_seconds: int,
    ) -> CommandClaim:
        if session.sync_session.get_bind().dialect.name == "postgresql":
            # autoflush off, like the claim: a flush of the caller's pending
            # objects here would surface the caller's own errors in the guard.
            with session.no_autoflush:
                locked = (
                    await session.execute(
                        _TRY_LOCK,
                        {"ns": ADVISORY_NAMESPACE, "k": advisory_key(dedupe_key)},
                    )
                ).scalar()
            if not locked:
                raise RequestLogBusy(dedupe_key)
        now = _utc_naive_now()
        try:
            claim = await REQUEST_LOG.claim(
                session,
                {
                    "dedupe_key": dedupe_key,
                    "scope": scope[:200],
                    "tenant_id": tenant,
                    "args_digest": fingerprint,
                    "status": PENDING,
                    "result": None,
                    "created_at": now,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                },
                now=now,
            )
        except DedupeBusy as exc:
            raise RequestLogBusy(dedupe_key) from exc
        if claim.outcome == "claimed":
            return CommandClaim("claimed")
        return CommandClaim(claim.outcome, status=claim.status, result=claim.result)

    async def complete(
        self, session: Any, *, dedupe_key: str, status: str, result: str | None
    ) -> None:
        await REQUEST_LOG.complete(
            session,
            {"dedupe_key": dedupe_key},
            status=status,
            result=result,
            now=_utc_naive_now(),
        )

    async def release(self, session: Any, *, dedupe_key: str) -> None:
        await REQUEST_LOG.release(session, {"dedupe_key": dedupe_key})

    async def purge_expired(self, uow_factory: Any, *, limit: int) -> int:
        """``DELETE`` up to ``limit`` expired rows (bounded batch, re-checked
        cutoff: a key a concurrent claim is taking over is never deleted)."""
        if limit <= 0:
            return 0
        unit: UnitOfWorkContract = uow_factory()
        async with unit as uow:
            if uow.session is None:  # pragma: no cover - an entered UoW has one
                raise RuntimeError("Unit of Work entered without a session")
            return await REQUEST_LOG.purge(
                uow.session, _utc_naive_now(), limit=limit, inclusive=True
            )


_STORE = SQLAlchemyRequestLogStore()


def sql_alchemy_request_log_store() -> SQLAlchemyRequestLogStore:
    """The (stateless) process-wide relational HTTP idempotency log."""
    return _STORE


__all__ = [
    "ADVISORY_NAMESPACE",
    "REQUEST_LOG",
    "REQUEST_RECORD",
    "SQLAlchemyRequestLogStore",
    "advisory_key",
    "sql_alchemy_request_log_store",
]
