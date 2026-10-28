"""Typed contracts (spec §5.3; ledger G-DECL-03/04, C-DECL-02, C-TEST-09)."""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core.module_api import contract_schema
from sandloom.sdk import Contract, command, contract
from sandloom.sdk._decl.contracts import called_methods
from sandloom.sdk._prim import contracts as prim_contracts
from sandloom.sdk.contracts import ContractRef, OptionalContract, service
from sandloom.sdk.errors import ContractDefinitionError, ServiceUnavailableInDeployment
from sandloom.sdk.sagas import step
from sandloom.sdk.testing import fake_contract

REF: ContractRef[Any] = ContractRef("sdkct.quota.v1")


@contract("sdkct.quota.v1", database="quota")
class Quota:
    async def may_create(self) -> bool:
        return True

    @command(idempotency_key="operation_key", idempotency_scope=("account",))
    async def reserve(self, account: str, amount: int, operation_key: str) -> int:
        return amount

    @contract.read(planned=True)
    async def planned_read(self) -> int:
        return 1


def test_provider_becomes_the_l2_form_with_every_option() -> None:
    schema = contract_schema(Quota)
    assert set(schema.methods) == {"may_create", "reserve", "planned_read"}
    described = {n: m.describe() for n, m in schema.methods.items()}
    assert described["reserve"]["kind"] == "command"
    assert described["reserve"]["idempotency_key"] == "operation_key"
    assert described["may_create"]["kind"] == "read"
    spec = prim_contracts.contract_spec(Quota.reserve)
    assert spec is not None and spec.idempotency_scope == ("account",)
    planned = prim_contracts.contract_spec(Quota.planned_read)
    assert planned is not None and planned.planned
    assert Quota.__sandloom_contract__.database == "quota"  # type: ignore[attr-defined]


class _WithInit:
    def __init__(self) -> None:
        pass


class _Sync:
    def sync(self) -> int:
        return 1


class _Constant:
    LIMIT = 3


class _Stateful:
    async def stateful(self) -> int:
        self.n = 1
        return 1


@pytest.mark.parametrize(
    ("cls", "message"),
    [
        (_WithInit, "__init__"),
        (_Sync, "not async"),
        (_Constant, "public int"),
        (_Stateful, "instance attributes"),
    ],
)
def test_stateful_or_non_async_providers_are_refused(cls: type, message: str) -> None:
    with pytest.raises(ContractDefinitionError, match=message):
        contract("sdkct.bad.v1")(cls)


def test_command_needs_its_key_parameter() -> None:
    with pytest.raises(ContractDefinitionError, match="idempotency_key"):
        command(idempotency_key="")
    with pytest.raises(ContractDefinitionError, match="not a parameter"):

        @command(idempotency_key="key")
        async def nope(self: Any, amount: int) -> int:
            return amount


async def test_caller_resolves_when_called_through_a_fake() -> None:
    quota = Contract(REF)

    class Allow:
        async def may_create(self) -> bool:
            return True

    previous = prim_contracts._bind_resolver(None)
    try:
        with fake_contract(REF, Allow()):
            assert await quota.may_create() is True
        with pytest.raises(RuntimeError, match="no contract resolver"):
            await quota.may_create()
    finally:
        prim_contracts._bind_resolver(previous)


def test_the_default_resolver_is_the_kernels_active_core() -> None:
    """Outside the test plugin, ``Contract(REF)`` resolves through the core
    ``create_app`` activated (``sandloom.core.di.get_service``)."""
    from sandloom.core.di import get_service

    assert prim_contracts._resolver is get_service


async def test_caller_uses_the_bound_resolver() -> None:
    calls: list[str] = []

    class Provider:
        async def may_create(self) -> bool:
            return False

    def resolve(name: str) -> Any:
        calls.append(name)
        return Provider()

    previous = prim_contracts._bind_resolver(resolve)
    try:
        assert await Contract(REF).may_create() is False
    finally:
        prim_contracts._bind_resolver(previous)
    assert calls == ["sdkct.quota.v1"]


def test_optional_is_none_only_when_not_deployed() -> None:
    def not_deployed(name: str) -> Any:
        raise ServiceUnavailableInDeployment(name)

    previous = prim_contracts._bind_resolver(not_deployed)
    try:
        maybe = Contract(REF, optional=True)
        assert isinstance(maybe, OptionalContract)
        assert maybe.client() is None
        with pytest.raises(ServiceUnavailableInDeployment):
            service("sdkct.quota.v1")
    finally:
        prim_contracts._bind_resolver(previous)


def test_optional_returns_a_deployed_provider_even_if_it_will_fail() -> None:
    sentinel = object()
    previous = prim_contracts._bind_resolver(lambda name: sentinel)
    try:
        assert Contract(REF, optional=True).client() is sentinel
    finally:
        prim_contracts._bind_resolver(previous)


def test_contract_needs_a_generated_reference() -> None:
    with pytest.raises(ContractDefinitionError, match="ContractRef"):
        Contract("sdkct.quota.v1")  # type: ignore[call-overload]


def test_methods_are_derived_from_direct_calls_only() -> None:
    source = (
        "quota = Contract(REF)\n"
        "q = quota\n"
        "async def f():\n"
        "    await quota.may_create()\n"
        "    await q.reserve('a', 1, 'k')\n"
    )
    assert called_methods(source, {"quota"}) == ("may_create", "reserve")
    assert called_methods(source + "g(quota)\n", {"quota"}) == ()
    assert called_methods(source + "x = getattr(quota, 'm')\n", {"quota"}) == ()
    assert (
        called_methods(
            "quota = Contract(REF, optional=True)\nquota.client()\n", {"quota"}
        )
        == ()
    )


def test_typed_saga_step_names_the_command() -> None:
    quota = Contract(REF)
    typed = step(
        "reserve",
        call=quota.reserve,
        args=lambda ctx: {"account": "a", "amount": 1},
        compensate=quota.release,
    )
    by_name = step(
        "reserve",
        call="sdkct.quota.v1.reserve",
        args=lambda ctx: {"account": "a", "amount": 1},
        compensate="sdkct.quota.v1.release",
    )
    assert (typed.call, typed.compensate) == (by_name.call, by_name.compensate)
    with pytest.raises(TypeError, match="contract caller method"):
        step("x", call=print, args=lambda ctx: {})  # type: ignore[arg-type]
