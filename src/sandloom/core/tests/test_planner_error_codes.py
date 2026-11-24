"""Every PlanningError the kernel raises prints a catalogue code (SANDLOOM-R023..R027).

Each raise site is a subclass of ``PlanningError`` with its own code; a bare
``PlanningError`` (a third-party adapter's) prints the R024 catch-all. The
tests build the failing declaration or call for every branch and check the
code AND that the catalogue entry exists with the fix (``sandloom explain``).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import String
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import di, planner
from sandloom.core.error_codes import code_for_exception, lookup
from sandloom.core.planner import (
    InvalidOperation,
    InvalidReadScope,
    JoinRead,
    ListOperation,
    NoExecutionAdapter,
    OperationSchemaMismatch,
    PlanningError,
    ReadOperation,
    Reads,
    UnencodableCursorValue,
)
from sandloom.core.sql_alchemy.planner_adapter import (
    SQLAlchemyExecutionAdapter,
    adapter_for_session,
)


class _Base(DeclarativeBase):
    pass


class _Thing(_Base):
    __tablename__ = "planner_error_codes_thing"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    maybe: Mapped[str | None] = mapped_column(String, index=True)


class _Id(BaseModel):
    id: int


class _Missing(BaseModel):
    nope: int


@pytest.fixture
def adapter() -> SQLAlchemyExecutionAdapter:
    # No connection is opened by compilation.
    return SQLAlchemyExecutionAdapter(
        create_async_engine("postgresql+asyncpg://u:p@localhost/test_planner_codes")
    )


def _assert_code(exc: BaseException, code: str, slug: str) -> None:
    assert code_for_exception(exc) == code, f"{type(exc).__name__}: {exc}"
    entry = lookup(code)
    assert entry is not None
    assert entry.slug == slug
    assert entry.fix  # `sandloom explain` carries a fix, not only a title
    assert isinstance(exc, PlanningError)


# --------------------------------------------------- R023: built wrongly

_INVALID: dict[str, Callable[[], object]] = {
    "empty by": lambda: ReadOperation(_Thing, _Id, by=()),
    "lock": lambda: ReadOperation(_Thing, _Id, lock="update"),
    "join read without join": lambda: JoinRead(_Thing, _Id, by=("id",)),
    "join read without a key": lambda: JoinRead(
        _Thing, _Id, by=(), join=_Thing, via="id"
    ),
    "join read argument twice": lambda: JoinRead(
        _Thing, _Id, by=("id",), join=_Thing, via="id", join_by={"id": "id"}
    ),
    "list without order_by": lambda: ListOperation(_Thing, _Id, order_by=()),
    "list join without via": lambda: ListOperation(_Thing, _Id, join=_Thing),
    "list max_limit": lambda: ListOperation(_Thing, _Id, max_limit=0),
    "list argument twice": lambda: ListOperation(
        _Thing, _Id, by=("id",), optional={"id": "name"}
    ),
    "list too many optional filters": lambda: ListOperation(
        _Thing,
        _Id,
        optional={f"a{i}": "name" for i in range(planner.MAX_OPTIONAL_FILTERS + 1)},
    ),
}


@pytest.mark.parametrize("build", list(_INVALID.values()), ids=list(_INVALID))
def test_an_invalid_declaration_prints_r023(build: Callable[[], object]) -> None:
    with pytest.raises(InvalidOperation) as raised:
        build()
    _assert_code(raised.value, "SANDLOOM-R023", "invalid-planner-operation")


def test_join_arguments_without_a_join_print_r023(
    adapter: SQLAlchemyExecutionAdapter,
) -> None:
    op = ListOperation(_Thing, _Id, joined_fields={"id": "id"})
    with pytest.raises(InvalidOperation) as raised:
        adapter.compile(op, optimized=True)
    _assert_code(raised.value, "SANDLOOM-R023", "invalid-planner-operation")


# ------------------------------------------- R024: does not fit the schema

_MISMATCH: dict[str, Callable[[], object]] = {
    "key is not a column": lambda: ReadOperation(_Thing, _Id, by=("nope",)),
    "result is no model": lambda: ReadOperation(_Thing, int),  # type: ignore[type-var]
    "result field missing": lambda: ReadOperation(_Thing, _Missing),
    "tenant_scoped declared": lambda: ReadOperation(_Thing, _Id, tenant_scoped=True),
    "nullable order": lambda: ListOperation(
        _Thing, _Id, order_by=("maybe",), require_index=False
    ),
    "filter is not a column": lambda: ListOperation(_Thing, _Id, where={"nope": 1}),
    "join read without a foreign key": lambda: JoinRead(
        _Thing, _Id, by=("id",), join=_Thing, via="name"
    ),
}


@pytest.mark.parametrize("build", list(_MISMATCH.values()), ids=list(_MISMATCH))
def test_a_schema_mismatch_prints_r024(
    adapter: SQLAlchemyExecutionAdapter, build: Callable[[], Any]
) -> None:
    op = build()
    with pytest.raises(OperationSchemaMismatch) as raised:
        adapter.compile(op, optimized=True)
    _assert_code(raised.value, "SANDLOOM-R024", "planner-schema-mismatch")


def test_a_bare_planning_error_prints_the_r024_catch_all() -> None:
    _assert_code(
        PlanningError("from a third-party adapter"),
        "SANDLOOM-R024",
        ("planner-schema-mismatch"),
    )


def test_the_specific_codes_win_over_the_catch_all() -> None:
    # MRO order: the subclass is looked up before PlanningError; the older
    # codes of this family stay what they were.
    assert code_for_exception(planner.SnapshotReadsUnavailable("x")) == "SANDLOOM-B027"
    assert code_for_exception(planner.UndeclaredOperation("x")) == "SANDLOOM-R022"
    assert code_for_exception(ValueError("not a planner error")) is None


# ------------------------------------------------ R025: no execution adapter


def test_a_database_without_an_adapter_prints_r025() -> None:
    provider = SimpleNamespace(driver="nodriver", planner_adapter=lambda: None)
    core = SimpleNamespace(database=lambda name: provider)
    with pytest.raises(NoExecutionAdapter) as raised:
        planner._adapter(core, "default")  # type: ignore[arg-type]
    assert "'nodriver'" in str(raised.value)
    _assert_code(raised.value, "SANDLOOM-R025", "planner-no-execution-adapter")


def test_a_session_without_an_async_engine_prints_r025() -> None:
    session = SimpleNamespace(bind=object())
    with pytest.raises(NoExecutionAdapter) as raised:
        adapter_for_session(session)  # type: ignore[arg-type]
    _assert_code(raised.value, "SANDLOOM-R025", "planner-no-execution-adapter")


# ------------------------------------------------------ R026: wrong scope


def test_reads_without_core_or_adapter_prints_r026() -> None:
    with pytest.raises(InvalidReadScope) as raised:
        Reads(None)
    _assert_code(raised.value, "SANDLOOM-R026", "invalid-planner-read-scope")


def test_an_unknown_isolation_prints_r026(
    adapter: SQLAlchemyExecutionAdapter,
) -> None:
    with pytest.raises(InvalidReadScope, match="unsupported read isolation") as raised:
        Reads(None, adapter=adapter, isolation="serializable")  # type: ignore[arg-type]
    _assert_code(raised.value, "SANDLOOM-R026", "invalid-planner-read-scope")


def test_repeatable_read_outside_a_shared_scope_prints_r026(
    adapter: SQLAlchemyExecutionAdapter,
) -> None:
    with pytest.raises(InvalidReadScope, match="read-only SCOPE") as raised:
        Reads(None, adapter=adapter, isolation="repeatable_read")
    _assert_code(raised.value, "SANDLOOM-R026", "invalid-planner-read-scope")


def _route(*calls: Any) -> Any:
    deps = [SimpleNamespace(call=call, dependencies=[]) for call in calls]
    return SimpleNamespace(path="/x", dependant=SimpleNamespace(dependencies=deps))


def test_two_read_scopes_on_one_route_print_r026() -> None:
    route = _route(di.get_planned_reads, di.get_snapshot_reads)
    with pytest.raises(InvalidReadScope, match="two read scopes") as raised:
        di._check_read_only_route(route)
    _assert_code(raised.value, "SANDLOOM-R026", "invalid-planner-read-scope")


def test_planned_reads_beside_a_unit_of_work_print_r026() -> None:
    route = _route(di.get_planned_reads, di.get_db)
    with pytest.raises(InvalidReadScope, match="Unit of Work") as raised:
        di._check_read_only_route(route)
    _assert_code(raised.value, "SANDLOOM-R026", "invalid-planner-read-scope")


# ------------------------------------------------- R027: cursor value type


def test_an_unencodable_cursor_value_prints_r027() -> None:
    with pytest.raises(UnencodableCursorValue, match="object") as raised:
        planner._tag(object())
    _assert_code(raised.value, "SANDLOOM-R027", "planner-unencodable-cursor-value")


# ----------------------------------------- no raise site is left without a code

_KERNEL = Path(planner.__file__).resolve().parents[1]


def _bare_planning_raises(source: str) -> list[int]:
    """Lines that ``raise PlanningError(...)`` / ``raise x.PlanningError(...)``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        func = node.exc.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "PlanningError":
            lines.append(node.lineno)
    return lines


def test_the_scan_finds_a_bare_raise() -> None:
    assert _bare_planning_raises("raise PlanningError('x')\n") == [1]
    assert _bare_planning_raises("raise _planner.PlanningError('x')\n") == [1]
    assert _bare_planning_raises("raise InvalidOperation('x')\n") == []


def test_the_kernel_raises_no_bare_planning_error() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in sorted(_KERNEL.rglob("*.py")):
        if "tests" in path.relative_to(_KERNEL).parts:
            continue
        scanned += 1
        offenders += [
            f"{path.relative_to(_KERNEL)}:{line}"
            for line in _bare_planning_raises(path.read_text())
        ]
    assert scanned > 100  # it walked the kernel, not an empty directory
    assert offenders == [], "raise a PlanningError subclass with a code instead"
