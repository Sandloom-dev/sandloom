"""Under the ``client`` fixture a route's session is the test's shared one
however the route obtains it: a ``DbSession`` route and a ``db()`` route
behave identically (C-TEST-11). Their writes are visible to the test and to
later requests; a failed request does not roll its writes back; the
``on_commit`` callbacks run after a successful request only.

Regression (DX eval p3): ``db()`` took the lazy request unit's own
connection, so a ``db()`` route committed for real (and a failure rolled
back) while a ``DbSession`` route shared the test session.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from sandloom.core import di
from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.testing.fixtures import *  # noqa: F403

PREFIX = "/__parity"
callbacks: list[str] = []


async def _write(session: Any, path: str, fail: bool) -> dict[str, bool]:
    await session.execute(text("INSERT INTO parity_probe (v) VALUES (:v)"), {"v": path})

    async def done() -> None:
        callbacks.append(path)

    await di.on_commit(done)
    if fail:
        raise HTTPException(status_code=409, detail="refused after the write")
    return {"ok": True}


@pytest.fixture
def parity_routes(sandloom_app: Any) -> Iterator[None]:
    before = list(sandloom_app.router.routes)

    @sandloom_app.post(f"{PREFIX}/dbsession", dependencies=[di.public(reason="probe")])
    async def via_dbsession(session: di.DbSession, fail: bool = False) -> Any:
        return await _write(session, "dbsession", fail)

    @sandloom_app.post(f"{PREFIX}/db", dependencies=[di.public(reason="probe")])
    async def via_db(fail: bool = False) -> Any:
        return await _write(di.db(), "db", fail)

    added = [r for r in sandloom_app.router.routes if r not in before]
    install_route_runtime(added, sandloom_app.state.core)
    callbacks.clear()
    try:
        yield
    finally:
        sandloom_app.router.routes[:] = before


async def _rows(db: Any) -> list[str]:
    return list(
        (await db.execute(text("SELECT v FROM parity_probe ORDER BY v"))).scalars()
    )


@pytest.mark.parametrize("route", ["dbsession", "db"])
async def test_a_dbsession_route_and_a_db_route_share_the_test_session(
    parity_routes: None, client: Any, db: Any, route: str
) -> None:
    await db.execute(text("CREATE TEMP TABLE parity_probe (v text)"))
    ok = await client.post(f"{PREFIX}/{route}")
    assert ok.status_code == 200, ok.text
    assert await _rows(db) == [route]  # visible in the test's transaction
    assert callbacks == [route]  # on_commit after a successful request

    refused = await client.post(f"{PREFIX}/{route}", params={"fail": "true"})
    assert refused.status_code == 409
    assert await _rows(db) == [route, route]  # not rolled back (shared session)
    assert callbacks == [route]  # never after a failure
