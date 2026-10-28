"""Session freshness after ADR-0009: every check reads the record live.

* Every authenticated request reads the subject-state record live (one
  ``GET``): a revocation or a block written by ANY process is seen on the very
  next request — there is no per-process copy and no staleness budget.
* ``block_user`` is audited, written last, needs a Unit of Work and a reason;
  a cache outage while writing it is a 503 that rolls the audit back;
  ``unblock_user`` lifts it only after commit.
* Redis down: fail-closed answers 503; fail-open (outside prod) accepts.

Real Redis (test ``CACHE_URL``) and Postgres (test ``DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sandloom.core.auth import blocks, marks, revocation, tokens, verification
from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.infra import access
from sandloom.core.testing import bind_test_uow, reload_test_env, run_on_commit

BACKEND = Path(__file__).resolve().parents[4]
ENV_TEST = BACKEND / ".env.test"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    if ENV_TEST.exists():
        reload_test_env(ENV_TEST)


@pytest.fixture(autouse=True)
def _platform_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composed platform is the active runtime: its bus knows the core
    event schemas ``block_user`` publishes (core.auth.*)."""
    from sandloom.core import runtime
    from sandloom.main import app

    monkeypatch.setattr(runtime, "_process_core", app.state.core)


def _claims(sub: str) -> dict[str, Any]:
    return {"sub": sub, "iat": int(time.time()) - 5}


def _redis_url() -> str:
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("no test CACHE_URL configured")
    return url


@pytest_asyncio.fixture
async def redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RedisCacheProvider]:
    provider = RedisCacheProvider(
        name="freshness", driver="redis", options={"url": _redis_url()}, shared=False
    )
    try:
        await asyncio.wait_for(provider.get("sandloom:test:ping"), 5)
    except Exception:  # noqa: BLE001 - test Redis unreachable: skip
        await provider.aclose()
        pytest.skip("test Redis not reachable")
    monkeypatch.setattr(access, "cache", lambda name="default": provider)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", True)
    yield provider
    await provider.aclose()


def _dead_cache() -> RedisCacheProvider:
    # Nothing listens on port 1: every command fails at once (connection refused).
    return RedisCacheProvider(
        name="dead",
        driver="redis",
        options={"url": "redis://127.0.0.1:1/0"},
        shared=False,
    )


def _probe_app() -> FastAPI:
    from sandloom.shared.guards.auth import get_current_user_id

    app = FastAPI()

    @app.get("/strict")
    async def _strict(uid: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"uid": str(uid)}

    @app.post("/write")
    async def _write(uid: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"uid": str(uid)}

    return app


@pytest.fixture
def probe() -> Iterator[httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_probe_app()), base_url="http://probe"
    )
    yield client


def _auth(sub: str) -> dict[str, str]:
    token = tokens.create_access_token(data={"sub": sub, "token_type": "access"})
    return {"Authorization": f"Bearer {token}"}


async def _write_mark_from_another_process(
    provider: RedisCacheProvider, sub: str
) -> None:
    """Another process revokes ``sub`` (nothing is announced to this one)."""
    await provider.set(
        marks.subject_key(sub),
        marks._format_mark(marks._seconds_to_us(time.time() + 1)),
        ttl_seconds=120,
    )


# ---------------------------------------------------------------------------
# every request is strict
# ---------------------------------------------------------------------------
async def test_strict_default_sees_revocation_on_next_request(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    sub = str(uuid4())
    headers = _auth(sub)
    for method, path in (("GET", "/strict"), ("POST", "/write")):
        assert (await probe.request(method, path, headers=headers)).status_code == 200
    await _write_mark_from_another_process(redis_cache, sub)
    for method, path in (("GET", "/strict"), ("POST", "/write")):
        resp = await probe.request(method, path, headers=headers)
        assert resp.status_code == 401, path
        assert resp.json()["detail"] == "Token has been revoked"
        assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_the_freshness_knobs_are_gone() -> None:
    """ADR-0009 "Removed outright": no budget marker, no strict marker, no
    prefix setting (a stale AUTH_REVOCATION_STRICT_PATH_PREFIXES env fails
    startup unless it is the inert ``[]``)."""
    import sandloom.core.di as di
    from sandloom.core.config import tiers

    for gone in (
        "revocation_freshness",
        "strict_revocation",
        "MAX_REVOCATION_STALENESS_SECONDS",
    ):
        assert not hasattr(di, gone), gone
    removed = {r.name: r for r in tiers.REMOVED}
    entry = removed["AUTH_REVOCATION_STRICT_PATH_PREFIXES"]
    assert entry.inert == []
    assert tiers.removed_setting_problem(entry, "[]")[1] is False
    assert tiers.removed_setting_problem(entry, "")[1] is False
    assert tiers.removed_setting_problem(entry, '["/admin"]')[1] is True
    assert not hasattr(settings, "auth_revocation_strict_path_prefixes")


# ---------------------------------------------------------------------------
# emergency block
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def audit_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
    from sandloom.core.sql_alchemy.models.outbox import OutboxMessage

    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    engine = create_async_engine(url)
    # block_user / unblock_user audit AND publish (outbox) in the same UoW:
    # create every table they write, never rely on another test having done it
    # (a fresh per-worker database under pytest-xdist has none of them).
    tables = [cast(Table, m.__table__) for m in (AuditLogEntry, OutboxMessage)]

    def _create(sync_conn: Any) -> None:
        for table in tables:
            table.create(sync_conn, checkfirst=True)

    async with engine.begin() as conn:
        await conn.run_sync(_create)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession], user: str
) -> list[tuple[str, Any, Any]]:
    from sandloom.core.sql_alchemy.models.audit import AuditLogEntry

    async with factory() as session:
        rows = (
            await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.resource_id == user)
                .order_by(AuditLogEntry.occurred_at)
            )
        ).scalars()
        return [(r.action, r.actor_id, r.data) for r in rows]


async def test_block_applies_on_next_request_even_on_budget_routes(
    redis_cache: RedisCacheProvider,
    probe: httpx.AsyncClient,
    audit_db: async_sessionmaker[AsyncSession],
) -> None:
    sub, admin = str(uuid4()), uuid4()
    headers = _auth(sub)
    assert (await probe.get("/strict", headers=headers)).status_code == 200
    async with audit_db() as session:
        with bind_test_uow(session):
            await blocks.block_user(sub, reason="credential leak", actor_id=admin)
        await session.commit()
    for method, path in (("GET", "/strict"), ("POST", "/write")):
        resp = await probe.request(method, path, headers=headers)
        assert resp.status_code == 401, path
        assert resp.json()["detail"] == "Account blocked"
    # A token minted AFTER the block is blocked too.
    assert (await probe.get("/strict", headers=_auth(sub))).status_code == 401
    assert await blocks.is_user_blocked(sub) is True
    client = await redis_cache.raw_client()
    assert await client.ttl(marks.subject_key(sub)) == -1  # until unblocked

    async with audit_db() as session:
        with bind_test_uow(session):
            await blocks.unblock_user(sub, reason="rotated", actor_id=admin)
        await session.commit()
        await run_on_commit(session)  # the record is swapped only after commit
    # Lifting the block does not revive the old sessions: the unblock writes
    # a revocation instant after every token issued so far.
    revived = await probe.get("/strict", headers=headers)
    assert revived.status_code == 401
    assert revived.json()["detail"] == "Token has been revoked"
    assert await blocks.is_user_blocked(sub) is False
    assert await _audit_rows(audit_db, sub) == [
        ("auth.user.blocked", admin, {"reason": "credential leak"}),
        ("auth.user.unblocked", admin, {"reason": "rotated"}),
    ]
    async with audit_db() as session:
        await session.execute(
            text("DELETE FROM audit_log WHERE resource_id = :r"), {"r": sub}
        )
        await session.commit()


async def test_unblock_lifts_the_mark_only_after_commit(
    redis_cache: RedisCacheProvider, audit_db: async_sessionmaker[AsyncSession]
) -> None:
    """A rolled-back unblock leaves the user blocked (fail-safe, never unaudited)."""
    sub, admin = str(uuid4()), uuid4()
    async with audit_db() as session:
        with bind_test_uow(session):
            await blocks.block_user(sub, reason="leak", actor_id=admin)
        await session.commit()
        await run_on_commit(session)
    try:
        async with audit_db() as session:
            with bind_test_uow(session):
                await blocks.unblock_user(sub, reason="oops", actor_id=admin)
                # Not committed yet: still blocked.
                assert await blocks.is_user_blocked(sub) is True
            await session.rollback()
        assert await blocks.is_user_blocked(sub) is True

        async with audit_db() as session:
            with bind_test_uow(session):
                await blocks.unblock_user(sub, reason="ok", actor_id=admin)
            await session.commit()
            await run_on_commit(session)
        assert await blocks.is_user_blocked(sub) is False
        assert [a for a, _, _ in await _audit_rows(audit_db, sub)] == [
            "auth.user.blocked",
            "auth.user.unblocked",
        ]
    finally:
        async with audit_db() as session:
            await session.execute(
                text("DELETE FROM audit_log WHERE resource_id = :r"), {"r": sub}
            )
            await session.commit()


async def test_block_mark_is_written_last(
    redis_cache: RedisCacheProvider,
    audit_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing revoke / publish leaves no block behind its rollback."""

    async def failing_revoke(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("revocation store down")

    monkeypatch.setattr(revocation, "revoke_user_tokens_on_commit", failing_revoke)
    sub = str(uuid4())
    async with audit_db() as session:
        with bind_test_uow(session):
            with pytest.raises(RuntimeError):
                await blocks.block_user(sub, reason="leak")
        await session.rollback()
    assert await blocks.is_user_blocked(sub) is False
    assert await _audit_rows(audit_db, sub) == []


async def test_block_requires_uow_and_reason(redis_cache: RedisCacheProvider) -> None:
    from sandloom.core.unit_of_work.effects import AuditUnavailableError

    sub = str(uuid4())
    with pytest.raises(AuditUnavailableError):
        await blocks.block_user(sub, reason="x")
    assert await blocks.is_user_blocked(sub) is False  # nothing unaudited
    with pytest.raises(ValueError):
        await blocks.block_user(sub, reason="  ")


async def test_block_write_outage_is_503_and_rolls_back_audit(
    monkeypatch: pytest.MonkeyPatch, audit_db: async_sessionmaker[AsyncSession]
) -> None:
    from sandloom.core.http.error_handlers import http_status_of

    dead = _dead_cache()
    monkeypatch.setattr(access, "cache", lambda name="default": dead)
    sub = str(uuid4())
    try:
        async with audit_db() as session:
            with bind_test_uow(session):
                with pytest.raises(blocks.BlockUnavailableError) as exc:
                    await blocks.block_user(sub, reason="leak")
            await session.rollback()  # the request UoW rolls back on the error
        assert http_status_of(exc.value) == 503
        assert await _audit_rows(audit_db, sub) == []
    finally:
        await dead.aclose()


# ---------------------------------------------------------------------------
# Redis outage
# ---------------------------------------------------------------------------
async def test_redis_down_fail_closed(
    redis_cache: RedisCacheProvider,
    probe: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid4())
    headers = _auth(sub)
    assert (await probe.get("/strict", headers=headers)).status_code == 200
    dead = _dead_cache()
    monkeypatch.setattr(access, "cache", lambda name="default": dead)
    try:
        # Freshness cannot be confirmed -> 503 on every authenticated route.
        assert (await probe.get("/strict", headers=headers)).status_code == 503
        assert (await probe.post("/write", headers=headers)).status_code == 503
        with pytest.raises(marks.RevocationUnavailableError):
            await verification.is_token_revoked(_claims(sub))
    finally:
        await dead.aclose()


async def test_redis_down_fail_open_outside_prod(
    probe: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = _dead_cache()
    monkeypatch.setattr(access, "cache", lambda name="default": dead)
    monkeypatch.setattr(settings, "auth_revocation_fail_closed", False)
    try:
        headers = _auth(str(uuid4()))
        assert (await probe.get("/strict", headers=headers)).status_code == 200
        assert (await probe.post("/write", headers=headers)).status_code == 200
    finally:
        await dead.aclose()


# ---------------------------------------------------------------------------
# revocation mark TTL unchanged
# ---------------------------------------------------------------------------
async def test_revocation_mark_ttl_unchanged(redis_cache: RedisCacheProvider) -> None:
    sub = str(uuid4())
    assert await revocation.revoke_user_tokens(sub) is True
    client = await redis_cache.raw_client()
    ttl = await client.ttl(marks.subject_key(sub))
    expected = marks._revocation_ttl_seconds()
    assert expected >= 24 * 3600
    assert expected - 5 <= ttl <= expected


# ---------------------------------------------------------------------------
# two processes sharing Redis
# ---------------------------------------------------------------------------
_CHILD = textwrap.dedent(
    """
    import asyncio, json, sys, time
    from sandloom.core.auth import verification
    from sandloom.core.infra import access
    from sandloom.core.drivers.redis_cache import RedisCacheProvider

    async def main():
        url, sub = sys.argv[1], sys.argv[2]
        provider = RedisCacheProvider(
            name="child", driver="redis", options={"url": url}, shared=False
        )
        access.cache = lambda name="default": provider
        claims = {"sub": sub, "iat": int(time.time()) - 5}
        assert await verification.is_token_revoked(claims) is False
        print("READY", flush=True)
        sys.stdin.readline()  # the parent revoked
        start = time.monotonic()
        revoked = await verification.is_token_revoked(claims)
        print(json.dumps({"revoked": revoked, "after": time.monotonic() - start}),
              flush=True)
        await provider.aclose()

    asyncio.run(main())
    """
)


async def test_revocation_in_one_process_invalidates_another(
    redis_cache: RedisCacheProvider,
) -> None:
    """The very next check in ANOTHER process sees the revocation (no copy,
    no broadcast to lose)."""
    sub = str(uuid4())
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, _redis_url(), sub],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(BACKEND),
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        ready = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 60)
        assert ready.strip() == "READY", proc.stderr.read() if proc.stderr else ""
        assert await revocation.revoke_user_tokens(sub, valid_after=time.time() + 1)
        proc.stdin.write("go\n")
        proc.stdin.flush()
        line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 60)
        assert json.loads(line)["revoked"] is True
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Superseded budget guarantees (G-REVOKE-13/14/15/16/20): ADR-0009 removed the
# staleness budget, so each of these node ids now pins the replacement, which
# is strictly stronger (no budget and no per-process copy can exist, every
# check reads the record live). The H1 ledger delta records the removals and
# the superseding G-REVOKE-21; the ids stay until the integrator applies it.
# ---------------------------------------------------------------------------
def _no_budget_api() -> None:
    import importlib

    import sandloom.core.di as di

    for gone in ("sandloom.core.auth.freshness", "sandloom.core.auth.local_marks"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(gone)
    assert not hasattr(di, "revocation_freshness")
    assert not hasattr(di, "MAX_REVOCATION_STALENESS_SECONDS")


def test_budget_outside_zero_to_cap_is_refused() -> None:
    """No budget at all can be declared (stronger than a 5 s cap)."""
    _no_budget_api()


def test_cap_is_five_seconds() -> None:
    """The bound on staleness is now zero: there is no budget to cap."""
    _no_budget_api()


def test_create_app_refuses_budget_on_post() -> None:
    _no_budget_api()


def test_budget_on_router_with_a_write_route_refused() -> None:
    _no_budget_api()


def test_validation_reports_budgets_and_ignores_strict() -> None:
    _no_budget_api()


def test_budget_ignored_on_always_strict_routes() -> None:
    """Every route is always-strict; the strict marker and prefixes are gone."""
    import sandloom.core.di as di

    _no_budget_api()
    assert not hasattr(di, "strict_revocation")


async def _revoked_from_elsewhere_is_seen_at_once(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    sub = str(uuid4())
    headers = _auth(sub)
    for _ in range(3):  # no warm-up can create a copy that answers later
        assert (await probe.get("/strict", headers=headers)).status_code == 200
    await _write_mark_from_another_process(redis_cache, sub)
    assert (await probe.get("/strict", headers=headers)).status_code == 401


async def test_budget_holds_at_decision_time_not_only_at_lookup(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    await _revoked_from_elsewhere_is_seen_at_once(redis_cache, probe)


async def test_expired_copy_is_never_extended(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    await _revoked_from_elsewhere_is_seen_at_once(redis_cache, probe)


async def test_invalidation_during_read_is_not_cached(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    await _revoked_from_elsewhere_is_seen_at_once(redis_cache, probe)


async def test_invalidation_during_block_read_forces_live_mark_read(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    await _revoked_from_elsewhere_is_seen_at_once(redis_cache, probe)


async def test_budget_bounds_staleness_when_broadcast_is_lost(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    """Nothing is broadcast any more, so nothing can be lost."""
    await _revoked_from_elsewhere_is_seen_at_once(redis_cache, probe)


async def test_own_revocation_is_immediate_on_budget_route(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    sub = str(uuid4())
    headers = _auth(sub)
    assert (await probe.get("/strict", headers=headers)).status_code == 200
    assert await revocation.revoke_user_tokens(sub, valid_after=time.time() + 1)
    assert (await probe.get("/strict", headers=headers)).status_code == 401


async def test_only_a_drop_of_the_same_subject_discards_a_racing_read(
    redis_cache: RedisCacheProvider, probe: httpx.AsyncClient
) -> None:
    """Another subject's revocation never affects this one; its own always does."""
    mine, other = str(uuid4()), str(uuid4())
    headers = _auth(mine)
    await _write_mark_from_another_process(redis_cache, other)
    assert (await probe.get("/strict", headers=headers)).status_code == 200
    await _write_mark_from_another_process(redis_cache, mine)
    assert (await probe.get("/strict", headers=headers)).status_code == 401


async def test_copy_expiring_during_block_read_fails_closed_on_outage(
    redis_cache: RedisCacheProvider,
    probe: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No copy answers through an outage: fail-closed is a 503 at once."""
    headers = _auth(str(uuid4()))
    assert (await probe.get("/strict", headers=headers)).status_code == 200
    dead = _dead_cache()
    monkeypatch.setattr(access, "cache", lambda name="default": dead)
    try:
        assert (await probe.get("/strict", headers=headers)).status_code == 503
    finally:
        await dead.aclose()


async def test_budget_fast_path_is_one_block_read(
    redis_cache: RedisCacheProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authenticated hot path is ONE read of ONE key (G-REVOKE-21)."""
    from types import SimpleNamespace

    sub = str(uuid4())
    keys: list[str] = []
    original = redis_cache.get

    async def counting(key: str) -> Any:
        keys.append(key)
        return await original(key)

    monkeypatch.setattr(redis_cache, "get", counting)
    request = SimpleNamespace(state=SimpleNamespace())
    await verification.ensure_token_not_revoked(request, _claims(sub))
    await verification.ensure_token_not_revoked(request, _claims(sub))  # memo
    assert keys == [marks.subject_key(sub)]
