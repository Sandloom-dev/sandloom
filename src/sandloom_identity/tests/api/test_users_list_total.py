"""``GET /api/v1/users``: ``X-Total-Count`` by default, no COUNT with
``include_total=false``, and the substring ``search`` filter
(docs/design/sql-profile-2026-10-21.md). The body stays a bare list."""

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.models.user import User


@pytest.fixture
def statements(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    seen: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    # A class-level listener leaves Engine._has_events set after removal,
    # which would leak into later tests (and switch the planner to its
    # general executor): monkeypatch restores the flag at teardown.
    monkeypatch.setattr(Engine, "_has_events", Engine._has_events)
    event.listen(Engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", capture)


async def _seed(test_db: AsyncSession) -> None:
    for i, name in enumerate(["Ada Lovelace", "Grace Hopper", "Alan Turing"]):
        test_db.add(
            User(
                email=f"listed{i}@example.com",
                full_name=name,
                hashed_password="dummy_hash",
                is_verified=True,
            )
        )
    await test_db.commit()


def _counts(statements: list[str]) -> int:
    return sum("count(*)" in s.lower() and "FROM users" in s for s in statements)


@pytest.mark.asyncio
async def test_total_header_by_default(
    async_client: AsyncClient,
    test_db: AsyncSession,
    admin_headers: dict[str, str],
    statements: list[str],
) -> None:
    await _seed(test_db)
    statements.clear()
    resp = await async_client.get("/api/v1/users?limit=2", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list) and len(resp.json()) == 2
    # 3 seeded + the admin; the page is limited, the total is not.
    assert int(resp.headers["X-Total-Count"]) == 4
    assert _counts(statements) == 1


@pytest.mark.asyncio
async def test_include_total_false_skips_the_count(
    async_client: AsyncClient,
    test_db: AsyncSession,
    admin_headers: dict[str, str],
    statements: list[str],
) -> None:
    await _seed(test_db)
    statements.clear()
    resp = await async_client.get(
        "/api/v1/users?limit=2&include_total=false", headers=admin_headers
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert "X-Total-Count" not in resp.headers
    assert _counts(statements) == 0


@pytest.mark.asyncio
async def test_search_filters_by_substring(
    async_client: AsyncClient, test_db: AsyncSession, admin_headers: dict[str, str]
) -> None:
    await _seed(test_db)
    resp = await async_client.get("/api/v1/users?q=HOPP", headers=admin_headers)
    assert resp.status_code == 200
    assert [u["full_name"] for u in resp.json()] == ["Grace Hopper"]
    assert resp.headers["X-Total-Count"] == "1"
    resp = await async_client.get("/api/v1/users?q=", headers=admin_headers)
    assert resp.status_code == 422
