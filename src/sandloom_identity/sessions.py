"""Per-user session revocation ("tokens valid after") for identity.

Access JWTs are stateless and authorization claims (roles/permissions) are
baked in at mint time, so a role revocation, password reset or deactivation is
otherwise only effective once the outstanding access token expires. Identity
asks the kernel to revoke the user's tokens (``revoke_user_tokens`` from
``sandloom.core.di``, a framework-owned cache mark); every framework token check —
``current_context`` / ``require(...)`` / ``get_current_user_id`` /
``TenantScope`` in any module, plus identity's own loaders — then rejects an
access token whose ``iat`` is older.

Refresh tokens are revoked durably in the database by the callers.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from sandloom.sdk.security import (
    apply_pending_revocation,
    ensure_token_not_revoked,
    revoke_user_tokens_on_commit,
)
from sandloom_identity.events import EventActor, UserSessionsRevokedEvent

logger = logging.getLogger("sandloom_identity.sessions")

SESSIONS_REVOKED_CHANNEL = "identity.user_sessions_revoked.v1"


async def revoke_sessions(
    user_id: UUID,
    *,
    reason: str,
    actor_id: UUID | None = None,
    publish: bool = True,
) -> None:
    """Invalidate every access token issued to ``user_id`` before now.

    Also publishes ``identity.user_sessions_revoked.v1`` (transactional outbox)
    so other modules / a gateway can drop cached sessions for the user.

    The revocation mark is a cache write OUTSIDE the transaction, so it goes
    through the framework's ``revoke_user_tokens_on_commit`` (the same path
    tenancy uses) with ``require_immediate=True``: the mark is written before
    the change commits — or the password change / deactivation / role
    revocation is refused with 503 — and re-marked after commit.
    ``valid_after`` is fixed NOW (not at commit), so tokens the same request
    mints afterwards (e.g. the fresh pair returned by a password change) stay
    valid.
    """
    # Risk belongs to the operation: password changes require reauthentication,
    # admin actions carry admin risk, and public resets prove their own token.
    # Tagging this shared mechanism security would impose step-up on every caller.
    valid_after = await (
        revoke_user_tokens_on_commit(  # sandloom: allow SANDLOOM-C022 caller owns risk
            user_id, reason=reason, require_immediate=True
        )
    )
    if publish:
        event = UserSessionsRevokedEvent(
            actor=EventActor(user_id=actor_id),
            payload={
                "user_id": str(user_id),
                "reason": reason,
                # whole seconds, rounded UP from the microsecond mark
                "tokens_valid_after": valid_after,
            },
        )
        await event.publish()


async def is_token_revoked(claims: dict[str, Any]) -> bool:
    """Return True when the access token predates the user's revocation mark.

    Delegates to the kernel check. A revocation-store outage follows
    ``AUTH_REVOCATION_FAIL_CLOSED`` (the 503 propagates when fail-closed).
    """
    try:
        await ensure_token_not_revoked(None, claims)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return True
        raise
    return False


async def apply_token_revocation(session: Any, envelope: dict[str, Any]) -> None:
    """``core.auth.tokens_revoked.v1`` consumer: the durable revocation applier.

    Every ``revoke_user_tokens_on_commit`` (any module, any service) writes this
    event into its own transaction. The mark is normally already written after
    commit; when that write failed (cache outage) this delivery writes it — and
    raises while it still cannot, so the event is redelivered (at-least-once).
    Marks are monotonic, so re-applying is a cheap no-op.
    """
    await apply_pending_revocation(envelope.get("payload") or {})


__all__ = [
    "SESSIONS_REVOKED_CHANNEL",
    "apply_token_revocation",
    "is_token_revoked",
    "revoke_sessions",
]
