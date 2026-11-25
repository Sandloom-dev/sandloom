"""Under the ``client`` fixture a contract call runs on the test's session, even
when the route never opens ``db()`` (docs/reference/testing.md).

Regression (apps dogfood): the contract session was bound only when the route
first opened ``db()`` (``SharedRequestUnit.bind_lazily``), so a route that only
called a contract ran it on the provider's own unit and missed the rows the test
had flushed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sandloom.core import di
from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.module_api import contract_method, contract_session
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.unit_of_work.request import open_test_request_unit

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


async def test_the_seam_opens_nothing_outside_a_request(client: Any) -> None:
    """No request scope, nothing to open (the test body is no request)."""
    assert open_test_request_unit() is None
