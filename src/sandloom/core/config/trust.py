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
    access: dict[str, str] = Field(default_factory=dict, alias="ACCESS")

    # Production profile: the trust model a production deployment promises
    # (sandloom.core.production_profile; docs/reference/security-model.md).
    # ``standard`` keeps every knob as configured. ``multi_tenant_strict``
    # (enforced only with ENVIRONMENT=prod) requires TENANCY_MODE=multi,
    # tenant_rls (+ role verification) on every database instance and the
    # tenant policy on every TenantScoped table (checked at startup),
    # fail-closed revocation, a separate SECURITY_CACHE_INSTANCE on its own
    # Redis, and — when split — the contract compatibility check.
    production_profile: Literal["standard", "multi_tenant_strict"] = Field(
        default="standard", alias="PRODUCTION_PROFILE"
    )
    # The ONE list of weaker guarantees this deployment accepts in prod
    # (sandloom.core.config.accepted_risks; JSON list or comma-separated ids, e.g.
    # ["fake_psp_in_prod"]). An unknown id fails startup; every accepted risk is
    # logged at every start, exported as sandloom_accepted_risk_active{risk} and
    # listed by /diagnostics/core and `sandloom doctor`.
    sandloom_accepted_risks: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="SANDLOOM_ACCEPTED_RISKS"
    )
    # Named cache instance holding security-critical state: revocation marks,
    # emergency user blocks and — unless IDEMPOTENCY_CACHE_INSTANCE moves them —
    # idempotency records (idempotency_guard, task-enqueue dedupe).
    # Contract-command dedupe is durable in the provider database
    # (contract_command_log), not in a cache.
    # ``default`` shares the best-effort cache; point it at a separate,
    # non-evicting, replicated Redis (Sentinel / managed HA) declared in
    # INFRA["cache"] so cache pressure can never evict a revocation mark.
    security_cache_instance: str = Field(
        default="default", alias="SECURITY_CACHE_INSTANCE"
    )
    # Opt-in: a separate named cache instance for idempotency records
    # (idempotency_guard reservations / replays, task-enqueue dedupe, the
    # cache fallback of contract-command dedupe). Empty (default) keeps them
    # in SECURITY_CACHE_INSTANCE. Revocation marks and blocks never move.
    # Point it at a Redis as durable as the security one (appendfsync always,
    # noeviction) so the idempotency write fsyncs stop queueing the hot
    # revocation-mark reads on the security Redis's single thread
    # (docs/design/write-login-profile-2026-10-22.md). multi_tenant_strict
    # requires it to be declared in INFRA["cache"] on its own Redis server.
    idempotency_cache_instance: str = Field(
        default="", alias="IDEMPOTENCY_CACHE_INSTANCE"
    )

    # Tenant isolation. Single-tenant is the degenerate multi-tenant case: the
    # configured canonical tenant is still bound and enforced on every
    # TenantScoped operation rather than disabling tenant filters.
    tenancy_mode: Literal["multi", "single"] = Field(
        default="multi", alias="TENANCY_MODE"
    )
    single_tenant_id: UUID | None = Field(default=None, alias="SINGLE_TENANT_ID")
