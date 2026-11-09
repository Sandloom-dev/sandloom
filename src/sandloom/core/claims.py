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

import base64
import hashlib
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
#: The tenant a token binds (``TenantScope`` reads them before the grant):
#: no contributor writes them, and a verifier trusts them with the authority
#: of ``tenant_ids`` (ADR-0014 decision 1).
TENANT_SELECTORS: frozenset[str] = frozenset({"tenant_id", "org_id"})

#: The claim policy version the merge stamps (ADR-0014 decision 4, owner
#: decision #10): ``{"v": 1, "k": {key: digest of (key, authority)}}``.
CPV_CLAIM = "cpv"
CPV_VERSION = 1
#: The key of a ``<m>.claims.v1`` answer naming the import path of the
#: module that served it (the authority binding, ADR-0014 decision 4): the
#: issuer refuses a remote authority whose service answers for another
#: module than the map's, whatever its short name. The kernel writes it; it
#: never reaches a token.
AUTHORITY_CLAIM = "sandloom.authority"


@dataclass(frozen=True)
class Authority:
    """One claim key's authority in ``create_app(claim_authorities=...)``.

    A plain string value is ``Authority(<value>)``. ``module`` is the import
    path when ``name`` is a short (entry-point) name; ``previous`` is the one
    other authority a verifier accepts for the key during a rollout
    (ADR-0014 decision 4): declared with its identity in full
    (``Authority("identity", module="sandloom_identity")``), never by short
    name, it need not be loaded or bound here, and an issuer never stamps it.
    It exists for one rollout and is removed by the next deploy.
    """

    name: str
    module: str | None = None
    previous: Authority | None = None


@dataclass(frozen=True)
class LegacyClaimsTokens:
    """The bounded compatibility mode of the initial ``cpv`` enablement
    (ADR-0014 decision 4; an accepted temporary risk, G-RISK-14).

    ``until`` (an absolute UTC time, required, no default, no setting) is the
    end of the transition: the time by which the last issuer that stamps no
    ``cpv`` stopped. A verifier then trusts a token without ``cpv`` per key
    when the key's authority ran at its mint and was kept, its ``iat`` is
    before ``until`` and its ``exp - iat`` does not exceed
    ``ACCESS_TOKEN_EXPIRE_MINUTES``; from ``until + ACCESS_TOKEN_EXPIRE_MINUTES
    + JWT_LEEWAY_SECONDS`` on the mode is off by itself (fail closed). The
    framework never moves or extends ``until``: a later one is a new operator
    decision written in the composition root.
    """

    until: str | datetime

    def until_timestamp(self) -> float:
        """``until`` as a POSIX timestamp; a ``ValueError`` when it is not an
        absolute UTC time (a naive time or an offset other than UTC)."""
        value = self.until
        if isinstance(value, str):
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            value = datetime.fromisoformat(text)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError(
                f"LegacyClaimsTokens(until={self.until!r}): an absolute UTC time "
                "is required (e.g. '2026-12-01T00:00Z')"
            )
        if value.utcoffset() != UTC.utcoffset(None):
            raise ValueError(
                f"LegacyClaimsTokens(until={self.until!r}): write the time in UTC"
            )
        return value.timestamp()


def cpv_digest(key: str, identity: str) -> str:
    """The ``cpv`` entry of ``key`` under the authority ``identity`` (its
    import path): 64 bits of SHA-256, URL-safe base64 (11 characters)."""
    raw = hashlib.sha256(f"{key}\x00{identity}".encode()).digest()[:8]
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


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
    #: The verifier's map (the trusted view, decision 3): claim key -> import
    #: path of its current authority. The root's whole map, never filtered
    #: to this process: in a split a verifier trusts an authority that runs
    #: elsewhere, proven by the token's ``cpv`` digest and provenance (the
    #: issuer's mint check verified it). ``None``: the same as
    #: ``authorities``.
    verified: Mapping[str, str] | None = None
    #: claim key -> short name of its ``verified`` authority (``None``: the
    #: same as ``owners``).
    verified_owners: Mapping[str, str] | None = None
    #: claim key -> import path of the one ``previous=`` authority a verifier
    #: also accepts during a rollout (decision 4), and its short name.
    previous: Mapping[str, str] = field(default_factory=dict)
    previous_owners: Mapping[str, str] = field(default_factory=dict)
    #: The end of the initial enablement's transition (POSIX time) when the
    #: root passed ``legacy_tokens=LegacyClaimsTokens(until=...)``.
    legacy_until: float | None = None
    #: When this map took effect in this process (POSIX time): a token with
    #: a ``cpv`` this map does not accept, issued before it, is outdated (a
    #: refresh fixes it); one issued after it is a mismatch.
    effective_at: float = 0.0
    #: Every protected key: :data:`PROTECTED_BASE` plus the map's keys.
    protected: frozenset[str] = field(init=False)
    #: The digests an issuer stamps: key -> ``cpv_digest(key, authority)``.
    digests: Mapping[str, str] = field(init=False)
    #: The verifier's accepted ``(owner short name, digest)`` per key: the
    #: current authority first, then a ``previous=`` one.
    accepted: Mapping[str, tuple[tuple[str, str], ...]] = field(init=False)
    #: Identifies the verifier policy in the verified-token cache key.
    marker: str = field(init=False)

    def __post_init__(self) -> None:
        authorities = MappingProxyType(dict(self.authorities))
        object.__setattr__(self, "authorities", authorities)
        object.__setattr__(self, "owners", MappingProxyType(dict(self.owners)))
        verified = dict(authorities if self.verified is None else self.verified)
        object.__setattr__(self, "verified", MappingProxyType(verified))
        verified_owners = dict(
            self.owners if self.verified_owners is None else self.verified_owners
        )
        object.__setattr__(self, "verified_owners", MappingProxyType(verified_owners))
        previous = MappingProxyType(dict(self.previous))
        object.__setattr__(self, "previous", previous)
        previous_owners = dict(self.previous_owners)
        object.__setattr__(self, "previous_owners", MappingProxyType(previous_owners))
        object.__setattr__(
            self,
            "protected",
            PROTECTED_BASE | frozenset(authorities) | frozenset(verified),
        )
        object.__setattr__(
            self,
            "digests",
            MappingProxyType({k: cpv_digest(k, p) for k, p in authorities.items()}),
        )
        accepted: dict[str, tuple[tuple[str, str], ...]] = {}
        for key, path in verified.items():
            entries = [(verified_owners[key], cpv_digest(key, path))]
            if key in previous:
                old = previous[key]
                entries.append((previous_owners[key], cpv_digest(key, old)))
            accepted[key] = tuple(entries)
        object.__setattr__(self, "accepted", MappingProxyType(accepted))
        object.__setattr__(
            self,
            "marker",
            hashlib.sha256(
                repr(
                    (
                        sorted(verified.items()),
                        sorted(previous.items()),
                        self.legacy_until,
                        self.effective_at,
                        self.assigned,
                    )
                ).encode()
            ).hexdigest()[:16],
        )

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


# ---------------------------------------------------------------------------
# The sealed mint (ADR-0014 decision 5, owner answer #13)
# ---------------------------------------------------------------------------

#: Provenance and the policy marker: only the merge (``collect_claims``) or
#: the kernel test authority writes them into a token.
PROVENANCE_KEYS: frozenset[str] = frozenset(
    {"claims_sources", "claims_sources_failed", CPV_CLAIM}
)
#: The token envelope a minting caller may set in ``data``: the subject, the
#: token type, the step-up instant and the registered claims
#: ``create_access_token`` otherwise stamps itself.
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "sub",
        "token_type",
        "auth_time",
        "iss",
        "aud",
        "exp",
        "nbf",
        "iat",
        "jti",
        "iat_us",
    }
)
#: ``UserContext``'s structural fields (shared/subject.py): formed by the
#: kernel from the token, never minted by a caller (``user_id`` wins over
#: ``sub``; ``provenance_authoritative`` decides how absent provenance reads;
#: ``claims`` is merged into every owner's namespaced claims).
SUBJECT_STRUCTURE_KEYS: frozenset[str] = frozenset(
    {"user_id", "provenance_authoritative", "claims"}
)


class UnsealedClaimsError(ValueError):
    """SANDLOOM-R017: a token mint received protected claims, provenance or
    the claim policy marker outside a sealed ``collect_claims`` result, or a
    service token carried one."""

    sandloom_code = "SANDLOOM-R017"


#: The kernel's seal: only code holding this object builds a
#: :class:`SealedClaims` (``collect_claims`` and the kernel test authority).
_SEAL = object()


class SealedClaims(Mapping[str, Any]):
    """The verified result of ``collect_claims``: the only carrier of
    protected claims, provenance and ``cpv`` that ``create_access_token``
    accepts (``create_access_token(data, claims=sealed)``).

    Read-only. Spreading it (``{**sealed}``) or copying it gives a plain
    ``dict``, which the mint refuses for those keys (SANDLOOM-R017): the
    seal does not survive an edit.
    """

    __slots__ = ("_data", "_seal")

    _data: dict[str, Any]
    _seal: object

    def __init__(self, data: Mapping[str, Any], *, seal: object = None) -> None:
        if seal is not _SEAL:
            raise UnsealedClaimsError(
                "SANDLOOM-R017: SealedClaims are built by collect_claims only"
            )
        # A fresh dict is owned as is (``_seal_claims`` callers hand over
        # one they built and drop); anything else is copied.
        owned = data if type(data) is dict else dict(data)
        object.__setattr__(self, "_data", owned)
        object.__setattr__(self, "_seal", seal)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SealedClaims are read-only")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"SealedClaims({dict(self._data)!r})"

    def is_sealed(self) -> bool:
        """True for a result the kernel built (always, unless forged)."""
        return self._seal is _SEAL


def _seal_claims(data: Mapping[str, Any]) -> SealedClaims:
    """Kernel-internal: seal a merge result (``collect_claims``) or the
    kernel test authority's contribution (``sandloom.core.testing.auth``).
    A ``dict`` is taken over, not copied: the caller built it for the seal
    and keeps no reference."""
    return SealedClaims(data, seal=_SEAL)


#: The last policy :func:`unsealed_keys` resolved, and its set (one policy
#: per composition: the mint path builds the set once, not per token).
_UNSEALED: list[tuple[ClaimAuthorities, frozenset[str]]] = []


def unsealed_keys(policy: ClaimAuthorities) -> frozenset[str]:
    """The keys a mint accepts only inside :class:`SealedClaims`, under
    ``policy``: every protected claim, the tenant selectors, provenance,
    ``cpv``, ``UserContext``'s structural fields and the authority binding.
    """
    if _UNSEALED and _UNSEALED[0][0] is policy:
        return _UNSEALED[0][1]
    keys = (
        policy.protected
        | TENANT_SELECTORS
        | PROVENANCE_KEYS
        | SUBJECT_STRUCTURE_KEYS
        | frozenset({AUTHORITY_CLAIM})
    )
    _UNSEALED[:] = [(policy, keys)]
    return keys


def sealed_data(claims: SealedClaims) -> dict[str, Any]:
    """Kernel-internal: the claims a sealed result carries, for the mint to
    merge (never handed to a caller)."""
    return claims._data


def refuse_unsealed(
    data: Mapping[str, Any], policy: ClaimAuthorities, what: str
) -> None:
    """Raise :class:`UnsealedClaimsError` when ``data`` carries a key of
    :func:`unsealed_keys`."""
    refused = unsealed_keys(policy)
    if refused.isdisjoint(data):
        return
    bad = sorted(k for k in data if k in refused)
    if bad:
        raise UnsealedClaimsError(
            f"SANDLOOM-R017: {what} carries {', '.join(bad)} outside a sealed "
            "collect_claims result: protected claims, provenance and cpv come "
            "only from collect_claims (create_access_token(data, "
            "claims=sealed)); a token that grants no user rights is "
            "create_service_token; a test token is mint_test_token / as_user "
            "(sandloom.sdk.testing)"
        )
