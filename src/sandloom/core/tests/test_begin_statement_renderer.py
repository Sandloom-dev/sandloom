"""Read-only and snapshot units through the renderer (spec infra-agnostic,
slice 7): ``begin_statement`` / ``transaction_options`` and SANDLOOM-B027.

On PostgreSQL every BEGIN text and every execution option is pinned against
the code before this slice (its literals and its ``_read_only``, kept here as
the oracle). SQLite declares neither ``snapshot_reads`` nor
``readonly_transactions`` yet: a snapshot scope is refused, a read-only unit
gets no options.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine

from sandloom.core import di, error_codes, planner
from sandloom.core.drivers.sql_dialects import POSTGRESQL, SQLITE
from sandloom.core.sql_alchemy import portable
from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter
from sandloom.core.sql_alchemy.read_only_policy import READ_ONLY_OPTION

# --- the oracle: the code before slice 7 -----------------------------------

#: forms._READ_ONLY_BEGIN before slice 7.
_OLD_READ_ONLY_BEGIN = "BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY"
#: di._read_only_uow_factory's option-engine options before slice 7.
_OLD_DI_OPTIONS = {READ_ONLY_OPTION: True, "postgresql_readonly": True}


def _old_read_only(sync: Any, isolation: str, server_rc: bool) -> None:
    """The planner adapter's ``_read_only`` before slice 7 (PostgreSQL)."""
    if isolation == "READ COMMITTED" and server_rc:
        sync.execution_options(postgresql_readonly=True)
    else:
        sync.execution_options(postgresql_readonly=True, isolation_level=isolation)


class _Sync:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execution_options(self, **options: Any) -> None:
        self.calls.append(options)


def _conn(sync: _Sync, default_level: str) -> Any:
    dialect = SimpleNamespace(default_isolation_level=default_level)
    return SimpleNamespace(
        sync_connection=sync, sync_engine=SimpleNamespace(dialect=dialect)
    )


@pytest.fixture
def pg_engine() -> Any:
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/x")
    yield engine
    engine.sync_engine.dispose()


def _lite_engine() -> Any:
    # An engine shape the adapter reads (no aiosqlite needed: no I/O here).
    dialect = sqlite.dialect()
    return SimpleNamespace(
        dialect=dialect, sync_engine=SimpleNamespace(dialect=dialect)
    )


# --- the renderer members ---------------------------------------------------


def test_the_members_are_required_by_the_contract() -> None:
    assert "begin_statement" in portable.REQUIRED_MEMBERS
    assert "transaction_options" in portable.REQUIRED_MEMBERS


def test_postgresql_begin_texts() -> None:
    begin = POSTGRESQL.begin_statement
    assert begin(write=False, read_only=True, snapshot=False) == _OLD_READ_ONLY_BEGIN
    assert (
        begin(write=False, read_only=True, snapshot=True)
        == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert begin(write=True, read_only=False, snapshot=False) is None
    assert begin(write=False, read_only=False, snapshot=False) is None
    assert (
        begin(write=True, read_only=False, snapshot=True)
        == "BEGIN ISOLATION LEVEL REPEATABLE READ"
    )


def test_postgresql_options_are_the_old_keywords_in_order() -> None:
    options = POSTGRESQL.transaction_options
    assert list(options(read_only=True, snapshot=None).items()) == [
        ("postgresql_readonly", True)
    ]
    assert list(options(read_only=True, snapshot=False).items()) == [
        ("postgresql_readonly", True),
        ("isolation_level", "READ COMMITTED"),
    ]
    assert list(options(read_only=True, snapshot=True).items()) == [
        ("postgresql_readonly", True),
        ("isolation_level", "REPEATABLE READ"),
    ]
    assert dict(options(read_only=False, snapshot=None)) == {}


def test_the_answers_are_precomputed() -> None:
    # Hot paths ask once per adapter; the renderer answers from a table.
    first = POSTGRESQL.transaction_options(read_only=True, snapshot=True)
    assert first is POSTGRESQL.transaction_options(read_only=True, snapshot=True)
    with pytest.raises(TypeError):
        first["isolation_level"] = "SERIALIZABLE"  # type: ignore[index]


def test_a_writing_read_only_unit_is_refused() -> None:
    for renderer in (POSTGRESQL, SQLITE):
        with pytest.raises(ValueError):
            renderer.begin_statement(write=True, read_only=True, snapshot=False)
    with pytest.raises(ValueError):
        portable.begin_statement_for(
            "postgresql", write=True, read_only=True, snapshot=False
        )


def test_sqlite_has_the_plain_begin_and_no_options() -> None:
    assert "snapshot_reads" not in SQLITE.features()
    assert "readonly_transactions" not in SQLITE.features()
    for read_only in (False, True):
        for snapshot in (False, True):
            assert (
                SQLITE.begin_statement(
                    write=False, read_only=read_only, snapshot=snapshot
                )
                is None
            )
        for level in (None, False, True):
            assert (
                dict(SQLITE.transaction_options(read_only=read_only, snapshot=level))
                == {}
            )


def test_helpers_without_a_renderer() -> None:
    assert (
        portable.begin_statement_for(
            "no-such-db", write=False, read_only=True, snapshot=False
        )
        is None
    )
    assert (
        dict(
            portable.transaction_options_for(
                "no-such-db", read_only=True, snapshot=True
            )
        )
        == {}
    )


# --- the planner adapter (the oracle: its _read_only before) ----------------


@pytest.mark.parametrize("server_level", ["READ COMMITTED", "REPEATABLE READ"])
@pytest.mark.parametrize("snapshot", [False, True])
def test_adapter_read_only_options_are_the_old_ones(
    pg_engine: Any, server_level: str, snapshot: bool
) -> None:
    adapter = SQLAlchemyExecutionAdapter(pg_engine)
    new, old = _Sync(), _Sync()
    adapter._read_only(_conn(new, server_level), snapshot)
    isolation = "REPEATABLE READ" if snapshot else "READ COMMITTED"
    _old_read_only(old, isolation, server_level == "READ COMMITTED")
    assert new.calls == old.calls
    assert [list(c) for c in new.calls] == [list(c) for c in old.calls]


def test_adapter_optimized_begin_is_the_old_literal(pg_engine: Any) -> None:
    adapter = SQLAlchemyExecutionAdapter(pg_engine)
    assert adapter._read_only_begin._begin_sql == _OLD_READ_ONLY_BEGIN
    assert adapter._raw_executor is True
    assert adapter._snapshot_reads is True


def test_adapter_without_readonly_transactions_sets_nothing() -> None:
    adapter = SQLAlchemyExecutionAdapter(_lite_engine())
    sync = _Sync()
    adapter._read_only(_conn(sync, "SERIALIZABLE"))
    assert sync.calls == []
    assert adapter._raw_executor is False


def test_snapshot_unit_without_snapshot_reads_is_refused_before_io() -> None:
    adapter = SQLAlchemyExecutionAdapter(_lite_engine())
    assert adapter.read_unit() is not None  # a fresh snapshot per statement
    with pytest.raises(planner.SnapshotReadsUnavailable, match="SANDLOOM-B027"):
        adapter.read_unit(snapshot=True)


def test_reads_ask_the_adapter_for_a_snapshot_unit() -> None:
    asked: list[bool] = []

    class _Adapter:
        def read_unit(self, *, snapshot: bool = False) -> Any:
            asked.append(snapshot)
            return object()

    for isolation, expected in (("read_committed", False), ("repeatable_read", True)):
        reads = planner.Reads(
            None, adapter=_Adapter(), shared=True, isolation=isolation
        )
        reads._read_unit(_Adapter(), open_new=True)
        assert asked[-1] is expected


# --- di: the read-only session's option-engine ------------------------------


def test_read_only_session_options_are_the_old_ones(pg_engine: Any) -> None:
    provider = SimpleNamespace(engine=pg_engine)
    core = SimpleNamespace(database=lambda name: provider)
    factory = di._read_only_uow_factory(core)  # type: ignore[arg-type]
    unit = factory()
    bind = unit._session_factory.kw["bind"]
    options = bind.get_execution_options()
    assert {k: options[k] for k in _OLD_DI_OPTIONS} == _OLD_DI_OPTIONS
    assert "isolation_level" not in options


# --- B027 at startup and in the catalogue -----------------------------------


class _Infra:
    def __init__(self, features: frozenset[str] | None) -> None:
        self._features = features

    def advertised_features(self, capability: str, name: str = "default") -> Any:
        assert (capability, name) == ("database", "default")
        return self._features


def _route(path: str, *calls: Any) -> Any:
    deps = [SimpleNamespace(call=c, dependencies=[]) for c in calls]
    return SimpleNamespace(path=path, dependant=SimpleNamespace(dependencies=deps))


def test_startup_refuses_a_snapshot_route_without_snapshot_reads() -> None:
    routes = [
        _route("/consistent", di.get_snapshot_reads),
        _route("/plain", di.get_planned_reads),
    ]
    problems = planner.snapshot_reads_problems(
        routes, _Infra(frozenset({"transactions"}))
    )
    assert len(problems) == 1
    assert "'/consistent'" in problems[0]
    assert "SANDLOOM-B027" in problems[0]


def test_startup_accepts_snapshot_reads_and_unknown_instances() -> None:
    routes = [_route("/consistent", di.get_snapshot_reads)]
    assert (
        planner.snapshot_reads_problems(routes, _Infra(frozenset({"snapshot_reads"})))
        == []
    )
    assert planner.snapshot_reads_problems(routes, _Infra(None)) == []
    assert planner.snapshot_reads_problems(routes, None) == []


def test_b027_is_catalogued() -> None:
    entry = error_codes.get("SANDLOOM-B027")
    assert entry.docs == "docs/modules/database/units.md"
    assert (
        error_codes.code_for_exception(planner.SnapshotReadsUnavailable("x"))
        == "SANDLOOM-B027"
    )
    assert issubclass(planner.SnapshotReadsUnavailable, planner.PlanningError)


def test_postgresql_dialect_is_postgresql() -> None:
    # The fixtures above stand for the dialects the renderers are named for.
    assert POSTGRESQL.name == postgresql.dialect.name
    assert SQLITE.name == sqlite.dialect.name
