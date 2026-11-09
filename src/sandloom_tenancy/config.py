"""Tenancy's declared settings (ADR-0015)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from sandloom.sdk.module import settings_of


class TenancySettings(BaseModel):
    tenant_claims_max_organizations: int = Field(
        default=128,
        ge=1,
        le=10000,
        alias="TENANT_CLAIMS_MAX_ORGANIZATIONS",
        description=(
            "Maximum organization memberships embedded in an access token; "
            "excessive claims are refused rather than silently truncated."
        ),
    )


def tenancy_settings() -> TenancySettings:
    """Tenancy's validated settings of the running application."""
    return settings_of(TenancySettings)
