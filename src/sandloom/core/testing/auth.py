"""Mint real access tokens for tests — users exist only as JWT claims.

Module tests never create identity rows; a "user" is a signed access token
carrying the claims every guard expects (``token_type="access"``, ``iat``) plus
a tenant binding so ``TenantScope()`` endpoints work out of the box.
"""

from __future__ import annotations

import uuid
from typing import Any

Headers = dict[str, str]


def email_of(user_id: uuid.UUID) -> str:
    """The deterministic ``email`` claim minted for a test user."""
    return f"{user_id.hex[:12]}@example.com"


def make_user_headers(
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    *,
    email: str | None = "",
    email_verified: bool | None = True,
    tenant: bool = True,
    **claims: Any,
) -> tuple[uuid.UUID, Headers]:
    """Return ``(user_id, headers)`` with a freshly minted access token.

    * ``user_id`` / ``tenant_id`` default to random UUIDs; the token carries
      ``tenant_id`` and the signed ``tenant_ids`` allowlist ``[tenant_id]``.
      ``tenant=False`` mints a tenant-less token.
    * ``email`` defaults to :func:`email_of` and ``email_verified`` to
      ``True`` (like identity-minted tokens); pass ``None`` to omit either.
    * Any extra keyword becomes a claim (``roles=["admin"]``, ``org_id=...``);
      it overrides the defaults above.

    ``create_access_token`` stamps ``iat`` / ``nbf`` / ``exp`` / ``jti``.
    """
    from sandloom.core.auth.tokens import create_access_token  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    uid = user_id or uuid.uuid4()
    data: dict[str, Any] = {"sub": str(uid), "token_type": "access"}
    if tenant:
        tid = tenant_id or uuid.uuid4()
        data["tenant_id"] = str(tid)
        data["tenant_ids"] = [str(tid)]
    if email is not None:
        data["email"] = email or email_of(uid)
    if email_verified is not None:
        data["email_verified"] = email_verified
    data.update(claims)
    token = create_access_token(data=data)
    return uid, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


__all__ = [
    "Headers",
    "email_of",
    "make_user_headers",
    "stop_revocation_listener",
    "wait_revocation_listener_subscribed",
]


async def wait_revocation_listener_subscribed(timeout: float = 5.0) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
    """Return once no per-process copy of a subject's state can be stale.

    Every authenticated request reads the subject-state record live
    (ADR-0009): there are no per-process copies and no invalidation listener,
    so this is always already true and returns at once.
    """
    return None


async def stop_revocation_listener() -> None:
    """Drop every per-process copy of a subject's state (test teardown).

    There are none (ADR-0009: every check reads the record live), so this
    returns at once.
    """
    return None
