"""The durable inbox, the contract command log and the durable HTTP
idempotency log are ONE primitive (ADR-0005).

All three are adapters of
:class:`sandloom.core.sql_alchemy.dedupe_log.RelationalDedupeLog` (the durable
dedupe record). Every test here runs against EVERY adapter on real
PostgreSQL, so the shared claim semantics cannot drift apart again: claimed
once, duplicate after commit, re-claimable after rollback, an in-flight claim
is resolved by its transaction's outcome (the inbox and the command log wait
for it; the HTTP log refuses at once — ``RequestLogBusy``), the caller's
pending objects are never flushed by a claim, and a bounded purge removes
only due records.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import effects
from sandloom.core.infra.contracts import RequestLogBusy
from sandloom.core.sql_alchemy.command_log import (
    COMMAND_LOG,
    sql_alchemy_command_log_store,
)
from sandloom.core.sql_alchemy.dedupe_log import RelationalDedupeLog
from sandloom.core.sql_alchemy.durable_inbox import INBOX_LOG, DurableConsumerInbox
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.sql_alchemy.models.http_idempotency import HttpIdempotencyRecord
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.request_log import (
    REQUEST_LOG,
    sql_alchemy_request_log_store,
)
from sandloom.core.testing import reload_test_env


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class Adapter:
    """One adapter of the durable dedupe record, driven uniformly."""

    name: str
    log: RelationalDedupeLog
    scope: str

    async def claim(self, session: AsyncSession, key: str) -> bool:
        """``True`` when newly claimed (run the effect), ``False`` otherwise."""
        if self.name == "inbox":
            return await DurableConsumerInbox(self.scope).claim(session, key)
        if self.name == "request":
            store = sql_alchemy_request_log_store()
            claim = await store.claim(
                session,
                dedupe_key=effects.key_digest(self.scope, "-", key),
                scope=self.scope,
                tenant=None,
                fingerprint="digest",
                ttl_seconds=3600,
            )
            if claim.outcome == "claimed":
                await store.complete(
                    session,
                    dedupe_key=effects.key_digest(self.scope, "-", key),
                    status=effects.SUCCEEDED,
                    result='{"status": 200}',
                )
                return True
            assert claim.outcome == "replay", claim
            return False
        outcome = await sql_alchemy_command_log_store().claim(
            session,
            dedupe_key=effects.key_digest(self.scope, "-", key),
            contract=self.scope,
            tenant=None,
            args_digest="digest",
            ttl_seconds=3600,
        )
        if outcome.outcome == "claimed":
            await sql_alchemy_command_log_store().complete(
                session,
                dedupe_key=effects.key_digest(self.scope, "-", key),
                status=effects.SUCCEEDED,
                result="1",
            )
            return True
        assert outcome.outcome == "replay", outcome
        return False

    async def count(self, session: AsyncSession) -> int:
        if self.name == "inbox":
            stmt = select(func.count()).where(ConsumerInboxEntry.consumer == self.scope)
        elif self.name == "request":
            stmt = select(func.count()).where(HttpIdempotencyRecord.scope == self.scope)
        else:
            stmt = select(func.count()).where(
                ContractCommandRecord.contract == self.scope
            )
        return int((await session.execute(stmt)).scalar_one())

    async def seed_due(self, session: AsyncSession, keys: list[str]) -> datetime:
        """Insert records already due for purging; return the purge cutoff."""
        old = _now() - timedelta(days=40)
        for key in keys:
            if self.name == "inbox":
                session.add(
                    ConsumerInboxEntry(
                        consumer=self.scope, event_id=key, processed_at=old
                    )
                )
            elif self.name == "request":
                session.add(
                    HttpIdempotencyRecord(
                        dedupe_key=effects.key_digest(self.scope, "-", key),
                        scope=self.scope,
                        args_digest="digest",
                        status=effects.SUCCEEDED,
                        created_at=old,
                        expires_at=old + timedelta(hours=1),
                    )
                )
            else:
                session.add(
                    ContractCommandRecord(
                        dedupe_key=effects.key_digest(self.scope, "-", key),
                        contract=self.scope,
                        args_digest="digest",
                        status=effects.SUCCEEDED,
                        created_at=old,
                        expires_at=old + timedelta(hours=1),
                    )
                )
        return _now() - timedelta(days=1)

    @property
    def inclusive(self) -> bool:
        return self.name != "inbox"

    @property
    def waits_for_in_flight(self) -> bool:
        """The HTTP log refuses a key held by an in-flight request at once."""
        return self.name != "request"


_LOGS = {"inbox": INBOX_LOG, "command": COMMAND_LOG, "request": REQUEST_LOG}


@pytest.fixture(params=list(_LOGS))
def adapter(request: pytest.FixtureRequest) -> Adapter:
    scope = f"test-dedupe@{request.param}:{uuid.uuid4().hex[:8]}"
    return Adapter(request.param, _LOGS[request.param], scope)


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng: AsyncEngine = create_async_engine(database_url, echo=False)
    tables = [
        ConsumerInboxEntry.__table__,
        ContractCommandRecord.__table__,
        HttpIdempotencyRecord.__table__,
        OutboxMessage.__table__,
    ]
    async with eng.begin() as conn:
        for table in tables:
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
        await conn.execute(
            text("DELETE FROM consumer_inbox WHERE consumer LIKE 'test-dedupe@%'")
        )
        await conn.execute(
            text("DELETE FROM contract_command_log WHERE contract LIKE 'test-dedupe@%'")
        )
        await conn.execute(
            text("DELETE FROM http_idempotency_log WHERE scope LIKE 'test-dedupe@%'")
        )
    yield async_sessionmaker(bind=eng, expire_on_commit=False)
    async with eng.begin() as conn:
        await conn.execute(
            text("DELETE FROM consumer_inbox WHERE consumer LIKE 'test-dedupe@%'")
        )
        await conn.execute(
            text("DELETE FROM contract_command_log WHERE contract LIKE 'test-dedupe@%'")
        )
        await conn.execute(
            text("DELETE FROM http_idempotency_log WHERE scope LIKE 'test-dedupe@%'")
        )
    await eng.dispose()


def test_every_store_is_an_adapter_of_one_dedupe_log() -> None:
    assert all(isinstance(log, RelationalDedupeLog) for log in _LOGS.values())
    assert INBOX_LOG.spec.table == "consumer_inbox"
    assert COMMAND_LOG.spec.table == "contract_command_log"
    assert REQUEST_LOG.spec.table == "http_idempotency_log"
    # the inbox has no TTL and nothing to replay; the two logs have both
    assert INBOX_LOG.spec.expires_at is None and not INBOX_LOG.spec.replays
    assert COMMAND_LOG.spec.expires_at and COMMAND_LOG.spec.replays
    assert REQUEST_LOG.spec.expires_at and REQUEST_LOG.spec.replays


async def test_claimed_once_then_duplicate(
    adapter: Adapter, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s, s.begin():
        assert await adapter.claim(s, "e1") is True
    async with sessionmaker() as s, s.begin():
        assert await adapter.claim(s, "e1") is False
        # the surrounding transaction stays usable after a duplicate
        assert await adapter.claim(s, "e2") is True
    async with sessionmaker() as s:
        assert await adapter.count(s) == 2


async def test_rolled_back_claim_leaves_no_record(
    adapter: Adapter, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The record commits iff the effect commits: a rollback frees the key."""
    async with sessionmaker() as s:
        await s.begin()
        assert await adapter.claim(s, "e1") is True
        await s.rollback()
    async with sessionmaker() as s, s.begin():
        assert await adapter.claim(s, "e1") is True
    async with sessionmaker() as s:
        assert await adapter.count(s) == 1


async def test_claim_never_flushes_the_callers_pending_objects(
    adapter: Adapter, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Only THIS record's conflict reads as a duplicate: the caller's own
    constraint violation surfaces at its own flush, never as "skip"."""
    existing = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(OutboxMessage(id=existing, channel="t.created.v1", payload={}))
    try:
        async with sessionmaker() as s:
            await s.begin()
            s.add(OutboxMessage(id=existing, channel="t.created.v1", payload={}))
            assert await adapter.claim(s, "e-own") is True
            with pytest.raises(IntegrityError):
                await s.flush()
            await s.rollback()
        async with sessionmaker() as s:
            assert await adapter.count(s) == 0
    finally:
        async with sessionmaker() as s, s.begin():
            await s.execute(
                text("DELETE FROM outbox_messages WHERE id = :id"), {"id": existing}
            )


@pytest.mark.parametrize("first_commits", [True, False])
async def test_concurrent_claim_is_decided_by_the_in_flight_transaction(
    adapter: Adapter,
    sessionmaker: async_sessionmaker[AsyncSession],
    first_commits: bool,
) -> None:
    """A second claim of a key held by an uncommitted claim learns the first
    one's outcome: it commits -> duplicate; it rolls back -> the second
    claims. The inbox / command log wait for it; the HTTP log refuses while
    it is in flight (``409``) and the client's retry then sees the outcome."""
    async with sessionmaker() as first, sessionmaker() as second:
        await first.begin()
        assert await adapter.claim(first, "e1") is True
        await second.begin()
        if adapter.waits_for_in_flight:
            racer = asyncio.create_task(adapter.claim(second, "e1"))
            await asyncio.sleep(0.3)
            assert not racer.done(), "the second claim must wait for the first"
        else:
            with pytest.raises(RequestLogBusy):
                await asyncio.wait_for(adapter.claim(second, "e1"), 2)
            await second.rollback()
            await second.begin()
        if first_commits:
            await first.commit()
        else:
            await first.rollback()
        if adapter.waits_for_in_flight:
            assert await asyncio.wait_for(racer, 10) is (not first_commits)
        else:
            assert await adapter.claim(second, "e1") is (not first_commits)
        await second.commit()
    async with sessionmaker() as s:
        assert await adapter.count(s) == 1


async def test_bounded_purge_removes_only_due_records(
    adapter: Adapter, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s, s.begin():
        cutoff = await adapter.seed_due(s, ["o1", "o2", "o3"])
        assert await adapter.claim(s, "fresh") is True
    removed = 0
    for _ in range(3):
        async with sessionmaker() as s, s.begin():
            removed += await adapter.log.purge(
                s, cutoff, limit=2, inclusive=adapter.inclusive
            )
    assert removed == 3
    async with sessionmaker() as s:
        assert await adapter.count(s) == 1  # the fresh record survives


async def test_claim_statement_is_prebuilt_once(
    adapter: Adapter, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s, s.begin():
        assert await adapter.claim(s, "e1") is True
        stmt = adapter.log.claim_statements["postgresql"]
        assert await adapter.claim(s, "e2") is True
        assert await adapter.claim(s, "e1") is False
    assert adapter.log.claim_statements["postgresql"] is stmt


# --------------------------------------------------------------------------- #
# The shared vocabulary (sandloom.core.effects)
# --------------------------------------------------------------------------- #


def test_key_digest_is_the_command_logs_historical_key() -> None:
    import hashlib

    parts = ("billing.credits.v1.spend", "-:scope:x", "op-1")
    assert (
        effects.key_digest(*parts)
        == hashlib.sha256("\x00".join(parts).encode()).hexdigest()
    )


def test_tag_and_ttl_and_replay_bounds() -> None:
    import hashlib

    assert effects.tag("u:1", 16) == hashlib.sha256(b"u:1").hexdigest()[:16]
    assert effects.ttl_or_default(None) == effects.RECORD_TTL_SECONDS == 86400
    assert effects.ttl_or_default(0) == effects.RECORD_TTL_SECONDS
    assert effects.ttl_or_default(60) == 60
    assert effects.RESULT_MAX_BYTES == 256 * 1024
    assert effects.fits_replay(effects.RESULT_MAX_BYTES)
    assert not effects.fits_replay(effects.RESULT_MAX_BYTES + 1)


def test_replay_outcome_caps_and_refuses_unencodable() -> None:
    assert effects.replay_outcome({"a": 1}) == (effects.SUCCEEDED, b'{"a":1}')
    big = "x" * effects.RESULT_MAX_BYTES
    assert effects.replay_outcome(big) == (effects.NOT_REPLAYABLE, None)
    assert effects.replay_outcome(object()) == (effects.NOT_REPLAYABLE, None)


def test_args_digest_is_canonical_with_a_repr_fallback() -> None:
    a = effects.args_digest({"x": {3, 1, 2}, "y": "k"})
    b = effects.args_digest({"y": "k", "x": {2, 3, 1}})
    assert a == b
    odd: Any = object()
    assert effects.args_digest({"o": odd}) == effects.args_digest({"o": odd})
    assert effects.args_digest({"o": 1}) != effects.args_digest({"o": 2})
