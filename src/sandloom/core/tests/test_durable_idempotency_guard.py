"""``idempotency_guard(durable=True)`` — the durable HTTP form (ADR-0005).

The record is claimed in the REQUEST's own Unit of Work and completed with the
response right before ``COMMIT``, so it commits iff the endpoint's effect
commits. Every test runs through the real request UoW on PostgreSQL (no
``get_db`` override): replay, ``422`` on a reused key, ``409`` while the first
request is in flight (never a wait), no record after a rollback / a failing
before-commit check / a returned ``5xx``, the ambiguous COMMIT (the
acknowledgement lost after the commit, and a commit that never happened), the
caller + tenant scope, the replay size cap, anonymous status-only replay, and
the ``create_app`` refusal of a replay-only guard on an ``F`` route.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text

from sandloom.core import effects, idempotency
from sandloom.core.di import DbSession, before_commit, record_audit, sensitive
from sandloom.core.idempotency import durable as idempotency_durable
from sandloom.core.idempotency import replay as idempotency_replay
from sandloom.core.route_policy import CapabilityReplayError
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.models.http_idempotency import HttpIdempotencyRecord
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks.relay import purge_expired_requests
from sandloom.core.testing import make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403 - DB/app fixtures for this module
from sandloom.core.testing.infra import override_instance
from sandloom.shared.guards.auth import get_current_user_id

pytestmark = pytest.mark.usefixtures("db_engine")

_ACTION = "test.durable.charge"


class _App:
    """A bare app on the platform's CoreFacade with durable-guarded routes."""

    def __init__(self, core: Any) -> None:
        self.runs = 0
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        app = FastAPI()
        app.state.core = core
        app.add_exception_handler(
            idempotency_replay._ReplayResponse, idempotency_replay._replay_handler
        )
        router = APIRouter(dependencies=[sensitive("money")])
        guard = idempotency.idempotency_guard(scope="test.charges", durable=True)
        owner = self

        @router.post("/charge")
        async def charge(
            request: Request,
            db: DbSession,
            user_id: str = Depends(get_current_user_id),
            _key: str | None = Depends(guard),
        ) -> Any:
            body = await request.json()
            owner.runs += 1
            owner.started.set()
            if owner.gate is not None:
                await owner.gate.wait()
            record_audit(
                _ACTION,
                actor_id=uuid.UUID(str(user_id)),
                resource_type="charge",
                resource_id=str(body.get("ref", "-")),
                data={"amount": body.get("amount")},
            )
            outcome = body.get("outcome")
            if outcome == "raise":
                raise HTTPException(400, "refused")
            if outcome == "check_fails":

                def _deny() -> None:
                    raise HTTPException(401, "revoked at commit")

                await before_commit(_deny)
            if outcome == "returned_4xx":
                return JSONResponse({"error": "declined"}, status_code=402)
            if outcome == "returned_5xx":
                return JSONResponse({"error": "upstream"}, status_code=502)
            if outcome == "big":
                return {"blob": "x" * (effects.RESULT_MAX_BYTES + 1)}
            if outcome == "stream":
                return StreamingResponse(iter([b"a", b"b"]), media_type="text/plain")
            return {"charged": body.get("amount"), "run": owner.runs}

        app.include_router(router)

        @app.post("/anon")
        async def anon(
            db: DbSession,
            _key: str | None = Depends(
                idempotency.idempotency_guard(scope="test.anon", durable=True)
            ),
        ) -> dict[str, int]:
            owner.runs += 1
            return {"run": owner.runs}

        idempotency_durable.install_durable_capture(app.routes)

        @app.post("/late")  # added after the capture was installed
        async def late(
            db: DbSession,
            user_id: str = Depends(get_current_user_id),
            _key: str | None = Depends(
                idempotency.idempotency_guard(scope="test.late", durable=True)
            ),
        ) -> dict[str, int]:
            owner.runs += 1
            return {"run": owner.runs}

        self.app = app


@pytest_asyncio.fixture
async def durable(sandloom_core: Any) -> AsyncIterator[tuple[_App, httpx.AsyncClient]]:
    async with AsyncExitStack() as stack:
        for capability, env, driver in (
            ("database", "DATABASE_URL", "sqlalchemy"),
            ("cache", "CACHE_URL", "redis"),
        ):
            await stack.enter_async_context(
                override_instance(
                    sandloom_core, capability, driver=driver, url=os.environ[env]
                )
            )
        target = _App(sandloom_core)
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=target.app, raise_app_exceptions=False
                ),
                base_url="http://test",
            )
        )
        yield target, http


async def _count(db_engine: Any, model: Any, *where: Any) -> int:
    async with db_engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    select(func.count()).select_from(model).where(*where)
                )
            ).scalar_one()
        )


async def _effects(db_engine: Any) -> int:
    return await _count(db_engine, AuditLogEntry, AuditLogEntry.action == _ACTION)


async def _records(db_engine: Any) -> int:
    return await _count(db_engine, HttpIdempotencyRecord)


def _headers(
    key: str, user: uuid.UUID | None = None, tenant: uuid.UUID | None = None
) -> dict[str, str]:
    _, headers = make_user_headers(user_id=user or _USER, tenant_id=tenant or _TENANT)
    return {**headers, "Idempotency-Key": key}


_USER = uuid.uuid4()
_TENANT = uuid.uuid4()


async def test_replay_returns_the_stored_response_and_runs_once(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    target, http = durable
    first = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    second = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json() == {"charged": 5, "run": 1}
    assert second.headers["Idempotent-Replay"] == "true"
    assert "Idempotent-Replay" not in first.headers
    assert target.runs == 1
    assert await _effects(db_engine) == 1
    assert await _records(db_engine) == 1


async def test_same_key_other_request_is_422(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    target, http = durable
    assert (
        await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    ).status_code == 200
    reused = await http.post("/charge", json={"amount": 6}, headers=_headers("k1"))
    assert reused.status_code == 422
    assert target.runs == 1 and await _effects(db_engine) == 1


async def test_missing_key_is_400(durable: tuple[_App, httpx.AsyncClient]) -> None:
    _, http = durable
    headers = _headers("k")
    del headers["Idempotency-Key"]
    assert (await http.post("/charge", json={}, headers=headers)).status_code == 400


async def test_concurrent_in_flight_duplicate_is_409_at_once(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    """The second request never waits on the first one's uncommitted claim."""
    target, http = durable
    target.gate = asyncio.Event()
    first = asyncio.create_task(
        http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    )
    await asyncio.wait_for(target.started.wait(), 10)
    loop = asyncio.get_running_loop()
    started = loop.time()
    racer = await asyncio.wait_for(
        http.post("/charge", json={"amount": 5}, headers=_headers("k1")), 5
    )
    assert racer.status_code == 409
    assert loop.time() - started < 2.0
    target.gate.set()
    assert (await first).status_code == 200
    target.gate = None
    after = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert after.status_code == 200 and after.headers["Idempotent-Replay"] == "true"
    assert target.runs == 1 and await _effects(db_engine) == 1


@pytest.mark.parametrize("outcome", ["raise", "check_fails"])
async def test_rolled_back_request_leaves_no_record(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any, outcome: str
) -> None:
    """A raised error or a failing before-commit check rolls the UoW back:
    neither the effect nor the record survives, so the retry runs afresh."""
    target, http = durable
    failed = await http.post(
        "/charge", json={"amount": 5, "outcome": outcome}, headers=_headers("k1")
    )
    assert failed.status_code in {400, 401}
    assert await _effects(db_engine) == 0 and await _records(db_engine) == 0
    retry = await http.post(
        "/charge", json={"amount": 5, "outcome": outcome}, headers=_headers("k1")
    )
    assert retry.status_code == failed.status_code
    assert "Idempotent-Replay" not in retry.headers
    assert target.runs == 2


async def test_returned_4xx_is_the_committed_outcome_and_replays(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    """A RETURNED 4xx commits (unlike a raised one): its effect and record
    are durable, and the retry gets the same answer instead of a re-run."""
    target, http = durable
    body = {"amount": 5, "outcome": "returned_4xx"}
    first = await http.post("/charge", json=body, headers=_headers("k1"))
    second = await http.post("/charge", json=body, headers=_headers("k1"))
    assert first.status_code == second.status_code == 402
    assert second.json() == {"error": "declined"}
    assert second.headers["Idempotent-Replay"] == "true"
    assert target.runs == 1 and await _effects(db_engine) == 1


async def test_returned_5xx_is_never_replayed(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    target, http = durable
    body = {"amount": 5, "outcome": "returned_5xx"}
    first = await http.post("/charge", json=body, headers=_headers("k1"))
    assert first.status_code == 502
    assert await _records(db_engine) == 0
    second = await http.post("/charge", json=body, headers=_headers("k1"))
    assert second.status_code == 502 and "Idempotent-Replay" not in second.headers
    assert target.runs == 2


async def test_ambiguous_commit_that_committed_is_replayed_not_rerun(
    durable: tuple[_App, httpx.AsyncClient],
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection drops while acknowledging COMMIT: the client sees a
    5xx, but the record committed with the effect — the retry replays."""
    target, http = durable
    original = SQLAlchemyUnitOfWork.commit
    lost: list[bool] = []

    async def commit_then_lose_ack(self: SQLAlchemyUnitOfWork) -> None:
        await original(self)
        if not lost:
            lost.append(True)
            raise ConnectionResetError("connection lost while committing")

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", commit_then_lose_ack)
    first = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert first.status_code == 500
    retry = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert retry.status_code == 200
    assert retry.headers["Idempotent-Replay"] == "true"
    assert retry.json() == {"charged": 5, "run": 1}
    assert target.runs == 1 and await _effects(db_engine) == 1


async def test_ambiguous_commit_that_did_not_commit_runs_afresh(
    durable: tuple[_App, httpx.AsyncClient],
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMMIT fails before it reached the database: no effect and no record,
    so the retry runs — once."""
    target, http = durable
    original = SQLAlchemyUnitOfWork.commit
    failed: list[bool] = []

    async def lose_before_commit(self: SQLAlchemyUnitOfWork) -> None:
        if not failed:
            failed.append(True)
            raise ConnectionResetError("connection lost before COMMIT")
        await original(self)

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", lose_before_commit)
    first = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert first.status_code == 500
    assert await _effects(db_engine) == 0 and await _records(db_engine) == 0
    retry = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert retry.status_code == 200 and "Idempotent-Replay" not in retry.headers
    assert target.runs == 2 and await _effects(db_engine) == 1


async def test_key_is_scoped_to_caller_and_tenant(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any
) -> None:
    target, http = durable
    other_user, other_tenant = uuid.uuid4(), uuid.uuid4()
    for headers in (
        _headers("k1"),
        _headers("k1", user=other_user),
        _headers("k1", tenant=other_tenant),
    ):
        response = await http.post("/charge", json={"amount": 5}, headers=headers)
        assert response.status_code == 200
        assert "Idempotent-Replay" not in response.headers
    assert target.runs == 3 and await _records(db_engine) == 3
    async with db_engine.connect() as conn:
        tenants = set(
            (await conn.execute(select(HttpIdempotencyRecord.tenant_id))).scalars()
        )
    assert tenants == {str(_TENANT), str(other_tenant)}


@pytest.mark.parametrize("outcome", ["big", "stream"])
async def test_unreplayable_response_replays_status_only(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any, outcome: str
) -> None:
    """Over the cap (or streamed): never re-run, the retry gets the status."""
    target, http = durable
    body = {"amount": 5, "outcome": outcome}
    first = await http.post("/charge", json=body, headers=_headers("k1"))
    assert first.status_code == 200
    second = await http.post("/charge", json=body, headers=_headers("k1"))
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.headers["Idempotent-Replay-Truncated"] == "true"
    assert second.content == b""
    assert target.runs == 1
    async with db_engine.connect() as conn:
        status = (await conn.execute(select(HttpIdempotencyRecord.status))).scalar_one()
    assert status == effects.NOT_REPLAYABLE


async def test_anonymous_caller_gets_status_only(
    durable: tuple[_App, httpx.AsyncClient],
) -> None:
    target, http = durable
    first = await http.post("/anon", headers={"Idempotency-Key": "a1"})
    second = await http.post("/anon", headers={"Idempotency-Key": "a1"})
    assert first.status_code == second.status_code == 200
    assert first.json() == {"run": 1}
    assert second.headers["Idempotent-Replay-Truncated"] == "true"
    assert target.runs == 1


async def test_route_without_capture_is_refused_not_rerun(
    durable: tuple[_App, httpx.AsyncClient],
) -> None:
    target, http = durable
    assert (await http.post("/late", headers=_headers("l1"))).status_code == 200
    again = await http.post("/late", headers=_headers("l1"))
    assert again.status_code == 409
    assert target.runs == 1


async def test_expired_record_is_taken_over_and_purged(
    durable: tuple[_App, httpx.AsyncClient], db_engine: Any, sandloom_core: Any
) -> None:
    target, http = durable
    assert (
        await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    ).status_code == 200
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE http_idempotency_log SET expires_at = now() - interval '1s'")
        )
    rerun = await http.post("/charge", json={"amount": 5}, headers=_headers("k1"))
    assert rerun.status_code == 200 and "Idempotent-Replay" not in rerun.headers
    assert target.runs == 2
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE http_idempotency_log SET expires_at = now() - interval '1s'")
        )
    factory = sandloom_core.database("default").uow_factory()
    assert await purge_expired_requests(sandloom_core, "default", factory) == 1
    assert await _records(db_engine) == 0


# --------------------------------------------------------------------------- #
# create_app: a money route may not rely on a replay-only guard (SANDLOOM-B019;
# the node ids keep their class-F names, ledger G-IDEM-13 / G-GUARD-03 ->
# G-SENS-05)
# --------------------------------------------------------------------------- #


def _f_app(*guards: Callable[..., Any], money: bool = True) -> FastAPI:
    app = FastAPI()
    router = APIRouter(dependencies=[sensitive("money")] if money else [])
    for index, guard in enumerate(guards):

        @router.post(f"/r{index}", dependencies=[Depends(guard)])
        async def _route() -> None:
            return None

    app.include_router(router)
    return app


def test_f_route_refuses_a_replay_only_guard() -> None:
    cache_guard = idempotency.idempotency_guard(scope="s", fail_closed=True)
    fail_open = idempotency.idempotency_guard(scope="s", store_response=False)
    problems = idempotency_durable.money_guard_problems(
        _f_app(cache_guard, fail_open).routes
    )
    assert len(problems) == 2
    assert "POST /r0" in problems[0] and "cache (replay-only)" in problems[0]
    assert "POST /r1" in problems[1] and "fail-open in-flight" in problems[1]
    with pytest.raises(CapabilityReplayError, match="SANDLOOM-B019"):
        idempotency_durable.validate_money_guards(_f_app(cache_guard).routes)


def test_f_route_accepts_durable_and_endpoint_owned_guards() -> None:
    durable = idempotency.idempotency_guard(scope="s", durable=True)
    owned = idempotency.idempotency_guard(
        scope="s", store_response=False, fail_closed=True
    )
    assert idempotency_durable.money_guard_problems(_f_app(durable, owned).routes) == []
    # a money route without any guard: its money effect needs its own dedupe
    # record or the client's key at run time (SANDLOOM-R009 / SANDLOOM-R010)
    assert idempotency_durable.money_guard_problems(_f_app().routes) == []
    # a cache guard stays fine on a route without the money risk
    cache_guard = idempotency.idempotency_guard(scope="s")
    assert (
        idempotency_durable.money_guard_problems(
            _f_app(cache_guard, money=False).routes
        )
        == []
    )


def test_bundled_f_routes_pass_and_the_platform_wraps_no_route(
    sandloom_app: Any,
) -> None:
    assert idempotency_durable.money_guard_problems(sandloom_app.routes) == []


def test_durable_guard_mode_and_misuse() -> None:
    durable = idempotency.idempotency_guard(scope="s", durable=True)
    assert idempotency_durable.guard_mode(durable) == idempotency_durable.GuardMode(
        "durable", True
    )
    assert idempotency_durable.guard_mode(
        idempotency.idempotency_guard(scope="s", store_response=False)
    ) == idempotency_durable.GuardMode("in_flight", False)
    assert idempotency_durable.guard_mode(object()) is None
    with pytest.raises(ValueError, match="store_response"):
        idempotency.idempotency_guard(scope="s", durable=True, store_response=False)


async def test_route_added_after_create_app_is_captured(
    sandloom_app: Any, real_uow_client: httpx.AsyncClient, db_engine: Any
) -> None:
    """The route index wraps a late durable route before it serves: its very
    first response is stored and the retry replays it."""
    router = APIRouter()
    runs: list[int] = []

    @router.post("/_test/durable-late")
    async def late(
        db: DbSession,
        user_id: str = Depends(get_current_user_id),
        _key: str | None = Depends(
            idempotency.idempotency_guard(scope="test.late_app", durable=True)
        ),
    ) -> dict[str, int]:
        runs.append(1)
        return {"run": len(runs)}

    saved = list(sandloom_app.router.routes)
    sandloom_app.include_router(router)
    try:
        first = await real_uow_client.post(
            "/_test/durable-late", headers=_headers("x1")
        )
        again = await real_uow_client.post(
            "/_test/durable-late", headers=_headers("x1")
        )
    finally:
        sandloom_app.router.routes[:] = saved
    assert first.status_code == again.status_code == 200, again.text
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json() == {"run": 1} and runs == [1]
