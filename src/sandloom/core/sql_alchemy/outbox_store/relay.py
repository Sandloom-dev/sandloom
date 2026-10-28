"""The relay half of :class:`SQLAlchemyOutboxStore`.

Two-phase delivery of committed outbox rows by the outbox relay: claim a batch
under a lease (``FOR UPDATE SKIP LOCKED``), publish OUTSIDE the transaction,
then settle each row — delivered, deferred while the broker is unavailable
(no attempt burnt), or retried with backoff until it is dead-lettered.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select, update

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.eventing.origin import event_origins
from sandloom.core.infra.contracts import (
    BrokerUnavailableError,
    DeliveryStatus,
    OutboxStore,
    PublishMany,
    RelayBatchResult,
)
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage, _utc_naive_now
from sandloom.core.sql_alchemy.outbox_store.statements import (
    _CLAIM_LEASE_SECONDS,
    _UNAVAILABLE_DEFER_SECONDS,
    _any_id,
    _hold_or_release,
    _is_postgres,
    _PublishOutcome,
    _system_uow,
    default_backoff,
)


class _RelayMixin(OutboxStore):
    """The relay half of :class:`SQLAlchemyOutboxStore` (see the module doc)."""

    async def deliver_pending(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
        publish: Callable[[str, dict[str, Any], str, str], Awaitable[None]],
        *,
        max_attempts: int,
        backoff: Callable[[int], float] | None = None,
    ) -> DeliveryStatus:
        """Deliver one message via a two-phase claim so broker I/O holds no lock.

        Phase 1 (claim) leases the row in a short transaction and COMMITS, so the
        publish runs with NO DB row lock and NO open transaction held against the
        broker's latency. Phase 2 (settle) records the outcome in a second short
        transaction. Worst case a worker crashes between publish and settle: the
        lease expires, another worker reclaims and republishes — a duplicate the
        stable envelope id lets consumers dedupe (at-least-once delivery).

        A :class:`BrokerUnavailableError` from ``publish`` is NOT an attempt: the
        event is fine, the broker is not. The row is released (briefly parked)
        with its budget intact and ``"unavailable"`` tells the relay to stop the
        batch instead of burning every pending row's budget on the outage.
        """
        claim = await self._claim(uow_factory, message_id)
        if claim is None:
            return "skipped"
        channel, payload, schema_version, msg_id_str, token, trace_id, emitted_at = (
            claim
        )
        # Publish OUTSIDE any transaction — no row lock is held here.
        try:
            with event_origins({msg_id_str: (trace_id, emitted_at)}):
                await publish(channel, payload, schema_version, msg_id_str)
        except BrokerUnavailableError as exc:
            return await self._settle_unavailable(
                uow_factory, message_id, token, str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - record + maybe dead-letter
            return await self._settle_failure(
                uow_factory,
                message_id,
                token,
                str(exc),
                max_attempts,
                backoff or default_backoff,
            )
        return await self._settle_success(uow_factory, message_id, token)

    async def _claim(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
    ) -> tuple[str, dict[str, Any], str, str, uuid.UUID, str | None, datetime] | None:
        """Lease the row and COMMIT; return its publish fields + ownership token.

        Mints a fresh ``locked_by`` token so settle can prove the lease is still
        ours. Captures channel/payload/version/id INSIDE the transaction because
        the ORM instance expires once the lease transaction commits.
        """

        now = _utc_naive_now()
        lease_until = now + timedelta(seconds=_CLAIM_LEASE_SECONDS)
        token = uuid.uuid4()
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            locked = await uow.session.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                    or_(
                        OutboxMessage.locked_until.is_(None),
                        OutboxMessage.locked_until < now,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            msg = locked.scalars().first()
            if msg is None:
                return None
            msg.locked_until = lease_until
            msg.locked_by = token
            return (
                msg.channel,
                dict(msg.payload),
                msg.schema_version,
                str(msg.id),
                token,
                msg.trace_id,
                msg.emitted_at,
            )

    @staticmethod
    async def _locked_owned(
        session: Any, message_id: uuid.UUID, token: uuid.UUID
    ) -> OutboxMessage | None:
        """Row-lock the message; return it only if still unsettled AND ours."""

        locked = await session.execute(
            select(OutboxMessage)
            .where(OutboxMessage.id == message_id)
            .with_for_update()
        )
        msg: OutboxMessage | None = locked.scalars().first()
        if msg is None or msg.delivered_at is not None or msg.dead_at is not None:
            # Already settled by another worker — nothing to do.
            return None
        if msg.locked_by != token:
            # Our lease expired and another worker reclaimed the row; it owns
            # delivery now and we must not stomp its claim or accounting.
            return None
        return msg

    async def _settle_success(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
        token: uuid.UUID,
    ) -> Literal["delivered", "skipped"]:
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            msg = await self._locked_owned(uow.session, message_id, token)
            if msg is None:
                # Settled elsewhere / lease lost: we published (a duplicate the
                # consumer dedupes) but the new owner keeps the row.
                return "skipped"
            msg.mark_delivered()
            msg.locked_until = None
            msg.locked_by = None
            msg.next_attempt_at = None
            return "delivered"

    async def _settle_unavailable(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
        token: uuid.UUID,
        error: str,
    ) -> Literal["unavailable", "skipped"]:
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            msg = await self._locked_owned(uow.session, message_id, token)
            if msg is None:
                return "skipped"
            # No attempt counted: the event is not poison, the broker is down.
            msg.last_error = f"broker unavailable: {error}"
            msg.locked_until = None
            msg.locked_by = None
            msg.next_attempt_at = _utc_naive_now() + timedelta(
                seconds=_UNAVAILABLE_DEFER_SECONDS
            )
            return "unavailable"

    async def _settle_failure(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        message_id: uuid.UUID,
        token: uuid.UUID,
        error: str,
        max_attempts: int,
        backoff: Callable[[int], float] = default_backoff,
    ) -> Literal["failed", "dead", "skipped"]:
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            msg = await self._locked_owned(uow.session, message_id, token)
            if msg is None:
                return "skipped"
            msg.mark_attempt(error=error)
            # Release the lease; the retry is deferred by the backoff policy so
            # a failing row is not re-hammered every relay tick.
            msg.locked_until = None
            msg.locked_by = None
            if msg.delivery_attempts >= max_attempts:
                msg.mark_dead(error=error)
                msg.next_attempt_at = None
                return "dead"
            delay = max(0.0, float(backoff(msg.delivery_attempts)))
            msg.next_attempt_at = _utc_naive_now() + timedelta(seconds=delay)
            return "failed"

    # -- batched relay (PostgreSQL) ----------------------------------------------
    async def deliver_batch(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        publish: Callable[[str, dict[str, Any], str, str], Awaitable[None]],
        *,
        max_attempts: int,
        grace_seconds: float,
        limit: int,
        backoff: Callable[[int], float] | None = None,
        publish_many: PublishMany | None = None,
    ) -> RelayBatchResult:
        """Claim a whole batch in ONE statement, settle it in ONE transaction.

        1. **Claim** (own short transaction): ``WITH due AS (SELECT … ORDER BY
           emitted_at LIMIT :n FOR UPDATE SKIP LOCKED) UPDATE … SET
           locked_until = :lease, locked_by = :token FROM due RETURNING …`` —
           concurrent relays partition the backlog (SKIP LOCKED) and every
           claimed row carries THIS batch's fresh ownership token.
        2. **Publish** outside any transaction, in ``emitted_at`` order —
           with ``publish_many`` the claimed rows go out in ONE transport
           round trip (Redis Streams ``MULTI``/``EXEC``). The batch is
           all-or-nothing for the settle: success marks every row delivered;
           :class:`BrokerUnavailableError` parks the FIRST row (no attempt
           counted) and releases the rest, exactly as a per-row outage at the
           first row; any other failure cannot be pinned on one row, so the
           batch is re-published row by row with ``publish`` (attempts,
           backoff and dead-lettering attributed per row as before; rows the
           failed batch did append become duplicates — at-least-once). A row
           that failed (or hit the outage) HOLDS the rows behind it on its
           channel: they are not published and become due together with it,
           so a later event never overtakes an earlier one parked in backoff
           (a row dead-lettered by the failure holds nothing).
        3. **Settle** (one transaction): successes in ONE ``UPDATE … WHERE id =
           ANY(:ids) AND locked_by = :token``; failures per row (attempt +
           backoff / dead-letter, as before); a broker outage parks the failing
           row without counting an attempt and releases the unpublished rest.

        Every settle statement is guarded by the token, so a batch whose lease
        expired (and was reclaimed by another relay) never touches the new
        owner's rows: those count as ``skipped`` (the duplicate publish is
        deduped by consumers — at-least-once). Publishing stops early (rest
        released) once half the lease is used, so a slow broker cannot let a
        lease lapse mid-batch. Non-PostgreSQL backends use the per-row default.
        """
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            postgres = _is_postgres(uow.session)
            if postgres:
                claimed = await self._claim_batch(
                    uow.session, max_attempts, grace_seconds, limit
                )
        if not postgres:
            return await super().deliver_batch(
                uow_factory,
                publish,
                max_attempts=max_attempts,
                grace_seconds=grace_seconds,
                limit=limit,
                backoff=backoff,
            )
        token, rows = claimed
        if not rows:
            return RelayBatchResult()
        started = time.monotonic()
        outcome = None
        if publish_many is not None and len(rows) > 1:
            outcome = await self._publish_batch(rows, publish_many)
        if outcome is None:
            outcome = await self._publish_rows(
                rows, publish, started, max_attempts=max_attempts
            )
        ok, failures, unavailable, released, held = outcome

        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            return await self._settle_batch(
                uow.session,
                token,
                claimed=len(rows),
                full=len(rows) >= limit,
                ok=ok,
                failures=failures,
                unavailable=unavailable,
                released=released,
                held=held,
                max_attempts=max_attempts,
                backoff=backoff or default_backoff,
            )

    @staticmethod
    async def _publish_rows(
        rows: Sequence[Any],
        publish: Callable[[str, dict[str, Any], str, str], Awaitable[None]],
        started: float,
        *,
        max_attempts: int | None = None,
    ) -> _PublishOutcome:
        """Publish claimed rows one at a time (per-row outcome attribution).

        A failed row blocks its CHANNEL for the rest of the batch: the rows
        behind it on that channel are not published but HELD (settled due
        with the blocking row, see :meth:`_settle_batch`), so a later event
        never reaches the stream before an earlier one parked in backoff —
        what the post-commit dispatcher guarantees by stopping at a UoW's
        first failure. Other channels carry on, and a failure that exhausts
        its row's budget (dead-lettered: it will never be delivered) blocks
        nothing.
        """
        ok: list[uuid.UUID] = []
        failures: list[tuple[uuid.UUID, int, str]] = []
        unavailable: tuple[uuid.UUID, str] | None = None
        released: list[uuid.UUID] = []
        held: list[tuple[uuid.UUID, uuid.UUID]] = []
        blocked: dict[str, uuid.UUID] = {}
        for index, row in enumerate(rows):
            if time.monotonic() - started > _CLAIM_LEASE_SECONDS / 2:
                rest_released, rest_held = _hold_or_release(rows[index:], blocked)
                released.extend(rest_released)
                held.extend(rest_held)
                break
            blocker = blocked.get(row.channel)
            if blocker is not None:
                held.append((row.id, blocker))
                continue
            try:
                with event_origins(
                    {str(row.id): (getattr(row, "trace_id", None), row.emitted_at)}
                ):
                    await publish(
                        row.channel, dict(row.payload), row.schema_version, str(row.id)
                    )
            except BrokerUnavailableError as exc:
                unavailable = (row.id, str(exc))
                blocked[row.channel] = row.id
                rest_released, rest_held = _hold_or_release(rows[index + 1 :], blocked)
                released.extend(rest_released)
                held.extend(rest_held)
                break
            except Exception as exc:  # noqa: BLE001 - record + maybe dead-letter
                failures.append((row.id, int(row.delivery_attempts), str(exc)))
                if max_attempts is None or row.delivery_attempts + 1 < max_attempts:
                    blocked[row.channel] = row.id
                continue
            ok.append(row.id)
        return ok, failures, unavailable, released, held

    @staticmethod
    async def _publish_batch(
        rows: Sequence[Any], publish_many: PublishMany
    ) -> _PublishOutcome | None:
        """Publish ALL claimed rows in one round trip; ``None`` = go per row.

        Success delivers every row. An outage parks the first row and releases
        the rest (no attempt counted, the circuit trips) — what the per-row
        loop does when its first publish hits the outage, including holding
        the rest of the first row's channel behind it. Any other failure
        returns ``None``: only a per-row retry can attribute it to one row.
        """
        try:
            with event_origins(
                {str(r.id): (getattr(r, "trace_id", None), r.emitted_at) for r in rows}
            ):
                await publish_many(
                    [
                        (r.channel, dict(r.payload), r.schema_version, str(r.id))
                        for r in rows
                    ]
                )
        except BrokerUnavailableError as exc:
            released, held = _hold_or_release(rows[1:], {rows[0].channel: rows[0].id})
            return [], [], (rows[0].id, str(exc)), released, held
        except Exception:  # noqa: BLE001 - attribute it per row
            return None
        return [r.id for r in rows], [], None, [], []

    @staticmethod
    async def _claim_batch(
        session: Any, max_attempts: int, grace_seconds: float, limit: int
    ) -> tuple[uuid.UUID, list[Any]]:
        table: Any = OutboxMessage.__table__
        now = _utc_naive_now()
        token = uuid.uuid4()
        due = (
            select(table.c.id)
            .where(
                table.c.delivered_at.is_(None),
                table.c.dead_at.is_(None),
                table.c.delivery_attempts < max_attempts,
                table.c.emitted_at < now - timedelta(seconds=grace_seconds),
                or_(table.c.next_attempt_at.is_(None), table.c.next_attempt_at <= now),
                or_(table.c.locked_until.is_(None), table.c.locked_until < now),
            )
            .order_by(table.c.emitted_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("due")
        )
        result = await session.execute(
            update(table)
            .where(table.c.id == due.c.id)
            .values(
                locked_until=now + timedelta(seconds=_CLAIM_LEASE_SECONDS),
                locked_by=token,
            )
            .returning(
                table.c.id,
                table.c.channel,
                table.c.payload,
                table.c.schema_version,
                table.c.emitted_at,
                table.c.trace_id,
                table.c.delivery_attempts,
            )
        )
        rows = sorted(result.all(), key=lambda r: (r.emitted_at, str(r.id)))
        return token, rows

    @staticmethod
    async def _settle_batch(
        session: Any,
        token: uuid.UUID,
        *,
        claimed: int,
        full: bool,
        ok: list[uuid.UUID],
        failures: list[tuple[uuid.UUID, int, str]],
        unavailable: tuple[uuid.UUID, str] | None,
        released: list[uuid.UUID],
        max_attempts: int,
        backoff: Callable[[int], float],
        held: list[tuple[uuid.UUID, uuid.UUID]] | None = None,
    ) -> RelayBatchResult:
        table: Any = OutboxMessage.__table__
        now = _utc_naive_now()

        def owned(*criteria: Any) -> Any:
            return update(table).where(
                *criteria,
                table.c.locked_by == token,
                table.c.delivered_at.is_(None),
                table.c.dead_at.is_(None),
            )

        delivered = 0
        if ok:
            result = await session.execute(
                owned(_any_id(table, ok)).values(
                    delivered_at=now,
                    last_error=None,
                    locked_until=None,
                    locked_by=None,
                    next_attempt_at=None,
                )
            )
            delivered = int(result.rowcount or 0)
        failed = dead = 0
        dead_ids: list[uuid.UUID] = []
        # blocking row -> when it is due again (None: dead / not ours, so
        # whatever it held is simply released)
        due_again: dict[uuid.UUID, datetime | None] = {}
        for message_id, attempts_before, error in failures:
            attempts = attempts_before + 1
            is_dead = attempts >= max_attempts
            delay = 0.0 if is_dead else max(0.0, float(backoff(attempts)))
            next_at = None if is_dead else now + timedelta(seconds=delay)
            result = await session.execute(
                owned(table.c.id == message_id).values(
                    delivery_attempts=table.c.delivery_attempts + 1,
                    last_error=error,
                    locked_until=None,
                    locked_by=None,
                    dead_at=now if is_dead else None,
                    next_attempt_at=next_at,
                )
            )
            due_again[message_id] = next_at if result.rowcount else None
            if result.rowcount:
                failed += 1
                if is_dead:
                    dead += 1
                    dead_ids.append(message_id)
        if unavailable is not None:
            message_id, error = unavailable
            # No attempt counted: the event is not poison, the broker is down.
            parked_until = now + timedelta(seconds=_UNAVAILABLE_DEFER_SECONDS)
            result = await session.execute(
                owned(table.c.id == message_id).values(
                    last_error=f"broker unavailable: {error}",
                    locked_until=None,
                    locked_by=None,
                    next_attempt_at=parked_until,
                )
            )
            due_again[message_id] = parked_until if result.rowcount else None
        for row_id, blocker in held or ():
            hold_until = due_again.get(blocker)
            if hold_until is None:
                released.append(row_id)
                continue
            # Unpublished, no attempt counted: due again together with the row
            # that blocks its channel. The claim orders by emitted_at, so the
            # blocking (earlier) row is published first.
            await session.execute(
                owned(table.c.id == row_id).values(
                    locked_until=None, locked_by=None, next_attempt_at=hold_until
                )
            )
        if released:
            # Unpublished: hand the rows straight back (no attempt, no delay).
            await session.execute(
                owned(_any_id(table, released)).values(
                    locked_until=None, locked_by=None
                )
            )
        return RelayBatchResult(
            claimed=claimed,
            delivered=delivered,
            failed=failed,
            dead=dead,
            skipped=claimed - delivered - failed,
            unavailable=unavailable is not None,
            full=full,
            dead_ids=tuple(dead_ids),
        )
