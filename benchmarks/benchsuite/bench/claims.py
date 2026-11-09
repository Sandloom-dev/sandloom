"""The stands' claims authority: the ``bench`` module (ADR-0014).

The S5-family stands (``SANDLOOM_MODULES=["benchsuite.bench"]``) load no
identity, tenancy or billing, so under the standard trust map nothing would
be trusted: ``tenant_ids`` and ``subscription_status`` would have no
authority and every authenticated request would be refused (401
``token_policy_outdated``, 402 on ``Subscribed``). The stand's composition
root (``benchsuite.apps.sandloom_app``) therefore assigns exactly those two
keys to this module, as the standard root assigns them to tenancy and
billing.

Tokens come from the real mint: ``benchsuite.claims_mint`` runs a kernel core
with this map and :func:`contributor`, calls ``collect_claims`` and
``create_access_token(claims=sealed)``. A bare HS256 JWT is no longer
accepted by the verifier (no ``cpv``, no provenance), and the sealed mint is
not bypassed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: The authority's import path (its short name, ``bench``, is the provenance).
AUTHORITY = "benchsuite.bench"

#: The stand's ``claim_authorities``: no issuer (the stand mints nothing at
#: run time; its tokens are minted by ``benchsuite.claims_mint``).
CLAIM_AUTHORITIES: dict[str, str] = {
    "tenant_ids": AUTHORITY,
    "subscription_status": AUTHORITY,
}


def contributor(
    tenant: str, subscription: str | None
) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """The module's claims contributor for one grant: ``tenant`` in
    ``tenant_ids`` and, when given, the ``subscription_status``."""

    async def bench_claims(_user_id: Any) -> dict[str, Any]:
        claims: dict[str, Any] = {"tenant_ids": [tenant]}
        if subscription is not None:
            claims["subscription_status"] = subscription
        return claims

    return bench_claims


__all__ = ["AUTHORITY", "CLAIM_AUTHORITIES", "contributor"]
