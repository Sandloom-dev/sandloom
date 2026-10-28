"""The intent reconciler's selections can use their partial indexes.

``ix_billing_intents_pending_created`` / ``ix_billing_intents_owed_expiry``
(billing chain 0010) only serve a query whose WHERE clause implies the index
predicate. This captures the SQL the reconciler really sends and asks the
planner for it with seq scans disabled: a change to either query (or either
predicate) that makes the index unusable fails here instead of silently
turning the run back into a full scan of the intents table
(docs/design/sql-profile-2026-10-21.md: 35 ms / 19 ms → 0.14 ms / 0.03 ms on
200k intents).
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.sdk.testing import override_instance
from sandloom_billing.services.intent_reconciler import IntentReconciler

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def reconciler_database(
    sandloom_core: Any, test_engine: AsyncEngine
) -> AsyncIterator[None]:
    # The reconciler opens its own UoW. Creating test_engine alone does not
    # configure the active application's provider (which may name a dev host).
    async with override_instance(
        sandloom_core,
        "database",
        driver="sqlalchemy",
        url=test_engine.url.render_as_string(hide_password=False),
    ):
        yield


@pytest.fixture
def captured(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "FROM billing_operation_intents" in statement and "ORDER BY" in statement:
            seen.append((statement, params))

    # A class-level listener leaves Engine._has_events set after removal,
    # which would leak into later tests (and switch the planner to its
    # general executor): monkeypatch restores the flag at teardown.
    monkeypatch.setattr(Engine, "_has_events", Engine._has_events)
    event.listen(Engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", capture)


async def _plan(engine: AsyncEngine, statement: str, params: Any) -> str:
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        rows = await conn.exec_driver_sql(f"EXPLAIN {statement}", params)
        plan = "\n".join(r[0] for r in rows)
        await conn.rollback()
    return plan


async def test_stale_pending_selection_uses_its_partial_index(
    test_engine: AsyncEngine, captured: list[tuple[str, Any]]
) -> None:
    await IntentReconciler._stale_pending(datetime.now(UTC), 5, 100)
    [(statement, params)] = captured
    assert "ix_billing_intents_pending_created" in await _plan(
        test_engine, statement, params
    )


async def test_owed_expiry_selection_uses_its_partial_index(
    test_engine: AsyncEngine, captured: list[tuple[str, Any]]
) -> None:
    await IntentReconciler._owed_expiries(5, 100)
    [(statement, params)] = captured
    assert "ix_billing_intents_owed_expiry" in await _plan(
        test_engine, statement, params
    )
