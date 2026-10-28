"""Billing's tenant resolution when tenancy's membership contract is missing.

In a split deployment where tenancy runs elsewhere but billing has no
``tenancy.membership.v1`` binding, the resolver must NOT silently fall back to
the PERSONAL tenant for a user the token says belongs to organizations (the
monolith — or a correctly bound split — would resolve the organization). It
answers 501 (deployment misconfiguration). A genuinely personal-only
deployment (no tenancy anywhere: the token carries no tenant claims and no
tenancy provenance) keeps resolving the personal tenant.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sandloom.shared.subject import UserContext
from sandloom_billing.services.tenant_resolver import TENANT_USER, resolve_tenant

pytestmark = pytest.mark.asyncio


class _NoMembershipCore:
    def get_service(self, name: str) -> Any:
        from sandloom.sdk.errors import ServiceUnavailableInDeployment

        raise ServiceUnavailableInDeployment(name)


def _subject(user_id: UUID, **claims: Any) -> UserContext:
    return UserContext.model_validate({"sub": str(user_id), **claims})


async def _resolve(subject: UserContext | None, org_id: UUID | None = None) -> Any:
    uid = subject.user_id if subject is not None else uuid4()
    return await resolve_tenant(
        core=_NoMembershipCore(),
        user_id=uid,
        org_id=org_id,
        db=None,  # type: ignore[arg-type]
        subject=subject,
    )


async def test_token_with_tenant_ids_and_no_membership_contract_is_personal() -> None:
    """Without org_id the account is personal (spec §7.4): no membership read,
    whatever the token's tenant claims say."""
    uid = uuid4()
    subject = _subject(
        uid,
        tenant_ids=[str(uuid4())],
        claims_sources=["tenancy", "identity"],
        claims_sources_failed=[],
    )
    ctx = await _resolve(subject)
    assert (ctx.tenant_type, ctx.tenant_id) == (TENANT_USER, uid)


@pytest.mark.parametrize(
    "provenance",
    [
        {"claims_sources": ["tenancy"], "claims_sources_failed": []},
        {"claims_sources": ["identity"], "claims_sources_failed": ["tenancy"]},
    ],
)
async def test_tenancy_deployed_but_unbound_resolves_personal(
    provenance: dict[str, Any],
) -> None:
    """Tenancy is part of the system, yet without org_id the account is the
    caller's own: an organization is only ever reached with org_id (and then
    a missing binding is 501, below)."""
    subject = _subject(uuid4(), tenant_ids=[], **provenance)
    ctx = await _resolve(subject)
    assert (ctx.tenant_type, ctx.tenant_id) == (TENANT_USER, subject.user_id)


async def test_explicit_org_without_membership_contract_is_501() -> None:
    subject = _subject(uuid4(), claims_sources=["identity"], claims_sources_failed=[])
    with pytest.raises(HTTPException) as exc:
        await _resolve(subject, org_id=uuid4())
    assert exc.value.status_code == 501


@pytest.mark.parametrize(
    "claims",
    [
        {"claims_sources": ["identity", "billing"], "claims_sources_failed": []},
        {},  # legacy token: no provenance, no tenant claims
    ],
)
async def test_personal_only_deployment_resolves_personal(
    claims: dict[str, Any],
) -> None:
    subject = _subject(uuid4(), **claims)
    ctx = await _resolve(subject)
    assert ctx.tenant_type == TENANT_USER
    assert ctx.tenant_id == subject.user_id
