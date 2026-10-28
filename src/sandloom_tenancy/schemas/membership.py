"""Result DTOs of tenancy's declared read operations, and the DTO of
``tenancy.membership.v1`` (``MemberState``)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MemberRoleOut(BaseModel):
    """A member's role (result of ``member_role_in_active_org``)."""

    role: str


class OrganizationIdOut(BaseModel):
    """An organization id (result of ``active_org_ids_of_user``)."""

    id: UUID


class MemberState(BaseModel):
    """The owner's current view of one ``(organization_id, user_id)`` pair
    (``tenancy.membership.v1.get_member_state``).

    ``role`` is ``None`` when the user is not a member of the ACTIVE
    organization; ``version`` is the last member-event version tenancy issued
    for the pair (``0`` when it never issued one — a pre-0005 membership).
    Callers import the generated copy
    (``sandloom.shared.contracts.tenancy_dto``), written by ``sandloom
    generate``; the ordering rule a member-event consumer applies is in
    :mod:`sandloom.shared.membership`.
    """

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    user_id: UUID
    role: str | None
    version: int
