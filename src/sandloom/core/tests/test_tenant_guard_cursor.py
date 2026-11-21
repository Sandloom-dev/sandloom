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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    column,
    delete,
    event,
    func,
    insert,
    inspect,
    literal,
    literal_column,
    select,
    table,
    text,
    true,
    update,
    values,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, StatementError
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
    joinedload,
    make_transient_to_detached,
    mapped_column,
    query_expression,
    registry,
    relationship,
    undefer,
    with_loader_criteria,
    with_polymorphic,
)
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement

from sandloom.core import tenant as tenant_mod
from sandloom.core.di import (
    MissingTenantError,
    TenantMismatchError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.tenant import TENANT_CRITERIA_PARAM
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
    install_write_admission(engine)  # the cursor rule every kernel engine carries
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
            await session.execute(
                insert(IMP),
                [
                    {"id": 1, "tenant_id": state.a, "name": "a-secret"},
                    {"id": 2, "tenant_id": state.b, "name": "b-secret"},
                ],
            )
            await session.commit()
    try:
        yield state
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


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
        async with env.engine.begin() as conn:
            await conn.run_sync(late.create)
        with pytest.raises(UnsafeTenantOperationError):
            await _bound(env, select(late.c.name))
        with pytest.raises(MissingTenantError):
            await _unbound(env, select(late.c.name))
    finally:
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


# B5 - B7, FP3: a rendered default that reads a tenant table, at the flush cursor
_SINK_MODELS: dict[str, Any] = {
    "a raw SQL-expression default": _RawSink,
    "a text() default": _TextSink,
    "a literal_column() default": _LiteralSink,
    "a Python default running Core on the flush connection": _CtxSink,
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
