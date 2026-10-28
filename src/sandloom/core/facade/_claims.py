"""Token-claims collection: contributors, the per-owner read units and the
``<owner>.claims.v1`` contract.

:meth:`CoreFacade.collect_claims` gathers every registered contributor's claims
at mint time — local owners in their own module's read unit (owners sharing a
database run sequentially in one lane), remote owners over their
``<owner>.claims.v1`` contract — and stamps ``claims_sources`` /
``claims_sources_failed``. A critical owner's failure aborts minting
(:class:`ClaimsUnavailable`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, status

from sandloom.core.facade._services import (
    CLAIMS_CONTRACT_SUFFIX,
    _ServicesMixin,
    _short_module_name,
    claims_contract_name,
)
from sandloom.core.module_api import contract_method

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade
import sandloom.core.module_api as _module_api


class ClaimsUnavailable(HTTPException):
    """A CRITICAL claims contributor failed: the token must not be minted.

    Surfaces as ``503`` (retry later) — never a token silently missing
    security claims such as tenancy's ``tenant_ids`` allowlist.
    """

    def __init__(self, owner: str) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Token claims from {owner!r} are unavailable; try again later",
        )
        self.owner = owner


#: SQLSTATE ``in_failed_sql_transaction``.
_FAILED_TRANSACTION_SQLSTATE = "25P02"


def _in_failed_transaction(exc: BaseException) -> bool:
    """True when ``exc`` (or what it wraps) is "current transaction is aborted".

    Duck-typed (``sqlstate`` / ``pgcode`` on the driver error, reached through
    ``orig`` / ``__cause__`` / ``__context__``): the kernel imports no driver.
    """
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode"):
            if getattr(current, attr, None) == _FAILED_TRANSACTION_SQLSTATE:
                return True
        stack.extend(
            (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
        )
    return False


#: One planned claims owner: ``(owner, critical, call, database)`` — the
#: database names the read unit a local owner shares; ``None`` runs alone.
_ClaimsStep = tuple[str | None, bool, Callable[[], Awaitable[Any]], str | None]
#: ``(True, claims)`` or ``(False, exception)``.
_ClaimsOutcome = tuple[bool, Any]


async def _attempt_claims(
    owner: str | None, call: Callable[[], Awaitable[Any]]
) -> _ClaimsOutcome:
    """``(True, claims)`` or ``(False, exception)`` — never raises."""
    try:
        contributed = await call()
        if contributed is not None and not isinstance(contributed, dict):
            raise TypeError(
                f"claims contributor {owner!r} returned "
                f"{type(contributed).__name__}, expected dict"
            )
    except Exception as exc:  # noqa: BLE001 - classified by the caller
        return False, exc
    return True, contributed


def _claims_lanes(plan: list[_ClaimsStep]) -> list[list[int]]:
    """Plan indices grouped into lanes: one per database, one per other step."""
    lanes: list[list[int]] = []
    by_database: dict[str, list[int]] = {}
    for index, (_owner, _critical, _call, database) in enumerate(plan):
        if database is None:
            lanes.append([index])
        elif database in by_database:
            by_database[database].append(index)
        else:
            by_database[database] = lane = [index]
            lanes.append(lane)
    return lanes


async def _run_claims_lane(
    plan: list[_ClaimsStep], lane: list[int], outcomes: dict[int, _ClaimsOutcome]
) -> None:
    for position, index in enumerate(lane):
        owner, _critical, call, _db = plan[index]
        outcome = await _attempt_claims(owner, call)
        if position and not outcome[0] and _in_failed_transaction(outcome[1]):
            # An earlier owner of the lane swallowed an SQL error
            # and left the shared transaction aborted; the failed
            # call dropped that unit, so the retry runs on a fresh
            # one — the outcome of running in its own UoW.
            outcome = await _attempt_claims(owner, call)
        outcomes[index] = outcome


async def _run_claims_plan(plan: list[_ClaimsStep]) -> dict[int, _ClaimsOutcome]:
    """Run every step; outcomes by plan index (never raises for a step).

    Local owners on the SAME database run back to back on one shared read
    unit (the contract read scope: one connection, one BEGIN … ROLLBACK,
    instead of one per owner); the issuer, each database group and each
    remote owner run CONCURRENTLY. Every owner keeps its own call and outcome;
    the caller applies them in registration order (same merge order, same
    "first critical failure aborts" result as running them in turn).
    """

    outcomes: dict[int, _ClaimsOutcome] = {}
    # A test-bound contract session (bind_contract_session) is ONE session
    # shared by every local contributor: keep them all sequential then.
    if _module_api.contract_session_bound():
        for index, (owner, _critical, call, _db) in enumerate(plan):
            outcomes[index] = await _attempt_claims(owner, call)
        return outcomes
    lanes = _claims_lanes(plan)
    shares = any(len(lane) > 1 for lane in lanes)
    scope = (
        _module_api.contract_read_scope()
        if shares and _module_api.current_contract_read_scope() is None
        else nullcontext()
    )
    async with scope:
        if len(lanes) == 1:
            await _run_claims_lane(plan, lanes[0], outcomes)
        elif lanes:
            # The issuer's lane runs on the issuer's OWN unit (its session):
            # in this task, which owns it (spec §4.3) — the other lanes run
            # concurrently in their own units / over RPC meanwhile.
            mine = [lane for lane in lanes if _is_issuer_lane(plan, lane)]
            others = [lane for lane in lanes if lane not in mine]
            pending = asyncio.gather(
                *(_run_claims_lane(plan, lane, outcomes) for lane in others)
            )
            try:
                for lane in mine:
                    await _run_claims_lane(plan, lane, outcomes)
            finally:
                await pending
    return outcomes


#: Attribute of the issuer step's call (runs on the issuer's own session).
_ISSUER_ATTR = "_sandloom_claims_issuer"


def _is_issuer_lane(plan: list[_ClaimsStep], lane: list[int]) -> bool:
    return any(getattr(plan[i][2], _ISSUER_ATTR, False) for i in lane)


class ClaimsContributorContract:
    """``<module>.claims.v1`` — a module's token-claims contributors as a contract.

    Auto-registered for every module that adds a claims contributor; runs the
    module's contributors (in its own read-only Unit of Work, like any read
    contract) and returns their merged claims. A token issuer in another
    process reaches it over internal RPC via ``SERVICE_BINDINGS``.
    """

    def __init__(self, facade: CoreFacade, owner: str | None) -> None:
        self._facade = facade
        self._owner = owner

    @contract_method
    async def contribute(self, user_id: Any) -> dict[str, Any]:
        """Merged claims of this module's contributors for ``user_id``."""
        return await self._merge(user_id, None)

    async def _merge(
        self,
        user_id: Any,
        precomputed: Mapping[Any, dict[str, Any] | None] | None,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for fn in self._facade._contributors_of(self._owner):
            if precomputed is not None and fn in precomputed:
                contributed = precomputed[fn]
            else:
                contributed = await fn(user_id)
            if contributed:
                merged.update(contributed)
        return merged


class _ClaimsMixin(_ServicesMixin):
    """CoreFacade methods: see :mod:`sandloom.core.facade._claims`."""

    # -----------------
    # Token claims contributors
    # -----------------
    def add_claims_contributor(
        self, fn: Any, *, critical: bool = False, owner: str | None = None
    ) -> None:
        """Register a contributor that adds claims to freshly minted tokens.

        ``fn`` is ``async (user_id) -> dict`` of JSON-serializable claim fields;
        it reads its data through :func:`contract_session`. It runs through
        the contract runtime — in
        the OWNING module's Unit of Work on that module's database — and is
        exposed as the ``<owner>.claims.v1`` contract (``contribute(user_id)``)
        so a token issuer deployed in another process collects it over RPC.
        ``critical=True`` means a failure must ABORT minting (503) rather than
        silently issue a token missing those claims — use it for security
        claims like roles/permissions/tenant allowlists, where a fail-open
        reduced-claims token is worse than a failed login.

        ``owner`` (the module's short name, e.g. ``"billing"``) is stamped into
        ``claims_sources``. When omitted it is taken from the module whose
        registration is in progress (last dotted segment of its name — works for
        ``sandloom_billing`` and ``myapp.modules.billing`` alike), else from the
        contributor's ``sandloom.<module>`` package.
        """
        resolved = owner or _short_module_name(self._registering_module)
        if resolved is None:
            module = getattr(fn, "__module__", "") or ""
            parts = module.split(".")
            if len(parts) >= 2 and parts[0] == "sandloom":
                resolved = parts[1]
        self._claims_contributors.append((fn, critical, resolved))
        if resolved is None:
            return
        if self._registering_module is not None:
            self._claims_owner_modules.setdefault(resolved, self._registering_module)
        contract_name = claims_contract_name(resolved)
        if self.service_registry.contains(contract_name):
            return
        if not self._exposable(contract_name, "claims contributor owner", resolved):
            return
        self.register_service(
            contract_name, ClaimsContributorContract(cast("CoreFacade", self), resolved)
        )

    @property
    def claims_contributors(self) -> list[Any]:
        return [fn for fn, _, _ in self._claims_contributors]

    def _contributors_of(self, owner: str | None) -> list[Any]:
        return [fn for fn, _, o in self._claims_contributors if o == owner]

    def _local_claim_owners(self) -> dict[str | None, bool]:
        """Local contributor owners (registration order) -> is any critical."""
        owners: dict[str | None, bool] = {}
        for _fn, critical, owner in self._claims_contributors:
            owners[owner] = owners.get(owner, False) or critical
        return owners

    def remote_claims_contributors(self) -> dict[str, bool]:
        """Remote claims contributors declared via ``SERVICE_BINDINGS``.

        Every binding named ``<module>.claims.v1`` whose module contributes no
        claims locally is a remote contributor (owner -> critical). A binding is
        critical unless it sets ``"critical": false`` — fail closed: a token is
        never silently minted without, e.g., the tenancy allowlist.
        """
        local = self._local_claim_owners()
        remote: dict[str, bool] = {}
        for name, binding in self.settings.service_bindings.items():
            if not name.endswith(CLAIMS_CONTRACT_SUFFIX):
                continue
            owner = name[: -len(CLAIMS_CONTRACT_SUFFIX)]
            if not owner or owner in local:
                continue
            remote[owner] = binding.get("critical", True) is not False
        return remote

    async def collect_claims(
        self,
        user_id: Any,
        issuer_session: Any = None,
        *,
        issuer: str | None = None,
        precomputed: Mapping[Any, dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Merge every contributor's claims for ``user_id`` (mint cold path).

        Local contributors run through the contract runtime in their owning
        module's own Unit of Work; remote ones (``<module>.claims.v1`` bindings
        in ``SERVICE_BINDINGS``) are called over internal RPC — so a token
        minted by an identity service deployed alone still carries tenancy's
        ``tenant_ids`` and billing's subscription claims.

        ``issuer`` names the module minting the token: ITS contributors run in
        ``issuer_session`` (the issuer's own, possibly uncommitted, request
        transaction — e.g. roles assigned during registration); a session
        without ``issuer`` is a ``ValueError``.

        ``precomputed`` maps one of the ISSUER's registered contributor
        functions to the claims it would return for ``user_id`` in
        ``issuer_session`` — read by the issuer in a statement it ran anyway
        (identity folds its roles / permissions into the refresh CAS). That
        contributor is then not called; the owner is still run, recorded and
        stamped in ``claims_sources`` exactly as if it had been, and any of the
        issuer's contributors NOT in the mapping still runs. It applies only to
        the issuer's own contributors (never another owner's, never remote)
        and only with ``issuer`` + ``issuer_session``.

        A non-critical contributor that fails is logged and recorded in
        ``claims_sources_failed`` (so claim-mode facts escalate to
        projection/live). A *critical* one aborts minting with
        :class:`ClaimsUnavailable` (503): a token silently missing security
        claims would strip privileges for its whole lifetime.
        """
        if issuer_session is not None and issuer is None:
            raise ValueError(
                "collect_claims(issuer_session=...) needs issuer=<module>: only "
                "the issuer's own contributors run in its session; every other "
                "one runs in its own module's Unit of Work"
            )
        plan = self._claims_plan(user_id, issuer, issuer_session, precomputed)
        outcomes = await _run_claims_plan(plan)
        return self._merge_claim_outcomes(plan, outcomes)

    def _claims_plan(
        self,
        user_id: Any,
        issuer: str | None,
        issuer_session: Any,
        precomputed: Mapping[Any, dict[str, Any] | None] | None,
    ) -> list[_ClaimsStep]:
        """One step per owner: local owners in registration order, then remote.

        Issuer contributors share the issuer's own session. Every other owner
        runs in its own read Unit of Work (or over RPC); a local owner's step
        names its database so owners on the SAME database share one read unit
        (see :func:`_run_claims_plan`).
        """
        plan: list[_ClaimsStep] = []
        for owner, critical in self._local_claim_owners().items():
            if owner is not None and owner == issuer and issuer_session is not None:
                plan.append(
                    (
                        owner,
                        critical,
                        self._issuer_claims_call(
                            owner, user_id, issuer_session, precomputed
                        ),
                        None,
                    )
                )
                continue
            plan.append(
                (
                    owner,
                    critical,
                    self._local_claims_call(owner, user_id),
                    self._claims_owner_database(owner),
                )
            )
        for owner, critical in self.remote_claims_contributors().items():
            plan.append(
                (owner, critical, self._remote_claims_call(owner, user_id), None)
            )
        return plan

    def _issuer_claims_call(
        self,
        owner: str | None,
        user_id: Any,
        issuer_session: Any,
        precomputed: Mapping[Any, dict[str, Any] | None] | None,
    ) -> Callable[[], Awaitable[Any]]:
        async def _issuer_call() -> Any:
            contract = ClaimsContributorContract(cast("CoreFacade", self), owner)
            with _module_api.bind_contract_session(issuer_session):
                if precomputed:
                    return await contract._merge(user_id, precomputed)
                return await contract.contribute(user_id)

        setattr(_issuer_call, _ISSUER_ATTR, True)
        return _issuer_call

    def _local_claims_call(
        self, owner: str | None, user_id: Any
    ) -> Callable[[], Awaitable[Any]]:
        async def _local_call() -> Any:
            contract = (
                self.local_contract(claims_contract_name(owner))
                if owner is not None
                else None
            )
            if contract is None:
                contract = _module_api.bind_contract(
                    ClaimsContributorContract(cast("CoreFacade", self), owner),
                    core=self,
                    database=self._claims_owner_database(owner),
                    name=claims_contract_name(owner) if owner is not None else "claims",
                )
            return await contract.contribute(user_id)

        return _local_call

    def _remote_claims_call(
        self, owner: str, user_id: Any
    ) -> Callable[[], Awaitable[Any]]:
        async def _remote_call() -> Any:
            return await self.get_service(claims_contract_name(owner)).contribute(
                user_id
            )

        return _remote_call

    def _merge_claim_outcomes(
        self, plan: list[_ClaimsStep], outcomes: dict[int, _ClaimsOutcome]
    ) -> dict[str, Any]:
        """Apply outcomes in plan order; the first critical failure aborts.

        A non-critical failure is logged and recorded in
        ``claims_sources_failed``.
        """
        claims: dict[str, Any] = {}
        sources: set[str] = set()
        failed: set[str] = set()
        for index, (owner, critical, _call, _db) in enumerate(plan):
            ok, value = outcomes[index]
            if not ok:
                if critical:
                    raise ClaimsUnavailable(owner or "?") from value
                self.logger.warning(
                    "claims contributor %r failed; skipping",
                    owner,
                    exc_info=(type(value), value, value.__traceback__),
                )
                # Provenance must still say the owner's claims are ABSENT so a
                # claim-mode fact escalates to live/projection instead of
                # reading a default as "genuinely false".
                if owner:
                    failed.add(owner)
                continue
            if value:
                claims.update(value)
            # Provenance: the owner RAN successfully (even if it contributed
            # nothing — an empty result is an authoritative "no").
            if owner:
                sources.add(owner)

        # Always stamped (JSON lists for the JWT; UserContext coerces to sets).
        # ``claims_sources_failed`` doubles as the marker that provenance is
        # authoritative: with it present an EMPTY ``claims_sources`` means "no
        # module contributed", not "legacy token, trust everything".
        claims["claims_sources"] = sorted(sources - failed)
        claims["claims_sources_failed"] = sorted(failed)
        return claims
