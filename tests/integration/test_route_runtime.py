"""The per-route runtime of model E over HTTP, real PostgreSQL and Redis
(spec §4.2, §4.5, §4.6; ADR-0009 items 3-4).

Independent oracles (spec §10.1): rows in the probe table and the HTTP
status, with two real actors racing (a revocation / block landing while the
handler runs, from another context):

* the commit-time re-check on a write that declares NOTHING; on a GET whose
  unit wrote (armed; ``on_commit`` kept); not on a GET that only read;
* a foreign revocation between two commits of one request: the second
  commit refuses;
* self-revocation, "check, then revoke": a route revoking its own subject
  commits; a foreign revocation landing BEFORE the self-revoking call is
  still refused; a self-revocation inside a rolled-back savepoint
  contributes nothing (the commit is checked and refused);
* ``Cache-Control: no-store`` on user routes unless the route sets its own;
* operation-intent mode: a 2xx without ``release_check()`` is discarded
  (SANDLOOM-R012), two write statements after it too; a child task opening a unit
  during ``external_call()`` is refused (SANDLOOM-R008); no billing connection is
  checked out inside the external call or the authority callback;
  ``release_check`` for an organization without authority is SANDLOOM-R012;
  ``db()`` on an intent route is SANDLOOM-R002;
* SANDLOOM-B019 at startup.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Response
from prometheus_client import REGISTRY
from sqlalchemy import Integer, String, UniqueConstraint, insert, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import di
from sandloom.core.auth import marks
from sandloom.core.http import route_runtime
from sandloom.core.route_policy import CapabilityReplayError
from sandloom.core.testing import make_user_headers
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError
from sandloom.core.unit_of_work.intent import IntentReleaseError, ResolvedAccount
from sandloom.core.unit_of_work.request import UnitRuleError
from sandloom.shared.authz import current_context

pytestmark = pytest.mark.asyncio

TABLE = "wsa_runtime_probe"
PREFIX = "/__wsa"


class _Base(DeclarativeBase):
    pass


class Probe(_Base):
    __tablename__ = TABLE

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def probe_table(db_engine: Any) -> AsyncIterator[None]:
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
    """Mount test routers on the platform app, through the route runtime."""
    before = list(sandloom_app.router.routes)

    def _mount(router: APIRouter) -> None:
        sandloom_app.include_router(router)
        new = [r for r in sandloom_app.router.routes if r not in before]
        route_runtime.install_route_runtime(new, sandloom_app.state.core)

    yield _mount
    sandloom_app.router.routes[:] = before


async def _elsewhere(coro: Any) -> Any:
    """Another actor: its own context, never the request's."""
    return await asyncio.create_task(coro, context=contextvars.Context())


async def _revoke(cache: Any, sub: Any) -> None:
    await cache.set(
        marks.subject_key(sub),
        marks._format_mark(marks._seconds_to_us(time.time() + 1)),
        ttl_seconds=120,
    )


async def _block(cache: Any, sub: Any) -> None:
    await cache.set(
        marks.subject_key(sub), marks._format_blocked(marks._seconds_to_us(time.time()))
    )


USER = [Depends(current_context)]


# --------------------------------------------------------------------------- #
# the commit-time re-check (spec §4.5, G-REVOKE-22)
# --------------------------------------------------------------------------- #
async def test_a_block_during_a_write_that_declares_nothing_refuses_and_rolls_back(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    uid, headers = make_user_headers(uuid.uuid4())

    @router.post("/write", dependencies=USER)
    async def write() -> dict[str, bool]:
        di.db().add(Probe(v="refused"))
        await di.db().flush()
        await _elsewhere(_block(sandloom_cache, uid))  # lands while the handler runs
        return {"ok": True}

    mount(router)
    resp = await real_uow_client.post(f"{PREFIX}/write", headers=headers)
    assert resp.status_code == 401 and resp.json()["detail"] == "Account blocked"
    assert await _rows(db_engine) == []


async def test_a_get_that_writes_is_rechecked_and_a_reading_get_is_not(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    ran: list[str] = []

    @router.get("/writing-get", dependencies=USER)
    async def writing_get(fine: bool = False) -> dict[str, bool]:
        await di.db().execute(insert(Probe).values(v=f"get:{fine}"))
        await di.on_commit(lambda: ran.append("on_commit"))
        if not fine:
            uid = await _subject(di.db())
            await _elsewhere(_revoke(sandloom_cache, uid))
        return {"ok": True}

    @router.get("/reading-get", dependencies=USER)
    async def reading_get() -> dict[str, bool]:
        await di.db().execute(text("SELECT 1"))
        await _elsewhere(_revoke(sandloom_cache, _CURRENT[0]))
        return {"ok": True}

    mount(router)
    uid, headers = make_user_headers(uuid.uuid4())
    _CURRENT[:] = [uid]
    ok = await real_uow_client.get(f"{PREFIX}/writing-get?fine=true", headers=headers)
    assert ok.status_code == 200 and ran == ["on_commit"]  # queued effect kept
    refused = await real_uow_client.get(f"{PREFIX}/writing-get", headers=headers)
    assert refused.status_code == 401
    assert await _rows(db_engine) == ["get:True"]

    uid2, headers2 = make_user_headers(uuid.uuid4())
    _CURRENT[:] = [uid2]
    read = await real_uow_client.get(f"{PREFIX}/reading-get", headers=headers2)
    assert read.status_code == 200  # nothing commits: no automatic re-check


_CURRENT: list[Any] = []


async def _subject(_session: Any) -> Any:
    return _CURRENT[0]


async def test_a_foreign_revocation_between_two_commits_refuses_the_second(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    uid, headers = make_user_headers(uuid.uuid4())
    committed: list[str] = []

    @router.post("/two-commits", dependencies=USER)
    async def two_commits() -> dict[str, bool]:
        async with di.db_session():  # commits a queued effect: checked, passes
            await di.on_commit(lambda: committed.append("first"))
        await _elsewhere(_revoke(sandloom_cache, uid))
        await di.db().execute(insert(Probe).values(v="second"))
        return {"ok": True}

    mount(router)
    resp = await real_uow_client.post(f"{PREFIX}/two-commits", headers=headers)
    assert committed == ["first"]
    assert resp.status_code == 401
    assert await _rows(db_engine) == []


# --------------------------------------------------------------------------- #
# self-revocation, "check, then revoke" (spec §4.5, G-REVOKE-23)
# --------------------------------------------------------------------------- #
async def test_a_route_revoking_its_own_subject_commits(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    uid, headers = make_user_headers(uuid.uuid4())

    @router.post("/change-password", dependencies=USER)
    async def change_password() -> dict[str, bool]:
        await di.db().execute(insert(Probe).values(v="password changed"))
        await di.revoke_user_tokens_on_commit(
            uid, reason="password changed", require_immediate=True
        )
        return {"ok": True}

    mount(router)
    resp = await real_uow_client.post(f"{PREFIX}/change-password", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _rows(db_engine) == ["password changed"]
    # ... and the old token is revoked from now on
    again = await real_uow_client.post(f"{PREFIX}/change-password", headers=headers)
    assert again.status_code == 401


async def test_a_foreign_revocation_before_the_self_revoking_call_is_still_seen(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    uid, headers = make_user_headers(uuid.uuid4())

    @router.post("/self-revoke-late", dependencies=USER)
    async def self_revoke_late() -> dict[str, bool]:
        await di.db().execute(insert(Probe).values(v="refused"))
        await _elsewhere(_block(sandloom_cache, uid))  # someone else, first
        await di.revoke_user_tokens_on_commit(
            uid, reason="self", require_immediate=True
        )
        return {"ok": True}

    mount(router)
    resp = await real_uow_client.post(f"{PREFIX}/self-revoke-late", headers=headers)
    assert resp.status_code == 401
    assert await _rows(db_engine) == []


async def test_a_self_revocation_in_a_rolled_back_savepoint_contributes_nothing(
    real_uow_client: Any,
    sandloom_cache: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = APIRouter(prefix=PREFIX)
    uid, headers = make_user_headers(uuid.uuid4())

    @router.post("/savepoint", dependencies=USER)
    async def savepoint() -> dict[str, bool]:
        session = di.db()
        await session.execute(insert(Probe).values(v="kept?"))
        nested = await session.begin_nested()
        # the early (require_immediate) mark is written; the self-revocation
        # record lives in the savepoint ...
        await di.revoke_user_tokens_on_commit(
            uid, reason="self", require_immediate=True
        )
        await nested.rollback()  # ... which rolls back: the record is gone
        return {"ok": True}

    mount(router)
    resp = await real_uow_client.post(f"{PREFIX}/savepoint", headers=headers)
    # the commit is checked normally and sees the (non-transactional) mark
    assert resp.status_code == 401
    assert await _rows(db_engine) == []


# --------------------------------------------------------------------------- #
# no-store (ADR-0009 item 4, G-HTTP-12)
# --------------------------------------------------------------------------- #
async def test_user_routes_are_no_store_unless_they_set_cache_control(
    real_uow_client: Any, probe_table: None, mount: Any
) -> None:
    router = APIRouter(prefix=PREFIX)

    @router.get("/private", dependencies=USER)
    async def private() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/own-cache", dependencies=USER)
    async def own_cache(response: Response) -> dict[str, bool]:
        response.headers["Cache-Control"] = "private, max-age=5"
        return {"ok": True}

    @router.get("/open", dependencies=[di.public(reason="test")])
    async def open_() -> dict[str, bool]:
        return {"ok": True}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    private_r = await real_uow_client.get(f"{PREFIX}/private", headers=headers)
    assert private_r.headers["cache-control"] == "no-store"
    own = await real_uow_client.get(f"{PREFIX}/own-cache", headers=headers)
    assert own.headers["cache-control"] == "private, max-age=5"


# --------------------------------------------------------------------------- #
# operation-intent mode (spec §4.6, G-UNIT-02, G-BILLING-19)
# --------------------------------------------------------------------------- #
def _intent_router() -> APIRouter:
    return APIRouter(
        prefix=PREFIX,
        dependencies=[
            *USER,
            di.operation_intent(reason="test PSP flow"),
        ],
    )


def _guard(name: str) -> Any:
    return Depends(
        di.idempotency_guard(scope=name, fail_closed=True, store_response=False)
    )


def _checked_out(sandloom_core: Any) -> int:
    return int(sandloom_core.database("default").engine.sync_engine.pool.checkedout())


@pytest.mark.sandloom_allow_server_errors
async def test_an_intent_2xx_without_release_check_is_discarded(
    real_uow_client: Any, probe_table: None, mount: Any
) -> None:
    router = _intent_router()

    @router.post("/no-release", dependencies=[_guard("t.no_release")])
    async def no_release() -> dict[str, str]:
        async with di.external_call("psp"):
            pass
        async with di.db_session() as s:
            await s.execute(insert(Probe).values(v="issued"))
        return {"url": "https://psp/secret"}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    resp = await real_uow_client.post(
        f"{PREFIX}/no-release", headers={**headers, "Idempotency-Key": "k1"}
    )
    assert resp.status_code == 500
    assert "secret" not in resp.text


@pytest.mark.sandloom_allow_server_errors
async def test_two_writes_after_the_release_check_are_discarded(
    real_uow_client: Any, probe_table: None, mount: Any
) -> None:
    router = _intent_router()

    @router.post("/two-writes", dependencies=[_guard("t.two_writes")])
    async def two_writes() -> dict[str, str]:
        async with di.external_call("psp"):
            pass
        await di.release_check(ResolvedAccount(kind="user", id=uuid.uuid4()))
        async with di.db_session() as s:
            await s.execute(insert(Probe).values(v="a"))
            await s.execute(insert(Probe).values(v="b"))
        return {"url": "https://psp/secret"}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    resp = await real_uow_client.post(
        f"{PREFIX}/two-writes", headers={**headers, "Idempotency-Key": "k2"}
    )
    assert resp.status_code == 500 and "secret" not in resp.text


async def test_the_issue_and_issued_retry_cases_pass_and_hold_no_connection(
    real_uow_client: Any,
    sandloom_core: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
) -> None:
    router = _intent_router()
    seen: dict[str, Any] = {}

    async def authority() -> None:
        seen["in_authority"] = _checked_out(sandloom_core)

        async def child() -> None:
            async with di.db_session():
                pass

        try:
            await asyncio.create_task(child())
        except UnitRuleError:
            seen["child_during_release"] = "refused"

    @router.post("/issue", dependencies=[_guard("t.issue")])
    async def issue(retry: bool = False) -> dict[str, str]:
        async with di.db_session() as s:  # reserve
            await s.execute(insert(Probe).values(v=f"reserve:{retry}"))
        if not retry:
            async with di.external_call("psp"):
                seen["in_psp"] = _checked_out(sandloom_core)

                async def child() -> None:
                    async with di.db_session():
                        pass

                try:
                    await asyncio.create_task(child())
                except UnitRuleError:
                    seen["child_during_psp"] = "refused"
        await di.release_check(
            ResolvedAccount(kind="org", id=uuid.uuid4()), authority=authority
        )
        if not retry:
            async with di.db_session() as s:  # issue: exactly one write
                await s.execute(
                    text(f"UPDATE {TABLE} SET v = 'issued' WHERE v = 'reserve:False'")
                )
        return {"url": "https://psp/session"}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    first = await real_uow_client.post(
        f"{PREFIX}/issue", headers={**headers, "Idempotency-Key": "k3"}
    )
    assert first.status_code == 200, first.text
    assert seen == {
        "in_psp": 0,
        "child_during_psp": "refused",
        "in_authority": 0,
        "child_during_release": "refused",
    }
    retry = await real_uow_client.post(
        f"{PREFIX}/issue?retry=true", headers={**headers, "Idempotency-Key": "k4"}
    )
    assert retry.status_code == 200, retry.text
    assert await _rows(db_engine) == ["issued", "reserve:True"]


async def test_release_check_for_an_org_needs_the_authority_and_db_is_refused(
    real_uow_client: Any, probe_table: None, mount: Any
) -> None:
    router = _intent_router()
    outcome: list[str] = []

    @router.post("/org", dependencies=[_guard("t.org")])
    async def org() -> Response:
        try:
            await di.release_check(ResolvedAccount(kind="org", id=uuid.uuid4()))
        except IntentReleaseError:
            outcome.append("R012")
        try:
            di.db()
        except UnitOfWorkRequiredError as exc:
            outcome.append(type(exc).__name__)
        return Response(status_code=409)

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    resp = await real_uow_client.post(
        f"{PREFIX}/org", headers={**headers, "Idempotency-Key": "k5"}
    )
    assert resp.status_code == 409
    assert outcome == ["R012", "UnitOfWorkRequiredError"]


async def test_capability_replay_checks_at_startup() -> None:
    """SANDLOOM-B019: an intent route needs the explicit mode-X guard and takes no
    request session; a token issuer's guard must be mode X."""
    app = FastAPI()

    async def ok() -> dict[str, str]:
        return {}

    intent = di.operation_intent(reason="t")
    app.add_api_route("/no-guard", ok, methods=["POST"], dependencies=[intent])
    app.add_api_route(
        "/with-session",
        ok,
        methods=["POST"],
        dependencies=[intent, _guard("t.s"), Depends(di.get_db)],
    )
    app.add_api_route(
        "/issuer-replays",
        ok,
        methods=["POST"],
        dependencies=[
            di.public(reason="login", issues_tokens=True),
            Depends(di.idempotency_guard(scope="t.login")),
        ],
    )
    app.add_api_route(
        "/fine",
        ok,
        methods=["POST"],
        dependencies=[intent, _guard("t.fine"), *USER],
    )
    problems = route_runtime.capability_replay_problems(app.routes)
    joined = "\n".join(problems)
    assert "POST /no-guard: an operation-intent route needs" in joined
    assert (
        "POST /with-session: an operation-intent route takes no request session"
        in joined
    )
    assert "POST /issuer-replays: a token issuer may only carry" in joined
    assert "/fine" not in joined
    with pytest.raises(CapabilityReplayError, match="SANDLOOM-B019"):
        route_runtime.validate_capability_replay(app.routes)


# --------------------------------------------------------------------------- #
# allow_multi_db_write: secondaries commit first, the write store last
# (docs/concepts/write-admission.md rule 4), whatever the parameter order
# --------------------------------------------------------------------------- #
@pytest.fixture
async def analytics(sandloom_app: FastAPI) -> AsyncIterator[list[str]]:
    """A second database ``analytics`` (the test database, its own engine);
    yields the order in which the two databases COMMIT (any other engine's
    commit is the write store's: the request unit may use the app's own
    ``default`` engine)."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    core = sandloom_app.state.core
    commits: list[str] = []
    async with override_instance(
        core,
        "database",
        "analytics",
        driver="sqlalchemy",
        url=os.environ["DATABASE_URL"],
    ):
        pool = core.database("analytics").engine.sync_engine.pool

        def hook(conn: Any) -> None:
            commits.append("analytics" if conn.engine.pool is pool else "default")

        event.listen(Engine, "commit", hook)
        try:
            yield commits
        finally:
            event.remove(Engine, "commit", hook)


MULTI = [
    *USER,
    di.writes("default"),
    di.allow_multi_db_write(secondaries=("analytics",), reason="rollup copy"),
]


async def test_the_secondary_commits_before_the_write_store_whatever_the_order(
    real_uow_client: Any,
    db_engine: Any,
    probe_table: None,
    mount: Any,
    analytics: list[str],
) -> None:
    """The documented best-effort 1PC: the secondary commits first, the write
    store last, independent of the endpoint's parameter order and of which
    database is written first; and a write to the secondary BEFORE the write
    store is admitted (the documented pattern, not SANDLOOM-R007). A dependency
    opening both sessions itself, in either order and at any depth, before or
    after the endpoint's own secondary, is ordered the same way."""
    router = APIRouter(prefix=PREFIX)
    secondary = di.Database("analytics")

    @router.post("/md/secondary-param-first", dependencies=MULTI)
    async def secondary_param_first(
        sec: Any = secondary,
        db: di.DbSession = None,  # type: ignore[assignment]
    ) -> dict[str, bool]:
        await db.execute(insert(Probe).values(v="p1"))
        await sec.execute(insert(Probe).values(v="s1"))
        return {"ok": True}

    @router.post("/md/primary-param-first", dependencies=MULTI)
    async def primary_param_first(
        db: di.DbSession, sec: Any = secondary
    ) -> dict[str, bool]:
        await db.execute(insert(Probe).values(v="p2"))
        await sec.execute(insert(Probe).values(v="s2"))
        return {"ok": True}

    @router.post("/md/secondary-written-first", dependencies=MULTI)
    async def secondary_written_first(
        db: di.DbSession, sec: Any = secondary
    ) -> dict[str, bool]:
        await sec.execute(insert(Probe).values(v="s3"))
        await db.execute(insert(Probe).values(v="p3"))
        return {"ok": True}

    @router.post("/md/secondary-first-both-ways", dependencies=MULTI)
    async def secondary_first_both_ways(
        sec: Any = secondary,
        db: di.DbSession = None,  # type: ignore[assignment]
    ) -> dict[str, bool]:
        await sec.execute(insert(Probe).values(v="s4"))
        await db.execute(insert(Probe).values(v="p4"))
        return {"ok": True}

    # nested: a dependency that itself injects both sessions, in either
    # order, next to (before or after) the endpoint's own secondary
    async def secondary_then_store(
        sec: Any = secondary,
        db: di.DbSession = None,  # type: ignore[assignment]
    ) -> tuple[Any, Any]:
        return sec, db

    async def store_then_secondary(
        db: di.DbSession, sec: Any = secondary
    ) -> tuple[Any, Any]:
        return sec, db

    async def outer(both: Any = Depends(secondary_then_store)) -> tuple[Any, Any]:
        return both  # type: ignore[no-any-return]

    async def _write_both(both: tuple[Any, Any], tag: str) -> None:
        sec, db = both
        await sec.execute(insert(Probe).values(v=f"s{tag}"))
        await db.execute(insert(Probe).values(v=f"p{tag}"))

    @router.post("/md/nested-secondary-store", dependencies=MULTI)
    async def nested_secondary_store(
        sec: Any = secondary, both: Any = Depends(secondary_then_store)
    ) -> dict[str, bool]:
        await _write_both(both, "5")
        return {"ok": True}

    @router.post("/md/nested-store-secondary", dependencies=MULTI)
    async def nested_store_secondary(
        sec: Any = secondary, both: Any = Depends(store_then_secondary)
    ) -> dict[str, bool]:
        await _write_both(both, "6")
        return {"ok": True}

    @router.post("/md/nested-first", dependencies=MULTI)
    async def nested_first(
        both: Any = Depends(secondary_then_store), sec: Any = secondary
    ) -> dict[str, bool]:
        await _write_both(both, "7")
        return {"ok": True}

    @router.post("/md/nested-two-levels", dependencies=MULTI)
    async def nested_two_levels(
        sec: Any = secondary, both: Any = Depends(outer)
    ) -> dict[str, bool]:
        await _write_both(both, "8")
        return {"ok": True}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    for path in (
        "secondary-param-first",
        "primary-param-first",
        "secondary-written-first",
        "secondary-first-both-ways",
        "nested-secondary-store",
        "nested-store-secondary",
        "nested-first",
        "nested-two-levels",
    ):
        analytics.clear()
        resp = await real_uow_client.post(f"{PREFIX}/md/{path}", headers=headers)
        assert resp.status_code == 200, (path, resp.text)
        assert analytics == ["analytics", "default"], path
    assert sorted(await _rows(db_engine)) == [
        "p1",
        "p2",
        "p3",
        "p4",
        "p5",
        "p6",
        "p7",
        "p8",
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
    ]


# --------------------------------------------------------------------------- #
# the partial-commit metric of best-effort 1PC (write-admission rule 4)
# --------------------------------------------------------------------------- #
PARTIAL_TABLE = "wsa_partial_probe"
PARTIAL_METRIC = "sandloom_multi_store_partial_commit_total"


class _PartialBase(DeclarativeBase):
    pass


class PartialProbe(_PartialBase):
    """``v`` is unique, checked at COMMIT: two equal values in one
    transaction make that transaction's COMMIT fail (a real PostgreSQL commit
    failure, after every statement succeeded)."""

    __tablename__ = PARTIAL_TABLE
    __table_args__ = (
        UniqueConstraint(
            "v", name=f"{PARTIAL_TABLE}_v_key", deferrable=True, initially="DEFERRED"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def partial_table(db_engine: Any) -> AsyncIterator[None]:
    async with db_engine.begin() as conn:
        await conn.run_sync(_PartialBase.metadata.create_all)
        await conn.execute(text(f"DELETE FROM {PARTIAL_TABLE}"))
    yield


async def _partial_rows(db_engine: Any) -> list[str]:
    async with db_engine.connect() as conn:
        query = text(f"SELECT v FROM {PARTIAL_TABLE} ORDER BY id")
        return [r[0] for r in await conn.execute(query)]


def _partial_samples() -> dict[tuple[str, str], float]:
    """Every ``sandloom_multi_store_partial_commit_total`` sample, by (route, store)."""
    return {
        (s.labels["route"], s.labels["store"]): s.value
        for family in REGISTRY.collect()
        if family.name == PARTIAL_METRIC.removesuffix("_total")
        for s in family.samples
        if s.name == PARTIAL_METRIC
    }


@pytest.mark.sandloom_allow_server_errors
async def test_a_write_store_commit_failing_after_the_secondary_committed_is_counted(
    real_uow_client: Any,
    db_engine: Any,
    partial_table: None,
    mount: Any,
    analytics: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Best-effort 1PC (write-admission rule 4): the secondary committed, then
    the write store's COMMIT failed. The partial commit is counted in
    ``sandloom_multi_store_partial_commit_total{route,store}`` (route: the
    template, never the raw path; store: the database that committed) and
    logged with the route, the committed stores and the failing store. A
    request that commits everything, or nothing, is not counted."""
    router = APIRouter(prefix=PREFIX)
    secondary = di.Database("analytics")

    @router.post("/md/partial/{item}", dependencies=MULTI)
    async def partial(
        item: str, db: di.DbSession, sec: Any = secondary
    ) -> dict[str, bool]:
        await sec.execute(insert(PartialProbe).values(v=f"s-{item}"))
        # two equal values: the write store's COMMIT fails (deferred unique)
        await db.execute(insert(PartialProbe).values(v=f"dup-{item}"))
        await db.execute(insert(PartialProbe).values(v=f"dup-{item}"))
        return {"ok": True}

    @router.post("/md/nothing", dependencies=MULTI)
    async def nothing_commits(
        db: di.DbSession, sec: Any = secondary
    ) -> dict[str, bool]:
        # the SECONDARY's COMMIT fails (it commits first): the write store
        # then rolls back, nothing committed
        await sec.execute(insert(PartialProbe).values(v="dup-nothing"))
        await sec.execute(insert(PartialProbe).values(v="dup-nothing"))
        await db.execute(insert(PartialProbe).values(v="p-nothing"))
        return {"ok": True}

    @router.post("/md/everything", dependencies=MULTI)
    async def everything_commits(
        db: di.DbSession, sec: Any = secondary
    ) -> dict[str, bool]:
        await sec.execute(insert(PartialProbe).values(v="s-everything"))
        await db.execute(insert(PartialProbe).values(v="p-everything"))
        return {"ok": True}

    mount(router)
    _uid, headers = make_user_headers(uuid.uuid4())
    template = f"{PREFIX}/md/partial/{{item}}"
    caplog.set_level("WARNING", logger="sandloom.core.unit_of_work")

    before = _partial_samples()
    resp = await real_uow_client.post(f"{PREFIX}/md/everything", headers=headers)
    assert resp.status_code == 200, resp.text
    assert analytics == ["analytics", "default"]
    analytics.clear()
    resp = await real_uow_client.post(f"{PREFIX}/md/nothing", headers=headers)
    assert resp.status_code == 500, resp.text
    assert sorted(await _partial_rows(db_engine)) == ["p-everything", "s-everything"]
    assert _partial_samples() == before  # all or nothing committed: not counted
    assert not [r for r in caplog.records if r.levelname == "WARNING"]

    for item in ("a", "b"):
        resp = await real_uow_client.post(
            f"{PREFIX}/md/partial/{item}", headers=headers
        )
        assert resp.status_code == 500, resp.text

    after = _partial_samples()
    key = (template, "analytics")
    assert after.get(key, 0.0) - before.get(key, 0.0) == 2.0, after
    assert {k: v for k, v in after.items() if k != key} == {
        k: v for k, v in before.items() if k != key
    }
    rows = sorted(await _partial_rows(db_engine))  # only the secondary committed
    assert rows == ["p-everything", "s-a", "s-b", "s-everything"]

    warnings = [
        r for r in caplog.records if getattr(r, "event", None) == "uow.partial_commit"
    ]
    assert len(warnings) == 2, [r.getMessage() for r in caplog.records]
    for record in warnings:
        assert record.levelname == "WARNING"
        assert record.__dict__["route"] == template
        assert record.__dict__["committed_stores"] == ["analytics"]
        assert record.__dict__["failing_store"] == "default"
        assert template in record.getMessage()
