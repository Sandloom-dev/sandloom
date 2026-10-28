"""The write-path effects: :func:`publish_event` (outbox write) and
:func:`record_audit`, plus :func:`durable_inbox`.

Both write in the current Unit of Work's transaction — the event row in the
transactional outbox (dispatched after commit), the audit row in ``audit_log``
— so they commit iff the change commits; outside a UoW they raise unless the
caller explicitly downgrades to best-effort (``required=False``).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import TYPE_CHECKING, Any

import sandloom.core.observability.request_context as _request_context

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Annotation-only: the engine is paradigm-agnostic and imports no SQLAlchemy
    # at runtime (relational specifics stay behind the lazy sql_alchemy imports),
    # so it sits OUTSIDE the relational-paradigm boundary ratchet.
    pass

import sandloom.core.eventing.envelope as _envelope
import sandloom.core.observability.guardrails as _guardrails
import sandloom.core.observability.tracing as _tracing
import sandloom.core.runtime as _runtime
from sandloom.core.unit_of_work.scope import (
    _OUTBOX_PENDING_KEY,
    _audit_store,
    _outbox_store,
    _PendingEvent,
    _refuse_in_read_only_scope,
    _resolve_scoped_session,
    _session_bus,
)

logger = logging.getLogger("sandloom.core.unit_of_work")


#: Bound by assignment (keeps the ``.get()`` method-call fast path).
_request_id_var = _request_context.request_id_var


def _snapshot_payload(value: Any) -> Any:
    """Copy an event payload's JSON containers; share its leaf values.

    ``publish_event`` stores and later dispatches this snapshot, never the
    caller's object: a caller that reuses or mutates its dict (or a nested
    list / dict) after the call must not make the bus deliver something other
    than the committed outbox row under the same event id (the relay would
    redeliver the row's version). "Deep enough" = every ``dict`` / ``list`` /
    ``tuple`` is copied, recursively (dict keys are immutable already); leaf
    values are shared — the JSON scalars and the immutable types the
    serializer stringifies (``UUID``, ``datetime``, ``Decimal``, enums).
    Mutating some other mutable object placed in a payload after publishing
    is outside the contract (an event payload is JSON data).
    """
    if isinstance(value, dict):
        return {key: _snapshot_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snapshot_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_snapshot_payload(item) for item in value)
    return value


class UnitOfWorkRequiredError(RuntimeError):  # SANDLOOM-R002
    """A guaranteed write-path effect was requested outside a Unit of Work.

    Raised by :func:`publish_event` when no UoW scope is active and the caller
    did not opt into best-effort semantics (``record_audit`` raises its own
    :class:`AuditUnavailableError` in the same situation). Without a UoW
    there is no transaction for the outbox row to commit with, so the
    guarantee ("published iff the change commits, never lost") cannot hold.
    """


async def publish_event(
    channel: str,
    payload: dict[str, Any],
    schema_version: str = "v1",
    *,
    database: str | None = None,
    required: bool | None = None,
) -> None:
    """Publish a domain event through the transactional outbox.

    Inside a UoW-managed scope (request via DbSession, ``db_session()``, an
    event consumer / contract command / Celery task UoW) the event is written
    to the transactional outbox in the SAME transaction as the business change
    — it becomes visible on the bus only after commit, and is never lost or
    phantom-published on rollback.

    Each database owns its own transactional outbox. When an endpoint holds
    more than one database session at once (e.g. ``DbSession`` plus
    ``Database("analytics")``) pass ``database=`` to choose which database's
    outbox the event is written to — the business change and its event then land
    in the same transaction. With a single active database ``database`` may be
    omitted; with several active it is REQUIRED (an omitted choice raises rather
    than guessing and risking the wrong outbox).

    **Outside a UoW scope the call RAISES** :class:`UnitOfWorkRequiredError`
    (``required`` defaults to ``True`` since 0.7). Best-effort is explicit:
    ``required=False`` still uses the outbox when a UoW is active, and
    otherwise publishes straight to the event bus — no transaction, no relay
    retry, lost if the broker is down. Business modules need an
    ``# sandloom: best-effort <reason>`` marker on such a call (``sandloom check`` rule
    ``best-effort-effects``).
    """

    # Normalize the version BEFORE the outbox write so the stored row and the
    # later dispatched envelope agree (a `<...>.vN` channel is authoritative).
    _refuse_in_read_only_scope("publish_event")
    schema_version = _envelope.normalize_schema_version(channel, schema_version)
    session = _resolve_scoped_session(database, op="publish_event")
    if session is not None:
        # The row and the dispatched event are ONE snapshot taken now, out of
        # the caller's reach (see _snapshot_payload).
        payload = _snapshot_payload(payload)
        # Full preflight (channel name + schema + size) INSIDE the transaction:
        # an event the bus would reject must never be committed to the outbox
        # and then fail forever at dispatch. Raises before the row is added.
        _session_bus(session).preflight(channel, payload)
        # The store (backend-specific) owns how the event is persisted — this
        # never imports a concrete ORM model.
        handle = _outbox_store(session).enqueue(
            session, channel, payload, schema_version
        )
        pending = session.info.setdefault(_OUTBOX_PENDING_KEY, [])
        pending.append(_PendingEvent(handle, channel, schema_version, payload))
        return

    if required is not False:
        raise UnitOfWorkRequiredError(
            f"publish_event({channel!r}) called outside a Unit of Work: there "
            "is no transaction for the outbox row, so delivery cannot be "
            "guaranteed. Fix: call it inside a UoW (a request with DbSession, "
            "`async with db_session():`, an event consumer or a Celery task "
            "body using db_session()), or pass required=False to accept a "
            "best-effort direct bus publish (no outbox, no retry). "
            "(sandloom explain SANDLOOM-R002)"
        )

    await _runtime.active_event_bus().publish_event(channel, payload, schema_version)


class AuditUnavailableError(RuntimeError):
    """A ``required`` audit write could not be performed.

    Raised by :func:`record_audit` (``required`` is the default since 0.7) when
    there is no UoW scope, the backend has no audit store, or the actor id is
    not a UUID — a security/compliance action must fail loudly rather than
    silently skip its audit trail.
    """


def record_audit(
    action: str,
    *,
    actor_id: _uuid.UUID | str | None = None,
    actor_type: str = "user",
    resource_type: str | None = None,
    resource_id: str | None = None,
    data: dict[str, Any] | None = None,
    required: bool | None = None,
    database: str | None = None,
) -> None:
    """Append an entry to the audit log within the current Unit of Work.

    Writes into the active request/task transaction, so the audit row commits
    iff the audited change commits (correct for auditing SUCCESSFUL actions).
    To audit a FAILED attempt that must survive a rollback, write it in a
    separate transaction opened after the request.

    Modules call this by importing from ``sandloom.core.di`` — no ORM import needed.

    Like ``publish_event``, when an endpoint holds more than one database
    session at once pass ``database=`` to choose which database's ``audit_log``
    the record lands in (so a billing action is audited in the billing database,
    not whichever session happened to be innermost). With a single active
    database it may be omitted; with several active and no choice the call
    raises rather than auditing in the wrong database.

    ``required`` defaults to ``True`` (since 0.7): no UoW scope, a backend
    without an audit store, or a non-UUID ``actor_id`` string raise
    :class:`AuditUnavailableError` instead of dropping the record. Best-effort
    is explicit — ``required=False`` skips (logged) in those cases; business
    modules need an ``# sandloom: best-effort <reason>`` marker on such a call
    (``sandloom check`` rule ``best-effort-effects``).
    """
    _refuse_in_read_only_scope("record_audit")
    session = _resolve_scoped_session(database, op="record_audit")
    if required is None:
        required = True
    if session is None:
        if required:
            raise AuditUnavailableError(
                f"record_audit({action!r}) called outside a Unit of Work; "
                "cannot persist a mandatory audit record. Fix: call it inside "
                "a UoW (a request with DbSession, `async with db_session():`, "
                "an event consumer), or pass required=False to accept a "
                "best-effort audit that is skipped without a UoW."
            )
        logger.debug("record_audit('%s') called outside a UoW scope; skipped", action)
        return

    resolved_actor: _uuid.UUID | None
    if isinstance(actor_id, str):
        try:
            resolved_actor = _uuid.UUID(actor_id)
        except ValueError:
            # Never silently corrupt the trail: surface the bad actor id. A
            # mandatory audit refuses it; otherwise we keep going with None but
            # leave a warning so the dropped identity is visible.
            if required:
                raise AuditUnavailableError(
                    f"record_audit('{action}', required=True) got a non-UUID "
                    f"actor_id {actor_id!r}; refusing to record a null actor"
                ) from None
            logger.warning(
                "record_audit('%s'): actor_id %r is not a valid UUID; "
                "recording actor_id=None",
                action,
                actor_id,
            )
            resolved_actor = None
    else:
        resolved_actor = actor_id

    # The store (backend-specific) owns the persistence — this never imports a
    # concrete ORM model. A backend without an audit store skips silently.
    store = _audit_store(session)
    if store is None:
        if required:
            raise AuditUnavailableError(
                f"record_audit('{action}', required=True): the database backend "
                "has no audit store; cannot persist a mandatory audit record"
            )
        logger.debug("record_audit('%s'): backend has no audit store; skipped", action)
        return
    store.record(
        session,
        action=action,
        actor_id=resolved_actor,
        actor_type=actor_type,
        resource_type=resource_type,
        resource_id=resource_id,
        # Defensively scrub credential-shaped keys: the contract asks callers to
        # keep PII/secrets out of `data`, but a stray token/password in an
        # audit diff should never land unredacted in audit_log.
        data=_guardrails.redact(data) if data is not None else None,
        request_id=_current_request_id(),
        trace_id=_current_trace_id(),
    )


def _current_request_id() -> str | None:
    return _request_id_var.get()


def _current_trace_id() -> str | None:
    try:
        return _tracing.get_current_trace_id()
    except Exception:  # pragma: no cover  # noqa: BLE001 - log context is best effort
        return None
