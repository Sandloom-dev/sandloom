"""``tenant_upsert`` and the one conflict insert the cursor guard admits
(ADR-0018, decision 6), on SQLite and on PostgreSQL without RLS.

A conflict insert on a ``TenantScoped`` table is refused at the cursor whatever
its caller built. ``tenant_upsert`` is the one way in: it takes plain rows,
builds ``INSERT ... ON CONFLICT DO UPDATE SET col = excluded.col WHERE tenant_id
= <bound>`` itself and marks the statement; the guard admits a marked statement
only when the statement that ran renders, text for text, as the one the kernel's
builder writes from the same parts. So:

* the helper's own behaviour (the happy path, the models it refuses before a
  flush, the plain values only rule, the column rules, the keys it picks, a
  deferrable key, a column default that would read a tenant table);
* the guard, driven with statements forged by hand: the canonical shape is
  admitted, and every deviation (the red-team findings of PR #337, insert side
  F1-F7 and update side F1-F11, then the exact-class and rendering spoofs of the
  security review, one slot or one parameter at a time) is refused with
  ``UnsafeTenantOperationError`` and leaves every row as it was;
* the marker alone is not trusted, only counts at the cursor, and a refused
  statement does not decide for the next one;
* the tenant is compared by its exact type on every path (the upsert, the flush,
  the cursor): a ``uuid.UUID`` subclass that only reads as the bound tenant is
  refused, a driver's honest subclass is replaced by the plain value;
* a ``sqlite_on_conflict*`` option and a column that is not exactly ``Column``
  are refused before a flush;
* what the helper does not claim: a unique key or a foreign key that leaves the
  tenant out acts as on a plain insert, and a composite key does what the docs
  advise.

Tenant A owns ``tups_items`` row 1 and tenant B row 2, both under the key
``shared``.
"""

from __future__ import annotations

import inspect as py_inspect
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
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
    event,
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
from sqlalchemy.sql.elements import ColumnClause

from sandloom.core import tenant as tenant_mod
from sandloom.core.config.settings import settings
from sandloom.core.di import (
    MissingTenantError,
    TenantMismatchError,
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


class _Keyed(TenantScoped, _Base):
    """Two unique keys over the tenant, one of them on the attribute ``label``
    whose column is ``title``: an ambiguity message names the ATTRIBUTE."""

    __tablename__ = "tups_keyed"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        UniqueConstraint("tenant_id", "title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column("title", Text)


class _Mixed(TenantScoped, _Base):
    """The key it conflicts on includes the tenant; ``email`` is a unique key that
    does not (RT-A3): it acts as on a plain insert."""

    __tablename__ = "tups_mixed"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, unique=True)


class _PgBase(DeclarativeBase):
    """Tables only PostgreSQL can create (SQLite has no ``DEFERRABLE`` unique key)
    or enforce (it checks foreign keys only when told to)."""


class _Deferred(TenantScoped, _PgBase):
    """PostgreSQL cannot infer a DEFERRABLE constraint as the arbiter of ``ON
    CONFLICT DO UPDATE``: it is no candidate, ``slug`` is the one key."""

    __tablename__ = "tups_deferred"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("tenant_id", "slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class _PgParent(TenantScoped, _PgBase):
    """What the two children below reference (RT-A2)."""

    __tablename__ = "tups_parents"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class _PgPlainChild(TenantScoped, _PgBase):
    """A foreign key that leaves the tenant out: acts as on a plain insert."""

    __tablename__ = "tups_plain_children"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[int] = mapped_column(ForeignKey("tups_parents.id"))


class _PgTenantChild(TenantScoped, _PgBase):
    """The composite foreign key ``(tenant_id, parent_id)`` the docs recommend."""

    __tablename__ = "tups_tenant_children"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["tups_parents.tenant_id", "tups_parents.id"],
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[int] = mapped_column(Integer)


class _ReplacingPk(TenantScoped, _Base):
    """A1: SQLite resolves a conflict on the GLOBAL primary key by deleting the row
    of any tenant that holds it, before ``ON CONFLICT DO UPDATE`` is asked."""

    __tablename__ = "tups_replacing_pk"
    __table_args__ = (
        PrimaryKeyConstraint("id", sqlite_on_conflict="REPLACE"),
        UniqueConstraint("tenant_id", "key"),
    )

    id: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class _ReplacingUnique(TenantScoped, _Base):
    __tablename__ = "tups_replacing_unique"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key"),
        UniqueConstraint("email", sqlite_on_conflict="REPLACE"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)


class _ReplacingColumn(TenantScoped, _Base):
    __tablename__ = "tups_replacing_column"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(
        Text, unique=True, sqlite_on_conflict_unique="REPLACE"
    )


class _Defaulted(TenantScoped, _Base):
    """A client-side SQL default that reads a tenant table: it renders into the
    ``VALUES`` of a statement that omits ``snap``, and only then."""

    __tablename__ = "tups_defaulted"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    snap: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=select(ITEMS.c.name).where(ITEMS.c.id == B_ITEM_ID).scalar_subquery(),
    )


class _Peek(TypeDecorator[str]):
    """A type whose ``bind_expression`` selects from a tenant table."""

    impl = Text
    cache_ok = True

    def bind_expression(self, bindvalue: Any) -> Any:
        peek = select(ITEMS.c.name).where(ITEMS.c.id == B_ITEM_ID).scalar_subquery()
        return func.coalesce(peek, bindvalue)


class _Peeking(TenantScoped, _Base):
    __tablename__ = "tups_peeking"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(_Peek())


class _OddColumn(Column[Any]):
    """A ``Column`` subclass: it may render its own SQL, which the statement and
    its rebuild share, so only the class itself can rule it out."""

    inherit_cache = True


class _Odd(TenantScoped, _Base):
    __tablename__ = "tups_odd"
    __table_args__ = (UniqueConstraint("tenant_id", "key"),)

    id = Column(Integer, primary_key=True)
    key = Column(Text)
    name = _OddColumn("name", Text)


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
        url = settings.database_url
        if not url.startswith("postgresql"):
            pytest.fail(f"the guard test needs a PostgreSQL URL, got {url[:12]!r}")
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

    def get_bind(self, *args: Any, **kwargs: Any) -> Any:
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


@pytest.mark.asyncio
async def test_a_unique_key_without_the_tenant_acts_as_on_a_plain_insert(
    env: _Env,
) -> None:
    """RT-A3: another tenant's row is never the conflicting row, and a unique key
    that leaves the tenant out is the database's own error, for a new row and for
    the column a conflict refreshes. The error shows that the key exists: the docs
    say so and advise keys that are not reused across tenants."""
    async with env.maker() as session:
        with unscoped():
            session.add(_Mixed(id=1, tenant_id=env.b, key="k", email="taken"))
            await session.commit()
    row = {"id": 2, "key": "k", "email": "mine"}
    with pytest.raises(IntegrityError):
        await _upsert(env, env.a, _Mixed, [{**row, "email": "taken"}], update=["email"])
    await _upsert(env, env.a, _Mixed, [row], update=["email"])  # key "k" is A's own
    with pytest.raises(IntegrityError):
        await _upsert(env, env.a, _Mixed, [{**row, "email": "taken"}], update=["email"])
    assert await _read(env, _Mixed.id, _Mixed.tenant_id, _Mixed.key, _Mixed.email) == [
        (1, env.b, "k", "taken"),
        (2, env.a, "k", "mine"),
    ]


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


class _Liar(uuid.UUID):
    """A UUID that says it equals everything. It is bound to the database as the
    UUID it was built from, whatever ``==`` told the guard (N1, P1, P2)."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = uuid.UUID.__hash__


def _liar(tenant: uuid.UUID) -> _Liar:
    return _Liar(int=tenant.int)


def _evil_insert(env: _Env) -> Any:
    """N2: an ``Insert`` subclass with its own ``_compiler_dispatch``: the clause it
    holds is the canonical one, the SQL it renders is not (the conflict target
    moved to the primary key, the WHERE always true)."""
    base = type(conflict_insert_for(env.dialect, ITEMS))

    class EvilInsert(base):  # type: ignore[misc, valid-type]
        inherit_cache = True

        def _compiler_dispatch(self, visitor: Any, **kw: Any) -> str:
            sql = visitor.visit_insert(self, **kw)
            sql = re.sub(r"ON CONFLICT \([^)]*\)", "ON CONFLICT (id)", sql)
            return sql.replace(" WHERE ", " WHERE 1=1 OR ")

    statement = EvilInsert(ITEMS)
    statement._post_values_clause = _canonical(env)._post_values_clause
    return statement


class _EvilColumn(ColumnClause):  # type: ignore[type-arg]
    """N3: looks like ``excluded.name`` (a column of the ``excluded`` alias, under
    its name) and renders a sub-select that reads B's row."""

    inherit_cache = True

    def _compiler_dispatch(self, visitor: Any, **kw: Any) -> str:
        return f"(SELECT x.note FROM tups_items AS x WHERE x.id = {B_ITEM_ID})"


class _SameColumn(ColumnClause):  # type: ignore[type-arg]
    """N3, the quiet one: a subclass that renders exactly like ``excluded.name``.
    Only its class can tell it from the real one."""

    inherit_cache = True


def _same_sql_insert(env: _Env) -> Any:
    """N2, the quiet one: an ``Insert`` subclass that changes nothing in the SQL."""
    base = type(conflict_insert_for(env.dialect, ITEMS))

    class QuietInsert(base):  # type: ignore[misc, valid-type]
        inherit_cache = True

    statement = QuietInsert(ITEMS)
    statement._post_values_clause = _canonical(env)._post_values_clause
    return statement


def _self_rendering(env: _Env) -> Any:
    """The classes are exactly the canonical ones and the OBJECT renders itself (the
    conflict target moved to the primary key, the WHERE always true): only the text
    of the SQL can tell."""
    statement = _canonical(env)
    render = type(statement)._compiler_dispatch

    def dispatch(visitor: Any, **kw: Any) -> str:
        sql = render(statement, visitor, **kw)
        sql = re.sub(r"ON CONFLICT \([^)]*\)", "ON CONFLICT (id)", sql)
        return sql.replace(" WHERE ", " WHERE 1=1 OR ")

    statement._compiler_dispatch = dispatch  # type: ignore[method-assign]
    return statement


class _ToB(TypeDecorator[uuid.UUID]):
    """N4: a tenant type that stores whatever it is given as ``target``."""

    impl = Uuid
    cache_ok = True
    target: uuid.UUID | None = None

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return _ToB.target


def _second_table(env: _Env) -> tuple[Any, list[dict[str, Any]]]:
    """N4: a ``Table`` of the same name that no mapper maps, whose tenant column
    rewrites every tenant to B."""
    _ToB.target = env.b
    second = Table(
        "tups_items",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", _ToB(), nullable=False),
        Column("key", Text),
        Column("name", Text),
    )
    return _canonical(env, table=second), [_row(env, name="pwned-n4")]


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
    # --- N1: the guard compares the tenant with an exact type check, never with
    # the ``==`` of an object the caller supplied
    "N1 a row tenant that is a UUID subclass equal to everything": lambda e: (
        _canonical(e),
        [_row(e, key="planted", tenant_id=_liar(e.b))],
    ),
    "N1 the WHERE parameter likewise: B's row rewritten": lambda e: (
        _canonical(e),
        [
            _row(
                e,
                id=B_ITEM_ID,
                tenant_id=_liar(e.b),
                **{tenant_mod.TENANT_UPSERT_PARAM: _liar(e.b)},
            )
        ],
    ),
    # --- N2 / N3: the SQL must be the SQL of the statement the builder makes
    "N2 an Insert subclass that rewrites the conflict target and the WHERE": lambda e: (
        _evil_insert(e),
        [_row(e, id=B_ITEM_ID, name="pwned-n2")],
    ),
    "N3 a SET value that looks like excluded.name and reads B": lambda e: (
        _canonical(
            e,
            set_=lambda x: {
                ITEMS.c.name: _EvilColumn("name", _selectable=x.name.table)
            },
        ),
        [_row(e)],
    ),
    "N2 an Insert subclass that renders the canonical SQL": lambda e: (
        _same_sql_insert(e),
        [_row(e)],
    ),
    "N2 an exact Insert that renders itself": lambda e: (
        _self_rendering(e),
        [_row(e, id=B_ITEM_ID, name="pwned-n2-text")],
    ),
    "N3 a SET value of a subclass that renders as excluded.name": lambda e: (
        _canonical(
            e,
            set_=lambda x: {
                ITEMS.c.name: _SameColumn("name", _selectable=x.name.table)
            },
        ),
        [_row(e)],
    ),
    # --- N4: the table is a mapper's own
    "N4 a second Table of the same name whose tenant type rewrites to B": (
        _second_table
    ),
    "N4 a model table whose column is a Column subclass": lambda e: (
        _canonical(e, table=_table(_Odd)),
        [{"id": 1, "key": "k", "name": "x", "tenant_id": e.a}],
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
            build = helper_mod.canonical_tenant_upsert
            if sets:
                tenant_mod.check_tenant_reach(context, compiled, upsert_builder=build)
            else:
                with pytest.raises(UnsafeTenantOperationError):
                    tenant_mod.check_tenant_reach(
                        context, compiled, upsert_builder=build
                    )
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
    with pytest.raises(IntegrityError):  # the database refuses the row
        # no ``name``: NOT NULL, so the statement reaches the database and fails
        await _upsert(env, env.a, _Item, [{"id": 5, "key": "k"}], update=["key"])
    (statement,) = captured  # the one statement of the one call
    assert statement not in tenant_mod._UPSERTS
    assert statement not in tenant_mod._UPSERT_SHAPES


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


# ------------------------------- what the cursor reads from the statement that ran


@pytest.mark.asyncio
async def test_a_refused_statement_does_not_decide_for_the_next_one(env: _Env) -> None:
    """N5: the shape is read from the statement object that ran, never kept on the
    compiled form the statement cache shares. A refused unmarked statement does not
    deny tenant B's honest call, and an honest call that filled the cache does not
    admit an unmarked statement of the same shape."""
    poison = _canonical(
        env,
        where=ITEMS.c.tenant_id
        == bindparam(
            tenant_mod.TENANT_UPSERT_PARAM,
            callable_=tenant_mod._tenant_criteria_value,
            type_=Uuid(),
        ),
    )
    await _assert_refused_and_untouched(
        env, lambda: _forge(env, poison, [_row(env)], seal=False)
    )
    await _upsert(
        env, env.b, _Item, [{"id": 7, "key": "kb", "name": "nb"}], update=["name"]
    )
    assert (await _items(env))[7][:3] == (env.b, "kb", "nb")
    await _assert_refused_and_untouched(
        env, lambda: _forge(env, _canonical(env), [_row(env)], seal=False)
    )


# ------------------------- a value that only reads as the bound tenant (P1, P2, N1)


class _Honest(uuid.UUID):
    """What a driver returns for a UUID column (asyncpg's ``UUID``): a subclass that
    tells the truth."""


class _SelectiveLiar(uuid.UUID):
    """Equal to ONE object only, the bound tenant the guard compares it with, and
    to nothing else, the value the row was loaded with included: the persistence
    layer sees a change (so the value reaches an UPDATE) while the guard sees the
    bound tenant. An always-equal liar is never written by an UPDATE."""

    passes: Any = None

    def __eq__(self, other: object) -> bool:
        return other is _SelectiveLiar.passes

    def __ne__(self, other: object) -> bool:
        return other is not _SelectiveLiar.passes

    __hash__ = uuid.UUID.__hash__


class _SplitBits(uuid.UUID):
    """Reads as ``int`` the tenant it was built from and writes another ``bytes``."""

    other: uuid.UUID = uuid.UUID(int=0)

    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = uuid.UUID.__hash__

    @property
    def bytes(self) -> bytes:
        return _SplitBits.other.bytes


def test_only_a_plain_uuid_that_is_the_bound_tenant_counts_as_it() -> None:
    tid = uuid.uuid4()
    assert tenant_mod._is_bound_tenant(uuid.UUID(int=tid.int), tid)
    assert not tenant_mod._is_bound_tenant(uuid.uuid4(), tid)
    assert not tenant_mod._is_bound_tenant(_liar(uuid.uuid4()), tid)  # ``==`` says yes
    assert not tenant_mod._is_bound_tenant(_liar(tid), tid)  # right bits, wrong class
    assert not tenant_mod._is_bound_tenant(_Honest(int=tid.int), tid)
    assert not tenant_mod._is_bound_tenant(str(tid), tid)
    assert not tenant_mod._is_bound_tenant(None, tid)


def test_the_bound_tenant_is_read_as_a_plain_uuid() -> None:
    tid = uuid.uuid4()
    assert tenant_mod._plain_uuid(tid) is tid
    plain = tenant_mod._plain_uuid(_Honest(int=tid.int))
    assert type(plain) is uuid.UUID and plain == tid
    plain = tenant_mod._plain_uuid(_liar(tid))
    assert type(plain) is uuid.UUID and plain.int == tid.int
    with bind_tenant(_Honest(int=tid.int)):
        bound = tenant_mod._bound_tenant()
    assert bound is not None and type(bound) is uuid.UUID and bound == tid


def test_a_row_carrying_a_driver_class_of_the_tenant_is_given_the_plain_one() -> None:
    tid = uuid.uuid4()
    honest = SimpleNamespace(tenant_id=_Honest(int=tid.int))
    assert tenant_mod._adopt_bound_tenant(honest, tid)
    assert honest.tenant_id is tid
    own = SimpleNamespace(tenant_id=tid)
    assert tenant_mod._adopt_bound_tenant(own, tid)
    assert own.tenant_id is tid


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_liar(uuid.uuid4()), id="equal to everything"),
        pytest.param(_Honest(int=uuid.uuid4().int), id="another tenant"),
        pytest.param("not a uuid", id="a string"),
        pytest.param(uuid.uuid4(), id="a plain other tenant"),
    ],
)
def test_a_row_whose_tenant_is_not_the_bound_one_is_not_adopted(value: Any) -> None:
    tid = uuid.uuid4()
    row = SimpleNamespace(tenant_id=value)
    assert not tenant_mod._adopt_bound_tenant(row, tid)
    assert row.tenant_id is value  # nothing was rewritten


def test_a_subclass_whose_bits_disagree_with_each_other_is_not_adopted() -> None:
    tid = uuid.uuid4()
    _SplitBits.other = uuid.uuid4()  # what it would write to the database
    row = SimpleNamespace(tenant_id=_SplitBits(int=tid.int))
    assert not tenant_mod._adopt_bound_tenant(row, tid)


@pytest.mark.asyncio
async def test_a_criterion_value_that_only_reads_as_the_bound_tenant_is_refused(
    env: _Env,
) -> None:
    """P1: the ORM refuses a caller's parameter that names the scoping bind, whatever
    it holds, and the cursor backstop takes only a plain UUID that IS the bound
    tenant (``==`` is the value's own word)."""
    param = tenant_mod.TENANT_CRITERIA_PARAM
    async with env.maker() as session:
        with bind_tenant(env.a):
            with pytest.raises(UnsafeTenantOperationError, match="caller-supplied"):
                await session.execute(
                    select(_Item.note).where(_Item.id == B_ITEM_ID),
                    {param: _liar(env.b)},
                )
    with bind_tenant(env.a):
        tenant_mod._check_criterion(
            SimpleNamespace(compiled_parameters=[{param: env.a}])
        )
        for value in (_liar(env.b), _liar(env.a), _Honest(int=env.a.int), str(env.a)):
            with pytest.raises(UnsafeTenantOperationError):
                tenant_mod._check_criterion(
                    SimpleNamespace(compiled_parameters=[{param: value}])
                )


@pytest.mark.asyncio
async def test_a_new_row_whose_tenant_id_only_reads_as_the_bound_tenant_is_refused(
    env: _Env,
) -> None:
    """P2, ``before_flush``: the row of tenant B whose ``==`` says A."""
    before = await _snapshot(env)
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_Item(id=55, tenant_id=_liar(env.b), key="planted", name="x"))
            with pytest.raises(TenantMismatchError):
                await session.commit()
    assert await _snapshot(env) == before


@pytest.mark.asyncio
async def test_a_tenant_id_swapped_after_the_before_flush_check_is_refused(
    env: _Env,
) -> None:
    """P2, the mapper event: a ``before_flush`` listener of the session itself runs
    after the guard's own and swaps the value in."""
    before = await _snapshot(env)

    def swap(session: Any, _context: Any, _instances: Any) -> None:
        for obj in session.new:
            if isinstance(obj, _Item):
                obj.tenant_id = _liar(env.b)

    async with env.maker() as session:
        event.listen(session.sync_session, "before_flush", swap)
        with bind_tenant(env.a):
            session.add(_Item(id=55, key="planted", name="x"))
            with pytest.raises(TenantMismatchError):
                await session.commit()
    assert await _snapshot(env) == before


@pytest.mark.asyncio
async def test_a_tenant_id_swapped_in_a_late_before_insert_is_refused_at_the_cursor(
    env: _Env,
) -> None:
    """P2, the cursor: a listener of the model runs after the mapper events of the
    guard, so the INSERT carries a value nothing verified. The refusal is the
    cursor's, not the mapper event's."""
    before = await _snapshot(env)

    def swap(_mapper: Any, _connection: Any, target: Any) -> None:
        target.tenant_id = _liar(env.b)

    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_Item(id=55, key="planted", name="x"))
            event.listen(_Item, "before_insert", swap)
            try:
                with pytest.raises(Exception) as caught:
                    await session.commit()
            finally:
                event.remove(_Item, "before_insert", swap)
    assert _is_refusal(caught.value), repr(caught.value)
    assert not isinstance(caught.value, TenantMismatchError)
    assert await _snapshot(env) == before


@pytest.mark.asyncio
async def test_a_tenant_id_swapped_in_a_late_before_update_is_refused_at_the_cursor(
    env: _Env,
) -> None:
    """P2, an UPDATE that would move the row of tenant A to B: the value is equal to
    the bound tenant for the guard and different for the persistence layer."""
    _SelectiveLiar.passes = env.a
    before = await _snapshot(env)

    def swap(_mapper: Any, _connection: Any, target: Any) -> None:
        target.tenant_id = _SelectiveLiar(int=env.b.int)

    async with env.maker() as session:
        with bind_tenant(env.a):
            item = await session.get(_Item, A_ITEM_ID)
            assert item is not None
            item.name = "moved"
            event.listen(_Item, "before_update", swap)
            try:
                with pytest.raises(Exception) as caught:
                    await session.commit()
            finally:
                event.remove(_Item, "before_update", swap)
    assert _is_refusal(caught.value), repr(caught.value)
    assert not isinstance(caught.value, TenantMismatchError)
    assert await _snapshot(env) == before


@pytest.mark.asyncio
async def test_a_driver_class_of_the_bound_tenant_is_not_refused(env: _Env) -> None:
    """The class a driver returns for a UUID column is a ``uuid.UUID`` subclass that
    tells the truth, and the outbox hooks, sagas and request scopes bind the value
    they loaded. Under it a flush, an upsert and a read all work as under the plain
    UUID, and the row is written with the plain one."""
    honest = _Honest(int=env.a.int)
    async with env.maker() as session:
        with bind_tenant(honest):
            item = _Item(id=56, tenant_id=_Honest(int=env.a.int), key="h", name="x")
            session.add(item)
            await session.commit()
            assert type(item.tenant_id) is uuid.UUID
            notes = await session.execute(select(_Item.note).order_by(_Item.id))
            assert notes.scalars().all() == ["a-note", None]
    await _upsert(
        env,
        honest,
        _Item,
        [{"id": 57, "key": "shared", "name": "honest-upsert"}],
        update=["name"],
    )
    items = await _items(env)
    assert items[56][:3] == (env.a, "h", "x")
    assert items[A_ITEM_ID][:3] == (env.a, "shared", "honest-upsert")
    assert items[B_ITEM_ID][2] == "b-name"


# --------------------------------- the helper: a table that resolves conflicts itself


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "row", "update", "named"),
    [
        (
            _ReplacingPk,
            {"id": 1, "key": "k", "name": "n"},
            ["name"],
            "constraint PrimaryKeyConstraint: sqlite_on_conflict",
        ),
        (
            _ReplacingUnique,
            {"id": 1, "key": "k", "email": "e"},
            ["email"],
            "constraint UniqueConstraint: sqlite_on_conflict",
        ),
        (
            _ReplacingColumn,
            {"id": 1, "key": "k", "email": "e"},
            ["email"],
            "column email: sqlite_on_conflict_unique",
        ),
    ],
    ids=["primary key", "unique constraint", "column"],
)
async def test_a_table_with_a_conflict_resolution_of_its_own_is_refused(
    model: Any, row: dict[str, Any], update: list[str], named: str
) -> None:
    error = await _refused(model, [row], update=update)
    assert named in str(error)


@pytest.mark.asyncio
async def test_sqlite_replace_never_deletes_the_row_of_another_tenant(
    env: _Env,
) -> None:
    """A1: under ``PRIMARY KEY (id) ON CONFLICT REPLACE`` SQLite deletes the row that
    holds the key, whoever owns it, before ``ON CONFLICT DO UPDATE`` is asked."""
    async with env.maker() as session:
        with unscoped():
            session.add(_ReplacingPk(id=1, tenant_id=env.b, key="k", name="b-name"))
            await session.commit()
    with pytest.raises(TenantUpsertError, match="sqlite_on_conflict"):
        await _upsert(
            env,
            env.a,
            _ReplacingPk,
            [{"id": 1, "key": "k2", "name": "pwned"}],
            update=["name"],
        )
    rows = await _read(env, _ReplacingPk.id, _ReplacingPk.tenant_id, _ReplacingPk.name)
    assert rows == [(1, env.b, "b-name")]


@pytest.mark.asyncio
async def test_a_column_that_is_not_exactly_a_column_is_refused() -> None:
    error = await _refused(_Odd, [_ROW], update=["name"])
    assert "not exactly sqlalchemy.Column" in str(error)


# ------------------------------------------ the helper: the database the rows go to


class _TwoBinds(_Recorder):
    """A session that routes the model's mapper to another database than its
    default one (``Session(binds=...)``)."""

    def __init__(self, default: Any, mapper_bind: Any) -> None:
        super().__init__(default)
        self._mapper_bind = type("Bind", (), {"dialect": mapper_bind})()
        self.bind_arguments: list[Any] = []

    def get_bind(self, mapper: Any = None, **kwargs: Any) -> Any:
        return self._bind if mapper is None else self._mapper_bind

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        self.bind_arguments.append(kwargs.get("bind_arguments"))
        return self


@pytest.mark.asyncio
async def test_the_statement_is_built_for_the_database_of_the_mapper() -> None:
    """A4: the dialect that renders and checks the statement is the one of the bind
    the connection resolves for the mapper, not the session's default."""
    rows = [{"id": 1, "key": "k", "name": "n"}]
    with bind_tenant(uuid.uuid4()):
        refused = _TwoBinds(sqlite.dialect(), mysql.dialect())  # type: ignore[no-untyped-call]
        with pytest.raises(UnsupportedSqlForm):
            await tenant_upsert(_Item, rows, update=["name"], session=refused)  # type: ignore[arg-type]
        routed = _TwoBinds(mysql.dialect(), postgresql.dialect())  # type: ignore[no-untyped-call]
        await tenant_upsert(_Item, rows, update=["name"], session=routed)  # type: ignore[arg-type]
    ((statement, _),) = routed.sent
    assert "ON CONFLICT" in str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert routed.bind_arguments == [{"mapper": inspect(_Item)}]


# ------------------------------------------------- the helper: which key it conflicts on


@pytest.mark.asyncio
async def test_the_candidate_keys_are_named_by_attribute() -> None:
    """``conflict_on`` takes attribute names; the message lists the keys in them
    (the attribute ``label`` is the column ``title``)."""
    row = {"id": 1, "key": "k", "label": "l"}
    error = await _refused(_Keyed, [row], update=["label"])
    assert "tenant_id, key; tenant_id, label" in str(error)
    assert "title" not in str(error)
    error = await _refused(_Keyed, [row], update=["label"], conflict_on=["id"])
    assert "tenant_id, key; tenant_id, label" in str(error)
    session = _Recorder(sqlite.dialect())  # type: ignore[no-untyped-call]
    with bind_tenant(uuid.uuid4()):
        await tenant_upsert(
            _Keyed,
            [row],
            update=["key"],
            conflict_on=["label"],
            session=session,  # type: ignore[arg-type]
        )
    ((statement, _),) = session.sent
    sql = str(statement.compile(dialect=sqlite.dialect())).replace('"', "")  # type: ignore[no-untyped-call]
    assert "ON CONFLICT (tenant_id, title)" in sql


def test_the_candidate_keys_come_in_the_order_of_the_columns() -> None:
    """``Table.constraints`` is a set: the order of the keys (in the message and in
    the statement) is the columns', not the order the hashes of the constraints
    give. Eight keys: the odds of the right order by luck are 1 in 40320."""
    table = Table(
        "tups_ordered",
        MetaData(),
        Column("tenant_id", Uuid()),
        *(Column(f"c{i}", Text) for i in range(8)),
        *(UniqueConstraint("tenant_id", f"c{i}") for i in range(8)),
    )
    keys = helper_mod._unique_keys(table)
    assert [[c.name for c in key] for key in keys] == [
        ["tenant_id", f"c{i}"] for i in range(8)
    ]


@pytest.mark.asyncio
async def test_a_deferrable_key_is_not_a_candidate() -> None:
    """PostgreSQL cannot infer a ``DEFERRABLE`` constraint as the arbiter: ``slug``
    is the one key, and the deferrable one cannot be named."""
    rows = [{"id": 1, "key": "k", "slug": "s", "name": "n"}]
    session = _Recorder(postgresql.dialect())  # type: ignore[no-untyped-call]
    with bind_tenant(uuid.uuid4()):
        await tenant_upsert(_Deferred, rows, update=["name"], session=session)  # type: ignore[arg-type]
    ((statement, _),) = session.sent
    sql = str(statement.compile(dialect=postgresql.dialect())).replace('"', "")  # type: ignore[no-untyped-call]
    assert "ON CONFLICT (tenant_id, slug)" in sql
    error = await _refused(_Deferred, rows, update=["name"], conflict_on=["key"])
    assert "not a unique key" in str(error)
    assert "by attribute: tenant_id, slug)" in str(error)


@pytest_asyncio.fixture
async def pg_tables() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """The PostgreSQL-only tables (``DEFERRABLE`` key, foreign keys), on the
    PostgreSQL the lane runs: a missing database is a failure, never a skip."""
    url = settings.database_url
    if not url.startswith("postgresql"):
        pytest.fail(f"these tests need a PostgreSQL URL, got {url[:12]!r}")
    engine = create_async_engine(url)
    install_write_admission(engine)
    try:
        with unscoped():
            async with engine.begin() as conn:
                await conn.run_sync(_PgBase.metadata.drop_all)
                await conn.run_sync(_PgBase.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            with unscoped():
                async with engine.begin() as conn:
                    await conn.run_sync(_PgBase.metadata.drop_all)
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_the_key_it_picks_next_to_a_deferrable_one_runs_on_postgresql(
    pg_tables: async_sessionmaker[AsyncSession],
) -> None:
    tenant = uuid.uuid4()
    for name in ("first", "second"):
        async with pg_tables() as session:
            with bind_tenant(tenant):
                await tenant_upsert(
                    _Deferred,
                    [{"id": 1, "key": name, "slug": "s", "name": name}],
                    update=["name"],
                    session=session,
                )
                await session.commit()
    async with pg_tables() as session:
        with unscoped():
            result = await session.execute(select(_Deferred.key, _Deferred.name))
            assert [tuple(row) for row in result.all()] == [("first", "second")]


async def _parent_of(
    maker: async_sessionmaker[AsyncSession], tenant: uuid.UUID, parent_id: int
) -> None:
    """A parent row of ``tenant`` (system write)."""
    async with maker() as session:
        with unscoped():
            session.add(_PgParent(id=parent_id, tenant_id=tenant, name="parent"))
            await session.commit()


async def _child_upsert(
    maker: async_sessionmaker[AsyncSession],
    tenant: uuid.UUID,
    model: Any,
    parent_id: int,
) -> None:
    async with maker() as session:
        with bind_tenant(tenant):
            await tenant_upsert(
                model,
                [{"id": 1, "key": "k", "parent_id": parent_id}],
                update=["parent_id"],
                session=session,
            )
            await session.commit()


@pytest.mark.asyncio
async def test_a_foreign_key_without_the_tenant_acts_as_on_a_plain_insert(
    pg_tables: async_sessionmaker[AsyncSession],
) -> None:
    """RT-A2: the helper keeps another tenant's row from being the conflicting row,
    nothing more. A foreign key that leaves the tenant out accepts the parent of
    another tenant, and a parent that does not exist is the database's error: the
    docs advise the composite key of the next test."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _parent_of(pg_tables, tenant_b, 7)
    await _child_upsert(pg_tables, tenant_a, _PgPlainChild, 7)
    async with pg_tables() as session:
        with unscoped():
            result = await session.execute(
                select(_PgPlainChild.tenant_id, _PgPlainChild.parent_id)
            )
            assert [tuple(row) for row in result.all()] == [(tenant_a, 7)]
    with pytest.raises(IntegrityError):
        await _child_upsert(pg_tables, tenant_a, _PgPlainChild, 99)


@pytest.mark.asyncio
async def test_a_composite_foreign_key_refuses_the_parent_of_another_tenant(
    pg_tables: async_sessionmaker[AsyncSession],
) -> None:
    """The advice of the docs holds: with ``(tenant_id, parent_id)`` the parent of
    another tenant is no parent, and the tenant's own one is."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _parent_of(pg_tables, tenant_b, 7)
    with pytest.raises(IntegrityError):
        await _child_upsert(pg_tables, tenant_a, _PgTenantChild, 7)
    await _parent_of(pg_tables, tenant_a, 8)
    await _child_upsert(pg_tables, tenant_a, _PgTenantChild, 8)
    async with pg_tables() as session:
        with unscoped():
            result = await session.execute(
                select(_PgTenantChild.tenant_id, _PgTenantChild.parent_id)
            )
            assert [tuple(row) for row in result.all()] == [(tenant_a, 8)]


# --------------------- the helper: a statement that reads a tenant table is refused first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "row", "update"),
    [
        (_Defaulted, {"id": 1, "key": "k", "name": "n"}, ["name"]),
        (_Peeking, {"id": 1, "key": "k", "name": "n"}, ["name"]),
    ],
    ids=["column default", "bind_expression"],
)
async def test_a_statement_that_would_read_a_tenant_table_is_refused_before_a_flush(
    model: Any, row: dict[str, Any], update: list[str]
) -> None:
    """The fake session fails the test on a flush: the refusal came first, so the
    pending changes of the session were not sent."""
    error = await _refused(model, [row], update=update)
    assert "would read the tenant table tups_items" in str(error)


@pytest.mark.asyncio
async def test_a_statement_that_reads_nothing_is_not_refused() -> None:
    """The controls: the default is not rendered when the row names its column, and
    the type's expression is not rendered when the column is left out."""
    with bind_tenant(uuid.uuid4()):
        defaulted = _Recorder(sqlite.dialect())  # type: ignore[no-untyped-call]
        await tenant_upsert(
            _Defaulted,
            [{"id": 1, "key": "k", "name": "n", "snap": "given"}],
            update=["name"],
            session=defaulted,  # type: ignore[arg-type]
        )
        peeking = _Recorder(sqlite.dialect())  # type: ignore[no-untyped-call]
        await tenant_upsert(
            _Peeking,
            [{"id": 1, "key": "k"}],
            update=["key"],
            session=peeking,  # type: ignore[arg-type]
        )
    assert len(defaulted.sent) == 1 and len(peeking.sent) == 1


@pytest.mark.asyncio
async def test_a_refused_read_leaves_the_pending_changes_of_the_session_pending(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            pending = _Item(id=77, key="pending", name="p")
            session.add(pending)
            with pytest.raises(TenantUpsertError, match="would read"):
                await tenant_upsert(
                    _Defaulted,
                    [{"id": 1, "key": "k", "name": "n"}],
                    update=["name"],
                    session=session,
                )
            assert pending in session.new
            await session.rollback()
    assert 77 not in await _items(env)
