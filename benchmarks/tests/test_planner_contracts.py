# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Planned read contracts: ``@contract_method(planned=True)`` + ``contract_reads()``.

A read contract made only of declared operations runs WITHOUT a Unit of Work:
its reads lease from the provider's CONTRACT pool in a read-only scope
(general executor: one shared read-only transaction; optimized executor when
enabled). The S5 environment (FORCEd RLS, NOBYPASSRLS role, request pool of
ONE) proves:

* local == remote (in-process RPC, pair-MAC authenticated), for both executors,
  including tenant binding from the verified context, errors and
  ``ReadOnlyScopeError`` for a write attempt;
* the same answers as the identical contract written on ``contract_session()``;
* the reads use the contract pool: a caller holding the request pool's only
  connection still gets an answer (no pool deadlock);
* nothing leaks: the pooled connections stay clean.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from test_planner_executors import _clean
from test_s5_secure_read import (  # noqa: F401 - fixtures
    A_ID,
    B_ID,
    S5,
    A,
    B,
    admin,
    pg_proxy,
    s5,
)

from sandloom.core import rpc
from sandloom.core.contract_testing import (
    ContractCase,
    assert_contract_conformance,
    rpc_request_headers,
    rpc_settings,
)
from sandloom.core.di import publish_event
from sandloom.core.module_api import contract_method, contract_reads, contract_session
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.config.settings import settings
from sandloom.core.tenant import bind_tenant
from sandloom.core.unit_of_work import db_session
from benchsuite import common
from benchsuite.bench.api import ItemOut
from benchsuite.bench.operations import get_tenant_item, list_tenant_items
from benchsuite.bench.tenant_models import BenchTenantItem

pytestmark = pytest.mark.asyncio

_SEQ = itertools.count()


class PlannedItems:
    """The planned contract (declared operations only)."""

    @classmethod
    @contract_method(planned=True)
    async def get(cls, item_id: int) -> ItemOut | None:
        return await contract_reads().one(get_tenant_item, id=item_id)  # type: ignore[no-any-return]

    @classmethod
    @contract_method(planned=True)
    async def first_ids(cls, limit: int) -> list[int]:
        page = await contract_reads().page(list_tenant_items, limit=limit)
        return [item.id for item in page.items]

    @classmethod
    @contract_method(planned=True)
    async def two_pages(cls, cursor: str) -> list[int]:
        """Page 1, then the caller's (possibly bad) cursor: a mid-call failure."""
        reads = contract_reads()
        first = await reads.page(list_tenant_items, limit=2)
        second = await reads.page(list_tenant_items, limit=2, cursor=cursor)
        return [i.id for i in (*first.items, *second.items)]

    @classmethod
    @contract_method(planned=True)
    async def write(cls, item_id: int) -> bool:
        await publish_event(common.EVENT_CHANNEL, {"id": item_id, "name": "x"})
        return True

    @classmethod
    @contract_method(planned=True)
    async def session(cls) -> str:
        contract_session()  # a planned contract has no Unit of Work
        return "unreachable"


class UowItems:
    """The same contract on the provider's own Unit of Work (the baseline)."""

    @classmethod
    @contract_method
    async def get(cls, item_id: int) -> ItemOut | None:
        item = await contract_session().get(BenchTenantItem, item_id)
        return (
            None if item is None else ItemOut.model_validate(item, from_attributes=True)
        )

    @classmethod
    @contract_method
    async def first_ids(cls, limit: int) -> list[int]:
        return [
            i.id
            for i in (await contract_reads().page(list_tenant_items, limit=limit)).items
        ]


def _register(sandloom_core: Any) -> tuple[str, str]:
    n = next(_SEQ)
    planned, uow = f"bench.planned_items{n}.v1", f"bench.uow_items{n}.v1"
    sandloom_core.register_service(planned, PlannedItems)
    sandloom_core.register_service(uow, UowItems)
    return planned, uow


CASES = [
    ContractCase("get", (A_ID,), tenant=A, label="own row"),
    ContractCase("get", (B_ID,), tenant=A, label="other tenant's row"),
    ContractCase("get", (B_ID,), tenant=B, label="tenant B"),
    ContractCase("get", (A_ID,), label="no tenant -> fail closed"),
    ContractCase("get", ("x",), tenant=A, label="bad argument"),
    ContractCase("first_ids", (3,), tenant=B),
    ContractCase("first_ids", (100_000,), tenant=A, label="limit above the cap"),
]


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_planned_contract_conforms_local_vs_rpc(s5: S5, sandloom_core: Any) -> None:
    planned, _ = _register(sandloom_core)
    await assert_contract_conformance(
        sandloom_core,
        planned,
        [
            *CASES,
            ContractCase("write", (A_ID,), tenant=A, label="write refused"),
            ContractCase("session", (), tenant=A, label="no UoW"),
        ],
    )
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_planned_contract_answers_like_the_uow_contract(
    s5: S5, sandloom_core: Any
) -> None:
    planned_name, uow_name = _register(sandloom_core)
    planned, uow = sandloom_core.get_service(planned_name), sandloom_core.get_service(uow_name)
    for tenant, item in ((A, A_ID), (A, B_ID), (B, B_ID), (B, A_ID)):
        with bind_tenant(tenant):
            assert await planned.get(item) == await uow.get(item)
            assert await planned.first_ids(5) == await uow.first_ids(5)
    with bind_tenant(A), pytest.raises(Exception) as err:  # noqa: PT011
        await planned.write(A_ID)
    assert (
        "ReadOnlyScopeError" in type(err.value).__name__
        or "read-only" in str(err.value).lower()
    )
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_planned_contract_reads_use_the_contract_pool(
    s5: S5, sandloom_core: Any
) -> None:
    """The caller holds the request pool's ONLY connection (pool of one) and
    calls the contract: it must not wait for that connection."""
    planned_name, _ = _register(sandloom_core)
    planned = sandloom_core.get_service(planned_name)
    provider = sandloom_core.database("default")
    contract_adapter = provider.contract_execution_adapter()
    assert contract_adapter.engine is not provider.engine
    seen: list[str] = []

    async def record(phase: str) -> None:
        seen.append(phase)

    contract_adapter.checkpoint = record
    try:
        with bind_tenant(A):
            async with db_session() as db:
                await db.execute(text("SELECT 1"))  # holds the pooled connection
                got = await asyncio.wait_for(planned.get(A_ID), 5)
    finally:
        contract_adapter.checkpoint = None
    assert got is not None and got.id == A_ID
    assert "leased" in seen  # leased from the contract pool's adapter


# --------------------------------------------------------------------------- #
# Over RPC: transport retry, timeouts, mid-call failures, auth + tenant context
# --------------------------------------------------------------------------- #
#: The in-process provider's service id: the process settings are keyed as
#: it, calling itself (pair-MAC + keyed response binding).
SVC = "planner-svc"


class _Flaky(httpx.AsyncBaseTransport):
    """ASGI transport that fails the next POSTs with the given errors."""

    def __init__(self, app: Any, failures: list[Exception]) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.failures = failures
        self.posts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts += 1
            if self.failures:
                raise self.failures.pop(0)
        return await self._inner.handle_async_request(request)


def _remote(sandloom_core: Any, name: str, failures: list[Exception], caller: Any = None):
    """A proxy to ``name`` served in-process; ``caller`` = the caller's
    settings (default: the process settings, keyed as :data:`SVC`).

    Needs the ``rpc_env`` fixture: ``name`` is added to :data:`SVC`'s
    allowlist as a NEW dict (the parsed allowlist is cached by identity),
    and the fixture's monkeypatch restores the original at teardown.
    """
    allowed = dict(settings.rpc_allowed_calls)
    allowed[SVC] = sorted({*allowed.get(SVC, []), name})
    settings.rpc_allowed_calls = allowed
    app = FastAPI()
    app.state.core = sandloom_core
    app.include_router(rpc.rpc_router(sandloom_core))
    transport = _Flaky(app, failures)
    client = httpx.AsyncClient(transport=transport, base_url="http://sandloom-conformance")
    proxy = rpc.RemoteServiceProxy(
        service_name=name,
        binding={"base_url": "http://sandloom-conformance", "service_id": SVC},
        default_timeout_seconds=5,
        client=client,
        settings=caller,
    )
    return proxy, transport, client


@pytest.fixture
def rpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in rpc_settings(SVC, allowed_calls={SVC: []}).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_one_transport_failure_is_retried_once(
    s5: S5, sandloom_core: Any, rpc_env: None
) -> None:
    planned, _ = _register(sandloom_core)
    local = sandloom_core.get_service(planned)
    for failure in (httpx.ConnectError("down"), httpx.ReadTimeout("slow")):
        remote, transport, client = _remote(sandloom_core, planned, [failure])
        async with client:
            with bind_tenant(A):
                assert await remote.get(A_ID) == await local.get(A_ID)
        assert transport.posts == 2  # the failed attempt + ONE retry
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_repeated_failures_and_timeouts_are_503(
    s5: S5, sandloom_core: Any, rpc_env: None
) -> None:
    planned, _ = _register(sandloom_core)
    cases = [
        ([httpx.ReadTimeout("t"), httpx.ReadTimeout("t")], "rpc_timeout"),
        ([httpx.ConnectError("x"), httpx.ConnectError("x")], None),
    ]
    for failures, code in cases:
        remote, transport, client = _remote(sandloom_core, planned, failures)
        async with client:
            with bind_tenant(A), pytest.raises(rpc.RemoteServiceUnavailable) as err:
                await remote.get(A_ID)
        assert err.value.status_code == 503
        if code:
            assert getattr(err.value, "code", None) == code
        assert transport.posts == 2  # never more than one retry
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_mid_call_failure_is_the_same_error_locally_and_remotely(
    s5: S5, sandloom_core: Any, rpc_env: None
) -> None:
    """The provider fails AFTER a successful first read (a tampered cursor):
    the same 400 on both paths, and nothing is left open."""
    planned, _ = _register(sandloom_core)
    await assert_contract_conformance(
        sandloom_core,
        planned,
        [
            ContractCase("two_pages", ("garbage",), tenant=A, label="bad cursor"),
            ContractCase("two_pages", ("x.y",), tenant=B, label="bad cursor (B)"),
        ],
    )
    local = sandloom_core.get_service(planned)
    with bind_tenant(A), pytest.raises(Exception) as err:  # noqa: PT011
        await local.two_pages("garbage")
    assert getattr(err.value, "status_code", None) == 400
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan"], indirect=True)
async def test_auth_and_tenant_context_propagation(
    s5: S5, sandloom_core: Any, rpc_env: None
) -> None:
    planned, _ = _register(sandloom_core)
    # the tenant travels in the authenticated context: each side sees its rows
    remote, _t, client = _remote(sandloom_core, planned, [])
    async with client:
        with bind_tenant(B):
            assert await remote.get(B_ID) is not None
            assert await remote.get(A_ID) is None
    # a caller whose keys the provider does not know is refused (401 at the
    # provider, unkeyed), which the caller sees as an unavailable remote
    # (503, not retried), never data
    stranger = settings.model_copy(update=rpc_settings("stranger", peers=[SVC]))
    bad, transport, client = _remote(sandloom_core, planned, [], caller=stranger)
    async with client:
        with bind_tenant(A), pytest.raises(rpc.RemoteServiceUnavailable) as err:
            await bad.get(A_ID)
    assert err.value.status_code == 503
    assert transport.posts == 1
    # an authenticated request with a bare X-Tenant-Id (outside the
    # authenticated context) is refused, never trusted
    app = FastAPI()
    app.state.core = sandloom_core
    app.include_router(rpc.rpc_router(sandloom_core))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://p"
    ) as raw:
        body = b"\x80"
        headers = rpc_request_headers(settings, f"{planned}/get", body)
        resp = await raw.post(
            f"/_sandloom/rpc/{planned}/get",
            content=body,
            headers={**headers, "X-Tenant-Id": str(A)},
        )
    assert resp.status_code == 400
    assert rpc_wire._unpack(resp.content)["error"]["code"] == "rpc_unsigned_tenant"
    await _clean(s5)


@pytest.mark.parametrize("s5", ["plan", "planopt"], indirect=True)
async def test_a_remote_membership_style_read_is_not_in_the_callers_transaction(
    s5: S5, sandloom_core: Any, rpc_env: None
) -> None:
    """The caller's uncommitted write is invisible to the planned contract,
    locally AND remotely: it is a separate read (its own transaction on the
    provider's pool), never part of the caller's Unit of Work."""
    planned, _ = _register(sandloom_core)
    local = sandloom_core.get_service(planned)
    remote, _t, client = _remote(sandloom_core, planned, [])
    new_id = 10 * common.S5_ROWS_PER_TENANT + 3

    class _Undo(Exception):
        pass

    async with client:
        with bind_tenant(A), pytest.raises(_Undo):
            async with db_session() as db:
                db.add(BenchTenantItem(id=new_id, name="uncommitted", value=1))
                await db.flush()
                assert await local.get(new_id) is None
                assert await remote.get(new_id) is None
                raise _Undo
    await _clean(s5)
