"""Pin tenancy's registration surface (what ``register(core)`` registers).

Recorded from the hand-written ``register`` before it became
``define_module(...)``; since Public API 1.0 the events and the contract are
L1 declarations (typed producers, ``@contract``) registered by discovery.
"""

from sandloom.sdk.module import CapabilityRequirement
from sandloom.sdk.testing import registration_surface

EXPECTED = [  # noqa: RUF005 - the expected surface reads as one list plus the shared tail
    (
        "include_router",
        "router[GET /api/v1/platform/tenancy/organizations, GET "
        "/api/v1/platform/tenancy/organizations/{record_id}, GET "
        "/api/v1/platform/tenancy/members]",
    )
] + [
    (
        "include_router",
        "router[POST /api/v1/orgs, GET /api/v1/orgs, GET /api/v1/orgs/{org_id}, PATCH "
        "/api/v1/orgs/{org_id}, DELETE /api/v1/orgs/{org_id}, POST "
        "/api/v1/orgs/{org_id}/transfer-ownership/{new_owner_id}, GET "
        "/api/v1/orgs/{org_id}/membership, GET "
        "/api/v1/orgs/{org_id}/members, PATCH "
        "/api/v1/orgs/{org_id}/members/{member_user_id}, DELETE "
        "/api/v1/orgs/{org_id}/members/{member_user_id}, POST "
        "/api/v1/orgs/{org_id}/invitations, GET /api/v1/orgs/{org_id}/invitations, "
        "DELETE /api/v1/orgs/{org_id}/invitations/{invitation_id}]",
    ),
    ("include_router", "router[POST /api/v1/invitations/accept]"),
    # Public API 1.0: the producer classes of ``events`` (L1 discovery
    # registers each class's schema under its class name).
    (
        "register_event_schema",
        "'tenancy.organization_created.v1'",
        "'OrganizationCreated'",
        "mapping:4e415acaeeac",
    ),
    (
        "register_event_schema",
        "'tenancy.organization_updated.v1'",
        "'OrganizationUpdated'",
        "mapping:fb00f840e4c4",
    ),
    (
        "register_event_schema",
        "'tenancy.organization_deleted.v1'",
        "'OrganizationDeleted'",
        "mapping:bca1019b3f10",
    ),
    (
        "register_event_schema",
        "'tenancy.member_added.v1'",
        "'MemberAdded'",
        "mapping:46032824bbfa",
    ),
    (
        "register_event_schema",
        "'tenancy.member_removed.v1'",
        "'MemberRemoved'",
        "mapping:62aedfc91fa8",
    ),
    (
        "register_event_schema",
        "'tenancy.member_role_changed.v1'",
        "'MemberRoleChanged'",
        "mapping:602f5701eb1c",
    ),
    (
        "register_event_schema",
        "'tenancy.invitation_created.v1'",
        "'InvitationCreated'",
        "mapping:0ecd6ba596c4",
    ),
    (
        "register_event_schema",
        "'tenancy.invitation_accepted.v1'",
        "'InvitationAccepted'",
        "mapping:e42b6306fec2",
    ),
    (
        "register_event_schema",
        "'tenancy.invitation_revoked.v1'",
        "'InvitationRevoked'",
        "mapping:982da25f6417",
    ),
    # v2 of the member channels (tenant_id, required version): dual-published;
    # the schemas carry the producer's field descriptions (sandloom docs <channel>)
    (
        "register_event_schema",
        "'tenancy.member_added.v2'",
        "'MemberAddedV2'",
        "mapping:8128242f194c",
    ),
    (
        "register_event_schema",
        "'tenancy.member_removed.v2'",
        "'MemberRemovedV2'",
        "mapping:3a0dab3f3a12",
    ),
    (
        "register_event_schema",
        "'tenancy.member_role_changed.v2'",
        "'MemberRoleChangedV2'",
        "mapping:c51c9f3c5f1a",
    ),
    # the @contract provider of ``contracts`` (discovered)
    (
        "register_service",
        "'tenancy.membership.v1'",
        "sandloom_tenancy.contracts.MembershipReadContract",
        "database=None",
    ),
    (
        "add_claims_contributor",
        "sandloom_tenancy.claims.tenancy_claims",
        "critical=True",
    ),
    # 0.6.0: the declared membership guard read (planned + logged at startup)
    (
        "register_operations",
        "JoinRead('tenancy.member.role_in_active_org')",
        "ReadOperation('tenancy.org.by_id')",
        "ListOperation('tenancy.members.of_org')",
        "ListOperation('tenancy.orgs.active_of_user')",
        "ListOperation('tenancy.orgs.active_ids_of_user')",
    ),
]


def test_registration_surface_is_pinned() -> None:
    import sandloom_tenancy

    surface = registration_surface(sandloom_tenancy.register)
    assert surface.calls == EXPECTED
    descriptor = surface.descriptor
    assert (descriptor.name, descriptor.version, descriptor.optional) == (
        "sandloom_tenancy",
        "1.0.0",
        False,
    )
    assert descriptor.consumes == []
    assert descriptor.capabilities == [
        CapabilityRequirement("database"),
        CapabilityRequirement("eventbus"),
    ]
