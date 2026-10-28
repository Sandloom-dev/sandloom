"""Tenancy's paged list reads, on declared ``ListOperation``s (read-only).

The API returns a JSON list body; ``limit`` bounds the page and the opaque,
tamper-evident ``cursor`` (the previous response's ``X-Next-Cursor``) selects
the next one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sandloom.sdk.data import Page
from sandloom_tenancy.schemas import MemberOut, OrganizationOut
from sandloom_tenancy.services.operations import active_orgs_of_user, members_of_org


class TenancyLists:
    """The member and organization lists (read-only routes)."""

    @staticmethod
    async def organizations_of(
        reads: Any, user_id: UUID, *, limit: int, cursor: str | None
    ) -> Page[OrganizationOut]:
        """The user's active organizations, creation order."""
        page: Page[OrganizationOut] = await reads.page(
            active_orgs_of_user, limit=limit, cursor=cursor, user_id=user_id
        )
        return page

    @staticmethod
    async def members_of(
        reads: Any, organization_id: UUID, *, limit: int, cursor: str | None
    ) -> Page[MemberOut]:
        """An organization's members, join order."""
        page: Page[MemberOut] = await reads.page(
            members_of_org, limit=limit, cursor=cursor, organization_id=organization_id
        )
        return page


__all__ = ["TenancyLists"]
