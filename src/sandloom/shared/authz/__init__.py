"""Unified, claims-first authorization for Sandloom.

A single composable algebra covers RBAC (``Role``/``Perm``), conditional access
(``Subscribed``/``Feature``/``WithinLimit``/``Credits``) and resource ownership
(``Owner``). Policies compose with ``& | ~`` and evaluate cheapest-tier first:
claim checks (zero I/O) before resource loads before live service calls.

Business modules import only from here — never a concrete backend or another
business module. Example::

    from sandloom.shared.authz import require, authorize_resource, Perm, Owner, Role

    @router.post(
        "/x",
        dependencies=[Depends(require(Subscribed(of=Account.user()) & Perm("p.w")))],
    )

    @router.patch("/posts/{post_id}")
    async def update_post(
        post: Post = Depends(authorize_resource(
            loader=PostService.get_or_404, by="post_id", cast=UUID,
            policy=Perm("post.write") & (Owner(lambda p: p.author_id) | Role("admin")),
        )),
    ): ...
"""

from sandloom.shared.authz.account import Account
from sandloom.shared.authz.algebra import (
    TIER_CLAIM,
    TIER_LIVE,
    TIER_RESOURCE,
    And,
    Not,
    Or,
    Requirement,
)
from sandloom.shared.authz.atoms_claims import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    Feature,
    Perm,
    Role,
    Subscribed,
    WithinLimit,
)
from sandloom.shared.authz.atoms_fact import Entitled
from sandloom.shared.authz.atoms_live import Credits, OrgRole, SubscriptionActive
from sandloom.shared.authz.atoms_resource import Owner
from sandloom.shared.authz.constants import Permissions, Roles
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.deps import (
    authorize,
    authorize_resource,
    current_context,
    require,
)
from sandloom.shared.authz.enforce import (
    SOURCE_HTTP,
    SOURCE_INTERNAL,
    SOURCE_SYSTEM,
    SOURCE_TASK,
    enforce,
    service_subject,
    system_subject,
)
from sandloom.shared.authz.facts import (
    AccessFact,
    ClaimRule,
    FactDenied,
    FactOwnerUnreachable,
    FactUnavailable,
    lookup_access_fact,
    resolve_fact,
)
from sandloom.shared.authz.live_contracts import (
    ENTITLEMENTS,
    MEMBERSHIP,
    EntitlementsAdapter,
    MembershipAdapter,
    register_live_adapter,
)
from sandloom.shared.authz.projection import (
    make_rebuild_gate,
    register_authz_projection,
)
from sandloom.shared.authz.results import (
    AccessDecision,
    AccessDenied,
    Category,
    PolicyIndeterminate,
)

__all__ = [
    # algebra
    "Requirement",
    "And",
    "Or",
    "Not",
    "TIER_CLAIM",
    "TIER_RESOURCE",
    "TIER_LIVE",
    # authorization subjects (spec §7)
    "Account",
    # atoms
    "Perm",
    "Role",
    "Subscribed",
    "Feature",
    "WithinLimit",
    "Credits",
    "SubscriptionActive",
    "OrgRole",
    "Owner",
    "Entitled",
    "ACTIVE_SUBSCRIPTION_STATUSES",
    # context
    "AuthzContext",
    # deps / entry points
    "current_context",
    "require",
    "authorize",
    "authorize_resource",
    # enforce chokepoint (HTTP / use-case / task)
    "enforce",
    "system_subject",
    "service_subject",
    "SOURCE_HTTP",
    "SOURCE_TASK",
    "SOURCE_INTERNAL",
    "SOURCE_SYSTEM",
    # structured results
    "AccessDecision",
    "AccessDenied",
    "PolicyIndeterminate",
    "Category",
    # configurable resolution (facts)
    "AccessFact",
    "ClaimRule",
    "FactUnavailable",
    "FactOwnerUnreachable",
    "FactDenied",
    "resolve_fact",
    "lookup_access_fact",
    # pluggable live-contract adapters (Credits / SubscriptionActive / OrgRole)
    "register_live_adapter",
    "EntitlementsAdapter",
    "MembershipAdapter",
    "ENTITLEMENTS",
    "MEMBERSHIP",
    # authorization projections
    "register_authz_projection",
    "make_rebuild_gate",
    # vocabulary
    "Permissions",
    "Roles",
]
