"""The SQL a module may write answers the same on every shipped database
(SANDLOOM-C031, risk fix 2).

- Every name of ``PORTABLE_SQL_FUNCTIONS`` runs, as ``func.<name>``, on the
  test PostgreSQL and on a SQLite file, and gives the same Python value, of
  the same type, on both: the list is proven, not taken from SQLAlchemy's
  registry. The names left out on purpose (``lower``, ``upper``, ``avg``)
  keep their cases, which answer differently on the two databases, so one
  put back on the list fails here.
- The ``pg`` parameter runs only where the test environment's
  ``DATABASE_URL`` is PostgreSQL: a SQLite run skips it rather than compare
  SQLite with itself.
- ``WallClock()``, the current-time form a module uses instead of
  ``func.now()``, reads back as an aware UTC time with microseconds on both,
  in the text format SQLite stores ``UtcDateTime`` binds in, so the value
  read back finds its row again and compares with bound times in SQL.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sandloom.core.descriptors.database_forms import PORTABLE_SQL_FUNCTIONS
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.sql_alchemy.portable import UtcDateTime, WallClock
from sandloom.core.testing.workers import worker_env_values

REPO = Path(__file__).resolve().parents[4]


def _pg_url() -> str:
    return worker_env_values(REPO / ".env.test")["DATABASE_URL"]


@pytest_asyncio.fixture
async def pg() -> AsyncGenerator[AsyncEngine, None]:
    url = _pg_url()
    if sa.engine.make_url(url).get_backend_name() != "postgresql":
        pytest.skip("the test environment's DATABASE_URL is not PostgreSQL")
    engine = driver.build_disposable_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'rf2.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture(params=["pg", "lite"])
def engine(request: pytest.FixtureRequest) -> AsyncEngine:
    """The test PostgreSQL, then a SQLite file: every shipped renderer."""
    engine: AsyncEngine = request.getfixturevalue(request.param)
    return engine


async def _in_rolled_back(
    engine: AsyncEngine, metadata: sa.MetaData, rows: list[dict[str, Any]], body: Any
) -> Any:
    """``body(connection)`` over ``metadata``'s table filled with ``rows``,
    all rolled back (nothing is left in the test PostgreSQL)."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(metadata.create_all)
            if rows:
                table = next(iter(metadata.tables.values()))
                await connection.execute(sa.insert(table), rows)
            return await body(connection)
        finally:
            await transaction.rollback()


# -- the portable SQL functions ------------------------------------------------

_VALUES = sa.Table(
    "rf2_portable_values",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("n", sa.Integer, nullable=True),
    sa.Column("m", sa.Integer, nullable=True),
    sa.Column("s", sa.String(20), nullable=True),
)
_ROWS: list[dict[str, Any]] = [
    {"id": 1, "n": 4, "m": 1, "s": "Ab"},
    {"id": 2, "n": -1, "m": 2, "s": "cdE"},
    {"id": 3, "n": None, "m": 2, "s": None},
]
_n, _m, _s = _VALUES.c.n, _VALUES.c.m, _VALUES.c.s

#: Each name, its cases: ``(label, expression, kind, expected)``. ``kind``:
#: ``"agg"`` over the table, ``"row"`` one value per row by id, ``"scalar"``
#: without a table. The expected value is the Python one, type included.
_CASES: dict[str, list[tuple[str, Any, str, Any]]] = {
    "count": [
        ("count(*)", sa.func.count(), "agg", 3),
        ("count(n)", sa.func.count(_n), "agg", 2),
    ],
    "sum": [("sum(n)", sa.func.sum(_n), "agg", 3)],
    "min": [
        ("min(n)", sa.func.min(_n), "agg", -1),
        ("min(s)", sa.func.min(_s), "agg", "Ab"),
    ],
    "max": [
        ("max(n)", sa.func.max(_n), "agg", 4),
        ("max(s)", sa.func.max(_s), "agg", "cdE"),
    ],
    "coalesce": [("coalesce(n, 0)", sa.func.coalesce(_n, 0), "row", [4, -1, 0])],
    "nullif": [("nullif(n, 4)", sa.func.nullif(_n, 4), "row", [None, -1, None])],
    "abs": [("abs(n)", sa.func.abs(_n), "row", [4, 1, None])],
    "length": [("length(s)", sa.func.length(_s), "row", [2, 3, None])],
    "char_length": [("char_length(s)", sa.func.char_length(_s), "row", [2, 3, None])],
    # Left out: answers that differ (test_the_left_out_functions_differ).
    "lower": [
        ("lower(s)", sa.func.lower(_s), "row", ["ab", "cde", None]),
        ("lower('ÄÖÉ')", sa.func.lower(sa.literal("ÄÖÉ")), "scalar", "äöé"),
    ],
    "upper": [
        ("upper(s)", sa.func.upper(_s), "row", ["AB", "CDE", None]),
        ("upper('äöé')", sa.func.upper(sa.literal("äöé")), "scalar", "ÄÖÉ"),
    ],
    "avg": [("avg(m)", sa.func.avg(_m), "agg", 5 / 3)],
}
_LEFT_OUT = frozenset({"lower", "upper", "avg"})


def _same(answer: Any, expected: Any) -> bool:
    """Equal, and of the same Python type (each item of a list)."""
    if isinstance(expected, list):
        return (
            isinstance(answer, list)
            and len(answer) == len(expected)
            and all(_same(a, e) for a, e in zip(answer, expected, strict=True))
        )
    return type(answer) is type(expected) and answer == expected


def test_every_portable_name_is_proven_here() -> None:
    """A name joins ``PORTABLE_SQL_FUNCTIONS`` only with a case below, and
    none of the names left out on purpose is on it."""
    assert set(PORTABLE_SQL_FUNCTIONS) == set(_CASES) - _LEFT_OUT


async def _function_answers(engine: AsyncEngine) -> dict[str, Any]:
    async def body(connection: AsyncConnection) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for cases in _CASES.values():
            for label, expression, kind, _ in cases:
                if kind == "row":
                    query = sa.select(expression).order_by(_VALUES.c.id)
                    answers[label] = list((await connection.execute(query)).scalars())
                    continue
                query = sa.select(expression)
                if kind == "agg":
                    query = query.select_from(_VALUES)
                answers[label] = (await connection.execute(query)).scalar_one()
        # An aggregate over no rows is NULL on both (count is 0).
        none = sa.select(sa.func.sum(_n), sa.func.max(_s), sa.func.count()).where(
            _VALUES.c.id > 99
        )
        answers["empty"] = list((await connection.execute(none)).one())
        return answers

    answers: dict[str, Any] = await _in_rolled_back(
        engine, _VALUES.metadata, _ROWS, body
    )
    return answers


def _expected(names: Iterable[str]) -> dict[str, Any]:
    return {label: value for name in names for label, _, _, value in _CASES[name]}


async def test_every_portable_function_answers_the_same(engine: AsyncEngine) -> None:
    answers = await _function_answers(engine)
    expected = _expected(PORTABLE_SQL_FUNCTIONS)
    expected["empty"] = [None, None, 0]
    wrong = {
        label: (answers[label], value)
        for label, value in expected.items()
        if not _same(answers[label], value)
    }
    assert wrong == {}, engine.dialect.name


async def test_the_left_out_functions_differ(
    pg: AsyncEngine, lite: AsyncEngine
) -> None:
    """Each name left out answers one of its cases differently on the two
    databases (value or type): ``lower`` / ``upper`` fold ASCII only on
    SQLite, ``avg`` is a ``Decimal`` on PostgreSQL and a ``float`` on SQLite."""
    on_pg, on_lite = await _function_answers(pg), await _function_answers(lite)
    for name in _LEFT_OUT:
        labels = [label for label, _, _, _ in _CASES[name]]
        assert any(not _same(on_pg[label], on_lite[label]) for label in labels), (
            name,
            {label: (on_pg[label], on_lite[label]) for label in labels},
        )


# -- WallClock: the current time, the same on both -----------------------------

_TIMES = sa.Table(
    "rf2_wall_clock_times",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("at", UtcDateTime(), nullable=True),
    sa.Column("expires_at", UtcDateTime(), nullable=True),
)
#: The database server's clock and this process's may differ a little.
_SKEW = timedelta(seconds=5)


async def _wall_clock_answers(engine: AsyncEngine) -> dict[str, Any]:
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    rows = [{"id": 1, "expires_at": past}, {"id": 2, "expires_at": future}]

    async def body(connection: AsyncConnection) -> dict[str, Any]:
        t = _TIMES
        before = datetime.now(UTC)
        # As invitation_service writes it: the stamp and the comparison in SQL.
        await connection.execute(
            sa.update(t).where(t.c.expires_at <= WallClock()).values(at=WallClock())
        )
        await connection.execute(
            sa.insert(t).values(id=3, at=WallClock(), expires_at=WallClock())
        )
        after = datetime.now(UTC)
        stamped = dict(
            (await connection.execute(sa.select(t.c.id, t.c.at).order_by(t.c.id))).all()
        )
        selected = (await connection.execute(sa.select(WallClock()))).scalar_one()
        at = stamped[1]
        # The value read back, bound again, finds its row: one text format.
        found = list(
            (
                await connection.execute(
                    sa.select(t.c.id).where(t.c.at == at).order_by(t.c.id)
                )
            ).scalars()
        )
        open_ids = list(
            (
                await connection.execute(
                    sa.select(t.c.id)
                    .where(t.c.expires_at > WallClock())
                    .order_by(t.c.id)
                )
            ).scalars()
        )
        return {
            "before": before,
            "after": after,
            "stamped": stamped,
            "selected": selected,
            "found": found,
            "open": open_ids,
        }

    answers: dict[str, Any] = await _in_rolled_back(engine, _TIMES.metadata, rows, body)
    return answers


async def test_wall_clock_reads_aware_utc_microseconds(engine: AsyncEngine) -> None:
    answers = await _wall_clock_answers(engine)
    stamped = answers["stamped"]
    assert stamped[2] is None  # not yet expired: not stamped
    for value in (stamped[1], stamped[3], answers["selected"]):
        assert isinstance(value, datetime)
        assert value.tzinfo is not None and value.utcoffset() == timedelta(0)
        assert answers["before"] - _SKEW <= value <= answers["after"] + _SKEW
    # Found again by equality with the value read back: on SQLite the stored
    # text has six fraction digits, as a bound UtcDateTime has.
    # (Row 3, stamped by the next statement, may share SQLite's millisecond.)
    assert answers["found"][0] == 1
    # Compared in SQL with bound times: only the one that has not expired.
    assert answers["open"] == [2]


async def test_wall_clock_round_trips_through_a_utc_column(engine: AsyncEngine) -> None:
    """A stamped value written back through ``UtcDateTime`` is the same
    instant to the microsecond, on both databases."""

    async def body(connection: AsyncConnection) -> tuple[datetime, datetime]:
        t = _TIMES
        await connection.execute(sa.insert(t).values(id=1, at=WallClock()))
        stamped = (await connection.execute(sa.select(t.c.at))).scalar_one()
        await connection.execute(sa.insert(t).values(id=2, at=stamped))
        again = (
            await connection.execute(sa.select(t.c.at).where(t.c.id == 2))
        ).scalar_one()
        return stamped, again

    stamped, again = await _in_rolled_back(engine, _TIMES.metadata, [], body)
    assert again == stamped
    assert again.microsecond == stamped.microsecond
    assert again.tzinfo is not None and again.utcoffset() == timedelta(0)


async def test_sqlite_now_is_whole_seconds_outside_the_stored_format(
    lite: AsyncEngine,
) -> None:
    """Why ``func.now()`` is not portable: on SQLite it is ``CURRENT_TIMESTAMP``,
    whole seconds without a fraction, so a text comparison with a stored
    ``UtcDateTime`` (``YYYY-MM-DD HH:MM:SS.ffffff``) is off within a second."""
    async with lite.connect() as connection:
        now = (await connection.execute(sa.select(sa.func.now()))).scalar_one()
        clock = (
            await connection.execute(sa.select(sa.cast(WallClock(), sa.String)))
        ).scalar_one()
    assert "." not in str(now)
    assert len(str(clock).split(".")[1]) == 6, clock
