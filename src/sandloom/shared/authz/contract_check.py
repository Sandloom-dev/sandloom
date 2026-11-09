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
provider that has no source for the answer raises
:class:`~sandloom.core.module_api.AccessUnknown`: indeterminate 501
``access_unknown`` inside the algebra, the same way (an unknown is never a
``False`` that ``Not`` would turn into an allow). A request value that does
not bind (an invalid organization id) is refused before any call, as by the
kernel atoms. A bound provider that is down (unreachable, a provider error, a
timeout) is indeterminate 503
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
from sandloom.core.auth import claims_view as _view
from sandloom.core.claims import CPV_CLAIM, CPV_VERSION
from sandloom.core.facade import facts_contract_name
from sandloom.core.module_api import AccessUnknown
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
from sandloom.shared.authz.atoms_claims import (
    _claim_authority_failed,
    _raise_if_policy_indeterminate,
)
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
#: The decision code of a provider with no source for the answer.
ACCESS_UNKNOWN = str(AccessUnknown.code)
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
        # What the fact's primary claim proven absent means: the owner's
        # declaration (KB-33h), "unknown" unless it declares "deny".
        "absent_claim": getattr(fact, "absent_claim", None) or "unknown",
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


#: The generated claim-tier atom classes imported in this process
#: (``ClaimFactCheck`` / ``ClaimReadCheck`` subclasses): the claim keys
#: their literals read are protected claims (ADR-0014 decisions 1 and 7).
_CLAIM_ATOMS: weakref.WeakSet[type[Requirement]] = weakref.WeakSet()


def generated_claim_keys() -> frozenset[str]:
    """The claim keys the imported generated claim atoms read (their
    literal's ``claim_key`` / ``claim_keys``); read at boot by ``create_app``
    to protect them, never on a request."""
    found: set[str] = set()
    for cls in list(_CLAIM_ATOMS):
        atom = cls.__dict__.get(ATOM_ATTR)
        if not isinstance(atom, dict):
            continue
        keys = atom.get("claim_keys") or [atom.get("claim_key")]
        found.update(k for k in keys if isinstance(k, str) and k)
    return frozenset(found)


def _declares(member: Any) -> bool:
    if not isinstance(member, classmethod | staticmethod) and not callable(member):
        return False
    return access_check_of(member) is not None


def declares_access_checks(impl: Any) -> bool:
    """Whether a contract implementation (a class or an instance) or a class
    it derives from (its declared ``@contract`` class) carries an
    :func:`access_check` method: its contract is access-bearing (ADR-0014
    decision 6). Reads the class dictionaries only (no attribute is
    computed); called at registration, never on a request."""
    cls = impl if isinstance(impl, type) else type(impl)
    return any(
        _declares(member) for klass in cls.__mro__ for member in vars(klass).values()
    )


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


def _access_unknown(
    contract: str, method: str, detail: Any, requirement: str
) -> Exception:
    """The provider has no source for the answer: indeterminate 501 inside
    the algebra (``Not`` never allows, ``Or`` only on another reliable
    allow), the provider's own words in the reason."""
    said = detail if isinstance(detail, str) and detail else "no source"
    return _indeterminate(
        ACCESS_UNKNOWN,
        f"{contract}.{method} cannot answer {requirement!r}: {said}",
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
            if code == ACCESS_UNKNOWN:
                raise _access_unknown(
                    self._contract,
                    self._method,
                    getattr(exc, "detail", None),
                    requirement,
                ) from exc
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

    Both reads share one trust rule (KB-33g, owner decision 2026-11-09:
    ``statuses=`` changes the accepted statuses, not the trust rules), so
    the default read and the verbatim read agree on every row, with zero
    I/O beyond the owner's declarative rule:

    * a status present that the owner vouched for decides: the default read
      by the owner's rule (e.g. a grace period), the verbatim read by the
      chosen statuses; outside them a definite deny (the declared 402);
    * the owner's own claim proven absent is the owner's definite deny only
      where its fact declares ``absent_claim="deny"`` (the literal's
      ``absent_claim``; missing means ``"unknown"``, KB-33h): the key is
      the fact's claim, assigned to the owner and verified, the owner ran
      at the mint (in ``claims_sources``, not failed) under the token's
      intact claim policy (its ``cpv`` entry is the one the verifier
      accepts for the owner), and the view removed nothing. Declared
      ``"unknown"`` (the default), the same proven absence is 501
      ``access_unknown``. An absent key never equals any status
      (``statuses={"none"}`` does not match it);
    * the claim removed by the view for a reason a refresh or a retry may
      change: its 401 ``token_policy_outdated`` / 503
      ``claims_policy_mismatch``;
    * absent (or present but unvouched) because its authority failed at the
      mint: 503 ``claim_authority_failed``, never asked live;
    * absent otherwise (no authority assigned for the key, another
      authority, the owner did not run, no provable claim policy, a legacy
      token, removed by the view as unassigned / unvouched, another
      declared ``claim_key=``), or not a string: 501 ``access_unknown``.

    Only a definite deny is a ``False``: ``Not`` allows only on a proven
    deny, and ``Not`` / ``Or`` / ``And`` never allow on an indeterminate.
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
        _CLAIM_ATOMS.add(cls)

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
        if ctx.subject.claim(self.claim_key) is None:
            # One code path for both reads (KB-33f, G-AUTH-26): the claim
            # removed by the trusted view for a reason a refresh or a retry
            # may change is its 401 / 503 first, zero I/O, never a deny that
            # Not would turn into an allow.
            _raise_if_policy_indeterminate(ctx, self.claim_key, self.describe())
        # One trust rule for both reads (KB-33g): the status the owner
        # vouched for, None when the owner's claim is proven absent.
        status = self._vouched_status(ctx)
        if status is None:
            return False
        if self._verbatim:
            return status in self._accepted_statuses()
        return await self._via_fact(ctx, status)

    def _owner_absent(self) -> Exception:
        atom = self.__sandloom_atom__
        return self._indeterminate(
            "fact_unregistered",
            f"access fact {atom['fact']!r}: its owner {atom['owner']!r} is neither "
            "loaded nor bound",
            501,
        )

    def _accepted_statuses(self) -> frozenset[str]:
        if self.statuses is not None:
            return self.statuses
        return frozenset(str(s) for s in self.__sandloom_atom__.get("statuses") or ())

    def _vouched_status(self, ctx: AuthzContext) -> str | None:
        """The claim under the owner's provenance, the one trust rule of both
        reads (KB-33g), zero I/O: the status (a string) the owner vouched
        for, ``None`` when the owner's claim is proven absent (its definite
        deny), else the row's indeterminate (see the class). The view's
        401 / 503 were raised by :meth:`evaluate`."""
        atom = self.__sandloom_atom__
        owner = str(atom["owner"])
        if not _facts.fact_owner_present(ctx, str(atom["fact"]), owner):
            # The owner is neither loaded nor bound: nobody vouches.
            raise self._owner_absent()
        subject = ctx.subject
        key = self.claim_key
        status = subject.claim(key)
        if status is None:
            if _claim_authority_failed(ctx, key):
                raise self._authority_failed()
            if key in (getattr(subject, "untrusted_claims", None) or {}):
                # The view removed the value (unassigned, another
                # authority's digest, its source absent): nobody vouches
                # for this claim, so unknown, never the owner's "no".
                raise self._claim_unknown(
                    f"claim {key!r} was removed by the trusted view: no "
                    "authority vouches for the status, so it is unknown, not "
                    "denied"
                )
            if (
                key == str(atom["claim_key"])
                and atom.get("absent_claim") == "deny"
                and _absence_proven(ctx, key, owner)
            ):
                # The owner declares that absence means "no" (KB-33h).
                return None
            raise self._claim_unknown(
                f"claim {key!r} is not in the token: no source vouches for the "
                "status, so it is unknown, not denied"
            )
        if not _value_vouched(ctx, key, owner):
            if owner in subject.claims_sources_failed:
                raise self._authority_failed()
            raise self._claim_unknown(
                f"claim {key!r} was not minted by its owner {owner!r}: no "
                "authority vouches for the status, so it is unknown, not denied"
            )
        if not isinstance(status, str):
            raise self._claim_unknown(f"claim {key!r} is not a string")
        return status

    def _authority_failed(self) -> Exception:
        return self._indeterminate(
            "claim_authority_failed",
            f"claim {self.claim_key!r} is not vouched for: its authority failed "
            "when the token was minted; try again later or sign in again",
            503,
        )

    def _claim_unknown(self, reason: str) -> Exception:
        return _indeterminate(
            ACCESS_UNKNOWN, reason, 501, self.describe(), {"claim": self.claim_key}
        )

    async def _via_fact(self, ctx: AuthzContext, status: str) -> bool:
        """The owner's declarative rule reads the vouched status (the fact
        local, or described over its facts contract once and cached); never
        asked live for ``Account.user()`` (KB-33g)."""
        atom = self.__sandloom_atom__
        try:
            fact = await _facts.lookup_access_fact(
                ctx, str(atom["fact"]), owner=str(atom["owner"])
            )
        except _facts.FactOwnerUnreachable as exc:
            raise self._indeterminate("fact_owner_unavailable", str(exc), 503) from exc
        if fact is None:
            raise self._owner_absent()
        return fact.interpret_claim(status)


def _value_vouched(ctx: AuthzContext, key: str, owner: str) -> bool:
    """Whether the present claim ``key`` is vouched for, zero I/O: a legacy
    token (no authoritative provenance) as before; else the owner ran at the
    mint, or the verifier's map assigns the key and the trusted view kept
    the value (an accepted authority vouched, a ``previous=`` one in a
    rollout included)."""
    subject = ctx.subject
    if not getattr(subject, "provenance_authoritative", False):
        return True
    if owner in subject.claims_sources and owner not in subject.claims_sources_failed:
        return True
    authorities = getattr(ctx.core(), "claim_authorities", None)
    return bool(
        authorities is not None
        and getattr(authorities, "assigned", False)
        and authorities.accepted.get(key)
    )


def _absence_proven(ctx: AuthzContext, key: str, owner: str) -> bool:
    """Whether the token proves that ``owner``, the key's assigned and
    verified authority, ran at the mint under the intact claim policy and
    wrote no ``key`` (KB-33g), zero I/O. Provenance, never the key's name:

    * the verifier's map assigns ``key`` to ``owner`` (its current
      authority, verified at boot);
    * the token's authoritative provenance lists ``owner`` in
      ``claims_sources`` and not in ``claims_sources_failed``;
    * the token's ``cpv`` entry for ``key`` is a digest the verifier accepts
      for ``owner`` (the issuer's policy is the verifier's). A token without
      a ``cpv`` (a legacy token, the bounded compatibility window) proves
      nothing. In a test composition a test-kit token is read as the view
      reads it: vouched for by the kernel test authority, or its stated
      provenance checked without a digest.

    The caller has already ruled out a value, a view removal and a failed
    authority.
    """
    subject = ctx.subject
    sources = subject.claims_sources
    failed = subject.claims_sources_failed
    cpv = subject.claim(CPV_CLAIM)
    test_token = _view.test_authority_enabled() and cpv == _view.TEST_CPV
    if (
        test_token
        and _view.TEST_AUTHORITY in sources
        and _view.TEST_AUTHORITY not in failed
    ):
        return True
    if not getattr(subject, "provenance_authoritative", False):
        return False
    if owner not in sources or owner in failed:
        return False
    authorities = getattr(ctx.core(), "claim_authorities", None)
    if authorities is None or not getattr(authorities, "assigned", False):
        return False
    accepted = authorities.accepted.get(key) or ()
    if not accepted or accepted[0][0] != owner:
        return False
    if test_token:
        return True
    if not isinstance(cpv, Mapping) or cpv.get("v") != CPV_VERSION:
        return False
    entries = cpv.get("k")
    if not isinstance(entries, Mapping):
        return False
    digest = entries.get(key)
    return any(name == owner and digest == want for name, want in accepted)


__all__ = [
    "ACCESS_CHECK_ATTR",
    "ACCESS_UNKNOWN",
    "ATOM_ATTR",
    "CONTRACT_MISMATCH",
    "AccessUnknown",
    "OWNER_UNAVAILABLE",
    "ClaimFactCheck",
    "ContractCheck",
    "From",
    "access_check",
    "access_check_of",
    "generated_access_contracts",
]
