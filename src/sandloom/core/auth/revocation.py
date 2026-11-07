"""Writing revocation marks: immediate, on commit (with a durable outbox
record), and the durable applier that replays a deferred one."""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
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
    PHASE_EARLY,
    PHASE_SETTLED,
    STATE_BLOCKED,
    TOKENS_REVOKED_CHANNEL,
    RevocationUnavailableError,
    _clock_skew_margin,
    _default_mark_us,
    _format_mark,
    _format_revocation,
    _issue_floors,
    _note_issue_floor,
    _parse_mark_us,
    _parse_state,
    _revocation_ttl_seconds,
    _seconds_to_us,
    _state_tail,
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
from sandloom.core.unit_of_work.hooks import _ON_COMMIT_KEY, _savepoint_token, on_commit
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
    revocation. When the stored mark is already at or past ours (a mark set
    into the future by an earlier revocation), the mark moves one microsecond
    past it: an issuer that saw the stored mark stamped its token there, from
    claims read before this revocation (:func:`_record_revocation_mark`).
    """
    mark_us = (
        _seconds_to_us(valid_after) if valid_after is not None else _default_mark_us()
    )
    stored = await _write_revocation(
        user_id, mark_us, _new_revocation_id() + PHASE_SETTLED
    )
    return stored is not None


#: ``session.info`` key of the revocation identities of a unit, per subject:
#: ``(on-commit queue, {subject: id})``. Every revocation of one subject in
#: one unit shares an identity — they commit together, so their post-commit
#: writes (and the durable applier's replays) recognise each other's write
#: instead of each lifting the mark again. The queue anchors the entry to
#: the unit's current commit: the kernel replaces it when the unit commits,
#: so a session reused after a commit never shares an identity across commits.
_REVOCATION_IDS_KEY = "sandloom_revocation_ids"


def _new_revocation_id() -> str:
    """A fresh revocation identity: decimal digits, which extend a mark's
    instant past the microsecond
    (:func:`~sandloom.core.auth.marks._format_revocation`)."""
    return str(secrets.randbits(62))


def _unit_revocation_id(user_id: Any, database: str | None) -> str:
    """The identity of ``user_id``'s revocations in the current unit (a fresh
    one outside a unit)."""
    session = _resolve_scoped_session(database, op="revoke_user_tokens_on_commit")
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return _new_revocation_id()
    queue = info.setdefault(_ON_COMMIT_KEY, [])
    entry = info.get(_REVOCATION_IDS_KEY)
    if not entry or entry[0] is not queue:
        entry = (queue, {})
        info[_REVOCATION_IDS_KEY] = entry
    ids: dict[str, str] = entry[1]
    return ids.setdefault(str(user_id), _new_revocation_id())


async def _write_revocation(
    user_id: Any, mark_us: int, tail: str | None, *, replay: bool = False
) -> str | None:
    """Write a revocation of ``user_id`` up to ``mark_us`` with identity
    ``tail`` (:func:`_record_revocation_mark`); the stored value after the
    write, ``None`` on failure. The floor this context mints past is the
    stored value's instant (a token minted next is stamped after it)."""
    _note_issue_floor(user_id, mark_us)
    try:
        provider = _access.security_cache()
    except Exception:  # cache outage must not break the action
        logger.error(
            "Could not record token revocation for subject %s", user_id, exc_info=True
        )
        return None
    stored = await _record_revocation_mark(
        provider, user_id, mark_us, tail=tail, replay=replay
    )
    if stored is not None:
        _note_issue_floor(user_id, _parse_mark_us(stored))
    return stored


async def _record_revocation_mark(
    provider: Any,
    user_id: Any,
    mark_us: int,
    *,
    tail: str | None,
    replay: bool = False,
) -> str | None:
    """Write the ``after`` value of a revocation up to ``mark_us``.

    **Strictly above what an issuer may have stamped.** An issuer that read a
    stored mark ``S`` stamps the token it mints at ``S + 1`` µs — whatever
    the claims it carries (:func:`~sandloom.core.auth.marks._issue_instant_us`).
    A revocation is written at ``now + skew`` of ITS clock, so a change
    committed after such a mint can carry a mark ``<= S`` (a slower clock,
    or ``S`` itself in the future): kept at the maximum, the stale token
    would outlive it. So the value written reads as ``max(mark_us, S + 1)``
    — one microsecond past the stored one when that already covers
    ``mark_us`` — and carries ``tail``, the revocation's identity
    (:func:`~sandloom.core.auth.marks._format_revocation`). A write that
    finds its own identity already stored at or past ``mark_us`` is done
    (the post-commit write and the durable applier replay one revocation:
    neither lifts the mark again, so the revoking request's own fresh pair,
    stamped past that value, stays valid).

    ``replay`` (the durable applier) re-applies a revocation whose
    post-commit write normally already ran: a stored value covering
    ``mark_us`` that was written after SOME change committed (a settled
    tail, or a value without a tail) counts as applied, so a late replay of
    an older revocation never lifts a newer revocation's mark (and revokes
    the fresh pair that one's request minted past it). Only a covering
    value written BEFORE a commit (a ``require_immediate`` early write: its
    post-commit re-mark failed) is lifted past. ``tail=None`` (a durable
    record without an identity, from an older producer) writes ``mark_us``
    unless the stored value covers it.

    One read, then the cache's monotonic ``set_subject_state``: the value is
    replaced only by a HIGHER ``after`` one, never over a ``blocked``
    record, in a single atomic round trip on drivers with the
    ``subject_state`` feature (Redis: a Lua script); duck-typed caches
    without the op get the portable read / add / compare-and-set loop
    (:func:`~sandloom.core.infra.contracts.portable_set_subject_state`).
    A concurrent writer that stored a higher value first wins, which covers
    ours: it was written after our read, so past every stamp we had to
    cover. Concurrent revocations therefore never fail each other and the
    mark never moves backwards. Only a cache error (outage) fails
    (``None``) — or a cache without ``compare_and_set``: a plain overwrite
    could lower the mark or lift a block, so the write is refused (logged).

    Returns the value the record holds after the call, ``None`` on failure.
    """
    key = subject_key(user_id)
    ttl = _revocation_ttl_seconds()
    try:
        current = await provider.get(key)
        state = _parse_state(current)
        if state is not None and state[0] == STATE_BLOCKED:
            return _text(current)
        reading = None if state is None else state[1]
        if reading is not None and reading >= mark_us:
            held = _state_tail(current)
            if tail is None or held == tail:
                return _text(current)
            if replay and (held is None or held.endswith(PHASE_SETTLED)):
                return _text(current)
        if tail is None:
            value = _format_mark(mark_us)
        else:
            target = mark_us if reading is None else max(mark_us, reading + 1)
            value = _format_revocation(target, tail)
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
    return _text(stored)


def _text(raw: Any) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


async def _publish_pending_revocation(
    user_id: Any, mark: float, reason: str, database: str | None, revocation_id: str
) -> None:
    """Write the durable revocation record into the current UoW's outbox."""
    if _resolve_scoped_session(database, op="publish_event") is None:
        return  # no transaction: the write below runs now, nothing to persist
    await publish_event(
        TOKENS_REVOKED_CHANNEL,
        {
            "user_id": str(user_id),
            "valid_after": mark,
            "reason": reason,
            "revocation_id": revocation_id,
        },
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
    (the fresh pair of a password change) are stamped after it and stay valid
    — also when the stored mark is already past it and the write after commit
    lands one microsecond past that (:func:`_record_revocation_mark`): the
    early write of ``require_immediate`` reads the stored mark, and without it
    the request's issuance check (``ensure_user_not_blocked``) has. Every
    revocation of one subject in one unit shares an identity, written into
    the value, so the post-commit write and the durable applier's replay
    recognise each other and never move the mark past that pair.

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
    read before the change was visible) is revoked too (one microsecond past
    the early mark when that is higher); tokens this request
    mints after the call are stamped past that second mark PROVIDED the UoW
    commits within ``AUTH_REVOCATION_CLOCK_SKEW_SECONDS`` of the call. A slower
    commit revokes them too (fail-closed: the client re-authenticates) and is
    counted in ``auth_revocation_remark_overtook_floor_total`` with a WARNING.
    """
    await check_then_revoke(user_id, database)
    margin = _clock_skew_margin()
    mark = float(valid_after) if valid_after is not None else time.time() + margin
    mark_us = _seconds_to_us(mark)
    revocation_id = _unit_revocation_id(user_id, database)
    seen_us = (_issue_floors.get() or {}).get(str(user_id))
    if require_immediate:
        early = await _write_revocation(user_id, mark_us, revocation_id + PHASE_EARLY)
        if early is None:
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
        early_us = _parse_mark_us(early)
        # Past the post-commit re-mark: ``max(mark, commit + skew)``, or one
        # microsecond past the early value when that already covers it.
        issue_floor_us = max(
            mark_us + _seconds_to_us(margin),
            (mark_us if early_us is None else early_us) + 1,
        )
    elif seen_us is not None and seen_us >= mark_us:
        # The stored mark this context saw already covers ours: the write
        # after commit lands one microsecond past it.
        issue_floor_us = seen_us + 1
    else:
        issue_floor_us = mark_us
    await _publish_pending_revocation(user_id, mark, reason, database, revocation_id)
    settled = revocation_id + PHASE_SETTLED

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
            if await _write_revocation(user_id, _seconds_to_us(target), settled):
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

    # This context mints AFTER the revocation from here on (same request) —
    # past the post-commit write too.
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

    Idempotent: a stored mark that covers the record's and was written after
    a commit (this revocation's post-commit write, or another's) is left as
    it is; only a ``require_immediate`` early mark whose post-commit write was
    lost is lifted past (:func:`_record_revocation_mark`, ``replay``).
    When the mark cannot be written it raises
    :class:`RevocationUnavailableError`, so the delivery is retried — the
    session owner registers the consumer with ``dead_letter=False``: it is
    retried FOREVER (capped backoff), never parked in the DLQ, while
    ``auth_revocation_pending_seconds`` and an ERROR log per attempt report it
    and ``/ready`` answers degraded. A payload that can NEVER apply (malformed)
    raises :class:`~sandloom.core.eventing.delivery.PoisonEventError`: it is
    dead-lettered at once, logged CRITICAL and counted in
    ``auth_revocation_malformed_total``
    (runbook: ``docs/operate/operator-reference.md``).
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
    revocation_id = payload.get("revocation_id")
    tail = (
        revocation_id + PHASE_SETTLED
        if isinstance(revocation_id, str)
        and revocation_id.isascii()
        and revocation_id.isdigit()
        and len(revocation_id) <= 32
        else None
    )
    if (
        await _write_revocation(user_id, _seconds_to_us(mark), tail, replay=True)
        is None
    ):
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
