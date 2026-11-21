"""The tenant guard at the cursor (ADR-0018): every red-team bypass of the
statement guard, on SQLite and on PostgreSQL without RLS.

The ``do_orm_execute`` guard reads the statement a caller hands the session.
SQL also reaches the cursor from places that guard never sees (a ``Table`` that
only shares the tenant table's NAME, SQL a mapper renders on its own, column
defaults, ``session.connection()``, the persistence layer's own statements, a
forged instance), so the rule sits where it all ends up: outside ``unscoped()``
a statement that names a tenant table is admitted only when the ORM guard
decided on it, or as a flush write of a row the mapper events verified.

Each ``_REFUSED`` shape (rt342-a #2-#14), each runtime cell below (rt342-a #1,
rt342-b B1-B10, FP3) is run bound, unbound and inside ``unscoped()`` (where it
reaches tenant B's row: the refusal is the guard's, not a broken statement).
Models and rows are small and fixed: tenant A owns item 1, tenant B item 2.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    DDL,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    Uuid,
    bindparam,
    cast,
    column,
    delete,
    event,
    func,
    insert,
    inspect,
    lambda_stmt,
    literal,
    literal_column,
    select,
    table,
    text,
    true,
    update,
    values,
)
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    column_property,
    deferred,
    immediateload,
    joinedload,
    load_only,
    make_transient_to_detached,
    mapped_column,
    query_expression,
    registry,
    relationship,
    selectinload,
    subqueryload,
    undefer,
    with_loader_criteria,
    with_polymorphic,
)
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement, ColumnElement

from sandloom.core import tenant as tenant_mod
from sandloom.core.di import (
    MissingTenantError,
    TenantMismatchError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy import model_schema
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.tenant import TENANT_CRITERIA_PARAM
from sandloom.core.testing import database as harness
from sandloom.core.testing import reload_test_env

A_ITEM_ID = 1
B_ITEM_ID = 2


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    __tablename__ = "tgcur_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_id: Mapped[int | None] = mapped_column(
        ForeignKey("tgcur_others.id"), nullable=True
    )


ITEMS = _Item.__table__


class _Leaf(_Base):
    __tablename__ = "tgcur_leaves"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class _Other(_Base):
    """Not tenant-scoped."""

    __tablename__ = "tgcur_others"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    items: Mapped[list[_Item]] = relationship(_Item, viewonly=True)
    leaves_via_items: Mapped[list[_Leaf]] = relationship(
        _Leaf,
        secondary=ITEMS,
        primaryjoin=lambda: _Other.id == ITEMS.c.other_id,
        secondaryjoin=lambda: _Leaf.name == ITEMS.c.secret,
        viewonly=True,
    )


class _OtherCP(_Base):
    """A raw ``column_property`` over the tenant table, on a non-tenant model."""

    __tablename__ = "tgcur_other_cp"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer: Mapped[str | None] = column_property(
        select(func.max(ITEMS.c.secret)).scalar_subquery()
    )


class _OtherCPOrm(_Base):
    __tablename__ = "tgcur_other_cp_orm"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer: Mapped[str | None] = column_property(
        select(func.max(_Item.secret)).scalar_subquery()
    )


class _OtherDeferred(_Base):
    __tablename__ = "tgcur_other_deferred"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer: Mapped[str | None] = deferred(
        select(func.max(_Item.secret)).scalar_subquery()
    )


class _OtherQuery(_Base):
    __tablename__ = "tgcur_other_query"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer: Mapped[str | None] = query_expression(
        select(func.max(ITEMS.c.secret)).scalar_subquery()
    )


class _ItemCP(TenantScoped, _Base):
    """A raw ``column_property`` over ANOTHER tenant table, on a tenant model."""

    __tablename__ = "tgcur_items_cp"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer: Mapped[str | None] = column_property(
        select(func.max(ITEMS.c.secret)).scalar_subquery()
    )


class _Animal(_Base):
    """A non-tenant joined-inheritance root with a TenantScoped subclass."""

    __tablename__ = "tgcur_animals"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "animal"}


class _Cat(TenantScoped, _Animal):
    __tablename__ = "tgcur_cats"

    id: Mapped[int] = mapped_column(ForeignKey("tgcur_animals.id"), primary_key=True)
    secret: Mapped[str] = mapped_column(Text)
    __mapper_args__ = {"polymorphic_identity": "cat"}


class _NameMix:
    name: Mapped[str] = mapped_column(Text)


class _MixA(_NameMix, _Base):
    __tablename__ = "tgcur_mix_a"

    id: Mapped[int] = mapped_column(primary_key=True)


class _MixB(_NameMix, _Base):
    __tablename__ = "tgcur_mix_b"

    id: Mapped[int] = mapped_column(primary_key=True)


IMP = Table(
    "tgcur_imp",
    _Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("name", Text),
)


class _Imp(TenantScoped):
    """Mapped imperatively: it has no ``__table__``."""


registry(metadata=_Base.metadata).map_imperatively(_Imp, IMP)

#: A second ``Table`` with the tenant table's NAME in another ``MetaData``.
_SHADOW_TABLE = Table(
    "tgcur_items",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("name", Text),
    Column("secret", Text),
    Column("tenant_id", Uuid),
)


class _Shadow:
    pass


registry().map_imperatively(_Shadow, _SHADOW_TABLE)

#: Second ``Table`` objects over the tenant table that no mapper owns (closure 2,
#: N1): one declares ``other_id`` as its primary key, one swaps the KEYS of
#: ``name`` and ``tenant_id`` (the parameter named ``tenant_id`` feeds ``name``).
_FAKE_KEY_ITEMS = Table(
    "tgcur_items",
    MetaData(),
    Column("id", Integer),
    Column("other_id", Integer, primary_key=True),
    Column("name", Text),
    Column("tenant_id", Uuid),
)
_KEY_SWAPPED_ITEMS = Table(
    "tgcur_items",
    MetaData(),
    Column("id", Integer, primary_key=True),
    Column("name", Uuid, key="tenant_id"),
    Column("tenant_id", Uuid, key="tid"),
    Column("other_id", Integer),
)

#: A tenant model whose mapper lists its primary key in the REVERSE of the
#: table's order: the identity key is ``(b, a)``, a statement's WHERE ``(a, b)``.
REVERSED = Table(
    "tgcur_reversed",
    _Base.metadata,
    Column("a", Integer, primary_key=True, autoincrement=False),
    Column("b", Integer, primary_key=True, autoincrement=False),
    Column("tenant_id", Uuid, nullable=False),
    Column("note", Text),
)


class _Reversed(TenantScoped):
    """Mapped imperatively with ``primary_key=[b, a]``."""


registry(metadata=_Base.metadata).map_imperatively(
    _Reversed, REVERSED, primary_key=[REVERSED.c.b, REVERSED.c.a]
)


class _Parent(_Base):
    """Not tenant-scoped; a many-to-one to the tenant model."""

    __tablename__ = "tgcur_parents"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("tgcur_items.id"))
    item: Mapped[_Item | None] = relationship()


def _steal_through_the_flush_connection(ctx: Any) -> Any:
    return ctx.connection.execute(
        select(_Item.secret).where(_Item.id == B_ITEM_ID)
    ).scalar()


_B_SECRET_RAW = select(ITEMS.c.secret).where(ITEMS.c.id == B_ITEM_ID).scalar_subquery()


class _RawSink(_Base):
    """Not tenant-scoped; a raw tenant-table read as default and onupdate."""

    __tablename__ = "tgcur_raw_sinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    stolen: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=_B_SECRET_RAW, onupdate=_B_SECRET_RAW
    )


class _TextSink(_Base):
    __tablename__ = "tgcur_text_sinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stolen: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=text(f"(SELECT secret FROM tgcur_items WHERE id = {B_ITEM_ID})"),
    )


class _LiteralSink(_Base):
    __tablename__ = "tgcur_literal_sinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stolen: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=literal_column(
            f"(SELECT secret FROM tgcur_items WHERE id = {B_ITEM_ID})"
        ),
    )


class _CtxSink(_Base):
    """A context-sensitive Python default that runs Core on the flush connection."""

    __tablename__ = "tgcur_ctx_sinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stolen: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=_steal_through_the_flush_connection
    )
    note: Mapped[str | None] = mapped_column(
        Text, nullable=True, onupdate=_steal_through_the_flush_connection
    )


class _Ticked(ColumnElement[str]):
    """A custom ``@compiles`` expression that reads no table."""

    inherit_cache = True
    type = Text()


class _CopiesB(ColumnElement[str]):
    """A custom ``@compiles`` expression that reads tenant B's secret."""

    inherit_cache = True
    type = Text()


@compiles(_Ticked)
def _render_ticked(element: Any, compiler: Any, **kw: Any) -> str:
    return "'ticked'"


@compiles(_CopiesB)
def _render_copies_b(element: Any, compiler: Any, **kw: Any) -> str:
    return f"(SELECT secret FROM tgcur_items WHERE id = {B_ITEM_ID})"


class _Stamped(TenantScoped, _Base):
    """Tenant-scoped, with a custom default and onupdate that read no table."""

    __tablename__ = "tgcur_stamped"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    touched: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=_Ticked(), onupdate=_Ticked()
    )


class _CustomSink(_Base):
    """Not tenant-scoped; a custom ``@compiles`` default that reads a tenant table."""

    __tablename__ = "tgcur_custom_sinks"

    id: Mapped[int] = mapped_column(primary_key=True)
    stolen: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=_CopiesB(), onupdate=_CopiesB()
    )


_ASSOC = Table(
    "tgcur_assoc",
    _Base.metadata,
    Column("left_id", ForeignKey("tgcur_left.id"), primary_key=True),
    Column("right_id", ForeignKey("tgcur_right.id"), primary_key=True),
    Column("stolen", Text, default=_B_SECRET_RAW),
)


class _Left(_Base):
    __tablename__ = "tgcur_left"

    id: Mapped[int] = mapped_column(primary_key=True)
    rights: Mapped[list[_Right]] = relationship(secondary=_ASSOC)


class _Right(_Base):
    __tablename__ = "tgcur_right"

    id: Mapped[int] = mapped_column(primary_key=True)


class _Holder(_Base):
    """Not tenant-scoped; a many-to-one to a model whose own load reads the tenant
    table raw (``_OtherCP``): the child loader statements inherit its options."""

    __tablename__ = "tgcur_holders"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column()
    cp_id: Mapped[int] = mapped_column(ForeignKey("tgcur_other_cp.id"))
    cp: Mapped[_OtherCP] = relationship(_OtherCP)


#: the tenant a flush-window default plants a row for (set by the test)
_PLANT_FOR: list[uuid.UUID] = []
_PLANTED_ID = 99


def _delete_b_item_on_the_flush_connection(ctx: Any) -> int:
    ctx.connection.execute(delete(ITEMS).where(ITEMS.c.id == B_ITEM_ID))
    return 7


def _plant_a_row_for_b_on_the_flush_connection(ctx: Any) -> int:
    ctx.connection.execute(
        insert(ITEMS).values(id=_PLANTED_ID, name="planted", tenant_id=_PLANT_FOR[0])
    )
    return 7


class _Wiper(_Base):
    """Not tenant-scoped; a Python default that deletes a tenant row (Core, flush connection)."""

    __tablename__ = "tgcur_wipers"

    id: Mapped[int] = mapped_column(primary_key=True)
    v: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=_delete_b_item_on_the_flush_connection
    )


class _Planter(_Base):
    """Not tenant-scoped; a Python default that inserts a row into a tenant table."""

    __tablename__ = "tgcur_planters"

    id: Mapped[int] = mapped_column(primary_key=True)
    v: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=_plant_a_row_for_b_on_the_flush_connection
    )


@dataclass
class _Env:
    engine: AsyncEngine
    maker: async_sessionmaker[AsyncSession]
    a: uuid.UUID
    b: uuid.UUID


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
        url = f"sqlite+aiosqlite:///{tmp_path / 'cursor.db'}"
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
            session.add_all([_Other(id=1, name="o1"), _Other(id=2, name="b-secret")])
            await session.flush()
            session.add_all(
                [
                    _Item(
                        id=A_ITEM_ID,
                        tenant_id=state.a,
                        name="a-name",
                        secret="a-secret",
                        other_id=1,
                    ),
                    _Item(
                        id=B_ITEM_ID,
                        tenant_id=state.b,
                        name="b-name",
                        secret="b-secret",
                        other_id=1,
                    ),
                    _Leaf(id=1, name="a-secret"),
                    _Leaf(id=2, name="b-secret"),
                    _OtherCP(id=1),
                    _OtherCPOrm(id=1),
                    _OtherDeferred(id=1),
                    _OtherQuery(id=1),
                    _ItemCP(id=1, tenant_id=state.a),
                    _MixA(id=1, name="b-secret"),
                    _MixB(id=1, name="b-secret"),
                    _Cat(id=1, tenant_id=state.a, secret="a-secret"),
                    _Cat(id=2, tenant_id=state.b, secret="b-secret"),
                    _Parent(id=1, item_id=B_ITEM_ID),
                ]
            )
            await session.flush()
            session.add(_Holder(id=1, item_id=A_ITEM_ID, cp_id=1))
            await session.flush()
            await session.execute(
                insert(IMP),
                [
                    {"id": 1, "tenant_id": state.a, "name": "a-secret"},
                    {"id": 2, "tenant_id": state.b, "name": "b-secret"},
                ],
            )
            await session.commit()
    return state


def _fetch(result: Any) -> list[Any]:
    if isinstance(result, CursorResult) and not result.returns_rows:
        return []
    return list(result.unique().all())


async def _bound(
    env: _Env, statement: Any, params: Any = None, **options: Any
) -> list[Any]:
    """Run ``statement`` as tenant A."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            result = await session.execute(statement, params, execution_options=options)
            rows = _fetch(result)
            await session.commit()
            return rows


async def _unbound(env: _Env, statement: Any) -> list[Any]:
    async with env.maker() as session:
        return _fetch(await session.execute(statement))


async def _unscoped(env: _Env, statement: Any) -> list[Any]:
    async with env.maker() as session:
        with unscoped():
            rows = _fetch(await session.execute(statement))
            await session.commit()
            return rows


async def _b_item(env: _Env) -> Any:
    rows = await _unscoped(
        env,
        select(_Item.name, _Item.secret, _Item.tenant_id).where(_Item.id == B_ITEM_ID),
    )
    return rows[0]


async def _assert_b_untouched(env: _Env) -> None:
    row = await _b_item(env)
    assert (row.name, row.secret, row.tenant_id) == ("b-name", "b-secret", env.b)
    count = await _unscoped(env, select(func.count()).select_from(_Item))
    assert count[0][0] == 2


def _is_refusal(error: BaseException) -> bool:
    """The guard's refusal, bare or wrapped by SQLAlchemy where a column default
    ran the refused Core statement while the outer one prepared its parameters."""
    cause = error.orig if isinstance(error, StatementError) else error
    return isinstance(cause, UnsafeTenantOperationError)


def _plain(value: Any) -> Any:
    if hasattr(value, "_sa_instance_state"):
        return {
            key: _plain(item)
            for key, item in inspect(value).dict.items()
            if not key.startswith("_sa_")
        }
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _show(rows: list[Any]) -> str:
    """Rows with their mapped values spelled out (an entity repr names none)."""
    return repr([_plain(value) for row in rows for value in row])


# ------------------------------------------- the statement shapes: A #2 - #14 ---
def _adapt_on_names(e: _Env) -> Any:
    sub = select(
        ITEMS.c.id,
        ITEMS.c.name,
        literal(e.a, Uuid).label("tenant_id"),
        ITEMS.c.other_id,
    ).subquery()
    return select(aliased(_Item, sub, adapt_on_names=True).name)


def _aliased_cross_join(e: _Env) -> Any:
    ra, rb = ITEMS.alias("ra"), ITEMS.alias("rb")
    sub = (
        select(ra.c.id, ra.c.tenant_id, rb.c.name, ra.c.other_id)
        .select_from(ra.join(rb, true()))
        .subquery()
    )
    return select(aliased(_Item, sub).name)


def _branching_criteria(e: _Env) -> Any:
    raw = select(func.max(ITEMS.c.secret)).scalar_subquery()
    option = with_loader_criteria(
        _NameMix,
        lambda cls: cls.name == (raw if cls.__name__ == "_MixB" else literal("zzz")),
        include_aliases=True,
    )
    return select(_MixB.name).options(option)


class _RawExecutable(Executable, ClauseElement):
    inherit_cache = True


@compiles(_RawExecutable)
def _render_raw_executable(element: Any, compiler: Any, **kw: Any) -> str:
    return "SELECT secret FROM tgcur_items"


def _values_over(inner: Any) -> Any:
    named = values(column("n", Text), name="v").data([(inner,)])
    return select(named.c.n)


#: SQL that names the tenant table where a scan that skips quoted text would not
#: see it (PostgreSQL's dollar quotes, E strings, a unicode-escaped identifier:
#: ``\0074`` is ``t``), each returning tenant B's value on PostgreSQL.
_QUOTE_FRAGMENTS = {
    "dollar quotes": "$q$'$q$ || (SELECT max(secret) FROM tgcur_items) || $q$'$q$",
    "an E string": "E'\\'' || (SELECT max(secret) FROM tgcur_items) || ''",
    "a unicode-escaped identifier": '(SELECT max(secret) FROM U&"\\0074gcur_items")',
}


def _hidden_in_quotes(fragment: str) -> Any:
    return select(_Other.id, literal_column(fragment))


#: shape -> statement (leaking tenant B's value when nothing guards it) -- the
#: rt342-a cells, one statement each; ``True``: it runs on both dialects inside
#: ``unscoped()`` and returns B's value (a named ``VALUES`` is PostgreSQL-only).
_REFUSED: dict[str, tuple[Callable[[_Env], Any], bool]] = {
    # 2: an ORM annotation over an arbitrary selectable
    "aliased(Item, subquery, adapt_on_names)": (_adapt_on_names, True),
    "aliased(Item, cross join of two raw aliases)": (_aliased_cross_join, True),
    # 3: a table known only by name
    "lightweight table()": (
        lambda e: select(table("tgcur_items", column("secret")).c.secret),
        True,
    ),
    "a second Table of the same name": (
        lambda e: select(
            Table("tgcur_items", MetaData(), Column("secret", Text)).c.secret
        ),
        True,
    ),
    "to_metadata copy": (
        lambda e: select(ITEMS.to_metadata(MetaData()).c.secret),
        True,
    ),
    "a Table of the same name with a schema": (
        lambda e: select(
            Table(
                "tgcur_items", MetaData(), Column("secret", Text), schema="x"
            ).c.secret
        ),
        False,
    ),
    # 4: mapper-level SQL
    "column_property on a non-tenant model (raw), entity": (
        lambda e: select(_OtherCP),
        True,
    ),
    "column_property on a non-tenant model (raw), column": (
        lambda e: select(_OtherCP.id, _OtherCP.peer),
        True,
    ),
    "deferred + undefer on a non-tenant model": (
        lambda e: select(_OtherDeferred).options(undefer(_OtherDeferred.peer)),
        True,
    ),
    "query_expression default on a non-tenant model": (
        lambda e: select(_OtherQuery),
        True,
    ),
    # 5: the same on a tenant model
    "column_property on a tenant model (raw)": (lambda e: select(_ItemCP), True),
    # 6: a relationship whose secondary is the tenant table
    "joinedload of a relationship with a tenant secondary": (
        lambda e: (
            select(_Other)
            .where(_Other.id == 1)
            .options(joinedload(_Other.leaves_via_items))
        ),
        True,
    ),
    # 7: a non-tenant polymorphic base with a tenant subclass
    "select of a non-tenant base with a tenant subclass": (
        lambda e: select(_Animal),
        True,
    ),
    "with_polymorphic over the tenant subclass": (
        lambda e: select(with_polymorphic(_Animal, [_Cat])),
        True,
    ),
    # 8: the rows of a VALUES construct
    "values() holding a raw subquery": (
        lambda e: _values_over(select(func.max(ITEMS.c.secret)).scalar_subquery()),
        False,
    ),
    # 9: a loader criterion that branches on the mapper
    "with_loader_criteria branching per class": (_branching_criteria, True),
    # 11-13: SQL no clause walk reads
    "literal_column subquery": (
        lambda e: select(literal_column("(select max(secret) from tgcur_items)")),
        True,
    ),
    "custom @compiles executable": (lambda e: _RawExecutable(), True),
    "text().columns()": (
        lambda e: text("select secret from tgcur_items").columns(column("secret")),
        True,
    ),
    # 14: a TenantScoped class mapped imperatively
    "an imperatively mapped tenant table": (lambda e: select(IMP.c.name), True),
    # closure round: SQL the recorder did not compile, or a scan that quoting fools
    "DDL() with an UPDATE": (
        lambda e: DDL("UPDATE tgcur_items SET name = 'pwned'"),
        False,
    ),
    "DDL() with a DELETE of a quoted name": (
        lambda e: DDL('DELETE FROM "tgcur_items"'),
        False,
    ),
    **{
        f"literal_column hiding the table behind {name}": (
            lambda e, fragment=fragment: _hidden_in_quotes(fragment),
            False,
        )
        for name, fragment in _QUOTE_FRAGMENTS.items()
    },
}


#: what the control finds instead of tenant B's value: a plain select of a
#: non-tenant polymorphic base returns the tenant subclass' identity, not its
#: columns (a superset refusal: the hierarchy reaches a tenant table).
_CONTROL_NEEDLE = {
    "select of a non-tenant base with a tenant subclass": "'kind': 'cat'"
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_REFUSED))
async def test_a_statement_reaching_a_tenant_table_unscoped_is_refused_bound(
    env: _Env, shape: str
) -> None:
    build, _ = _REFUSED[shape]
    with pytest.raises(UnsafeTenantOperationError):
        await _bound(env, build(env))
    await _assert_b_untouched(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_REFUSED))
async def test_a_statement_reaching_a_tenant_table_unscoped_fails_closed_unbound(
    env: _Env, shape: str
) -> None:
    build, _ = _REFUSED[shape]
    with pytest.raises(MissingTenantError):
        await _unbound(env, build(env))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape", [name for name, (_, runs) in _REFUSED.items() if runs]
)
async def test_the_refused_statement_really_reaches_tenant_bs_row(
    env: _Env, shape: str
) -> None:
    """Control: inside ``unscoped()`` each refused statement runs and returns B's
    value, so the refusal above is the guard's, not a broken statement."""
    build, _ = _REFUSED[shape]
    rows = await _unscoped(env, build(env))
    # tenant B's rows all start with "b-"; a non-tenant base only names the subclass
    assert _CONTROL_NEEDLE.get(shape, "b-") in _show(rows)


@pytest.mark.asyncio
async def test_a_tenant_class_mapped_imperatively_after_the_caches_were_built_is_seen(
    env: _Env,
) -> None:
    """rt342-a #14: the tenant table set was built before the class was mapped
    and never learned the table (``__init_subclass__`` runs before mapping)."""
    assert tenant_mod._tenant_names()  # the memo exists before the mapping below
    late = Table(
        "tgcur_late_imp",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Uuid, nullable=False),
        Column("name", Text),
    )

    class _LateImp(TenantScoped):
        pass

    reg = registry()
    reg.map_imperatively(_LateImp, late)
    try:
        with unscoped():  # schema DDL names the table: system work (ADR-0018)
            async with env.engine.begin() as conn:
                await conn.run_sync(late.create)
        with pytest.raises(UnsafeTenantOperationError):
            await _bound(env, select(late.c.name))
        with pytest.raises(MissingTenantError):
            await _unbound(env, select(late.c.name))
    finally:
        with unscoped():
            async with env.engine.begin() as conn:
                await conn.run_sync(late.drop)
        reg.dispose()


@pytest.mark.asyncio
async def test_a_deferred_column_load_on_a_non_tenant_model_is_refused(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            obj = (await session.scalars(select(_OtherDeferred))).one()
            with pytest.raises(UnsafeTenantOperationError):
                await session.refresh(obj, ["peer"])


@pytest.mark.asyncio
async def test_a_polymorphic_subclass_column_load_is_refused(env: _Env) -> None:
    """Reading a tenant subclass column through its non-tenant base."""
    async with env.maker() as session:
        with unscoped():
            animals = (await session.scalars(select(_Animal))).all()
        cat = next(a for a in animals if getattr(a, "id", None) == 2)
        session.expire(cat)
        with bind_tenant(env.a):
            with pytest.raises(UnsafeTenantOperationError):
                await session.refresh(cat, ["secret"])


# ------------------------------------------------ the criteria parameter: A #1 ---
def _param_select(e: _Env) -> Any:
    return select(_Item.name)


def _param_update(e: _Env) -> Any:
    return update(_Item).values(name="pwned")


def _param_delete(e: _Env) -> Any:
    return delete(_Item)


def _param_insert_copy(e: _Env) -> Any:
    return insert(_Leaf).from_select(["id", "name"], select(_Item.id + 100, _Item.name))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [_param_select, _param_update, _param_delete, _param_insert_copy],
    ids=["select", "update", "delete", "insert-from-select"],
)
@pytest.mark.parametrize("form", ["dict", "executemany"])
async def test_execution_parameters_cannot_override_the_tenant_bind(
    env: _Env, build: Callable[[_Env], Any], form: str
) -> None:
    forged = {TENANT_CRITERIA_PARAM: env.b}
    with pytest.raises(UnsafeTenantOperationError, match=TENANT_CRITERIA_PARAM):
        await _bound(env, build(env), forged if form == "dict" else [forged])
    await _assert_b_untouched(env)
    leaves = await _unscoped(env, select(func.count()).select_from(_Leaf))
    assert leaves[0][0] == 2


@pytest.mark.asyncio
async def test_execution_parameters_cannot_override_the_tenant_bind_unbound(
    env: _Env,
) -> None:
    with pytest.raises(MissingTenantError):
        await _unbound(env, select(_Item.name).params({TENANT_CRITERIA_PARAM: env.b}))


@pytest.mark.asyncio
async def test_a_stream_cannot_override_the_tenant_bind(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            with pytest.raises(UnsafeTenantOperationError, match=TENANT_CRITERIA_PARAM):
                await session.stream(select(_Item.name), {TENANT_CRITERIA_PARAM: env.b})


@pytest.mark.asyncio
async def test_the_cursor_refuses_a_criterion_value_a_later_listener_swapped_in(
    env: _Env,
) -> None:
    """A ``do_orm_execute`` listener registered after the guard may rewrite the
    parameters it checked: the cursor still compares the bind to the bound tenant."""

    def forge(state: Any) -> None:
        if state.is_select and not state.is_column_load:
            state.parameters = {TENANT_CRITERIA_PARAM: env.b}

    event.listen(Session, "do_orm_execute", forge)
    try:
        # one entity, then a join of two: both share the one criteria bind
        with pytest.raises(UnsafeTenantOperationError, match="criterion"):
            await _bound(env, select(_Item.name))
        joined = select(_Item.name, _Imp.name).join(_Imp, _Imp.id == _Item.id)  # type: ignore[attr-defined]
        with pytest.raises(UnsafeTenantOperationError, match="criterion"):
            await _bound(env, joined)
    finally:
        event.remove(Session, "do_orm_execute", forge)


# ----------------------------------------------------- stays allowed (no FP) ---
@pytest.mark.asyncio
async def test_orm_statements_over_tenant_models_stay_scoped_and_allowed(
    env: _Env,
) -> None:
    assert [r.name for r in await _bound(env, select(_Item.name))] == ["a-name"]
    assert [
        r[0] for r in await _bound(env, select(func.count()).select_from(_Item))
    ] == [1]
    joined = select(_Item.name, _Imp.name).join(_Imp, _Imp.id == _Item.id)  # type: ignore[attr-defined]
    assert [tuple(r) for r in await _bound(env, joined)] == [("a-name", "a-secret")]
    parents = await _bound(env, select(_Parent).options(joinedload(_Parent.item)))
    assert [row[0].item for row in parents] == [None]  # B's item is out of scope
    scoped_peer = await _bound(env, select(_OtherCPOrm.id, _OtherCPOrm.peer))
    assert scoped_peer[0].peer == "a-secret"  # the ORM subquery got the criterion
    await _bound(env, update(_Item).values(name="renamed").where(_Item.id == A_ITEM_ID))
    names = {r.id: r.name for r in await _unscoped(env, select(_Item.id, _Item.name))}
    assert names == {A_ITEM_ID: "renamed", B_ITEM_ID: "b-name"}


@pytest.mark.asyncio
async def test_statements_that_name_no_tenant_table_stay_allowed(env: _Env) -> None:
    assert [
        r.name for r in await _bound(env, select(_Other.name).order_by(_Other.id))
    ] == [
        "o1",
        "b-secret",
    ]
    async with env.maker() as session:
        with bind_tenant(env.a):
            connection = await session.connection()
            rows = (await connection.execute(select(_Leaf.name))).all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_session_text_statement_stays_the_callers_responsibility(
    env: _Env,
) -> None:
    """``session.execute(text(...))`` is the documented raw-SQL escape: the
    caller scopes it (``scoped()`` / the ``unsafe-tenant-sql`` check / RLS)."""
    rows = await _bound(env, text("select count(*) from tgcur_items"))
    assert rows[0][0] == 2


@pytest.mark.asyncio
async def test_a_connection_text_statement_naming_a_tenant_table_is_refused(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            connection = await session.connection()
            with pytest.raises(UnsafeTenantOperationError):
                await connection.execute(text("select secret from tgcur_items"))
            with pytest.raises(UnsafeTenantOperationError):
                await connection.execute(select(ITEMS.c.secret))
        with unscoped():
            connection = await session.connection()
            rows = (await connection.execute(select(ITEMS.c.secret))).all()
    assert {r[0] for r in rows} == {"a-secret", "b-secret"}


@pytest.mark.asyncio
async def test_session_connection_cannot_read_a_tenant_table_unbound(
    env: _Env,
) -> None:
    async with env.maker() as session:
        connection = await session.connection()
        with pytest.raises(MissingTenantError):
            await connection.execute(select(ITEMS.c.secret))


# ----------------------------- the session paths: rt342-b B1 - B10 and FP3 ---
async def _bulk(
    env: _Env, call: Callable[[Session], None], *, tenant: bool = True
) -> None:
    async with env.maker() as session:
        if tenant:
            with bind_tenant(env.a):
                await session.run_sync(call)
                await session.commit()
        else:
            await session.run_sync(call)
            await session.commit()


def _bulk_insert_sink(sync: Session) -> None:
    sync.bulk_insert_mappings(_RawSink, [{"id": 7}])


def _bulk_save_sink(sync: Session) -> None:
    sync.bulk_save_objects([_RawSink(id=7)])


def _bulk_update_sink(sync: Session) -> None:
    sync.bulk_update_mappings(_RawSink, [{"id": 1, "note": "x"}])


def _private_bulk_update_item(sync: Session) -> None:
    sync._bulk_save_mappings(  # type: ignore[attr-defined]
        inspect(_Item),
        [{"id": B_ITEM_ID, "name": "pwned"}],
        isupdate=True,
        isstates=False,
        return_defaults=False,
        update_changed_only=False,
        render_nulls=False,
    )


def _unwrapped_bulk_update_item(sync: Session) -> None:
    tenant_mod._original_legacy_bulk["bulk_update_mappings"](
        sync, _Item, [{"id": B_ITEM_ID, "name": "pwned"}]
    )


def _bulk_update_shadow(sync: Session) -> None:
    sync.bulk_update_mappings(_Shadow, [{"id": B_ITEM_ID, "name": "pwned"}])


def _bulk_insert_shadow(sync: Session) -> None:
    sync.bulk_insert_mappings(
        _Shadow, [{"id": 9, "name": "planted", "tenant_id": uuid.UUID(int=2)}]
    )


_BULK = {
    "bulk_insert_mappings, a raw default reading the tenant table": _bulk_insert_sink,
    "bulk_save_objects, a raw default reading the tenant table": _bulk_save_sink,
    "bulk_update_mappings, a raw onupdate reading the tenant table": _bulk_update_sink,
    "the private _bulk_save_mappings": _private_bulk_update_item,
    "the unwrapped bulk_update_mappings": _unwrapped_bulk_update_item,
    "bulk_update_mappings through a shadow Table": _bulk_update_shadow,
    "bulk_insert_mappings through a shadow Table": _bulk_insert_shadow,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_BULK))
async def test_a_bulk_path_that_reaches_a_tenant_table_is_refused(
    env: _Env, shape: str
) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _bulk(env, _BULK[shape])
    await _assert_b_untouched(env)
    sinks = await _unscoped(env, select(func.count()).select_from(_RawSink))
    assert sinks[0][0] == 0


@pytest.mark.asyncio
async def test_a_flush_through_a_shadow_mapper_cannot_plant_a_row(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            planted = _Shadow()
            planted.id = 9  # type: ignore[attr-defined]
            planted.name = "planted"  # type: ignore[attr-defined]
            planted.tenant_id = env.b  # type: ignore[attr-defined]
            session.add(planted)
            with pytest.raises(UnsafeTenantOperationError):
                await session.commit()
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_shadow_table_cannot_be_read_through_the_session(env: _Env) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _bound(env, select(_SHADOW_TABLE.c.secret))


@pytest.mark.asyncio
async def test_a_bulk_path_on_a_non_tenant_model_without_a_reading_default_runs(
    env: _Env,
) -> None:
    def bulk_insert_other(sync: Session) -> None:
        sync.bulk_insert_mappings(_Leaf, [{"id": 9, "name": "plain"}])

    await _bulk(env, bulk_insert_other)
    rows = await _unscoped(env, select(_Leaf.name).where(_Leaf.id == 9))
    assert [r.name for r in rows] == ["plain"]


def _forge_update(sync: Session, env: _Env) -> None:
    forged = _Item(id=B_ITEM_ID, tenant_id=env.a, name="x")
    make_transient_to_detached(forged)
    sync.add(forged)
    forged.name = "forged"


def _forge_delete(sync: Session, env: _Env) -> None:
    forged = _Item(id=B_ITEM_ID, tenant_id=env.a, name="x")
    make_transient_to_detached(forged)
    sync.add(forged)
    sync.delete(forged)


def _forge_merge_without_load(sync: Session, env: _Env) -> None:
    forged = _Item(id=B_ITEM_ID, tenant_id=env.a, name="x")
    make_transient_to_detached(forged)
    sync.merge(forged, load=False).name = "merged"


_FORGED_WRITES = {
    "UPDATE of a forged detached instance": _forge_update,
    "DELETE of a forged detached instance": _forge_delete,
    "merge(load=False) then UPDATE": _forge_merge_without_load,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_FORGED_WRITES))
async def test_a_forged_identity_cannot_write_another_tenants_row(
    env: _Env, shape: str
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            await session.run_sync(lambda sync: _FORGED_WRITES[shape](sync, env))
            with pytest.raises(
                UnsafeTenantOperationError, match="read from the database"
            ):
                await session.commit()
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_forged_identity_cannot_read_another_tenants_row(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            forged = _Item(id=B_ITEM_ID)
            forged.tenant_id = env.a
            make_transient_to_detached(forged)  # the unloaded attributes are expired
            session.add(forged)
            with pytest.raises(
                UnsafeTenantOperationError, match="read from the database"
            ):
                await session.refresh(forged, ["secret"])
            with pytest.raises(
                UnsafeTenantOperationError, match="read from the database"
            ):
                await session.refresh(forged)


@pytest.mark.asyncio
async def test_an_instance_loaded_under_the_bound_tenant_flushes_and_refreshes(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            item = (await session.scalars(select(_Item))).one()
            item.name = "renamed"
            await session.flush()
            await session.refresh(item, ["secret"])
            assert item.secret == "a-secret"
            session.expire(item)
            # an expired attribute loads again (a column load of a stamped instance)
            assert await session.run_sync(lambda _sync: item.name) == "renamed"
            await session.delete(item)
            await session.commit()
    rows = await _unscoped(env, select(_Item.id))
    assert [r.id for r in rows] == [B_ITEM_ID]


@pytest.mark.asyncio
async def test_a_new_instance_inserts_updates_and_refreshes_in_one_session(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            created = _Item(id=50, name="created", secret="s")
            session.add(created)
            await session.commit()
            created.name = "changed"
            await session.commit()
            await session.refresh(created)
            assert (created.tenant_id, created.name) == (env.a, "changed")
            await session.delete(created)
            await session.commit()


@pytest.mark.asyncio
async def test_an_instance_read_under_unscoped_flushes_under_its_own_tenant(
    env: _Env,
) -> None:
    """The stamp is the tenant_id the database returned: trusted system code that
    loads an instance under ``unscoped()`` may flush it under that tenant."""
    async with env.maker() as session:
        with unscoped():
            item = (
                await session.scalars(select(_Item).where(_Item.id == A_ITEM_ID))
            ).one()
        with bind_tenant(env.a):
            item.name = "system-set"
            await session.commit()
        with bind_tenant(env.b):
            item.name = "another-tenant"
            with pytest.raises(TenantMismatchError):
                await session.commit()


@pytest.mark.asyncio
async def test_a_tenant_joined_inheritance_subclass_inserts_updates_and_deletes(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            cat = _Cat(id=60, secret="purr")
            session.add(cat)
            await session.commit()
            cat.secret = "meow"
            await session.commit()
            await session.delete(cat)
            await session.commit()


@pytest.mark.asyncio
async def test_a_custom_compiled_default_that_reads_no_table_flushes(env: _Env) -> None:
    """A ``@compiles`` element renders SQL no ``visit_*`` hook sees, so the cursor
    reads the fragment it rendered. The statement's own target table is not part
    of that fragment: an INSERT, an UPDATE (``onupdate``) and a DELETE of a row
    whose default is such an element are flush writes like any other
    (projecthub's ``PATCH`` with ``updated_at = clock_timestamp()``)."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            stamped = _Stamped(id=1, name="n")
            session.add(stamped)
            await session.commit()
            stamped.name = "m"
            await session.commit()
            await session.refresh(stamped)
            assert (stamped.name, stamped.touched) == ("m", "ticked")
            await session.delete(stamped)
            await session.commit()
    rows = await _unscoped(env, select(func.count()).select_from(_Stamped))
    assert rows[0][0] == 0


# B5 - B7, FP3: a rendered default that reads a tenant table, at the flush cursor
_SINK_MODELS: dict[str, Any] = {
    "a raw SQL-expression default": _RawSink,
    "a text() default": _TextSink,
    "a literal_column() default": _LiteralSink,
    "a Python default running Core on the flush connection": _CtxSink,
    "a custom @compiles default": _CustomSink,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_SINK_MODELS))
async def test_a_flush_whose_default_reads_a_tenant_table_is_refused(
    env: _Env, kind: str
) -> None:
    model = _SINK_MODELS[kind]
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(model(id=7))
            with pytest.raises((UnsafeTenantOperationError, StatementError)) as raised:
                await session.commit()
            assert _is_refusal(raised.value)
    sinks = await _unscoped(env, select(func.count()).select_from(model))
    assert sinks[0][0] == 0
    # inside unscoped() the default is real: it copies tenant B's value
    async with env.maker() as session:
        with unscoped():
            session.add(model(id=7))
            await session.commit()
    stolen = await _unscoped(env, select(model.stolen).where(model.id == 7))
    assert stolen[0][0] == "b-secret"


@pytest.mark.asyncio
async def test_a_core_insert_with_a_text_default_reading_a_tenant_table_is_refused(
    env: _Env,
) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _bound(env, insert(_TextSink).values(id=7))
    sinks = await _unscoped(env, select(func.count()).select_from(_TextSink))
    assert sinks[0][0] == 0


@pytest.mark.asyncio
async def test_an_onupdate_that_reads_the_tenant_table_through_core_is_refused(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with unscoped():
            session.add(_CtxSink(id=7, stolen="x"))
            await session.commit()
    async with env.maker() as session:
        with bind_tenant(env.a):
            sink = (await session.scalars(select(_CtxSink))).one()
            sink.stolen = "y"
            with pytest.raises((UnsafeTenantOperationError, StatementError)) as raised:
                await session.commit()
            assert _is_refusal(raised.value)


@pytest.mark.asyncio
async def test_a_secondary_table_default_reading_a_tenant_table_is_refused(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_Left(id=1, rights=[_Right(id=1)]))
            with pytest.raises(UnsafeTenantOperationError):
                await session.commit()
    rows = await _unscoped(env, select(func.count()).select_from(_ASSOC))
    assert rows[0][0] == 0


@pytest.mark.asyncio
async def test_a_default_set_in_python_is_not_refused(env: _Env) -> None:
    """The refusal judges the column the INSERT renders: a value set in Python
    leaves the reading default unrendered (the message says to set it there)."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_RawSink(id=7, stolen="mine"))
            await session.commit()
    stolen = await _unscoped(env, select(_RawSink.stolen).where(_RawSink.id == 7))
    assert stolen[0][0] == "mine"


# B8: identity-map residue of an unscoped() load
@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.LegacyAPIWarning")
async def test_query_get_does_not_return_another_tenants_loaded_instance(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with unscoped():
            held = await session.get(_Item, B_ITEM_ID)
        assert held is not None
        with bind_tenant(env.a):
            assert await session.get(_Item, B_ITEM_ID) is None
            got = await session.run_sync(
                lambda sync: sync.query(_Item).get(B_ITEM_ID)  # legacy Query.get
            )
            assert got is None


@pytest.mark.asyncio
async def test_a_many_to_one_lazy_load_does_not_return_another_tenants_instance(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with unscoped():
            held = await session.get(_Item, B_ITEM_ID)
        assert held is not None
        with bind_tenant(env.a):
            parent = await session.get(_Parent, 1)
            assert parent is not None
            loaded = await session.run_sync(lambda sync: parent.item)
            assert loaded is None


@pytest.mark.asyncio
async def test_an_identity_lookup_with_no_tenant_bound_fails_closed(env: _Env) -> None:
    async with env.maker() as session:
        with unscoped():
            held = await session.get(_Item, B_ITEM_ID)
        assert held is not None
        with pytest.raises(MissingTenantError):
            await session.get(_Item, B_ITEM_ID)


@pytest.mark.asyncio
async def test_an_own_identity_lookup_is_served_from_the_map(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            first = await session.get(_Item, A_ITEM_ID)
            assert first is not None
            assert await session.get(_Item, A_ITEM_ID) is first


# B10: a before_flush listener registered after the guard's own
@pytest.mark.asyncio
async def test_a_later_before_flush_listener_cannot_plant_another_tenants_row(
    env: _Env,
) -> None:
    def plant(session: Session, _context: Any, _instances: Any) -> None:
        if not session.info.get("planted"):
            session.info["planted"] = True
            session.add(_Item(id=70, tenant_id=env.b, name="late", secret="z"))

    async with env.maker() as session:
        event.listen(session.sync_session, "before_flush", plant)
        with bind_tenant(env.a):
            session.add(_Parent(id=9))
            with pytest.raises((TenantMismatchError, UnsafeTenantOperationError)):
                await session.commit()
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_late_listener_cannot_rewrite_the_tenant_of_a_flushed_insert(
    env: _Env,
) -> None:
    def retarget(_mapper: Any, _connection: Any, target: Any) -> None:
        target.tenant_id = env.b

    event.listen(_Item, "before_insert", retarget)
    try:
        async with env.maker() as session:
            with bind_tenant(env.a):
                session.add(_Item(id=71, name="mine"))
                with pytest.raises((TenantMismatchError, UnsafeTenantOperationError)):
                    await session.commit()
    finally:
        event.remove(_Item, "before_insert", retarget)
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_merge_of_another_tenants_row_still_writes_nothing(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.sync_session.info.clear()
            await session.merge(_Item(id=B_ITEM_ID, tenant_id=env.a, name="m"))
            with pytest.raises(IntegrityError):
                await session.flush()
    await _assert_b_untouched(env)


# ------------------------------------ the test harness's own reset statements ---
async def _reset_that_fails(*args: Any, **kwargs: Any) -> None:
    raise DBAPIError("reset", None, RuntimeError("boom"))


#: the reset path -> what to break so the harness takes it
_RESET_PATHS = ("dirty tables", "a table over the row limit", "all tables", "rebuild")


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("path", _RESET_PATHS)
async def test_the_harness_schema_reset_runs_over_tenant_tables(
    env: _Env, monkeypatch: pytest.MonkeyPatch, path: str, bound: bool
) -> None:
    """``reset_schema`` probes, ``DELETE``s, ``TRUNCATE``s and rebuilds every table of
    a metadata, the tenant-owned ones included, with a tenant bound or not: the
    harness's own system work, so the cursor rule does not refuse it. (Without
    ``unscoped()`` around it every test after the first failed in any app with a
    ``TenantScoped`` model.)"""
    metadata = _Base.metadata
    await harness.reset_schema(env.engine, metadata)  # builds; later ones empty
    async with env.maker() as session:
        with unscoped():
            session.add(_Item(id=9, tenant_id=env.a, name="x"))
            await session.commit()
    if path == "a table over the row limit":
        monkeypatch.setattr(harness, "DELETE_ROW_LIMIT", 0)  # TRUNCATE on PostgreSQL
    if path in ("all tables", "rebuild"):
        monkeypatch.setattr(harness, "_truncate_dirty", _reset_that_fails)
    if path == "rebuild":
        monkeypatch.setattr(harness, "_truncate_all", _reset_that_fails)
    with bind_tenant(env.a) if bound else nullcontext():
        await harness.reset_schema(env.engine, metadata)
    rows = await _unscoped(env, select(func.count()).select_from(_Item))
    assert rows[0][0] == 0


# ============================================ closure round (rt342-closure) ===
# Each test below is one probe of the closure report that bypassed the first
# round of the cursor rule, run on SQLite and PostgreSQL; a control inside
# ``unscoped()`` shows the refused statement really does the damage.
async def _b_damaged(env: _Env) -> bool:
    """Tenant B's item was deleted, renamed or retargeted, or a row was planted."""
    rows = await _unscoped(env, select(ITEMS.c.id, ITEMS.c.name, ITEMS.c.tenant_id))
    by_id = {row.id: row for row in rows}
    return (
        B_ITEM_ID not in by_id
        or by_id[B_ITEM_ID].name != "b-name"
        or by_id[B_ITEM_ID].tenant_id != env.b
        or _PLANTED_ID in by_id
    )


# M1: SQL compiled by something other than the recorder is read whole
@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_a_ddl_element_through_a_connection_is_refused(
    env: _Env, bound: bool
) -> None:
    async with env.engine.connect() as connection:
        with bind_tenant(env.a) if bound else nullcontext():
            with pytest.raises(
                UnsafeTenantOperationError if bound else MissingTenantError
            ):
                await connection.execute(DDL("DELETE FROM tgcur_items"))
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_ddl_element_naming_a_tenant_table_really_rewrites_it_unscoped(
    env: _Env,
) -> None:
    await _unscoped(env, DDL("UPDATE tgcur_items SET name = 'pwned'"))
    assert (await _b_item(env)).name == "pwned"


@pytest.mark.asyncio
async def test_a_ddl_element_naming_no_tenant_table_stays_allowed(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            await session.execute(DDL("SELECT 1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_creating_a_tenant_table_outside_unscoped_is_refused(
    env: _Env, bound: bool
) -> None:
    async with env.engine.connect() as connection:
        with bind_tenant(env.a) if bound else nullcontext():
            with pytest.raises(
                UnsafeTenantOperationError if bound else MissingTenantError
            ):
                await connection.run_sync(
                    lambda sync: ITEMS.create(sync, checkfirst=False)
                )
        # control: unscoped, the statement is real (the table exists already)
        with unscoped(), pytest.raises(DBAPIError):
            await connection.run_sync(lambda sync: ITEMS.create(sync, checkfirst=False))


@pytest.mark.asyncio
async def test_the_schema_build_from_the_models_runs_with_no_tenant_bound(
    env: _Env,
) -> None:
    """``ensure_model_schema`` builds every tenant table at startup, where no
    tenant is bound: its ``create_all`` is system work."""
    with unscoped():
        async with env.engine.begin() as connection:
            await connection.run_sync(_Base.metadata.drop_all)
    drift = await model_schema.ensure_model_schema(
        env.engine, _Base.metadata, where="the guard test database"
    )
    assert drift == []
    rows = await _unscoped(env, select(func.count()).select_from(_Item))
    assert rows[0][0] == 0


# M1: a compiled statement from a compiler that is no recorder (a dialect a driver
# built without it, an extension's compiler) is read whole, whatever it is
@pytest.mark.parametrize("renders", ["a tenant table", "no tenant table"])
def test_a_statement_compiled_without_the_recorder_is_scanned_whole(
    renders: str,
) -> None:
    statement = select(ITEMS.c.secret if renders == "a tenant table" else _Leaf.name)
    compiled = statement.compile(dialect=sqlite_dialect.dialect())
    assert not isinstance(compiled, tenant_mod.ReachRecorder)  # the setup is real
    if renders == "no tenant table":
        with bind_tenant(uuid.uuid4()):
            tenant_mod.check_tenant_reach(None, compiled)
        return
    with bind_tenant(uuid.uuid4()):
        with pytest.raises(UnsafeTenantOperationError):
            tenant_mod.check_tenant_reach(None, compiled)
    with pytest.raises(MissingTenantError):
        tenant_mod.check_tenant_reach(None, compiled)
    with unscoped():
        tenant_mod.check_tenant_reach(None, compiled)


# M4: PostgreSQL quoting that hides the table name from a scan that skips quotes
@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_QUOTE_FRAGMENTS))
async def test_a_connection_text_hiding_the_table_behind_quoting_is_refused(
    env: _Env, name: str
) -> None:
    async with env.engine.connect() as connection:
        with pytest.raises(MissingTenantError):
            await connection.execute(text("SELECT " + _QUOTE_FRAGMENTS[name]))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_QUOTE_FRAGMENTS))
async def test_the_quoting_shapes_really_reach_tenant_bs_row(
    env: _Env, name: str
) -> None:
    """Control: on PostgreSQL the shapes read B's value inside ``unscoped()``;
    SQLite reads none of that quoting, so there the database itself rejects them
    (the refusals above are the guard's on both)."""
    statement = _hidden_in_quotes(_QUOTE_FRAGMENTS[name])
    if env.engine.dialect.name == "postgresql":
        assert "b-secret" in repr(await _unscoped(env, statement))
    else:
        with pytest.raises(DBAPIError):
            await _unscoped(env, statement)


# M3: a child loader statement gets no approval of its own from its parent
_CHILD_LOADERS = {
    "selectinload": selectinload,
    "immediateload": immediateload,
    "subqueryload": subqueryload,
}


def _holders_of_tenant_items(loader: str) -> Any:
    return (
        select(_Holder)
        .join(_Item, _Item.id == _Holder.item_id)
        .options(_CHILD_LOADERS[loader](_Holder.cp))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", list(_CHILD_LOADERS))
async def test_a_child_loader_statement_does_not_inherit_the_parents_approval(
    env: _Env, loader: str
) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _bound(env, _holders_of_tenant_items(loader))
    await _assert_b_untouched(env)
    # controls: the parent alone is allowed; unscoped, the child load reads B's value
    parents = await _bound(
        env, select(_Holder).join(_Item, _Item.id == _Holder.item_id)
    )
    assert [row[0].id for row in parents] == [1]
    loaded = await _unscoped(env, _holders_of_tenant_items(loader))
    assert loaded[0][0].cp.peer == "b-secret"


# M2: the flush window admits the rows the flush verified, nothing else it runs
def _rewrite_every_item(_mapper: Any, connection: Any, _target: Any) -> None:
    connection.execute(update(ITEMS).values(name="pwned-by-a-listener"))


def _update_b_item_by_primary_key(_mapper: Any, connection: Any, _target: Any) -> None:
    connection.execute(
        update(ITEMS).where(ITEMS.c.id == B_ITEM_ID).values(name="pwned-by-pk")
    )


def _update_b_item_by_a_named_key(_mapper: Any, connection: Any, _target: Any) -> None:
    """The shape the persistence layer itself builds: ``pk = :pk`` with a named
    parameter, naming a row no verified mapper event of this flush covers."""
    connection.execute(
        update(ITEMS).where(ITEMS.c.id == bindparam("pk")).values(name="pwned-by-key"),
        {"pk": B_ITEM_ID},
    )


_FLUSH_MODELS: dict[str, Any] = {
    "a Python default deleting B's row": _Wiper,
    "a Python default inserting a row for B": _Planter,
}
_FLUSH_LISTENERS: dict[str, Callable[[Any, Any, Any], None]] = {
    "an after_insert listener updating every row": _rewrite_every_item,
    "an after_insert listener updating B's row by primary key": (
        _update_b_item_by_primary_key
    ),
    "an after_insert listener updating B's row by a named primary-key parameter": (
        _update_b_item_by_a_named_key
    ),
}


async def _flush_beside_a_writer(env: _Env, kind: str, scope: Any) -> None:
    """Flush a new row of tenant A together with the writer ``kind`` names."""
    listener = _FLUSH_LISTENERS.get(kind)
    if listener is not None:
        event.listen(_Item, "after_insert", listener)
    try:
        async with env.maker() as session:
            with scope:
                session.add(_Item(id=3, tenant_id=env.a, name="mine"))
                if listener is None:
                    session.add(_FLUSH_MODELS[kind](id=1))
                await session.commit()
    finally:
        if listener is not None:
            event.remove(_Item, "after_insert", listener)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [*_FLUSH_MODELS, *_FLUSH_LISTENERS])
async def test_the_flush_window_admits_only_the_rows_the_flush_verified(
    env: _Env, kind: str
) -> None:
    _PLANT_FOR[:] = [env.b]
    with pytest.raises((UnsafeTenantOperationError, StatementError)) as raised:
        await _flush_beside_a_writer(env, kind, bind_tenant(env.a))
    assert _is_refusal(raised.value)
    assert not await _b_damaged(env)
    ids = {row.id for row in await _unscoped(env, select(ITEMS.c.id))}
    assert ids == {A_ITEM_ID, B_ITEM_ID}  # the whole flush went with the refusal
    # control: inside unscoped() the same writer does its damage (the parent row
    # that references B's item goes first: PostgreSQL enforces the foreign key)
    await _unscoped(env, delete(_Parent))
    await _flush_beside_a_writer(env, kind, unscoped())
    assert await _b_damaged(env)


# (b): a do_orm_execute listener registered after the guard replaces its statement
@pytest.mark.asyncio
async def test_a_later_listener_replacing_the_guarded_statement_is_refused(
    env: _Env,
) -> None:
    def swap(state: Any) -> None:
        if state.is_select and not state.is_column_load:
            state.statement = select(_Item.name)

    assert [r.name for r in await _bound(env, select(_Item.name))] == ["a-name"]
    event.listen(Session, "do_orm_execute", swap)
    try:
        with pytest.raises(UnsafeTenantOperationError):
            await _bound(env, select(_Item.name))
    finally:
        event.remove(Session, "do_orm_execute", swap)


@pytest.mark.asyncio
async def test_a_criterion_approval_needs_the_criterion_parameter(env: _Env) -> None:
    """The cursor re-reads the parameter of an approved criterion statement: one
    that holds no tenant criterion (it lost it after the guard looked) is refused."""
    statement = select(ITEMS.c.secret)
    tenant_mod._approve(statement, tenant_mod._CRITERION)
    async with env.engine.connect() as connection:
        with bind_tenant(env.a):
            with pytest.raises(UnsafeTenantOperationError, match="criterion"):
                await connection.execute(statement)


@pytest.mark.asyncio
async def test_nothing_a_listener_can_read_back_admits_a_statement(env: _Env) -> None:
    """A module's ``do_orm_execute`` listener sees every execution option of a
    guarded statement; replaying them on its own Core statement admits nothing."""
    seen: dict[str, Any] = {}

    def spy(state: Any) -> None:
        seen.update(state.execution_options)
        seen.update(state.local_execution_options)

    event.listen(Session, "do_orm_execute", spy)
    try:
        await _bound(env, select(_Item.name))
    finally:
        event.remove(Session, "do_orm_execute", spy)
    async with env.engine.connect() as connection:
        with bind_tenant(env.a):
            with pytest.raises(UnsafeTenantOperationError):
                await connection.execute(select(ITEMS.c.secret), execution_options=seen)


# (c): a stamp does not outlive the INSERT that never happened or was rolled back
@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["update", "delete"])
async def test_a_failed_insert_leaves_no_stamp_to_reattach_with(
    env: _Env, write: str
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            ghost = _Item(id=B_ITEM_ID, tenant_id=env.a, name="x")  # B owns the id
            session.add(ghost)
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
            make_transient_to_detached(ghost)
            session.add(ghost)
            if write == "update":
                ghost.name = "pwned"
            else:
                await session.delete(ghost)
            with pytest.raises(UnsafeTenantOperationError):
                await session.commit()
    await _assert_b_untouched(env)


@pytest.mark.asyncio
async def test_a_rolled_back_insert_loses_its_stamp(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            row = _Item(id=3, name="mine")
            session.add(row)
            await session.flush()  # inserted, so stamped ...
            await session.rollback()  # ... and gone again
            make_transient_to_detached(row)
            session.add(row)
            row.name = "renamed"
            with pytest.raises(UnsafeTenantOperationError):
                await session.commit()
    await _assert_b_untouched(env)


# A #10: a verdict memoized under a cache key that does not fix the SQL. The
# closure variable of an untracked lambda is not part of the key, so two
# statements with one key can render different SQL: a "safe" verdict of the first
# would bless a raw tenant subquery in the second. SQLAlchemy keeps a lambda per
# code object, so each variant below is a function of its own.
def _untracked_criteria(holder: list[Any]) -> Any:
    return select(_Leaf.id).options(
        with_loader_criteria(
            _Leaf, lambda cls: cls.name == holder[0], track_closure_variables=False
        )
    )


def _tracked_criteria(holder: list[Any]) -> Any:
    return select(_Leaf.id).options(
        with_loader_criteria(
            _Leaf, lambda cls: cls.name == holder[0], track_closure_variables=True
        )
    )


def test_a_statement_with_an_untracked_closure_gets_no_memoized_verdict() -> None:
    tenant_mod._invalidate_guard_caches()
    statement = _untracked_criteria([literal("zzz")])
    assert not tenant_mod._verdict_cacheable(statement)
    tenant_mod._unsafe_core_reference(statement)
    tenant_mod._references_tenant_scoped(statement)
    assert not tenant_mod._unsafe_core_cache
    assert not tenant_mod._reference_cache


def test_a_statement_with_a_tracked_closure_keeps_its_memoized_verdict() -> None:
    """Control: the same two checks do store a verdict where the key fixes the SQL."""
    tenant_mod._invalidate_guard_caches()
    statement = _tracked_criteria([literal("zzz")])
    assert tenant_mod._verdict_cacheable(statement)
    tenant_mod._unsafe_core_reference(statement)
    tenant_mod._references_tenant_scoped(statement)
    assert tenant_mod._unsafe_core_cache
    assert tenant_mod._reference_cache


def _untracked_lambda_statement(holder: list[Any]) -> Any:
    return lambda_stmt(
        lambda: select(_Leaf.id).where(_Leaf.name == holder[0]),
        track_closure_variables=False,
    )


def _tracked_lambda_statement(holder: list[Any]) -> Any:
    # two functions: the lambda cache is per code object, whatever the options
    return lambda_stmt(
        lambda: select(_Leaf.id).where(_Leaf.name == holder[0]),
        track_closure_variables=True,
    )


def test_an_untracked_lambda_statement_gets_no_memoized_verdict() -> None:
    """The statement itself is the lambda (not a loader option holding one)."""
    tenant_mod._invalidate_guard_caches()
    statement = _untracked_lambda_statement([literal("zzz")])
    assert not tenant_mod._verdict_cacheable(statement)
    tenant_mod._unsafe_core_reference(statement)
    tenant_mod._references_tenant_scoped(statement)
    assert not tenant_mod._unsafe_core_cache
    assert not tenant_mod._reference_cache


def test_a_tracked_lambda_statement_keeps_its_memoized_verdict() -> None:
    tenant_mod._invalidate_guard_caches()
    statement = _tracked_lambda_statement([literal("zzz")])
    assert tenant_mod._verdict_cacheable(statement)
    tenant_mod._unsafe_core_reference(statement)
    tenant_mod._references_tenant_scoped(statement)
    assert tenant_mod._unsafe_core_cache
    assert tenant_mod._reference_cache


def _untracked_raw_criteria(holder: list[Any]) -> Any:
    # A function of its own: SQLAlchemy's lambda cache is per code object, so a
    # lambda shared with the benign variant above would replay that variant's SQL.
    return select(_Leaf.id).options(
        with_loader_criteria(
            _Leaf, lambda cls: cls.name == holder[0], track_closure_variables=False
        )
    )


@pytest.mark.asyncio
async def test_an_untracked_closure_holding_a_raw_tenant_subquery_is_refused(
    env: _Env,
) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _bound(env, _untracked_raw_criteria([_B_SECRET_RAW]), compiled_cache=None)
    await _assert_b_untouched(env)


# Behaviours the docs name: a projection without tenant_id carries no stamp, and the
# identity lookup of an EXPIRED foreign instance raises where an unexpired one misses.
@pytest.mark.asyncio
async def test_a_deferred_load_needs_the_tenant_id_a_projection_left_out(
    env: _Env,
) -> None:
    def projection(*columns: Any) -> Any:
        return select(_Item).where(_Item.id == A_ITEM_ID).options(load_only(*columns))

    async with env.maker() as session:
        with bind_tenant(env.a):
            bare = (await session.execute(projection(_Item.name))).scalar_one()
            with pytest.raises(UnsafeTenantOperationError, match="column load"):
                await session.refresh(bare, ["secret"])
    async with env.maker() as session:  # the control: the column is in the projection
        with bind_tenant(env.a):
            kept = (
                await session.execute(projection(_Item.name, _Item.tenant_id))
            ).scalar_one()
            await session.refresh(kept, ["secret"])
            assert kept.secret == "a-secret"


@pytest.mark.asyncio
async def test_the_identity_lookup_of_an_expired_foreign_instance_is_refused(
    env: _Env,
) -> None:
    async with env.maker() as session:
        with unscoped():
            held = await session.get(_Item, B_ITEM_ID)
        assert held is not None
        with bind_tenant(env.a):
            assert await session.get(_Item, B_ITEM_ID) is None  # unexpired: a miss
            session.expire(held)
            with pytest.raises(UnsafeTenantOperationError, match="column load"):
                await session.get(_Item, B_ITEM_ID)  # expired: the refresh refuses
    await _assert_b_untouched(env)


# ================================ final round (closure 2: N1 and N2) =============
# N1: a flush write is admitted only when it is what the PERSISTENCE LAYER builds
# over a ``Table`` a mapper verified in this flush. A listener (or a context-aware
# Python default) runs Core on the flush connection while the window is open:
# whatever the statement BRINGS (its own Table object, the types of its
# parameters, values that name ``tenant_id``) must not be believed.
_Hook = Callable[[Any, Any, Any], None]


class _RewritesToB(TypeDecorator[int]):
    """Turns whatever a parameter holds into tenant B's item id on the way to the
    database: the raw value a guard reads is not the value the row is pinned by."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> int:
        return B_ITEM_ID


def _tenant_literal(tenant: uuid.UUID) -> Any:
    """A tenant id as a SQL literal no execution parameter carries."""
    return cast(literal_column(f"'{tenant.hex}'"), Uuid())


def _n1_second_table_update(_env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(_FAKE_KEY_ITEMS)
            .where(_FAKE_KEY_ITEMS.c.other_id == bindparam("p"))
            .values(name="pwned-by-a-second-table"),
            {"p": A_ITEM_ID},  # the verified id, but the ``other_id`` of B's row too
        )

    return hook


def _n1_second_table_delete(_env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            delete(_FAKE_KEY_ITEMS).where(_FAKE_KEY_ITEMS.c.other_id == bindparam("p")),
            {"p": A_ITEM_ID},
        )

    return hook


def _n1_second_table_insert(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        # a row that looks like tenant A's own, written through a Table no mapper owns
        connection.execute(
            insert(_FAKE_KEY_ITEMS),
            {"id": 99, "other_id": 1, "name": "second-table", "tenant_id": env.a},
        )

    return hook


def _n1_typed_pin(_env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(ITEMS)
            .where(ITEMS.c.id == bindparam("p", type_=_RewritesToB()))
            .values(name="pwned-by-a-typed-pin"),
            {"p": A_ITEM_ID},  # verified; the database receives B's id
        )

    return hook


def _n1_key_swapped_insert(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        # the key ``tenant_id`` is the column ``name``; the tenant column is ``tid``
        connection.execute(
            insert(_KEY_SWAPPED_ITEMS).values(
                id=98, tenant_id=env.a, tid=env.b, other_id=1
            )
        )

    return hook


def _n1_key_swapped_update(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(_KEY_SWAPPED_ITEMS)
            .where(_KEY_SWAPPED_ITEMS.c.id == bindparam("p"))
            .values(tid=env.b),
            {"p": A_ITEM_ID},
        )

    return hook


def _n1_bind_named_tenant_id(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        # the parameter NAMED tenant_id holds A; the column is set to B by a literal
        connection.execute(
            insert(ITEMS).values(
                id=97,
                tenant_id=_tenant_literal(env.b),
                name=bindparam("tenant_id", type_=Uuid()),
            ),
            {"tenant_id": env.a},
        )

    return hook


def _n1_bind_named_tenant_id_multi_values(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            insert(ITEMS).values(
                [
                    {
                        "id": 96,
                        "tenant_id": _tenant_literal(env.b),
                        "name": bindparam("tenant_id", type_=Uuid()),
                    }
                ]
            ),
            {"tenant_id": env.a},
        )

    return hook


def _n1_insert_from_select(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            insert(ITEMS).from_select(
                ["id", "name", "tenant_id"],
                select(
                    literal(95),
                    bindparam("tenant_id", env.a, type_=Uuid()),
                    _tenant_literal(env.b),
                ),
            )
        )

    return hook


def _n1_update_setting_tenant_id(env: _Env) -> _Hook:
    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(ITEMS)
            .where(ITEMS.c.id == bindparam("p"))
            .values(tenant_id=_tenant_literal(env.b)),
            {"p": A_ITEM_ID},
        )

    return hook


def _n1_executemany_with_a_foreign_key(_env: _Env) -> _Hook:
    """One verified key does not admit a second one riding in the same execution."""

    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(ITEMS).where(ITEMS.c.id == bindparam("p")).values(name="pwned-many"),
            [{"p": A_ITEM_ID}, {"p": B_ITEM_ID}],
        )

    return hook


_N1_SHAPES: dict[str, tuple[str, Callable[[_Env], _Hook]]] = {
    "UPDATE through a second Table of the same name keyed on other_id": (
        "after_update",
        _n1_second_table_update,
    ),
    "DELETE through a second Table of the same name keyed on other_id": (
        "after_update",
        _n1_second_table_delete,
    ),
    "INSERT through a second Table of the same name carrying the bound tenant": (
        "after_insert",
        _n1_second_table_insert,
    ),
    "UPDATE pinned by a parameter whose type rewrites the verified key": (
        "after_update",
        _n1_typed_pin,
    ),
    "INSERT through a Table whose column keys are swapped": (
        "after_insert",
        _n1_key_swapped_insert,
    ),
    "UPDATE through a Table whose column keys are swapped": (
        "after_update",
        _n1_key_swapped_update,
    ),
    "INSERT whose bind named tenant_id is not the tenant column": (
        "after_insert",
        _n1_bind_named_tenant_id,
    ),
    "multi-row VALUES whose bind named tenant_id is not the tenant column": (
        "after_insert",
        _n1_bind_named_tenant_id_multi_values,
    ),
    "INSERT ... SELECT whose bind named tenant_id feeds another column": (
        "after_insert",
        _n1_insert_from_select,
    ),
    "UPDATE of a verified row that sets tenant_id itself": (
        "after_update",
        _n1_update_setting_tenant_id,
    ),
    "executemany of a verified key and a foreign one": (
        "after_update",
        _n1_executemany_with_a_foreign_key,
    ),
}


async def _flush_with(env: _Env, trigger: str, hook: _Hook, scope: Any) -> None:
    """One legitimate flush of tenant A (``after_insert``: a new item 3,
    ``after_update``: a rename of item 1) with ``hook`` run on its connection."""
    event.listen(_Item, trigger, hook)
    try:
        async with env.maker() as session:
            with scope:
                if trigger == "after_insert":
                    session.add(_Item(id=3, tenant_id=env.a, name="mine"))
                else:
                    mine = await session.get(_Item, A_ITEM_ID)
                    assert mine is not None
                    mine.name = "a-touched"
                await session.commit()
    finally:
        event.remove(_Item, trigger, hook)


async def _item_rows(env: _Env) -> dict[int, tuple[str, uuid.UUID]]:
    rows = await _unscoped(env, select(ITEMS.c.id, ITEMS.c.name, ITEMS.c.tenant_id))
    return {row.id: (row.name, row.tenant_id) for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_N1_SHAPES))
async def test_a_flush_listener_cannot_write_through_what_its_statement_brings(
    env: _Env, kind: str
) -> None:
    trigger, build = _N1_SHAPES[kind]
    seed = await _item_rows(env)
    with pytest.raises((UnsafeTenantOperationError, StatementError)) as raised:
        await _flush_with(env, trigger, build(env), bind_tenant(env.a))
    assert _is_refusal(raised.value)
    assert await _item_rows(env) == seed  # the whole flush went with the refusal
    # control: inside unscoped() the same listener does its damage, so the refusal
    # above is the guard's (the parent row that references B's item goes first:
    # PostgreSQL enforces the foreign key)
    await _unscoped(env, delete(_Parent))
    await _flush_with(env, trigger, build(env), unscoped())
    flush_alone = dict(seed)
    if trigger == "after_insert":
        flush_alone[3] = ("mine", env.a)
    else:
        flush_alone[A_ITEM_ID] = ("a-touched", env.a)
    assert await _item_rows(env) != flush_alone


@pytest.mark.asyncio
async def test_a_listener_update_of_the_row_being_flushed_by_its_key_is_admitted(
    env: _Env,
) -> None:
    """The statement shape the persistence layer builds itself (a ``post_update``
    pins the row it just inserted by ``pk = :param``) still runs: the Table is the
    mapper's own and an untyped parameter takes the column's own type."""

    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(ITEMS).where(ITEMS.c.id == bindparam("pk")).values(name="renamed"),
            {"pk": 3},
        )

    await _flush_with(env, "after_insert", hook, bind_tenant(env.a))
    assert (await _item_rows(env))[3] == ("renamed", env.a)


@pytest.mark.asyncio
async def test_a_primary_key_change_of_the_tenants_own_row_flushes(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            mine = await session.get(_Item, A_ITEM_ID)
            assert mine is not None
            mine.id = 50
            await session.commit()
    rows = await _item_rows(env)
    assert rows[50] == ("a-name", env.a)
    assert rows[B_ITEM_ID] == ("b-name", env.b)


_SOME_TENANT = uuid.uuid4()


@pytest.mark.parametrize(
    ("label", "statement", "supplies"),
    [
        ("no values", insert(ITEMS), False),
        ("another column", insert(ITEMS).values(name="x"), False),
        ("the column by key", insert(ITEMS).values(tenant_id=_SOME_TENANT), True),
        (
            "the column by object",
            insert(ITEMS).values({ITEMS.c.tenant_id: _SOME_TENANT}),
            True,
        ),
        (
            "a column of another Table object",
            update(ITEMS).values({_FAKE_KEY_ITEMS.c.tenant_id: _SOME_TENANT}),
            True,
        ),
        (
            "ordered_values",
            update(ITEMS).ordered_values(
                (ITEMS.c.name, "x"), (ITEMS.c.tenant_id, _SOME_TENANT)
            ),
            True,
        ),
        (
            "a multi-row VALUES",
            insert(ITEMS).values(
                [{"id": 1, "name": "x"}, {"id": 2, "tenant_id": _SOME_TENANT}]
            ),
            True,
        ),
        (
            "a positional row",
            insert(ITEMS).values([(1, "x", None, None, _SOME_TENANT)]),
            True,
        ),
        (
            "the key of the tenant column when its NAME is tenant_id elsewhere",
            insert(_KEY_SWAPPED_ITEMS).values(tid=_SOME_TENANT),
            True,
        ),
        (
            "the key tenant_id that belongs to another column",
            insert(_KEY_SWAPPED_ITEMS).values(tenant_id=_SOME_TENANT),
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_values_a_statement_carries_name_the_tenant_column_by_name(
    label: str, statement: Any, supplies: bool
) -> None:
    assert tenant_mod._supplies_tenant_id(statement) is supplies, label


def test_the_tenant_column_of_a_table_is_found_by_name_not_by_key() -> None:
    assert tenant_mod._tenant_column(ITEMS) is ITEMS.c.tenant_id
    assert tenant_mod._tenant_column(_KEY_SWAPPED_ITEMS) is _KEY_SWAPPED_ITEMS.c.tid
    assert tenant_mod._tenant_column(_Other.__table__) is None  # type: ignore[arg-type]


def test_a_mapper_with_a_reversed_primary_key_lists_each_tables_key_in_table_order() -> (
    None
):
    assert list(tenant_mod._row_keys(inspect(_Reversed), (2, 1))) == [
        (REVERSED, (1, 2))
    ]
    # the single-column control
    assert list(tenant_mod._row_keys(inspect(_Item), (7,))) == [(ITEMS, (7,))]


@pytest.mark.asyncio
async def test_a_reversed_composite_key_flushes_updates_and_deletes(env: _Env) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(_Reversed(a=1, b=2, tenant_id=env.a, note="first"))
            await session.commit()
    async with env.maker() as session:
        with bind_tenant(env.a):
            row = (await session.execute(select(_Reversed))).scalar_one()
            row.note = "second"
            await session.commit()
    async with env.maker() as session:
        with bind_tenant(env.a):
            row = (await session.execute(select(_Reversed))).scalar_one()
            assert row.note == "second"
            await session.delete(row)
            await session.commit()
    assert await _unscoped(env, select(REVERSED.c.a)) == []


@pytest.mark.asyncio
async def test_a_listener_update_of_a_just_inserted_reversed_key_is_admitted(
    env: _Env,
) -> None:
    """``after_insert`` records the key of the row it inserted in the table's own
    order, so a later statement of the flush (a ``post_update``) may pin it."""

    def hook(_mapper: Any, connection: Any, _target: Any) -> None:
        connection.execute(
            update(REVERSED)
            .where(REVERSED.c.a == bindparam("pa"), REVERSED.c.b == bindparam("pb"))
            .values(note="post-updated"),
            {"pa": 1, "pb": 2},
        )

    event.listen(_Reversed, "after_insert", hook)
    try:
        async with env.maker() as session:
            with bind_tenant(env.a):
                session.add(_Reversed(a=1, b=2, tenant_id=env.a, note="first"))
                await session.commit()
    finally:
        event.remove(_Reversed, "after_insert", hook)
    notes = await _unscoped(env, select(REVERSED.c.note))
    assert [row.note for row in notes] == ["post-updated"]


# N2: a stamp lives while the row can exist. ``session.close()`` rolls the database
# back without ``after_soft_rollback``, so the stamp of an insert that never
# committed went on vouching for a primary key another tenant may now hold.
async def _insert_that_never_commits(env: _Env, shape: str, key: int) -> _Item:
    """Flush tenant A's item ``key`` in a transaction the session's close ends."""
    async with env.maker() as session:
        with bind_tenant(env.a):
            ghost = _Item(id=key, tenant_id=env.a, name="ghost")
            if shape == "flush":
                session.add(ghost)
                await session.flush()
            else:
                # a root-level write first: pysqlite opens its transaction only at
                # the first DML, and RELEASE of a savepoint that opened it COMMITS
                session.add(_Item(id=key + 1000, tenant_id=env.a, name="root"))
                await session.flush()
                async with session.begin_nested():
                    session.add(ghost)
                    await session.flush()
    return ghost  # the session closed: no commit, no rollback call


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["flush", "released savepoint"])
@pytest.mark.parametrize("write", ["update", "delete", "refresh"])
async def test_a_closed_session_leaves_no_stamp_on_an_insert_that_never_committed(
    env: _Env, shape: str, write: str
) -> None:
    ghost = await _insert_that_never_commits(env, shape, 5)
    assert 5 not in await _item_rows(env)  # the row is gone
    async with env.maker() as session:  # tenant B takes the primary key
        with bind_tenant(env.b):
            session.add(_Item(id=5, tenant_id=env.b, name="b-five"))
            await session.commit()
    before = await _item_rows(env)
    assert before[5] == ("b-five", env.b)
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(ghost)  # detached, with its key (and, once, its stamp)
            with pytest.raises(UnsafeTenantOperationError):
                if write == "refresh":
                    await session.refresh(ghost)  # would READ B's row
                elif write == "update":
                    ghost.name = "pwned"
                    await session.commit()
                else:
                    await session.delete(ghost)
                    await session.commit()
    assert await _item_rows(env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["flush", "released savepoint"])
async def test_a_committed_insert_keeps_its_stamp_when_the_session_closes(
    env: _Env, shape: str
) -> None:
    async with env.maker() as session:
        with bind_tenant(env.a):
            kept = _Item(id=6, tenant_id=env.a, name="kept")
            if shape == "flush":
                session.add(kept)
            else:
                async with session.begin_nested():
                    session.add(kept)
                    await session.flush()
            await session.commit()
    async with env.maker() as session:
        with bind_tenant(env.a):
            session.add(kept)
            kept.name = "renamed"
            await session.commit()  # the row exists and the stamp says it is A's
    assert (await _item_rows(env))[6] == ("renamed", env.a)
