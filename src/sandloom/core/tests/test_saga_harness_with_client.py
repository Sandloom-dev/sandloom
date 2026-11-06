"""``saga_harness`` beside the ``client`` fixture (spec: "SQLite app gaps, as built").

A request through ``client`` writes in the test's shared ``db`` session; on a
database whose writers are serial (SQLite) that session then holds the write
lock, and the harness's units nest in it as SAVEPOINTs. Their post-commit
stages wait for the host's ``on_commit`` stage, which the ``client`` fixture
runs after each successful request without an outbox stage (the test owns the
transaction, which never commits). The nested units' outbox rows are rows of
that transaction: they are neither published nor marked delivered. Marking
them used to run on a connection of its own and wait SQLite's busy timeout
(about 30 s) for the ``db`` session's lock, once per marked unit.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

import sandloom.core.unit_of_work.dispatch as uow_dispatch
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.testing.fixtures import *  # noqa: F403 - saga_harness + DB fixtures
from sandloom.core.testing.infra import has_serial_writers
from sandloom.core.tests.saga_fixtures import (
    Ledger,
    enroll_input,
    enroll_saga,
    install_fakes,
)
from sandloom.core.unit_of_work.hooks import on_commit
from sandloom.sdk.testing import as_user

ENROLL = enroll_saga()

#: Far below SQLite's busy timeout (30 s): a mark that waits for the ``db``
#: session's write lock fails this bound.
BOUND_SECONDS = 5.0


async def _outbox_rows(db: Any) -> int:
    return int(
        (await db.execute(select(func.count()).select_from(OutboxMessage))).scalar_one()
    )


async def _delivered_rows(db: Any) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.delivered_at.is_not(None))
            )
        ).scalar_one()
    )


class _CommitTrackingLedger(Ledger):
    """The billing fake queues an ``on_commit`` callback in its command's unit
    (nested in the transition's unit, nested in ``db`` on SQLite)."""

    def __init__(self) -> None:
        super().__init__()
        self.after_commit: list[str] = []

    async def spend(
        self, customer_id: uuid.UUID, amount: int, operation_key: str
    ) -> int:
        await on_commit(lambda: self.after_commit.append(operation_key))
        return await super().spend(customer_id, amount, operation_key)


async def test_a_saga_run_between_requests_waits_for_no_lock(
    saga_harness: Any,
    client: Any,
    db: Any,
    db_engine: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _CommitTrackingLedger()
    install_fakes(saga_harness, ledger)
    sent: list[str] = []
    publish_in_order = uow_dispatch._publish_in_order

    async def recording(bus: Any, events: Any) -> Any:
        sent.extend(str(e.channel) for e in events)
        return await publish_in_order(bus, events)

    monkeypatch.setattr(uow_dispatch, "_publish_in_order", recording)
    _, headers = as_user()
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/api/v1/orgs", json={"name": "Acme", "slug": slug}, headers=headers
    )
    assert created.status_code == 201, created.text
    assert await _outbox_rows(db) >= 1  # the request published into the shared db

    caplog.set_level(logging.WARNING, logger="sandloom")
    started = time.monotonic()
    run = await saga_harness.run(ENROLL, input=enroll_input(price=30))
    nested = has_serial_writers(db_engine)
    # SQLite: the units nested in db, their stages wait for its next on_commit
    # stage; PostgreSQL: each committed on its own, its callbacks ran
    assert len(ledger.after_commit) == (0 if nested else 1)
    # the next writing request runs the db session's on_commit stage: the
    # stages the harness's nested units deferred to it
    again = await client.post(
        "/api/v1/orgs", json={"name": "Acme 2", "slug": f"{slug}-2"}, headers=headers
    )
    elapsed = time.monotonic() - started

    assert again.status_code == 201, again.text
    assert run.status == "completed"
    assert run.calls == ["charge:do", "grant_access:do", "notify:do"]
    assert ledger.balance == 70
    assert elapsed < BOUND_SECONDS, f"{elapsed:.1f}s: a mark waited for a lock"
    failures = [
        r.getMessage() for r in caplog.records if "Failed to mark" in r.getMessage()
    ]
    assert failures == []
    # the nested units' on_commit callbacks ran (once), with the host's stage
    assert ledger.after_commit == [run.keys[0]]
    if nested:
        # their outbox rows are rows of db's transaction, which never commits:
        # nothing reached the bus and nothing is marked delivered
        assert sent == []
        assert await _delivered_rows(db) == 0
