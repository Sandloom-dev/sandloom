"""The subject-state record (ADR-0009 item 13): one cache key per subject,
its codec (microsecond precision), lifetime, issue floors and metrics.

The record ``sandloom:auth:subject:{id}`` holds one of two values:

* ``v1:after:<seconds.micros>`` — every access token issued at or before the
  instant is revoked (a token is revoked iff its ``iat_us`` is ``<=`` it).
  Written with a TTL (:func:`_revocation_ttl_seconds`), monotonic;
* ``v1:blocked:<seconds.micros>`` — the subject is blocked since the instant
  (no TTL). It ranks above every ``after`` value, so a revocation never
  unblocks; unblocking replaces it by compare-and-set with an ``after`` value
  (:func:`sandloom.core.auth.blocks.unblock_user`), never a delete.

A value outside that domain is corrupt and fails closed (every token of the
subject is rejected) until the issuance check repairs it. Writers live in
:mod:`sandloom.core.auth.revocation` / :mod:`sandloom.core.auth.blocks`, the reader in
:mod:`sandloom.core.auth.verification` (one ``GET`` per authenticated request).
"""

from __future__ import annotations

import logging
import math
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import ROUND_CEILING
from typing import Any

from prometheus_client import Counter, Gauge

from sandloom.core.config.settings import settings
from sandloom.core.infra.contracts import parse_subject_state

logger = logging.getLogger(__name__)

#: Framework-owned cache namespace of the per-subject state record.
SUBJECT_KEY_PREFIX = "sandloom:auth:subject:"
#: Value kinds of the record.
STATE_AFTER = "after"
STATE_BLOCKED = "blocked"
#: Microsecond issued-at claim stamped by :func:`create_access_token`. The
#: registered ``iat`` (whole seconds, RFC 7519) cannot order a token against a
#: revocation in the same second; ``iat_us`` can. A token without it is
#: treated as revoked whenever its subject carries a mark (fail closed).
IAT_US_CLAIM = "iat_us"
#: Core event written to the outbox IN the transaction that revokes (see
#: :func:`revoke_user_tokens_on_commit`): the durable record of a pending
#: revocation, applied by the session owner's consumer
#: (:func:`apply_pending_revocation`) — at-least-once, idempotent.
TOKENS_REVOKED_CHANNEL = "core.auth.tokens_revoked.v1"


class RevocationUnavailableError(RuntimeError):
    """The revocation store is unreachable and the policy is fail-closed.

    Also raised by ``revoke_user_tokens_on_commit(..., require_immediate=True)``
    when the mark cannot be written before commit: the change is refused
    (``503``) and the Unit of Work rolls back.
    """

    http_status = 503
    code = "revocation_unavailable"
    detail = "Authentication store temporarily unavailable"


def subject_key(subject: Any) -> str:
    """The cache key of ``subject``'s state record."""
    return f"{SUBJECT_KEY_PREFIX}{subject}"


#: Default floor of a revocation mark's lifetime (see _revocation_ttl_seconds).
_REVOCATION_MARK_FLOOR_SECONDS = 24 * 3600
_REVOCATION_SKEW_SECONDS = 120
_US = 1_000_000

_METRIC_REVOCATION_DEFERRED = Counter(
    "auth_revocation_deferred_total",
    "Token revocations whose mark could not be written after commit (retries "
    "exhausted): pending until the durable applier writes them",
)
_METRIC_REVOCATION_APPLIER_FAILURES = Counter(
    "auth_revocation_applier_failures_total",
    "Durable applier deliveries that could not write a revocation mark "
    "(the core.auth.tokens_revoked.v1 event is redelivered)",
)
_METRIC_REVOCATION_CORRUPT_MARK = Counter(
    "auth_revocation_corrupt_mark_total",
    "Revocation marks that could not be parsed (treated as revoking every "
    "token of the subject: fail-closed)",
)
_METRIC_REVOCATION_IMMEDIATE_REFUSED = Counter(
    "auth_revocation_immediate_refused_total",
    "Changes refused (503) because a require_immediate revocation mark could "
    "not be written before commit",
)
_METRIC_REVOCATION_REMARK_OVERTOOK_FLOOR = Counter(
    "auth_revocation_remark_overtook_floor_total",
    "require_immediate revocations whose UoW committed more than "
    "AUTH_REVOCATION_CLOCK_SKEW_SECONDS after the call: the post-commit re-mark "
    "also revoked tokens the revoking request itself minted (fail-closed; the "
    "client re-authenticates)",
)
_METRIC_REVOCATION_OUTLIVED_MARK_TTL = Counter(
    "auth_revocation_outlived_mark_ttl_total",
    "Access tokens rejected because they are older than the revocation-mark "
    "TTL (a revoking mark may have expired): token-lifetime settings drift "
    "between services",
)
_METRIC_REVOCATION_MALFORMED = Counter(
    "auth_revocation_malformed_total",
    "Durable revocation events that can never be applied (malformed payload): "
    "dead-lettered — CRITICAL, see the runbook in OPERATIONS.md",
)
_GAUGE_REVOCATION_PENDING = Gauge(
    "auth_revocation_pending_seconds",
    "Age (since the revoking change) of the oldest durable revocation this "
    "process could not apply yet (0 = none stuck); /ready reports it",
    multiprocess_mode="livemax",
)
_METRIC_REVOCATION_APPLIED = Counter(
    "auth_revocation_applied_total",
    "Revocation marks written (or found already covered)",
    ["path"],
)


def _counter_value(counter: Counter) -> float:
    total = 0.0
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += float(sample.value)
    return total


def revocation_metrics_snapshot() -> dict[str, float]:
    """Process-local revocation counters (tests / diagnostics)."""
    return {
        "deferred": _counter_value(_METRIC_REVOCATION_DEFERRED),
        "applier_failures": _counter_value(_METRIC_REVOCATION_APPLIER_FAILURES),
        "applied": _counter_value(_METRIC_REVOCATION_APPLIED),
        "corrupt": _counter_value(_METRIC_REVOCATION_CORRUPT_MARK),
        "immediate_refused": _counter_value(_METRIC_REVOCATION_IMMEDIATE_REFUSED),
        "outlived_mark_ttl": _counter_value(_METRIC_REVOCATION_OUTLIVED_MARK_TTL),
        "remark_overtook_floor": _counter_value(
            _METRIC_REVOCATION_REMARK_OVERTOOK_FLOOR
        ),
    }


def _revocation_ttl_seconds() -> int:
    """Lifetime of a revocation mark.

    The mark must outlive every access token minted before it — by ANY
    service, not just this (writer) process: in a split deployment tenancy
    writes the mark while identity mints the tokens, and a mark that expires
    first lets a removed member's token through again. So the writer's own
    ``ACCESS_TOKEN_EXPIRE_MINUTES`` is only a lower bound:
    ``AUTH_REVOCATION_MARK_TTL_SECONDS`` when set (clamped up to that bound),
    else ``max(own access TTL + skew, 24h)``. A missing mark means "no
    revocation" to the verifying service — except for a token OLDER than this
    TTL, which is rejected (:func:`_outlived_mark_ttl`): a mark that revoked it
    may have expired, so a drifted token lifetime fails closed, never revives.
    """
    own = (
        int(settings.access_token_expire_minutes) * 60
        + _REVOCATION_SKEW_SECONDS
        + math.ceil(_clock_skew_margin())
    )
    configured = getattr(settings, "auth_revocation_mark_ttl_seconds", None)
    if configured:
        return max(int(configured), own)
    return max(own, _REVOCATION_MARK_FLOOR_SECONDS)


# --- revocation instants (microsecond precision) ---------------------------- #
def _clock_skew_margin() -> float:
    margin = getattr(settings, "auth_revocation_clock_skew_seconds", 0.0)
    return max(float(margin or 0.0), 0.0)


def _seconds_to_us(seconds: float) -> int:
    return round(float(seconds) * _US)


def _datetime_us(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * _US + delta.microseconds


def _format_instant(instant_us: int) -> str:
    return f"{instant_us // _US}.{instant_us % _US:06d}"


def _format_mark(mark_us: int) -> str:
    """The ``after`` value revoking every token issued at or before ``mark_us``."""
    return f"v1:{STATE_AFTER}:{_format_instant(mark_us)}"


def _format_blocked(since_us: int) -> str:
    """The ``blocked`` value of a subject blocked since ``since_us``."""
    return f"v1:{STATE_BLOCKED}:{_format_instant(since_us)}"


def _parse_state(raw: Any) -> tuple[str, int] | None:
    """``(kind, instant_us)`` of a record value, ``None`` when it is corrupt.

    The instant is rounded UP to the microsecond (never earlier than the
    writer meant). The value domain is the cache's ``set_subject_state`` one
    (:func:`~sandloom.core.infra.contracts.parse_subject_state`), so a value the
    reader treats as corrupt (fail closed, repaired) is exactly one the
    monotonic writer replaces.
    """
    if raw is not None and not isinstance(raw, (str, bytes)):
        raw = str(raw)
    parsed = parse_subject_state(raw)
    if parsed is None:
        return None
    kind, value = parsed
    return kind, int((value * _US).to_integral_value(rounding=ROUND_CEILING))


def _parse_mark_us(raw: Any) -> int | None:
    """The revocation instant of an ``after`` value (µs), else ``None``
    (absent, corrupt or ``blocked``)."""
    state = _parse_state(raw)
    if state is None or state[0] != STATE_AFTER:
        return None
    return state[1]


def state_covers(raw: Any, mark_us: int) -> bool:
    """True when the stored value already revokes up to ``mark_us``: a
    ``blocked`` value (it outranks every revocation) or an ``after`` value at
    or past it."""
    state = _parse_state(raw)
    if state is None:
        return False
    return state[0] == STATE_BLOCKED or state[1] >= mark_us


def state_is_blocked(raw: Any) -> bool:
    """True for a ``blocked`` record value."""
    state = _parse_state(raw)
    return state is not None and state[0] == STATE_BLOCKED


def _default_mark_us() -> int:
    """``now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS`` on this process's clock."""
    return _seconds_to_us(time.time() + _clock_skew_margin())


#: Revocation marks this context knows per subject (µs): the issuance check
#: read them, or this request scheduled them. A token minted afterwards in the
#: same context is stamped after the mark (see :func:`create_access_token`).
_issue_floors: ContextVar[dict[str, int] | None] = ContextVar(
    "sandloom_revocation_issue_floors", default=None
)


def _note_issue_floor(subject: Any, mark_us: int | None) -> None:
    if subject is None or mark_us is None:
        return
    key = str(subject)
    floors = dict(_issue_floors.get() or {})
    if floors.get(key, -1) >= mark_us:
        return
    floors[key] = mark_us
    _issue_floors.set(floors)


def _issue_instant_us(subject: Any, now: datetime) -> int:
    now_us = _datetime_us(now)
    if subject is None:
        return now_us
    floor = (_issue_floors.get() or {}).get(str(subject))
    if floor is None:
        return now_us
    return max(now_us, floor + 1)


def note_revocation_mark_seen(subject: Any, raw_mark: Any) -> None:
    """Record a subject-state value an ISSUER read for ``subject`` (kernel use).

    Called by :func:`sandloom.core.auth.blocks.ensure_user_not_blocked` — the
    live check every issuance path runs before collecting claims — so the
    token it then mints is stamped after an ``after`` mark (the claims it
    carries were collected after the revocation was visible).
    """
    _note_issue_floor(subject, _parse_mark_us(raw_mark))


def revocation_mark_is_corrupt(raw_mark: Any) -> bool:
    """True when a subject-state value exists but cannot be parsed (kernel use)."""
    return raw_mark is not None and _parse_state(raw_mark) is None


def _issued_us(claims: dict[str, Any]) -> int | None:
    """The token's issue instant in microseconds (``iat_us``; ``None`` when
    absent: such a token proves no order against a revocation mark)."""
    precise = claims.get(IAT_US_CLAIM)
    if isinstance(precise, int) and not isinstance(precise, bool):
        return precise
    return None
