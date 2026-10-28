"""Tenant-selection claims contributed to freshly minted access tokens.

Runs in tenancy's OWN Unit of Work (:func:`sandloom.core.module_api.contract_session`)
and is exposed as ``tenancy.claims.v1``, so an identity service deployed in
another process still mints tokens carrying ``tenant_ids`` (it collects them
over RPC via ``SERVICE_BINDINGS``). Registered ``critical=True``: if tenancy
cannot answer, token issuance fails (503) instead of minting a token without
the allowlist.
"""

from __future__ import annotations

from uuid import UUID

from sandloom.core.module_api import contract_session
from sandloom.sdk.infra import settings
from sandloom_tenancy.services import MembershipService


async def tenancy_claims(user_id: UUID | str) -> dict[str, list[str]]:
    """Return every active organization the user may select as a tenant
    (read in tenancy's contract session)."""
    session = contract_session()
    uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
    cap = settings.tenant_claims_max_organizations
    tenant_ids = await MembershipService.list_user_organization_ids(
        uid, session, limit=cap + 1
    )
    if len(tenant_ids) > cap:
        raise ValueError(
            "Organization claims exceed TENANT_CLAIMS_MAX_ORGANIZATIONS; "
            "reduce memberships or explicitly raise the configured limit"
        )
    return {"tenant_ids": [str(tenant_id) for tenant_id in tenant_ids]}


__all__ = ["tenancy_claims"]
