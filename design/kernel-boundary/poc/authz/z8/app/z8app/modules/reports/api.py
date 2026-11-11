"""Z-8 fixture consumer: routes gated by a third-party provider's atoms."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sandloom.sdk import Contract, require
from sandloom.sdk.authz import TIER_CLAIM, TIER_LIVE, Account, Category, Requirement
from sandloom.sdk.web import TenantScope
from z8app.shared.contracts.projects_roles_v1 import (
    PROJECTS_ROLES_V1,
    ProjectRole,
    SeatsAvailable,
)

# APIRouter: the static gate-order rule reads only an APIRouter's dependencies
router = APIRouter()
roles = Contract(PROJECTS_ROLES_V1)


# 1. gate order: a gate that needs an organisation before TenantScope()
@router.get("/board", dependencies=[Depends(require(ProjectRole("viewer")))])
async def board(tenant: Annotated[UUID, TenantScope()]) -> dict[str, str]:
    return {}


# 2. C027: an organization route gating the personal account
@router.get(
    "/orgs/{org_id}/seats",
    dependencies=[Depends(require(SeatsAvailable(1, of=Account.user())))],
)
async def seats(org_id: UUID) -> dict[str, str]:
    return {}


# 3a. sensitive: an admin-risk gate tags the write route (no report)
@router.post(
    "/orgs/{org_id}/settings",
    dependencies=[Depends(require(ProjectRole("lead")))],
)
async def settings(org_id: UUID, flag: bool) -> dict[str, str]:
    if not flag:
        raise HTTPException(status_code=403)
    return {}


# 3b. sensitive: a viewer gate carries no admin risk (reported)
@router.post(
    "/orgs/{org_id}/notes",
    dependencies=[Depends(require(ProjectRole("viewer")))],
)
async def notes(org_id: UUID, flag: bool) -> dict[str, str]:
    if not flag:
        raise HTTPException(status_code=403)
    return {}


# 4a. hand-written atom over the declared check, lowering its tier and risk
class CheapLead(Requirement):
    tier = TIER_CLAIM
    category = Category.FORBIDDEN

    async def evaluate(self, ctx) -> bool:  # type: ignore[no-untyped-def]
        return await roles.has_project_role(ctx.tenant, ctx.subject.user_id, "lead")


# 4b. hand-written atom stating the declared metadata (recognised, no report)
class HonestLead(Requirement):
    tier = TIER_LIVE
    category = Category.FORBIDDEN

    async def evaluate(self, ctx) -> bool:  # type: ignore[no-untyped-def]
        return await roles.has_project_role(ctx.tenant, ctx.subject.user_id, "lead")

    def risks(self) -> frozenset[str]:
        return frozenset({"admin"})
