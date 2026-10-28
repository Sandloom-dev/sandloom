"""Relational durable command-contract idempotency log.

The relational implementation behind ``DatabaseProvider.command_log()``: an
adapter of the durable dedupe record (ADR-0005,
:mod:`sandloom.core.sql_alchemy.dedupe_log`) onto the ``contract_command_log``
table — key = digest of (contract.method, tenant + scope arguments,
idempotency key), with args digest, status, result, the call effects the
execution left on its caller (re-applied by a replay) and a TTL. The contract
runtime (:func:`sandloom.core.module_api.run_contract_call`) claims a command's
key here IN the command's own Unit of Work, runs the body, stores the encoded
result on the same row and commits — the record and the business change are
one transaction. See :mod:`sandloom.core.sql_alchemy.models.contract_command`.

Claim protocol (one statement on the happy path)::

    INSERT INTO contract_command_log (...) VALUES (...)
    ON CONFLICT (dedupe_key) DO UPDATE SET ... -- take over an EXPIRED row only
        WHERE contract_command_log.expires_at <= :now
    RETURNING dedupe_key

* a row comes back -> claimed (new key, or an expired record taken over);
* no row -> a live record exists: read it (now locked by the statement) and
  replay it (same args digest) or report a mismatch;
* the TTL never lets a duplicate take over a RUNNING command: its claim is
  uncommitted, so the conflict below applies whatever its ``expires_at``;
  completion restarts the TTL (``expires_at`` += runtime) so the committed
  record is live even when the command outlived the TTL;
* a concurrent IN-FLIGHT claim of the same key makes PostgreSQL wait for that
  transaction (unique-index speculative insertion): it commits -> we replay;
  it rolls back -> we claim and run. The wait is bounded by the connection's
  ``lock_timeout`` (``CONTRACT_DB_LOCK_TIMEOUT_MS`` on the contract pool);
  exceeding it raises :class:`~sandloom.core.infra.contracts.CommandLogBusy`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import Table

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.effects import PENDING, DedupeBusy, DedupeRecordSpec
from sandloom.core.infra.contracts import CommandClaim, CommandLogBusy, CommandLogStore
from sandloom.core.sql_alchemy.dedupe_log import RelationalDedupeLog
from sandloom.core.sql_alchemy.models.contract_command import (
    ContractCommandRecord,
    _utc_naive_now,
)

COMMAND_RECORD = DedupeRecordSpec(
    table="contract_command_log",
    conflict=("dedupe_key",),
    columns=(
        "dedupe_key",
        "contract",
        "tenant_id",
        "args_digest",
        "status",
        "result",
        "effects",
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
    effects="effects",
    busy_on_lock_timeout=True,
)

#: The command log's durable dedupe log (one per process).
COMMAND_LOG = RelationalDedupeLog(
    COMMAND_RECORD, lambda: cast(Table, ContractCommandRecord.__table__)
)
#: Statements of the per-command hot path, built once (every value a bind
#: parameter). The claim's ``excluded`` alias in particular copied every
#: column of the table on each call (and left reference cycles to the GC).
_upserts: dict[str, Any] = COMMAND_LOG.claim_statements
_completions: dict[bool, Any] = COMMAND_LOG.completion_statements


def __getattr__(name: str) -> Any:
    # ``_replay_lookup``: the prebuilt replay SELECT (``None`` until the
    # first replay builds it) — kept readable under its historical name.
    if name == "_replay_lookup":
        return COMMAND_LOG.prebuilt_replay_statement()
    raise AttributeError(name)


class SQLAlchemyCommandLogStore(CommandLogStore):
    """Command idempotency log over the ``contract_command_log`` table."""

    async def claim(
        self,
        session: Any,
        *,
        dedupe_key: str,
        contract: str,
        tenant: str | None,
        args_digest: str,
        ttl_seconds: int,
    ) -> CommandClaim:
        now = _utc_naive_now()
        try:
            claim = await COMMAND_LOG.claim(
                session,
                {
                    "dedupe_key": dedupe_key,
                    "contract": contract,
                    "tenant_id": tenant,
                    "args_digest": args_digest,
                    "status": PENDING,
                    "result": None,
                    "effects": None,
                    "created_at": now,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                },
                now=now,
            )
        except DedupeBusy as exc:
            raise CommandLogBusy(dedupe_key) from exc
        if claim.outcome == "claimed":
            return CommandClaim("claimed")
        return CommandClaim(
            claim.outcome,
            status=claim.status,
            result=claim.result,
            effects=claim.effects,
        )

    async def complete(
        self,
        session: Any,
        *,
        dedupe_key: str,
        status: str,
        result: str | None,
        effects: str | None = None,
    ) -> None:
        await COMMAND_LOG.complete(
            session,
            {"dedupe_key": dedupe_key},
            status=status,
            result=result,
            now=_utc_naive_now(),
            effects=effects,
        )

    async def amend_effects(
        self, uow_factory: Any, *, dedupe_key: str, effects: str | None
    ) -> None:
        unit: UnitOfWorkContract = uow_factory()
        async with unit as uow:
            await COMMAND_LOG.amend_effects(
                uow.session, {"dedupe_key": dedupe_key}, effects
            )

    async def purge_expired(self, uow_factory: Any, *, limit: int) -> int:
        """``DELETE`` up to ``limit`` expired rows (bounded batch).

        The expiry is re-checked on the outer ``DELETE`` (see
        :meth:`RelationalDedupeLog.purge`): a key a concurrent claim is taking
        over is never deleted once that claim commits.
        """
        if limit <= 0:
            return 0
        unit: UnitOfWorkContract = uow_factory()
        async with unit as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            return await COMMAND_LOG.purge(
                uow.session, _utc_naive_now(), limit=limit, inclusive=True
            )


_STORE = SQLAlchemyCommandLogStore()


def sql_alchemy_command_log_store() -> SQLAlchemyCommandLogStore:
    """The (stateless) process-wide relational command log."""
    return _STORE


__all__ = [
    "COMMAND_LOG",
    "COMMAND_RECORD",
    "SQLAlchemyCommandLogStore",
    "sql_alchemy_command_log_store",
]
