"""The per-test schema reset (``sandloom.core.testing.database.reset_schema``).

It truncates only the tables that hold a row, so it must leave exactly what a
full ``TRUNCATE … RESTART IDENTITY CASCADE`` of every table leaves: all tables
empty (referencing rows included) and every owned sequence at its start, even
one a rolled-back insert advanced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, text
from sqlalchemy.exc import DBAPIError

from sandloom.core.testing import database as reset
from sandloom.core.tests.test_testing_ownership import scratch_db  # noqa: F401

metadata = MetaData()
parents = Table(
    "reset_parents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(20)),
)
children = Table(
    "reset_children",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("parent_id", Integer, ForeignKey("reset_parents.id"), nullable=False),
)
untouched = Table(
    "reset_untouched",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
)


@pytest.fixture
async def engine(scratch_db: str) -> AsyncIterator[Any]:  # noqa: F811
    from sandloom.core._internal.db_provider import build_disposable_engine

    eng = build_disposable_engine(scratch_db, pool_size=1, max_overflow=1)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _counts(engine: Any) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            t.name: (
                await conn.execute(text(f"SELECT count(*) FROM {t.name}"))
            ).scalar()
            for t in metadata.sorted_tables
        }


async def _next_id(engine: Any, table: Table) -> int:
    async with engine.connect() as conn:
        trans = await conn.begin()
        row = await conn.execute(table.insert().returning(table.c.id))
        new_id = int(row.scalar_one())
        await trans.rollback()
        return new_id


async def test_reset_empties_dirty_tables_and_their_referencing_rows(
    engine: Any,
) -> None:
    await reset.reset_schema(engine, metadata)  # builds
    await reset.reset_schema(engine, metadata)  # fast path on an empty schema
    async with engine.begin() as conn:
        pid = (
            await conn.execute(
                parents.insert().values(name="p").returning(parents.c.id)
            )
        ).scalar_one()
        await conn.execute(children.insert().values(parent_id=pid))
    await reset.reset_schema(engine, metadata)
    assert await _counts(engine) == dict.fromkeys(
        ("reset_parents", "reset_children", "reset_untouched"), 0
    )
    async with engine.connect() as conn:
        used = await conn.execute(
            text("SELECT count(*) FROM pg_sequences WHERE last_value IS NOT NULL")
        )
        assert used.scalar() == 0  # every sequence rewound (children's too)
    # Truncated tables restart their identity.
    assert await _next_id(engine, parents) == 1


async def test_reset_rewinds_a_sequence_a_rolled_back_insert_advanced(
    engine: Any,
) -> None:
    await reset.reset_schema(engine, metadata)
    for _ in range(3):
        await _next_id(engine, untouched)  # rolled back: table stays empty
    assert await _next_id(engine, untouched) == 4
    await reset.reset_schema(engine, metadata)
    assert await _next_id(engine, untouched) == 1


async def test_a_failing_probe_falls_back_to_truncating_everything(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await reset.reset_schema(engine, metadata)
    async with engine.begin() as conn:
        await conn.execute(untouched.insert())

    async def _refuse(*args: Any, **kwargs: Any) -> None:
        raise DBAPIError("probe", {}, Exception("row security"))

    monkeypatch.setattr(reset, "_truncate_dirty", _refuse)
    await reset.reset_schema(engine, metadata)
    assert (await _counts(engine))["reset_untouched"] == 0


async def test_the_probe_refuses_rather_than_trust_a_row_security_filter(
    engine: Any,
) -> None:
    """With ``row_security = off`` a query RLS would filter fails: a policy
    can never make a table holding rows look empty to the probe."""
    await reset.reset_schema(engine, metadata)
    async with engine.connect() as conn:
        trans = await conn.begin()
        await conn.execute(text("CREATE ROLE reset_probe_role NOLOGIN"))
        try:
            await conn.execute(
                text("ALTER TABLE reset_untouched ENABLE ROW LEVEL SECURITY")
            )
            await conn.execute(
                text("GRANT SELECT ON reset_untouched TO reset_probe_role")
            )
            await conn.execute(text("SET LOCAL ROLE reset_probe_role"))
            await conn.execute(text("SET LOCAL row_security = off"))
            with pytest.raises(DBAPIError, match="row-level security"):
                async with conn.begin_nested():
                    await conn.execute(text("SELECT 1 FROM reset_untouched LIMIT 1"))
        finally:
            await trans.rollback()  # the role and the policy vanish with it


async def test_a_table_holding_many_rows_is_truncated(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reset, "DELETE_ROW_LIMIT", 1)
    await reset.reset_schema(engine, metadata)
    async with engine.begin() as conn:
        for name in ("a", "b"):
            pid = (
                await conn.execute(
                    parents.insert().values(name=name).returning(parents.c.id)
                )
            ).scalar_one()
            await conn.execute(children.insert().values(parent_id=pid))
        await conn.execute(untouched.insert())
    await reset.reset_schema(engine, metadata)
    assert set((await _counts(engine)).values()) == {0}
    assert await _next_id(engine, parents) == 1
    assert await _next_id(engine, untouched) == 1


async def test_a_row_outside_the_schema_blocking_a_delete_falls_back(
    engine: Any,
) -> None:
    """A table the metadata does not know references a dirty one: the DELETE
    fails, and the full TRUNCATE … CASCADE empties both as before."""
    await reset.reset_schema(engine, metadata)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE reset_outsider (id int PRIMARY KEY,"
                " parent_id int NOT NULL REFERENCES reset_parents(id))"
            )
        )
        pid = (
            await conn.execute(
                parents.insert().values(name="p").returning(parents.c.id)
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO reset_outsider VALUES (1, :p)"), {"p": pid}
        )
    await reset.reset_schema(engine, metadata)
    assert (await _counts(engine))["reset_parents"] == 0
    async with engine.connect() as conn:
        left = await conn.execute(text("SELECT count(*) FROM reset_outsider"))
        assert left.scalar() == 0
