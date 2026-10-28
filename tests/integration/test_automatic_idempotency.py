"""The automatic idempotency guard (spec §4.7; ledger G-IDEM-17, G-IDEM-18),
over HTTP with real PostgreSQL and Redis.

Independent oracles (spec §10.1): the rows the handler wrote (plain SQL), the
HTTP status / body / headers, a handler-run counter.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import Integer, String, insert, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import di
from sandloom.core.auth import marks
from sandloom.core.http import route_runtime
from sandloom.core.testing import make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.shared.authz import Role, current_context, require

pytestmark = pytest.mark.asyncio

PREFIX = "/__wsa_idem"
TABLE = "wsa_idem_probe"


class _Base(DeclarativeBase):
    pass


class Probe(_Base):
    __tablename__ = TABLE

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(80))


@pytest.fixture
async def table(db_engine: Any) -> AsyncIterator[None]:
    async with db_engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.execute(text(f"DELETE FROM {TABLE}"))
    yield


async def _rows(db_engine: Any) -> list[str]:
    async with db_engine.connect() as conn:
        return [
            r[0] for r in await conn.execute(text(f"SELECT v FROM {TABLE} ORDER BY id"))
        ]


@pytest.fixture
def mount(sandloom_app: FastAPI) -> Iterator[Callable[[APIRouter], None]]:
    before = list(sandloom_app.router.routes)

    def _mount(router: APIRouter) -> None:
        sandloom_app.include_router(router)
        new = [r for r in sandloom_app.router.routes if r not in before]
        route_runtime.install_route_runtime(new, sandloom_app.state.core)

    yield _mount
    sandloom_app.router.routes[:] = before


USER = [Depends(current_context)]
OWNER: dict[str, str] = {}


def _key(headers: Any, key: str) -> dict[str, str]:
    return {**dict(headers), "Idempotency-Key": key}


async def test_the_automatic_guard_replays_the_status_only_never_a_body(
    real_uow_client: Any, db_engine: Any, table: None, mount: Any
) -> None:
    """Mode S, including the ownership-transfer case of spec §4.7: the body the
    first attempt returned is never replayed."""
    router = APIRouter(prefix=PREFIX, dependencies=USER)
    runs: list[int] = []

    @router.post("/docs", status_code=201)
    async def create(
        db: di.DbSession, user: Any = Depends(current_context)
    ) -> dict[str, Any]:
        runs.append(1)
        await db.execute(insert(Probe).values(v="doc"))
        mine = OWNER.get("doc") == str(user.user_id)
        return {"secret": "s3cr3t"} if mine else {}

    mount(router)
    uid, headers = make_user_headers(uuid.uuid4())
    OWNER["doc"] = str(uid)
    first = await real_uow_client.post(f"{PREFIX}/docs", headers=_key(headers, "k1"))
    assert first.status_code == 201 and first.json() == {"secret": "s3cr3t"}
    OWNER["doc"] = "someone-else"  # ownership transferred between attempts
    retry = await real_uow_client.post(f"{PREFIX}/docs", headers=_key(headers, "k1"))
    assert retry.status_code == 201
    assert retry.headers["idempotent-replay"] == "true"
    assert "s3cr3t" not in retry.text and retry.content in (b"", b"null")
    assert runs == [1] and await _rows(db_engine) == ["doc"]
    # a different request under the same key is 422; another route or another
    # tenant never replays it
    mismatch = await real_uow_client.post(
        f"{PREFIX}/docs?x=1", headers=_key(headers, "k1")
    )
    assert mismatch.status_code == 422
    _uid, other_tenant = make_user_headers(uid)
    fresh = await real_uow_client.post(
        f"{PREFIX}/docs", headers=_key(other_tenant, "k1")
    )
    assert fresh.status_code == 201 and "idempotent-replay" not in fresh.headers
    assert runs == [1, 1]
    # without the header nothing is deduped
    await real_uow_client.post(f"{PREFIX}/docs", headers=headers)
    await real_uow_client.post(f"{PREFIX}/docs", headers=headers)
    assert len(runs) == 4


async def test_replay_safe_replays_the_body_after_every_route_dependency(
    real_uow_client: Any, db_engine: Any, table: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX)
    runs: list[int] = []

    @router.post(
        "/notes",
        status_code=201,
        dependencies=[
            Depends(require(Role("writer"))),
            di.replay_safe(reason="echoes the caller's own input and the new id"),
        ],
    )
    async def create(db: di.DbSession) -> dict[str, int]:
        runs.append(1)
        await db.execute(insert(Probe).values(v="note"))
        return {"id": len(runs)}

    @router.post(
        "/sensitive-notes",
        dependencies=[*USER, di.sensitive("admin"), di.replay_safe(reason="r")],
    )
    async def sensitive_create(db: di.DbSession) -> dict[str, str]:
        await db.execute(insert(Probe).values(v="sensitive"))
        return {"body": "never replayed"}

    mount(router)
    uid, headers = make_user_headers(uuid.uuid4(), roles=["writer"])
    first = await real_uow_client.post(f"{PREFIX}/notes", headers=_key(headers, "n1"))
    retry = await real_uow_client.post(f"{PREFIX}/notes", headers=_key(headers, "n1"))
    assert first.json() == retry.json() == {"id": 1}
    assert retry.headers["idempotent-replay"] == "true" and runs == [1]
    # authority lost between attempts: the gate refuses before any replay
    _uid, demoted = make_user_headers(uid, roles=[])
    lost = await real_uow_client.post(f"{PREFIX}/notes", headers=_key(demoted, "n1"))
    assert lost.status_code == 403
    # a sensitive write is mode S even with replay_safe
    s1 = await real_uow_client.post(
        f"{PREFIX}/sensitive-notes", headers=_key(headers, "s1")
    )
    s2 = await real_uow_client.post(
        f"{PREFIX}/sensitive-notes", headers=_key(headers, "s1")
    )
    assert s1.json() == {"body": "never replayed"}
    assert s2.headers["idempotent-replay"] == "true" and "never" not in s2.text


async def test_a_revoked_session_is_refused_before_any_replay(
    real_uow_client: Any, sandloom_cache: Any, table: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX, dependencies=USER)

    @router.post("/items")
    async def create(db: di.DbSession) -> dict[str, bool]:
        await db.execute(insert(Probe).values(v="item"))
        return {"ok": True}

    mount(router)
    uid, headers = make_user_headers(uuid.uuid4())
    assert (
        await real_uow_client.post(f"{PREFIX}/items", headers=_key(headers, "r1"))
    ).status_code == 200
    await sandloom_cache.set(
        marks.subject_key(uid),
        marks._format_mark(marks._seconds_to_us(time.time() + 1)),
        ttl_seconds=120,
    )
    refused = await real_uow_client.post(f"{PREFIX}/items", headers=_key(headers, "r1"))
    assert refused.status_code == 401 and "idempotent-replay" not in refused.headers


async def test_a_lazy_db_write_gets_the_durable_status_replay(
    real_uow_client: Any, db_engine: Any, table: None, mount: Any
) -> None:
    """G-IDEM-17: an ordinary write that reaches the database through db()
    (the lazy request unit) is mode S like a DbSession write: the guard opens
    the lazy unit for a keyed write and claims the durable record in it, so a
    sequential retry is a status replay (one row), a concurrent duplicate 409."""
    router = APIRouter(prefix=PREFIX, dependencies=USER)
    gate = asyncio.Event()
    entered = asyncio.Event()
    runs: list[int] = []

    @router.post("/lazy", status_code=201)
    async def lazy(wait: bool = False) -> dict[str, int]:
        runs.append(1)
        if wait:
            entered.set()
            await gate.wait()
        await di.db().execute(insert(Probe).values(v="lazy"))
        return {"n": len(runs)}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    first = asyncio.create_task(
        real_uow_client.post(f"{PREFIX}/lazy?wait=true", headers=_key(headers, "l1"))
    )
    await asyncio.wait_for(entered.wait(), 5)
    duplicate = await real_uow_client.post(
        f"{PREFIX}/lazy?wait=true", headers=_key(headers, "l1")
    )
    assert duplicate.status_code == 409
    gate.set()
    done = await first
    assert done.status_code == 201 and done.json() == {"n": 1}
    again = await real_uow_client.post(
        f"{PREFIX}/lazy?wait=true", headers=_key(headers, "l1")
    )
    assert again.status_code == 201
    assert again.headers["idempotent-replay"] == "true" and again.content == b""
    assert await _rows(db_engine) == ["lazy"]
    assert runs == [1]


async def test_without_a_request_unit_the_guard_is_the_fail_closed_in_flight_form(
    real_uow_client: Any,
    db_engine: Any,
    table: None,
    mount: Any,
    sandloom_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G-IDEM-18: a write whose request unit has no durable request log gets
    the fail-closed in-flight form: a concurrent duplicate is 409; nothing is
    stored, so a later retry runs again."""
    database = sandloom_app.state.core.database("default")
    monkeypatch.setattr(database, "request_log", lambda: None)
    router = APIRouter(prefix=PREFIX, dependencies=USER)
    gate = asyncio.Event()
    entered = asyncio.Event()
    runs: list[int] = []

    @router.post("/nolog")
    async def nolog(wait: bool = False) -> dict[str, int]:
        runs.append(1)
        if wait:
            entered.set()
            await gate.wait()
        await di.db().execute(insert(Probe).values(v="nolog"))
        return {"n": len(runs)}

    @router.post("/opted-out", dependencies=[di.no_idempotency(reason="stream")])
    async def opted_out(db: di.DbSession) -> dict[str, bool]:
        runs.append(1)
        return {"ok": True}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    first = asyncio.create_task(
        real_uow_client.post(f"{PREFIX}/nolog?wait=true", headers=_key(headers, "l1"))
    )
    await asyncio.wait_for(entered.wait(), 5)
    duplicate = await real_uow_client.post(
        f"{PREFIX}/nolog?wait=true", headers=_key(headers, "l1")
    )
    assert duplicate.status_code == 409
    gate.set()
    assert (await first).status_code == 200
    again = await real_uow_client.post(
        f"{PREFIX}/nolog?wait=true", headers=_key(headers, "l1")
    )
    assert again.status_code == 200 and again.json() == {"n": 2}
    assert await _rows(db_engine) == ["nolog", "nolog"]
    runs.clear()
    for _ in range(2):
        await real_uow_client.post(f"{PREFIX}/opted-out", headers=_key(headers, "o1"))
    assert runs == [1, 1]


async def test_mode_x_re_runs_authorization_and_the_handler(
    real_uow_client: Any, table: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX)
    runs: list[int] = []
    guard = Depends(
        di.idempotency_guard(scope="wsa.x", fail_closed=True, store_response=False)
    )

    @router.post("/x", dependencies=[Depends(require(Role("writer"))), guard])
    async def x() -> dict[str, int]:
        runs.append(1)
        return {"n": len(runs)}

    mount(router)
    uid, headers = make_user_headers(uuid.uuid4(), roles=["writer"])
    assert (
        await real_uow_client.post(f"{PREFIX}/x", headers=_key(headers, "x1"))
    ).json() == {"n": 1}
    assert (
        await real_uow_client.post(f"{PREFIX}/x", headers=_key(headers, "x1"))
    ).json() == {"n": 2}
    _uid, demoted = make_user_headers(uid, roles=[])
    assert (
        await real_uow_client.post(f"{PREFIX}/x", headers=_key(demoted, "x1"))
    ).status_code == 403


async def test_the_snapshot_records_the_mode() -> None:
    from sandloom.core import route_snapshots

    app = FastAPI()

    async def ok() -> None:
        return None

    app.add_api_route("/s", ok, methods=["POST"], dependencies=USER)
    app.add_api_route(
        "/b", ok, methods=["POST"], dependencies=[*USER, di.replay_safe(reason="echo")]
    )
    app.add_api_route(
        "/n",
        ok,
        methods=["POST"],
        dependencies=[*USER, di.no_idempotency(reason="sse")],
    )
    modes = {
        e["path"]: e["policy"]["idempotency"]
        for e in route_snapshots.route_entries(app.routes)
    }
    assert modes == {
        "/s": "automatic S",
        "/b": "automatic B (echo)",
        "/n": "none (sse)",
    }
