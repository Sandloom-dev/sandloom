"""A contract call has IDENTICAL observable semantics locally and over RPC.

Every test runs the same provider through ``core.get_service`` (in-process)
and through a generated RPC proxy talking to the same core over an in-process
ASGI transport (signed context, describe endpoint, wire codec), using the
contract conformance harness (:mod:`sandloom.core.contract_testing`).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core import contract_wire, rpc
from sandloom.core.contract_testing import (
    UNIQUE,
    ContractCase,
    assert_contract_conformance,
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_request_headers,
    rpc_settings,
)
from sandloom.core.contract_wire import ContractTypeError
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.module_api import (
    ContractArgumentError,
    ContractCallError,
    ContractResultNotReplayable,
    contract_method,
    contract_read_scope,
    current_contract_read_scope,
)
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc import envelope as rpc_envelope
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.testing import InMemoryCache

#: This process's RPC identity: every RPC test calls itself as ``svc``.
_SVC = "svc"


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Point(BaseModel):
    x: int
    y: Decimal
    tags: tuple[str, ...] = ()
    color: Color = Color.RED


class Denied(Exception):
    http_status = 402
    code = "payment_required"
    detail = {"reason": "no credits", "needed": 3}


class Demo:
    _calls: list[str] = []
    _current = 0

    @classmethod
    @contract_method
    async def changing(cls) -> list[int]:
        return [cls._current]

    @classmethod
    @contract_method(command=True, idempotency_key="key")
    async def change(cls, key: str) -> int:
        cls._current += 1
        return cls._current

    @classmethod
    @contract_method
    async def echo_point(cls, p: Point) -> Point:
        return p

    @classmethod
    @contract_method
    async def maybe_point(cls, p: Point | None = None) -> Point | None:
        return p

    @classmethod
    @contract_method
    async def points(cls, by_name: dict[str, Point]) -> dict[str, Point]:
        return by_name

    @classmethod
    @contract_method
    async def color(cls, c: Color) -> Color:
        return c

    @classmethod
    @contract_method
    async def counts(cls, ids: list[UUID]) -> dict[UUID, int]:
        return {i: n for n, i in enumerate(ids)}

    @classmethod
    @contract_method
    async def money(cls) -> Decimal:
        return Decimal("10.10")

    @classmethod
    @contract_method
    async def anything(cls, tag: UUID) -> dict[str, Any]:
        return {
            "d": Decimal("0.1"),
            "u": tag,
            "t": (1, 2),
            "s": {3, 1, 2},
            "m": Point(x=1, y=Decimal("2.5")),
            "e": Color.BLUE,
        }

    @classmethod
    @contract_method
    async def pair(cls) -> tuple[int, str]:
        return (1, "a")

    @classmethod
    @contract_method
    async def bag(cls, items: set[int]) -> frozenset[int]:
        return frozenset(items)

    @classmethod
    @contract_method
    async def kwonly(cls, a: int, *, b: int = 0) -> int:
        return a + b

    @classmethod
    @contract_method
    async def posonly(cls, a: int, /, b: int = 0) -> int:
        return a - b

    @classmethod
    @contract_method
    async def denied(cls) -> int:
        raise HTTPException(status_code=403, detail={"reason": "foreign org"})

    @classmethod
    @contract_method
    async def throttled(cls) -> int:
        raise HTTPException(
            status_code=429, detail="slow down", headers={"Retry-After": "7"}
        )

    @classmethod
    @contract_method
    async def protocol_error(cls) -> int:
        raise Denied()

    @classmethod
    @contract_method
    async def crash(cls) -> int:
        raise ValueError("provider bug")

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def paint(cls, c: Color, operation_key: str) -> Color:
        cls._calls.append(operation_key)
        return c

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def huge(cls, operation_key: str) -> str:
        cls._calls.append(operation_key)
        return "x" * (300 * 1024)


class _RecordingCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[str, int | None] = {}

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self.ttls[key] = ttl_seconds
        return await super().add(key, value, ttl_seconds=ttl_seconds)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self.ttls[key] = ttl_seconds
        await super().set(key, value, ttl_seconds=ttl_seconds)

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self.ttls[key] = ttl_seconds
        if self.store.get(key) != expected:
            return False
        self.store[key] = value
        return True


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    def __init__(self) -> None:
        self.session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def mem(monkeypatch: pytest.MonkeyPatch) -> _RecordingCache:
    cache = _RecordingCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": cache)
    return cache


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch, mem: _RecordingCache) -> Iterator[CoreFacade]:
    from sandloom.core.config.settings import settings

    allowed = {_SVC: ["demo.parity.v1", "nobody.here.v1"]}
    for name, value in rpc_settings(_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service("demo.parity.v1", Demo)
    Demo._calls.clear()
    yield facade


POINT = Point(x=1, y=Decimal("2.50"), tags=("a", "b"), color=Color.BLUE)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("memo", [False, True])
async def test_external_changes_and_command_invalidation_have_same_read_policy(
    core: CoreFacade,
    remote: bool,
    memo: bool,
) -> None:
    Demo._current = 0
    rpc_proxy._remote_contracts.clear()
    async with in_process_rpc_client(core) as client:

        def service() -> Any:
            return (
                rpc_proxy_for(core, "demo.parity.v1", client)
                if remote
                else core.get_service("demo.parity.v1")
            )

        async with contract_read_scope(memo=memo):
            first = await service().changing()
            first.append(99)  # a caller must never mutate the memo's copy
            Demo._current = 1  # another request committed a change
            assert await service().changing() == ([0] if memo else [1])
            scope = current_contract_read_scope()
            assert scope is not None
            scope.invalidate()
            assert await service().changing() == [1]
            assert await service().change(key=uuid4().hex) == 2
            assert await service().changing() == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["asgi", "aiohttp", "httpx"])
async def test_local_and_rpc_results_and_errors_are_identical(
    core: CoreFacade, transport: str
) -> None:
    # asgi: in-process; aiohttp / httpx: the production RPC client of
    # that RPC_TRANSPORT over a real loopback socket to uvicorn.
    ids = [uuid4(), uuid4()]
    await assert_contract_conformance(
        core,
        "demo.parity.v1",
        [
            ContractCase("echo_point", (POINT,)),
            # A dict where a model is expected validates into the model.
            ContractCase("echo_point", ({"x": 2, "y": "3.10"},)),
            ContractCase("maybe_point", (), {"p": POINT}),
            ContractCase("maybe_point"),
            ContractCase("points", ({"a": POINT},)),
            ContractCase("color", (Color.RED,)),
            ContractCase("color", ("blue",)),
            ContractCase("counts", (ids,)),
            ContractCase("money"),
            ContractCase("anything", (ids[0],)),
            ContractCase("pair"),
            ContractCase("bag", ({3, 1, 2},)),
            ContractCase("kwonly", (1,), {"b": 2}),
            ContractCase("posonly", (5, 2)),
            # Binding rules hold remotely exactly like a Python call: 422.
            ContractCase("kwonly", (1, 2), label="keyword-only passed positionally"),
            ContractCase("posonly", (), {"a": 5}, label="positional-only by keyword"),
            ContractCase("kwonly", (1,), {"bb": 2}, label="typo kwarg"),
            ContractCase("echo_point", ({"x": "nope", "y": 1},), label="bad model"),
            ContractCase("color", ("green",), label="bad enum"),
            # Domain errors: same status/detail/headers/code both ways.
            ContractCase("denied"),
            ContractCase("throttled"),
            ContractCase("protocol_error"),
            ContractCase("crash"),
            ContractCase("paint", (Color.BLUE, UNIQUE)),
        ],
        transport=transport,
    )


@pytest.mark.asyncio
async def test_rpc_type_fidelity(core: CoreFacade) -> None:
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        point = await remote.echo_point(POINT)
        assert isinstance(point, Point) and point == POINT
        assert point.tags == ("a", "b") and point.color is Color.BLUE
        assert await remote.color("red") is Color.RED
        money = await remote.money()
        assert isinstance(money, Decimal) and str(money) == "10.10"
        uid = uuid4()
        anything = await remote.anything(uid)
        assert anything["d"] == Decimal("0.1") and isinstance(anything["d"], Decimal)
        assert anything["u"] == uid and anything["t"] == (1, 2)
        assert anything["s"] == {1, 2, 3}
        counts = await remote.counts([uid])
        assert counts == {uid: 0}
        assert await remote.pair() == (1, "a")
        assert await remote.bag({1, 2}) == frozenset({1, 2})
        with pytest.raises(ContractArgumentError):
            await remote.kwonly(1, bb=2)


@pytest.mark.asyncio
async def test_domain_403_over_rpc_is_not_a_503(core: CoreFacade) -> None:
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        with pytest.raises(ContractCallError) as remote_exc:
            await remote.denied()
    with pytest.raises(ContractCallError) as local_exc:
        await core.get_service("demo.parity.v1").denied()
    assert remote_exc.value.status_code == local_exc.value.status_code == 403
    remote_detail: Any = remote_exc.value.detail
    local_detail: Any = local_exc.value.detail
    assert remote_detail == local_detail == {"reason": "foreign org"}


@pytest.mark.asyncio
async def test_retry_after_header_survives_rpc(core: CoreFacade) -> None:
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        with pytest.raises(ContractCallError) as exc_info:
            await remote.throttled()
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "7"}


@pytest.mark.asyncio
async def test_reads_persist_no_idempotency_record(
    core: CoreFacade, mem: _RecordingCache
) -> None:
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        await remote.money()
        await remote.color("red")
        await remote.money()
    assert mem.store == {}


@pytest.mark.asyncio
async def test_command_dedupe_is_shared_bounded_and_size_capped(
    core: CoreFacade, mem: _RecordingCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core.settings, "contract_command_dedupe_ttl_seconds", 120)
    local = core.get_service("demo.parity.v1")
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        assert await remote.paint(Color.RED, "op-1") is Color.RED
        # Local replay of the remote command returns the Enum (codec), and
        # the method ran once.
        assert await local.paint(Color.RED, "op-1") is Color.RED
        assert Demo._calls == ["op-1"]
        # Only the contract runtime's record exists (no RPC-level record).
        assert all(key.startswith("idem:contract:") for key in mem.store)
        assert {mem.ttls[key] for key in mem.store} == {120}

        await local.huge("op-big")
        with pytest.raises(ContractResultNotReplayable):
            await local.huge("op-big")
        with pytest.raises(ContractResultNotReplayable):
            await remote.huge("op-big")
    assert Demo._calls == ["op-1", "op-big"]
    assert max(len(v) for v in mem.store.values()) < 64 * 1024


@pytest.mark.asyncio
async def test_unknown_method_at_provider_is_501(core: CoreFacade) -> None:
    async with in_process_rpc_client(core) as client:
        remote = rpc_proxy_for(core, "demo.parity.v1", client)
        with pytest.raises(ServiceUnavailableInDeployment) as exc_info:
            await remote.no_such_method()
        missing = rpc_proxy_for(core, "nobody.here.v1", client)
        with pytest.raises(ServiceUnavailableInDeployment):
            await missing.anything()
    assert exc_info.value.status_code == 501


@pytest.mark.asyncio
async def test_open_dev_mode_does_not_open_rpc(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings
    from sandloom.core.di import bind_tenant

    monkeypatch.setattr(settings, "internal_endpoints_open_in_dev", True)
    body = rpc_wire._pack({"args": []})
    async with in_process_rpc_client(core) as client:
        # An unauthenticated request is refused even in open dev mode (the
        # hidden 404: the endpoint's existence is not revealed).
        bare = await client.post(
            "/_sandloom/rpc/demo.parity.v1/money",
            content=body,
            headers={"Content-Type": "application/msgpack"},
        )
        # A keyed one is served; its tenant travels in the MACed context.
        with bind_tenant(uuid4()):
            headers = rpc_request_headers(settings, "demo.parity.v1/money", body)
            keyed = await client.post(
                "/_sandloom/rpc/demo.parity.v1/money", content=body, headers=headers
            )
    assert bare.status_code == 404, bare.content
    assert keyed.status_code == 200, keyed.content


def test_local_provider_exposes_only_contract_methods(core: CoreFacade) -> None:
    local = core.get_service("demo.parity.v1")
    assert "money" in dir(local)
    with pytest.raises(AttributeError):
        local._calls  # noqa: B018 - private members are never exposed
    with pytest.raises(AttributeError):
        local.nope  # noqa: B018


class _VarArgs:
    @classmethod
    @contract_method
    async def spread(cls, *items: int) -> int:  # pragma: no cover - never runs
        return 0


class _Untyped:
    @classmethod
    @contract_method
    async def untyped(cls, value):  # type: ignore[no-untyped-def]  # pragma: no cover
        return value


class _Plain:
    @classmethod
    async def helper(cls) -> int:  # pragma: no cover - never runs
        return 1


class _BadReturn:
    @classmethod
    @contract_method
    async def handle(cls) -> object:  # pragma: no cover - never runs
        return object()


class _Legacy:
    @classmethod
    async def charge(cls, amount: int, db: AsyncSession) -> int:  # pragma: no cover
        return amount


@pytest.mark.parametrize(
    ("impl", "fragment"),
    [
        (_VarArgs, "*args"),
        (_Untyped, "missing type annotation"),
        (_Plain, "neither a @contract_method nor private"),
        (_BadReturn, "not a contract wire type"),
        # A legacy session-taking provider is refused, not deprecated.
        (_Legacy, "neither a @contract_method"),
    ],
)
def test_non_portable_contracts_are_rejected_at_registration(
    impl: Any, fragment: str
) -> None:
    facade = CoreFacade(app=FastAPI())
    with pytest.raises(ContractTypeError, match=fragment.replace("*", r"\*")):
        facade.register_service("demo.bad.v1", impl)


def test_rpc_in_progress_maps_to_a_clear_unavailable() -> None:
    import httpx

    response = httpx.Response(
        503,
        content=rpc_wire._pack(
            {
                "ok": False,
                "error": {"status": 503, "code": "rpc_in_progress", "message": "x"},
            }
        ),
    )
    with pytest.raises(rpc.RemoteServiceUnavailable) as exc_info:
        rpc_envelope._decode_envelope("demo.parity.v1", response)
    assert exc_info.value.code == "rpc_in_progress"
    assert "still running" in str(exc_info.value.detail)


def test_set_digest_is_stable_across_hash_seeds() -> None:
    code = (
        "from sandloom.core.contract_wire import digest;"
        "print(digest({'items': {'alpha', 'beta', 'gamma', 'delta'}}))"
    )
    digests = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": seed, "PATH": ""},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2", "3")
    }
    assert len(digests) == 1


def test_wire_codec_round_trips_exactly() -> None:
    value = {"d": Decimal("1.100"), "t": (1, (2, 3)), "f": frozenset({"a"})}
    assert contract_wire.roundtrip(value) == value
    assert contract_wire.roundtrip(value)["d"].as_tuple() == Decimal("1.100").as_tuple()
