"""Tier-1 atoms: pure checks over JWT claims (zero I/O).

``Perm`` and ``Role`` read only :class:`UserContext` data carried in the
access token, so they evaluate without any database or network access. Claims
are populated at mint time by per-module contributors (see ``core.claims``).

The domain atoms (``Subscribed``, ``Feature``, ``WithinLimit``, ...) are not
here: each is generated from its provider's ``access_check`` declaration into
the consumer's contract stub (ADR-0014 decision 8). The helpers below are the
claim-reading rules those generated atoms share (``claim_read``,
``contract_check``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sandloom.core.auth import claims_view as _view
from sandloom.shared.authz.algebra import TIER_CLAIM, Requirement
from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext


def _raise_if_policy_indeterminate(ctx: AuthzContext, key: str, atom: str) -> None:
    """Raise the indeterminate of a protected claim the trusted view removed
    for a reason a refresh or a retry may change (ADR-0014 decision 3):
    ``token_policy_outdated`` (401: the token predates the claim policy, one
    refresh fixes it) or ``claims_policy_mismatch`` (503: issuer and verifier
    disagree). Inside the algebra, so ``A | B`` still allows on ``B`` and
    ``~A`` never allows. Any other reason is a definite deny (the caller
    answers ``False``)."""
    untrusted = getattr(ctx.subject, "untrusted_claims", None) or {}
    reason = untrusted.get(key)
    if reason == _view.OUTDATED:
        raise PolicyIndeterminate(
            AccessDecision.error(
                code=_view.OUTDATED,
                category=Category.UNAUTHENTICATED,
                reason=f"the token predates the claim policy of {key!r}: refresh once",
                http_status=401,
                failed_requirement=atom,
            )
        )
    if reason == _view.MISMATCH:
        raise PolicyIndeterminate(
            AccessDecision.error(
                code=_view.MISMATCH,
                reason=f"issuer and verifier disagree on the authority of {key!r}",
                http_status=503,
                failed_requirement=atom,
            )
        )


class Perm(Requirement):
    """Subject holds the fine-grained permission ``perm``.

    The permission is stored as ``perm`` — ``code`` stays the machine-readable
    denial code of the atom (``"Perm"``), not the permission name.
    """

    tier = TIER_CLAIM

    def __init__(self, perm: str) -> None:
        self.perm = perm

    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.perm in ctx.subject.permissions:
            return True
        _raise_if_policy_indeterminate(ctx, "permissions", self.describe())
        return False

    def describe(self) -> str:
        return f"permission '{self.perm}'"


class Role(Requirement):
    """Subject holds the global role ``role``.

    The role is stored as ``role`` — ``code`` stays the machine-readable denial
    code of the atom (``"Role"``), not the role name.
    """

    tier = TIER_CLAIM

    def __init__(self, role: str) -> None:
        self.role = role

    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.role in ctx.subject.roles:
            return True
        _raise_if_policy_indeterminate(ctx, "roles", self.describe())
        return False

    def risks(self) -> frozenset[str]:
        return frozenset({"admin"}) if self.role == "admin" else frozenset()

    def describe(self) -> str:
        return f"role '{self.role}'"


def _claim_authority_failed(ctx: AuthzContext, key: str) -> bool:
    """Whether the token's claim ``key`` is absent because its authority
    failed at the mint (ADR-0014 decision 7, billing option (c)).

    Read from the token's authoritative provenance and the composition's
    claim authorities (``core.claim_authorities``), zero I/O:

    * no authoritative provenance, or nothing failed: ``False`` (the
      authority ran, so an absent value is its "no");
    * the composition assigns ``key``: whether that authority (or the one
      ``previous=`` authority of a rollout) is in ``claims_sources_failed``;
      an unassigned key is a definite deny (``False``). Read from the
      VERIFIER's map (``verified_owners``, the root's whole map), never the
      mint map a split service narrows to what it composes: a verifier
      running no plans authority still knows whose failure it was;
    * no assignment readable here (no core, or a root without a map): any
      failed source might have been the authority, so ``True`` (the caller
      answers indeterminate, which never allows; a deny could be inverted
      by ``Not``).
    """
    subject = ctx.subject
    failed = getattr(subject, "claims_sources_failed", None)
    if not getattr(subject, "provenance_authoritative", False) or not failed:
        return False
    authorities = getattr(ctx.core(), "claim_authorities", None)
    if authorities is None or not getattr(authorities, "assigned", False):
        return True
    owner = authorities.verified_owners.get(key)
    if owner is None:
        return False
    return owner in failed or authorities.previous_owners.get(key) in failed
