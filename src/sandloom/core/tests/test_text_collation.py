"""Text order is the database's collation (risk fix 3's follow-up, risk fix 5).

``ORDER BY``, ``<`` / ``>``, ``min`` / ``max`` and ``DISTINCT`` on text (a
``String`` column, ``json_path(...)``) compare with the database's collation:
SQLite's ``BINARY`` is byte order; PostgreSQL's is usually linguistic (glibc
or ICU: ``'a' < 'B'``, punctuation ignored at the first level). A database
whose collation is ``C``, or a PostgreSQL built on musl libc (the Alpine
image the local test services use), sorts bytewise too, so the difference
is visible only on some lanes: the CI lanes run ``postgres:16`` (Debian,
glibc, ``en_US.utf8``), where :func:`test_plain_order_is_linguistic_where_the_server_is`
runs; elsewhere it skips, saying why.

The portable order is byte order: plain on SQLite, ``COLLATE "C"`` on
PostgreSQL (``docs/modules/database/conventions.md``), or sort in Python.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.testing.workers import worker_env_values

REPO = Path(__file__).resolve().parents[4]

_WORDS = ["b", "B", "a", "A", "a-c", "ab", "Z", "_x"]
#: Python's order of the UTF-8 bytes: what SQLite's BINARY collation gives.
BYTEWISE = sorted(_WORDS, key=lambda word: word.encode())

_TABLE = sa.Table(
    "rf5_collation_words",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("word", sa.String(20), nullable=False),
)


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[AsyncEngine, None]:
    url = worker_env_values(REPO / ".env.test")["DATABASE_URL"]
    if sa.engine.make_url(url).get_backend_name() != "postgresql":
        pytest.skip("the test environment's DATABASE_URL is not PostgreSQL")
    engine = driver.build_disposable_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'rf5.db'}")
    yield engine
    await engine.dispose()


async def _ordered(engine: AsyncEngine, *, byte_order: bool) -> list[str]:
    """The words in the database's order (``byte_order``: ``COLLATE "C"``
    on PostgreSQL; SQLite's default is already byte order), rolled back."""
    column = _TABLE.c.word
    if byte_order and engine.dialect.name == "postgresql":
        column = column.collate("C")
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(_TABLE.metadata.create_all)
            await connection.execute(
                sa.insert(_TABLE),
                [{"id": i, "word": w} for i, w in enumerate(_WORDS)],
            )
            rows = await connection.execute(
                sa.select(_TABLE.c.word).order_by(column, _TABLE.c.id)
            )
            return [row.word for row in rows]
        finally:
            await transaction.rollback()


async def test_sqlite_orders_text_bytewise(lite: AsyncEngine) -> None:
    assert await _ordered(lite, byte_order=False) == BYTEWISE


async def test_collate_c_orders_bytewise_on_postgresql(pg: AsyncEngine) -> None:
    assert await _ordered(pg, byte_order=True) == BYTEWISE


async def test_plain_order_is_linguistic_where_the_server_is(pg: AsyncEngine) -> None:
    async with pg.connect() as connection:
        linguistic = await connection.scalar(
            sa.select(sa.literal("a", sa.String) < sa.literal("B", sa.String))
        )
        collation = await connection.scalar(
            sa.text(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            )
        )
    if not linguistic:
        pytest.skip(
            f"this PostgreSQL sorts text bytewise (collation {collation!r}; C, or "
            "musl libc as in the Alpine image): no difference to see here; the "
            "CI lanes' postgres:16 (Debian, glibc) sees it"
        )
    plain = await _ordered(pg, byte_order=False)
    assert plain != BYTEWISE, (collation, plain)
    assert plain.index("a") < plain.index("B"), (collation, plain)
    assert sorted(plain) == sorted(BYTEWISE)
