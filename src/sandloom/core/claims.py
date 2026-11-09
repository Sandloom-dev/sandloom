"""Token claims contributors and the claim authorities that may write them.

A module declares what it wants embedded in a freshly minted access token by
registering a *claims contributor* — ``async (user_id) -> dict`` — via
``core.add_claims_contributor``. The contributor reads its module's data
through ``contract_session()`` (the module's own Unit of Work). At mint time
(login / refresh, the cold path) the kernel runs every contributor in its
owning module's own Unit of Work (remote ones over RPC as
``<module>.claims.v1``) and merges the results into the JWT payload. This
keeps module isolation intact: ``identity`` mints the token but never imports
``billing``; ``billing`` simply contributes its own ``subscription_status`` /
``plan_code`` claims.

Contributors run on the cold path only, so the per-request authorization path
stays pure claim reads (zero I/O). A failing non-critical contributor is logged
and recorded in ``claims_sources_failed`` (its claims are absent and the
matching claim atoms escalate or fail closed); a failing *critical* one aborts
minting with ``ClaimsUnavailable`` (503). See ``CoreFacade.collect_claims``.

A protected claim (:data:`PROTECTED_BASE` and every key of the composition's
map) is accepted only from the authority the composition root assigned it
(:class:`ClaimAuthorities`, ADR-0014 decisions 1 and 2): any other
contributor writing it is a violator, and an optional violator loses its whole
result (``CoreFacade.collect_claims``). The kernel holds no default map and
names no module; :mod:`sandloom.core.app_factory.claim_authorities` resolves
the root's map and refuses the boot on an assignment it cannot honour
(SANDLOOM-B035). This module is data only: it imports no loader.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import UUID

#: The current contributor signature: ``async (user_id) -> dict`` of
#: JSON-serializable claim fields; data is read via ``contract_session()``.
ClaimsContributor = Callable[[UUID], Awaitable[dict[str, Any]]]

#: The entry of ``claim_authorities`` naming the module that mints tokens
#: (adversarial X2): an ``issues_tokens=True`` module that is not this one,
#: or this one loaded without ``issues_tokens=True``, refuses the boot.
ISSUER_ENTRY = "issuer"

#: Always protected: the kernel's own authorization concepts (``Role``,
#: ``Perm``, ``TenantScope`` and PostgreSQL row security read them).
ALWAYS_PROTECTED: frozenset[str] = frozenset({"roles", "permissions", "tenant_ids"})
#: The issuer's fields (answer #10): protected and the map's issuer's only.
#: A map entry naming another module for one refuses the boot; a map with an
#: issuer that names none assigns them to the issuer, which writes them at
#: the mint through the same check (``collect_claims(issuer_claims=...)``).
ISSUER_FIELDS: frozenset[str] = frozenset({"email", "email_verified"})
#: Keys the kernel's claim atoms read to grant access (``Subscribed``,
#: ``Feature``, ``WithinLimit``): protected whether or not the map names them
#: (answer #10). Every kernel reader takes an absent value as "not granted",
#: never as allow: ``subscription_status`` escalates to its owner's live
#: subscription fact when the writer is in ``claims_sources_failed``, the plan
#: keys deny. So these are the only keys whose authority may be registered
#: non-critical (decision 7, billing option (c)): a billing outage still mints
#: a login.
FAIL_SAFE_KEYS: frozenset[str] = frozenset(
    {"subscription_status", "plan_code", "features", "limits"}
)
#: Protected in every composition, assigned or not.
PROTECTED_BASE: frozenset[str] = ALWAYS_PROTECTED | ISSUER_FIELDS | FAIL_SAFE_KEYS


@dataclass(frozen=True)
class ClaimAuthorities:
    """The resolved claim-authority assignment of one composition.

    ``authorities`` maps a claim key to the import path of its authority and
    ``owners`` the same key to the short module name the merge stamps that
    authority under (``claims_sources``); ``issuer`` is the import path of the
    token issuer (``None``: none assigned). ``assigned`` is ``False`` only
    when the composition root passed no map at all (a lean root): every
    protected key is then unassigned, so no contributor may write one.

    Immutable: ``authorities`` and ``owners`` are read-only views of copies
    taken at construction, so the one-shot assignment
    (``CoreFacade._assign_claim_authorities``) cannot be edited in place
    after the startup checks passed.
    """

    authorities: Mapping[str, str] = field(default_factory=dict)
    owners: Mapping[str, str] = field(default_factory=dict)
    issuer: str | None = None
    assigned: bool = False
    #: Every protected key: :data:`PROTECTED_BASE` plus the map's keys.
    protected: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        authorities = MappingProxyType(dict(self.authorities))
        object.__setattr__(self, "authorities", authorities)
        object.__setattr__(self, "owners", MappingProxyType(dict(self.owners)))
        object.__setattr__(self, "protected", PROTECTED_BASE | frozenset(authorities))

    def owner_of(self, key: str) -> str | None:
        """Short name of ``key``'s authority (``None``: unassigned)."""
        return self.owners.get(key)

    @staticmethod
    def critical_required(key: str) -> bool:
        """Whether ``key``'s authority must be a critical contributor.

        Decision 7: only a :data:`FAIL_SAFE_KEYS` key may come from a
        non-critical authority; ``roles``, ``permissions``, ``tenant_ids``,
        the issuer's fields and every key an application assigns need theirs
        critical (``Role``, ``Perm`` and ``TenantScope`` have no live
        fallback, and the kernel cannot know an application key's readers).
        """
        return key not in FAIL_SAFE_KEYS


#: A composition whose root passed no ``claim_authorities``: nothing assigned.
NO_AUTHORITIES = ClaimAuthorities()
