"""``tenant_upsert`` and the one conflict insert the cursor guard admits
(ADR-0018, decision 6), on SQLite and on PostgreSQL without RLS.

A conflict insert on a ``TenantScoped`` table is refused at the cursor whatever
its caller built. ``tenant_upsert`` is the one way in: it takes plain rows,
builds ``INSERT ... ON CONFLICT DO UPDATE SET col = excluded.col WHERE tenant_id
= <bound>`` itself and marks the statement; the guard admits a marked statement
only when it has exactly that shape and the bound tenant in every row. So:

* the helper's own behaviour (the happy path, the models it refuses, the plain
  values only rule, the column rules);
* the guard, driven with statements forged by hand: the canonical shape is
  admitted, and every deviation (the red-team findings of PR #337, insert side
  F1-F7 and update side F1-F11, one slot or one parameter at a time) is refused
  with ``UnsafeTenantOperationError`` and leaves every row as it was;
* the marker alone is not trusted, and only counts at the cursor.

Tenant A owns ``tups_items`` row 1 and tenant B row 2, both under the key
``shared``.
"""

from __future__ import annotations

import inspect as py_inspect
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    and_,
    bindparam,
    cast,
    column,
    delete,
    func,
    insert,
    inspect,
    literal,
    literal_column,
    select,
    text,
    true,
    update,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    column_property,
    mapped_column,
    synonym,
)

from sandloom.core import tenant as tenant_mod
from sandloom.core.di import (
    MissingTenantError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy import tenant_upsert as helper_mod
from sandloom.core.sql_alchemy.portable import UnsupportedSqlForm, conflict_insert_for
from sandloom.core.sql_alchemy.tenant_upsert import TenantUpsertError, tenant_upsert
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.testing import reload_test_env

A_ITEM_ID = 1
B_ITEM_ID = 2


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    """The model most tests upsert into: unique key (tenant_id, key); the
    attribute ``label`` is the column ``title``."""

    __tablename__ = "tups_items"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column("title", Text, nullable=True)
    owner = synonym("tenant_id")


def _table(model: type[_Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


ITEMS = _table(_Item)


class _Pair(TenantScoped, _Base):
    """The tenant is part of the primary key: the key the upsert conflicts on."""

    __tablename__ = "tups_pairs"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "code"),)

    code: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)


class _Two(TenantScoped, _Base):
    """Two unique keys that include the tenant: the caller must pick one."""

    __tablename__ = "tups_twos"
    __table_args__ = (
        UniqueConstraint("tenant_id", "one"),
        UniqueConstraint("tenant_id", "other"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    one: Mapped[str] = mapped_column(Text)
    other: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class _Global(TenantScoped, _Base):
    """A unique key that does not include the tenant: no tenant-scoped upsert."""

    __tablename__ = "tups_globals"
    __table_args__ = (UniqueConstraint("key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class _Renamed(TenantScoped, _Base):
    """F7: the tenant column is not called ``tenant_id`` in the database."""

    __tablename__ = "tups_renamed"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column("org_id", Uuid(), nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class _Aliased(TenantScoped, _Base):
    """A second attribute mapped to the tenant column."""

    __tablename__ = "tups_aliased"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


_Aliased.__mapper__.add_property(
    "owner_column", column_property(_Aliased.__table__.c.tenant_id)
)


class _Vehicle(_Base):
    __tablename__ = "tups_vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}


class _Car(TenantScoped, _Vehicle):
    """Joined inheritance: two tables, no plain upsert."""

    __tablename__ = "tups_cars"
    __table_args__ = (UniqueConstraint("tenant_id", "plate"),)

    id: Mapped[int] = mapped_column(ForeignKey("tups_vehicles.id"), primary_key=True)
    plate: Mapped[str] = mapped_column(Text)
    __mapper_args__ = {"polymorphic_identity": "car"}


class _HexUuid(TypeDecorator[uuid.UUID]):
    """Stores a UUID as ``t-<hex>``: the value in the database is not the value a
    parameter holds, so a bind that skipped the column's type would not match."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else "t-" + value.hex

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        return None if value is None else uuid.UUID(value[2:])


class _Hexed(TenantScoped, _Base):
    __tablename__ = "tups_hexed"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(_HexUuid(), nullable=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


@dataclass
class _Env:
    engine: AsyncEngine
    maker: async_sessionmaker[AsyncSession]
    a: uuid.UUID
    b: uuid.UUID

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def env(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncGenerator[_Env, None]:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'upsert.db'}"
    else:
        import os

        url = os.getenv("DATABASE_URL", "")
        if not url:
            pytest.fail("DATABASE_URL not set: the guard test needs a PostgreSQL URL")
    engine = create_async_engine(url)
    try:  # the engine is released whether setup, the test or the drop fails
        state = await _seed(engine)
        yield state
    finally:
        try:
            with unscoped():
                async with engine.begin() as conn:
                    await conn.run_sync(_Base.metadata.drop_all)
        finally:
            await engine.dispose()


async def _seed(engine: AsyncEngine) -> _Env:
    install_write_admission(engine)  # the cursor rule every kernel engine carries
    with unscoped():  # schema DDL names every table: system work (ADR-0018)
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
            await conn.run_sync(_Base.metadata.create_all)
    state = _Env(
        engine=engine,
        maker=async_sessionmaker(engine, expire_on_commit=False),
        a=uuid.uuid4(),
        b=uuid.uuid4(),
    )
    async with state.maker() as session:
        with unscoped():
            session.add_all(
                [
                    _Item(
                        id=A_ITEM_ID,
                        tenant_id=state.a,
                        key="shared",
                        name="a-name",
                        note="a-note",
                        label="a-label",
                    ),
                    _Item(
                        id=B_ITEM_ID,
                        tenant_id=state.b,
                        key="shared",
                        name="b-name",
                        note="b-note",
                        label="b-label",
                    ),
                    _Pair(tenant_id=state.a, code="p", label="a-pair"),
                    _Pair(tenant_id=state.b, code="p", label="b-pair"),
                    _Global(id=1, tenant_id=state.b, key="gk", name="b-global"),
                ]
            )
            await session.commit()
    return state


# ---------------------------------------------------------------- helpers


async def _upsert(
    env: _Env, tenant: uuid.UUID, model: Any, rows: Any, **options: Any
) -> None:
    """Run the helper as ``tenant``, in its own unit, committed."""
    async with env.maker() as session:
        with bind_tenant(tenant):
            await tenant_upsert(model, rows, session=session, **options)
            await session.commit()


async def _read(env: _Env, *entities: Any) -> list[Any]:
    """Every row of a table, whatever its tenant (system read), ordered."""
    async with env.maker() as session:
        with unscoped():
            result = await session.execute(select(*entities))
            return sorted(tuple(row) for row in result.all())


async def _items(env: _Env) -> dict[int, tuple[Any, ...]]:
    rows = await _read(
        env,
        _Item.id,
        _Item.tenant_id,
        _Item.key,
        _Item.name,
        _Item.note,
        _Item.label,
    )
    return {row[0]: row[1:] for row in rows}


async def _snapshot(env: _Env) -> list[Any]:
    """Every row of every table a forged statement could reach."""
    return [
        await _items(env),
        await _read(env, _Global.id, _Global.tenant_id, _Global.key, _Global.name),
        await _read(env, _Pair.tenant_id, _Pair.code, _Pair.label),
        await _read(env, _Renamed.id, _Renamed.key, _Renamed.name),
    ]


def _is_refusal(error: BaseException) -> bool:
    """The guard's refusal, bare or wrapped by SQLAlchemy."""
    cause = error.orig if isinstance(error, StatementError) else error
    return isinstance(cause, UnsafeTenantOperationError)


class _Fake:
    """A session that only names its dialect: it fails the test on any other use,
    so a refusal that is the helper's own provably came before any statement."""

    def __init__(self, dialect: Any) -> None:
        self._bind = type("Bind", (), {"dialect": dialect})()

    def get_bind(self) -> Any:
        return self._bind

    async def flush(self) -> None:
        raise AssertionError("the upsert reached the session")

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the upsert reached the connection")


async def _refused(model: Any, rows: Any, **options: Any) -> TenantUpsertError:
    """``tenant_upsert`` refuses ``rows`` for a bound tenant, before any statement."""
    with bind_tenant(uuid.uuid4()):
        with pytest.raises(TenantUpsertError) as caught:
            await tenant_upsert(
                model,
                rows,
                session=_Fake(sqlite.dialect()),  # type: ignore[arg-type]
                **options,
            )
    assert caught.value.sandloom_code == "SANDLOOM-R018"
    return caught.value


# ---------------------------------------------------------- the helper itself


@pytest.mark.asyncio
async def test_it_inserts_new_rows_and_refreshes_only_the_listed_columns(
    env: _Env,
) -> None:
    await _upsert(
        env,
        env.a,
        _Item,
        [
            {"id": 100, "key": "shared", "name": "a-new", "note": "n1", "label": "L"},
            {
                "id": 101,
                "key": "fresh",
                "name": "f-name",
                "note": "f-note",
                "label": "F",
            },
        ],
        update=["name", "label"],
    )
    items = await _items(env)
    # the existing row: id (primary key) kept, name and label (title) refreshed,
    # note not listed so kept; no row 100 was created
    assert items[A_ITEM_ID] == (env.a, "shared", "a-new", "a-note", "L")
    assert 100 not in items
    # the new key inserted whole, under the bound tenant
    assert items[101] == (env.a, "fresh", "f-name", "f-note", "F")
    # tenant B is untouched
    assert items[B_ITEM_ID] == (env.b, "shared", "b-name", "b-note", "b-label")
    assert len(items) == 3


@pytest.mark.asyncio
async def test_the_same_key_under_another_tenant_is_its_own_row(env: _Env) -> None:
    other = uuid.uuid4()
    await _upsert(
        env,
        other,
        _Item,
        [{"id": 50, "key": "shared", "name": "c-name"}],
        update=["name"],
    )
    items = await _items(env)
    # the conflict target includes the tenant: tenant ``other`` neither updates
    # nor even probes the rows of A and B (no existence oracle, no row lock)
    assert items[50] == (other, "shared", "c-name", None, None)
    assert items[A_ITEM_ID][2] == "a-name"
    assert items[B_ITEM_ID][2] == "b-name"


@pytest.mark.asyncio
async def test_a_second_call_updates_the_row_the_first_inserted(env: _Env) -> None:
    for name in ("first", "second"):
        await _upsert(
            env,
            env.a,
            _Item,
            [{"id": 60, "key": "again", "name": name}],
            update=["name"],
        )
    assert (await _items(env))[60][1:3] == ("again", "second")


@pytest.mark.asyncio
async def test_the_model_may_conflict_on_its_composite_primary_key(env: _Env) -> None:
    await _upsert(
        env, env.a, _Pair, [{"code": "p", "label": "a-new"}], update=["label"]
    )
    assert await _read(env, _Pair.tenant_id, _Pair.code, _Pair.label) == sorted(
        [(env.a, "p", "a-new"), (env.b, "p", "b-pair")]
    )


@pytest.mark.asyncio
async def test_two_unique_keys_are_picked_with_conflict_on(env: _Env) -> None:
    error = await _refused(
        _Two, [{"id": 1, "one": "x", "other": "y", "name": "n"}], update=["name"]
    )
    assert "conflict_on" in str(error)
    await _upsert(
        env,
        env.a,
        _Two,
        [{"id": 1, "one": "x", "other": "y", "name": "n"}],
        update=["name"],
        conflict_on=["one"],
    )
    await _upsert(
        env,
        env.a,
        _Two,
        [{"id": 2, "one": "x", "other": "z", "name": "n2"}],
        update=["name", "other"],
        conflict_on=["one"],
    )
    assert await _read(env, _Two.id, _Two.one, _Two.other, _Two.name) == [
        (1, "x", "z", "n2")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [["key"], ["tenant_id", "key"], ["key", "tenant_id"]])
async def test_conflict_on_may_name_the_tenant_column_or_leave_it_out(
    env: _Env, on: list[str]
) -> None:
    await _upsert(
        env,
        env.a,
        _Item,
        [{"id": 70, "key": "shared", "name": "via-conflict-on"}],
        update=["name"],
        conflict_on=on,
    )
    assert (await _items(env))[A_ITEM_ID][2] == "via-conflict-on"


@pytest.mark.asyncio
async def test_the_pending_changes_of_the_session_are_flushed_first(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_Item(id=200, key="pending", name="p1"))  # stamped A
            await tenant_upsert(
                _Item,
                [{"id": 201, "key": "pending", "name": "p2"}],
                update=["name"],
                session=session,
            )
            await session.commit()
    items = await _items(env)
    assert items[200][1:3] == ("pending", "p2")
    assert 201 not in items


@pytest.mark.asyncio
async def test_the_tenant_value_is_bound_with_the_tenant_columns_own_type(
    env: _Env,
) -> None:
    for name in ("one", "two"):
        await _upsert(
            env,
            env.a,
            _Hexed,
            [{"id": 1, "key": "k", "name": name}],
            update=["name"],
        )
    await _upsert(
        env, env.b, _Hexed, [{"id": 2, "key": "k", "name": "b"}], update=["name"]
    )
    async with env.maker() as session:
        with unscoped():
            raw = (
                await session.execute(
                    text("SELECT tenant_id, name FROM tups_hexed ORDER BY id")
                )
            ).all()
    # stored through the column's type (the update matched the row the insert
    # wrote: one row for A, whose name the second call refreshed)
    assert [tuple(row) for row in raw] == [
        ("t-" + env.a.hex, "two"),
        ("t-" + env.b.hex, "b"),
    ]


@pytest.mark.asyncio
async def test_a_call_leaves_nothing_marked_behind(env: _Env) -> None:
    await _upsert(
        env, env.a, _Item, [{"id": 5, "key": "k", "name": "n"}], update=["name"]
    )
    assert len(tenant_mod._UPSERTS) == 0


@pytest.mark.asyncio
async def test_a_primary_key_another_tenant_holds_is_the_databases_error(
    env: _Env,
) -> None:
    """The conflict key is (tenant, key); a primary key that is global and held by
    tenant B collides outside it. The database refuses the row and B's row stays
    as it was: the update is never reached for it."""
    before = await _snapshot(env)
    with pytest.raises(IntegrityError):
        await _upsert(
            env,
            env.a,
            _Item,
            [{"id": B_ITEM_ID, "key": "new", "name": "taken"}],
            update=["name"],
        )
    assert await _snapshot(env) == before


class _Recorder(_Fake):
    """Takes the statement the helper executes instead of running it."""

    def __init__(self, dialect: Any) -> None:
        super().__init__(dialect)
        self.sent: list[tuple[Any, Any]] = []

    async def flush(self) -> None:
        return None

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        return self

    async def execute(self, statement: Any, parameters: Any) -> None:
        self.sent.append((statement, parameters))


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])  # type: ignore[no-untyped-call]
async def test_the_statement_it_builds_conflicts_on_the_tenant_and_nothing_else(
    dialect: Any,
) -> None:
    """The statement: the conflict target is the tenant and the key, SET takes
    ``excluded`` values only, WHERE pins the tenant, no RETURNING (so no
    existence oracle, and no row lock on another tenant's row: PR #337, update
    side F10 and F11); the tenant is a parameter of every row."""
    tenant = uuid.uuid4()
    session = _Recorder(dialect)
    with bind_tenant(tenant):
        await tenant_upsert(
            _Item,
            [{"id": 1, "key": "k", "name": "n", "label": "l"}],
            update=["name", "label"],
            session=session,  # type: ignore[arg-type]
        )
    ((statement, parameters),) = session.sent
    sql = " ".join(str(statement.compile(dialect=dialect)).replace('"', "").split())
    assert sql.startswith("INSERT INTO tups_items (")
    assert (
        " ON CONFLICT (tenant_id, key) DO UPDATE SET name = excluded.name, "
        "title = excluded.title WHERE tups_items.tenant_id = " in sql
    )
    assert "RETURNING" not in sql
    assert parameters == [
        {"id": 1, "key": "k", "name": "n", "title": "l", "tenant_id": tenant}
    ]


def test_the_public_helper_takes_no_sql_from_the_caller() -> None:
    from sandloom.sdk.data import tenant_upsert as public

    assert list(py_inspect.signature(public).parameters) == [
        "model",
        "rows",
        "update",
        "conflict_on",
        "session",
    ]


@pytest.mark.asyncio
async def test_no_tenant_bound_refuses_even_for_no_rows() -> None:
    with pytest.raises(MissingTenantError):
        await tenant_upsert(_Item, [], update=["name"], session=_Fake(sqlite.dialect()))  # type: ignore[arg-type]
    with unscoped(), pytest.raises(MissingTenantError):  # unscoped binds no tenant
        await tenant_upsert(
            _Item,
            [{"id": 1, "key": "k", "name": "n"}],
            update=["name"],
            session=_Fake(sqlite.dialect()),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_no_rows_send_nothing() -> None:
    with bind_tenant(uuid.uuid4()):
        await tenant_upsert(_Item, [], update=["name"], session=_Fake(sqlite.dialect()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_database_without_on_conflict_refuses() -> None:
    with bind_tenant(uuid.uuid4()):
        with pytest.raises(UnsupportedSqlForm, match="conflict_insert.*no SQL dialect"):
            await tenant_upsert(
                _Item,
                [{"id": 1, "key": "k", "name": "n"}],
                update=["name"],
                session=_Fake(mysql.dialect()),  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_a_clause_the_guard_cannot_read_refuses_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard reads the ``ON CONFLICT`` clause of PostgreSQL and SQLite. A
    statement of another shape would be refused at the cursor, so the helper says
    so first (SANDLOOM-R015); the fake session fails the test on a flush."""
    monkeypatch.setattr(helper_mod, "_tenant_upsert_where", lambda tenant: true())
    with bind_tenant(uuid.uuid4()):
        with pytest.raises(
            UnsupportedSqlForm, match="tenant_upsert.*does not write it"
        ):
            await tenant_upsert(
                _Item,
                [{"id": 1, "key": "k", "name": "n"}],
                update=["name"],
                session=_Fake(sqlite.dialect()),  # type: ignore[arg-type]
            )


# ------------------------------------------------ what the helper refuses to build

_ROW = {"id": 1, "key": "k", "name": "n"}

_REFUSED_INPUT: dict[str, tuple[Any, Any, dict[str, Any], str]] = {
    # models
    "not a model": (object, [_ROW], {"update": ["name"]}, "not a TenantScoped model"),
    "an instance": (_Item(), [_ROW], {"update": ["name"]}, "not a TenantScoped model"),
    "no tenant key": (
        _Global,
        [{"id": 1, "key": "k", "name": "n"}],
        {"update": ["name"]},
        "no unique key that includes the tenant column",
    ),
    "joined inheritance": (
        _Car,
        [{"id": 1, "plate": "p"}],
        {"update": ["plate"]},
        "inheritance",
    ),
    # F7: the tenant column under another name
    "renamed tenant column": (
        _Renamed,
        [{"id": 1, "key": "k", "name": "n"}],
        {"update": ["name"]},
        "named 'tenant_id'",
    ),
    # the tenant in a row
    "tenant in a row": (
        _Item,
        [{**_ROW, "tenant_id": uuid.uuid4()}],
        {"update": ["name"]},
        "names the tenant column",
    ),
    "tenant through a synonym": (
        _Item,
        [{**_ROW, "owner": uuid.uuid4()}],
        {"update": ["name"]},
        "not a column attribute",
    ),
    "tenant through a second attribute": (
        _Aliased,
        [{"id": 1, "key": "k", "name": "n", "owner_column": uuid.uuid4()}],
        {"update": ["name"]},
        "names the tenant column",
    ),
    "tenant under its column name": (
        _Renamed,
        [{"id": 1, "key": "k", "name": "n", "org_id": uuid.uuid4()}],
        {"update": ["name"]},
        "named 'tenant_id'",
    ),
    # keys
    "unknown attribute": (
        _Item,
        [{**_ROW, "nope": 1}],
        {"update": ["name"]},
        "not a column attribute",
    ),
    "the column name of a renamed attribute": (
        _Item,
        [{**_ROW, "title": "x"}],
        {"update": ["name"]},
        "not a column attribute",
    ),
    "a non-string key": (
        _Item,
        [{_Item.name: "x", "id": 1, "key": "k"}],
        {"update": ["name"]},
        "not an attribute name",
    ),
    "rows of different columns": (
        _Item,
        [_ROW, {"id": 2, "key": "k2", "name": "n", "note": "x"}],
        {"update": ["name"]},
        "same columns",
    ),
    "a missing conflict key": (
        _Item,
        [{"id": 1, "name": "n"}],
        {"update": ["name"]},
        "missing ['key']",
    ),
    "a missing updated column": (
        _Item,
        [{"id": 1, "key": "k"}],
        {"update": ["name"]},
        "missing ['name']",
    ),
    "a row that is no mapping": (_Item, [("k", "n")], {"update": ["name"]}, "mapping"),
    "rows as a string": (_Item, "rows", {"update": ["name"]}, "is not a mapping"),
    # plain values only (insert side F4-F6)
    "an attribute as a value": (
        _Item,
        [{**_ROW, "name": _Item.note}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a function as a value": (
        _Item,
        [{**_ROW, "name": func.upper("x")}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a literal column as a value": (
        _Item,
        [
            {
                **_ROW,
                "name": literal_column("(SELECT name FROM tups_items WHERE id = 2)"),
            }
        ],
        {"update": ["name"]},
        "SQL expression",
    ),
    "text as a value": (
        _Item,
        [{**_ROW, "name": text("(SELECT name FROM tups_items WHERE id = 2)")}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a bind as a value": (
        _Item,
        [{**_ROW, "name": bindparam("p")}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a literal as a value": (
        _Item,
        [{**_ROW, "name": literal("x")}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a cast as a value": (
        _Item,
        [{**_ROW, "name": cast("x", Text)}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a select as a value": (
        _Item,
        [{**_ROW, "name": select(_Item.name).scalar_subquery()}],
        {"update": ["name"]},
        "SQL expression",
    ),
    "a column as a value": (
        _Item,
        [{**_ROW, "name": column("name")}],
        {"update": ["name"]},
        "SQL expression",
    ),
    # update
    "no update": (_Item, [_ROW], {"update": []}, "non-empty list"),
    "update as a string": (_Item, [_ROW], {"update": "name"}, "non-empty list"),
    "update of the tenant": (
        _Item,
        [_ROW],
        {"update": ["tenant_id"]},
        "tenant column or a primary key",
    ),
    "update of the primary key": (
        _Item,
        [_ROW],
        {"update": ["id"]},
        "tenant column or a primary key",
    ),
    "update of a primary key that is the tenant's": (
        _Pair,
        [{"code": "p", "label": "l"}],
        {"update": ["code"]},
        "tenant column or a primary key",
    ),
    "update twice": (_Item, [_ROW], {"update": ["name", "name"]}, "twice"),
    "update of an unknown attribute": (
        _Item,
        [_ROW],
        {"update": ["nope"]},
        "not a column attribute",
    ),
    "update of the column name of a renamed attribute": (
        _Item,
        [{**_ROW, "title": "x"}],
        {"update": ["title"]},
        "not a column attribute",
    ),
    "update of a synonym": (_Item, [_ROW], {"update": ["owner"]}, "not a column"),
    # the conflict target
    "conflict_on empty": (
        _Item,
        [_ROW],
        {"update": ["name"], "conflict_on": []},
        "non-empty list",
    ),
    "conflict_on as a string": (
        _Item,
        [_ROW],
        {"update": ["name"], "conflict_on": "key"},
        "non-empty list",
    ),
    "conflict_on of part of a key": (
        _Two,
        [{"id": 1, "one": "x", "other": "y", "name": "n"}],
        {"update": ["name"], "conflict_on": ["one", "other"]},
        "not a unique key",
    ),
    "conflict_on of a column that is no key": (
        _Item,
        [_ROW],
        {"update": ["name"], "conflict_on": ["name"]},
        "not a unique key",
    ),
    "conflict_on of the key without the tenant (global unique)": (
        _Global,
        [{"id": 1, "key": "k", "name": "n"}],
        {"update": ["name"], "conflict_on": ["key"]},
        "not a unique key",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_REFUSED_INPUT))
async def test_it_refuses_what_it_cannot_turn_into_a_tenant_scoped_statement(
    case: str,
) -> None:
    model, rows, options, message = _REFUSED_INPUT[case]
    error = await _refused(model, rows, **options)
    assert message in str(error)


@pytest.mark.asyncio
async def test_a_second_attribute_of_the_tenant_column_is_the_tenant_column() -> None:
    """The check is the column, not the attribute's spelling: ``owner_column`` and
    ``tenant_id`` are one Column of ``tups_aliased``."""
    mapper = inspect(_Aliased)
    assert (
        mapper.column_attrs["owner_column"].columns[0]
        is mapper.column_attrs["tenant_id"].columns[0]
    )


# ----------------------------------------------------- the guard, by forged statements


class _Unset:
    pass


_DEFAULT: Any = _Unset()


def _canonical(env: _Env, table: Table = ITEMS, **parts: Any) -> Any:
    """The statement ``tenant_upsert`` builds for ``table``, with the ``parts``
    replaced: ``target`` (the conflict key), ``set_`` (a dict, or a function of the
    ``excluded`` alias) and ``where``."""
    statement = conflict_insert_for(env.dialect, table)
    excluded = statement.excluded
    tenant = next(c for c in table.c if c.name in ("tenant_id", "org_id"))
    set_ = parts.get("set_", _DEFAULT)
    if set_ is _DEFAULT:
        set_ = {table.c.name: excluded.name}
    elif callable(set_):
        set_ = set_(excluded)
    where = parts.get("where", _DEFAULT)
    return statement.on_conflict_do_update(
        index_elements=parts.get("target", [tenant, table.c.key]),
        set_=set_,
        where=tenant_mod._tenant_upsert_where(tenant) if where is _DEFAULT else where,
        **({"index_where": parts["index_where"]} if "index_where" in parts else {}),
    )


def _row(env: _Env, **over: Any) -> dict[str, Any]:
    """One parameter set that conflicts with tenant A's own row."""
    return {"id": 90, "key": "shared", "name": "forged", "tenant_id": env.a} | over


Forgery = Callable[[_Env], tuple[Any, list[dict[str, Any]]]]


def _with_cte(env: _Env, dml: Any) -> tuple[Any, list[dict[str, Any]]]:
    return _canonical(env).add_cte(dml.cte("planted")), [_row(env)]


def _b_row() -> Any:
    return ITEMS.c.id == B_ITEM_ID


_FORGED: dict[str, Forgery] = {
    # --- the slots a statement has besides the ones the helper fills (insert side).
    # A CTE with DML is also refused at compile time (``check_compiled_statement``),
    # before the upsert admission: F1-F3 pin the end-to-end result, and the slot
    # check is pinned by the RETURNING, VALUES, prefix, hint and inline cases.
    "F1 a CTE that rewrites B's row": lambda e: _with_cte(
        e,
        # every column named: a statement run with parameters sets the ones a
        # nested UPDATE leaves out from them
        update(ITEMS)
        .where(_b_row())
        .values(
            {
                ITEMS.c.id: ITEMS.c.id,
                ITEMS.c.key: ITEMS.c.key,
                ITEMS.c.name: literal_column("'pwned'"),
                ITEMS.c.tenant_id: ITEMS.c.tenant_id,
            }
        )
        .returning(ITEMS.c.id),
    ),
    "F2 a CTE that plants a row for B": lambda e: _with_cte(
        e,
        insert(ITEMS)
        .values(id=77, tenant_id=e.b, key="planted", name="x")
        .returning(ITEMS.c.id),
    ),
    "F3 a CTE that deletes B's row": lambda e: _with_cte(
        e, delete(ITEMS).where(_b_row()).returning(ITEMS.c.id)
    ),
    "F4 RETURNING of raw SQL": lambda e: (
        _canonical(e).returning(
            literal_column("(SELECT name FROM tups_items WHERE id = 2)")
        ),
        [_row(e)],
    ),
    "RETURNING of a column": lambda e: (
        _canonical(e).returning(ITEMS.c.name),
        [_row(e)],
    ),
    "return_defaults": lambda e: (_canonical(e).return_defaults(), [_row(e)]),
    "F5 VALUES of raw SQL": lambda e: (
        _canonical(e).values(
            name=literal_column("(SELECT name FROM tups_items WHERE id = 2)")
        ),
        [_row(e)],
    ),
    "F6 VALUES of text": lambda e: (
        _canonical(e).values(name=text("(SELECT name FROM tups_items WHERE id = 2)")),
        [_row(e)],
    ),
    "VALUES that name the tenant": lambda e: (
        _canonical(e).values(tenant_id=e.b),
        [_row(e)],
    ),
    "INSERT ... SELECT": lambda e: (
        _canonical(e).from_select(
            ["id", "tenant_id", "key", "name"],
            select(ITEMS.c.id + 100, ITEMS.c.tenant_id, ITEMS.c.key, ITEMS.c.name),
        ),
        [],
    ),
    "a prefix": lambda e: (_canonical(e).prefix_with("/* x */"), [_row(e)]),
    "a hint": lambda e: (
        _canonical(e).with_hint("x", dialect_name="mysql"),
        [_row(e)],
    ),
    "inline": lambda e: (_canonical(e).inline(), [_row(e)]),
    "DO NOTHING": lambda e: (
        conflict_insert_for(e.dialect, ITEMS).on_conflict_do_nothing(
            index_elements=[ITEMS.c.tenant_id, ITEMS.c.key]
        ),
        [_row(e)],
    ),
    # --- the conflict target (update side F4-F7, F9, F10)
    "U-F4 a raw literal in the target": lambda e: (
        _canonical(e, target=[literal_column("tenant_id"), ITEMS.c.key]),
        [_row(e)],
    ),
    "U-F9 a sub-select in the target": lambda e: (
        _canonical(
            e,
            target=[
                select(func.max(ITEMS.c.name)).scalar_subquery(),
                ITEMS.c.tenant_id,
            ],
        ),
        [_row(e)],
    ),
    "U-F7 a custom operator in the target": lambda e: (
        _canonical(e, target=[ITEMS.c.tenant_id, ITEMS.c.key.op("||")("")]),
        [_row(e)],
    ),
    "U-F5 a raw index predicate": lambda e: (
        _canonical(e, index_where=literal_column("true")),
        [_row(e)],
    ),
    "an index predicate": lambda e: (
        _canonical(e, index_where=ITEMS.c.name != "x"),
        [_row(e)],
    ),
    "U-F10 a target without the tenant": lambda e: (
        # the one key of the table leaves the tenant out: tenant B's row is the arbiter
        _canonical(e, table=_table(_Global), target=[_table(_Global).c.key]),
        [{"id": 90, "key": "gk", "name": "forged", "tenant_id": e.a}],
    ),
    "a target of a column of another table": lambda e: (
        _canonical(e, target=[ITEMS.c.tenant_id, _table(_Global).c.key]),
        [_row(e)],
    ),
    # --- DO UPDATE SET: only excluded.<column> of the table itself
    "SET from the existing row": lambda e: (
        _canonical(e, set_={ITEMS.c.name: ITEMS.c.note}),
        [_row(e)],
    ),
    "SET of a function of excluded": lambda e: (
        _canonical(e, set_=lambda x: {ITEMS.c.name: func.upper(x.name)}),
        [_row(e)],
    ),
    "SET of raw SQL": lambda e: (
        _canonical(
            e,
            set_={
                ITEMS.c.name: literal_column(
                    "(SELECT name FROM tups_items WHERE id = 2)"
                )
            },
        ),
        [_row(e)],
    ),
    "SET of text": lambda e: (
        _canonical(
            e,
            set_={ITEMS.c.name: text("(SELECT name FROM tups_items WHERE id = 2)")},
        ),
        [_row(e)],
    ),
    "SET of a bound value": lambda e: (
        _canonical(e, set_={ITEMS.c.name: "fixed"}),
        [_row(e)],
    ),
    "SET of the tenant": lambda e: (
        _canonical(e, set_=lambda x: {ITEMS.c.tenant_id: x.tenant_id}),
        [_row(e)],
    ),
    "SET of the primary key": lambda e: (
        _canonical(e, set_=lambda x: {ITEMS.c.id: x.id}),
        [_row(e)],
    ),
    "SET of excluded of another column": lambda e: (
        _canonical(e, set_=lambda x: {ITEMS.c.name: x.note}),
        [_row(e)],
    ),
    "SET of excluded of another table": lambda e: (
        _canonical(
            e,
            set_={
                ITEMS.c.name: conflict_insert_for(
                    e.dialect, _table(_Global)
                ).excluded.name
            },
        ),
        [_row(e)],
    ),
    "SET of another alias": lambda e: (
        _canonical(e, set_={ITEMS.c.name: ITEMS.alias("shadow").c.name}),
        [_row(e)],
    ),
    # --- DO UPDATE WHERE: the one condition tenant_id = <the bound tenant>
    "no WHERE": lambda e: (_canonical(e, where=None), [_row(e)]),
    "WHERE true": lambda e: (_canonical(e, where=true()), [_row(e)]),
    "WHERE raw true": lambda e: (
        _canonical(e, where=literal_column("true")),
        [_row(e)],
    ),
    "U-F3 WHERE with an OR of the caller": lambda e: (
        _canonical(e, where=true().op("OR", precedence=100)(ITEMS.c.name == "zz")),
        [_row(e)],
    ),
    "U-F1 WHERE with a second term": lambda e: (
        _canonical(
            e,
            where=and_(
                tenant_mod._tenant_upsert_where(ITEMS.c.tenant_id),
                ITEMS.c.name != "zz",
            ),
        ),
        [_row(e)],
    ),
    "U-F2 WHERE with a cast of the row": lambda e: (
        _canonical(
            e,
            where=and_(
                tenant_mod._tenant_upsert_where(ITEMS.c.tenant_id),
                cast(ITEMS.c.name, Integer) > 0,
            ),
        ),
        [_row(e)],
    ),
    "WHERE with a literal tenant": lambda e: (
        _canonical(e, where=ITEMS.c.tenant_id == literal(e.a, ITEMS.c.tenant_id.type)),
        [_row(e)],
    ),
    "WHERE with a bind of another name": lambda e: (
        _canonical(
            e,
            where=ITEMS.c.tenant_id
            == bindparam(
                "sandloom_tenant_criterion",
                callable_=tenant_mod._tenant_criteria_value,
                type_=ITEMS.c.tenant_id.type,
            ),
        ),
        [_row(e)],
    ),
    "WHERE on another column": lambda e: (
        _canonical(
            e,
            where=ITEMS.c.name
            == bindparam(
                tenant_mod.TENANT_UPSERT_PARAM,
                callable_=tenant_mod._tenant_criteria_value,
            ),
        ),
        [_row(e)],
    ),
    "WHERE on another column, the bind typed as the tenant": lambda e: (
        # only the column differs from the canonical WHERE; both are text in the
        # database, so the statement is valid SQL and a guard that let it by would run it
        _canonical(
            e,
            table=_table(_Hexed),
            where=_table(_Hexed).c.name
            == bindparam(
                tenant_mod.TENANT_UPSERT_PARAM,
                callable_=tenant_mod._tenant_criteria_value,
                type_=_table(_Hexed).c.tenant_id.type,
            ),
        ),
        [{"id": 90, "key": "k", "name": "forged", "tenant_id": e.a}],
    ),
    "WHERE with the bind typed otherwise": lambda e: (
        _canonical(
            e,
            where=ITEMS.c.tenant_id
            == bindparam(
                tenant_mod.TENANT_UPSERT_PARAM,
                callable_=tenant_mod._tenant_criteria_value,
                type_=Text(),
            ),
        ),
        [_row(e)],
    ),
    "WHERE <>": lambda e: (
        _canonical(
            e,
            where=ITEMS.c.tenant_id
            != bindparam(
                tenant_mod.TENANT_UPSERT_PARAM,
                callable_=tenant_mod._tenant_criteria_value,
                type_=ITEMS.c.tenant_id.type,
            ),
        ),
        [_row(e)],
    ),
    # --- the parameters: the bound tenant in every row and in the WHERE
    "a row of tenant B": lambda e: (_canonical(e), [_row(e, tenant_id=e.b)]),
    "a row without a tenant": lambda e: (
        _canonical(e),
        [{"id": 90, "key": "shared", "name": "forged"}],
    ),
    "a row with no tenant value": lambda e: (
        _canonical(e),
        [_row(e, tenant_id=None)],
    ),
    "one row of tenant B among the bound tenant's": lambda e: (
        _canonical(e),
        [_row(e), _row(e, id=91, key="two", tenant_id=e.b)],
    ),
    "the WHERE parameter set to B": lambda e: (
        _canonical(e),
        [_row(e, **{tenant_mod.TENANT_UPSERT_PARAM: e.b})],
    ),
    "the WHERE parameter set to B and the row too": lambda e: (
        _canonical(e),
        [_row(e, tenant_id=e.b, **{tenant_mod.TENANT_UPSERT_PARAM: e.b})],
    ),
    "the WHERE parameter set on one row only": lambda e: (
        _canonical(e),
        [_row(e), _row(e, id=91, key="two", **{tenant_mod.TENANT_UPSERT_PARAM: e.b})],
    ),
    # --- F7: the tenant column under another name
    "F7 a table whose tenant column is org_id": lambda e: (
        _canonical(e, table=_table(_Renamed)),
        [{"id": 1, "key": "k", "name": "forged", "org_id": e.b}],
    ),
}


async def _forge(
    env: _Env,
    statement: Any,
    parameters: Any,
    *,
    seal: bool = True,
    bound: uuid.UUID | None = None,
    through_orm: bool = False,
) -> None:
    """Run ``statement`` as the bound tenant (A) on the unit's connection, the way
    ``tenant_upsert`` does: marked, unless ``seal=False``."""
    async with env.maker() as session:
        with bind_tenant(env.a if bound is None else bound):
            if seal:
                tenant_mod._seal_tenant_upsert(statement)
            try:
                if through_orm:
                    await session.execute(statement, parameters)
                else:
                    connection = await session.connection()
                    await connection.execute(statement, parameters)
            finally:
                tenant_mod._unseal_tenant_upsert(statement)
            await session.commit()


async def _assert_refused_and_untouched(env: _Env, run: Any) -> None:
    before = await _snapshot(env)
    with pytest.raises(Exception) as caught:
        await run()
    assert _is_refusal(caught.value), repr(caught.value)
    assert await _snapshot(env) == before


@pytest.mark.asyncio
async def test_the_canonical_statement_is_admitted(env: _Env) -> None:
    """The control: the statement the helper builds, forged by hand and marked,
    runs. Every refusal below differs from it in one slot or one parameter."""
    await _forge(env, _canonical(env), [_row(env)])
    items = await _items(env)
    assert items[A_ITEM_ID][2] == "forged"
    assert items[B_ITEM_ID] == (env.b, "shared", "b-name", "b-note", "b-label")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_FORGED))
async def test_a_forged_upsert_is_refused_and_touches_nothing(
    env: _Env, shape: str
) -> None:
    statement, parameters = _FORGED[shape](env)
    await _assert_refused_and_untouched(env, lambda: _forge(env, statement, parameters))


@pytest.mark.asyncio
async def test_a_statement_without_the_marker_is_refused(env: _Env) -> None:
    """The shape alone is not enough: the marker is the helper's."""
    await _assert_refused_and_untouched(
        env, lambda: _forge(env, _canonical(env), [_row(env)], seal=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["no parameter set", "the helper's own set"])
async def test_a_marked_upsert_needs_a_parameter_set_to_check(
    env: _Env, reported: str
) -> None:
    """Nothing proves the tenant of a row the cursor reports no parameter set for
    (``all()`` of nothing is true): refused. The control reports the one set the
    helper's own statement runs with, and passes."""
    statement = _canonical(env)
    compiled = statement.compile(dialect=env.engine.dialect)
    assert isinstance(compiled, tenant_mod.ReachRecorder)  # the setup is real
    sets = (
        []
        if reported == "no parameter set"
        else [_row(env) | {tenant_mod.TENANT_UPSERT_PARAM: env.a}]
    )
    context = SimpleNamespace(invoked_statement=statement, compiled_parameters=sets)
    with bind_tenant(env.a):
        tenant_mod._seal_tenant_upsert(statement)
        try:
            if sets:
                tenant_mod.check_tenant_reach(context, compiled)
            else:
                with pytest.raises(UnsafeTenantOperationError):
                    tenant_mod.check_tenant_reach(context, compiled)
        finally:
            tenant_mod._unseal_tenant_upsert(statement)


@pytest.mark.asyncio
async def test_the_marker_counts_only_at_the_cursor(env: _Env) -> None:
    """A marked statement of the exact shape handed to the session's ORM execute
    is the refused conflict insert it always was."""
    await _assert_refused_and_untouched(
        env, lambda: _forge(env, _canonical(env), [_row(env)], through_orm=True)
    )


@pytest.mark.asyncio
async def test_the_marker_is_gone_when_the_helper_returns(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A statement a caller captured from the helper cannot be replayed."""
    captured: list[Any] = []
    real = tenant_mod._seal_tenant_upsert

    def record(statement: Any) -> None:
        captured.append(statement)
        real(statement)

    monkeypatch.setattr(helper_mod, "_seal_tenant_upsert", record)
    await _upsert(
        env, env.a, _Item, [{"id": 5, "key": "k", "name": "n"}], update=["name"]
    )
    (statement,) = captured
    assert statement not in tenant_mod._UPSERTS
    await _assert_refused_and_untouched(
        env,
        lambda: _forge(
            env, statement, [_row(env, key="k", name="replayed")], seal=False
        ),
    )


@pytest.mark.asyncio
async def test_the_marker_is_gone_when_the_statement_fails(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []
    real = tenant_mod._seal_tenant_upsert

    def record(statement: Any) -> None:
        captured.append(statement)
        real(statement)

    monkeypatch.setattr(helper_mod, "_seal_tenant_upsert", record)
    with pytest.raises(Exception):  # noqa: B017 - the database refuses the row
        # no ``name``: NOT NULL, so the statement reaches the database and fails
        await _upsert(env, env.a, _Item, [{"id": 5, "key": "k"}], update=["key"])
    assert all(statement not in tenant_mod._UPSERTS for statement in captured)
    assert len(tenant_mod._UPSERTS) == 0


@pytest.mark.asyncio
async def test_a_conflict_insert_the_caller_builds_stays_refused(env: _Env) -> None:
    """Exactly as before the helper: the caller's own ``conflict_insert`` on a
    tenant table is refused bound, in both forms, and runs only in ``unscoped()``."""
    for statement in (
        conflict_insert_for(env.dialect, ITEMS)
        .values(id=90, tenant_id=env.a, key="n", name="x")
        .on_conflict_do_nothing(index_elements=[ITEMS.c.tenant_id, ITEMS.c.key]),
        conflict_insert_for(env.dialect, ITEMS)
        .values(id=90, tenant_id=env.a, key="shared", name="x")
        .on_conflict_do_update(
            index_elements=[ITEMS.c.tenant_id, ITEMS.c.key],
            set_={"name": "x"},
            where=tenant_mod._tenant_upsert_where(ITEMS.c.tenant_id),
        ),
    ):
        await _assert_refused_and_untouched(
            env, lambda statement=statement: _forge(env, statement, None, seal=False)
        )
