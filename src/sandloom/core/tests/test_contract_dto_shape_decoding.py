"""A replacement provider's DTOs decode into the consumer's copy (KB-23, I-06).

``acme_orgs`` serves ``tenancy.membership.v1`` with its own ``MemberState``
(same fields, no ``sandloom_tenancy`` anywhere). The consumer's generated copy
is registered for the origin ``sandloom_tenancy...:MemberState``, which the
replacement's class matches by no name. On the untyped path
(``core.get_service(name)`` over RPC) the result used to be a plain ``dict``;
the copies a consumer's stubs name for the contract are now matched by wire
shape, so the result is the consumer's copy, as with the original provider.

Deliberately no ``from __future__ import annotations``: the stubs and
providers below are annotated with dynamically created classes.
"""

from collections.abc import Iterator
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from sandloom.core import contract_wire
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.descriptors.contract_refs import (
    ContractRef,
    contract_copies,
    dto_copy,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import RemoteServiceProxy, rpc_router
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.testing import InMemoryCache

_SVC = "svc"
_CONTRACT = "kb23orgs.membership.v1"
_OTHER = "kb23other.membership.v1"
_ORIGIN = "kb23_tenancy.schemas.membership:MemberState"
_ROLE_ORIGIN = "kb23_tenancy.schemas.membership:Role"


# --- the consumer: its generated copies and its stubs ------------------------
class Role(str, Enum):
    MEMBER = "member"
    ADMIN = "admin"


Role.__module__ = "kb23_billing._generated.contracts.tenancy_dto"
dto_copy(_ROLE_ORIGIN)(Role)


class MemberState(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    user_id: UUID
    role: Role | None
    version: int


MemberState.__module__ = "kb23_billing._generated.contracts.tenancy_dto"
dto_copy(_ORIGIN)(MemberState)


class Kb23OrgsMembershipV1(Protocol):
    async def get_member_state(
        self, organization_id: UUID, user_id: UUID
    ) -> MemberState: ...

    async def list_states(self, organization_id: UUID) -> list[MemberState]: ...

    async def role_of(self, organization_id: UUID, user_id: UUID) -> Role: ...


KB23_ORGS_MEMBERSHIP_V1: ContractRef[Kb23OrgsMembershipV1] = ContractRef(
    _CONTRACT, Kb23OrgsMembershipV1
)


# --- the replacement provider: its own DTOs, another module path --------------
class AcmeRole(str, Enum):
    MEMBER = "member"
    ADMIN = "admin"


class AcmeMemberState(BaseModel):
    """Acme's docstring: it is not part of the wire shape."""

    model_config = ConfigDict(frozen=True)

    organization_id: UUID
    user_id: UUID
    role: AcmeRole | None
    version: int


class AcmeWiderState(BaseModel):
    """A deviation: one more field than the consumer's copy."""

    organization_id: UUID
    user_id: UUID
    role: AcmeRole | None
    version: int
    display_name: str


for _cls in (AcmeRole, AcmeMemberState, AcmeWiderState):
    _cls.__module__ = "acme_orgs.schemas"
    _cls.__qualname__ = _cls.__name__.removeprefix("Acme")

_ORG, _USER = uuid4(), uuid4()


class AcmeOrgs:
    @classmethod
    @contract_method
    async def get_member_state(
        cls, organization_id: UUID, user_id: UUID
    ) -> AcmeMemberState:
        return AcmeMemberState(
            organization_id=organization_id,
            user_id=user_id,
            role=AcmeRole.ADMIN,
            version=7,
        )

    @classmethod
    @contract_method
    async def list_states(cls, organization_id: UUID) -> list[AcmeMemberState]:
        return [await cls.get_member_state(organization_id, _USER)]


class AcmeWider:
    @classmethod
    @contract_method
    async def get_member_state(
        cls, organization_id: UUID, user_id: UUID
    ) -> AcmeWiderState:
        return AcmeWiderState(
            organization_id=organization_id,
            user_id=user_id,
            role=None,
            version=1,
            display_name="Ada",
        )


# --- the wire: a provider app behind an in-process transport ----------------
class _FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _FakeUoW:
    session: _FakeSession | None = None

    async def __aenter__(self) -> "_FakeUoW":
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


def _provider(
    monkeypatch: pytest.MonkeyPatch, impl: Any, contract: str
) -> httpx.AsyncClient:
    from sandloom.core.config.settings import settings

    allowed = {_SVC: [contract]}
    for name, value in rpc_settings(_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    core = CoreFacade(app=FastAPI())
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core.register_service(contract, impl)
    app = FastAPI()
    app.state.core = core
    app.include_router(rpc_router(core))
    rpc_proxy._remote_contracts.clear()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://acme-peer"
    )


def _proxy(client: httpx.AsyncClient, contract: str) -> RemoteServiceProxy:
    return RemoteServiceProxy(
        service_name=contract,
        binding={"base_url": "http://acme-peer", "service_id": _SVC},
        default_timeout_seconds=10.0,
        client=client,
    )


async def _described(proxy: RemoteServiceProxy, *args: Any) -> Any:
    """The result of ``get_member_state`` after the proxy learned the contract
    (the first call precedes the describe)."""
    await proxy.get_member_state(*args)
    return await proxy.get_member_state(*args)


@pytest.fixture(autouse=True)
def _clean_remote_contracts() -> Iterator[None]:
    yield
    rpc_proxy._remote_contracts.clear()


# --- the PoC scenario (contracts PoC section 3.3) -----------------------------
@pytest.mark.asyncio
async def test_replacement_provider_result_decodes_into_the_consumers_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _provider(monkeypatch, AcmeOrgs, _CONTRACT) as client:
        proxy = _proxy(client, _CONTRACT)
        state = await _described(proxy, _ORG, _USER)
        states = await proxy.list_states(_ORG)
    assert type(state) is MemberState
    assert state == MemberState(
        organization_id=_ORG, user_id=_USER, role=Role.ADMIN, version=7
    )
    assert type(state.role) is Role
    assert [type(s) for s in states] == [MemberState]


@pytest.mark.asyncio
async def test_a_deviating_replacement_stays_plain_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a guess: one more field is another shape, so no copy fits."""
    async with _provider(monkeypatch, AcmeWider, _CONTRACT) as client:
        state = await _described(_proxy(client, _CONTRACT), _ORG, _USER)
    assert type(state) is dict
    assert state["display_name"] == "Ada"


@pytest.mark.asyncio
async def test_a_copy_is_matched_within_its_contract_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _provider(monkeypatch, AcmeOrgs, _OTHER) as client:
        state = await _described(_proxy(client, _OTHER), _ORG, _USER)
    assert type(state) is dict


# --- the matching itself -------------------------------------------------------
def _descriptor(cls: type) -> dict[str, Any]:
    return contract_wire.type_descriptor(cls)


def test_shape_ignores_class_names_and_docstrings_but_not_fields() -> None:
    resolve = contract_wire.annotation_from_descriptor
    assert resolve(_descriptor(AcmeMemberState), _CONTRACT) is MemberState
    assert resolve(_descriptor(AcmeWiderState), _CONTRACT) == dict[str, Any]
    # no contract: the origin is the only key, as before
    assert resolve(_descriptor(AcmeMemberState)) == dict[str, Any]


def test_enum_and_containers_resolve_by_shape() -> None:
    resolve = contract_wire.annotation_from_descriptor
    assert resolve(_descriptor(AcmeRole), _CONTRACT) is Role
    listed = {"k": "list", "item": _descriptor(AcmeMemberState)}
    assert resolve(listed, _CONTRACT) == list[MemberState]


def test_the_origin_still_wins_over_the_shape() -> None:
    descriptor = {**_descriptor(AcmeMemberState), "ref": _ORIGIN}
    resolved = contract_wire.annotation_from_descriptor(descriptor, _OTHER)
    assert resolved is MemberState


def test_equal_shapes_of_different_origins_are_told_by_name_or_refused() -> None:
    contract = "kb23amb.things.v1"

    class Thing(BaseModel):
        id: int

    class Other(BaseModel):
        id: int

    Thing.__module__ = Other.__module__ = "kb23_consumer.dto"
    dto_copy("kb23_provider.schemas:Thing")(Thing)
    dto_copy("kb23_provider.schemas:Other")(Other)

    class Stub(Protocol):
        async def thing(self) -> Thing: ...

        async def other(self) -> Other: ...

    ContractRef(contract, Stub)
    assert set(contract_copies(contract)) == {Thing, Other}

    class Replacement(BaseModel):
        id: int

    Replacement.__module__ = "acme_things.schemas"
    resolve = contract_wire.annotation_from_descriptor
    for name, expected in (("Thing", Thing), ("Other", Other)):
        Replacement.__qualname__ = name
        assert resolve(_descriptor(Replacement), contract) is expected
    Replacement.__qualname__ = "Unrelated"
    assert resolve(_descriptor(Replacement), contract) == dict[str, Any]
