"""The effect model's dedupe record — one vocabulary for every "already ran".

ADR-0005 (``docs/adr/0005-effect-model.md``) defines the model; this module
is its shared, paradigm-agnostic half. Every store that remembers that an
operation already ran is a **dedupe record**:

* **scope** — who dedupes (a consumer group, ``<contract>.<method>`` + bound
  tenant, an endpoint scope + caller + tenant, a task namespace + tenant);
* **key** — the operation's identity in the scope (an envelope id, an
  idempotency / operation key, an ``Idempotency-Key`` header);
* **args digest** — the canonical digest of its inputs: the same key with
  another digest is a conflict, never a replay (:func:`args_digest`);
* **status** — :data:`PENDING` → :data:`SUCCEEDED` / :data:`NOT_REPLAYABLE`,
  or :data:`PROCESSED` for a claim with no outcome to replay;
* **result** — the encoded outcome, at most :data:`RESULT_MAX_BYTES`
  (:func:`replay_outcome`);
* **TTL** — :data:`RECORD_TTL_SECONDS` unless the store configures another
  (:func:`ttl_or_default`).

Two forms implement it. The **durable** form (exactly-once *effect* on the
state of the database holding the record) is claimed with ``INSERT … ON
CONFLICT`` in the effect's own Unit of Work; its relational implementation
is :mod:`sandloom.core.sql_alchemy.dedupe_log`, mapped onto a table by a
:class:`DedupeRecordSpec` (the consumer inbox, the contract command log and
the durable HTTP idempotency log are its three adapters). The **cache** form
(replay-only: ``idempotency_guard`` without ``durable=True``, the cache
fallback of command contracts, the cache consumer inbox, the task-queue
enqueue dedupe) keeps its own owner-token state machine
but takes its bounds, tags and digests from here.

A new dedupe store is an amendment to ADR-0005, not a local detail:
New mechanisms follow the ADR-0005 rule (reviewed, not linted).
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import orjson

from sandloom.core import contract_wire
from sandloom.core.config.replay_limits import (
    REPLAY_BODY_MAX_BYTES,
    REPLAY_RECORD_TTL_SECONDS,
)


class Guarantee(enum.StrEnum):
    """ADR-0005 guarantee levels, strongest first.

    ``EXACTLY_ONCE_EFFECT`` is scoped to the state of the database that holds
    the durable record (effects a handler sends elsewhere stay at-least-once);
    ``REPLAY_ONLY`` deduplicates the response, never the effect;
    ``AT_MOST_ONCE`` never runs twice and may be lost (the ``# sandloom:
    best-effort`` opt-in marker keeps its historical name).
    """

    TRANSACTIONAL = "transactional"
    EXACTLY_ONCE_EFFECT = "exactly-once-effect"
    AT_LEAST_ONCE = "at-least-once"
    REPLAY_ONLY = "replay-only"
    AT_MOST_ONCE = "at-most-once"
    #: Former name of :attr:`AT_MOST_ONCE` (an alias, same member).
    BEST_EFFORT = "at-most-once"


# --------------------------------------------------------------------------- #
# Bounds (one source: sandloom.core.config.replay_limits)
# --------------------------------------------------------------------------- #

#: Default lifetime of a dedupe record (24 h).
RECORD_TTL_SECONDS = REPLAY_RECORD_TTL_SECONDS
#: Largest encoded outcome kept for replay (256 KiB); larger -> not replayable.
RESULT_MAX_BYTES = REPLAY_BODY_MAX_BYTES


def ttl_or_default(raw: object, default: int = RECORD_TTL_SECONDS) -> int:
    """A configured record TTL: a positive ``int``, else ``default``."""
    return raw if isinstance(raw, int) and raw > 0 else default


def fits_replay(size: int) -> bool:
    """Whether an outcome of ``size`` bytes is kept for replay."""
    return size <= RESULT_MAX_BYTES


# --------------------------------------------------------------------------- #
# Record statuses
# --------------------------------------------------------------------------- #

#: Claimed, the operation is in flight (durable: visible only inside the
#: claiming transaction; cache: an owner-token reservation).
PENDING = "pending"
#: Completed; the stored result is replayed to a duplicate.
SUCCEEDED = "succeeded"
#: Completed with an outcome larger than :data:`RESULT_MAX_BYTES`: a
#: duplicate is refused, never re-run, and gets no body.
NOT_REPLAYABLE = "not_replayable"
#: Claimed and applied; nothing to replay (an inbox claim).
PROCESSED = "processed"

ClaimOutcome = Literal["claimed", "replay", "mismatch"]


@dataclass(frozen=True, slots=True)
class DedupeClaim:
    """Outcome of claiming a dedupe record.

    ``outcome`` is ``"claimed"`` (run the effect; the record commits with
    it), ``"replay"`` (the operation already ran with the same arguments:
    ``status`` / ``result`` are its stored record) or ``"mismatch"`` (the key
    was used with different arguments).
    """

    outcome: ClaimOutcome
    status: str | None = None
    result: str | None = None
    #: the stored call effects (JSON) of a replaying record that keeps them
    effects: str | None = None


class DedupeBusy(Exception):  # a state, not an error kind
    """A concurrent claim of the same key outlasted the store's lock wait."""


# --------------------------------------------------------------------------- #
# Keys and digests
# --------------------------------------------------------------------------- #


def key_digest(*parts: str) -> str:
    """Full sha256 hex of ``parts`` joined by NUL — a durable record's key.

    NUL cannot occur in a scope label, so distinct part tuples never collide
    by concatenation.
    """
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def tag(value: str, length: int) -> str:
    """The first ``length`` hex chars of sha256(``value``) — a cache key tag."""
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def canonical_digest(value: Any) -> str:
    """Canonical digest of a wire value (sets / maps sorted; process-stable).

    Raises :class:`sandloom.core.contract_wire.ContractWireError` for a value that
    has no wire form (use :func:`args_digest` to fall back instead).
    """
    return contract_wire.digest(value)


def args_digest(arguments: Mapping[str, Any]) -> str:
    """The args digest of a dedupe record (same key + other digest = conflict).

    The canonical wire digest; arguments without a wire form fall back to a
    digest of their ``repr`` (still stable for the same call in one process).
    """
    try:
        return contract_wire.digest(
            arguments if isinstance(arguments, dict) else dict(arguments)
        )
    except contract_wire.ContractWireError:
        raw = orjson.dumps(
            {name: repr(value) for name, value in arguments.items()},
            option=orjson.OPT_SORT_KEYS,
        )
        return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def replay_outcome(result: Any) -> tuple[str, bytes | None]:
    """``(status, encoded)`` to store for ``result``.

    :data:`SUCCEEDED` with the result's JSON-encoded contract-wire form when
    it has one and fits :data:`RESULT_MAX_BYTES`; otherwise
    :data:`NOT_REPLAYABLE` with ``None`` (a duplicate is refused, never
    re-run).
    """
    try:
        encoded = orjson.dumps(contract_wire.encode(result))
    except (contract_wire.ContractWireError, TypeError):
        return NOT_REPLAYABLE, None
    if not fits_replay(len(encoded)):
        return NOT_REPLAYABLE, None
    return SUCCEEDED, encoded


# --------------------------------------------------------------------------- #
# The durable form's table mapping
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DedupeRecordSpec:
    """How a durable dedupe record maps onto one table (column NAMES only).

    ``conflict`` names the columns of the unique key a claim conflicts on.
    Optional columns switch features of the claim on:

    * ``expires_at`` — the record has a TTL: an EXPIRED record is taken over
      by the next claim (``ON CONFLICT DO UPDATE … WHERE expires_at <= now``),
      a live one is replayed; completion restarts the TTL (PostgreSQL).
      Without it the record never expires (``ON CONFLICT DO NOTHING``) and is
      purged by age (``purge_column``).
    * ``args_digest`` / ``status`` / ``result`` — the record replays: on a
      conflict the stored row is read back (mismatch / replay). Without
      ``args_digest`` a conflict is a replay with status ``replay_status``
      and no read.
    * ``effects`` — a replaying record also keeps what the operation left on
      its caller (the command log: risks, calls in doubt, self-revocation,
      pending step-ups), stored at completion and read back with the result.

    ``returning`` is the primary-key column (returned by a claim, used by a
    bounded purge); ``purge_column`` is the column a purge compares with its
    cutoff. ``constraint`` names the unique constraint, so a dialect without
    ``ON CONFLICT`` reads only THIS conflict as a duplicate.
    ``busy_on_lock_timeout`` maps a lock-wait timeout of the claim to
    :class:`DedupeBusy`.
    """

    table: str
    conflict: tuple[str, ...]
    columns: tuple[str, ...]
    returning: str
    purge_column: str
    expires_at: str | None = None
    created_at: str | None = None
    args_digest: str | None = None
    status: str | None = None
    result: str | None = None
    effects: str | None = None
    replay_status: str = PROCESSED
    constraint: str | None = None
    busy_on_lock_timeout: bool = False

    @property
    def replays(self) -> bool:
        return self.args_digest is not None


def relay_retry_horizon_seconds(settings: Any) -> float:
    """Upper bound of the outbox relay's redelivery budget for one row.

    The sum of the backoff delays (``OUTBOX_RELAY_BACKOFF_BASE_SECONDS`` *
    2**(n-1), capped at ``OUTBOX_RELAY_BACKOFF_MAX_SECONDS``) over the
    ``OUTBOX_RELAY_MAX_ATTEMPTS`` attempts before the row is dead-lettered —
    the jittered delays never exceed these. Broker outages (the circuit does
    not burn attempts) extend it without bound.
    """
    base = float(settings.outbox_relay_backoff_base_seconds)
    cap = float(settings.outbox_relay_backoff_max_seconds)
    attempts = int(settings.outbox_relay_max_attempts)
    return sum(min(cap, base * 2.0**n) for n in range(max(0, attempts)))


__all__ = [
    "relay_retry_horizon_seconds",
    "NOT_REPLAYABLE",
    "PENDING",
    "PROCESSED",
    "RECORD_TTL_SECONDS",
    "RESULT_MAX_BYTES",
    "SUCCEEDED",
    "ClaimOutcome",
    "DedupeBusy",
    "DedupeClaim",
    "DedupeRecordSpec",
    "Guarantee",
    "args_digest",
    "canonical_digest",
    "fits_replay",
    "key_digest",
    "replay_outcome",
    "tag",
    "ttl_or_default",
]
