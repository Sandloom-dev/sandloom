"""A command's declared risk marks the caller's request the same way locally
and over RPC (ADR-0009 item 7; G-SENS-02 / G-SENS-04 after a split): the
automatic audit of a sensitive write must not depend on the deployment."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI

from sandloom.core.contract_testing import (
    in_process_rpc_client,
    rpc_proxy_for,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.testing import InMemoryCache
from sandloom.core.tests.test_contract_parity import _FakeDatabase
from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope

_SVC = "svc"
CONTRACT = "demo.money.v1"
WRAPPER = "demo.wrapper.v1"
#: the provider core the wrapper's nested local call goes through
_PROVIDER: list[CoreFacade] = []


class MoneyDemo:
    @classmethod
    @contract_method(command=True, idempotency_key="operation_key", risk="money")
    async def spend(cls, amount: int, operation_key: str) -> int:
        return amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def note(cls, operation_key: str) -> bool:
        return True


class Wrapper:
    """An UNTAGGED command whose body calls the tagged money command."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def checkout(cls, operation_key: str) -> int:
        spent: int = (
            await _PROVIDER[0]
            .get_service(CONTRACT)
            .spend(amount=3, operation_key=f"{operation_key}:inner")
        )
        return spent


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[CoreFacade]:
    from sandloom.core.config.settings import settings

    cache = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": cache)
    allowed = {_SVC: [CONTRACT, WRAPPER]}
    for name, value in rpc_settings(_SVC, allowed_calls=allowed).items():
        monkeypatch.setattr(settings, name, value)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service(CONTRACT, MoneyDemo)
    facade.register_service(WRAPPER, Wrapper)
    _PROVIDER[:] = [facade]
    yield facade
    _PROVIDER.clear()


async def _risks_after(proxy: Any, method: str, **kwargs: Any) -> Any:
    scope = RequestScope(core=None, policy=None)
    with bind_request_scope(scope):
        await getattr(proxy, method)(**kwargs)
    return (
        None if scope.risk is None else {k: set(v) for k, v in scope.risk.risks.items()}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("described", [False, True])
async def test_a_money_command_marks_the_callers_request_locally_and_remotely(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch, described: bool
) -> None:
    local = await _risks_after(
        core.get_service(CONTRACT), "spend", amount=5, operation_key="k-local"
    )
    assert local == {"money": {f"command:{CONTRACT}"}}
    monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
    async with in_process_rpc_client(core) as client:
        proxy = rpc_proxy_for(core, CONTRACT, client)
        if described:
            await proxy.note(operation_key="warm")  # describes the contract
        remote = await _risks_after(proxy, "spend", amount=5, operation_key="k-rpc")
        plain = await _risks_after(proxy, "note", operation_key="k-plain")
    assert remote == local
    assert plain is None  # a command without a declared risk marks nothing


@pytest.mark.asyncio
@pytest.mark.parametrize("described", [False, True])
async def test_a_nested_money_command_marks_the_callers_request_locally_and_remotely(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch, described: bool
) -> None:
    """An untagged command whose body calls a tagged money command: locally
    the nested call marks the request (its audit unit opens); over RPC the
    provider reports the risk it OBSERVED, nested calls included, so a split
    deploy marks the caller's request identically (G-SENS-04)."""
    local = await _risks_after(
        core.get_service(WRAPPER), "checkout", operation_key="n-local"
    )
    assert local == {"money": {f"command:{CONTRACT}"}}
    monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
    async with in_process_rpc_client(core) as client:
        proxy = rpc_proxy_for(core, WRAPPER, client)
        if described:
            await proxy.checkout(operation_key="warm")  # describes the contract
        scope = RequestScope(core=None, policy=None)
        with bind_request_scope(scope):
            await proxy.checkout(operation_key="n-rpc")
    assert scope.risk is not None
    assert {k: set(v) for k, v in scope.risk.risks.items()} == local
    # the remote command committed in a unit of its own: the request owes
    # its audit row even if its own units write nothing (as locally)
    assert scope.risk.elsewhere is True
