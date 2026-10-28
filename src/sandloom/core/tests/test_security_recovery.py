"""Restoring a STALE security-cache snapshot must not re-enable anyone (H-20).

The hard case: the security Redis is restored from a snapshot taken BEFORE a
later ``block_user`` / revocation. The block mark is simply absent from the
snapshot, and an absent mark means "not blocked" — the restore silently
unblocks the user. ``sandloom backup reconcile-security``
(:mod:`sandloom.core.auth.recovery`) re-derives the marks from the durable
records (audit log, outbox) and closes the hole; these tests reproduce the
hole first, then prove the reconciliation closes it.

Real Redis (test ``CACHE_URL``) and Postgres (test ``DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sandloom.core.auth import blocks, marks, revocation, tokens
from sandloom.core.auth.recovery import (
    BlockEvent,
    latest_block_states,
    read_block_events,
    read_block_events_file,
    read_revocations,
    reconcile_security_state,
    revocation_window_start,
)
from sandloom.core.config.settings import settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.infra import access
from sandloom.core.sql_alchemy.outbox_store import (
    sql_alchemy_audit_store,
    sql_alchemy_outbox_store,
)
from sandloom.core.testing import (
    InMemoryCache,
    bind_test_uow,
    reload_test_env,
    run_on_commit,
)

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


@pytest_asyncio.fixture
async def redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RedisCacheProvider]:
    url = os.getenv("CACHE_URL")
    if not url:
        pytest.skip("no test CACHE_URL configured")
    provider = RedisCacheProvider(
        name="recovery", driver="redis", options={"url": url}, shared=False
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


@pytest_asyncio.fixture
async def records_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
    from sandloom.core.sql_alchemy.models.outbox import OutboxMessage

    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    engine = create_async_engine(url)
    tables = [cast(Table, m.__table__) for m in (AuditLogEntry, OutboxMessage)]

    def _create(sync_conn: Any) -> None:
        for table in tables:
            table.create(sync_conn, checkfirst=True)

    async with engine.begin() as conn:
        await conn.run_sync(_create)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


def _probe_app() -> FastAPI:
    from sandloom.shared.guards.auth import get_current_user_id

    app = FastAPI()

    @app.get("/me")
    async def _me(uid: UUID = Depends(get_current_user_id)) -> dict[str, str]:
        return {"uid": str(uid)}

    return app


@pytest.fixture
def probe() -> Iterator[httpx.AsyncClient]:
    yield httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_probe_app()), base_url="http://probe"
    )


def _auth(sub: str) -> dict[str, str]:
    token = tokens.create_access_token(data={"sub": sub, "token_type": "access"})
    return {"Authorization": f"Bearer {token}"}


def _keys(sub: str) -> list[str]:
    return [marks.subject_key(sub)]


async def _snapshot(provider: RedisCacheProvider, sub: str) -> dict[str, Any]:
    """The subject's security keys as an RDB snapshot would hold them now."""
    return {key: await provider.get(key) for key in _keys(sub)}


async def _restore(provider: RedisCacheProvider, snap: dict[str, Any]) -> None:
    """Replace the live keys with the snapshot's (absent stays absent)."""
    for key, value in snap.items():
        if value is None:
            await provider.delete(key)
        else:
            await provider.set(key, value)


async def _durable_records(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[list[BlockEvent], dict[str, float]]:
    async with factory() as session:
        events = await read_block_events(
            session, sql_alchemy_audit_store(), source="default"
        )
        mark_values, _oldest = await read_revocations(
            session, sql_alchemy_outbox_store(), since=revocation_window_start()
        )
    return events, mark_values


async def test_stale_snapshot_after_block_does_not_reenable_the_user(
    redis_cache: RedisCacheProvider,
    records_db: async_sessionmaker[AsyncSession],
    probe: httpx.AsyncClient,
) -> None:
    sub, admin = str(uuid4()), uuid4()
    old_token = _auth(sub)
    assert (await probe.get("/me", headers=old_token)).status_code == 200
    snapshot = await _snapshot(redis_cache, sub)  # taken BEFORE the block

    async with records_db() as session:
        with bind_test_uow(session):
            await blocks.block_user(sub, reason="credential leak", actor_id=admin)
        await session.commit()
    assert (await probe.get("/me", headers=old_token)).status_code == 401

    # Disaster recovery: the security Redis comes back from the older snapshot.
    await _restore(redis_cache, snapshot)
    # The hole this guards against: the block is silently gone.
    assert await blocks.is_user_blocked(sub) is False
    assert (await probe.get("/me", headers=old_token)).status_code == 200
    assert (await probe.get("/me", headers=_auth(sub))).status_code == 200

    events, mark_values = await _durable_records(records_db)
    dry = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=False
    )
    assert sub in dry.missing_blocks
    assert sub in dry.missing_revocations
    assert not dry.clean
    assert await blocks.is_user_blocked(sub) is False  # dry-run changed nothing

    applied = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=True
    )
    assert applied.clean, applied.as_dict()
    assert sub in applied.missing_blocks
    # Blocked again — old tokens, tokens minted after the restore, issuance.
    assert await blocks.is_user_blocked(sub) is True
    for headers in (old_token, _auth(sub)):
        resp = await probe.get("/me", headers=headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Account blocked"
    with pytest.raises(HTTPException) as refused:
        await blocks.ensure_user_not_blocked(sub)
    assert refused.value.status_code == 401
    # Lifting the block later (compare-and-set to a revocation, never a
    # delete) does not revive the tokens issued before it.
    assert await blocks._swap_blocked_for_after(redis_cache, sub) is True
    resp = await probe.get("/me", headers=old_token)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token has been revoked"

    # Idempotent: a second run finds nothing missing.
    await redis_cache.set(
        marks.subject_key(sub), marks._format_blocked(marks._seconds_to_us(1))
    )
    again = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=False
    )
    assert sub not in again.missing_blocks
    assert sub not in again.missing_revocations


async def test_stale_snapshot_after_unblock_keeps_the_user_blocked_unless_lifted(
    redis_cache: RedisCacheProvider,
    records_db: async_sessionmaker[AsyncSession],
) -> None:
    sub, admin = str(uuid4()), uuid4()
    async with records_db() as session:
        with bind_test_uow(session):
            await blocks.block_user(sub, reason="investigation", actor_id=admin)
        await session.commit()
    snapshot = await _snapshot(redis_cache, sub)  # taken WHILE blocked
    async with records_db() as session:
        with bind_test_uow(session):
            await blocks.unblock_user(sub, reason="cleared", actor_id=admin)
            await run_on_commit(session)
        await session.commit()
    assert await blocks.is_user_blocked(sub) is False

    await _restore(redis_cache, snapshot)
    assert await blocks.is_user_blocked(sub) is True  # safe direction

    events, mark_values = await _durable_records(records_db)
    kept = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=True
    )
    assert sub in kept.stale_blocks
    assert sub not in kept.lifted_stale_blocks
    assert await blocks.is_user_blocked(sub) is True

    lifted = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=True, lift_stale_blocks=True
    )
    assert sub in lifted.lifted_stale_blocks
    assert await blocks.is_user_blocked(sub) is False


async def test_lost_revocation_is_reapplied_from_the_outbox(
    redis_cache: RedisCacheProvider,
    records_db: async_sessionmaker[AsyncSession],
    probe: httpx.AsyncClient,
) -> None:
    sub = str(uuid4())
    old_token = _auth(sub)
    snapshot = await _snapshot(redis_cache, sub)
    async with records_db() as session:
        with bind_test_uow(session):
            await revocation.revoke_user_tokens_on_commit(
                sub, reason="password changed", require_immediate=True
            )
        await session.commit()
    assert (await probe.get("/me", headers=old_token)).status_code == 401

    await _restore(redis_cache, snapshot)
    assert (await probe.get("/me", headers=old_token)).status_code == 200

    events, mark_values = await _durable_records(records_db)
    assert sub in mark_values
    report = await reconcile_security_state(
        block_events=events, revocations=mark_values, apply=True
    )
    assert sub in report.missing_revocations
    resp = await probe.get("/me", headers=old_token)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token has been revoked"


# --------------------------------------------------------------------------- #
# without a database: the off-site audit export, ordering, coverage
# --------------------------------------------------------------------------- #
@pytest.fixture
def memory_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    cache = InMemoryCache()
    monkeypatch.setattr(access, "cache", lambda name="default": cache)
    return cache


async def test_audit_export_restores_blocks_a_restored_database_lost(
    memory_cache: InMemoryCache, tmp_path: Path
) -> None:
    sub = str(uuid4())
    export = tmp_path / "audit.jsonl"
    rows = [
        {
            "database": "default",
            "action": "auth.user.blocked",
            "resource_type": "user",
            "resource_id": sub,
            "occurred_at": datetime.now(UTC).isoformat(),
            "id": str(uuid4()),
        },
        {"action": "identity.password.changed", "resource_id": sub},
    ]
    export.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    events = read_block_events_file(export)
    assert [e.user_id for e in events] == [sub]

    report = await reconcile_security_state(
        block_events=events, revocations={}, apply=True
    )
    assert report.missing_blocks == [sub]
    assert await blocks.is_user_blocked(sub) is True


def test_newest_record_wins_and_a_tie_is_blocked() -> None:
    now = datetime.now(UTC)
    earlier = now - timedelta(minutes=1)
    events = [
        BlockEvent("a", earlier, True, "db"),
        BlockEvent("a", now, False, "db"),
        BlockEvent("b", now, False, "db"),
        BlockEvent("b", now, True, "file"),
        BlockEvent("c", now, True, "db"),
        BlockEvent("c", earlier, False, "db"),
    ]
    states = latest_block_states(events)
    assert {u: s.blocked for u, s in states.items()} == {
        "a": False,
        "b": True,
        "c": True,
    }


async def test_short_outbox_retention_is_flagged_as_a_coverage_gap(
    memory_cache: InMemoryCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "outbox_retention_hours", 0.1)
    monkeypatch.setattr(settings, "access_token_expire_minutes", 30)
    report = await reconcile_security_state(block_events=[], revocations={}, apply=True)
    assert not report.clean
    assert any("OUTBOX_RETENTION_HOURS" in w for w in report.warnings)
    monkeypatch.setattr(settings, "outbox_retention_hours", 0)
    full = await reconcile_security_state(block_events=[], revocations={}, apply=True)
    assert full.clean


async def test_a_newer_mark_in_the_cache_is_never_lowered(
    memory_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    newer = time.time() + 60
    await memory_cache.set(
        marks.subject_key(sub), marks._format_mark(marks._seconds_to_us(newer))
    )
    report = await reconcile_security_state(
        block_events=[], revocations={sub: newer - 600}, apply=True
    )
    assert report.missing_revocations == []
    held = await memory_cache.get(marks.subject_key(sub))
    assert marks._parse_mark_us(held) / 1_000_000 == pytest.approx(newer, abs=1e-5)
