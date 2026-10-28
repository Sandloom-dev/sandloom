"""Tenancy module: organizations, membership, invitations.

Registered via the core plugin system. Exposes the membership READ contract
through the service registry as `tenancy.membership.v1`
(:class:`~sandloom_tenancy.contracts.MembershipReadContract`: ``get_role``,
``is_member``, ``has_role_at_least``, ``get_member_state``,
``list_user_organization_ids``, ``list_member_user_ids``) so other
modules can resolve membership without importing tenancy internals. The write
side (add / change role / remove) is deliberately NOT part of any contract.

Contract semantics: the methods take NO session — every call runs in tenancy's
own read-only Unit of Work on tenancy's database, identically in-process and
over internal RPC (``SERVICE_BINDINGS``). The ``tenant_ids`` claims contributor
is likewise exposed as ``tenancy.claims.v1`` so an identity service deployed
alone still mints tokens with the allowlist; it is critical, so if tenancy
cannot answer, token issuance fails with 503 rather than minting a token
without ``tenant_ids``.

Staleness of the JWT ``tenant_ids`` allowlist
---------------------------------------------
The claims contributor embeds the user's organization ids in the access token
at mint time. To stop a token minted before a member was removed / left (or
before an org was deleted) from still selecting that tenant, tenancy calls the
kernel's ``revoke_user_tokens_on_commit(user_id, reason=...,
require_immediate=True)`` for every affected user (role changes and ownership
transfers too) — the mark is written before the change commits, or the change
is refused with 503 (same path identity uses): the
framework token checks (``current_context`` / ``require(...)`` /
``TenantScope``) then reject the stale access token with 401 and the client
refreshes, receiving a token whose ``tenant_ids`` reflects the change. A
request whose guards passed before the change committed still completes, so
authorization that must observe a removal with certainty still uses the live
contract — ``has_role_at_least`` / ``is_member`` (or the shared ``OrgRole``
atom).

A role CHANGE revokes the member's tokens too (owner decision): organization
roles are not token claims today (the contributor embeds only ``tenant_ids``)
and every role gate reads the role live, so a demotion is already effective on
the next request — the revocation ends the stale session and keeps a future
membership cache / role claims safe. The member re-authenticates.

Consumers of the member events apply ``version``-ed events with a version
guard and REFRESH on a version-less one (an instance from before the upgrade)
via ``get_member_state`` — see :mod:`sandloom.shared.membership`.

Member lifecycle events
-----------------------
Every way a member appears or changes is announced on the member channels —
``tenancy.member_added.v1`` (``add_member``, accepting an invitation, the
owner of a new organization), ``tenancy.member_role_changed.v1`` (role change,
both sides of an ownership transfer), ``tenancy.member_removed.v1`` — written
to the outbox in the change's own UoW. Each carries ``version``: a
per-``(organization_id, user_id)`` counter (``tenancy_member_versions``),
monotonic across removal and re-add, so consumers order and dedupe with a
version guard (delivery has no ordering guarantee). It is an optional field
added within v1 (additive; older version-less events still validate).
"""

from sandloom.sdk import define_module
from sandloom.sdk.module import ModuleAPI

from .claims import tenancy_claims
from .services.operations import OPERATIONS

MEMBERSHIP_SERVICE_V1 = "tenancy.membership.v1"


def _setup(core: ModuleAPI) -> None:
    # Tenant selection is authorization-sensitive: never mint an access token
    # without the allowlist when the tenancy module is present.
    core.add_claims_contributor(tenancy_claims, critical=True)
    # Declared planner operations: planned (and their executor logged) at boot.
    core.register_operations(*OPERATIONS)


# Discovered (sandloom.sdk define_module): the routers of ``api.__all__``, the
# producer classes of ``events`` and the read-only ``tenancy.membership.v1``
# provider of ``contracts`` (never the full MembershipService: its write
# methods would be callable by any module or internal RPC caller).
register = define_module(
    __name__,
    version="1.0.0",
    capabilities=["database", "eventbus"],
    setup=_setup,
)

__all__ = ["register", "MEMBERSHIP_SERVICE_V1"]
