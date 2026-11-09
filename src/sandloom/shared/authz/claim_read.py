"""Claim-tier atoms that read an entry of a mapping claim (ADR-0014 decision
15; KB-33).

A provider declares the claim-tier atom of one of its access checks with
:func:`claim_atom`, placed above :func:`~sandloom.shared.authz.contract_check.\
access_check`: for ``Account.user()`` the atom reads the token claim
``claim_key`` (a mapping) at the entry its parameter ``entry`` names, with zero
I/O; for an organization account it asks the access check below it, live,
with the same parameters as contract arguments (in-process or over internal
RPC alike). ``sandloom generate`` writes the typed atom beside the consumer's
stub (a :class:`ClaimReadCheck` subclass carrying the declaration as a
literal), as it writes the other access atoms.

Two reads, the kernel's claim atoms' own:

* ``flag`` (no ``below``): the entry is ``True``;
* ``limit`` (``below=<parameter>``): the parameter is strictly below the
  entry, an integer (an absent or non-integer entry is ``0``: denied).

From the claims (the semantics of the claim atoms, ADR-0014 decisions 3 and
7): the view's indeterminate for the key (``token_policy_outdated`` 401,
``claims_policy_mismatch`` 503) first; an absent claim whose authority failed
at the mint is indeterminate 503 ``claim_authority_failed``; an authority that
ran and wrote no value for the entry denies. Never an allow on missing data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from sandloom.shared.authz.account import (
    _MISSING,
    Account,
    account_org_params,
    require_account,
    resolve_account,
)
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE, Requirement
from sandloom.shared.authz.atoms_claims import (
    _claim_authority_failed,
    _raise_if_policy_indeterminate,
)
from sandloom.shared.authz.contract_check import (
    ATOM_ATTR,
    ContractCheck,
    _indeterminate,
    access_check_of,
)
from sandloom.shared.authz.results import Category

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

#: The reads a claim atom may declare.
READ_FLAG = "flag"
READ_LIMIT = "limit"


def claim_atom(
    name: str,
    *,
    claim_key: str,
    entry: str,
    below: str | None = None,
    claim_keys: Iterable[str] = (),
) -> Callable[[Any], Any]:
    """Declare ``name``, the claim-tier atom of the access check below it.

    ``claim_key`` is the token claim (a mapping) read for ``Account.user()``
    with zero I/O; ``entry`` the check's parameter whose value names the
    mapping's entry. Without ``below`` the entry must be ``True``; with
    ``below="used"`` the parameter ``used`` must be strictly below the entry
    (an integer). ``claim_keys`` lists further claims a caller may read
    instead (``claim_key=`` of the generated atom). For an organization
    account the atom asks the access check, live, with its parameters. The
    atom's category and label are the access check's.

    Placed ABOVE ``@access_check`` (it completes that declaration)::

        @claim_atom("Feature", claim_key="features", entry="key")
        @access_check("LiveFeature", bind=ACCOUNT, describe="feature '{key}'")
        async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str):
            ...

    where ``ACCOUNT`` is ``{"tenant_type": From.account_type(), "tenant_id":
    From.account_id()}``.
    """
    if not isinstance(name, str) or not name.isidentifier() or not name[:1].isupper():
        raise ValueError(f"claim_atom name must be a class name, not {name!r}")
    keys = [claim_key, *(k for k in claim_keys if k != claim_key)]
    if not all(isinstance(k, str) and k for k in keys):
        raise ValueError(f"claim_atom {name}: claim keys are non-empty strings")
    for label, param in (("entry", entry), ("below", below)):
        if param is not None and (not isinstance(param, str) or not param):
            raise ValueError(f"claim_atom {name}: {label} names a parameter")

    def decorate(fn: Any) -> Any:
        spec = _access_spec(fn, name)
        claim: dict[str, Any] = {
            "name": name,
            "read": READ_FLAG if below is None else READ_LIMIT,
            "claim_key": keys[0],
            "claim_keys": keys,
            "entry": entry,
            "category": spec.get("category", Category.FORBIDDEN),
        }
        if below is not None:
            claim["below"] = below
        if spec.get("describe"):
            claim["describe"] = spec["describe"]
        spec["claim"] = claim
        return fn

    return decorate


def _access_spec(fn: Any, name: str) -> dict[str, Any]:
    """The ``@access_check`` declaration below ``claim_atom(name)``: one that
    binds the account (``of=``) and names no claim atom yet."""
    spec = access_check_of(fn)
    if spec is None:
        raise ValueError(
            f"claim_atom {name}: place it above @access_check (it declares "
            "the claim-tier atom of that access check)"
        )
    kinds = {source[0] for source in (spec.get("bind") or {}).values()}
    if not {"account_type", "account_id"} <= kinds:
        raise ValueError(
            f"claim_atom {name}: the access check {spec['name']!r} must bind "
            "From.account_type() and From.account_id() (the account of=)"
        )
    if spec.get("claim") is not None:
        raise ValueError(
            f"claim_atom {name}: the access check {spec['name']!r} already "
            "names a claim atom"
        )
    return spec


class ClaimReadCheck(Requirement):
    """The generic evaluator of a generated claim atom over a mapping claim.

    A generated subclass sets ``__sandloom_atom__`` (``read``, ``claim_key``,
    ``claim_keys``, ``entry``, ``below``, ``category``, ``describe`` and
    ``otherwise``, the access check's own literal) and a typed ``__init__``
    calling ``super().__init__(params, of=..., claim_key=...)``.
    ``Account.user()`` (or a bound personal tenant) reads the claim with zero
    I/O; an organization account asks the live check with the same
    parameters.
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
            # The provider's live check, from the same literal; registered as
            # a generated atom (access-bearing) by ContractCheck.
            cls._otherwise = type(
                f"{cls.__name__}Live",
                (ContractCheck,),
                {ATOM_ATTR: otherwise, "__module__": cls.__module__},
            )

    def __init__(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        of: Account = _MISSING,
        claim_key: str | None = None,
    ) -> None:
        atom = self.__sandloom_atom__
        if not atom or self._otherwise is None:
            raise TypeError(
                "ClaimReadCheck is the evaluator of a generated atom: import the "
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
        self.params = dict(params or {})
        self._read = str(atom.get("read") or READ_FLAG)
        self._entry = str(atom.get("entry"))
        self._below = atom.get("below")
        # The live check, built once (it validates the parameters' values).
        self._live = self._otherwise(self.params, of=self.account)
        template = atom.get("describe")
        self._label = (
            template.format(**self.params)
            if isinstance(template, str)
            else type(self).__name__
        )
        if self.account.kind != "user":
            self.tier = TIER_LIVE

    # -- wiring-time metadata -------------------------------------------------
    def org_params(self) -> frozenset[str]:
        return account_org_params(self.account)

    def contracts(self) -> frozenset[str]:
        if self.account.kind == "user":
            return frozenset()  # the claim only: zero I/O
        return self._live.contracts()

    def risks(self) -> frozenset[str]:
        return self._live.risks()

    def needs_request_session(self) -> bool:
        # The claim is read from the token; the live check runs in the
        # provider's own unit: require() opens no session for it (K6).
        return self._live.needs_request_session()

    def describe(self) -> str:
        return self._label

    # -- evaluation -------------------------------------------------------------
    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.account.kind != "user":
            tenant_type, _tenant = await resolve_account(
                ctx, self.account, requirement=self.describe()
            )
            if tenant_type != "user":
                return await self._live.evaluate(ctx)
        return self._from_claims(ctx)

    def _claim(self, ctx: AuthzContext) -> Any:
        """The token's claim; indeterminate when it is absent for a reason a
        refresh (401) or a retry (503) may change, or because its authority
        failed at the mint (503)."""
        value = ctx.subject.claim(self.claim_key)
        if value is None:
            _raise_if_policy_indeterminate(ctx, self.claim_key, self.describe())
            if _claim_authority_failed(ctx, self.claim_key):
                raise _indeterminate(
                    "claim_authority_failed",
                    f"claim {self.claim_key!r} is not in the token: its authority "
                    "failed when the token was minted; try again later or sign "
                    "in again",
                    503,
                    self.describe(),
                )
        return value

    def _from_claims(self, ctx: AuthzContext) -> bool:
        mapping = self._claim(ctx)
        key = self.params.get(self._entry)
        raw = mapping.get(key) if isinstance(mapping, dict) else None
        if self._read == READ_FLAG:
            return raw is True
        if isinstance(raw, bool) or not isinstance(raw, int):
            return False  # an absent or non-integer limit is 0: denied
        return bool(self.params[str(self._below)] < raw)


__all__ = ["READ_FLAG", "READ_LIMIT", "ClaimReadCheck", "claim_atom"]
