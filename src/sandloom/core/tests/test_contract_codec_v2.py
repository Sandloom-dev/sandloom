"""Wire codec v2 (annotation-driven, pydantic-core) + typed RPC calls.

* :class:`~sandloom.core.contract_wire.WirePlan` gives an in-process call EXACTLY
  what the tagged codec (v1) round trip gave, for every annotation shape —
  immutable exact-type values pass through untouched, fully typed ones go
  through pydantic-core, the rest keep the tagged tree;
* every provider speaks codecs 1 and 2: a proxy that has described the
  contract sends typed arguments and accepts a typed result for THAT
  contract hash (the provider answers typed only on a hash match).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import httpx
import msgpack
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from sandloom.core import contract_wire
from sandloom.core.contract_testing import rpc_request_headers, rpc_settings
from sandloom.core.contract_wire import (
    CODEC_TAGGED,
    CODEC_TYPED,
    is_fully_typed,
    wire_plan,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, rpc_router
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.testing import InMemoryCache

#: This process's RPC identity: the proxy calls the core itself as ``svc``.
_SVC = "svc"


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Item(BaseModel):
    id: UUID
    name: str
    at: datetime
    amount: Decimal
    tags: tuple[str, ...] = ()
    color: Color = Color.RED


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    n: int


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: int


class WithAny(BaseModel):
    meta: dict[str, Any]


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
U = UUID(int=42)
ITEM = Item(id=U, name="x", at=NOW, amount=Decimal("1.50"), tags=("a",))


# --------------------------------------------------------------------------- #
# WirePlan == the v1 round trip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("annotation", "typed"),
    [
        (int, True),
        (str | None, True),
        (UUID, True),
        (list[Item], True),
        (dict[UUID, int], True),
        (tuple[int, str], True),
        (set[int], True),
        (Literal["a", "b"], True),
        (Color, True),
        (Frozen, True),
        (Item | None, True),
        (Any, False),
        (list, False),
        (dict[str, Any], False),
        (WithAny, False),
        (Loose, False),
        (UUID | str, False),  # both dump to a JSON string
        (Item | Frozen, False),  # both dump to an object
        (Color | str, False),
    ],
)
def test_is_fully_typed(annotation: Any, typed: bool) -> None:
    assert is_fully_typed(annotation) is typed


CASES: list[tuple[Any, Any]] = [
    (int, 3),
    (int, True),
    (float, 3),
    (float, float("inf")),
    (str, "s"),
    (str | None, None),
    (UUID, U),
    (UUID, str(U)),
    (datetime, NOW),
    (date, date(2026, 1, 2)),
    (Decimal, Decimal("10.10")),
    (Color, Color.BLUE),
    (Color, "red"),
    (Literal["a", "b"], "a"),
    (Item, ITEM),
    (Item, ITEM.model_dump()),
    (list[Item], [ITEM, ITEM]),
    (dict[UUID, int], {U: 1}),
    (dict[str, Item], {"k": ITEM}),
    (tuple[int, str], (1, "a")),
    (tuple[int, ...], [1, 2]),
    (set[int], {3, 1}),
    (frozenset[int], frozenset({1})),
    (Frozen, Frozen(id=U, n=1)),
    (Item | None, None),
    (Any, {"u": U, "s": {1, 2}, "t": (1, 2)}),
    (dict[str, Any], {"d": Decimal("0.1"), "m": ITEM}),
    (UUID | str, U),
    (Annotated[int, Field(ge=0)], 5),
]


@pytest.mark.parametrize(("annotation", "value"), CASES)
def test_local_plan_equals_v1_roundtrip(annotation: Any, value: Any) -> None:
    expected = contract_wire.roundtrip(value, annotation)
    got = wire_plan(annotation).local(value)
    assert got == expected
    assert type(got) is type(expected)


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (str, 5),
        (UUID, "not-a-uuid"),
        (Item, {"id": "x"}),
        (Annotated[int, Field(ge=0)], -1),
    ],
)
def test_local_plan_rejects_like_v1(annotation: Any, value: Any) -> None:
    with pytest.raises(Exception) as v1:
        contract_wire.roundtrip(value, annotation)
    with pytest.raises(type(v1.value)):
        wire_plan(annotation).local(value)


def test_immutable_exact_values_pass_through_and_mutables_are_copied() -> None:
    frozen = Frozen(id=U, n=1)
    assert wire_plan(UUID).local(U) is U
    assert wire_plan(str | None).local("x") == "x"
    assert wire_plan(Frozen).local(frozen) is frozen
    items = [ITEM]
    copied = wire_plan(list[Item]).local(items)
    assert copied == items and copied is not items and copied[0] is not ITEM


def test_typed_wire_form_is_json_native_and_decodes_back() -> None:
    plan = wire_plan(list[Item])
    codec, wire = plan.to_wire([ITEM], typed=True)
    assert codec == CODEC_TYPED
    assert wire[0]["id"] == str(U)  # no $sandloom_type tag anywhere
    assert msgpack.unpackb(msgpack.packb(wire)) == wire
    assert plan.decode(wire, CODEC_TYPED) == [ITEM]
    # A value that does not exactly fit the annotation keeps the tagged codec.
    codec, wire = plan.to_wire([ITEM.model_dump()], typed=True)
    assert codec == CODEC_TAGGED
    assert plan.decode(wire, CODEC_TAGGED) == [ITEM]


# --------------------------------------------------------------------------- #
# RPC negotiation (new <-> new, new <-> old, old <-> new)
# --------------------------------------------------------------------------- #
class Catalog:
    @classmethod
    @contract_method
    async def items(cls, xs: list[Item], limit: int = 10) -> list[Item]:
        return xs[:limit]

    @classmethod
    @contract_method
    async def pair(cls, org: UUID, user: UUID) -> dict[UUID, str]:
        return {org: "org", user: "user"}

    @classmethod
    @contract_method
    async def loose(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data


class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    session: _FakeSession | None = None

    async def __aenter__(self) -> _FakeUoW:
        self.session = _FakeSession()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _FakeDatabase:
    def uow_factory(self) -> Any:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[CoreFacade]:
    from sandloom.core.config.settings import settings

    allowed = {_SVC: ["demo.codec.v1"]}
    for name, value in rpc_settings(_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service("demo.codec.v1", Catalog)
    rpc_proxy._remote_contracts.clear()
    yield facade
    rpc_proxy._remote_contracts.clear()


def _client(core: CoreFacade, seen: list[httpx.Request]) -> Any:
    app = FastAPI()
    app.state.core = core
    app.include_router(rpc_router(core))

    async def record(request: httpx.Request) -> None:
        seen.append(request)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://codec-peer",
        event_hooks={"request": [record]},
    )


def _proxy(client: httpx.AsyncClient) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name="demo.codec.v1",
        binding={"base_url": "http://codec-peer", "service_id": _SVC},
        default_timeout_seconds=10.0,
        client=client,
    )


def _posts(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.method == "POST"]


async def _exercise(proxy: RemoteServiceProxy) -> None:
    org, user = uuid4(), uuid4()
    for _ in range(2):  # 1st call teaches the proxy the contract (describe)
        assert await proxy.items([ITEM, ITEM], limit=1) == [ITEM]
        assert await proxy.pair(org, user) == {org: "org", user: "user"}
        assert await proxy.loose({"u": org, "s": {1}}) == {"u": org, "s": {1}}


@pytest.mark.asyncio
async def test_describe_document_negotiates_no_codecs(core: CoreFacade) -> None:
    from sandloom.core.config.settings import settings

    # Every provider speaks codecs 1 and 2: nothing to advertise.
    seen: list[httpx.Request] = []
    async with _client(core, seen) as client:
        resp = await client.get(
            "/_sandloom/rpc/demo.codec.v1",
            headers=rpc_request_headers(settings, "demo.codec.v1", http_method="GET"),
        )
    assert resp.status_code == 200, resp.content
    contract = rpc_wire._unpack(resp.content)["contract"]
    assert "codecs" not in contract


@pytest.mark.asyncio
async def test_new_client_new_server_uses_typed_codec(core: CoreFacade) -> None:
    seen: list[httpx.Request] = []
    async with _client(core, seen) as client:
        await _exercise(_proxy(client))
    posts = _posts(seen)
    bodies = [rpc_wire._unpack(r.content) for r in posts]
    # The very first call precedes the describe; then fully typed methods go
    # typed and the dict[str, Any] one keeps codec 1.
    assert [b.get("codec") for b in bodies] == [
        None,
        CODEC_TYPED,
        None,
        CODEC_TYPED,
        CODEC_TYPED,
        None,
    ]
    assert bodies[3]["args"][0][0]["id"] == str(ITEM.id)  # untagged
    assert rpc_wire._ACCEPT_CODEC_HEADER in posts[3].headers
    assert rpc_wire._ACCEPT_CODEC_HEADER not in posts[5].headers


@pytest.mark.asyncio
async def test_server_answers_typed_only_for_the_accepted_contract_hash(
    core: CoreFacade,
) -> None:
    from sandloom.core.config.settings import settings

    body = msgpack.packb({"args": [str(U), str(U)], "kwargs": {}, "codec": 2})
    target = "demo.codec.v1/pair"
    seen: list[httpx.Request] = []
    async with _client(core, seen) as client:
        schema_hash = core.get_service("demo.codec.v1").contract_schema.hash
        stale = await client.post(
            f"/_sandloom/rpc/{target}",
            content=body,
            headers={
                **rpc_request_headers(settings, target, body),
                rpc_wire._ACCEPT_CODEC_HEADER: "2;not-the-hash",
            },
        )
        fresh = await client.post(
            f"/_sandloom/rpc/{target}",
            content=body,
            headers={
                **rpc_request_headers(settings, target, body),
                rpc_wire._ACCEPT_CODEC_HEADER: f"2;{schema_hash}",
            },
        )
    assert stale.status_code == fresh.status_code == 200
    assert rpc_wire._CODEC_HEADER not in stale.headers
    assert rpc_wire._unpack(stale.content)["result"]["$sandloom_type"] == "map"
    assert fresh.headers[rpc_wire._CODEC_HEADER] == "2"
    assert rpc_wire._unpack(fresh.content)["result"] == {str(U): "user"}
