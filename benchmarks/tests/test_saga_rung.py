"""Saga rung (``ladder/saga_rung.py``): the statement budget of ADR-0008.

Timings are machine-dependent; the statements each operation sends are not.
Pinned: the start adds three to the caller's work (the instance INSERT, the
outbox nudge and the post-commit mark-delivered UPDATE); a local step's
transition is five (lock, step upsert, instance update, outbox nudge, its
mark-delivered); a command step adds the command log's two (claim INSERT +
result UPDATE in the provider's own UoW).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ladder import saga_rung


async def test_saga_statement_budget(db_engine: Any) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS bench_saga_rows "
                "(id varchar(64) PRIMARY KEY, value integer NOT NULL)"
            )
        )
    try:
        report = await saga_rung.run(20)
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS bench_saga_rows"))
    rungs = report["rungs"]
    assert report["derived"]["start_path_statements"] == 3.0
    assert rungs["B0"]["statements"] == 1.0
    assert rungs["L1"]["statements"] == 5.0
    # +1 since the durable "started" marker of a command step's first
    # forward call (resolve-before-compensate after a crash, G-SAGA-06).
    assert rungs["C1"]["statements"] == 8.0
