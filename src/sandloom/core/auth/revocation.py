"""Writing revocation marks: immediate, on commit (with a durable outbox
record), and the durable applier that replays a deferred one."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from sandloom.core.auth.marks import (
    _GAUGE_REVOCATION_PENDING,
    _METRIC_REVOCATION_APPLIED,
    _METRIC_REVOCATION_APPLIER_FAILURES,
    _METRIC_REVOCATION_DEFERRED,
    _METRIC_REVOCATION_IMMEDIATE_REFUSED,
    _METRIC_REVOCATION_MALFORMED,
    _METRIC_REVOCATION_REMARK_OVERTOOK_FLOOR,
    TOKENS_REVOKED_CHANNEL,
    RevocationUnavailableError,
    _clock_skew_margin,
    _default_mark_us,
    _format_mark,
    _note_issue_floor,
    _revocation_ttl_seconds,
    _seconds_to_us,
    state_covers,
    subject_key,
)
from sandloom.core.config.settings import settings
from sandloom.core.eventing.delivery import PoisonEventError
from sandloom.core.infra import access as _access
from sandloom.core.infra.contracts import (
    CacheProvider,
    portable_set_subject_state,
)
from sandloom.core.unit_of_work.effects import publish_event
from sandloom.core.unit_of_work.hooks import _savepoint_token, on_commit
from sandloom.core.unit_of_work.request import (
    current_request_scope,
    record_self_revocation,
)
from sandloom.core.unit_of_work.scope import _resolve_scoped_session

logger = logging.getLogger(__name__)


async def revoke_user_tokens(user_id: Any, *, valid_after: float | None = None) -> bool:
    """Invalidate every access token issued to ``user_id`` up to now.

    Writes an ``after`` value into the framework-owned subject-state record
    ``sandloom:auth:subject:{user_id}`` that every framework token check honours
    (``current_context`` / ``require(...)`` / ``get_current_user_id`` /
    ``TenantScope``).

    **The rule.** A token is revoked iff its issue instant is ``<=`` the mark:
    ``iat_us`` (microseconds; a token without it cannot prove it came after,
    so it is revoked by any mark). Without ``valid_after`` the mark is
    ``now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS``: an issuer whose clock runs
    up to that margin ahead of ours cannot stamp a token minted BEFORE the
    revocation past it. The margin costs a framework issuer nothing — its
    issuance check reads the mark and stamps the new token after it — but a
    token minted by a path that skipped that check within the margin is
    rejected (the client refreshes again).

    Takes effect immediately (a cache write, not part of the UoW). Tie it to a
    database change with ``await revoke_user_tokens_on_commit(uid, reason=...)``
    (``sandloom.core.di``) so a rolled-back change revokes nobody — and so a failed
    write is retried and then applied durably. Returns ``False`` (logged) when
    the cache is unreachable.

    The mark NEVER moves backwards, even under concurrent revocations, and
    never lifts a block: it is written with the cache's monotonic
    ``set_subject_state`` (one atomic round trip on Redis; a read / ``add`` /
    ``compare_and_set`` loop elsewhere that retries while the stored value
    ranks below ours), so a concurrent lower mark can never overwrite a higher
    one, a ``blocked`` record outranks it, and contention alone never fails a
    revocation.
    """
    mark_us = (
        _seconds_to_us(valid_after) if valid_after is not None else _default_mark_us()
    )
    _note_issue_floor(user_id, mark_us)
    try:
        provider = _access.security_cache()
    except Exception:  # cache outage must not break the action
        logger.error(
            "Could not record token revocation for subject %s", user_id, exc_info=True
        )
        return False
    return await _record_revocation_mark(provider, user_id, mark_us) is not None


async def _record_revocation_mark(
    provider: Any, user_id: Any, mark_us: int
) -> bool | None:
    """Monotonically write the ``after`` value of :func:`revoke_user_tokens`.

    One cache ``set_subject_state``: the value is replaced only by a LATER
    ``after`` one, never over a ``blocked`` record, in a single atomic round
    trip on drivers with the ``subject_state`` feature (Redis: a Lua script).
    Concurrent revocations therefore never fail each other and the stored
    mark always ends at the maximum. Duck-typed caches without the op get the
    portable read / add / compare-and-set loop
    (:func:`~sandloom.core.infra.contracts.portable_set_subject_state`). Only a
    cache error (outage) fails (``None``) — or a cache without
    ``compare_and_set``: a plain overwrite could lower the mark or lift a
    block, so the write is refused (logged) instead.

    Returns ``None`` on failure, else whether the stored value may have MOVED:
    ``False`` only when the stored value already covered ``mark_us``.
    """
    key = subject_key(user_id)
    ttl = _revocation_ttl_seconds()
    value = _format_mark(mark_us)
    try:
        own = getattr(type(provider), "set_subject_state", None)
        if own is not None and own is not CacheProvider.set_subject_state:
            stored = await provider.set_subject_state(key, value, ttl_seconds=ttl)
        else:
            stored = await portable_set_subject_state(
                provider, key, value, ttl_seconds=ttl
            )
    except Exception:  # cache outage must not break the action
        logger.error(
            "Could not record token revocation for subject %s", user_id, exc_info=True
        )
        return None
    return stored == value or not state_covers(stored, mark_us)


async def _publish_pending_revocation(
    user_id: Any, mark: float, reason: str, database: str | None
) -> None:
    """Write the durable revocation record into the current UoW's outbox."""
    if _resolve_scoped_session(database, op="publish_event") is None:
        return  # no transaction: the write below runs now, nothing to persist
    await publish_event(
        TOKENS_REVOKED_CHANNEL,
        {"user_id": str(user_id), "valid_after": mark, "reason": reason},
        database=database,
    )


async def check_then_revoke(user_id: Any, database: str | None) -> None:
    """Self-revocation, "check, then revoke" (ADR-0009 item 3).

    When the request's authenticated subject revokes or blocks ITSELF inside
    a unit the commit-time re-check covers, the kernel FIRST runs that check
    live, here (``401`` revoked / blocked, ``503`` on a fail-closed outage),
    before anything is written — the ``require_immediate`` early write
    included. If it passes, the subject is recorded in the unit (in the
    savepoint that made the call: a rolled-back savepoint discards it), and
    the unit's COMMIT skips the subject: the request's own revocation never
    refuses its own commit, while a withdrawal by someone else that landed
    before this call is still seen.
    """
    scope = current_request_scope()
    if scope is None or scope.check is None or scope.subject != str(user_id):
        return
    if scope.intent is not None and not scope.at_commit:
        return
    session = _resolve_scoped_session(database, op="revoke_user_tokens_on_commit")
    if session is None:
        return
    await scope.check()
    record_self_revocation(session, scope.subject, _savepoint_token(session))


async def revoke_user_tokens_on_commit(
    user_id: Any,
    *,
    reason: str,
    valid_after: float | None = None,
    database: str | None = None,
    require_immediate: bool = False,
) -> float:
    """Schedule :func:`revoke_user_tokens` to run after the current UoW commits.

    The one sanctioned way to tie a token revocation to a database change (role
    revoked, member removed, password changed, account deactivated): nothing is
    revoked when the change rolls back. ``valid_after`` (unix seconds, sub-second
    precision; default ``now + AUTH_REVOCATION_CLOCK_SKEW_SECONDS``) is fixed
    NOW, not at commit, and returned. Tokens the same request mints afterwards
    (the fresh pair of a password change) are stamped after it and stay valid.

    Delivery is at-least-once:

    * inside a UoW the pending revocation is ALSO written to the outbox
      (``core.auth.tokens_revoked.v1``) in the same transaction, so it commits
      iff the change commits;
    * after commit the mark is written with ``AUTH_REVOCATION_WRITE_ATTEMPTS``
      attempts (exponential backoff from ``AUTH_REVOCATION_RETRY_BASE_SECONDS``);
    * if every attempt fails (cache outage) it is logged, counted in
      ``auth_revocation_deferred_total`` (alert on it), and the durable event
      is applied later by the session owner's consumer
      (:func:`apply_pending_revocation`; identity registers it), redelivered
      until the mark is written. Marks are monotonic, so a late or repeated
      application is harmless. Until then old tokens may still pass (fail-open
      outage, or the cache is back before the applier ran): the window is
      bounded by the relay / consumer retry policy (``docs/security-model.md``).

    ``require_immediate=True`` closes that window for changes whose revocation
    is a required guarantee (membership removal, role change): the mark is
    written SYNCHRONOUSLY, before commit, and if that write fails
    :class:`RevocationUnavailableError` (``503``) is raised so the change is
    refused and rolls back — a committed change therefore always has its mark.
    Call it as the last fallible step of the UoW: a rollback AFTER it leaves
    the user logged out (the safe direction; they re-authenticate). After
    commit a second mark ``max(valid_after, commit time + skew)`` is written,
    so a token an issuer minted between the early mark and the commit (claims
    read before the change was visible) is revoked too; tokens this request
    mints after the call are stamped past that second mark PROVIDED the UoW
    commits within ``AUTH_REVOCATION_CLOCK_SKEW_SECONDS`` of the call. A slower
    commit revokes them too (fail-closed: the client re-authenticates) and is
    counted in ``auth_revocation_remark_overtook_floor_total`` with a WARNING.
    """
    await check_then_revoke(user_id, database)
    margin = _clock_skew_margin()
    mark = float(valid_after) if valid_after is not None else time.time() + margin
    mark_us = _seconds_to_us(mark)
    if require_immediate:
        if not await revoke_user_tokens(user_id, valid_after=mark):
            _METRIC_REVOCATION_IMMEDIATE_REFUSED.inc()
            logger.error(
                "Refusing change: the token revocation of subject %s (%s) could "
                "not be written before commit",
                user_id,
                reason,
            )
            raise RevocationUnavailableError(
                "token revocation store unavailable: change refused"
            )
        _METRIC_REVOCATION_APPLIED.labels(path="immediate").inc()
    await _publish_pending_revocation(user_id, mark, reason, database)

    # This context mints AFTER the revocation from here on (same request) —
    # past the post-commit re-mark too when the revocation is immediate.
    issue_floor_us = mark_us + (_seconds_to_us(margin) if require_immediate else 0)

    async def _revoke() -> None:
        target = mark
        if require_immediate:
            target = max(mark, time.time() + margin)
            if _seconds_to_us(target) >= issue_floor_us:
                # Inherent trade-off, resolved fail-closed: an issuer's token
                # minted from pre-change claims before the commit cannot be
                # told apart from this request's own fresh pair.
                _METRIC_REVOCATION_REMARK_OVERTOOK_FLOOR.inc()
                logger.warning(
                    "Revocation of subject %s (%s) committed more than %.3fs "
                    "after it was scheduled: tokens this request minted are "
                    "revoked too and the client must re-authenticate (call "
                    "revoke_user_tokens_on_commit as the last step before commit)",
                    user_id,
                    reason,
                    margin,
                )
        attempts = max(int(getattr(settings, "auth_revocation_write_attempts", 1)), 1)
        base = max(float(getattr(settings, "auth_revocation_retry_base_seconds", 0)), 0)
        for attempt in range(attempts):
            if await revoke_user_tokens(user_id, valid_after=target):
                _METRIC_REVOCATION_APPLIED.labels(path="on_commit").inc()
                return
            if attempt + 1 < attempts and base > 0:
                await asyncio.sleep(base * (2**attempt))
        _METRIC_REVOCATION_DEFERRED.inc()
        logger.error(
            "Could not record token revocation for subject %s (%s) after %d "
            "attempts; deferred to the durable applier (%s)",
            user_id,
            reason,
            attempts,
            TOKENS_REVOKED_CHANNEL,
        )

    _note_issue_floor(user_id, issue_floor_us)
    await on_commit(_revoke, database=database)
    return mark


#: Durable revocations this process failed to apply, by subject: the instant
#: (unix seconds) of the revoking change. Cleared when a later apply succeeds.
#: Deliberately PER PROCESS (each gunicorn worker reports its own /ready and
#: the ``livemax`` gauge aggregates them): what it reports is a cache outage,
#: so the shared state it could live in is the very store that is down. An
#: entry clears when this process's retry lands — or, since ANOTHER process
#: may apply the delivery instead (a Streams reclaim hands an idle worker's
#: pending entry to a healthy one, which acks it), when
#: :func:`reconcile_pending_revocations` sees the stored mark already covers
#: it. Without that, a worker whose delivery was taken over stayed degraded
#: forever.
_stuck_revocations: dict[str, float] = {}
#: The mark (microseconds) each stuck subject still needs, for the reconcile.
_stuck_marks: dict[str, int] = {}
#: Most subjects one reconcile reads (each is one cache GET).
_RECONCILE_MAX_SUBJECTS = 64


def pending_revocations() -> dict[str, float]:
    """``{"count", "oldest_seconds"}`` of durable revocations this process
    could not apply yet (kernel use: ``/ready`` and the pending gauge)."""
    now = time.time()
    oldest = max((now - since for since in _stuck_revocations.values()), default=0.0)
    oldest = max(oldest, 0.0)
    _GAUGE_REVOCATION_PENDING.set(oldest)
    return {"count": float(len(_stuck_revocations)), "oldest_seconds": oldest}


async def apply_pending_revocation(payload: dict[str, Any]) -> None:
    """Apply a durable ``core.auth.tokens_revoked.v1`` record (consumer body).

    Idempotent (the mark is monotonic: an already-covered mark is a no-op).
    When the mark cannot be written it raises
    :class:`RevocationUnavailableError`, so the delivery is retried — the
    session owner registers the consumer with ``dead_letter=False``: it is
    retried FOREVER (capped backoff), never parked in the DLQ, while
    ``auth_revocation_pending_seconds`` and an ERROR log per attempt report it
    and ``/ready`` answers degraded. A payload that can NEVER apply (malformed)
    raises :class:`~sandloom.core.eventing.delivery.PoisonEventError`: it is
    dead-lettered at once, logged CRITICAL and counted in
    ``auth_revocation_malformed_total``
    (runbook: ``docs/operations/operator-reference.md``).
    """
    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    valid_after = payload.get("valid_after") if isinstance(payload, dict) else None
    mark: float | None = None
    if (
        user_id
        and isinstance(valid_after, (int, float, str))
        and not isinstance(valid_after, bool)
    ):
        try:
            mark = float(valid_after)
        except ValueError:
            mark = None
    if mark is None or not math.isfinite(mark):
        _METRIC_REVOCATION_MALFORMED.inc()
        logger.critical(
            "Malformed durable revocation can never be applied; dead-lettering "
            "it (%s): %r",
            TOKENS_REVOKED_CHANNEL,
            payload,
        )
        raise PoisonEventError(f"malformed {TOKENS_REVOKED_CHANNEL} payload")
    subject = str(user_id)
    if not await revoke_user_tokens(user_id, valid_after=mark):
        _METRIC_REVOCATION_APPLIER_FAILURES.inc()
        since = min(
            _stuck_revocations.get(subject, math.inf), mark - _clock_skew_margin()
        )
        _stuck_revocations[subject] = since
        _stuck_marks[subject] = max(_stuck_marks.get(subject, 0), _seconds_to_us(mark))
        age = pending_revocations()["oldest_seconds"]
        logger.error(
            "Durable revocation of subject %s still pending (oldest pending "
            "revocation %.1f s old); retrying with capped backoff — revoked "
            "tokens may be accepted where the cache is reachable",
            user_id,
            age,
        )
        raise RevocationUnavailableError(
            f"could not apply the pending revocation of subject {user_id}"
        )
    _stuck_revocations.pop(subject, None)
    _stuck_marks.pop(subject, None)
    pending_revocations()
    _METRIC_REVOCATION_APPLIED.labels(path="applier").inc()


async def reconcile_pending_revocations() -> None:
    """Drop stuck entries whose mark another process already stored.

    Run by ``/ready`` before it reports :func:`pending_revocations` (a no-op
    while nothing is stuck). Reads each stuck subject's live mark (bounded to
    :data:`_RECONCILE_MAX_SUBJECTS` reads); an entry clears only when the
    stored mark is at or after the one this process failed to write. A cache
    error stops the reconcile: the outage the entries report is still on.
    """
    if not _stuck_marks:
        return
    try:
        provider = _access.security_cache()
        for subject in list(_stuck_marks)[:_RECONCILE_MAX_SUBJECTS]:
            raw = await provider.get(subject_key(subject))
            wanted = _stuck_marks.get(subject)
            if wanted is not None and state_covers(raw, wanted):
                _stuck_marks.pop(subject, None)
                _stuck_revocations.pop(subject, None)
    except Exception:  # still an outage: keep reporting it
        logger.debug("pending-revocation reconcile skipped", exc_info=True)
    pending_revocations()
