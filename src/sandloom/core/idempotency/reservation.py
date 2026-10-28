"""The cache guard's reservation lifecycle: reserve, keep alive, settle.

:func:`reserve_key` reserves / replays / rejects one key (shared by the HTTP
guard and the RPC command dedupe). A reservation whose request is still
running is renewed by a keeper task (:func:`_keep_pending_alive`); the request
then settles it — :func:`_persist_terminal` writes the terminal ``succeeded``
record, :func:`_release_key` frees the key after a failed attempt. Every
transition is a compare-and-set on the reservation's own pending value.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from dataclasses import dataclass
from typing import Any, Literal

import orjson

from sandloom.core.idempotency.cache_store import (
    _pending_record,
    _store_add,
    _store_cas_delete,
    _store_cas_set,
    _store_reserve,
)
from sandloom.core.idempotency.fingerprint import (
    IdempotencyReservation,
    _CacheUnavailable,
    _parse_record,
)
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.idempotency")


# Bound on the reserve loop: each iteration either reserves (and arms), replays,
# rejects, or re-reads after losing the atomic SET NX race. A handful of
# iterations comfortably absorbs realistic contention; exhausting them means the
# key is churning pathologically (TTL flapping) and we refuse rather than arm a
# request WITHOUT a reservation (which would let a duplicate side effect run).
_MAX_RESERVE_ATTEMPTS = 5
# Terminal record persistence (succeeded marker / failure release) runs AFTER
# the response, off the request path, so a transient cache blip can be retried a
# few times before we give up — and a give-up is LOUD (logged + metric), never
# the old silent single-shot.
_TERMINAL_PERSIST_ATTEMPTS = 3

# Upper bound on how long a ``pending`` reservation may live. The terminal
# (succeeded/failed) record keeps the full ``ttl_seconds``, but an in-flight
# reservation must self-heal quickly: if the request dies WITHOUT writing a
# terminal record (client disconnect / cancellation, OOM, hard crash — none of
# which a middleware ``except`` can reliably clean up), the key would otherwise
# block every legitimate retry with 409 for the whole TTL (up to 24h). Capping
# the pending TTL bounds that window to minutes while still comfortably covering
# any realistic request duration — and a request still RUNNING past it keeps its
# reservation (renewed while its task lives, see ``_keep_pending_alive``).
_PENDING_RESERVATION_TTL_SECONDS = 5 * 60


# A reservation whose request is still RUNNING is renewed at this interval
# (well inside the pending TTL), so a request slower than the pending TTL never
# loses its key to a concurrent retry. Only while the task that reserved it is
# alive: a dead request's reservation still self-heals at the pending TTL.
_PENDING_RENEW_INTERVAL_SECONDS = 60.0


def _pending_ttl(ttl_seconds: int) -> int:
    """TTL for a ``pending`` reservation: short, never above the record TTL."""
    return min(ttl_seconds, _PENDING_RESERVATION_TTL_SECONDS)


@dataclass
class _LiveReservation:
    reservation: IdempotencyReservation
    owner: weakref.ref[asyncio.Task[Any]]
    deadline: float  # monotonic; never renewed past the record TTL


#: Reservations whose owner task is still running, renewed by the keeper.
_live_reservations: dict[tuple[str, str], _LiveReservation] = {}
_keeper: asyncio.Task[None] | None = None


def _track_reservation(reservation: IdempotencyReservation) -> None:
    """Keep ``reservation`` pending while the task that made it runs."""
    owner = asyncio.current_task()
    if owner is None:  # pragma: no cover - reserve_key always runs in a task
        return
    key = (reservation.full_key, reservation.pending_value)
    _live_reservations[key] = _LiveReservation(
        reservation,
        weakref.ref(owner),
        time.monotonic() + reservation.ttl_seconds,
    )
    owner.add_done_callback(lambda _task: _live_reservations.pop(key, None))
    global _keeper
    loop = asyncio.get_running_loop()
    if _keeper is None or _keeper.done() or _keeper.get_loop() is not loop:
        _keeper = loop.create_task(
            _keep_pending_alive(), name="sandloom-idempotency-keeper"
        )


def _untrack_reservation(reservation: IdempotencyReservation) -> None:
    _live_reservations.pop((reservation.full_key, reservation.pending_value), None)


async def _keep_pending_alive() -> None:
    """Renew every live reservation (compare-and-set on its own pending value,
    so a finalised, released or lost key is never resurrected); exits when
    none is left."""
    while _live_reservations:
        await asyncio.sleep(_PENDING_RENEW_INTERVAL_SECONDS)
        now = time.monotonic()
        for key, live in list(_live_reservations.items()):
            owner = live.owner()
            if owner is None or owner.done() or now >= live.deadline:
                _live_reservations.pop(key, None)
                continue
            res = live.reservation
            try:
                kept = await _store_cas_set(
                    res.full_key,
                    res.pending_value,
                    res.pending_value,
                    _pending_ttl(res.ttl_seconds),
                )
            except _CacheUnavailable:
                logger.warning(
                    "Could not renew the in-flight idempotency reservation %s; "
                    "retrying on the next sweep",
                    res.full_key,
                )
                continue
            if not kept:  # finalised / released / expired meanwhile
                _live_reservations.pop(key, None)


async def _persist_terminal(
    reservation: IdempotencyReservation, record: dict[str, Any]
) -> bool:
    """Finalise OUR reservation into the terminal ``succeeded`` record.

    Compare-and-set against our own pending value, so a request whose
    reservation already expired and was re-reserved by a retry never stomps
    that retry's reservation. If our reservation is simply gone (expired, no
    one re-reserved) the terminal record is still written with SET NX — a
    later retry then replays instead of RE-EXECUTING a committed operation.
    Retries transient cache errors; a persistent failure is LOUD (logged +
    ``idempotency_terminal_persist_failures_total``). Returns True if written.

    This cache record is NOT a source of truth for exactly-once *effects* —
    money / irreversible operations must guarantee that with ``durable_inbox``.
    """
    _untrack_reservation(reservation)
    full_key = reservation.full_key
    payload = orjson.dumps(record).decode()
    ttl = reservation.ttl_seconds
    for attempt in range(_TERMINAL_PERSIST_ATTEMPTS):
        try:
            if await _store_cas_set(full_key, reservation.pending_value, payload, ttl):
                return True
            if await _store_add(full_key, payload, ttl):
                return True
            # Another request owns the key now (our reservation expired and a
            # retry re-reserved it). Not ours to overwrite.
            safe_inc("idempotency_terminal_persist_failures_total")
            logger.warning(
                "Idempotency reservation for %s was lost before completion "
                "(expired and re-reserved by another request); not overwriting",
                full_key,
            )
            return False
        except _CacheUnavailable:
            if attempt + 1 < _TERMINAL_PERSIST_ATTEMPTS:
                await asyncio.sleep(random.uniform(0, 0.05 * 2**attempt))  # noqa: S311 - retry jitter, not a secret
                continue
            safe_inc("idempotency_terminal_persist_failures_total")
            logger.error(
                "Failed to persist terminal idempotency record for %s after %d "
                "attempts; a later retry may re-execute the completed operation "
                "(use durable_inbox for exactly-once effects)",
                full_key,
                _TERMINAL_PERSIST_ATTEMPTS,
            )
    return False


async def _release_key(reservation: IdempotencyReservation) -> None:
    """Release OUR reservation after a FAILED attempt so a retry can reserve.

    Compare-and-delete on our own pending value: if the reservation expired
    and a retry re-reserved the key, that retry's reservation is left intact
    (deleting it would let yet another request run concurrently). Deleting
    (rather than writing a ``failed`` marker) means the next retry reserves
    atomically via SET NX. Best-effort with retries: a give-up leaves our
    pending record to self-heal at the short pending TTL (delayed, never
    duplicated), so it is a warning, not an error.
    """
    _untrack_reservation(reservation)
    for attempt in range(_TERMINAL_PERSIST_ATTEMPTS):
        try:
            await _store_cas_delete(reservation.full_key, reservation.pending_value)
            return
        except _CacheUnavailable:
            if attempt + 1 < _TERMINAL_PERSIST_ATTEMPTS:
                await asyncio.sleep(random.uniform(0, 0.05 * 2**attempt))  # noqa: S311 - retry jitter, not a secret
                continue
            safe_inc("idempotency_terminal_persist_failures_total")
            logger.warning(
                "Failed to release idempotency key %s after a failed attempt; it "
                "will self-heal when the pending reservation TTL expires",
                reservation.full_key,
            )


ReserveOutcome = Literal["reserved", "replay", "in_progress", "mismatch"]


async def reserve_key(
    full_key: str,
    fingerprint: str,
    ttl_seconds: int,
    *,
    anonymous: bool = False,
) -> tuple[ReserveOutcome, IdempotencyReservation | dict[str, Any] | None]:
    """Reserve / replay / reject one idempotency key (shared by HTTP and RPC).

    Returns ``("reserved", reservation)``, ``("replay", stored_response)``,
    ``("in_progress", None)`` or ``("mismatch", None)``. Raises
    :class:`_CacheUnavailable` on a cache error (the caller decides fail
    open/closed). Loops so the only ways out are a REAL reservation (atomic
    SET NX), a replay, or an explicit reject — never proceeding without a
    reservation. A corrupt record (unparseable) is removed (compare-and-delete
    on its exact bytes) and re-reserved instead of blocking the key with 409
    for its whole TTL.
    """
    for _ in range(_MAX_RESERVE_ATTEMPTS):
        # Reserve-or-read in ONE atomic step (``SET NX GET`` where the driver
        # fuses it): either our pending record is in, or we see what is there.
        pending_value = _pending_record(fingerprint)
        created, raw = await _store_reserve(
            full_key, pending_value, _pending_ttl(ttl_seconds)
        )
        if created:
            reservation = IdempotencyReservation(
                full_key=full_key,
                fingerprint=fingerprint,
                ttl_seconds=ttl_seconds,
                pending_value=pending_value,
                anonymous=anonymous,
            )
            _track_reservation(reservation)
            return "reserved", reservation
        if raw is None:
            # The key vanished between the steps of a non-fused driver
            # (expired / released concurrently): try to reserve again.
            continue
        record = _parse_record(raw)
        state = record.get("state")
        if state == "corrupt":
            logger.warning("Discarding corrupt idempotency record %s", full_key)
            if raw is not None:
                await _store_cas_delete(full_key, raw)
            continue
        if record.get("fp") not in (None, fingerprint):
            return "mismatch", None
        if state == "succeeded":
            return "replay", record.get("response") or {}
        # pending (or unknown): a first request is still in flight.
        return "in_progress", None
    # Pathological churn: refuse rather than proceed without a reservation.
    return "in_progress", None
