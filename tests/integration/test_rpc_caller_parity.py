"""A split deploy behaves EXACTLY like the monolith for security context
(round-3 review, "RPC parity"): what crosses a signed RPC call in both
directions is one design, the *caller facts* out and the *call effects* back.

* out (``usr`` of the MAC-covered context): the end user, the facts the
  provider's checks read (``iat_us`` for the session check, ``auth_time`` and
  the request's re-authentication for step-up, the request's operation key
  for money dedupe) and whether the commit-time re-check reaches command
  units. The provider binds them as its call's request scope, so a nested
  call carries them on, hop after hop.
* back (``fx`` of the MAC-bound response, success AND error): every risk the
  execution observed, the subjects it self-revoked, a re-authentication and
  the step-ups it left pending.

Each scenario runs LOCAL (``core.get_service``) and SPLIT (every call, nested
ones included, through the generated proxy and the provider's real signed RPC
routes, in-process ASGI) and must produce identical outcomes: status or
result, risks, the request's audit row, the authorization decision. The
intent-window timeout case needs real timing: a loopback HTTP server.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, ClassVar
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from sandloom.core import contract_testing, rpc_binding, rpc_identity
from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
    served_rpc,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import reservation as idempotency_reservation
from sandloom.core.module_api import (
    ContractCallError,
    ContractCallInProgress,
    contract_method,
)
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.rpc.errors import RemoteServiceBadResponse
from sandloom.core.rpc.proxy import RemoteServiceProxy
from sandloom.core.rpc_transport import HttpxRpcClient
from sandloom.core.testing import InMemoryCache
from sandloom.core.tests.test_contract_parity import _FakeDatabase
from sandloom.core.unit_of_work import risk as _risk
from sandloom.core.unit_of_work.hooks import on_commit
from sandloom.core.unit_of_work.intent import IntentState, external_call
from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope
from sandloom.core.unit_of_work.scope import _AUDIT_STORE_KEY
from sandloom_billing.credits import ensure_tenant_matches

pytestmark = pytest.mark.asyncio

_SVC = "svc"
MONEY = "parity.money.v1"
BILL = "parity.bill.v1"
WRAP = "parity.wrap.v1"
GATE = "parity.gate.v1"

#: contract name -> what a provider body calls (a local service or a proxy)
_VIA: dict[str, Any] = {}
#: a result size above the bounded client's limit (:data:`_CLIENT_LIMIT`)
_BLOATED, _CLIENT_LIMIT = 64 * 1024, 4096


@_risk.sensitive_effect("security")
async def _change_secret() -> bool:
    return True


@_risk.sensitive_effect("money")
async def _settle(*, operation_key: str) -> None:
    """A keyed money effect an on_commit callback runs."""


#: the risk source the callback's money effect is recorded under
SETTLE = f"{_settle.__module__}.{_settle.__qualname__}"


class Money:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key", risk="money")
    async def spend(cls, amount: int, operation_key: str) -> int:
        return amount


class Bill:
    """billing's REAL personal-account rule (G-BILLING-20)."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend_personal(cls, account: UUID, operation_key: str) -> int:
        ensure_tenant_matches("user", account)
        return 77


class Wrap:
    """Untagged commands whose bodies make the nested calls."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend_then_conflict(cls, operation_key: str) -> int:
        await _VIA[MONEY].spend(amount=3, operation_key=f"{operation_key}:inner")
        raise HTTPException(status_code=409, detail="conflict after the spend")

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def charge(cls, account: UUID, operation_key: str) -> int:
        charged: int = await _VIA[BILL].spend_personal(
            account=account, operation_key=f"{operation_key}:inner"
        )
        return charged

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def change_secret(cls, operation_key: str) -> bool:
        return await _change_secret()

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def reauthenticate(cls, operation_key: str) -> bool:
        _risk.mark_reauthenticated()
        return True

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend_quietly(cls, operation_key: str) -> int:
        """Untagged: its money risk is observed only while its body runs."""
        spent: int = await _VIA[MONEY].spend(
            amount=3, operation_key=f"{operation_key}:inner"
        )
        return spent

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def settle_after_commit(cls, operation_key: str, tagged: bool) -> int:
        """Untagged: its money effect runs in an on_commit callback, the
        tagged effect itself or a closure around it."""
        if tagged:
            await on_commit(partial(_settle, operation_key=operation_key))
        else:
            await on_commit(lambda: _settle(operation_key=operation_key))
        return 5

    _hung: ClassVar[list[str]] = []
    _paid: ClassVar[asyncio.Event | None] = None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def settle_then_hang(cls, operation_key: str) -> int:
        """Untagged: an on_commit closure completes a keyed money effect,
        then hangs until its request is cancelled (a request timeout)."""
        assert operation_key not in cls._hung, "a replay re-executed the body"
        cls._hung.append(operation_key)
        paid = cls._paid = asyncio.Event()

        async def pay_then_hang() -> None:
            await _settle(operation_key=operation_key)
            paid.set()
            await asyncio.Event().wait()

        await on_commit(pay_then_hang)
        return 5

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def gate_quietly(cls, operation_key: str) -> int:
        """Swallows the failure of its nested (remote) gate command."""
        try:
            ran: int = await _VIA[GATE].run(operation_key=f"{operation_key}:gate")
        except HTTPException:
            return 0
        return ran

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def gate_bloated(cls, operation_key: str) -> str:
        """Swallows the failure of its nested gate command, then answers a
        result too large for its caller's bounded client."""
        try:
            await _VIA[GATE].run(operation_key=f"{operation_key}:gate")
        except HTTPException:
            pass
        return "x" * _BLOATED

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def relay_gate(cls, operation_key: str) -> int:
        """One more hop in front of :meth:`gate_quietly`."""
        relayed: int = await _VIA[WRAP].gate_quietly(
            operation_key=f"{operation_key}:relay"
        )
        return relayed


class Gate:
    _running = False
    _release: asyncio.Event | None = None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def run(cls, operation_key: str) -> int:
        cls._running = True
        try:
            if cls._release is not None:
                await cls._release.wait()
        finally:
            cls._running = False
        return 1

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def refuse(cls, operation_key: str) -> int:
        raise HTTPException(status_code=409, detail="no")


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[CoreFacade]:
    from sandloom.core.config.settings import settings

    cache = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": cache)
    monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
    allowed = {_SVC: [MONEY, BILL, WRAP, GATE]}
    for name, value in rpc_settings(_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    for name, provider in ((MONEY, Money), (BILL, Bill), (WRAP, Wrap), (GATE, Gate)):
        facade.register_service(name, provider)
    Gate._running, Gate._release = False, None
    Wrap._hung, Wrap._paid = [], None
    yield facade
    _VIA.clear()


@asynccontextmanager
async def _deployed(core: CoreFacade, mode: str) -> AsyncIterator[None]:
    """Bind every contract as a body sees it: local services, or proxies."""
    if mode == "local":
        _VIA.update({n: core.get_service(n) for n in (MONEY, BILL, WRAP)})
        yield
        return
    async with in_process_rpc_client(core) as client:
        _VIA.update({n: rpc_proxy_for(core, n, client) for n in (MONEY, BILL, WRAP)})
        yield


ALICE, BOB = uuid4(), uuid4()


class _Policy:
    method, path, is_write, risk = "POST", "/parity", True, None


class _AuditRecorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, session: Any, **row: Any) -> None:
        self.rows.append(row)


def _scope(principal: str) -> RequestScope | None:
    """The calling request: ``system`` (none), ``user`` (fresh auth),
    ``armed`` (fresh, commit-time re-check armed), ``stale`` (auth an hour
    old), ``intent`` (stale, an operation-intent route: no re-check context),
    ``reauth`` (stale, re-authenticated in the request)."""
    if principal == "system":
        return None
    scope = RequestScope(core=None, policy=_Policy())
    fresh = principal in {"user", "armed"}
    auth_time = int(time.time()) - (5 if fresh else 3600)
    scope.subject = str(ALICE)
    scope.claims = {"sub": str(ALICE), "iat_us": 1, "auth_time": auth_time}
    if principal == "armed":

        async def _check() -> None:
            return None

        scope.check = _check
    if principal == "intent":
        scope.intent = IntentState()
    return scope


async def _observe(core: CoreFacade, mode: str, principal: str, scenario: str) -> Any:
    """One scenario, observed: (outcome, risks, the audit row's risks and
    actor or the commit's refusal, re-authenticated, pending step-ups)."""
    scope = _scope(principal)
    key = f"{scenario}-{uuid4()}"
    async with _deployed(core, mode):
        wrap = _VIA[WRAP]
        call: Callable[[], Awaitable[Any]] = {
            "conflict": lambda: wrap.spend_then_conflict(operation_key=key),
            "bob": lambda: wrap.charge(account=BOB, operation_key=key),
            "self": lambda: wrap.charge(account=ALICE, operation_key=key),
            "secret": lambda: wrap.change_secret(operation_key=key),
            "reauth": lambda: wrap.reauthenticate(operation_key=key),
        }[scenario]
        if scope is None:
            try:
                return ("ok", await call())
            except ContractCallError as exc:
                return ("error", exc.status_code)
        with bind_request_scope(scope):
            if principal == "reauth":
                _risk.mark_reauthenticated()
            try:
                outcome: Any = ("ok", await call())
            except ContractCallError as exc:
                outcome = ("error", exc.status_code)
    state = scope.risk
    if state is None:
        return (outcome, None)
    audit = _AuditRecorder()
    session = type("S", (), {"info": {_AUDIT_STORE_KEY: audit}})()
    try:
        with bind_request_scope(scope):
            _risk.before_commit(session, scope, wrote=False)
        commit: Any = [(r["data"]["risks"], r["actor_id"]) for r in audit.rows]
    except HTTPException as exc:
        commit = ("commit refused", exc.status_code)
    return (
        outcome,
        {risk: sorted(sources) for risk, sources in state.risks.items()},
        commit,
        state.reauthenticated,
        sorted(state.step_up),
    )


_MATRIX = [
    (principal, scenario)
    for principal in ("system", "user", "armed", "stale", "intent", "reauth")
    for scenario in ("conflict", "bob", "self", "secret", "reauth")
]


@pytest.mark.parametrize(("principal", "scenario"), _MATRIX)
async def test_a_split_call_matches_the_local_one(
    core: CoreFacade, principal: str, scenario: str
) -> None:
    """The parity matrix: the same scenario local vs split is identical."""
    local = await _observe(core, "local", principal, scenario)
    split = await _observe(core, "split", principal, scenario)
    assert split == local


async def test_risks_of_a_failing_command_mark_the_request_local_and_split(
    core: CoreFacade,
) -> None:
    """(G-SENS-04, error variant) an untagged wrapper's nested money command
    commits, then the wrapper raises 409 and the caller goes on: the request
    keeps the money risk and owes its audit row, locally and after a split."""
    for mode in ("local", "split"):
        outcome, risks, commit, *_ = await _observe(core, mode, "user", "conflict")
        assert outcome == ("error", 409), mode
        assert risks == {"money": [f"command:{MONEY}"]}, mode
        assert commit == [({"money": [f"command:{MONEY}"]}, ALICE)], mode


@pytest.mark.parametrize("principal", ["user", "intent"])
async def test_the_end_user_crosses_every_hop(core: CoreFacade, principal: str) -> None:
    """(G-BILLING-20) billing's personal-account rule sees the end user after
    two RPC hops, with or without a commit-time re-check context: Alice
    charging Bob's personal account is 403 local and split; her own is 77;
    a system call (no end user) is unchanged."""
    for mode in ("local", "split"):
        assert (await _observe(core, mode, principal, "bob"))[0] == ("error", 403)
        assert (await _observe(core, mode, principal, "self"))[0] == ("ok", 77)
        assert (await _observe(core, mode, "system", "bob")) == ("ok", 77)


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        ("user", ("ok", True)),
        ("armed", ("ok", True)),
        ("stale", ("error", 401)),
        ("intent", ("error", 401)),
        ("reauth", ("ok", True)),
    ],
)
async def test_step_up_is_enforced_alike_local_and_split(
    core: CoreFacade, principal: str, expected: Any
) -> None:
    """(G-SENS-03) a remote command running a security effect enforces the
    step-up on the caller's auth recency exactly as locally: stale auth is
    401 (with no re-check context too), fresh auth (with a re-check context
    too) or a re-authentication in the request passes."""
    for mode in ("local", "split"):
        assert (await _observe(core, mode, principal, "secret"))[0] == expected, mode


async def test_a_reauthentication_in_a_remote_command_reaches_the_request(
    core: CoreFacade,
) -> None:
    """The reverse direction: a provider that re-authenticates the user
    (identity's current-password check) satisfies the request's later
    step-ups, as the same command run locally does."""
    for mode in ("local", "split"):
        observed = await _observe(core, mode, "stale", "reauth")
        assert observed[0] == ("ok", True) and observed[3] is True, mode


# --------------------------------------------------------------------------- #
# Intent windows after a transport failure (G-UNIT-02 / G-BILLING-19)
# --------------------------------------------------------------------------- #
def _timed_proxy(
    core: CoreFacade,
    base_url: str,
    client: Any,
    service: str = GATE,
    timeout: float = 0.3,
) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name=service,
        binding={
            "base_url": base_url,
            "service_id": rpc_identity.service_id(core.settings),
            "timeout_seconds": timeout,
        },
        default_timeout_seconds=timeout,
        client=client,
        settings=core.settings,
    )


async def test_no_window_opens_while_a_timed_out_remote_command_may_run(
    core: CoreFacade,
) -> None:
    """Real signed RPC over loopback HTTP: the client times out, the
    provider's command keeps running, the retry is refused (in progress) and
    the caller catches the failure. The intent window must not open beside
    the still-running provider unit: it is refused, 503, while that call is
    in doubt (fail closed; the client retries the request with its key)."""
    release = Gate._release = asyncio.Event()
    async with served_rpc(core) as base_url, httpx.AsyncClient() as client:
        try:
            proxy = _timed_proxy(core, base_url, client)
            scope = RequestScope(core=None, policy=None)
            scope.intent = IntentState()
            opened: list[bool] = []
            with bind_request_scope(scope):
                with pytest.raises(HTTPException):
                    await proxy.run(operation_key="in-doubt")
                assert Gate._running  # the provider unit is still active
                with pytest.raises(HTTPException) as refused:
                    async with external_call("psp.create"):
                        opened.append(Gate._running)
            assert opened == []
            assert refused.value.status_code == 503
            assert "SANDLOOM-R008" in str(refused.value.detail)
        finally:
            release.set()
            for _ in range(200):
                if not Gate._running:
                    break
                await asyncio.sleep(0.01)


async def test_an_answered_remote_failure_leaves_the_window_open(
    core: CoreFacade,
) -> None:
    """A command the provider ANSWERED (even with an error) ran to its end:
    the window opens as after a local call."""
    async with served_rpc(core) as base_url, httpx.AsyncClient() as client:
        proxy = _timed_proxy(core, base_url, client)
        scope = RequestScope(core=None, policy=None)
        scope.intent = IntentState()
        opened = False
        with bind_request_scope(scope):
            with pytest.raises(ContractCallError):
                await proxy.refuse(operation_key="answered")
            async with external_call("psp.create"):
                opened = True
        assert opened


async def _until_gate_idle() -> None:
    for _ in range(200):
        if not Gate._running:
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("hops", [1, 2])
async def test_a_nested_remote_timeout_keeps_the_root_in_doubt(
    core: CoreFacade, hops: int
) -> None:
    """(G-UNIT-02 / G-BILLING-19, any number of hops) root -> wrapper (RPC)
    [-> relay (RPC)] -> gate command (RPC) that blocks: the wrapper's call
    times out, the wrapper swallows the failure and answers OK. The gate's
    provider unit is still running, so the root's intent windows must refuse
    exactly as if the root had made the timed-out call itself: the provider
    reports the uncertainty back (fx) and every caller merges it.

    The uncertainty is not kept with the wrapper's dedupe record: once the
    gate finished, the client's retry (a replay of the wrapper) opens its
    window, as the monolith's replay does (never a 503 until expiry)."""
    release = Gate._release = asyncio.Event()
    async with served_rpc(core) as base_url, httpx.AsyncClient() as client:
        try:
            _VIA[GATE] = _timed_proxy(core, base_url, client)
            _VIA[WRAP] = _timed_proxy(core, base_url, client, WRAP, 10.0)
            root = _timed_proxy(core, base_url, client, WRAP, 10.0)
            scope = RequestScope(core=None, policy=None)
            scope.intent = IntentState()
            opened: list[bool] = []
            call = root.gate_quietly if hops == 1 else root.relay_gate
            key = f"doubt-{uuid4()}"
            with bind_request_scope(scope):
                assert await call(operation_key=key) == 0
                assert Gate._running  # the gate's provider unit still runs
                with pytest.raises(HTTPException) as refused:
                    async with external_call("psp.create"):
                        opened.append(Gate._running)
            assert opened == []
            assert refused.value.status_code == 503
            assert GATE in str(refused.value.detail)
        finally:
            release.set()
            await _until_gate_idle()
        retry = RequestScope(core=None, policy=None)
        retry.intent = IntentState()
        with bind_request_scope(retry):
            assert await call(operation_key=key) == 0  # replayed
            async with external_call("psp.create"):
                opened.append(Gate._running)
        assert opened == [False]


async def test_a_local_wrapper_catching_a_remote_timeout_leaves_the_request_in_doubt(
    core: CoreFacade,
) -> None:
    """The same chain with a LOCAL wrapper: the nested proxy's timeout is
    recorded on the request's own intent state, caught or not."""
    release = Gate._release = asyncio.Event()
    async with served_rpc(core) as base_url, httpx.AsyncClient() as client:
        try:
            _VIA[GATE] = _timed_proxy(core, base_url, client)
            wrap = core.get_service(WRAP)
            scope = RequestScope(core=None, policy=None)
            scope.intent = IntentState()
            with bind_request_scope(scope):
                assert await wrap.gate_quietly(operation_key=f"doubt-{uuid4()}") == 0
                assert Gate._running
                with pytest.raises(HTTPException) as refused:
                    async with external_call("psp.create"):
                        pass
            assert refused.value.status_code == 503
        finally:
            release.set()
            await _until_gate_idle()


async def test_an_oversized_answer_keeps_the_nested_doubt(core: CoreFacade) -> None:
    """(G-UNIT-02 / G-BILLING-19, round 5) root -> wrapper (RPC) -> gate (RPC)
    that blocks: the wrapper swallows its gate timeout and answers a result
    larger than the root's PRODUCTION bounded client accepts. The root never
    read that answer's effects (``fx.id`` carried the gate's doubt), so the
    call counts as in doubt: the root catching the 502 cannot open an intent
    window beside the still-running gate unit."""
    release = Gate._release = asyncio.Event()
    async with (
        served_rpc(core) as base_url,
        httpx.AsyncClient() as client,
        HttpxRpcClient(max_response_bytes=_CLIENT_LIMIT) as bounded,
    ):
        try:
            _VIA[GATE] = _timed_proxy(core, base_url, client)
            root = _timed_proxy(core, base_url, bounded, WRAP, 10.0)
            scope = RequestScope(core=None, policy=None)
            scope.intent = IntentState()
            opened: list[bool] = []
            with bind_request_scope(scope):
                with pytest.raises(RemoteServiceBadResponse):
                    await root.gate_bloated(operation_key=f"big-{uuid4()}")
                assert Gate._running  # the gate's provider unit still runs
                with pytest.raises(HTTPException) as refused:
                    async with external_call("psp.create"):
                        opened.append(Gate._running)
            assert opened == []
            assert refused.value.status_code == 503
            assert WRAP in str(refused.value.detail)
        finally:
            release.set()
            await _until_gate_idle()


class _Tamper(httpx.AsyncBaseTransport):
    """The provider's real answer replaced by ``(status, body)`` and bound to
    its request again with the provider's own pair key, so the caller
    authenticates it: what a provider answering something else than its
    envelope looks like on the wire. ``unkeyed``: bound by nonce only (the
    provider refusing a request it could not authenticate)."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        cfg: Any,
        answer: tuple[int, bytes],
        *,
        unkeyed: bool = False,
    ) -> None:
        self.inner, self.cfg, self.answer, self.unkeyed = inner, cfg, answer, unkeyed

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        await response.aread()
        if request.method != "POST":
            return response
        status_code, body = self.answer
        own = rpc_binding.own_dh_key(self.cfg)
        key = (
            None
            if self.unkeyed or own is None
            else rpc_binding.provider_key(self.cfg, (own.service_id, own.kid))
        )
        bound = rpc_binding.response_headers(
            nonce=request.headers[rpc_binding.NONCE_HEADER],
            key=key,
            req_auth=rpc_binding.request_auth(request.headers),
            status_code=status_code,
            body=body,
        )
        return httpx.Response(status_code, headers=bound, content=body)


def _envelope(content: dict[str, Any]) -> bytes:
    return rpc_wire._pack(content)


@pytest.mark.parametrize(
    ("answer", "unkeyed", "raised"),
    [
        # authenticated, but not an envelope: no effects were merged
        ((200, b"not msgpack"), False, RemoteServiceBadResponse),
        ((500, b"provider crashed"), False, HTTPException),
        ((200, _envelope({"result": 1})), False, RemoteServiceBadResponse),
        # an envelope whose effects are malformed: they cannot be merged
        ((200, _envelope({"ok": True, "result": 1, "fx": [1]})), False, None),
        ((200, _envelope({"ok": True, "result": 1, "fx": {"id": "x"}})), False, None),
        # round 6: a malformed ENTRY of any field is not merged either
        *(
            ((200, _envelope({"ok": True, "result": 1, "fx": fx})), False, None)
            for fx in (
                {"id": [None]},
                {"id": [""]},
                {"id": ["x" * 257]},
                {"id": [f"svc{i}" for i in range(17)]},
                {"rk": [1]},
                {"rk": {"money": "x"}},
                {"rk": {"money": [None]}},
                {"rk": {"money": [""]}},
                {"rk": {"money": ["x" * 257]}},
                {"rk": {"money": [f"s{i}" for i in range(65)]}},
                {"rk": {"no-such-risk": ["x"]}},
                {"sr": "x"},
                {"sr": [None]},
                {"sr": ["x" * 257]},
                {"sr": [f"u{i}" for i in range(17)]},
                {"ra": "yes"},
                {"ra": 2},
                # round 7: exactly the int 1 (a float or a bool equals it)
                {"ra": 1.0},
                {"ra": True},
                {"su": "x"},
                {"su": ["x"]},
                {"su": [0]},
                {"su": [True]},
                {"su": list(range(1, 18))},
                {"zz": 1},
            )
        ),
        # an unauthenticated refusal: nothing in it is the provider's word
        ((401, _envelope({"ok": False, "error": {}})), True, HTTPException),
    ],
)
async def test_an_answer_whose_effects_were_not_merged_is_in_doubt(
    core: CoreFacade,
    answer: tuple[int, bytes],
    unkeyed: bool,
    raised: type[Exception] | None,
) -> None:
    """(G-UNIT-02, round 5) "answered" means "authenticated effects merged":
    every answer whose ``fx`` the caller did not merge (no envelope, a
    malformed one, an unauthenticated refusal) leaves the call in doubt, as
    a timeout does; a well-formed envelope does not."""
    transport = _Tamper(
        httpx.ASGITransport(app=contract_testing._rpc_app(core)),
        core.settings,
        answer,
        unkeyed=unkeyed,
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://sandloom-conformance"
    ) as tampered:
        proxy = rpc_proxy_for(core, GATE, tampered)
        scope = RequestScope(core=None, policy=None)
        scope.intent = IntentState()
        with bind_request_scope(scope):
            if raised is None:
                with pytest.raises(RemoteServiceBadResponse):
                    await proxy.run(operation_key=f"t-{uuid4()}")
            else:
                with pytest.raises(raised):
                    await proxy.run(operation_key=f"t-{uuid4()}")
        # the call itself, after the well-formed ids it reported (fail closed)
        assert scope.intent.in_doubt[-1] == GATE
        assert set(scope.intent.in_doubt[:-1]) <= {f"svc{i}" for i in range(16)}


async def test_a_well_formed_answer_settles_the_call(core: CoreFacade) -> None:
    """The control of the test above: the same tampering transport passing a
    well-formed envelope (success or error, with or without ``fx``) leaves
    nothing in doubt."""
    for body in (
        {"ok": True, "result": 1},
        {"ok": True, "result": 1, "fx": {}},
        {
            "ok": True,
            "result": 1,
            "fx": {"rk": {"money": ["a"]}, "sr": ["u"], "ra": 1, "su": [60]},
        },
        {"ok": False, "error": {"code": "conflict", "status": 409}},
    ):
        status_code = 200 if body["ok"] else 409
        transport = _Tamper(
            httpx.ASGITransport(app=contract_testing._rpc_app(core)),
            core.settings,
            (status_code, _envelope(body)),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sandloom-conformance"
        ) as tampered:
            proxy = rpc_proxy_for(core, GATE, tampered)
            scope = RequestScope(core=None, policy=None)
            scope.intent = IntentState()
            with bind_request_scope(scope):
                try:
                    await proxy.run(operation_key=f"t-{uuid4()}")
                except ContractCallError:
                    pass
            assert scope.intent.in_doubt == [], body


# --------------------------------------------------------------------------- #
# A replayed command marks its caller like its first execution (G-SENS-04)
# --------------------------------------------------------------------------- #
class _LoseFirstAnswer(httpx.AsyncBaseTransport):
    """The provider runs the first ``method`` call to its end, then its
    answer is lost on the way back (the proxy retries the call)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, method: str) -> None:
        self.inner, self.method, self.lost = inner, method, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if (
            not self.lost
            and request.method == "POST"
            and request.url.path.endswith(f"/{self.method}")
        ):
            self.lost += 1
            await response.aread()
            await response.aclose()
            raise httpx.ReadError("the answer was lost", request=request)
        return response


def _risks(scope: RequestScope) -> Any:
    state = scope.risk
    if state is None:
        return None
    return (
        {risk: sorted(sources) for risk, sources in state.risks.items()},
        state.elsewhere,
    )


async def test_a_replayed_command_marks_the_request_like_its_first_execution(
    core: CoreFacade,
) -> None:
    """(G-SENS-04, replay) an untagged command's body runs a money command;
    a replay of it (the body skipped) marks the caller with the same risk
    and audit obligation: locally (the same caller's next request) and over
    RPC (the first answer is lost, the proxy's automatic retry replays)."""
    expected = ({"money": [f"command:{MONEY}"]}, True)
    key = f"replay-{uuid4()}"
    async with _deployed(core, "local"):
        for _ in range(2):
            scope = _scope("user")
            assert scope is not None
            with bind_request_scope(scope):
                assert await _VIA[WRAP].spend_quietly(operation_key=key) == 3
            assert _risks(scope) == expected
    transport = _LoseFirstAnswer(
        httpx.ASGITransport(app=contract_testing._rpc_app(core)), "spend_quietly"
    )
    async with (
        _deployed(core, "split"),
        httpx.AsyncClient(
            transport=transport, base_url="http://sandloom-conformance"
        ) as lossy,
    ):
        root = rpc_proxy_for(core, WRAP, lossy)
        scope = _scope("user")
        assert scope is not None
        with bind_request_scope(scope):
            assert await root.spend_quietly(operation_key=f"lost-{uuid4()}") == 3
        assert transport.lost == 1
        assert _risks(scope) == expected


@pytest.mark.parametrize("tagged", [True, False])
async def test_a_replay_marks_the_request_with_its_on_commit_callbacks(
    core: CoreFacade, tagged: bool
) -> None:
    """(G-SENS-04, replay, round 5; the cache dedupe record) an untagged
    command's on_commit callback runs a keyed money effect after its commit:
    the first execution marks its caller with money and the audit
    obligation, and so does its replay, locally (the same caller's next
    request) and over RPC (the first answer is lost, the proxy's automatic
    retry replays). The record is written after the callbacks ran."""
    expected = ({"money": [SETTLE]}, True)
    key = f"after-{uuid4()}"
    async with _deployed(core, "local"):
        for _ in range(2):
            scope = _scope("user")
            assert scope is not None
            with bind_request_scope(scope):
                assert await _VIA[WRAP].settle_after_commit(key, tagged) == 5
            assert _risks(scope) == expected
    transport = _LoseFirstAnswer(
        httpx.ASGITransport(app=contract_testing._rpc_app(core)),
        "settle_after_commit",
    )
    async with (
        _deployed(core, "split"),
        httpx.AsyncClient(
            transport=transport, base_url="http://sandloom-conformance"
        ) as lossy,
    ):
        root = rpc_proxy_for(core, WRAP, lossy)
        scope = _scope("user")
        assert scope is not None
        with bind_request_scope(scope):
            assert await root.settle_after_commit(f"lost-{uuid4()}", tagged) == 5
        assert transport.lost == 1
        assert _risks(scope) == expected


async def _cancel(task: asyncio.Task[Any], *, level: bool) -> None:
    """Cancel ``task`` like a request timeout: once (asyncio), or again at
    every await until it ends (anyio's level-triggered cancel scopes)."""
    task.cancel()
    while level and not task.done():
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("cancel", ["once", "level"])
async def test_a_cancelled_on_commit_callback_keeps_its_effects_on_the_cache_record(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch, cancel: str
) -> None:
    """(G-SENS-04, round 6; the cache dedupe record) an untagged command's
    on_commit closure completes a money effect, then hangs and its request
    is cancelled (a timeout). The cache record is written on every exit
    after the COMMIT, shielded from the cancellation (its store write
    suspends here, as a real one does), with what the callbacks left: the
    same caller's retry replays it marked with money and the audit
    obligation, and never re-executes the body."""
    real_cas_set = idempotency_reservation._store_cas_set

    async def suspending_cas_set(*args: Any) -> bool:
        await asyncio.sleep(0)
        return await real_cas_set(*args)

    monkeypatch.setattr(idempotency_reservation, "_store_cas_set", suspending_cas_set)
    expected = ({"money": [SETTLE]}, True)
    key = f"hang-{uuid4()}"
    async with _deployed(core, "local"):
        first = _scope("user")
        assert first is not None

        async def call() -> int:
            with bind_request_scope(first):
                return int(await _VIA[WRAP].settle_then_hang(operation_key=key))

        task = asyncio.create_task(call())
        for _ in range(500):
            if Wrap._paid is not None and Wrap._paid.is_set():
                break
            await asyncio.sleep(0.01)
        assert Wrap._paid is not None and Wrap._paid.is_set()
        await _cancel(task, level=cancel == "level")
        assert _risks(first) == expected  # the live request had money
        scope = _scope("user")
        assert scope is not None
        for _ in range(500):  # the shielded record write outlives its caller
            try:
                with bind_request_scope(scope):
                    assert await _VIA[WRAP].settle_then_hang(operation_key=key) == 5
                break
            except ContractCallInProgress:
                await asyncio.sleep(0.01)
        assert Wrap._hung == [key]  # replayed, never re-executed
        assert _risks(scope) == expected
