"""Under the ``client`` fixture a contract call runs on the test's session, even
when the route never opens ``db()`` (docs/reference/testing.md).

Regression (apps dogfood): the contract session was bound only when the route
first opened ``db()`` (``SharedRequestUnit.bind_lazily``), so a route that only
called a contract ran it on the provider's own unit and missed the rows the test
had flushed.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core import di
from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.module_api import contract_method, contract_session
from sandloom.core.testing._uow import _ContractSessionBinder
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.request import (
    BORROWERS_KEY,
    OWNER_KEY,
    ConcurrentSessionUseError,
    _refuse_foreign,
    claim_unit,
    join_request_scope,
    return_request_scope,
)

PREFIX = "/__contract_session"
CONTRACT = "ctprobe.session.v1"


class Probe:
    _seen: list[Any] = []

    @classmethod
    @contract_method
    async def which(cls) -> bool:
        cls._seen.append(contract_session())
        return True


@pytest.fixture
def probe(sandloom_app: Any) -> Iterator[None]:
    core = sandloom_app.state.core
    core.register_service(CONTRACT, Probe, database="default")
    Probe._seen.clear()
    before = list(sandloom_app.router.routes)

    @sandloom_app.get(f"{PREFIX}/nodb", dependencies=[di.public(reason="probe")])
    async def without_db() -> Any:  # never opens db() or DbSession
        return {"ok": await core.get_service(CONTRACT).which()}

    @sandloom_app.get(f"{PREFIX}/db", dependencies=[di.public(reason="probe")])
    async def with_db() -> Any:
        di.db()
        return {"ok": await core.get_service(CONTRACT).which()}

    @sandloom_app.get(f"{PREFIX}/unit", dependencies=[di.public(reason="probe")])
    async def with_explicit_unit() -> Any:
        async with db_session() as own:
            await core.get_service(CONTRACT).which()
            return {"own_unit_current_after": di.db() is own}

    added = [r for r in sandloom_app.router.routes if r not in before]
    install_route_runtime(added, core)
    try:
        yield
    finally:
        sandloom_app.router.routes[:] = before
        core.service_registry.remove(CONTRACT)


@pytest.mark.parametrize("route", ["nodb", "db"])
async def test_a_contract_call_runs_on_the_tests_session_however_the_route_gets_its_unit(
    probe: None, client: Any, db: Any, route: str
) -> None:
    response = await client.get(f"{PREFIX}/{route}")

    assert response.status_code == 200, response.text
    assert Probe._seen == [db]


async def test_a_contract_call_inside_an_explicit_unit_leaves_that_unit_current(
    probe: None, client: Any, db: Any
) -> None:
    response = await client.get(f"{PREFIX}/unit")

    assert response.status_code == 200, response.text
    assert Probe._seen == [db]  # the contract ran on the test's session
    assert response.json() == {"own_unit_current_after": True}


async def test_a_request_in_another_context_gives_the_session_back(
    probe: None, client: Any, db: Any
) -> None:
    """A concurrent actor of the test borrows the shared session for its
    request: the test's own task owns it again afterwards."""
    before = db.info.get(OWNER_KEY)

    response = await asyncio.create_task(
        client.get(f"{PREFIX}/db"), context=contextvars.Context()
    )

    assert response.status_code == 200, response.text
    assert db.info.get(OWNER_KEY) is before


@pytest.mark.parametrize("first_out", ["a", "b"])
async def test_overlapping_requests_each_keep_the_session_until_the_last_is_done(
    first_out: str,
) -> None:
    """The test's task T owns the session; two actors A and B join from other
    tasks. Whichever ends first, the other keeps working, a task that never
    joined is refused, and T owns the session again after the last one."""
    session = SimpleNamespace(info={})
    claim_unit(session)
    test_owner = session.info[OWNER_KEY]
    joined = {"a": asyncio.Event(), "b": asyncio.Event()}
    leave = {"a": asyncio.Event(), "b": asyncio.Event()}
    left = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def borrower(name: str) -> None:
        join_request_scope(session)
        joined[name].set()
        try:
            await leave[name].wait()
            _refuse_foreign(session)  # still this request's session
        finally:
            return_request_scope(session)
            left[name].set()

    async def outsider() -> None:
        _refuse_foreign(session)

    tasks = []
    for name in ("a", "b"):
        tasks.append(asyncio.create_task(borrower(name)))
        await joined[name].wait()
    with pytest.raises(ConcurrentSessionUseError):
        await asyncio.create_task(outsider())

    second_out = "b" if first_out == "a" else "a"
    leave[first_out].set()
    await left[first_out].wait()
    leave[second_out].set()
    await left[second_out].wait()
    await asyncio.gather(*tasks)

    assert session.info[OWNER_KEY] is test_owner
    assert BORROWERS_KEY not in session.info


async def test_the_binding_ends_with_the_test(sandloom_app: Any, db: Any) -> None:
    """Outside the ``client`` fixture the router is the app's own."""
    assert not isinstance(sandloom_app.router.middleware_stack, _ContractSessionBinder)
