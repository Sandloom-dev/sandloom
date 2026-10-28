"""The admin user search matches substrings case-insensitively and its SQL
can use ``ix_users_search_trgm`` (pg_trgm GIN, identity chain 0006).

The planner only uses the trigram index when EVERY arm of the OR is
indexable: ``email`` is citext, whose own ILIKE operator a text opclass cannot
serve, so the query compares ``email::text`` (same matches). This test
captures the statements ``list_users`` really sends and EXPLAINs them with
seq scans off (docs/design/sql-profile-2026-10-21.md: 146 / 142 ms → 1.8 /
1.4 ms on 100k users).
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.services.user_service import UserService


@pytest.fixture
def captured(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if (
            "FROM users" in statement
            and "ILIKE" in statement
            and not (statement.startswith("EXPLAIN"))
        ):
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


@pytest.mark.asyncio
async def test_search_matches_any_column_case_insensitively(
    test_db: AsyncSession, make_user: Any
) -> None:
    await make_user("Grace.Hopper@Example.com", full_name="Rear Admiral")
    await make_user("ada@example.com", full_name="Ada Lovelace")

    by_email = await UserService.list_users(test_db, search="grace.hop")
    by_name = await UserService.list_users(test_db, search="LOVELACE")
    none = await UserService.list_users(test_db, search="turing")

    assert [u.email.lower() for u in by_email.users] == ["grace.hopper@example.com"]
    assert [u.full_name for u in by_name.users] == ["Ada Lovelace"]
    assert (by_email.total, by_name.total, none.total) == (1, 1, 0)


@pytest.mark.asyncio
async def test_search_statements_can_use_the_trigram_index(
    test_db: AsyncSession, captured: list[tuple[str, Any]]
) -> None:
    await UserService.list_users(test_db, search="hopper")
    assert len(captured) == 2  # the count and the page
    for statement, params in list(captured):
        # Bitmap scans only: on a near-empty table the planner would rather
        # walk ix_users_created_at for the ORDER BY page.
        await test_db.execute(text("SET LOCAL enable_seqscan = off"))
        await test_db.execute(text("SET LOCAL enable_indexscan = off"))
        conn = await test_db.connection()
        rows = await conn.exec_driver_sql(f"EXPLAIN {statement}", params)
        plan = "\n".join(r[0] for r in rows)
        assert "ix_users_search_trgm" in plan, plan
