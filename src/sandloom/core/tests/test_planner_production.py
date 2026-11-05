"""Conservative eligibility: an optimized read must preserve ORM semantics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import ForeignKey, Index, String, Uuid, event, make_url, text
from sqlalchemy import bindparam as sa_bindparam
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import TypeDecorator

from sandloom.core.config.settings import Settings
from sandloom.core.planner import JoinRead, ListOperation, PlanningError, ReadOperation
from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter
from sandloom.core.sql_alchemy.planner_safety import covering_indexes, unique_keys
from sandloom.core.sql_alchemy.portable import (
    fulltext_index,
    partial_index,
    search_index,
)


class _Base(DeclarativeBase):
    pass


class _Codec(TypeDecorator[str]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        return None if value is None else "encoded:" + value

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        return None if value is None else value.removeprefix("encoded:")


class _Parent(_Base):
    __tablename__ = "planner_production_parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    code: Mapped[str] = mapped_column(_Codec())
    optional: Mapped[str | None] = mapped_column(String, index=True)
    partial: Mapped[str] = mapped_column(String)
    emulated_uuid: Mapped[Any] = mapped_column(Uuid(native_uuid=False))
    variant: Mapped[str] = mapped_column(String().with_variant(_Codec(), "postgresql"))
    __table_args__ = (
        Index("ix_planner_production_enabled_id", "enabled", "id"),
        partial_index("ix_planner_partial", "partial", where="id > 10", unique=True),
        Index("ix_planner_production_code", "code"),
        search_index("ix_planner_production_search", "variant"),
        fulltext_index("ix_planner_production_fts", "variant", language="simple"),
    )


class _Child(_Base):
    __tablename__ = "planner_production_child"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey(_Parent.id))


class _Polymorphic(_Base):
    __tablename__ = "planner_production_polymorphic"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "root"}


class _Deferred(_Base):
    __tablename__ = "planner_production_deferred"
    id: Mapped[int] = mapped_column(primary_key=True)
    secret: Mapped[str] = mapped_column(deferred=True, deferred_raiseload=True)


class _CaseInsensitive(_Base):
    __tablename__ = "planner_production_citext"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    alias: Mapped[str] = mapped_column(CITEXT().with_variant(String(), "postgresql"))
    __table_args__ = (Index("ix_planner_citext_email_id", "email", "id"),)


class _Owner(_Base):
    __tablename__ = "planner_production_owner"
    id: Mapped[int] = mapped_column(primary_key=True)
    handle: Mapped[str] = mapped_column(String, unique=True)
    label: Mapped[str] = mapped_column(String, index=True)


class _Detail(_Base):
    """One-to-one with its owner (``owner_id`` UNIQUE)."""

    __tablename__ = "planner_production_detail"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey(_Owner.id), unique=True)


class _Many(_Base):
    """Many per owner (``owner_id`` not unique)."""

    __tablename__ = "planner_production_many"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey(_Owner.id), index=True)


class _Id(BaseModel):
    id: int


class _Email(BaseModel):
    id: int
    email: str


class _Alias(BaseModel):
    id: int
    alias: str


class _Encoded(BaseModel):
    id: int
    code: str


class _Secret(BaseModel):
    id: int
    secret: str


@pytest.fixture
def adapter():
    # No connection is opened by compilation.
    return SQLAlchemyExecutionAdapter(
        create_async_engine("postgresql+asyncpg://u:p@localhost/test_planner")
    )


def test_nonunique_single_read_uses_orm_cardinality_check(adapter):
    plan = adapter.compile(ReadOperation(_Parent, _Id, by=("name",)), optimized=True)
    assert plan.executor == "general"
    assert "unique" in plan.reason


@pytest.mark.parametrize(
    "op",
    [
        ReadOperation(_Parent, _Encoded),
        ReadOperation(_Parent, _Id, by=("id", "code")),
        ReadOperation(_Parent, _Id, by=("id", "emulated_uuid")),
        ReadOperation(_Parent, _Id, by=("id", "variant")),
        ListOperation(_Parent, _Id, where={"code": "abc"}, order_by=("id",)),
        JoinRead(
            _Child, _Id, join=_Parent, via="parent_id", join_where={"code": "abc"}
        ),
    ],
)
def test_custom_types_never_bypass_bind_or_result_processors(adapter, op):
    plan = adapter.compile(op, optimized=True)
    assert plan.executor == "general"
    assert "type" in plan.reason


@pytest.mark.parametrize(
    "op",
    [
        ReadOperation(_Parent, _Id, by=("id", "enabled")),
        ListOperation(_Parent, _Id, by=("enabled",), order_by=("id",)),
        ListOperation(_Parent, _Id, where={"enabled": 1}, order_by=("id",)),
        ListOperation(_Parent, _Id, order_by=("enabled", "id"), require_index=False),
        JoinRead(_Child, _Id, join=_Parent, via="parent_id", join_where={"enabled": 1}),
    ],
)
def test_boolean_parameters_preserve_sqlalchemy_bind_processing(adapter, op):
    # SQLAlchemy accepts 0/1 for Boolean and normalizes to False/True;
    # asyncpg's raw codec rejects those integers. Dynamic keys/cursors and
    # constants needing that conversion must retain the ORM bind processor.
    plan = adapter.compile(op, optimized=True)
    assert plan.executor == "general"
    assert "Boolean" in plan.reason


@pytest.mark.parametrize(
    "op",
    [
        ListOperation(_Parent, _Id, where={"enabled": True}, order_by=("id",)),
        JoinRead(
            _Child, _Id, join=_Parent, via="parent_id", join_where={"enabled": True}
        ),
    ],
)
def test_native_boolean_constants_do_not_need_a_bind_conversion(adapter, op):
    assert adapter.compile(op, optimized=True).executor == "optimized"


def test_citext_result_column_is_native(adapter):
    """``citext`` has no SQLAlchemy result processing and asyncpg decodes it
    to the same ``str`` on both executors: as a RESULT column it is native."""
    plan = adapter.compile(ReadOperation(_CaseInsensitive, _Email), optimized=True)
    assert plan.executor == "optimized", plan.reason
    plan = adapter.compile(
        ListOperation(_CaseInsensitive, _Email, order_by=("id",)), optimized=True
    )
    assert plan.executor == "optimized", plan.reason


@pytest.mark.parametrize(
    "op",
    [
        ReadOperation(_CaseInsensitive, _Id, by=("email",)),
        ListOperation(_CaseInsensitive, _Id, by=("email",), order_by=("id",)),
        ListOperation(
            _CaseInsensitive, _Id, where={"email": "a@b.c"}, order_by=("id",)
        ),
        ListOperation(_CaseInsensitive, _Id, order_by=("email",)),
    ],
)
def test_citext_keys_filters_and_orders_keep_sqlalchemy_binds(adapter, op):
    # citext comparison semantics depend on the parameter's type (citext vs a
    # cast text/varchar is case-sensitive): never compared on the raw path
    plan = adapter.compile(op, optimized=True)
    assert plan.executor == "general"
    assert "CITEXT" in plan.reason


def test_citext_with_a_variant_is_not_native(adapter):
    plan = adapter.compile(ReadOperation(_CaseInsensitive, _Alias), optimized=True)
    assert plan.executor == "general"
    assert "type" in plan.reason


def test_polymorphic_root_without_subclasses_still_needs_orm_loading(adapter):
    plan = adapter.compile(ReadOperation(_Polymorphic, _Id), optimized=True)
    assert plan.executor == "general"
    assert "polymorphic" in plan.reason


def test_deferred_raiseload_column_policy_is_not_bypassed(adapter):
    plan = adapter.compile(ReadOperation(_Deferred, _Secret), optimized=True)
    assert plan.executor == "general"
    assert "deferred" in plan.reason


def test_nullable_keyset_order_is_refused(adapter):
    op = ListOperation(_Parent, _Id, order_by=("optional",), require_index=False)
    with pytest.raises(PlanningError, match="nullable"):
        adapter.compile(op, optimized=True)


def test_partial_unique_index_does_not_prove_a_global_unique_key(adapter):
    op = ReadOperation(_Parent, _Id, by=("partial",))
    assert adapter.compile(op, optimized=True).executor == "general"


def _index_keys(mapper: Any) -> tuple[list[set[str]], list[list[str]]]:
    return (
        [{c.name for c in key} for key in unique_keys(mapper)],
        [[c.name for c in key] for key in covering_indexes(mapper)],
    )


def test_index_decisions_read_the_form_not_a_database_option() -> None:
    """The planner knows a partial or search index from the form that made
    it (``Index.info``), so its unique-key and covering-index decisions are
    the same whichever renderers gave the index their options."""
    mapper = _Parent.__mapper__
    expected = ([{"id"}], [["id"], ["name"], ["optional"], ["enabled", "id"], ["code"]])
    keys, covering = _index_keys(mapper)
    assert keys == expected[0]
    assert sorted(covering) == sorted(expected[1])
    table = _Parent.__table__
    saved = {ix.name: dict(ix.dialect_kwargs) for ix in table.indexes}
    try:
        # as on a database whose renderer is not PostgreSQL's
        for ix in table.indexes:
            for key in [k for k in ix.dialect_kwargs if k.startswith("postgresql_")]:
                del ix.dialect_kwargs[key]
        keys, covering = _index_keys(mapper)
        assert keys == expected[0]
        assert sorted(covering) == sorted(expected[1])
    finally:
        for ix in table.indexes:
            ix.dialect_kwargs.update(saved[ix.name])


@pytest.mark.parametrize(
    "target,event_name", [(Session, "do_orm_execute"), (_Parent, "load")]
)
def test_custom_orm_hooks_prevent_raw_reads(adapter, target, event_name):
    def policy(*args):
        pass

    event.listen(target, event_name, policy)
    try:
        plan = adapter.compile(ReadOperation(_Parent, _Id), optimized=True)
        assert plan.executor == "general"
        assert "listener" in plan.reason
    finally:
        event.remove(target, event_name, policy)


def test_engine_statement_hooks_are_honored(adapter):
    def policy(*args):
        pass

    event.listen(adapter.engine.sync_engine, "before_cursor_execute", policy)
    try:
        plan = adapter.compile(ReadOperation(_Parent, _Id), optimized=True)
        assert plan.executor == "general"
        assert "listener" in plan.reason
    finally:
        event.remove(adapter.engine.sync_engine, "before_cursor_execute", policy)


def test_engine_schema_translation_never_uses_literal_raw_table_names(adapter):
    translated = SQLAlchemyExecutionAdapter(
        adapter.engine.execution_options(schema_translate_map={None: "another_schema"})
    )
    plan = translated.compile(ReadOperation(_Parent, _Id), optimized=True)
    assert plan.executor == "general"
    assert "schema translation" in plan.reason


def test_default_executor_has_an_explicit_rollback_switch():
    assert Settings.model_fields["planner_optimized_executor"].default is True
    config = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/test",
        CACHE_URL="redis://localhost/0",
        BROKER_URL="redis://localhost/1",
        JWT_SECRET="planner-test-config-not-a-production-secret",
        PLANNER_OPTIMIZED_EXECUTOR=False,
    )
    assert config.planner_optimized_executor is False


def test_join_read_keyed_on_a_unique_join_row_and_a_unique_via(adapter):
    """``by=()``: the entity row is pinned through the join — the joined row
    by a UNIQUE key, the entity by its UNIQUE ``via`` (a one-to-one detail)."""
    op = JoinRead(
        _Detail,
        _Id,
        by=(),
        join=_Owner,
        via="owner_id",
        join_by={"handle": "handle"},
    )
    plan = adapter.compile(op, optimized=True)
    assert plan.executor == "optimized", plan.reason
    assert op.keys == ("handle",)
    assert plan.compiled.sql.endswith(
        'WHERE j."handle" = $1'
    ) or plan.compiled.sql.endswith("WHERE j.handle = $1"), plan.compiled.sql


@pytest.mark.parametrize(
    "entity,join_by",
    [
        (_Many, {"handle": "handle"}),  # via not unique: several rows per owner
        (_Detail, {"label": "label"}),  # the owner is not pinned to one row
    ],
)
def test_join_read_keyed_on_the_join_must_prove_one_row(adapter, entity, join_by):
    op = JoinRead(entity, _Id, by=(), join=_Owner, via="owner_id", join_by=join_by)
    with pytest.raises(PlanningError, match="at most one row"):
        adapter.compile(op, optimized=True)


def test_join_read_needs_a_key_argument():
    with pytest.raises(PlanningError, match="at least one key argument"):
        JoinRead(_Detail, _Id, by=(), join=_Owner, via="owner_id")


class _Board(_Base):
    __tablename__ = "planner_production_board"
    id: Mapped[int] = mapped_column(primary_key=True)
    grp: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(CITEXT)
    flag: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (Index("ix_planner_board_grp_id", "grp", "id"),)


def _board(**over: Any) -> ListOperation[Any]:
    spec: dict[str, Any] = {
        "by": ("grp",),
        "optional": {"status": "status"},
        "prefix": {"starts": "title"},
        "order_by": ("id",),
    }
    spec.update(over)
    return ListOperation(_Board, _Id, **spec)


def test_optional_and_prefix_filters_are_optimized(adapter):
    op = _board(prefix_ignore_case=True)
    assert op.keys == ("grp", "status", "starts") and op.n_required == 1
    plan = adapter.compile(op, optimized=True)
    assert plan.executor == "optimized", plan.reason
    assert "optional / prefix" in plan.reason
    base, values, names = plan.compiled.bind([7, None, None])
    assert base is plan.compiled and list(values) == [7] and list(names) == ["grp"]
    form, values, names = plan.compiled.bind([7, "open", "a_%"])
    assert list(values) == [7, "open", "a\\_\\%%"]
    assert list(names) == ["grp", "status", "starts"]
    assert "status = $2" in form.sql and "ILIKE $3" in form.sql, form.sql
    # the forms of one combination are built once
    assert plan.compiled.bind([1, "x", "y"])[0] is form


#: Mixed case and every character a LIKE or GLOB pattern treats specially.
_PREFIX_TITLES = [
    "Ab%x", "ab%x", "abx", "a_b", "aXb", "a*b", "aZZb", "a?b", "a!b",
    "a[b]", "ab]", "a\\b", "a\\\\b", "AB*", "ab*", "",
]  # fmt: skip
_PREFIXES = ["a", "A", "ab%", "a_", "a*", "a?", "a[", "a\\", "a\\\\", "AB", "Ab%x", "x"]


async def _prefix_engine(name: str, tmp_path: Any) -> Any:
    from sandloom.core.drivers import sqlalchemy_database as driver
    from sandloom.core.testing.workers import worker_env_values

    if name == "sqlite":
        return driver._build_engine(f"sqlite+aiosqlite:///{tmp_path / 'prefix.db'}")
    repo = Path(__file__).resolve().parents[4]
    url = worker_env_values(repo / ".env.test")["DATABASE_URL"]
    if make_url(url).get_backend_name() != "postgresql":
        pytest.skip("the test environment's DATABASE_URL is not PostgreSQL")
    return driver.build_disposable_engine(url)


@pytest.mark.parametrize("database", ["postgresql", "sqlite"])
@pytest.mark.parametrize("ignore_case", [False, True])
async def test_the_general_prefix_filter_is_literal_and_keeps_its_case(
    database: str, ignore_case: bool, tmp_path: Any
) -> None:
    """The renderer's prefix_pattern / prefix_match: literal on both databases
    (``%``, ``_``, ``*``, ``?``, ``[``, ``\\`` match themselves), case-sensitive
    unless ``prefix_ignore_case`` (SQLite's LIKE folds case: a GLOB there)."""
    engine = await _prefix_engine(database, tmp_path)
    adapter = SQLAlchemyExecutionAdapter(engine)
    plan = adapter.compile(_board(prefix_ignore_case=ignore_case), optimized=False)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.exec_driver_sql(  # CITEXT has no SQLite type
                    "CREATE TABLE planner_production_board (id INTEGER PRIMARY "
                    "KEY, grp INTEGER, status TEXT, title TEXT, email TEXT, "
                    "flag BOOLEAN)"
                )
                for i, title in enumerate(_PREFIX_TITLES, 1):
                    await conn.execute(
                        text(
                            "INSERT INTO planner_production_board VALUES "
                            "(:i, 7, 's', :t, 'e', false)"
                        ),
                        {"i": i, "t": title},
                    )
                for prefix in _PREFIXES:
                    form, values, names = plan.compiled.bind([7, None, prefix])
                    params = {**dict(zip(names, values, strict=True)), "_limit": 50}
                    rows = (await conn.execute(form.stmt, params)).all()
                    found = sorted(_PREFIX_TITLES[row.id - 1] for row in rows)
                    expected = sorted(
                        t
                        for t in _PREFIX_TITLES
                        if (
                            t.lower().startswith(prefix.lower())
                            if ignore_case
                            else t.startswith(prefix)
                        )
                    )
                    assert found == expected, (database, prefix, ignore_case)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def test_a_renderer_without_the_prefix_members_falls_back_to_like() -> None:
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    from sandloom.core.sql_alchemy import portable

    assert portable.prefix_pattern("no_such", "a_%", ignore_case=False) == "a\\_\\%%"
    expr = portable.prefix_match(
        "no_such", _Board.title, sa_bindparam("p"), ignore_case=False
    )
    sql = str(expr.compile(dialect=sqlite_dialect.dialect()))
    assert "LIKE" in sql and "ESCAPE" in sql, sql


def test_prefix_needs_a_string_column(adapter):
    with pytest.raises(PlanningError, match="not a string column"):
        adapter.compile(_board(prefix={"starts": "grp"}), optimized=True)


def test_citext_prefix_and_dynamic_boolean_filters_stay_general(adapter):
    plan = adapter.compile(_board(prefix={"starts": "email"}), optimized=True)
    assert plan.executor == "general" and "citext" in plan.reason.lower()
    plan = adapter.compile(_board(optional={"flag": "flag"}), optimized=True)
    assert plan.executor == "general" and "Boolean" in plan.reason


def test_optional_filter_declarations_are_checked():
    with pytest.raises(PlanningError, match="used twice"):
        _board(optional={"grp": "status"})
    with pytest.raises(PlanningError, match="at most"):
        _board(optional={f"s{i}": "status" for i in range(4)})
