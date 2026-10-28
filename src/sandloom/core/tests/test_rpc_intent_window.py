"""Intent windows over the signed RPC path (G-UNIT-02 / G-BILLING-19).

A REMOTE contract call obeys exactly the local rules: a command started
inside ``external_call()`` / ``release_check()`` is refused (SANDLOOM-R008) before
anything is sent, and a call already running when a window is entered is
waited for, so the external call never runs beside a provider transaction of
the request (local == remote). The calls go through the generated proxy and
the provider's real, signed RPC routes (in-process ASGI).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

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
from sandloom.core.unit_of_work.intent import (
    IntentState,
    ResolvedAccount,
    external_call,
    release_check,
)
from sandloom.core.unit_of_work.request import (
    RequestScope,
    UnitRuleError,
    bind_request_scope,
)

_SVC = "svc"
CONTRACT = "demo.gate.v1"


class Gate:
    _calls: list[str] = []
    _started: asyncio.Event | None = None
    _release: asyncio.Event | None = None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def run(cls, operation_key: str) -> int:
        cls._calls.append(operation_key)
        if cls._started is not None and cls._release is not None:
            cls._started.set()
            await cls._release.wait()
        return 1

    @classmethod
    @contract_method
    async def peek(cls) -> int:
        cls._calls.append("peek")
        return 7


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[CoreFacade]:
    from sandloom.core.config.settings import settings

    cache = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": cache)
    monkeypatch.setattr(rpc_proxy, "_remote_contracts", {})
    for name, value in rpc_settings(_SVC, allowed_calls={_SVC: [CONTRACT]}).items():
        monkeypatch.setattr(settings, name, value)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    facade.register_service(CONTRACT, Gate)
    Gate._calls = []
    Gate._started = Gate._release = None
    yield facade


def _intent_scope() -> RequestScope:
    scope = RequestScope(core=None, policy=None)
    scope.intent = IntentState()
    return scope


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["external_call", "release_check"])
async def test_a_remote_command_inside_an_intent_window_is_refused(
    core: CoreFacade, window: str
) -> None:
    """Inside the window a remote command is refused before it is sent (the
    provider never runs it); in release_check's authority callback a remote
    READ runs, described or not (it is the re-read's own call)."""
    seen: dict[str, Any] = {}
    async with in_process_rpc_client(core) as client:
        proxy = rpc_proxy_for(core, CONTRACT, client)

        async def in_window() -> None:
            if window == "release_check":
                seen["peek"] = await proxy.peek()
            with pytest.raises(UnitRuleError, match="SANDLOOM-R008"):
                await proxy.run(operation_key=f"in-{window}")

        with bind_request_scope(_intent_scope()):
            if window == "external_call":
                async with external_call("psp.create"):
                    await in_window()
            else:
                await release_check(
                    ResolvedAccount(kind="org", id=uuid4()), authority=in_window
                )
            # after the window the same call runs
            assert await proxy.run(operation_key=f"after-{window}") == 1
    assert f"in-{window}" not in Gate._calls
    assert Gate._calls[-1] == f"after-{window}"
    if window == "release_check":
        assert seen == {"peek": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["external_call", "release_check"])
async def test_an_intent_window_waits_for_a_running_remote_command(
    core: CoreFacade, window: str
) -> None:
    """A remote command already running when the window is entered is
    counted: the window opens only after it returned (never beside it)."""
    Gate._started, Gate._release = asyncio.Event(), asyncio.Event()
    order: list[str] = []
    async with in_process_rpc_client(core) as client:
        proxy = rpc_proxy_for(core, CONTRACT, client)

        async def command() -> int:
            result: int = await proxy.run(operation_key=f"child-{window}")
            order.append("command returned")
            return result

        async def enter_window() -> None:
            if window == "external_call":
                async with external_call("psp.create"):
                    order.append("window open")
            else:

                async def authority() -> None:
                    order.append("window open")

                await release_check(
                    ResolvedAccount(kind="org", id=uuid4()), authority=authority
                )

        with bind_request_scope(_intent_scope()):
            running = asyncio.create_task(command())
            await Gate._started.wait()
            entered = asyncio.create_task(enter_window())
            for _ in range(20):  # the window is entered and waiting
                await asyncio.sleep(0)
            assert order == []  # never beside the running provider call
            Gate._release.set()
            await entered
            assert await running == 1
    assert order == ["command returned", "window open"]
