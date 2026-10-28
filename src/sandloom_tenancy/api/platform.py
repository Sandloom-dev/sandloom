"""Platform directory: organization membership never grants platform access."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from sandloom.sdk.web import DbSession, require_platform_admin
from sandloom_tenancy.schemas.organization import OrganizationOut
from sandloom_tenancy.schemas.platform import PlatformMember
from sandloom_tenancy.services.platform_service import PlatformOrganizationService

platform_organizations_router = APIRouter(
    prefix="/api/v1/platform/tenancy",
    tags=["platform-organizations"],
    dependencies=[
        Depends(require_platform_admin),
    ],
)


@platform_organizations_router.get(
    "/organizations", response_model=list[OrganizationOut]
)
async def organizations(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    owner_id: UUID | None = None,
) -> list[OrganizationOut]:
    rows = await PlatformOrganizationService.list_organizations(
        db, skip, limit, q, is_active, owner_id
    )
    return [OrganizationOut.model_validate(row) for row in rows]


@platform_organizations_router.get(
    "/organizations/{record_id}", response_model=OrganizationOut
)
async def organization(record_id: UUID, db: DbSession) -> OrganizationOut:
    return OrganizationOut.model_validate(
        await PlatformOrganizationService.get_organization(record_id, db)
    )


@platform_organizations_router.get("/members", response_model=list[PlatformMember])
async def members(
    db: DbSession,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=101),
    organization_id: UUID | None = None,
    user_id: UUID | None = None,
) -> list[PlatformMember]:
    rows = await PlatformOrganizationService.members(
        db, skip, limit, organization_id, user_id
    )
    return [PlatformMember.model_validate(row) for row in rows]
