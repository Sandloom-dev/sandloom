"""Global organization directory for explicitly authorized platform operators."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_tenancy.models.membership import OrganizationMember
from sandloom_tenancy.models.organization import Organization


class PlatformOrganizationService:
    @staticmethod
    async def list_organizations(
        db: AsyncSession,
        skip: int,
        limit: int,
        q: str | None,
        is_active: bool | None,
        owner_id: UUID | None,
    ) -> list[Organization]:
        query = select(Organization)
        if q:
            literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(
                or_(
                    Organization.name.ilike(f"%{literal}%", escape="\\"),
                    Organization.slug.ilike(f"%{literal}%", escape="\\"),
                )
            )
        if is_active is not None:
            query = query.where(Organization.is_active == is_active)
        if owner_id is not None:
            query = query.where(Organization.owner_id == owner_id)
        return list(
            (
                await db.scalars(
                    query.order_by(Organization.created_at.desc(), Organization.id)
                    .offset(skip)
                    .limit(limit)
                )
            ).all()
        )

    @staticmethod
    async def get_organization(record_id: UUID, db: AsyncSession) -> Organization:
        row = await db.get(Organization, record_id)
        if row is None:
            raise HTTPException(404, "Organization not found")
        return row

    @staticmethod
    async def members(
        db: AsyncSession,
        skip: int,
        limit: int,
        organization_id: UUID | None,
        user_id: UUID | None,
    ) -> list[OrganizationMember]:
        query = select(OrganizationMember)
        if organization_id is not None:
            query = query.where(OrganizationMember.organization_id == organization_id)
        if user_id is not None:
            query = query.where(OrganizationMember.user_id == user_id)
        return list(
            (
                await db.scalars(
                    query.order_by(
                        OrganizationMember.created_at.desc(), OrganizationMember.id
                    )
                    .offset(skip)
                    .limit(limit)
                )
            ).all()
        )
