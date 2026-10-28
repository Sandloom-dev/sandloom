"""The ``idempotency_guard`` factory: validate the options, then build the
cache dependency or the durable one (:mod:`sandloom.core.idempotency.durable`).

The cache dependency makes the reserve / replay / retry / mismatch decision
*before* the endpoint runs (:func:`~sandloom.core.idempotency.reservation.reserve_key`)
and arms a fresh first request so :mod:`sandloom.core.idempotency.capture` settles
it from the response.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import HTTPException, Request

import sandloom.core.unit_of_work.risk as _risk
from sandloom.core import effects
from sandloom.core.guard_order import mark_idempotency_guard
from sandloom.core.idempotency.durable import _MODE_ATTR, GuardMode, _durable_guard
from sandloom.core.idempotency.fingerprint import (
    _ARMED_ATTR,
    _DEFAULT_TTL_SECONDS,
    IdempotencyReservation,
    _CacheUnavailable,
    _caller_identity,
    _request_fingerprint,
    _request_key,
    _tenant_identity,
)
from sandloom.core.idempotency.replay import _ReplayResponse
from sandloom.core.idempotency.reservation import ReserveOutcome, reserve_key

logger = logging.getLogger("sandloom.core.idempotency")


def idempotency_guard(
    *,
    scope: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    required: bool = True,
    fail_closed: bool = False,
    store_response: bool = True,
    durable: bool = False,
) -> Callable[..., Awaitable[str | None]]:
    """Build an Idempotency-Key dependency for one logical operation ``scope``.

    ``durable=True`` — the DURABLE form (:mod:`sandloom.core.idempotency`): the
    record lives in the request's default database (``DbSession``'s Unit of
    Work, shared with the endpoint) and commits iff the endpoint's effect
    commits; no cache is involved (a database outage fails the request
    anyway). Required on a money route (``sensitive("money")``) that stores
    responses (``create_app`` refuses a cache guard there,
    :func:`~sandloom.core.idempotency.durable.money_guard_problems`, SANDLOOM-B019).
    The endpoint's effect must be written through the request's
    ``DbSession``; ``store_response`` / ``fail_closed`` do not apply.

    Stateful: a first request reserves the key (``pending``, with an owner
    token) and is armed so its response is captured. A retry while still
    pending gets ``409``; after the first request succeeded the stored response
    is replayed; after it failed the retry is allowed; the same key reused with
    a different request body gets ``422``.

    ``fail_closed`` controls behavior when the cache is unavailable:

    * ``False`` (default) — fail OPEN (allow). Right for non-critical endpoints.
    * ``True`` — fail CLOSED (``503``). REQUIRED for money / irreversible side
      effects: a cache outage must never let duplicate charges through.

    ``store_response=False`` — in-flight dedupe only: a concurrent duplicate
    still gets ``409`` and a reused key with another body ``422``, but a
    finished request (success or failure) releases the key and NOTHING is
    stored or replayed. For endpoints that own durable idempotency themselves
    and whose response must not sit in the cache or be replayed without
    re-authorization (billing's checkout / portal URLs: bearer credentials,
    re-issued by the operation-intent path after a fresh authority check).

    Anonymous callers (no ``Authorization``): identity falls back to the source
    IP, which collapses distinct clients behind a proxy/NAT (or without
    ``TRUST_PROXY_HEADERS``) into one namespace. Their stored record is
    therefore status-only — a retry is still deduplicated (never re-executed)
    but a response BODY is never replayed to an anonymous caller, so one client
    cannot read another's result by reusing its key. Declare the guard after
    the route's authentication: ``create_app`` refuses a route that solves it
    first (:mod:`sandloom.core.guard_order`).

    NOTE (cache form): the terminal ``succeeded`` / ``failed`` record reflects
    the HTTP *status code*, not the transaction outcome — a request that
    committed a side effect and then returned ``>=400`` is recorded ``failed``
    and a retry is allowed; a crash or a lost ``COMMIT`` acknowledgement
    before the terminal write lets a retry re-run a committed effect. The
    cache form is therefore replay-only, NOT an exactly-once *effect* on money
    / irreversible operations: use ``durable=True`` (or a ``UNIQUE``
    operation key on the effect's own row).
    """
    _validate_options(durable=durable, store_response=store_response)
    if durable:
        return _durable_guard(scope=scope, ttl_seconds=ttl_seconds, required=required)
    return _cache_guard(
        scope=scope,
        ttl_seconds=ttl_seconds,
        required=required,
        fail_closed=fail_closed,
        store_response=store_response,
    )


def _validate_options(*, durable: bool, store_response: bool) -> None:
    """Refuse an option combination the chosen guard form cannot honour."""
    if durable and not store_response:
        raise ValueError(
            "idempotency_guard(durable=True) stores the response; "
            "store_response=False is the cache form's in-flight-only mode"
        )


def _cache_guard(
    *,
    scope: str,
    ttl_seconds: int,
    required: bool,
    fail_closed: bool,
    store_response: bool,
) -> Callable[..., Awaitable[str | None]]:
    """The cache-form dependency (replay-only, or in-flight-only)."""

    async def _dependency(request: Request) -> str | None:
        key = await _request_key(request, required)
        if key is None:
            return None

        # Scope the cache key to both caller and tenant. The same user and
        # client-generated key may legitimately perform one operation per
        # tenant; omitting the tenant would replay one tenant's response into
        # another tenant's request.
        caller = _caller_identity(request)
        tenant = _tenant_identity(request)
        caller_tag = effects.tag(caller, 16)
        tenant_tag = effects.tag(tenant, 16)
        full_key = f"idem:{scope}:{caller_tag}:{tenant_tag}:{key}"
        fingerprint = await _request_fingerprint(request, caller, tenant)
        try:
            outcome, value = await reserve_key(
                full_key,
                fingerprint,
                ttl_seconds,
                anonymous=caller.startswith("anon:"),
            )
        except _CacheUnavailable:
            _store_unavailable(scope, fail_closed)
            _risk.bind_operation_key(
                effects.key_digest("http", scope, caller, tenant, key)
            )
            return key
        _apply_outcome(request, outcome, value, store_response)
        _risk.bind_operation_key(effects.key_digest("http", scope, caller, tenant, key))
        return key

    mode = GuardMode("cache" if store_response else "in_flight", fail_closed)
    setattr(_dependency, _MODE_ATTR, mode)
    return mark_idempotency_guard(_dependency)


def _store_unavailable(scope: str, fail_closed: bool) -> None:
    """Cache outage: raise ``503`` when failing closed, else log and allow."""
    if fail_closed:
        logger.error(
            "Idempotency store unavailable for scope=%s; failing closed",
            scope,
        )
        raise HTTPException(
            status_code=503,
            detail="Idempotency store unavailable; retry later",
        ) from None
    logger.warning(
        "Idempotency store unavailable for scope=%s; failing open (allowing)",
        scope,
    )


def _apply_outcome(
    request: Request,
    outcome: ReserveOutcome,
    value: IdempotencyReservation | dict[str, Any] | None,
    store_response: bool,
) -> None:
    """Arm a reserved request; replay, or reject (``422`` / ``409``) the rest."""
    if outcome == "reserved":
        if not store_response:
            value = dataclasses.replace(
                cast(IdempotencyReservation, value), in_flight_only=True
            )
        setattr(request.state, _ARMED_ATTR, value)
        return
    if outcome == "replay":
        raise _ReplayResponse(cast(dict[str, Any], value))
    if outcome == "mismatch":
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key reused with a different request",
        )
    raise HTTPException(
        status_code=409,
        detail="A request with this Idempotency-Key is still in progress",
    )
