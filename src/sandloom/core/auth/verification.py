"""Checking an access token against its subject's state record: one live
cache ``GET`` (ADR-0009 item 2), memoised per request."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status

from sandloom.core.auth.bearer import _state_mapping
from sandloom.core.auth.marks import (
    _METRIC_REVOCATION_CORRUPT_MARK,
    _METRIC_REVOCATION_OUTLIVED_MARK_TTL,
    _US,
    STATE_BLOCKED,
    RevocationUnavailableError,
    _issued_us,
    _parse_state,
    _revocation_ttl_seconds,
    subject_key,
)
from sandloom.core.config.settings import settings
from sandloom.core.infra import access as _access
from sandloom.core.observability.metrics import auth_revocation_check_duration_seconds

logger = logging.getLogger(__name__)

# Attribute under which the per-request revocation verdict is memoised.
_REVOCATION_CACHE_ATTR = "_sandloom_jwt_revocation_cache"
# Child resolved once: the check runs on every authenticated request.
_REVOCATION_CHECK = auth_revocation_check_duration_seconds.labels("strict")

_outlived_ttl_logged = False


def _outlived_mark_ttl(claims: dict[str, Any]) -> bool:
    """True for a token older than the revocation-mark TTL (fail closed).

    The absence of a mark proves nothing for such a token: one that revoked
    it may have expired already — an issuer configured with a longer
    ``ACCESS_TOKEN_EXPIRE_MINUTES`` than the writer (config drift between
    services) would otherwise revive revoked tokens silently. In a consistent
    deployment no unexpired token is that old (the TTL outlives the access
    lifetime), so the common path costs one clock read and one comparison.
    """
    issued = _issued_us(claims)
    if issued is None:
        return False
    age = time.time() - issued / _US
    if age <= int(settings.access_token_expire_minutes) * 60:
        return False
    if age <= _revocation_ttl_seconds():
        return False
    _METRIC_REVOCATION_OUTLIVED_MARK_TTL.inc()
    global _outlived_ttl_logged
    if not _outlived_ttl_logged:
        _outlived_ttl_logged = True
        logger.error(
            "Rejecting an access token issued %.0fs ago: older than the "
            "revocation-mark TTL (%ss), so an expired mark could have revoked it. "
            "ACCESS_TOKEN_EXPIRE_MINUTES / AUTH_REVOCATION_MARK_TTL_SECONDS "
            "differ between services — keep them identical everywhere",
            age,
            _revocation_ttl_seconds(),
        )
    return True


def _verdict(raw: str | None, claims: dict[str, Any]) -> str:
    """The outcome of the record value ``raw`` for the token ``claims``.

    No record: the token passes unless it is older than the record lifetime
    (:func:`_outlived_mark_ttl`). ``blocked``: blocked, whatever the token.
    ``after``: revoked iff the token was issued at or before the instant (a
    token with no issue instant is revoked). A value that cannot be parsed
    FAILS CLOSED: some writer meant to revoke or block the subject and we
    cannot tell what, so every token is rejected (logged,
    ``auth_revocation_corrupt_mark_total``). The next issuance check
    (:func:`note_revocation_mark_seen` via ``ensure_user_not_blocked``)
    repairs the record, so a re-login works.
    """
    if raw is None:
        return TOKEN_REVOKED if _outlived_mark_ttl(claims) else TOKEN_OK
    state = _parse_state(raw)
    if state is None:
        _METRIC_REVOCATION_CORRUPT_MARK.inc()
        logger.error(
            "Corrupt subject-state record for subject %s: rejecting the token "
            "(fail-closed)",
            claims.get("sub"),
        )
        return TOKEN_REVOKED
    kind, instant_us = state
    if kind == STATE_BLOCKED:
        return TOKEN_BLOCKED
    issued = _issued_us(claims)
    if issued is None or issued <= instant_us:
        return TOKEN_REVOKED
    return TOKEN_OK


#: Outcomes of :func:`token_access_status`.
TOKEN_OK = "ok"  # noqa: S105 - verdict label, not a secret
TOKEN_REVOKED = "revoked"  # noqa: S105 - verdict label, not a secret
TOKEN_BLOCKED = "blocked"  # noqa: S105 - verdict label, not a secret


async def is_token_revoked(claims: dict[str, Any]) -> bool:
    """True when the token may not be used: revoked by its subject's record,
    or the subject is blocked (a blocked record outranks every revocation, so
    the token's own revocation instant is no longer known: fail closed).

    A token without ``sub`` is revoked. On a cache outage
    ``AUTH_REVOCATION_FAIL_CLOSED`` decides: fail closed raises
    :class:`RevocationUnavailableError` (the guards answer 503); fail open
    (the default outside prod) logs and accepts the token.
    """
    return await token_access_status(claims) != TOKEN_OK


async def token_access_status(claims: dict[str, Any]) -> str:
    """``"ok"`` / ``"revoked"`` / ``"blocked"`` for an access token's claims:
    one live cache ``GET`` of the subject-state record (it runs on every
    authenticated request). On a cache outage ``AUTH_REVOCATION_FAIL_CLOSED``
    decides: fail closed raises :class:`RevocationUnavailableError`, fail open
    logs and accepts the token."""
    sub = claims.get("sub")
    if not sub:
        return TOKEN_REVOKED
    try:
        raw = await _access.security_cache().get(subject_key(str(sub)))
    except Exception as exc:
        if settings.revocation_fail_closed:
            raise RevocationUnavailableError(
                "token revocation store unavailable"
            ) from exc
        logger.warning("Token revocation lookup unavailable (fail-open)", exc_info=True)
        return TOKEN_OK
    if raw is None and not _may_have_outlived(claims):
        return TOKEN_OK  # the common case: no record, a fresh token
    return _verdict(raw, claims)


def _may_have_outlived(claims: dict[str, Any]) -> bool:
    """A cheap pre-test of :func:`_outlived_mark_ttl`: only a token older than
    the access lifetime can be (it then decides)."""
    issued = _issued_us(claims)
    if issued is None:
        return False
    return time.time() - issued / _US > int(settings.access_token_expire_minutes) * 60


_INVALID_TOKEN = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


def _raise_for(outcome: str) -> None:
    """``401`` for a revoked / blocked outcome (RFC 6750 ``invalid_token``)."""
    if outcome == TOKEN_BLOCKED:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Account blocked",
            headers=_INVALID_TOKEN,
        )
    if outcome == TOKEN_REVOKED:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers=_INVALID_TOKEN,
        )


async def _live_status(claims: dict[str, Any]) -> str:
    """:func:`token_access_status`, a fail-closed outage as ``503``."""
    try:
        return await token_access_status(claims)
    except RevocationUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication temporarily unavailable",
        ) from exc


def _request_memo(request: Any) -> dict[Any, str] | None:
    """The request's revocation memo (created on first use; ``None`` for a
    request-like object without state)."""
    mapping = _state_mapping(request)
    if mapping is not None:
        memo = mapping.get(_REVOCATION_CACHE_ATTR)
        if memo is None:
            memo = mapping[_REVOCATION_CACHE_ATTR] = {}
        return memo
    state = getattr(request, "state", None)
    if state is None:
        return None
    memo = getattr(state, _REVOCATION_CACHE_ATTR, None)
    if memo is None:
        memo = {}
        setattr(state, _REVOCATION_CACHE_ATTR, memo)
    return memo


async def ensure_token_not_revoked(request: Any, claims: dict[str, Any]) -> None:
    """Raise 401 (revoked / blocked) or 503 (fail-closed outage); memoised.

    The single revocation hook every framework token check calls, so several
    guards on one request cost one cache round trip: one live ``GET`` of the
    subject-state record (ADR-0009 item 2; there is no route-level
    freshness setting).
    """
    memo = _request_memo(request)
    memo_key = (claims.get("sub"), claims.get("jti"), claims.get("iat"))
    outcome = memo.get(memo_key) if memo is not None else None
    if outcome is None:
        started = time.perf_counter()
        try:
            outcome = await _live_status(claims)
        finally:
            _REVOCATION_CHECK.observe(time.perf_counter() - started)
        if memo is not None:
            memo[memo_key] = outcome
    if outcome is not TOKEN_OK:
        _raise_for(outcome)


async def assert_token_valid_now(claims: dict[str, Any]) -> None:
    """Re-read ``claims``' subject-state record LIVE; raise if not valid.

    Unlike :func:`ensure_token_not_revoked` there is no per-request memo:
    this is the commit-time re-check
    (``sandloom.core.di.subject_commit_check``), which must see a mark written
    while the request was running. ``401`` revoked / blocked, ``503`` on a
    fail-closed cache outage (fail-open: accepted, as at request start).
    """
    outcome = await _live_status(claims)
    if outcome is not TOKEN_OK:
        _raise_for(outcome)
