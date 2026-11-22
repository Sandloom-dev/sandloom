"""The sealed mint, service tokens and test tokens (ADR-0014 decision 5,
owner answer #13, adversarial C-a2; SANDLOOM-R017).

* T-11: code in the issuer process that hands ``create_access_token``
  protected claims, provenance or ``cpv`` outside the sealed
  ``collect_claims`` result is refused; a service token
  (``create_service_token``) authenticates and every gate answers 403.
* ``collect_claims`` seals its inputs: the issuer is resolved from the
  module that registered the contributor it is identified by (never a
  string), its session is the map's token issuer's only, and no claim
  value is taken from the caller (SEC-2: every claim is a contributor's).
* T-12: ``as_user`` / ``mint_test_token`` grant rights through the kernel
  test authority in a test composition and are refused outside one; a test
  that states ``claims_sources`` gets that provenance checked.

Kernel-only: fake module paths on a facade with fake databases (no I/O
beyond the in-memory revocation cache).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, TypeVar
from uuid import uuid4

import pytest
from fastapi import FastAPI

import sandloom.core.facade._claims as _claims
from sandloom.core import error_codes
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.auth import tokens
from sandloom.core.claims import (
    AUTHORITY_CLAIM,
    CPV_VERSION,
    SealedClaims,
    UnsealedClaimsError,
)
from sandloom.core.facade import ClaimsUnavailable, CoreFacade, CoreRuntime
from sandloom.core.module_api import contract_session
from sandloom.core.runtime import use_active_core
from sandloom.core.testing.auth import make_user_headers, mint_test_token, sealed_claims
from sandloom.core.tests import test_auth_rotation_revocation as _revocation
from sandloom.core.tests.test_claims_split_matrix import GATES, statuses

mem_cache = _revocation.mem_cache

IDP = "acme.idp"
GRANTS = "acme.grants"
ROGUE = "acme.rogue"
MAP: dict[str, Any] = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": GRANTS,
    "loyalty_tier": IDP,
}
TENANT = str(uuid4())
ENVELOPE = {"token_type": "access"}
ALL_403 = dict.fromkeys(GATES, 403)
ALL_200 = dict.fromkeys(GATES, 200)

#: Keys modules of this composition declare without the map naming them
#: (a contributor's fail-safe declaration or an imported claim atom's key;
#: the kernel names none since B-N12): protected all the same.
DECLARED = frozenset({"subscription_status", "plan_code", "features", "limits"})

#: Every key a mint takes only from a sealed result: the protected claims
#: (base, an application key the map assigns, a declared key), the tenant
#: selectors, provenance, ``cpv``, the subject structure and the authority
#: binding.
SEALED_ONLY = (
    "roles",
    "permissions",
    "tenant_ids",
    "email",
    "email_verified",
    "subscription_status",
    "plan_code",
    "features",
    "limits",
    "loyalty_tier",
    "tenant_id",
    "org_id",
    "claims_sources",
    "claims_sources_failed",
    "cpv",
    "user_id",
    "provenance_authoritative",
    "claims",
    AUTHORITY_CLAIM,
)


class _FakeUoW:
    def __init__(self) -> None:
        """A unit whose session has an empty ``info`` mapping."""
        self.session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        """Enter the unit; nothing to open."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Leave the unit; nothing to commit."""
        return None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        """The fake unit-of-work class."""
        return _FakeUoW

    def outbox(self) -> None:
        """No outbox."""
        return None

    def audit(self) -> None:
        """No audit sink."""
        return None


class _Counted:
    """A contributor answering ``result`` and counting its calls."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the answer."""
        self.result = result
        self.calls = 0

    async def __call__(self, user_id: Any) -> dict[str, Any]:
        """Answer and count."""
        self.calls += 1
        return dict(self.result)


class _Gated(_Counted):
    """A contributor that stops inside its call until ``release`` is set, then
    answers (or raises ``fail``): a local step suspended mid-flight."""

    def __init__(self, result: dict[str, Any]) -> None:
        """Remember the answer; the gate starts closed."""
        super().__init__(result)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail: Exception | None = None

    async def __call__(self, user_id: Any) -> dict[str, Any]:
        """Signal the entry, wait for the release, then answer or raise."""
        self.entered.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        return await super().__call__(user_id)


class _Driven(_Counted):
    """A contributor whose answer is made by ``body`` (a test that drives
    another call from inside a contributor's step)."""

    def __init__(self, body: Callable[[Any], Awaitable[dict[str, Any]]]) -> None:
        """Remember the body."""
        super().__init__({})
        self.body = body

    async def __call__(self, user_id: Any) -> dict[str, Any]:
        """Count, then answer what the body makes of the user."""
        self.calls += 1
        return await self.body(user_id)


class _SameTaskProxy:
    """A remote ``<owner>.claims.v1`` client whose server runs on the caller's
    task, as an in-process transport does (``httpx.ASGITransport``,
    ``contract_testing.in_process_rpc_client``)."""

    def __init__(self, server: CoreFacade, contract: str) -> None:
        """Serve ``contract`` of ``server``."""
        self.server = server
        self.contract = contract

    async def contribute(self, user_id: Any) -> dict[str, Any]:
        """Answer as the server's served method does, on this task."""
        answer: dict[str, Any] = await self.server.local_contract(
            self.contract
        ).contribute(user_id)
        return answer


_G = TypeVar("_G", bound=_Counted)


def _register_fn(core: CoreFacade, path: str, fn: _G, *, critical: bool = False) -> _G:
    with core.module_registration(path) as api:
        api.add_claims_contributor(fn, critical=critical)
    return fn


def _register(
    core: CoreFacade, path: str, result: dict[str, Any], *, critical: bool = False
) -> _Counted:
    return _register_fn(core, path, _Counted(result), critical=critical)


def _core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    policy = dataclasses.replace(build_claim_authorities(MAP), declared=DECLARED)
    core._assign_claim_authorities(policy)
    return core


@pytest.fixture
def core() -> CoreFacade:
    """The issuer and verifier: ``acme.idp`` vouches for ``roles`` /
    ``permissions``, ``acme.grants`` for the tenant grant."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"], "permissions": ["x.read"]}, critical=True)
    _register(core, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    return core


@pytest.fixture(autouse=True)
def _fresh_verification_cache() -> Iterator[None]:
    tokens._verified_tokens.clear()
    yield
    tokens._verified_tokens.clear()


def _envelope() -> dict[str, Any]:
    return {"sub": str(uuid4()), **ENVELOPE}


# --------------------------------------------------------------------------- #
# the sealed result
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_collect_claims_returns_a_sealed_read_only_result(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    assert isinstance(sealed, SealedClaims) and sealed.is_sealed()
    assert sealed["roles"] == ["admin"]
    assert sealed["claims_sources"] == ["grants", "idp"]
    with pytest.raises(TypeError):
        sealed["roles"] = ["owner"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        sealed._data = {}  # type: ignore[misc]
    with pytest.raises(UnsealedClaimsError, match="SANDLOOM-R017"):
        SealedClaims({"roles": ["admin"]})


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_the_sealed_result_mints_a_token_every_gate_trusts(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    token = tokens.create_access_token(_envelope(), claims=sealed)
    assert (await statuses(core, token))["/role"] == 200
    with use_active_core(core):
        payload = tokens.decode_token(token)
    assert payload is not None
    assert view.UNTRUSTED_KEY not in payload
    assert payload["tenant_ids"] == [TENANT]


# --------------------------------------------------------------------------- #
# T-11: hand-minting is refused
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", SEALED_ONLY)
def test_a_hand_minted_sealed_key_is_refused(core: CoreFacade, key: str) -> None:
    with use_active_core(core), pytest.raises(UnsealedClaimsError) as refused:
        tokens.create_access_token({**_envelope(), key: ["admin"]})
    assert "SANDLOOM-R017" in str(refused.value)
    assert key in str(refused.value)
    assert error_codes.code_for_exception(refused.value) == "SANDLOOM-R017"


def test_without_an_active_composition_the_base_keys_are_still_refused() -> None:
    """No core bound: the protected base, provenance and ``cpv`` still are."""
    for key in ("roles", "tenant_ids", "email", "claims_sources", "cpv"):
        with pytest.raises(UnsealedClaimsError):
            tokens.create_access_token({**_envelope(), key: "x"})


@pytest.mark.asyncio
async def test_a_spread_sealed_result_is_a_plain_dict_and_refused(
    core: CoreFacade,
) -> None:
    sealed = await core.collect_claims(uuid4())
    with use_active_core(core), pytest.raises(UnsealedClaimsError):
        tokens.create_access_token({**sealed, **_envelope()})
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match="sealed"):
        tokens.create_access_token(_envelope(), claims=dict(sealed))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_data_never_overrides_a_sealed_claim(core: CoreFacade) -> None:
    _register(core, "acme.prefs", {"nickname": "ann"})
    sealed = await core.collect_claims(uuid4())
    assert sealed["nickname"] == "ann"
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match="nickname"):
        tokens.create_access_token({**_envelope(), "nickname": "eve"}, claims=sealed)


def test_an_unprotected_key_in_data_is_minted(core: CoreFacade) -> None:
    with use_active_core(core):
        token = tokens.create_access_token({**_envelope(), "locale": "en"})
        payload = tokens.decode_token(token)
    assert payload is not None and payload["locale"] == "en"


def test_r017_is_in_the_catalogue() -> None:
    entry = error_codes.get("SANDLOOM-R017")
    assert entry.slug == "unsealed-claims"
    assert entry.kind == "runtime"


# --------------------------------------------------------------------------- #
# collect_claims seals its inputs: the issuer and its session (C-a2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_issuer_is_never_a_string(core: CoreFacade) -> None:
    with pytest.raises(UnsealedClaimsError, match="not a name"):
        await core.collect_claims(uuid4(), object(), issuer="idp")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_issuer_is_a_registered_contributor(core: CoreFacade) -> None:
    async def stray(_user_id: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(UnsealedClaimsError, match="not a claims contributor"):
        await core.collect_claims(uuid4(), object(), issuer=stray)


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["precomputed", "issuer_claims"])
async def test_collect_claims_takes_no_claim_value(
    core: CoreFacade, given: str
) -> None:
    """SEC-2: no entry hands the mint a claim value. ``precomputed`` and
    ``issuer_claims`` are gone (no alias), on the facade and on the
    ``CoreRuntime`` view module code holds; nothing runs."""
    idp = core._contributors_of("idp")[0]
    value = {idp: {"roles": ["owner"]}} if given == "precomputed" else {"email": "e"}
    with pytest.raises(TypeError, match=given):
        await core.collect_claims(uuid4(), object(), issuer=idp, **{given: value})
    with pytest.raises(TypeError, match=given):
        await CoreRuntime(core).collect_claims(
            uuid4(), object(), issuer=idp, **{given: value}
        )
    assert idp.calls == 0


@pytest.mark.asyncio
async def test_the_issuers_fields_come_from_its_own_contributor() -> None:
    """``email`` / ``email_verified`` are what the issuer's contributor
    returned in this pass, run once in the issuer's session."""
    core = _core()
    fields = {"roles": [], "email": "u@example.com", "email_verified": True}
    idp = _register(core, IDP, fields, critical=True)
    sealed = await core.collect_claims(uuid4(), object(), issuer=idp)
    assert sealed["email"] == "u@example.com" and sealed["email_verified"] is True
    assert "idp" in sealed["claims_sources"]
    assert idp.calls == 1


@pytest.mark.asyncio
async def test_another_module_writing_the_issuers_fields_is_a_violator() -> None:
    """``acme.rogue`` writes ``email``: optional, it loses its whole result;
    critical, it refuses the mint. The issuer's value is never replaced."""
    core = _core()
    idp = _register(
        core, IDP, {"email": "u@example.com", "email_verified": False}, critical=True
    )
    _register(core, ROGUE, {"email": "eve@example.com", "email_verified": True})
    sealed = await core.collect_claims(uuid4(), object(), issuer=idp)
    assert sealed["email"] == "u@example.com" and sealed["email_verified"] is False
    assert sealed["claims_sources_failed"] == ["rogue"]

    critical = _core()
    idp = _register(critical, IDP, {"email": "u@example.com"}, critical=True)
    _register(critical, ROGUE, {"email_verified": True}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await critical.collect_claims(uuid4(), object(), issuer=idp)
    assert refused.value.owner == "rogue"


# --------------------------------------------------------------------------- #
# the authority binding of a local step (kb-mint-budget-optimize)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session", [None, object()], ids=["no-session", "issuer-session"]
)
async def test_a_local_step_builds_no_authority_binding_the_served_method_does(
    monkeypatch: pytest.MonkeyPatch, session: object | None
) -> None:
    """The mint's own ``contribute`` calls (the issuer's and a local owner's)
    drop the binding of their answer anyway, so they do not build it; the
    same method served over RPC (here: called outside the mint) still names
    its module, which the remote issuer binds to its map."""
    core = _core()
    idp = _register(core, IDP, {"roles": ["admin"]}, critical=True)
    _register(core, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    raw: list[dict[str, Any]] = []
    drop = _claims._local_answer

    def spy(answer: Any) -> Any:
        raw.append(dict(answer))
        return drop(answer)

    monkeypatch.setattr(_claims, "_local_answer", spy)
    if session is None:
        sealed = await core.collect_claims(uuid4())
    else:
        sealed = await core.collect_claims(uuid4(), session, issuer=idp)
    assert sealed["roles"] == ["admin"] and sealed["tenant_ids"] == [TENANT]
    assert AUTHORITY_CLAIM not in sealed
    assert len(raw) == 2
    assert all(AUTHORITY_CLAIM not in answer for answer in raw)
    served = await core.local_contract("idp.claims.v1").contribute(uuid4())
    assert served[AUTHORITY_CLAIM] == IDP
    served = await core.local_contract("grants.claims.v1").contribute(uuid4())
    assert served[AUTHORITY_CLAIM] == GRANTS


@pytest.mark.asyncio
async def test_a_served_call_beside_a_local_step_still_names_its_module() -> None:
    """The local step is told apart per task, not per process: a served
    ``contribute`` (an RPC request of the same process) that runs while a
    local step of a mint is suspended inside its own call still builds the
    binding, so the remote issuer that asked binds it to its map."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    grants = _register_fn(core, GRANTS, _Gated({"tenant_ids": [TENANT]}), critical=True)
    mint = asyncio.create_task(core.collect_claims(uuid4()))
    try:
        await asyncio.wait_for(grants.entered.wait(), timeout=5)
        assert len(_claims._LOCAL_STEP_TASKS) == 1
        served = await core.local_contract("idp.claims.v1").contribute(uuid4())
        assert served[AUTHORITY_CLAIM] == IDP
    finally:
        grants.release.set()
    sealed = await mint
    assert sealed["tenant_ids"] == [TENANT] and AUTHORITY_CLAIM not in sealed


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raises", "cancelled"])
async def test_a_local_step_leaves_no_mark_however_it_ends(how: str) -> None:
    """A task left in the set would make its next served ``contribute`` answer
    without the binding (a remote issuer then refuses the owner)."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    gated = _Gated({"tenant_ids": [TENANT]})
    if how == "raises":
        gated.fail = RuntimeError("boom")
    _register_fn(core, GRANTS, gated, critical=True)
    mint = asyncio.create_task(core.collect_claims(uuid4()))
    try:
        await asyncio.wait_for(gated.entered.wait(), timeout=5)
        assert len(_claims._LOCAL_STEP_TASKS) == 1
        if how == "cancelled":
            mint.cancel()
    finally:
        gated.release.set()
    if how == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await mint
    else:
        with pytest.raises(ClaimsUnavailable):
            await mint
    assert _claims._LOCAL_STEP_TASKS == set()
    gated.fail = None
    served = await core.local_contract("grants.claims.v1").contribute(uuid4())
    assert served[AUTHORITY_CLAIM] == GRANTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session", [None, object()], ids=["no-session", "issuer-session"]
)
@pytest.mark.parametrize("value", [GRANTS, {"a": 1}, 1], ids=["str", "dict", "int"])
async def test_a_local_contributor_writing_the_binding_key_is_a_violator(
    session: object | None, value: Any
) -> None:
    """Not building the binding for a local call must not let a contributor's
    own value under the kernel's key through: optional, it loses its whole
    result; critical, it refuses the mint (a str is never dropped as a
    binding)."""
    core = _core()
    idp = _register(core, IDP, {"roles": ["admin"]}, critical=True)
    _register(core, ROGUE, {"loyalty_tier": "gold", AUTHORITY_CLAIM: value})

    def collect(c: CoreFacade) -> Any:
        if session is None:
            return c.collect_claims(uuid4())
        return c.collect_claims(uuid4(), session, issuer=idp)

    sealed = await collect(core)
    assert sealed["claims_sources_failed"] == ["rogue"]
    assert "loyalty_tier" not in sealed and AUTHORITY_CLAIM not in sealed

    critical = _core()
    idp = _register(critical, IDP, {"roles": ["admin"]}, critical=True)
    _register(critical, ROGUE, {AUTHORITY_CLAIM: value}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        await collect(critical)
    assert refused.value.owner == "rogue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session", [None, object()], ids=["no-session", "issuer-session"]
)
@pytest.mark.parametrize(
    "value",
    [IDP, GRANTS, {"a": 1}, 1],
    ids=["own-path", "other-path", "dict", "int"],
)
async def test_the_issuers_own_contributor_writing_the_binding_key_is_a_violator(
    session: object | None, value: Any
) -> None:
    """The contributor passed as ``issuer=`` is the one whose step the mint
    runs itself (a :class:`~sandloom.core.facade._claims._MintIssuerContract`
    when it has the issuer's session): it must not carry the kernel's key
    into a token either. A str, its own path included, would look like the
    binding the local call drops, so the key is replaced by a marker before
    that; a critical issuer refuses the mint, with or without the session."""
    core = _core()
    idp = _register(
        core, IDP, {"roles": ["admin"], AUTHORITY_CLAIM: value}, critical=True
    )
    _register(core, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    with pytest.raises(ClaimsUnavailable) as refused:
        if session is None:
            await core.collect_claims(uuid4(), issuer=idp)
        else:
            await core.collect_claims(uuid4(), session, issuer=idp)
    assert refused.value.owner == "idp"
    assert idp.calls == 1


def _remote_issuer(server: CoreFacade, monkeypatch: pytest.MonkeyPatch) -> CoreFacade:
    """A process whose map assigns ``tenant_ids`` to ``acme.grants`` with no
    local owner for it: its mint asks ``grants.claims.v1`` of ``server`` over
    an in-process transport."""
    issuer = _core()
    monkeypatch.setattr(
        issuer.settings,
        "service_bindings",
        {"grants.claims.v1": {"base_url": "http://grants"}},
    )
    proxy = _SameTaskProxy(server, "grants.claims.v1")
    issuer.set_service_resolver(lambda name, binding, **_: proxy)
    return issuer


@pytest.mark.asyncio
async def test_a_served_call_on_the_marked_task_answers_no_binding_and_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark is per task, not per call (the bound contract is shared with
    RPC and carries no per-call state), so a served ``contribute`` awaited on
    the task of a suspended local step answers without its binding. Only an
    in-process transport reaches it: the remote issuer refuses the answer as
    a missing binding (``ClaimsUnavailable``, fail closed, never an
    acceptance); a request served on its own task names its module."""
    server = _core()
    _register(server, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    issuer = _remote_issuer(server, monkeypatch)
    ok = await issuer.collect_claims(uuid4())
    assert ok["tenant_ids"] == [TENANT]
    seen: dict[str, Any] = {}

    async def idp_body(user_id: Any) -> dict[str, Any]:
        seen["marked"] = asyncio.current_task() in _claims._LOCAL_STEP_TASKS
        try:
            seen["nested"] = await issuer.collect_claims(uuid4())
        except ClaimsUnavailable as refused:
            seen["nested"] = refused
        return {"roles": []}

    _register_fn(server, IDP, _Driven(idp_body), critical=True)
    await server.collect_claims(uuid4())
    assert seen["marked"] is True
    refused = seen["nested"]
    assert isinstance(refused, ClaimsUnavailable) and refused.owner == "grants"
    assert isinstance(refused.__cause__, _claims.ClaimsAuthorityMismatch)
    assert _claims._LOCAL_STEP_TASKS == set()


@pytest.mark.asyncio
async def test_a_task_a_contributor_spawns_is_not_marked() -> None:
    """Only an await on the marked task itself is the mint's own: a child task
    however made (``create_task``, ``gather``, a ``TaskGroup``, a copied
    context, ``shield``) is another task and its served call names its
    module."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    got: dict[str, Any] = {}

    async def served() -> dict[str, Any]:
        answer: dict[str, Any] = await core.local_contract("idp.claims.v1").contribute(
            uuid4()
        )
        return answer

    async def grants_body(user_id: Any) -> dict[str, Any]:
        got["create_task"] = await asyncio.create_task(served())
        got["gather"] = (await asyncio.gather(served()))[0]
        async with asyncio.TaskGroup() as group:
            child = group.create_task(served())
        got["taskgroup"] = child.result()
        got["copied_context"] = await asyncio.get_running_loop().create_task(
            served(), context=contextvars.copy_context()
        )
        got["shield"] = await asyncio.shield(served())
        got["same_task"] = await served()
        return {"tenant_ids": [TENANT]}

    _register_fn(core, GRANTS, _Driven(grants_body), critical=True)
    sealed = await core.collect_claims(uuid4())
    assert sealed["tenant_ids"] == [TENANT]
    for how in ("create_task", "gather", "taskgroup", "copied_context", "shield"):
        assert got[how][AUTHORITY_CLAIM] == IDP, how
    assert AUTHORITY_CLAIM not in got["same_task"]
    assert _claims._LOCAL_STEP_TASKS == set()


@pytest.mark.asyncio
async def test_a_mint_re_entered_from_a_contributor_keeps_the_outer_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inner step of a mint run from inside a contributor finds its task
    marked already: it adds nothing and discards nothing, so the outer step
    stays marked until its own call ends (its answer builds no binding)."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    state: dict[str, Any] = {"depth": 0}
    raw: list[dict[str, Any]] = []
    drop = _claims._local_answer

    def spy(answer: Any) -> Any:
        raw.append(dict(answer))
        return drop(answer)

    monkeypatch.setattr(_claims, "_local_answer", spy)

    async def grants_body(user_id: Any) -> dict[str, Any]:
        state["depth"] += 1
        if state["depth"] == 1:
            state["before"] = asyncio.current_task() in _claims._LOCAL_STEP_TASKS
            await core.collect_claims(uuid4())
            state["after"] = asyncio.current_task() in _claims._LOCAL_STEP_TASKS
        return {"tenant_ids": [TENANT]}

    _register_fn(core, GRANTS, _Driven(grants_body), critical=True)
    sealed = await core.collect_claims(uuid4())
    assert state["before"] is True and state["after"] is True
    assert sealed["tenant_ids"] == [TENANT] and AUTHORITY_CLAIM not in sealed
    assert all(AUTHORITY_CLAIM not in answer for answer in raw)
    assert _claims._LOCAL_STEP_TASKS == set()


def test_a_served_call_runs_without_a_running_event_loop() -> None:
    """``asyncio.current_task`` raises with no running loop (another async
    backend, a coroutine stepped by hand): the call still answers, with its
    binding."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    step = _claims.ClaimsContributorContract(core, "idp").contribute(uuid4())
    with pytest.raises(StopIteration) as answered:
        step.send(None)
    assert answered.value.value[AUTHORITY_CLAIM] == IDP


@pytest.mark.asyncio
async def test_a_step_with_no_task_marks_nothing() -> None:
    """A local step run from a loop callback has no current task: nothing is
    marked (no shared ``None``), so a served call in that window still names
    its module and the step leaves the set as it found it."""
    core = _core()
    _register(core, IDP, {"roles": ["admin"]}, critical=True)
    seen: dict[str, Any] = {}

    async def grants_body(user_id: Any) -> dict[str, Any]:
        seen["marks"] = set(_claims._LOCAL_STEP_TASKS)
        seen["served"] = await _claims.ClaimsContributorContract(
            core, "idp"
        ).contribute(uuid4())
        return {"tenant_ids": [TENANT]}

    _register_fn(core, GRANTS, _Driven(grants_body), critical=True)
    finished: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def callback() -> None:
        assert asyncio.current_task() is None
        step = core._local_claims_call("grants", uuid4())()
        try:
            step.send(None)
        except StopIteration as stop:
            finished.set_result(stop.value)

    asyncio.get_running_loop().call_soon(callback)
    answer = await asyncio.wait_for(finished, timeout=5)
    assert answer["tenant_ids"] == [TENANT]
    assert seen["marks"] == set() and _claims._LOCAL_STEP_TASKS == set()
    assert seen["served"][AUTHORITY_CLAIM] == IDP


@pytest.mark.asyncio
async def test_nothing_set_on_the_public_class_strips_a_served_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mint's own issuer call is a private subclass: an attribute set on
    ``ClaimsContributorContract`` (reachable through ``sandloom.sdk.infra``)
    does not turn the binding off for the answers a process serves."""
    core = _core()
    _register(core, GRANTS, {"tenant_ids": [TENANT]}, critical=True)
    monkeypatch.setattr(
        _claims.ClaimsContributorContract, "_for_the_mint", True, raising=False
    )
    served = await core.local_contract("grants.claims.v1").contribute(uuid4())
    assert served[AUTHORITY_CLAIM] == GRANTS


@pytest.mark.asyncio
async def test_the_issuers_contributor_reads_the_issuer_session() -> None:
    """The issuer's step builds no binding and is no registered contract, but
    its contributors still run as a contract call: ``contract_session()``
    answers the issuer's own session there."""
    core = _core()
    seen: list[Any] = []

    async def idp_body(user_id: Any) -> dict[str, Any]:
        seen.append(contract_session())
        return {"roles": ["admin"]}

    idp = _register_fn(core, IDP, _Driven(idp_body), critical=True)
    session = object()
    sealed = await core.collect_claims(uuid4(), session, issuer=idp)
    assert sealed["roles"] == ["admin"] and seen == [session]


# --------------------------------------------------------------------------- #
# the mint issuer binding (SEC-2): the issuer session is the map's issuer's
# only, identified by its short name AND import path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_authority_that_is_not_the_issuer_takes_no_issuer_session(
    core: CoreFacade,
) -> None:
    """``acme.grants`` is the authority of ``tenant_ids`` but not the
    issuer: naming its own contributor as ``issuer`` with a stand-in session
    is refused before anything runs."""
    grants = core._contributors_of("grants")[0]
    with pytest.raises(UnsealedClaimsError, match="not the claim_authorities issuer"):
        await core.collect_claims(uuid4(), object(), issuer=grants)
    assert grants.calls == 0


@pytest.mark.asyncio
async def test_a_module_named_like_the_issuer_from_another_path_is_not_it() -> None:
    """``evil.idp`` registers under the issuer's short name ``idp``: the map
    names ``acme.idp``, so its session is refused before anything runs."""
    core = _core()
    impostor = _register(core, "evil.idp", {"roles": []}, critical=True)
    with pytest.raises(UnsealedClaimsError, match="not the claim_authorities issuer"):
        await core.collect_claims(uuid4(), object(), issuer=impostor)
    assert impostor.calls == 0


@pytest.mark.asyncio
async def test_a_process_whose_map_names_no_issuer_takes_no_issuer_session() -> None:
    """A process that does not compose the map's issuer (the filtered map
    names none) runs no contributor in a caller's session."""
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core._assign_claim_authorities(build_claim_authorities({"roles": IDP}))
    idp = _register(core, IDP, {"roles": ["admin"]}, critical=True)
    with pytest.raises(UnsealedClaimsError, match="names no issuer"):
        await core.collect_claims(uuid4(), object(), issuer=idp)
    assert idp.calls == 0
    # Without a session the contributor runs in its own unit, as any other.
    sealed = await core.collect_claims(uuid4(), issuer=idp)
    assert sealed["roles"] == ["admin"]


# --------------------------------------------------------------------------- #
# T-11: the service token grants no user rights
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", SEALED_ONLY)
def test_a_service_token_refuses_every_sealed_key(core: CoreFacade, key: str) -> None:
    with use_active_core(core), pytest.raises(UnsealedClaimsError, match=key):
        tokens.create_service_token({**_envelope(), key: ["admin"]})


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_service_token_is_denied_by_every_gate(core: CoreFacade) -> None:
    with use_active_core(core):
        token = tokens.create_service_token({**_envelope(), "scope": "billing"})
        payload = tokens.decode_token(token)
    assert payload is not None
    # The kernel's own provenance and the current cpv: absent protected
    # claims are a definite deny, never "refresh" (token_policy_outdated).
    assert payload["claims_sources"] == []
    assert payload["claims_sources_failed"] == []
    assert payload["cpv"] == {
        "v": CPV_VERSION,
        "k": dict(core.claim_authorities.digests),
    }
    assert view.UNTRUSTED_KEY not in payload
    assert payload["scope"] == "billing"
    got = await statuses(core, token)
    assert {k: v for k, v in got.items() if k != "/tenant"} == {
        k: v for k, v in ALL_403.items() if k != "/tenant"
    }
    assert got["/tenant"] in (400, 403)


# --------------------------------------------------------------------------- #
# T-12: the kernel test authority
# --------------------------------------------------------------------------- #


def _admin_claims() -> dict[str, Any]:
    return {
        **_envelope(),
        "roles": ["admin"],
        "permissions": ["x.read"],
        "tenant_id": TENANT,
        "tenant_ids": [TENANT],
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_token_passes_every_gate_in_a_test_app(core: CoreFacade) -> None:
    assert view.test_authority_enabled()
    assert await statuses(core, mint_test_token(_admin_claims())) == ALL_200
    _, headers = make_user_headers(roles=["admin"], permissions=["x.read"])
    bearer = headers["Authorization"].removeprefix("Bearer ")
    assert (await statuses(core, bearer))["/role"] == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_stating_claims_sources_gets_that_provenance_checked(
    core: CoreFacade,
) -> None:
    """``roles`` stated from ``rogue``: the map's authority is ``idp``, so
    the roles are untrusted (403); stated from ``idp`` they are trusted."""
    claims = {**_admin_claims(), "claims_sources_failed": []}
    rogue = mint_test_token({**claims, "claims_sources": ["rogue", "grants"]})
    got = await statuses(core, rogue)
    assert got["/role"] == got["/admin"] == got["/perm"] == 403
    assert got["/tenant"] == 200
    idp = mint_test_token({**claims, "claims_sources": ["idp", "grants"]})
    assert await statuses(core, idp) == ALL_200
    failed = mint_test_token(
        {
            **claims,
            "claims_sources": ["idp", "grants"],
            "claims_sources_failed": ["idp"],
        }
    )
    assert (await statuses(core, failed))["/role"] == 403


def test_the_test_authority_is_refused_outside_a_test_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(view, "_test_composition", [False])
    for mint in (
        lambda: sealed_claims({"roles": ["admin"]}),
        lambda: mint_test_token(_admin_claims()),
        lambda: make_user_headers(roles=["admin"]),
    ):
        with pytest.raises(UnsealedClaimsError, match="test composition"):
            mint()


@pytest.mark.asyncio
@pytest.mark.usefixtures("mem_cache")
async def test_a_test_token_grants_nothing_outside_a_test_composition(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint_test_token(_admin_claims())
    monkeypatch.setattr(view, "_test_composition", [False])
    got = await statuses(core, token)
    assert got["/role"] != 200 and got["/admin"] != 200 and got["/tenant"] != 200


def test_sealed_claims_states_a_cpv_or_none() -> None:
    assert sealed_claims({"roles": []})["cpv"] == view.TEST_CPV
    assert sealed_claims({"roles": []})["claims_sources"] == [view.TEST_AUTHORITY]
    assert "cpv" not in sealed_claims({"roles": [], "cpv": None})
    stated = sealed_claims({"roles": [], "claims_sources": ["idp"]})
    assert stated["claims_sources"] == ["idp"]
