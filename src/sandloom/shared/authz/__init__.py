"""Unified, claims-first authorization for Sandloom.

A single composable algebra covers RBAC (``Role``/``Perm``), registered access
facts (``Entitled``), resource ownership (``Owner``) and the access checks a
provider declares (``access_check``: each consumer's generated stub carries its
atoms, such as billing's ``Subscribed`` or tenancy's ``OrgRole``). Policies
compose with ``& | ~`` and evaluate cheapest-tier first: claim checks (zero
I/O) before resource loads before live service calls.

Example::

    from sandloom.shared.authz import require, authorize_resource, Perm, Owner, Role

    @router.post("/x", dependencies=[Depends(require(Perm("p.w")))])

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
from sandloom.shared.authz.atoms_claims import Perm, Role
from sandloom.shared.authz.atoms_fact import Entitled
from sandloom.shared.authz.atoms_resource import Owner
from sandloom.shared.authz.claim_read import ClaimReadCheck, claim_atom
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import (
    AccessUnknown,
    ClaimFactCheck,
    ContractCheck,
    From,
    access_check,
)
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
    "Owner",
    "Entitled",
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
    "AccessUnknown",
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
    # contract-anchored access checks (ADR-0014 decision 8)
    "access_check",
    "From",
    "ContractCheck",
    "ClaimFactCheck",
    "ClaimReadCheck",
    "claim_atom",
    # authorization projections
    "register_authz_projection",
    "make_rebuild_gate",
]
