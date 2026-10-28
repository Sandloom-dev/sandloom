"""Billing's token-claims contributor.

Adds the PERSONAL tenant's ``subscription_status`` (and ``plan_code`` while the
subscription grants access) to the JWT, powering the claim-based
``Subscribed`` / ``Feature`` atoms and the claim mode of the
``subscription.active`` access fact. The claim is computed with the same
predicate as the live check, so both modes agree at mint time.

The contributor runs in billing's OWN Unit of Work on billing's database (the
contract runtime supplies the session) and is exposed as ``billing.claims.v1``
so an identity service deployed in another process collects it over RPC. It is
non-critical: if billing is down, the token is minted with ``billing`` in
``claims_sources_failed`` and claim-mode facts escalate to the live check.

Organization subscriptions are deliberately NOT claimed: a user may belong to
many organizations and the token has no per-organization claim slot, so an
org-scoped gate MUST be live — ``SubscriptionActive(of=Account.org())``
or ``Entitled("organization.subscription.active")`` (see
:mod:`sandloom_billing.access`). Money-sensitive checks (credit balance) are never
claimed either; they use the live ``Credits`` atom.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sandloom.core.module_api import contract_session
from sandloom_billing.entitlements import EntitlementsService


async def contribute_billing_claims(user_id: UUID | str) -> dict[str, Any]:
    """Return subscription claims for the user's personal tenant (read in
    billing's contract session)."""
    session = contract_session()
    uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    return await EntitlementsService.subscription_claims("user", uid, session)
