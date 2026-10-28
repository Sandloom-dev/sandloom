"""Access facts — one fact, several resolution modes chosen by configuration.

A *fact* is a boolean eligibility statement about the subject ("subscription is
active", "is an org admin"). The SAME fact can be answered three ways, sitting
on one freshness/cost curve:

- ``claim``      — read from the JWT snapshot (zero I/O; stale up to token TTL;
                   trustworthy only where the owning module ran at mint).
- ``projection`` — read a local denormalised read model the consumer maintains
                   from the owner's events (one indexed SELECT; eventually
                   consistent; identical in monolith and split).
- ``live``       — call the owner's service contract (synchronous truth; an RPC
                   when the owner is a different service).

Which mode is used is **configuration, not code**: an endpoint composes
``Entitled("subscription.active")`` and the deployment picks the mode via the
``ACCESS`` setting (per fact) or the fact's ``default_mode`` — but never weaker
than the fact's ``min_freshness`` floor (a money check declares ``live`` and
config can't downgrade it). ``auto`` prefers the cheapest *available* mode
(claim if its provenance is present → projection → live), so a split topology
self-heals to "slower but correct" instead of falsely denying.

Facts are part of the owner's PUBLIC contract. Registering a fact
(``core.register_access_fact``) also exposes the owner's ``<owner>.facts.v1``
contract (:mod:`sandloom.shared.authz.facts_contract`): ``describe()`` returns every
fact as a wire-safe descriptor — its claim key, a DECLARATIVE claim
interpretation (:class:`ClaimRule`, never a Python callable), freshness floor,
default mode and deny category — and ``evaluate(name, subject, request)``
answers the fact authoritatively in the owner's process. A consumer process
without the owner looks an unknown fact up on first use through the bound
``<owner>.facts.v1`` (cached), so ``Entitled("subscription.active")`` decides
the same in a monolith and in a split deployment (:func:`lookup_access_fact`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sandloom.core.facade import facts_contract_name
from sandloom.shared.authz._resolve import ContractUnavailable, resolve_contract
from sandloom.shared.authz.metrics import increment
from sandloom.shared.authz.results import Category

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

logger = logging.getLogger("sandloom.access.facts")

MODE_CLAIM = "claim"
MODE_PROJECTION = "projection"
MODE_LIVE = "live"
MODE_AUTO = "auto"

_VALID_MODES = frozenset({MODE_CLAIM, MODE_PROJECTION, MODE_LIVE, MODE_AUTO})
#: Freshness/cost rank — higher is fresher and more expensive.
_RANK = {MODE_CLAIM: 0, MODE_PROJECTION: 1, MODE_LIVE: 2}


#: How long a consumer caches an owner's ``describe()`` (seconds).
REMOTE_FACT_TTL_SECONDS = 300.0

_RULE_ONE_OF = "one_of"
_RULE_TRUTHY = "truthy"
_SCALAR = (str, int, float, bool)


@dataclass(frozen=True)
class ClaimRule:
    """Declarative claim -> bool interpretation: wire-safe, no callables.

    ``ClaimRule.one_of({"active", "trialing"})`` is True iff the claim value is
    one of the listed scalars; ``ClaimRule.truthy()`` is ``bool(value)``. The
    owner describes its facts with these, so a consumer in another process
    interprets the token claim EXACTLY like the owner does locally (one source
    of truth). A rule that depends on the owner's settings (billing's
    ``past_due`` grace) is a zero-argument factory on the fact
    (``AccessFact(claim_rule=factory)``), serialized with the owner's current
    settings at ``describe()`` time.
    """

    kind: str = _RULE_TRUTHY
    values: frozenset[Any] = frozenset()

    @classmethod
    def one_of(cls, values: Iterable[Any]) -> ClaimRule:
        return cls(kind=_RULE_ONE_OF, values=frozenset(values))

    @classmethod
    def truthy(cls) -> ClaimRule:
        return cls(kind=_RULE_TRUTHY)

    def interpret(self, value: Any) -> bool:
        if self.kind == _RULE_ONE_OF:
            return isinstance(value, _SCALAR) and value in self.values
        return bool(value)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"kind": self.kind}
        if self.kind == _RULE_ONE_OF:
            wire["values"] = sorted(self.values, key=repr)
        return wire

    @classmethod
    def from_wire(cls, raw: Any) -> ClaimRule | None:
        """Parse a wire rule; ``None`` for anything not understood.

        Fail closed: a consumer that cannot parse the rule never reads the
        claim and escalates to the owner's live answer instead.
        """
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        if kind == _RULE_TRUTHY:
            return cls.truthy()
        values = raw.get("values")
        if (
            kind == _RULE_ONE_OF
            and isinstance(values, list)
            and all(isinstance(v, _SCALAR) for v in values)
        ):
            return cls.one_of(values)
        return None


ProjectionReader = Callable[["AuthzContext"], Awaitable[bool]]
LiveResolver = Callable[[Any, "AuthzContext"], Awaitable[bool]]


class FactUnavailable(Exception):
    """A fact could not be resolved in any acceptable mode (a misconfiguration).

    Raised when an explicitly-requested mode cannot be honored (live service not
    bound, no projection reader) or ``auto`` exhausts every option. The atom
    maps this to a ``policy_misconfigured`` decision — never a silent allow.
    """


class FactDenied(Exception):
    """A resolver's *definite* denial for a reason other than "the fact is False".

    Raised by a projection reader / live resolver when the fact is
    inapplicable to the subject — e.g. ``organization.subscription.active``
    with no organization in context, or for an org the subject is not a
    member of. :class:`~sandloom.shared.authz.atoms_fact.Entitled` maps it to a
    definite deny with ``category`` (default ``forbidden``, 403) instead of
    the fact's ``deny_category`` — "you may not ask" is not "pay to unlock".
    """

    def __init__(
        self,
        reason: str,
        *,
        category: str = Category.FORBIDDEN,
        code: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category
        self.code = code


@dataclass(frozen=True)
class AccessFact:
    """Declarative binding of a fact name to its three resolution paths.

    A module registers one of these in ``register(core)`` via
    ``core.register_access_fact(...)``, wiring together mechanisms that already
    exist: its claims contributor (``claim_key``), an optional local projection
    (``projection_reader``) and its service contract (``live_service`` +
    ``live_resolver``).
    """

    name: str
    #: Module that owns this fact; ``claim`` mode is only trusted when this
    #: appears in ``subject.claims_sources`` (mint-time provenance). Also names
    #: the public ``<owner_module>.facts.v1`` contract consumers resolve it by.
    owner_module: str
    claim_key: str | None = None
    projection_reader: ProjectionReader | None = None
    live_service: str | None = None
    live_resolver: LiveResolver | None = None
    #: The freshest mode the fact may be downgraded to; config cannot weaken it.
    min_freshness: str = MODE_CLAIM
    default_mode: str = MODE_AUTO
    #: Denial category when the fact is False (e.g. ``Category.PAYMENT_REQUIRED``
    #: for a subscription fact → 402, consistent with ``Subscribed`` /
    #: ``SubscriptionActive`` / ``Credits``). ``None`` → ``forbidden`` (403).
    #: An explicit ``Entitled(..., category=...)`` overrides it; a resolver
    #: raising :class:`FactDenied` chooses its own category for that denial.
    deny_category: str | None = None
    #: Declarative claim interpretation (:class:`ClaimRule`), or a zero-arg
    #: factory returning one (rules that depend on the owner's settings); without
    #: one, a ``claim_key`` means truthiness.
    claim_rule: ClaimRule | Callable[[], ClaimRule] | None = None

    def __post_init__(self) -> None:
        if self.min_freshness not in _RANK:
            raise ValueError(
                f"AccessFact {self.name!r}: min_freshness must be one of "
                f"{sorted(_RANK)}, got {self.min_freshness!r}"
            )
        if self.default_mode not in _VALID_MODES:
            raise ValueError(
                f"AccessFact {self.name!r}: default_mode must be one of "
                f"{sorted(_VALID_MODES)}, got {self.default_mode!r}"
            )

    def current_claim_rule(self) -> ClaimRule | None:
        """The declarative claim rule in effect now (``None``: not declarative)."""
        rule = self.claim_rule
        if rule is None:
            return ClaimRule.truthy() if self.claim_key is not None else None
        if isinstance(rule, ClaimRule):
            return rule
        return rule()

    def interpret_claim(self, value: Any) -> bool:
        """Claim value -> bool, exactly as the owner interprets it."""
        rule = self.current_claim_rule()
        return rule.interpret(value) if rule is not None else bool(value)

    def to_wire(self) -> dict[str, Any]:
        """Wire-safe descriptor published by the owner's ``describe()``."""
        rule = self.current_claim_rule()
        return {
            "name": self.name,
            "owner_module": self.owner_module,
            # A claim the consumer cannot interpret declaratively is not
            # offered at all: the consumer then escalates to live.
            "claim_key": self.claim_key if rule is not None else None,
            "claim_rule": rule.to_wire() if rule is not None else None,
            "min_freshness": self.min_freshness,
            "default_mode": self.default_mode,
            "deny_category": self.deny_category,
            "evaluable": self.live_resolver is not None
            or self.projection_reader is not None,
        }

    @classmethod
    def build_facts_contract(cls, core: Any, owner: str) -> Any:
        """The ``<owner>.facts.v1`` provider (called by the kernel on register).

        A duck-typed hook, so the kernel exposes the contract without
        importing the authorization subsystem.
        """
        from sandloom.shared.authz.facts_contract import FactsContract  # noqa: PLC0415

        return FactsContract(core, owner)


def _configured_mode(fact: AccessFact) -> str:
    """The mode for ``fact`` from settings, clamped to its freshness floor."""
    from sandloom.core.di import settings  # noqa: PLC0415 - lazy, monkeypatchable in tests

    raw = settings.access.get(fact.name, fact.default_mode)
    if raw not in _VALID_MODES:
        logger.warning(
            "ACCESS[%s]=%r is not a valid mode; falling back to %r",
            fact.name,
            raw,
            fact.default_mode,
        )
        raw = fact.default_mode
    # A concrete (non-auto) mode may never be weaker than the floor.
    if raw != MODE_AUTO and _RANK[raw] < _RANK[fact.min_freshness]:
        logger.warning(
            "ACCESS[%s]=%r weakens the fact's min_freshness=%r; using the floor",
            fact.name,
            raw,
            fact.min_freshness,
        )
        raw = fact.min_freshness
    return raw


def _claim_trustworthy(fact: AccessFact, ctx: AuthzContext) -> bool:
    """True when the claim for ``fact`` was actually minted into this token.

    Provenance is mandatory: the owning module must be listed in the token's
    ``claims_sources``.

    * **Authoritative provenance** (the token carries ``claims_sources_failed``
      — every current issuer stamps it): trust the claim ONLY when the owner is
      in ``claims_sources`` and not in ``claims_sources_failed`` — even when
      ``claims_sources`` is empty (then nothing is trusted). A contributor that
      failed at mint (e.g. a remote billing service that was down) is never
      read as a genuine "false".
    * **Legacy token** (no ``claims_sources_failed`` key): unchanged behaviour —
      trust iff the owner is listed in ``claims_sources``.

    An untrusted claim makes ``auto`` escalate to projection/live and ``claim``
    mode refuse (``FactUnavailable``) rather than reading a claim nobody
    vouched for.
    """
    if fact.claim_key is None:
        return False
    subject = ctx.subject
    if getattr(subject, "provenance_authoritative", False):
        return (
            fact.owner_module in subject.claims_sources
            and fact.owner_module not in subject.claims_sources_failed
        )
    return fact.owner_module in subject.claims_sources


def _read_claim(fact: AccessFact, ctx: AuthzContext) -> bool:
    value = ctx.subject.claim(fact.claim_key) if fact.claim_key else None
    return fact.interpret_claim(value)


def _inc_metric(ctx: AuthzContext, name: str) -> None:
    increment(ctx.core(), name)


async def _resolve_live(fact: AccessFact, ctx: AuthzContext) -> bool:
    if fact.live_service is None or fact.live_resolver is None:
        raise FactUnavailable(f"fact {fact.name!r} has no live resolver")
    try:
        # core.get_service: local provider, else the SERVICE_BINDINGS RPC proxy.
        service = resolve_contract(ctx, fact.live_service)
    except ContractUnavailable as exc:
        raise FactUnavailable(
            f"fact {fact.name!r}: service {fact.live_service!r} is not configured"
        ) from exc
    return bool(await fact.live_resolver(service, ctx))


async def _resolve_projection(fact: AccessFact, ctx: AuthzContext) -> bool:
    if fact.projection_reader is None:
        raise FactUnavailable(f"fact {fact.name!r} has no projection reader")
    return bool(await fact.projection_reader(ctx))


async def _resolve_auto(fact: AccessFact, ctx: AuthzContext) -> bool:
    floor = _RANK[fact.min_freshness]
    # 1) claim — cheapest, only if the floor permits and provenance is present.
    if floor <= _RANK[MODE_CLAIM] and _claim_trustworthy(fact, ctx):
        return _read_claim(fact, ctx)
    # 2) projection — local read model, if the consumer maintains one.
    if floor <= _RANK[MODE_PROJECTION] and fact.projection_reader is not None:
        return await _resolve_projection(fact, ctx)
    # 3) live — authoritative, the last resort (an RPC in a split).
    if fact.live_resolver is not None:
        _inc_metric(ctx, "access_fact_live_fallback_total")
        return await _resolve_live(fact, ctx)
    raise FactUnavailable(
        f"fact {fact.name!r}: no resolution mode available (floor={fact.min_freshness})"
    )


async def resolve_fact(
    fact: AccessFact, ctx: AuthzContext, *, mode: str | None = None
) -> bool:
    """Resolve ``fact`` to a boolean, caching the result on ``ctx``.

    ``mode`` overrides the configured mode (used by tests and to force a path);
    otherwise the mode comes from settings, clamped to the fact's floor.
    """
    # Keyed by (name, requested mode): a forced ``mode="live"`` must never be
    # answered from a cheaper mode's cached result for the same fact.
    cache_key = (fact.name, mode)
    if cache_key in ctx.fact_cache:
        return ctx.fact_cache[cache_key]

    if mode is not None and mode not in _VALID_MODES:
        raise FactUnavailable(f"fact {fact.name!r}: unknown resolution mode {mode!r}")
    chosen = mode if mode is not None else _configured_mode(fact)
    if chosen != MODE_AUTO and _RANK[chosen] < _RANK[fact.min_freshness]:
        # A forced mode may not weaken the owner's freshness floor either.
        chosen = fact.min_freshness
    _inc_metric(ctx, f"access_fact_mode_{chosen}_total")

    if chosen == MODE_AUTO:
        value = await _resolve_auto(fact, ctx)
    elif chosen == MODE_CLAIM:
        if not _claim_trustworthy(fact, ctx):
            raise FactUnavailable(
                f"fact {fact.name!r}: claim mode requested but the owning module "
                f"{fact.owner_module!r} did not contribute claims to this token"
            )
        value = _read_claim(fact, ctx)
    elif chosen == MODE_PROJECTION:
        value = await _resolve_projection(fact, ctx)
    else:  # MODE_LIVE
        value = await _resolve_live(fact, ctx)

    ctx.fact_cache[cache_key] = value
    return value


# ---------------------------------------------------------------------------
# Facts across processes: the owner's ``<owner>.facts.v1`` contract
# ---------------------------------------------------------------------------


class FactOwnerUnreachable(FactUnavailable):
    """The fact's owner is bound but could not describe its facts (503).

    Distinct from "not configured" (501): the deployment is correct, the
    owner is down. Never a silent allow or a definite deny.
    """


def subject_to_wire(subject: Any) -> dict[str, Any]:
    """The authenticated subject as a wire-safe dict (for ``evaluate``)."""
    return {
        "user_id": str(subject.user_id),
        "roles": sorted(subject.roles),
        "permissions": sorted(subject.permissions),
        "claims": dict(subject.claims),
        "claims_sources": sorted(subject.claims_sources),
        "claims_sources_failed": sorted(subject.claims_sources_failed),
        "provenance_authoritative": bool(subject.provenance_authoritative),
    }


#: Request headers an owner-side resolver may consult (tenant selector only).
_FORWARDED_HEADERS = ("X-Tenant-Id",)


def request_to_wire(request: Any) -> dict[str, Any] | None:
    """The parts of the request a fact resolver may read, wire-safe.

    Path / query parameters (e.g. ``org_id``) and the tenant selector header.
    The bound tenant travels separately in the signed RPC caller context.
    """
    if request is None:
        return None
    path = getattr(request, "path_params", None) or {}
    query = getattr(request, "query_params", None) or {}
    headers = getattr(request, "headers", None) or {}
    forwarded = {}
    for name in _FORWARDED_HEADERS:
        value = headers.get(name)
        if value is not None:
            forwarded[name] = str(value)
    return {
        "path_params": {str(k): str(v) for k, v in dict(path).items()},
        "query_params": {str(k): str(v) for k, v in dict(query).items()},
        "headers": forwarded,
    }


def decode_evaluation(name: str, raw: Any) -> bool:
    """Map an owner's ``evaluate`` answer to bool / FactDenied / FactUnavailable."""
    if isinstance(raw, dict):
        if "value" in raw:
            return bool(raw["value"])
        denied = raw.get("denied")
        if isinstance(denied, dict):
            code = denied.get("code")
            raise FactDenied(
                str(denied.get("reason") or f"fact {name!r} denied"),
                category=str(denied.get("category") or Category.FORBIDDEN),
                code=code if isinstance(code, str) else None,
            )
        if "unavailable" in raw:
            raise FactUnavailable(str(raw["unavailable"]))
    raise FactUnavailable(f"fact {name!r}: malformed answer from its owner")


def _remote_live_resolver(name: str) -> LiveResolver:
    async def resolve(service: Any, ctx: AuthzContext) -> bool:
        raw = await service.evaluate(
            name, subject_to_wire(ctx.subject), request_to_wire(ctx.request)
        )
        return decode_evaluation(name, raw)

    return resolve


def remote_fact(descriptor: Any, contract: str) -> AccessFact | None:
    """Build a consumer-side :class:`AccessFact` from an owner's descriptor.

    Claim mode uses the owner's declarative rule; live mode calls the owner's
    ``evaluate`` over ``contract``. A malformed descriptor yields ``None``; an
    unknown freshness floor is read as ``live`` (never weaker than intended).
    """
    if not isinstance(descriptor, dict):
        return None
    name = descriptor.get("name")
    owner = descriptor.get("owner_module")
    if not isinstance(name, str) or not name or not isinstance(owner, str):
        return None
    rule = ClaimRule.from_wire(descriptor.get("claim_rule"))
    claim_key = descriptor.get("claim_key")
    if rule is None or not isinstance(claim_key, str):
        rule, claim_key = None, None
    floor = descriptor.get("min_freshness")
    if floor not in _RANK:
        floor = MODE_LIVE
    default_mode = descriptor.get("default_mode")
    if default_mode not in _VALID_MODES:
        default_mode = MODE_AUTO
    deny = descriptor.get("deny_category")
    evaluable = descriptor.get("evaluable", True) is not False
    return AccessFact(
        name=name,
        owner_module=owner,
        claim_key=claim_key,
        claim_rule=rule,
        live_service=contract if evaluable else None,
        live_resolver=_remote_live_resolver(name) if evaluable else None,
        min_freshness=str(floor),
        default_mode=str(default_mode),
        deny_category=deny if isinstance(deny, str) else None,
    )


async def _describe_owner(core: Any, owner: str) -> dict[str, AccessFact]:
    contract = facts_contract_name(owner)
    service = core.get_service(contract)
    raw = await service.describe()
    found: dict[str, AccessFact] = {}
    for descriptor in raw if isinstance(raw, list) else []:
        fact = remote_fact(descriptor, contract)
        if fact is not None:
            found[fact.name] = fact
    return found


#: Facts whose owner is not their first dotted segment (the framework's
#: subscription facts belong to billing).
WELL_KNOWN_FACT_OWNERS = {
    "subscription.active": "billing",
    "organization.subscription.active": "billing",
}


def fact_owner_contract(name: str) -> str:
    """The ``<owner>.facts.v1`` contract that publishes fact ``name``.

    The owner is the fact's first dotted segment (the lookup order of
    :func:`lookup_access_fact`), except for :data:`WELL_KNOWN_FACT_OWNERS`.
    """
    owner = WELL_KNOWN_FACT_OWNERS.get(name) or name.split(".", 1)[0]
    return facts_contract_name(owner)


async def lookup_access_fact(ctx: AuthzContext, name: str) -> AccessFact | None:  # noqa: C901
    """The fact ``name``: the local registration, else the owner's public one.

    Order: ``core.get_access_fact(name)`` (owner co-deployed) → every bound
    ``<owner>.facts.v1`` with no local provider (the owner named by the fact's
    first dotted segment first), whose ``describe()`` is cached on the core for
    :data:`REMOTE_FACT_TTL_SECONDS`. ``None`` when no owner knows the fact (the
    caller reports 501); :class:`FactOwnerUnreachable` (503) when a bound owner
    could not be asked and none of the others knows it.
    """
    core = ctx.core()
    if core is None:
        return None
    get_fact = getattr(core, "get_access_fact", None)
    local = get_fact(name) if callable(get_fact) else None
    if local is not None:
        return cast(AccessFact, local)
    owners_fn = getattr(core, "remote_fact_owners", None)
    owners = list(owners_fn()) if callable(owners_fn) else []
    if not owners:
        return None
    cache = getattr(core, "remote_access_fact_cache", None)
    if not isinstance(cache, dict):
        cache = {}
    now = time.monotonic()
    hit = cache.get(("fact", name))
    if hit is not None and hit[0] > now:
        return cast(AccessFact, hit[1])
    prefix = name.split(".", 1)[0]
    owners.sort(key=lambda o: o != prefix)
    failures: list[str] = []
    for owner in owners:
        described = cache.get(("owner", owner))
        if described is not None and described[0] > now:
            continue  # described recently and the fact was not among them
        try:
            facts = await _describe_owner(core, owner)
        except Exception as exc:  # noqa: BLE001 - owner down / RPC error
            logger.warning(
                "access fact %r: could not describe %r: %s",
                name,
                facts_contract_name(owner),
                exc,
            )
            failures.append(f"{facts_contract_name(owner)}: {exc}")
            continue
        expires = now + REMOTE_FACT_TTL_SECONDS
        cache[("owner", owner)] = (expires, None)
        for fact_name, fact in facts.items():
            cache[("fact", fact_name)] = (expires, fact)
        if name in facts:
            return facts[name]
    if failures:
        raise FactOwnerUnreachable(
            f"access fact {name!r}: owner unreachable ({'; '.join(failures)})"
        )
    return None
