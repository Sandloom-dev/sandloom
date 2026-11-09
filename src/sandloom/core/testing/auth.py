"""Mint real access tokens for tests — users exist only as JWT claims.

Module tests never create identity rows; a "user" is a signed access token
carrying the claims every guard expects (``token_type="access"``, ``iat``) plus
a tenant binding so ``TenantScope()`` endpoints work out of the box.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sandloom.core.auth import claims_view as _claims_view
from sandloom.core.claims import (
    ENVELOPE_KEYS,
    SealedClaims,
    UnsealedClaimsError,
    _seal_claims,
)

Headers = dict[str, str]


def sealed_claims(claims: Mapping[str, Any]) -> SealedClaims:
    """The kernel test authority's sealed claims: what a test passes as
    ``create_access_token(data, claims=...)`` where the application passes
    the ``collect_claims`` result (a test that fakes ``collect_claims``).

    Refused (SANDLOOM-R017) unless this process is a test composition: the
    ``sandloom.sdk.testing`` pytest plugin or the framework's harness is
    active (the kernel test authority exists nowhere else). Without a
    ``claims_sources`` of the caller's own, the test authority
    (``sandloom-test``) vouches for every claim; a test that states
    ``claims_sources`` (and ``claims_sources_failed``) gets exactly that
    provenance checked against the composition's ``claim_authorities``
    (parts 1 and 3 of the trusted view). A stated ``cpv`` is minted as
    given (a test of the view's policy versions); ``cpv=None`` mints none
    (a token of a release before ``cpv``).
    """
    if not _claims_view.test_authority_enabled():
        raise UnsealedClaimsError(
            "SANDLOOM-R017: the kernel test authority mints only in a test "
            "composition (the sandloom.sdk.testing pytest plugin)"
        )
    data = dict(claims)
    data.setdefault("cpv", _claims_view.TEST_CPV)
    if data["cpv"] == _claims_view.TEST_CPV and "claims_sources" not in claims:
        data["claims_sources"] = [_claims_view.TEST_AUTHORITY]
    if data["cpv"] is None:
        del data["cpv"]
    return _seal_claims(data)


def _split_test_claims(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], SealedClaims]:
    """``payload`` as the envelope (``data``) and the test authority's
    sealed claims (every other key)."""
    envelope = {k: v for k, v in payload.items() if k in ENVELOPE_KEYS}
    rest = {k: v for k, v in payload.items() if k not in ENVELOPE_KEYS}
    return envelope, sealed_claims(rest)


def mint_test_token(
    data: Mapping[str, Any],
    expires_delta: int | None = None,
    *,
    claims_read_at: datetime | None = None,
) -> str:
    """A signed test token from ``data``, the test kit's replacement of a
    hand-assembled ``create_access_token(data={...})``.

    The envelope (``sub``, ``token_type``, ``auth_time``, ``iat``, ``exp``,
    ...) is minted as given; every other key (``roles``, ``tenant_ids``,
    ``tenant_id``, ``email``, an application claim) is the kernel test
    authority's sealed contribution (:func:`sealed_claims`): trusted in a
    test composition only, with the provenance a test states checked.
    """
    from sandloom.core.auth.tokens import create_access_token  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    envelope, sealed = _split_test_claims(data)
    return create_access_token(
        envelope,
        expires_delta,
        claims_read_at=claims_read_at,
        claims=sealed,
    )


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
    Every claim but the envelope is the kernel test authority's
    (:func:`mint_test_token`): a test composition only.
    """
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
    # The kernel test authority (a test composition only) mints every claim
    # but the envelope as its sealed contribution: with no
    # ``claims_sources`` of the caller's own it vouches for every claim; a
    # caller stating provenance gets that provenance checked by the
    # verifier's own policy, parts 1 and 3 of the trusted view (ADR-0014).
    token = mint_test_token(data)
    return uid, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


__all__ = [
    "Headers",
    "email_of",
    "make_user_headers",
    "mint_test_token",
    "sealed_claims",
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
