"""Token-claims collection: contributors, the per-owner read units and the
``<owner>.claims.v1`` contract.

:meth:`CoreFacade.collect_claims` gathers every registered contributor's claims
at mint time — local owners in their own module's read unit (owners sharing a
database run sequentially in one lane), remote owners over their
``<owner>.claims.v1`` contract — and stamps ``claims_sources`` /
``claims_sources_failed``. A critical owner's failure aborts minting
(:class:`ClaimsUnavailable`).

The mint check runs in the same merge pass (ADR-0014 decision 2): a protected
claim only from the authority the composition assigned it
(:class:`~sandloom.core.claims.ClaimAuthorities`), the reserved keys from
nobody, any other key from one owner per token. A step that breaks it is a
violator: an optional one loses its whole result, a critical one refuses the
mint naming itself. The issuer's own fields (``email``, ``email_verified``)
pass the same check as a last, critical step of the issuer
(``collect_claims(issuer_claims=...)``): never written after it, unchecked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, status

from sandloom.core.claims import (
    AUTHORITY_CLAIM,
    CPV_CLAIM,
    CPV_VERSION,
    NO_AUTHORITIES,
    ClaimAuthorities,
    SealedClaims,
    UnsealedClaimsError,
    _seal_claims,
)
from sandloom.core.facade._services import (
    CLAIMS_CONTRACT_SUFFIX,
    _ServicesMixin,
    _short_module_name,
    claims_contract_name,
)
from sandloom.core.module_api import contract_method
from sandloom.core.sql_alchemy.portable import is_error_kind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade
import sandloom.core.module_api as _module_api

#: Optional contributions dropped by the violator rule (ADR-0014 decision 2),
#: exported as ``sandloom_facade_counter_total{name=...}``.
CLAIMS_VIOLATIONS_METRIC = "claims_contributor_violations_total"


class ClaimsUnavailable(HTTPException):
    """A CRITICAL claims contributor failed, or wrote a claim it may not
    write: the token must not be minted.

    Surfaces as ``503`` (retry later) naming that contributor — never a token
    silently missing security claims such as tenancy's ``tenant_ids``
    allowlist, nor one carrying another module's.
    """

    def __init__(self, owner: str) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Token claims from {owner!r} are unavailable; try again later",
        )
        self.owner = owner


def _in_failed_transaction(exc: BaseException) -> bool:
    """True when ``exc`` (or what it wraps) is "current transaction is
    aborted": the renderer's ``transaction_aborted`` kind (PostgreSQL's
    SQLSTATE 25P02). The kernel imports no driver."""
    return is_error_kind(exc, "transaction_aborted")


#: Keys no claims contributor may write: the provenance the merge stamps and
#: the token envelope the minting code and ``create_access_token`` stamp (the
#: latter only ``setdefault``\ s ``iat`` / ``nbf`` / ``jti`` / ``iss`` / ``aud``
#: and keeps a supplied ``iat_us``, so a contributed one would survive: a
#: token past every revocation mark, or past step-up via ``auth_time``).
RESERVED_CLAIM_KEYS: frozenset[str] = frozenset(
    {
        "claims_sources",
        "claims_sources_failed",
        # UserContext's structural fields: ``user_id`` wins over ``sub``,
        # ``provenance_authoritative`` decides whether claim-mode atoms trust
        # absent provenance, and ``claims`` is merged into every owner's
        # namespaced claims (shared/subject.py _FIELD_KEYS).
        "user_id",
        "provenance_authoritative",
        "claims",
        # The tenant the request binds: TenantScope and the authz tenant
        # resolver read these BEFORE the ``tenant_ids`` grant
        # (core/http/tenant_scope.py, shared/authz/_resolve.py), so a
        # contributed value would bind a foreign tenant. Only trusted minting
        # code (a service or test token) may set them.
        "tenant_id",
        "org_id",
        "sub",
        "iss",
        "aud",
        "exp",
        "nbf",
        "iat",
        "jti",
        "iat_us",
        "auth_time",
        "token_type",
        # The claim policy version the kernel stamps (ADR-0014 decision 4):
        # never a contributor's.
        "cpv",
        # The verifier's trusted-view verdict (core.auth.claims_view).
        "sandloom.untrusted",
        # The authority binding of a ``<m>.claims.v1`` answer (the kernel's).
        AUTHORITY_CLAIM,
    }
)


class ClaimsAuthorityMismatch(RuntimeError):
    """A remote ``<m>.claims.v1`` answer served by another module than the
    one the map assigns (or naming none): the step fails, so a critical
    authority refuses the mint (ADR-0014 decision 4, the authority binding).
    """


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


def _local_answer(answer: Any) -> Any:
    """A local ``contribute`` answer without its authority binding: a local
    authority is bound by its registration (the mint check). A value a
    contributor wrote stays, for the mint check to refuse."""
    if isinstance(answer, dict) and isinstance(answer.get(AUTHORITY_CLAIM), str):
        del answer[AUTHORITY_CLAIM]
    return answer


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
        """Merged claims of this module's contributors for ``user_id``.

        The answer names, under :data:`~sandloom.core.claims.AUTHORITY_CLAIM`,
        the import path of the module registered under this owner: a remote
        issuer binds it to its map (the pinned ``service_id`` proves the
        service, this proves which module the service runs under the name).
        """
        merged = await self._merge(user_id, None)
        path = self._facade._claims_owner_modules.get(self._owner or "")
        if AUTHORITY_CLAIM in merged:
            # A contributor wrote the kernel's key: it stays a reserved-key
            # violation locally and fails the binding remotely, never the
            # kernel's answer.
            merged[AUTHORITY_CLAIM] = {"written_by": "a contributor"}
        elif path:
            merged[AUTHORITY_CLAIM] = path
        return merged

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

    #: The composition's claim authorities (``create_app`` assigns them).
    _claim_authorities: ClaimAuthorities = NO_AUTHORITIES
    _claim_authorities_assigned: bool = False

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

        The owner stamped into ``claims_sources`` is the short name of the
        module whose registration is in progress (``sandloom_billing`` and
        ``myapp.modules.billing`` alike give ``"billing"``). ``owner``, when
        given, must equal it; another module's name, or a contributor
        registered outside a module's registration, is refused
        (SANDLOOM-B034).
        """
        module = self._registering_module
        resolved = self._require_own_name(
            owner or _short_module_name(module) or "?", "a claims contributor"
        )
        self._claims_contributors.append((fn, critical, resolved))
        self._claims_owner_modules.setdefault(resolved, str(module))
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
        issuer: Callable[..., Any] | None = None,
        precomputed: Mapping[Any, dict[str, Any] | None] | None = None,
        issuer_claims: Mapping[str, Any] | None = None,
    ) -> SealedClaims:
        """Merge every contributor's claims for ``user_id`` (mint cold path).

        Returns the sealed result (:class:`~sandloom.core.claims.SealedClaims`,
        read-only): pass it whole to ``create_access_token(data,
        claims=sealed)``, the only way its protected claims, provenance and
        ``cpv`` reach a token (ADR-0014 decision 5).

        Local contributors run through the contract runtime in their owning
        module's own Unit of Work; remote ones (``<module>.claims.v1`` bindings
        in ``SERVICE_BINDINGS``) are called over internal RPC — so a token
        minted by an identity service deployed alone still carries tenancy's
        ``tenant_ids`` and billing's subscription claims.

        ``issuer`` identifies the module minting the token by one of its own
        registered claims contributors (the function it passed to
        ``add_claims_contributor``): the issuer is the module that registered
        it, never a caller-supplied name (a string, or a function no module
        registered, is refused: SANDLOOM-R017). ITS contributors run in
        ``issuer_session`` (the issuer's own, possibly uncommitted, request
        transaction — e.g. roles assigned during registration); a session
        without ``issuer`` is a ``ValueError``.

        ``precomputed`` maps one of the ISSUER's registered contributor
        functions to the claims it would return for ``user_id`` in
        ``issuer_session`` — read by the issuer in a statement it ran anyway
        (identity folds its roles / permissions into the refresh CAS). That
        contributor is then not called; the owner is still run, recorded and
        stamped in ``claims_sources`` exactly as if it had been, and any of the
        issuer's contributors NOT in the mapping still runs. It is accepted
        only with ``issuer`` + ``issuer_session``, only for the issuer's own
        contributors and only with keys the composition assigns to the
        issuer (SANDLOOM-R017 otherwise): a caller never supplies another
        module's claims, or a key its issuer is not the authority of.

        A non-critical contributor that fails is logged and recorded in
        ``claims_sources_failed`` (so claim-mode facts escalate to
        projection/live). A *critical* one aborts minting with
        :class:`ClaimsUnavailable` (503): a token silently missing security
        claims would strip privileges for its whole lifetime.

        A protected claim is kept only from its assigned authority
        (:attr:`claim_authorities`); a contributor writing one it is not
        assigned, or a reserved key, is a violator (see
        :meth:`_merge_claim_outcomes`).

        ``issuer_claims``: the claims the issuer writes itself from the row
        it mints for (``email``, ``email_verified``), merged as a last,
        critical step of ``issuer`` through the same check: a key the
        composition does not assign to the issuer refuses the mint
        (:class:`ClaimsUnavailable`), so an issuer field is never minted
        unchecked (no map: nothing is assigned). No call is added.
        """
        if issuer_session is not None and issuer is None:
            raise ValueError(
                "collect_claims(issuer_session=...) needs issuer=<contributor>: "
                "only the issuer's own contributors run in its session; every "
                "other one runs in its own module's Unit of Work"
            )
        if issuer_claims and issuer is None:
            raise ValueError(
                "collect_claims(issuer_claims=...) needs issuer=<contributor>: "
                "they are checked as the issuer's own claims"
            )
        owner = self._resolve_issuer(issuer)
        if precomputed:
            self._check_precomputed(owner, issuer_session, precomputed)
        plan = self._claims_plan(user_id, owner, issuer_session, precomputed)
        outcomes = await _run_claims_plan(plan)
        return _seal_claims(
            self._merge_claim_outcomes(
                plan, outcomes, issuer=owner, issuer_claims=issuer_claims
            )
        )

    def _resolve_issuer(self, issuer: Any) -> str | None:
        """The short name of the module that registered ``issuer`` as a
        claims contributor (``None`` for ``None``): the issuer is resolved
        from its registration, never taken from the caller (ADR-0014
        decision 5, adversarial C-a2)."""
        if issuer is None:
            return None
        if isinstance(issuer, str):
            raise UnsealedClaimsError(
                f"SANDLOOM-R017: collect_claims(issuer={issuer!r}) takes the "
                "issuer's own claims contributor (the function it passed to "
                "add_claims_contributor), not a name: the issuer is the module "
                "that registered it"
            )
        owners = {o for fn, _critical, o in self._claims_contributors if fn == issuer}
        if len(owners) != 1 or None in owners:
            raise UnsealedClaimsError(
                f"SANDLOOM-R017: collect_claims(issuer={issuer!r}) is not a "
                "claims contributor a module registered: the issuer is "
                "identified by one of its own registered contributors"
            )
        return owners.pop()

    def _check_precomputed(
        self,
        owner: str | None,
        issuer_session: Any,
        precomputed: Mapping[Any, dict[str, Any] | None],
    ) -> None:
        """Refuse a ``precomputed`` contribution that is not the verified
        issuer's own: without ``issuer`` + ``issuer_session``, for a function
        the issuer did not register, or with a key the composition does not
        assign to the issuer (SANDLOOM-R017)."""
        if owner is None or issuer_session is None:
            raise UnsealedClaimsError(
                "SANDLOOM-R017: collect_claims(precomputed=...) is accepted "
                "only with issuer=<contributor> and issuer_session"
            )
        mine = self._contributors_of(owner)
        authorities = self._claim_authorities
        for fn, contributed in precomputed.items():
            if fn not in mine:
                raise UnsealedClaimsError(
                    f"SANDLOOM-R017: collect_claims(precomputed=...) names "
                    f"{fn!r}, which is not a claims contributor of the issuer "
                    f"{owner!r}"
                )
            foreign = sorted(
                key
                for key in (contributed or {})
                if authorities.owners.get(key) != owner
            )
            if foreign:
                raise UnsealedClaimsError(
                    f"SANDLOOM-R017: collect_claims(precomputed=...) carries "
                    f"{', '.join(foreign)}, which claim_authorities does not "
                    f"assign to the issuer {owner!r}"
                )

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
                return _local_answer(await contract.contribute(user_id))

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
            return _local_answer(await contract.contribute(user_id))

        return _local_call

    def _remote_claims_call(
        self, owner: str, user_id: Any
    ) -> Callable[[], Awaitable[Any]]:
        async def _remote_call() -> Any:
            answer = await self.get_service(claims_contract_name(owner)).contribute(
                user_id
            )
            if isinstance(answer, dict):
                self._bind_remote_authority(owner, answer.pop(AUTHORITY_CLAIM, None))
            return answer

        return _remote_call

    def _bind_remote_authority(self, owner: str, reported: Any) -> None:
        """The authority binding of a remote ``<owner>.claims.v1`` answer.

        When the map assigns a protected key to ``owner``, the answering
        service must name the assigned import path: the pinned
        ``service_id`` and the pair MAC prove which service answered, not
        which module it runs under the short name. Another path, or none
        (a provider older than this check), fails the step
        (:class:`ClaimsAuthorityMismatch`); a critical authority then
        refuses the mint (503). No call is added: the path rides on the
        answer. An owner the map assigns nothing is not bound (it may write
        only unprotected keys, which the mint check enforces).
        """
        authorities = self._claim_authorities
        expected = {
            path
            for key, path in authorities.authorities.items()
            if authorities.owners.get(key) == owner
        }
        if not expected or expected == {reported}:
            return
        self.logger.error(
            "claims authority %r answered over RPC as %r, but claim_authorities "
            "assigns %s: its claims are refused (the pinned service_id proves "
            "the service, not the module it runs under that name)",
            owner,
            reported,
            ", ".join(sorted(repr(p) for p in expected)),
        )
        raise ClaimsAuthorityMismatch(
            f"{owner}.claims.v1 is served by {reported!r}, not by "
            f"{', '.join(sorted(expected))}"
        )

    def _merge_claim_outcomes(
        self,
        plan: list[_ClaimsStep],
        outcomes: dict[int, _ClaimsOutcome],
        *,
        issuer: str | None = None,
        issuer_claims: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply outcomes in plan order; the first critical failure aborts.

        A non-critical failure is logged and recorded in
        ``claims_sources_failed``. The mint check runs here, in the same pass
        (ADR-0014 decision 2, no extra call): a step that writes a key it may
        not write (:meth:`_claim_violation`) is a violator. A critical
        violator refuses the mint naming itself (:class:`ClaimsUnavailable`);
        an optional one loses its WHOLE result (never a partial one), is
        recorded in ``claims_sources_failed``, logged at ERROR naming it and
        the key, and counted in ``claims_contributor_violations_total``.

        ``issuer_claims`` are the last step, the issuer's and critical: a key
        it may not write refuses the mint; its values win over the issuer's
        contributors' (it reads the row it mints for).
        """
        claims: dict[str, Any] = {}
        sources: set[str] = set()
        failed: set[str] = set()
        key_owner: dict[str, str | None] = {}
        authorities = self._claim_authorities
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
                violation = self._claim_violation(owner, value, key_owner, authorities)
                if violation is not None:
                    self._violator(owner, critical, *violation)
                    if owner:
                        failed.add(owner)
                    continue
                for key in value:
                    key_owner.setdefault(key, owner)
                claims.update(value)
            # Provenance: the owner RAN successfully (even if it contributed
            # nothing — an empty result is an authoritative "no").
            if owner:
                sources.add(owner)
        self._merge_issuer_claims(claims, key_owner, issuer, issuer_claims, sources)

        # Always stamped (JSON lists for the JWT; UserContext coerces to sets).
        # ``claims_sources_failed`` doubles as the marker that provenance is
        # authoritative: with it present an EMPTY ``claims_sources`` means "no
        # module contributed", not "legacy token, trust everything".
        claims["claims_sources"] = sorted(sources - failed)
        claims["claims_sources_failed"] = sorted(failed)
        # The per-key claim policy version (ADR-0014 decision 4, owner
        # decision #10): the digest of (key, its authority) for every key
        # this issuer's map assigns, so a verifier trusts a key only under
        # the authority both maps name. Changing one key's authority changes
        # only that key's digest.
        claims[CPV_CLAIM] = {"v": CPV_VERSION, "k": dict(authorities.digests)}
        return claims

    def _merge_issuer_claims(
        self,
        claims: dict[str, Any],
        key_owner: Mapping[str, str | None],
        issuer: str | None,
        issuer_claims: Mapping[str, Any] | None,
        sources: set[str],
    ) -> None:
        """The issuer's own claims, the last and critical step: a key it may
        not write (:meth:`_claim_violation`) refuses the mint. The issuer is
        then their verified source in ``sources`` (ADR-0014 decision 3,
        part 3)."""
        if not issuer_claims:
            return
        violation = self._claim_violation(
            issuer, issuer_claims, key_owner, self._claim_authorities
        )
        if violation is not None:
            self._violator(issuer, True, *violation)
        claims.update(issuer_claims)
        if issuer:
            sources.add(issuer)

    def _claim_violation(
        self,
        owner: str | None,
        value: Mapping[str, Any],
        key_owner: Mapping[str, str | None],
        authorities: ClaimAuthorities,
    ) -> tuple[str, str] | None:
        """The first key ``owner`` may not write, with why; else ``None``.

        - a :data:`RESERVED_CLAIM_KEYS` key is nobody's;
        - a protected key (:attr:`ClaimAuthorities.protected`) is its assigned
          authority's only: the step's owner is the authority's short name
          and, for a local owner, the module registered from the assigned
          import path (a module merely NAMED like the authority is not it);
          a remote owner is the authority's ``<m>.claims.v1`` binding, pinned
          and pair-MAC authenticated by the configuration checks. An
          unassigned protected key is nobody's;
        - any other key keeps one owner per token (PR #70): a second owner
          writing a key an earlier step wrote is a violator.
        """
        protected = authorities.protected
        for key in value:
            if key in RESERVED_CLAIM_KEYS:
                return key, "a reserved claim"
            if key in protected:
                authority = authorities.owners.get(key)
                if authority is None:
                    return key, "a protected claim no authority is assigned"
                if owner != authority:
                    return key, f"a protected claim assigned to {authority!r}"
                local = self._claims_owner_modules.get(authority)
                path = authorities.authorities.get(key)
                if local is not None and local != path:
                    return key, (
                        f"a protected claim assigned to {path!r}, not to the "
                        f"module {local!r} registered under that name"
                    )
                continue
            first = key_owner.get(key, owner)
            if first != owner:
                return key, f"a claim {first!r} wrote first in this token"
        return None

    def _violator(self, owner: str | None, critical: bool, key: str, why: str) -> None:
        """Log ``owner``'s violation; refuse the mint when it is critical."""
        self.logger.error(
            "claims contributor %r wrote the claim %r (%s); %s",
            owner,
            key,
            why,
            "refusing to mint the token"
            if critical
            else "its whole contribution is dropped",
        )
        if critical:
            raise ClaimsUnavailable(owner or "?")
        cast("CoreFacade", self).increment_metric(CLAIMS_VIOLATIONS_METRIC, 1)

    def _assign_claim_authorities(self, authorities: ClaimAuthorities) -> None:
        """Make ``authorities`` the assignment the mint check applies.

        Called once by ``create_app`` after the startup checks
        (:mod:`sandloom.core.app_factory.claim_authorities`); a facade built
        without it assigns nothing (:data:`NO_AUTHORITIES`). Once only: code
        running in the composition never re-assigns the trust map.
        """
        if self._claim_authorities_assigned:
            raise RuntimeError(
                "claim authorities are assigned once, by create_app; they "
                "cannot be re-assigned at run time"
            )
        self._claim_authorities = authorities
        self._claim_authorities_assigned = True

    @property
    def claim_authorities(self) -> ClaimAuthorities:
        """The composition's claim-authority assignment."""
        return self._claim_authorities
