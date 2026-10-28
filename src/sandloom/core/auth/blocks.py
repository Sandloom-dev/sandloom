"""The emergency user block.

:func:`block_user` writes a ``blocked`` value into the subject-state record
(:mod:`sandloom.core.auth.marks`) that EVERY authenticated request reads live from
the shared cache, so it takes effect on the very next request everywhere. A
``blocked`` value ranks above every revocation, so no revocation (concurrent,
late, redelivered) ever unblocks; :func:`unblock_user` replaces it by
compare-and-set after commit, never by a delete. Block / unblock are audited
(``record_audit(..., required=True)``). Token issuers call
:func:`ensure_user_not_blocked` before minting.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status

from sandloom.core.auth import revocation as _revocation
from sandloom.core.auth.marks import (
    RevocationUnavailableError,
    _clock_skew_margin,
    _format_blocked,
    _format_mark,
    _revocation_ttl_seconds,
    _seconds_to_us,
    note_revocation_mark_seen,
    revocation_mark_is_corrupt,
    state_is_blocked,
    subject_key,
)
from sandloom.core.config.settings import settings
from sandloom.core.infra import access as _access
from sandloom.core.infra.contracts import CacheProvider, portable_set_subject_state
from sandloom.core.unit_of_work.effects import publish_event, record_audit
from sandloom.core.unit_of_work.hooks import on_commit

logger = logging.getLogger(__name__)

#: Core event published (transactional outbox) by :func:`block_user`; the
#: session owner (identity) revokes the user's refresh tokens on it.
USER_BLOCKED_CHANNEL = "core.auth.user_blocked.v1"


class BlockUnavailableError(RuntimeError):
    """The emergency-block mark could not be written / removed (cache outage)."""

    http_status = 503
    detail = "Account security state temporarily unavailable"


async def _write_state(provider: Any, user_id: Any, value: str, ttl: int | None) -> str:
    """Monotonic write of the subject-state record (``set_subject_state``)."""
    key = subject_key(user_id)
    own = getattr(type(provider), "set_subject_state", None)
    if own is not None and own is not CacheProvider.set_subject_state:
        return str(await provider.set_subject_state(key, value, ttl_seconds=ttl))
    return await portable_set_subject_state(provider, key, value, ttl_seconds=ttl)


async def block_user(
    user_id: Any,
    *,
    reason: str,
    actor_id: Any = None,
    database: str | None = None,
) -> None:
    """Block ``user_id`` NOW: every authenticated request of theirs gets 401.

    The ``blocked`` value of the subject-state record is read live on every
    authenticated request, so it applies on the very next request in every
    process — including tokens minted after the block. It stays until
    :func:`unblock_user` (no TTL) and outranks every revocation.

    Must run inside a Unit of Work: the audit record
    (``auth.user.blocked``, actor + reason), the immediate token revocation
    and the event are all written BEFORE the mark, and the mark is the LAST
    step — a failure of any earlier step leaves no mark behind the rolled-back
    audit (no block happens unaudited). A cache outage raises
    :class:`BlockUnavailableError` (503) and the UoW rolls the audit back.
    Call it as the last step of the request (the mark itself is not
    transactional).

    On commit it also ends the user's sessions, so lifting the block does not
    revive them: every access token issued so far is revoked
    (``revoke_user_tokens_on_commit``) and ``core.auth.user_blocked.v1`` is
    published (transactional outbox) — identity revokes the user's refresh
    tokens on it, locally or in its own service.
    """
    reason = _require_reason(reason)
    # A self-block is checked live FIRST, before the audit row (ADR-0009).
    await _revocation.check_then_revoke(user_id, database)
    record_audit(
        "auth.user.blocked",
        actor_id=actor_id,
        resource_type="user",
        resource_id=str(user_id),
        data={"reason": reason},
        required=True,
        database=database,
    )
    blocked_at = time.time()
    # Sessions end too, so lifting the block does not revive them: every
    # access token issued so far is revoked once this UoW commits, and the
    # session owner (identity) revokes the user's refresh tokens on the event
    # (transactional outbox -> its durable consumer, local or remote).
    try:
        await _revocation.revoke_user_tokens_on_commit(
            user_id,
            reason=f"user blocked: {reason}",
            database=database,
            require_immediate=True,
        )
    except RevocationUnavailableError as exc:  # same cache: one 503 contract
        raise BlockUnavailableError(
            f"could not block user {user_id}: cache unavailable"
        ) from exc
    await publish_event(
        USER_BLOCKED_CHANNEL,
        {
            "user_id": str(user_id),
            "actor_id": None if actor_id is None else str(actor_id),
            "blocked_at": int(blocked_at),
        },
        database=database,
    )
    # The mark goes LAST: had it been written first, a failing revocation /
    # publish would roll the audit back and leave a permanent, unaudited block.
    try:
        await _write_state(
            _access.security_cache(),
            user_id,
            _format_blocked(_seconds_to_us(blocked_at)),
            None,
        )
    except Exception as exc:  # surfaced as 503
        raise BlockUnavailableError(
            f"could not block user {user_id}: cache unavailable"
        ) from exc
    logger.warning(
        "Emergency block of user %s by %s: %s", user_id, actor_id or "system", reason
    )


async def unblock_user(
    user_id: Any,
    *,
    reason: str,
    actor_id: Any = None,
    database: str | None = None,
) -> None:
    """Lift an emergency block (audited ``auth.user.unblocked``, same rules).

    The ``blocked`` value is replaced only AFTER the UoW commits
    (``on_commit``), by compare-and-set with ``after:<now + skew>`` (with the
    revocation TTL) — never deleted, so every token issued before the unblock
    stays revoked and a concurrent writer is never overwritten blindly. A
    rolled-back unblock leaves the user blocked, never unblocked without an
    audit row. Fail-safe: if the post-commit swap fails (cache outage) the
    user stays blocked — logged as an error; run the unblock again.
    """
    reason = _require_reason(reason)
    record_audit(
        "auth.user.unblocked",
        actor_id=actor_id,
        resource_type="user",
        resource_id=str(user_id),
        data={"reason": reason},
        required=True,
        database=database,
    )

    async def _lift() -> None:
        try:
            await _swap_blocked_for_after(_access.security_cache(), user_id)
        except Exception:  # fail-safe: the user stays blocked
            logger.error(
                "Unblock of user %s committed but the block mark could not be "
                "deleted (cache unavailable); the user STAYS BLOCKED — run the "
                "unblock again",
                user_id,
                exc_info=True,
            )
            return
        logger.warning(
            "Emergency block of user %s lifted by %s: %s",
            user_id,
            actor_id or "system",
            reason,
        )

    await on_commit(_lift, database=database)


async def _swap_blocked_for_after(provider: Any, user_id: Any) -> bool:
    """Replace a ``blocked`` record by ``after:<now + skew>`` (compare-and-set).

    Retries while another writer changes the record and it is still
    ``blocked``; returns False when the record is not blocked (nothing to
    lift). Never deletes: the ``after`` value revokes every token issued
    before the unblock.
    """
    key = subject_key(user_id)
    for _ in range(16):
        current = await provider.get(key)
        if current is None or not state_is_blocked(current):
            return False
        after = _format_mark(_seconds_to_us(time.time() + _clock_skew_margin()))
        if await provider.compare_and_set(
            key, current, after, ttl_seconds=_revocation_ttl_seconds()
        ):
            return True
    raise RuntimeError(f"unblock of {user_id}: the record kept changing")


async def is_user_blocked(user_id: Any) -> bool:
    """Live read of the subject-state record (raises on a cache outage)."""
    return state_is_blocked(await _access.security_cache().get(subject_key(user_id)))


async def _read_issuance_marks(user_id: Any) -> bool:
    """Live block check for an ISSUER; also notes the revocation mark.

    One ``GET`` of the subject-state record: ``blocked`` refuses; an ``after``
    mark is remembered for this context so the token the issuer mints next is
    stamped after it (:mod:`sandloom.core.auth.tokens`). Raises on a cache outage.
    """
    provider = _access.security_cache()
    mark_raw = await provider.get(subject_key(user_id))
    if state_is_blocked(mark_raw):
        return True
    note_revocation_mark_seen(user_id, mark_raw)
    if revocation_mark_is_corrupt(mark_raw):
        # Every token of the subject is rejected while the mark is unreadable
        # (fail-closed). Repair it NOW (replace it with ``now + skew`` by
        # compare-and-set; the floor is noted) so the token minted next is
        # accepted; if the repair fails that token is rejected too — never
        # an accepted stale one.
        await _revocation.revoke_user_tokens(user_id)
    return False


async def ensure_user_not_blocked(user_id: Any) -> None:
    """Refuse to authenticate a blocked user: ``401 "Account blocked"``.

    For token ISSUERS (password / SSO / 2FA login, refresh-token exchange):
    call it before minting or rotating anything — and before collecting the
    token's claims. A live read of the subject-state record — never a cached
    copy — so the token minted next in this context is stamped after any
    revocation it saw. On a
    cache outage ``AUTH_REVOCATION_FAIL_CLOSED`` decides, like the per-request
    check: closed answers ``503``, open logs and lets the issuance proceed.
    """
    try:
        blocked = await _read_issuance_marks(user_id)
    except Exception as exc:
        if settings.revocation_fail_closed:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication temporarily unavailable",
            ) from exc
        logger.warning("Emergency-block lookup unavailable (fail-open)", exc_info=True)
        return
    if blocked:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Account blocked",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a non-empty reason is required")
    return reason.strip()
