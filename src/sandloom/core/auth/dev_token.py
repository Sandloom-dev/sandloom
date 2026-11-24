"""The mint behind ``sandloom dev token``: a development access token that the
app's own claim policy accepts (ADR-0014).

A running app trusts a protected claim (``roles``, ``tenant_ids``) only when
the token names the authority the composition assigned to that key (its
``cpv`` digest and its ``claims_sources`` entry), so a hand-signed JWT carries
no rights. A developer has no issuer to ask in the first hour, so this module
plays the assigned authority for the one claim set a developer asks for. That
is a forgery of provenance by design, and the reason for every refusal below:

* it mints only in ``ENVIRONMENT=dev`` or ``test`` (:func:`mint_dev_token`
  checks the loaded settings again, whatever the caller checked);
* it takes a claim only when the policy assigns an authority for it: an
  unassigned key would be dropped by the app's trusted view, so asking for it
  is an error, never a token that looks fine and answers 403;
* it signs with the app's own key (``JWT_SECRET`` or the asymmetric
  ``JWT_PRIVATE_KEY``) and never returns or names it;
* every token it mints carries the sealed marker ``dev_mint``
  (:data:`~sandloom.core.claims.DEV_MINT_CLAIM`), and the verifier
  (:func:`sandloom.core.auth.tokens.decode_token`) refuses that marker unless
  its own ``ENVIRONMENT`` is ``dev`` or ``test``. This is what keeps a
  production app from accepting a token minted with its key on a developer
  machine (a copied production ``.env`` with ``ENVIRONMENT=dev`` passes the
  checks of the command; the key it holds is not protected by them).

Not a replacement for an issuer: the subject has no account, so a route that
reads one (identity's ``/auth/me``) answers as it does for an unknown user.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sandloom.core.auth.tokens import DEV_ENVIRONMENTS, create_access_token
from sandloom.core.claims import (
    CPV_CLAIM,
    CPV_VERSION,
    DEV_MINT_CLAIM,
    ClaimAuthorities,
    _seal_claims,
)
from sandloom.core.config.settings import settings

#: A token lives one hour unless asked otherwise; never longer than a day.
DEFAULT_TTL_MINUTES = 60
MAX_TTL_MINUTES = 24 * 60
#: How many roles one token may carry, and what a role name looks like.
MAX_ROLES = 16
ROLE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


class DevTokenRefused(ValueError):
    """The dev token is not minted: the message says why and what to change.
    It never carries a secret."""


@dataclass(frozen=True, slots=True)
class DevToken:
    """A minted dev token and what it claims (the token itself is a bearer
    credential: print it once, never log it)."""

    token: str = field(repr=False)
    user_id: UUID
    tenant_id: UUID | None
    roles: tuple[str, ...]
    ttl_minutes: int
    #: Short names of the authorities the token claims to be vouched by.
    authorities: tuple[str, ...]


def _check_environment() -> None:
    environment = settings.environment.value
    if environment not in DEV_ENVIRONMENTS:
        raise DevTokenRefused(
            f"ENVIRONMENT={environment}: a development token is minted only "
            f"with ENVIRONMENT={' or '.join(DEV_ENVIRONMENTS)}"
        )


def _check_request(roles: Sequence[str], ttl_minutes: int) -> tuple[str, ...]:
    if not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise DevTokenRefused(
            f"a dev token lives 1 to {MAX_TTL_MINUTES} minutes, not {ttl_minutes}"
        )
    unique = sorted(set(roles))
    if len(unique) > MAX_ROLES:
        raise DevTokenRefused(f"a dev token carries at most {MAX_ROLES} roles")
    for role in unique:
        if ROLE_PATTERN.fullmatch(role) is None:
            raise DevTokenRefused(
                f"role {role!r} is not a role name: letters, digits and "
                "`_ . : -`, 1 to 64 characters, starting with a letter or digit"
            )
    return tuple(unique)


def _authority_of(policy: ClaimAuthorities, key: str, flag: str) -> tuple[str, str]:
    """``(short name, digest)`` of the authority this app's verifier accepts
    for ``key``, or a refusal that names the way out."""
    entries = policy.accepted.get(key)
    if not entries:
        raise DevTokenRefused(
            f"{flag}: this app assigns no authority for the `{key}` claim "
            "(claim_authorities of its composition root), so its verifier drops "
            f"`{key}` from every token. Compose the module that owns it "
            "(identity for roles, tenancy for tenant_ids) or leave the flag out"
        )
    return entries[0]


def mint_dev_token(
    policy: ClaimAuthorities,
    *,
    user_id: UUID,
    tenant_id: UUID | None = None,
    roles: Sequence[str] = (),
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> DevToken:
    """An access token for ``user_id`` that the app composed with ``policy``
    accepts, carrying ``roles`` and the grant of ``tenant_id`` when given.

    Raises :class:`DevTokenRefused` outside dev/test, for a request out of
    bounds, or for a claim the policy assigns to nobody. Needs the running
    process's settings (``sandloom.core.config.settings``) to be loadable and
    the app composed in this process (the claim policy is read from it).
    """
    _check_environment()
    unique_roles = _check_request(roles, ttl_minutes)
    claims: dict[str, Any] = {}
    sources: set[str] = set()
    if unique_roles:
        sources.add(_authority_of(policy, "roles", "--role")[0])
        claims["roles"] = list(unique_roles)
    if tenant_id is not None:
        sources.add(_authority_of(policy, "tenant_ids", "--tenant")[0])
        claims["tenant_id"] = str(tenant_id)
        claims["tenant_ids"] = [str(tenant_id)]
    claims["claims_sources"] = sorted(sources)
    claims["claims_sources_failed"] = []
    claims[DEV_MINT_CLAIM] = True
    # What an issuer holding the whole map stamps: the digest of every key's
    # current authority, so a key the token does not carry reads as a definite
    # deny (403), never as "refresh" (401 token_policy_outdated).
    claims[CPV_CLAIM] = {
        "v": CPV_VERSION,
        "k": {key: entries[0][1] for key, entries in policy.accepted.items()},
    }
    token = create_access_token(
        {"sub": str(user_id), "token_type": "access"},
        ttl_minutes,
        claims=_seal_claims(claims),
    )
    return DevToken(
        token=token,
        user_id=user_id,
        tenant_id=tenant_id,
        roles=unique_roles,
        ttl_minutes=ttl_minutes,
        authorities=tuple(sorted(sources)),
    )


__all__ = [
    "DEFAULT_TTL_MINUTES",
    "DEV_ENVIRONMENTS",
    "MAX_ROLES",
    "MAX_TTL_MINUTES",
    "ROLE_PATTERN",
    "DevToken",
    "DevTokenRefused",
    "mint_dev_token",
]
