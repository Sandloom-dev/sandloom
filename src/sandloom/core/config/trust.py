"""The deployment's trust model: access-fact modes, production profile,
accepted risks, security caches and tenancy mode.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_settings import NoDecode


class TrustConfig(BaseModel):
    """The deployment's trust model: access-fact modes, production profile,
    accepted risks, security caches and tenancy mode.
    """

    # Authorization access-fact resolution modes (per fact name). Maps an
    # access fact -> "claim" | "projection" | "live" | "auto"; a fact omitted
    # here uses its own default_mode. A configured mode never weakens the
    # fact's min_freshness floor (the subsystem clamps it). Shape:
    #   {"subscription.active": "live", "org.admin": "projection"}
    # See sandloom.shared.authz.facts.
    access: dict[str, str] = Field(
        default_factory=dict,
        alias="ACCESS",
        description=(
            "JSON map overriding access-fact resolution modes (claim, projection, "
            "live or auto); each fact freshness floor is still enforced."
        ),
    )

    # Production profile: the trust model a production deployment promises
    # (sandloom.core.production_profile; docs/advanced/security-model.md).
    # ``standard`` keeps every knob as configured. ``multi_tenant_strict``
    # (enforced only with ENVIRONMENT=prod) requires TENANCY_MODE=multi,
    # tenant_rls (+ role verification) on every database instance and the
    # tenant policy on every TenantScoped table (checked at startup),
    # fail-closed revocation, a separate non-evicting INFRA.atomic_store.security
    # instance, and — when split — the contract compatibility check.
    production_profile: Literal["standard", "multi_tenant_strict"] = Field(
        default="standard",
        alias="PRODUCTION_PROFILE",
        description=(
            "Production trust profile: standard or multi_tenant_strict. The "
            "strict profile enforces tenant RLS, isolated security infrastructure "
            "and other startup guarantees."
        ),
    )
    # The ONE list of weaker guarantees this deployment accepts in prod
    # (sandloom.core.config.accepted_risks; JSON list or comma-separated ids, e.g.
    # ["billing.fake_psp_in_prod"]). An unknown id fails startup; every accepted risk is
    # logged at every start, exported as sandloom_accepted_risk_active{risk} and
    # listed by /diagnostics/core and `sandloom doctor`.
    sandloom_accepted_risks: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="SANDLOOM_ACCEPTED_RISKS",
        description=(
            "Explicit production guarantee exceptions as a JSON list or "
            "comma-separated risk IDs; unknown IDs fail startup and accepted "
            "risks are reported."
        ),
    )
    # Tenant isolation. Single-tenant is the degenerate multi-tenant case: the
    # configured canonical tenant is still bound and enforced on every
    # TenantScoped operation rather than disabling tenant filters.
    tenancy_mode: Literal["multi", "single"] = Field(
        default="multi",
        alias="TENANCY_MODE",
        description=(
            "Tenant-binding mode: multi derives the current tenant; single "
            "consistently binds the canonical SINGLE_TENANT_ID without disabling "
            "tenant filters."
        ),
    )
    single_tenant_id: UUID | None = Field(
        default=None,
        alias="SINGLE_TENANT_ID",
        description=("Canonical tenant UUID required when TENANCY_MODE is single."),
    )
