"""Under the ``client`` fixture a contract call made by a route that never opens
``db()`` still runs on the test's session (docs/reference/testing.md).

Regression (apps dogfood): the contract session was bound only when the route
first opened ``db()`` (``SharedRequestUnit.bind_lazily``), so a route that only
called a contract ran it on the provider's own unit and missed the rows the test
had flushed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from sandloom.core import di
from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.module_api import contract_method
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.unit_of_work.request import db as unit_db

PATH = "/__contract_session/rows"


class Probe:
    @classmethod
    @contract_method
    async def rows(cls) -> list[str]:
        result = await unit_db().execute(text("SELECT v FROM contract_probe"))
        return sorted(result.scalars())


@pytest.fixture
def probe_route(sandloom_app: Any) -> Iterator[None]:
    before = list(sandloom_app.router.routes)

    @sandloom_app.get(PATH, dependencies=[di.public(reason="probe")])
    async def rows() -> Any:  # never opens db() or DbSession itself
        return {"rows": await Probe.rows()}

    added = [r for r in sandloom_app.router.routes if r not in before]
    install_route_runtime(added, sandloom_app.state.core)
    try:
        yield
    finally:
        sandloom_app.router.routes[:] = before


async def test_a_contract_call_from_a_route_without_db_sees_the_tests_rows(
    probe_route: None, client: Any, db: Any
) -> None:
    await db.execute(text("CREATE TEMP TABLE contract_probe (v text)"))
    await db.execute(text("INSERT INTO contract_probe (v) VALUES ('seeded')"))

    response = await client.get(PATH)

    assert response.status_code == 200, response.text
    assert response.json() == {"rows": ["seeded"]}
