"""Contract-anchored access checks (ADR-0014 decisions 8 to 12).

A provider marks a boolean read method of its contract with
:func:`access_check`; ``sandloom generate`` writes a typed atom class beside
the consumer's stub of that contract, carrying the declaration as a literal
(``__sandloom_atom__``). Every such atom is evaluated by one generic evaluator
here, :class:`ContractCheck`; the kernel names no module, contract, method or
role word, the literal does.

Binding sources (:class:`From`) are the kernel's own concepts:

* ``From.subject_id()``: the authenticated principal's user id;
* ``From.org(param=..., validate_against_token=...)``: the organization from
  the path or query parameter (the atom's ``org_param=``), else
  ``X-Tenant-Id``, else the bound tenant (:func:`resolve_org_id`);
* ``From.account_type()`` / ``From.account_id()``: the account the atom's
  required ``of=`` names (:func:`resolve_account`), a neutral target, not a
  billing notion.

Every other parameter of the method is a parameter of the atom (its
constructor), passed through unchanged, in signature order. The evaluator
calls the same provider method with the same arguments through the same
provider unit as a hand-written atom would (decision 9): no extra call.

A claim-tier atom (:class:`ClaimFactCheck`) is declared with ``claim=`` on
the owner's live check: an owner's :class:`~sandloom.shared.authz.facts.\
AccessFact` that names an ``atom``. It reads the owner's claim under its
provenance with zero I/O for ``Account.user()`` and delegates to the live
check for an organization account. Its ``statuses=`` / ``claim_key=`` are
bound to the owner's declaration (typed ``Literal`` in the generated class);
with the owner neither loaded nor bound it is indeterminate 501.

Outcomes: a denying provider is the declared category (402 or 403); no
provider is indeterminate 501 ``service_unavailable``; a shape mismatch
between the literal and the provider (an argument it no longer takes, a new
required one, a method it no longer serves, a result that is not a boolean)
is indeterminate 501 ``contract_mismatch`` inside the algebra, so ``Or`` may
still allow on another branch and ``Not`` never allows (decision 12). A
request value that does not bind (an invalid organization id) is refused
before any call, as by the kernel atoms. A bound provider that is down
(unreachable, a provider error, a timeout) is indeterminate 503
``owner_unavailable`` inside the algebra, logged and counted per branch
(K5, owner answer #19); an RPC authentication or allowlist refusal stays an
error.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

import sandloom.shared.authz.facts as _facts
from sandloom.core.facade import facts_contract_name
from sandloom.shared.authz._resolve import (
    ContractUnavailable,
    resolve_contract,
    resolve_org_id,
)
from sandloom.shared.authz.account import (
    _MISSING,
    Account,
    account_org_params,
    record_membership,
    require_account,
    resolve_account,
    tenant_witness,
)
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE, Requirement
from sandloom.shared.authz.metrics import increment
from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

#: Attribute :func:`access_check` sets on the provider method (read by name
#: in ``sandloom.core.module_api``, which does not import this package).
ACCESS_CHECK_ATTR = "__sandloom_access_check__"
#: Attribute of a generated atom class: the declaration's literal.
ATOM_ATTR = "__sandloom_atom__"
#: The decision code of a literal the provider no longer matches.
CONTRACT_MISMATCH = "contract_mismatch"

_KINDS = frozenset({"subject", "org", "account_type", "account_id"})
_CATEGORIES = frozenset(
    v for k, v in vars(Category).items() if k.isupper() and isinstance(v, str)
)
#: Provider answers that mean "the call does not fit the method": the
#: arguments do not bind, or the method is not served (local and remote).
_MISMATCH_CODES = frozenset({"contract_argument_invalid", "method_not_found"})
#: The decision code of a provider that is down (K5).
OWNER_UNAVAILABLE = "owner_unavailable"
#: RPC failures (503) that mean "the provider is down": unreachable, a
#: provider error, a timeout. Never an authentication or allowlist refusal
#: (``rpc_caller_forbidden``, ``rpc_auth_*``, ``rpc_identity_*``, ...): a
#: broken security invariant stays an error, not an indeterminate.
_DOWN_CODES = frozenset({"remote_service_unavailable", "rpc_timeout"})

logger = logging.getLogger("sandloom.access")


class From:
    """Where an access check's argument comes from (the kernel's sources)."""

    @staticmethod
    def subject_id() -> list[Any]:
        """The authenticated principal's user id."""
        return ["subject"]

    @staticmethod
    def org(
        *, param: str | None = "org_id", validate_against_token: bool = True
    ) -> list[Any]:
        """The organization: path or query ``param``, else ``X-Tenant-Id``,
        else the bound tenant. ``validate_against_token=False`` when the
        check itself is the authority on membership."""
        if param is not None and (not isinstance(param, str) or not param):
            raise ValueError("From.org(param=) is a parameter name or None")
        return ["org", param, bool(validate_against_token)]

    @staticmethod
    def account_type() -> list[Any]:
        """``"user"`` or ``"organization"``: the kind of the atom's ``of=``."""
        return ["account_type"]

    @staticmethod
    def account_id() -> list[Any]:
        """The id of the account the atom's ``of=`` names."""
        return ["account_id"]


def _claim_spec(fact: Any) -> dict[str, Any]:
    atom = getattr(fact, "atom", None)
    if not isinstance(atom, str) or not atom.isidentifier() or not atom[:1].isupper():
        raise ValueError(
            f"access_check claim=: the fact {getattr(fact, 'name', fact)!r} names "
            "no atom (AccessFact(atom=<class name>))"
        )
    keys = list(getattr(fact, "atom_claim_keys", ()) or ())
    if fact.claim_key is None or fact.claim_key not in keys:
        keys = [k for k in (fact.claim_key, *keys) if k]
    if not keys:
        raise ValueError(f"access_check claim=: the fact {fact.name!r} reads no claim")
    spec: dict[str, Any] = {
        "name": atom,
        "fact": fact.name,
        "owner": fact.owner_module,
        "claim_key": keys[0],
        "claim_keys": keys,
        "statuses": sorted(getattr(fact, "atom_statuses", ()) or ()),
        "category": fact.deny_category or Category.FORBIDDEN,
    }
    describe = getattr(fact, "atom_describe", None)
    if describe:
        spec["describe"] = describe
    return spec


def _witness_spec(
    name: str, bind: Mapping[str, list[Any]], values: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The ``witnesses_membership`` declaration: one organization and one
    subject bind, a JSON scalar for each other parameter (checked against
    the signature by the contract schema and at boot)."""
    if values is None:
        return {}
    kinds = sorted(source[0] for source in bind.values())
    if kinds != ["org", "subject"]:
        raise ValueError(
            f"access_check {name}: a membership witness binds exactly one "
            "From.org() and one From.subject_id(), nothing else"
        )
    if not isinstance(values, Mapping) or not all(
        isinstance(v, str | int | bool) for v in values.values()
    ):
        raise ValueError(
            f"access_check {name}: witnesses_membership maps each own "
            "parameter to the value that asks about membership alone"
        )
    return {"witnesses_membership": {str(k): v for k, v in values.items()}}


def access_check(
    name: str,
    *,
    bind: Mapping[str, list[Any]],
    category: str = Category.FORBIDDEN,
    values: Mapping[str, Iterable[Any]] | None = None,
    risk: Mapping[str, Mapping[str, Iterable[Any]]] | None = None,
    describe: str | None = None,
    claim: Any = None,
    witnesses_membership: Mapping[str, Any] | None = None,
) -> Callable[[Any], Any]:
    """Declare a boolean contract read method as the access check ``name``.

    ``bind`` maps method parameters to :class:`From` sources; the method's
    other parameters become the atom's own parameters, in signature order.
    ``values`` narrows one of them to a closed set (typed as a ``Literal``
    in the generated class, e.g. ``{"minimum": ["member", "admin",
    "owner"]}``). ``risk`` is a value table: ``{"admin": {"minimum":
    ["admin", "owner"]}}`` means the gate carries the ``admin`` risk when
    every listed parameter has one of the listed values. ``category`` is the
    denial category of a ``False`` answer (``Category.PAYMENT_REQUIRED``:
    402). ``describe`` is the denial label, ``{param}`` placeholders
    allowed. ``claim`` is the owner's
    :class:`~sandloom.shared.authz.facts.AccessFact` naming a claim-tier
    ``atom`` this check answers for an organization account.
    ``witnesses_membership`` declares the check a membership witness: a
    ``True`` answer, for any value of its own parameters, proves the
    subject (``From.subject_id()``) a member of the organization
    (``From.org()``); the mapping gives the value of each own parameter
    that asks about membership alone (``{"minimum": "member"}``). The
    tenant authority's witness decides ``tenant_kind`` (ADR-0014 decision
    13); only the authority the root assigned to ``tenant_ids`` counts.

    The declaration is part of the method's describe document and the
    contract snapshot: removing it, re-binding it or lowering a risk is an
    incompatible change (SANDLOOM-T004).
    """
    if not isinstance(name, str) or not name.isidentifier() or not name[:1].isupper():
        raise ValueError(f"access_check name must be a class name, not {name!r}")
    if category not in _CATEGORIES:
        raise ValueError(f"access_check {name}: unknown category {category!r}")
    for arg, source in bind.items():
        if not isinstance(source, list) or not source or source[0] not in _KINDS:
            raise ValueError(f"access_check {name}: bind[{arg!r}] is not a From source")
    spec: dict[str, Any] = {
        "name": name,
        "bind": {str(k): list(v) for k, v in bind.items()},
        "category": category,
    }
    if values:
        spec["values"] = {str(k): list(v) for k, v in values.items()}
    if risk:
        spec["risk"] = {
            str(r): {str(p): list(v) for p, v in conditions.items()}
            for r, conditions in risk.items()
        }
    if describe:
        spec["describe"] = describe
    if claim is not None:
        spec["claim"] = _claim_spec(claim)
    spec.update(_witness_spec(name, spec["bind"], witnesses_membership))

    def decorate(fn: Any) -> Any:
        setattr(getattr(fn, "__func__", fn), ACCESS_CHECK_ATTR, spec)
        return fn

    return decorate


def access_check_of(method: Any) -> dict[str, Any] | None:
    """The :func:`access_check` declaration of a provider method, else ``None``."""
    func = getattr(method, "__func__", method)
    for candidate in (func, getattr(func, "__wrapped__", None)):
        spec = getattr(candidate, ACCESS_CHECK_ATTR, None)
        if isinstance(spec, dict):
            return spec
    return None


#: The generated atom classes imported in this process (a consumer's
#: generated copies): their contracts are access-bearing (ADR-0014
#: decision 6, KB-13 source (c)). Weak: a class dropped with its module
#: leaves the set.
_GENERATED: weakref.WeakSet[type[Requirement]] = weakref.WeakSet()


def generated_access_contracts() -> frozenset[str]:
    """The contracts the imported generated atoms call (read at boot by
    ``create_app``'s access-provider sweep, never on a request)."""
    found: set[str] = set()
    for cls in list(_GENERATED):
        atom = cls.__dict__.get(ATOM_ATTR)
        if isinstance(atom, dict):
            contract = atom.get("contract")
            if isinstance(contract, str) and contract:
                found.add(contract)
            otherwise = atom.get("otherwise")
            if isinstance(otherwise, dict) and isinstance(
                otherwise.get("contract"), str
            ):
                found.add(otherwise["contract"])
    return frozenset(found)


def _indeterminate(
    code: str,
    reason: str,
    status: int,
    requirement: str,
    metadata: dict[str, Any] | None = None,
) -> PolicyIndeterminate:
    return PolicyIndeterminate(
        AccessDecision.error(
            code=code,
            category=Category.POLICY_MISCONFIGURED,
            http_status=status,
            reason=reason,
            failed_requirement=requirement,
            metadata=metadata,
        )
    )


def _resolve_service(ctx: AuthzContext, name: str, requirement: str) -> Any:
    """The provider of ``name`` (local or the bound proxy), else 501: only a
    genuinely absent service degrades; an error inside one propagates."""
    try:
        return resolve_contract(ctx, name)
    except ContractUnavailable as exc:
        raise _indeterminate(
            "service_unavailable",
            f"contract {name!r} is not configured",
            501,
            requirement,
            {"service": name, "error": str(exc)},
        ) from exc


def _mismatch(contract: str, method: str, detail: str, requirement: str) -> Exception:
    return _indeterminate(
        CONTRACT_MISMATCH,
        f"{contract}.{method}: the provider does not match the generated "
        f"access check ({detail}); run `sandloom generate` against the "
        "deployed provider",
        501,
        requirement,
        {"service": contract, "method": method},
    )


def _owner_unavailable(
    ctx: AuthzContext, contract: str, code: str, requirement: str
) -> Exception:
    """The provider is down (K5, owner answer #19): an indeterminate 503
    inside the algebra, so ``A | B`` still allows on a branch that reliably
    allowed and ``~`` never allows. Reported per branch, a sibling's allow
    included: a warning in the deny log and ``access_indeterminate_branch_total``."""
    logger.warning(
        "access check indeterminate: %s (%s, %s)",
        OWNER_UNAVAILABLE,
        requirement,
        contract,
        extra={
            "access_code": OWNER_UNAVAILABLE,
            "access_category": Category.POLICY_MISCONFIGURED,
            "failed_requirement": requirement,
            "access_indeterminate": True,
            "access_http_status": 503,
            "service": contract,
            "rpc_code": code,
            "source": ctx.source,
            "actor_id": str(ctx.subject.user_id),
        },
    )
    increment(ctx.core(), "access_indeterminate_branch_total")
    return _indeterminate(
        OWNER_UNAVAILABLE,
        f"contract {contract!r} is unavailable; try again later",
        503,
        requirement,
        {"service": contract, "error": code},
    )


class ContractCheck(Requirement):
    """The generic evaluator of a generated, contract-anchored atom.

    A generated subclass sets ``__sandloom_atom__`` (``contract``,
    ``method``, ``args`` in signature order, ``binds``, ``tier``,
    ``category``, and optionally ``values``, ``risk``, ``describe``) and a
    typed ``__init__`` that calls ``super().__init__(params, of=...,
    org_param=...)``. Every wiring-time answer (``tier``, ``contracts()``,
    ``org_params()``, ``risks()``, the denial category) is read from the
    literal (decision 10).
    """

    __sandloom_atom__: ClassVar[dict[str, Any]] = {}
    tier = TIER_LIVE

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        atom = cls.__dict__.get(ATOM_ATTR)
        if not isinstance(atom, dict) or not atom:
            return
        cls.tier = int(atom.get("tier", TIER_LIVE))
        cls.category = str(atom.get("category", Category.FORBIDDEN))
        _GENERATED.add(cls)

    def __init__(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        of: Account | None = None,
        org_param: str | None = None,
    ) -> None:
        atom = self.__sandloom_atom__
        if not atom:
            raise TypeError(
                "ContractCheck is the evaluator of a generated atom: import the "
                "atom from the contract's generated stub"
            )
        params = dict(params or {})
        for name, allowed in (atom.get("values") or {}).items():
            if params.get(name) not in allowed:
                raise ValueError(
                    f"{type(self).__name__} {name} must be one of "
                    f"{', '.join(map(str, allowed))}"
                )
        self.params = params
        self.org_param = org_param
        self._binds: dict[str, list[Any]] = dict(atom.get("binds") or {})
        self._args: tuple[str, ...] = tuple(atom.get("args") or ())
        self._contract = str(atom.get("contract"))
        self._method = str(atom.get("method"))
        template = atom.get("describe")
        # The label is fixed by the parameters: formatted once, not per request.
        self._label = (
            template.format(**params)
            if isinstance(template, str)
            else type(self).__name__
        )
        kinds = {source[0] for source in self._binds.values()}
        self.account: Account | None = None
        if kinds & {"account_type", "account_id"}:
            self.account = require_account(
                _MISSING if of is None else of, type(self).__name__
            )

    # -- wiring-time metadata (read by require(), create_app) ---------------
    def org_params(self) -> frozenset[str]:
        found: set[str] = set()
        for source in self._binds.values():
            if source[0] == "org":
                found.add(self.org_param or source[1] or "")
        if self.account is not None:
            found |= account_org_params(self.account)
        return frozenset(p for p in found if p)

    def contracts(self) -> frozenset[str]:
        return frozenset({self._contract})

    def needs_request_session(self) -> bool:
        # The provider runs the call in its own unit (local or remote), never
        # in the caller's session: require() opens none for it (K6).
        return False

    def risks(self) -> frozenset[str]:
        out: set[str] = set()
        for risk, conditions in (self.__sandloom_atom__.get("risk") or {}).items():
            if all(self.params.get(p) in vals for p, vals in conditions.items()):
                out.add(str(risk))
        return frozenset(out)

    def describe(self) -> str:
        return self._label

    # -- evaluation ----------------------------------------------------------
    async def _bind(self, ctx: AuthzContext, requirement: str) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        account: tuple[str, UUID] | None = None
        for arg, source in self._binds.items():
            kind = source[0]
            if kind == "subject":
                bound[arg] = ctx.subject.user_id
            elif kind == "org":
                bound[arg] = resolve_org_id(
                    ctx,
                    org_param=self.org_param or source[1],
                    requirement=requirement,
                    validate_against_token=bool(source[2]),
                )
            else:
                if account is None:
                    target = self.account
                    if target is None:  # pragma: no cover - set in __init__
                        raise TypeError("an account-bound access check needs of=")
                    account = await resolve_account(
                        ctx, target, requirement=requirement
                    )
                bound[arg] = account[0] if kind == "account_type" else account[1]
        return bound

    async def evaluate(self, ctx: AuthzContext) -> bool:
        requirement = self.describe()
        # Request values bind first: an invalid one is refused before any call.
        bound = await self._bind(ctx, requirement)
        service = _resolve_service(ctx, self._contract, requirement)
        try:
            args = [bound[a] if a in bound else self.params[a] for a in self._args]
        except KeyError as exc:
            raise _mismatch(
                self._contract,
                self._method,
                f"no value for {exc.args[0]!r}",
                requirement,
            ) from exc
        try:
            method = getattr(service, self._method)
        except AttributeError as exc:
            raise _mismatch(
                self._contract, self._method, "method not served", requirement
            ) from exc
        try:
            result = await method(*args)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in _MISMATCH_CODES:
                raise _mismatch(
                    self._contract, self._method, str(exc), requirement
                ) from exc
            if code in _DOWN_CODES and getattr(exc, "status_code", None) == 503:
                raise _owner_unavailable(
                    ctx, self._contract, str(code), requirement
                ) from exc
            raise
        if type(result) is not bool:
            raise _mismatch(
                self._contract,
                self._method,
                f"result {type(result).__name__} is not a boolean",
                requirement,
            )
        if result:
            witness = tenant_witness(ctx)
            if witness is not None:
                # The tenant authority's witness allowed: the caller is a
                # member, this request's tenant kind need not ask (KB-37).
                record_membership(
                    ctx,
                    self._contract,
                    self._method,
                    bound.get(witness.organization),
                    bound.get(witness.subject),
                )
        return result


class ClaimFactCheck(Requirement):
    """The generic evaluator of a generated claim-tier atom.

    For ``Account.user()`` it reads the owner's claim ``claim_key`` with zero
    I/O: interpreted by the owner's fact (its declarative rule, e.g. a grace
    period) under the token's provenance, or, with explicit ``statuses=`` or
    another declared ``claim_key=``, verbatim against those statuses. With
    the owner neither loaded nor bound it is indeterminate 501 (nobody may
    vouch for the claim). For an organization account it delegates to the
    owner's live access check (``otherwise`` in the literal).
    """

    __sandloom_atom__: ClassVar[dict[str, Any]] = {}
    tier = TIER_CLAIM
    _otherwise: ClassVar[type[ContractCheck] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        atom = cls.__dict__.get(ATOM_ATTR)
        if not isinstance(atom, dict) or not atom:
            return
        cls.category = str(atom.get("category") or Category.FORBIDDEN)
        otherwise = atom.get("otherwise")
        if isinstance(otherwise, dict) and otherwise:
            # The owner's live check, from the same literal; registered as a
            # generated atom by ContractCheck.__init_subclass__.
            cls._otherwise = type(
                f"{cls.__name__}Live",
                (ContractCheck,),
                {ATOM_ATTR: otherwise, "__module__": cls.__module__},
            )
        _GENERATED.add(cls)

    def __init__(
        self,
        statuses: Iterable[str] | None = None,
        *,
        of: Account = _MISSING,
        claim_key: str | None = None,
    ) -> None:
        atom = self.__sandloom_atom__
        if not atom:
            raise TypeError(
                "ClaimFactCheck is the evaluator of a generated atom: import the "
                "atom from the contract's generated stub"
            )
        self.account = require_account(of, type(self).__name__)
        default_key = str(atom.get("claim_key"))
        keys = [str(k) for k in atom.get("claim_keys") or (default_key,)]
        self.claim_key = default_key if claim_key is None else claim_key
        if self.claim_key not in keys:
            raise ValueError(
                f"{type(self).__name__} claim_key must be one of {', '.join(keys)} "
                "(the owner's declaration)"
            )
        declared = [str(s) for s in atom.get("statuses") or ()]
        self.statuses: frozenset[str] | None = None
        if statuses is not None:
            chosen = frozenset(statuses)
            unknown = sorted(s for s in chosen if s not in declared)
            if unknown:
                raise ValueError(
                    f"{type(self).__name__} statuses must be among "
                    f"{', '.join(declared)} (the owner's declaration), not "
                    f"{', '.join(map(str, unknown))}"
                )
            self.statuses = chosen
        self._verbatim = statuses is not None or self.claim_key != default_key
        if self.account.kind != "user":
            self.tier = TIER_LIVE

    def org_params(self) -> frozenset[str]:
        return account_org_params(self.account)

    def contracts(self) -> frozenset[str]:
        atom = self.__sandloom_atom__
        needed = {facts_contract_name(str(atom["owner"]))}
        if self.account.kind != "user" and self._otherwise is not None:
            needed.add(str(self._otherwise.__sandloom_atom__["contract"]))
        return frozenset(needed)

    def describe(self) -> str:
        return str(self.__sandloom_atom__.get("describe") or type(self).__name__)

    def _indeterminate(self, code: str, reason: str, status: int) -> Exception:
        return _indeterminate(code, reason, status, self.describe())

    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.account.kind != "user":
            tenant_type, _tenant = await resolve_account(
                ctx, self.account, requirement=self.describe()
            )
            if tenant_type != "user":
                if self._otherwise is None:
                    raise self._indeterminate(
                        "no_live_check",
                        f"{type(self).__name__}: the owner declares no live check "
                        "for an organization account",
                        501,
                    )
                return await self._otherwise({}, of=self.account).evaluate(ctx)
        if self._verbatim:
            return self._via_statuses(ctx)
        return await self._via_fact(ctx)

    def _owner_absent(self) -> Exception:
        atom = self.__sandloom_atom__
        return self._indeterminate(
            "fact_unregistered",
            f"access fact {atom['fact']!r}: its owner {atom['owner']!r} is neither "
            "loaded nor bound",
            501,
        )

    def _via_statuses(self, ctx: AuthzContext) -> bool:
        """The claim read verbatim against the chosen statuses, zero I/O; the
        owner must be part of the composition (loaded, or its facts contract
        bound), else nobody vouches for the claim."""
        owner = str(self.__sandloom_atom__["owner"])
        if not _facts.fact_owner_present(
            ctx, str(self.__sandloom_atom__["fact"]), owner
        ):
            raise self._owner_absent()
        status = ctx.subject.claim(self.claim_key)
        allowed = self.statuses
        if allowed is None:
            allowed = frozenset(
                str(s) for s in self.__sandloom_atom__.get("statuses") or ()
            )
        return isinstance(status, str) and status in allowed

    async def _via_fact(self, ctx: AuthzContext) -> bool:
        """The owner's fact decides (local, or described over its facts
        contract once and cached): its claim under provenance, else its live
        answer when the owner did not contribute to this token."""
        atom = self.__sandloom_atom__
        status = ctx.subject.claim(self.claim_key)
        try:
            fact = await _facts.lookup_access_fact(
                ctx, str(atom["fact"]), owner=str(atom["owner"])
            )
        except _facts.FactOwnerUnreachable as exc:
            raise self._indeterminate("fact_owner_unavailable", str(exc), 503) from exc
        if fact is None:
            raise self._owner_absent()
        subject = ctx.subject
        if _facts._claim_trustworthy(fact, ctx) or not subject.provenance_authoritative:
            return fact.interpret_claim(status)
        try:
            return await _facts.resolve_fact(fact, ctx, mode=_facts.MODE_LIVE)
        except _facts.FactDenied:
            return False
        except _facts.FactUnavailable as exc:
            raise self._indeterminate(
                "claim_unverifiable",
                f"claim not minted by {fact.owner_module!r} and no live check is "
                f"available: {exc}",
                503,
            ) from exc


__all__ = [
    "ACCESS_CHECK_ATTR",
    "ATOM_ATTR",
    "CONTRACT_MISMATCH",
    "OWNER_UNAVAILABLE",
    "ClaimFactCheck",
    "ContractCheck",
    "From",
    "access_check",
    "access_check_of",
    "generated_access_contracts",
]
