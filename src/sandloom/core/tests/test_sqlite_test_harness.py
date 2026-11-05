"""The test harness on SQLite (``design/infra-agnostic/spec.md``, slice 8c).

``SANDLOOM_TEST_DATABASE_URL=sqlite+aiosqlite:///<dir>/<name>_test.db`` runs a
module suite on a file: each xdist worker gets ``<name>_test_gw<N>.db`` in the
same directory, the file carries its owner beside it (``<file>.owner``), the
schema is built from the models and emptied between tests, and the harness's
sessions begin their transaction at their first write. ``skip_unless_db_feature``
skips a test whose database lacks a feature. PostgreSQL's harness paths are
unchanged (``test_testing_reset.py``, ``test_testing_ownership.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    insert,
    select,
    text,
)

from sandloom.core.drivers.schema_reset import (
    PostgresqlSchemaReset,
    SchemaReset,
    SqliteSchemaReset,
    schema_reset_for,
)
from sandloom.core.testing import database as harness
from sandloom.core.testing import ownership, workers
from sandloom.core.testing.infra import (
    DB_FEATURE_MARKER,
    db_feature_skip_reason,
    missing_db_features,
    skip_unless_db_feature,
)

metadata = MetaData()
parents = Table(
    "lite_parents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
)
children = Table(
    "lite_children",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("parent_id", Integer, ForeignKey("lite_parents.id"), nullable=False),
)


# -- workers, ownership, the disposable check ----------------------------------


def test_each_worker_gets_its_own_file_beside_the_named_one() -> None:
    url = "sqlite+aiosqlite:////srv/runs/app_test.db"
    assert workers.worker_database_url(url, "gw3") == (
        "sqlite+aiosqlite:////srv/runs/app_test_gw3.db"
    )
    assert workers.worker_database_url(
        "sqlite+aiosqlite:////srv/runs/app_test_gw3.db", "gw3"
    ) == ("sqlite+aiosqlite:////srv/runs/app_test_gw3.db")  # idempotent
    relative = workers.worker_database_url("sqlite+aiosqlite:///runs/a_test.db", "gw0")
    assert relative == "sqlite+aiosqlite:///runs/a_test_gw0.db"
    # PostgreSQL keeps its database-name suffix
    assert workers.worker_database_url(
        "postgresql+asyncpg://u:p@h/app_test", "gw1"
    ) == ("postgresql+asyncpg://u:p@h/app_test_gw1")


def test_a_sqlite_file_is_claimed_by_its_marker(tmp_path: Path) -> None:
    file = tmp_path / "claim_test.db"
    url = f"sqlite+aiosqlite:///{file}"
    workers.ensure_database(url)  # the directory; the file is claimed below
    ownership.claim_database(url)
    assert ownership.sqlite_file_owner(file) == ownership.current_owner()
    ownership._claimed.clear()
    ownership.claim_database(url)  # our own: no refusal
    ownership.stamp_sqlite_file(file, "someone-else:/elsewhere")
    ownership._claimed.clear()
    with pytest.raises(ownership.TestResourceOwnershipError, match="SQLite"):
        ownership.claim_database(url)


def test_only_a_test_file_of_this_run_is_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = f"sqlite+aiosqlite:///{tmp_path / 'run_test.db'}"
    monkeypatch.setenv("SANDLOOM_TEST_DATABASE_URL", named)
    harness.assert_disposable_test_database(named)
    harness.assert_disposable_test_database(
        f"sqlite+aiosqlite:///{tmp_path / 'run_test_gw2.db'}"
    )
    with pytest.raises(RuntimeError, match="contains 'test'"):
        # "test" in a directory above the file does not count
        harness.assert_disposable_test_database(
            f"sqlite+aiosqlite:///{tmp_path / 'tests' / 'app.db'}"
        )
    with pytest.raises(RuntimeError, match="SANDLOOM_TEST_DATABASE_URL"):
        harness.assert_disposable_test_database(
            f"sqlite+aiosqlite:///{tmp_path / 'other_test.db'}"
        )


# -- the reset ------------------------------------------------------------------


def test_each_database_has_its_reset() -> None:
    def engine(name: str) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name=name))

    assert isinstance(schema_reset_for(engine("postgresql")), PostgresqlSchemaReset)
    assert isinstance(schema_reset_for(engine("sqlite")), SqliteSchemaReset)
    assert type(schema_reset_for(engine("mysql"))) is SchemaReset


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncIterator[Any]:
    from sandloom.core._internal.db_provider import build_disposable_engine

    engine = build_disposable_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'reset_test.db'}", pool_size=2, max_overflow=2
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_sqlite_reset_empties_every_table_and_its_counters(lite: Any) -> None:
    await harness.reset_schema(lite, metadata)
    async with lite.begin() as conn:
        parent = (
            await conn.execute(insert(parents).values().returning(parents.c.id))
        ).scalar_one()
        await conn.execute(insert(children).values(parent_id=parent))
    await harness.reset_schema(lite, metadata)
    async with lite.connect() as conn:
        for table in (parents, children):
            assert (
                await conn.execute(text(f"SELECT count(*) FROM {table.name}"))
            ).scalar_one() == 0
        nxt = (
            await conn.execute(insert(parents).values().returning(parents.c.id))
        ).scalar_one()
    assert nxt == 1


# -- the harness's sessions: a transaction begun at its first write --------------


@pytest.mark.asyncio
async def test_a_harness_session_reads_every_commit_until_it_writes(lite: Any) -> None:
    """Until its first write the ``db`` session holds no lock and each read
    sees the last commit (READ COMMITTED, as on PostgreSQL); its first write
    takes the write lock, and another writer then waits for it."""
    await harness.reset_schema(lite, metadata)
    reset = schema_reset_for(lite)
    async with harness.open_test_session(lite) as db:
        assert (await db.execute(select(parents.c.id))).all() == []
        async with lite.begin() as other:  # commits beside the reading session
            await other.execute(insert(parents).values(id=1))
        assert (await db.execute(select(parents.c.id))).scalars().all() == [1]
        await db.execute(insert(parents).values(id=2))  # takes the write lock

        async def other_writer() -> None:
            async with reset.caller_engine(lite).begin() as conn:
                await conn.execute(insert(parents).values(id=3))

        waiting = asyncio.create_task(other_writer())
        await asyncio.sleep(0.2)
        assert not waiting.done()  # one writer: it waits for the session
        await db.commit()
        await asyncio.wait_for(waiting, 10)
    async with lite.connect() as conn:
        ids = (await conn.execute(select(parents.c.id))).scalars().all()
    assert sorted(ids) == [1, 2, 3]


@pytest.mark.asyncio
async def test_transaction_lock_takes_the_write_lock_before_any_write(
    lite: Any,
) -> None:
    from sandloom.core.sql_alchemy.portable import WRITE_LOCK_HELD, transaction_lock

    await harness.reset_schema(lite, metadata)
    async with harness.open_test_session(lite) as db:
        await db.execute(select(parents.c.id))
        assert not (await db.connection()).info.get(WRITE_LOCK_HELD)
        await transaction_lock(7, session=db)
        assert (await db.connection()).info.get(WRITE_LOCK_HELD)


# -- skip_unless_db_feature -------------------------------------------------------


class _Infra:
    def __init__(self, features: frozenset[str] | None) -> None:
        self._features = features

    def declared_features(self, capability: str, name: str) -> frozenset[str] | None:
        return self._features if name == "default" else None


def _item(*marks: Any) -> Any:
    class Item:
        def iter_markers(self, name: str) -> list[Any]:
            return [m.mark for m in marks if m.mark.name == name]

    return Item()


def test_skip_unless_db_feature_names_what_is_missing() -> None:
    core = SimpleNamespace(infra=_Infra(frozenset({"transactions", "json_path"})))
    mark = skip_unless_db_feature("arrays", "json_path")
    assert mark.mark.name == DB_FEATURE_MARKER
    assert missing_db_features(core, ["arrays", "json_path"]) == ["arrays"]
    reason = db_feature_skip_reason(core, _item(mark))
    assert reason is not None
    assert "database/default" in reason and "arrays" in reason
    assert (
        db_feature_skip_reason(core, _item(skip_unless_db_feature("json_path"))) is None
    )
    # an instance that is not configured lacks every feature
    other = skip_unless_db_feature("json_path", database="analytics")
    assert "database/analytics" in (db_feature_skip_reason(core, _item(other)) or "")
    with pytest.raises(TypeError):
        skip_unless_db_feature()


@skip_unless_db_feature("a_feature_no_database_has")
def test_a_marked_test_is_skipped_by_the_harness() -> None:
    raise AssertionError("the harness skips this test before it runs")
