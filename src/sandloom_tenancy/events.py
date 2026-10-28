"""Tenancy module events: typed producers of the ``tenancy.*.v1`` channels.

Additive-only within v1 (a breaking change bumps the version). Each class is
the producer of its channel: ``sandloom test`` registers and snapshots its schema
and exports it to ``sandloom/shared/events/<channel>.json`` so views elsewhere can
be verified. Publish with ``await MemberAdded(...).publish()`` inside the
change's unit (transactional outbox).

An optional field that is unset is omitted from the payload (as before the
typed classes), so v1 consumers see the same payloads.
"""

from typing import Any
from uuid import UUID

from pydantic import Field

from sandloom.sdk import Event

# Channels (additive-only within v1; breaking changes bump the version)
ORG_CREATED_V1 = "tenancy.organization_created.v1"
ORG_UPDATED_V1 = "tenancy.organization_updated.v1"
ORG_DELETED_V1 = "tenancy.organization_deleted.v1"
MEMBER_ADDED_V1 = "tenancy.member_added.v1"
MEMBER_REMOVED_V1 = "tenancy.member_removed.v1"
MEMBER_ROLE_CHANGED_V1 = "tenancy.member_role_changed.v1"
# v2 of the member channels: ``tenant_id`` and a REQUIRED ``version``, so a
# read model consumes them with a declarative Projection. Dual-published with
# v1 (publish_member) until no v1 reader is left.
MEMBER_ADDED_V2 = "tenancy.member_added.v2"
MEMBER_REMOVED_V2 = "tenancy.member_removed.v2"
MEMBER_ROLE_CHANGED_V2 = "tenancy.member_role_changed.v2"
INVITATION_CREATED_V1 = "tenancy.invitation_created.v1"
INVITATION_ACCEPTED_V1 = "tenancy.invitation_accepted.v1"
INVITATION_REVOKED_V1 = "tenancy.invitation_revoked.v1"


def _is_unset(value: object) -> bool:
    return value is None


class _TenancyEvent(Event):
    """Shared payload rule: unset optional fields are not sent.

    Each optional field is excluded while it is ``None`` (``exclude_if``), so
    the wire schema (the serialization schema) still lists it as optional.
    """


class OrganizationEvent(_TenancyEvent):
    """Fields of the organization channels."""

    organization_id: UUID
    actor_id: UUID | None = Field(default=None, exclude_if=_is_unset)


class MemberEvent(_TenancyEvent):
    """Fields of the member channels.

    ``version`` is the per-membership version, monotonic over the whole
    lifecycle of the ``(organization_id, user_id)`` pair (removal and re-add
    included). Order and dedupe member events by it: delivery has no ordering
    guarantee. Optional within v1, so a version-less event of an older
    producer still validates.
    """

    organization_id: UUID
    user_id: UUID
    role: str | None = Field(default=None, exclude_if=_is_unset)
    actor_id: UUID | None = Field(default=None, exclude_if=_is_unset)
    version: int | None = Field(default=None, ge=1, exclude_if=_is_unset)


class MemberEventV2(_TenancyEvent):
    """Fields of the member channels, v2: the organization is also the
    event's ``tenant_id`` and ``version`` is required (every event a current
    tenancy publishes carries it), so consumers order by it without the
    refresh fallback v1 needs."""

    tenant_id: UUID = Field(description="The organization (the event's tenant).")
    organization_id: UUID = Field(description="The organization (= tenant_id).")
    user_id: UUID = Field(description="The member.")
    role: str | None = Field(
        default=None,
        exclude_if=_is_unset,
        description="member, admin or owner; absent on member_removed.",
    )
    actor_id: UUID | None = Field(
        default=None, exclude_if=_is_unset, description="Who made the change."
    )
    version: int = Field(
        ge=1,
        description="Per (organization_id, user_id), monotonic over the "
        "membership's whole lifecycle: order and dedupe by it.",
    )


class InvitationEvent(_TenancyEvent):
    """Fields of the invitation channels."""

    organization_id: UUID
    invitation_id: UUID
    role: str | None = Field(default=None, exclude_if=_is_unset)
    actor_id: UUID | None = Field(default=None, exclude_if=_is_unset)


class OrganizationCreated(OrganizationEvent, channel=ORG_CREATED_V1):
    """An organization was created (its owner is ``actor_id``)."""


class OrganizationUpdated(OrganizationEvent, channel=ORG_UPDATED_V1):
    """An organization's name or settings changed."""


class OrganizationDeleted(OrganizationEvent, channel=ORG_DELETED_V1):
    """An organization was deactivated."""


class MemberAdded(MemberEvent, channel=MEMBER_ADDED_V1):
    """A member joined (``add_member``, an accepted invitation, a new owner)."""


class MemberRemoved(MemberEvent, channel=MEMBER_REMOVED_V1):
    """A member was removed or left (no ``role``)."""


class MemberRoleChanged(MemberEvent, channel=MEMBER_ROLE_CHANGED_V1):
    """A member's role changed (both sides of an ownership transfer too)."""


class InvitationCreated(InvitationEvent, channel=INVITATION_CREATED_V1):
    """An invitation was sent."""


class InvitationAccepted(InvitationEvent, channel=INVITATION_ACCEPTED_V1):
    """An invitation was accepted."""


class InvitationRevoked(InvitationEvent, channel=INVITATION_REVOKED_V1):
    """An invitation was revoked."""


class MemberAddedV2(MemberEventV2, channel=MEMBER_ADDED_V2):
    """A member joined (v2).

    The organization is ``tenant_id`` (a consumer binds it; a projection's
    ``tenant_from``). ``version`` is required and per ``(organization_id,
    user_id)``: order and dedupe by it (a projection's ``version_field``).
    Published with the v1 channel in one unit.
    """


class MemberRemovedV2(MemberEventV2, channel=MEMBER_REMOVED_V2):
    """A member was removed or left (v2, no ``role``).

    The organization is ``tenant_id`` (a consumer binds it; a projection's
    ``tenant_from``). ``version`` is required and per ``(organization_id,
    user_id)``: order and dedupe by it (a projection's ``version_field``).
    Published with the v1 channel in one unit.
    """


class MemberRoleChangedV2(MemberEventV2, channel=MEMBER_ROLE_CHANGED_V2):
    """A member's role changed (v2).

    The organization is ``tenant_id`` (a consumer binds it; a projection's
    ``tenant_from``). ``version`` is required and per ``(organization_id,
    user_id)``: order and dedupe by it (a projection's ``version_field``).
    Published with the v1 channel in one unit.
    """


_V2: dict[type[MemberEvent], type[MemberEventV2]] = {
    MemberAdded: MemberAddedV2,
    MemberRemoved: MemberRemovedV2,
    MemberRoleChanged: MemberRoleChangedV2,
}


async def publish_member(event: MemberEvent) -> None:
    """Publish a member event on v1 and v2 in the running unit (one outbox
    row and envelope id each; both commit or neither does)."""
    await event.publish()
    if event.version is None:  # pragma: no cover - every caller sets it
        raise ValueError(f"{type(event).__name__} needs a version for v2")
    v2 = _V2[type(event)](
        tenant_id=event.organization_id,
        organization_id=event.organization_id,
        user_id=event.user_id,
        role=event.role,
        actor_id=event.actor_id,
        version=event.version,
    )
    await v2.publish()


def member_payload(
    organization_id: UUID,
    user_id: UUID,
    role: str | None = None,
    actor_id: UUID | None = None,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    """The wire payload of a member event (what consumers receive)."""
    return MemberEvent(
        organization_id=organization_id,
        user_id=user_id,
        role=role,
        actor_id=actor_id,
        version=version,
    ).model_dump(mode="json")
