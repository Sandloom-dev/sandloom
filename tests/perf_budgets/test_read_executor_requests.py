"""Request-level semantics of the read-executor prototypes (real request UoW).

A route declared with ``ReadOnlyDbSession`` (``DB_READ_ONLY_SESSIONS=true``):
BEGIN READ ONLY, ``on_commit`` hooks still run after success, and
``publish_event`` / ``record_audit`` / ORM writes are REFUSED (the request
fails, nothing reaches the database). With the setting off the same route is
a normal ``DbSession`` (BEGIN / SELECT / COMMIT).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter
from sqlalchemy import column, select, table

from sandloom.core.config.settings import settings
from sandloom.core.di import (
    DbSession,
    ReadOnlyDbSession,
    on_commit,
    publish_event,
    record_audit,
)
from sandloom.core.unit_of_work.dispatch import drain_post_commit

from .pgcount import PgCountingProxy

pytestmark = pytest.mark.asyncio

HOOKS: list[str] = []
_T = table("outbox_messages", column("id"))


def _router() -> APIRouter:
    r = APIRouter()

    @r.get("/_rx/ro/{action}")
    async def ro(action: str, db: ReadOnlyDbSession) -> dict[str, Any]:
        await db.execute(select(_T.c.id).limit(1))
        if action == "event":
            await publish_event("rx.probe.happened.v1", {"x": 1})
        elif action == "audit":
            record_audit("rx.probe", actor_id=None, resource_type="x", resource_id="1")
            await db.flush()
        elif action == "lock":
            await db.execute(select(_T.c.id).limit(1).with_for_update())
        await on_commit(lambda: HOOKS.append(action))
        return {"ok": True}

    @r.get("/_rx/rw")
    async def rw(db: DbSession) -> dict[str, Any]:
        await db.execute(select(_T.c.id).limit(1))
        await on_commit(lambda: HOOKS.append("rw"))
        return {"ok": True}

    return r


@pytest.fixture
def policy(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, sandloom_app: Any
) -> Iterator[str]:
    mode = request.param
    monkeypatch.setattr(settings, "db_read_only_sessions", mode == "read_only")
    before = list(sandloom_app.router.routes)
    sandloom_app.include_router(_router())
    HOOKS.clear()
    yield mode
    sandloom_app.router.routes[:] = before


def _kinds(proxy: PgCountingProxy) -> list[str]:
    kinds = []
    for rt in proxy.reset():
        sql = " ".join(rt.sql).upper()
        kinds.append(
            next(
                (
                    k
                    for k in (
                        "BEGIN",
                        "COMMIT",
                        "ROLLBACK",
                        "INSERT",
                        "UPDATE",
                        "SELECT",
                    )
                    if k in sql
                ),
                sql[:30],
            )
        )
    return kinds


async def _get(client: Any, proxy: PgCountingProxy, path: str) -> tuple[int, list[str]]:
    await client.get(path)  # warm
    await drain_post_commit()
    proxy.reset()
    HOOKS.clear()
    resp = await client.get(path)
    await drain_post_commit()
    return resp.status_code, _kinds(proxy)


@pytest.mark.parametrize("policy", ["read_only"], indirect=True)
async def test_declared_read_only_route_is_transactional_and_runs_hooks(
    policy: str, counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    status, kinds = await _get(counted_client, pg_proxy, "/_rx/ro/read")
    assert status == 200 and kinds == ["BEGIN", "SELECT", "COMMIT"], kinds
    assert HOOKS == ["read"]
    # an undeclared route on the same app keeps its transaction
    status, kinds = await _get(counted_client, pg_proxy, "/_rx/rw")
    assert status == 200 and kinds == ["BEGIN", "SELECT", "COMMIT"], kinds


@pytest.mark.parametrize("policy", ["read_only"], indirect=True)
@pytest.mark.parametrize("action", ["event", "audit", "lock"])
async def test_declared_read_only_route_refuses_hidden_writes(
    policy: str, counted_client: Any, pg_proxy: PgCountingProxy, action: str
) -> None:
    pg_proxy.reset()
    HOOKS.clear()
    resp = await counted_client.get(f"/_rx/ro/{action}")
    await drain_post_commit()
    assert resp.status_code == 500
    kinds = _kinds(pg_proxy)
    assert not {"INSERT", "UPDATE"} & set(kinds), kinds
    assert "FOR UPDATE" not in " ".join(kinds)
    assert HOOKS == []  # no on_commit after a failed request


@pytest.mark.parametrize("policy", ["off"], indirect=True)
async def test_setting_off_read_only_route_is_a_normal_uow(
    policy: str, counted_client: Any, pg_proxy: PgCountingProxy
) -> None:
    status, kinds = await _get(counted_client, pg_proxy, "/_rx/ro/read")
    assert status == 200 and kinds == ["BEGIN", "SELECT", "COMMIT"], kinds
    assert HOOKS == ["read"]
