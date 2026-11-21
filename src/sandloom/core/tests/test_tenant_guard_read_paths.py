"""Reads and writes that skipped the tenant guard (PR #338 red-team, P1-P4).

Where tenancy is enforced by the application (SQLite, PostgreSQL without RLS)
the ``do_orm_execute`` guard is the only thing between a bound tenant and the
rows of another. Four paths never reached it:

* the legacy ``Session.bulk_update_mappings`` / ``bulk_insert_mappings`` /
  ``bulk_save_objects`` (no ORM execute, no flush: they rewrite or plant rows);
* SQL carried by a loader option (``with_expression``, ``with_loader_criteria``,
  the extra criteria of ``rel.and_(...)``), which ``get_children`` skips;
* the rows of a multi-row ``VALUES`` and the SQL-expression defaults of an
  INSERT/UPDATE target;
* a statement that is none of select/insert/update/delete (a bare ``func.f(...)``).

Each runs on SQLite and on PostgreSQL (RLS off): the refusals and the scoping
happen in the guard, before the database sees the statement.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    ForeignKey,
    Text,
    Uuid,
    delete,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    make_transient_to_detached,
    mapped_column,
    query_expression,
    relationship,
    selectinload,
    with_expression,
    with_loader_criteria,
)

from sandloom.core.di import (
    MissingTenantError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.sql_alchemy.write_admission import install_write_admission
from sandloom.core.testing import reload_test_env

#: Fixed ids: the column defaults of ``_Sink`` / ``_OrmSink`` name tenant B's row.
A_ITEM_ID = uuid.uuid4()
B_ITEM_ID = uuid.uuid4()


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    __tablename__ = "test_tgrp_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class _RawItem(_Base):
    """A second mapper over the tenant table that does NOT inherit TenantScoped."""

    __table__ = _Item.__table__


class _Soft:
    """A plain mixin (the soft-delete recipe's root entity)."""

    deleted: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)


class _Other(_Soft, _Base):
    """NOT tenant-scoped."""

    __tablename__ = "test_tgrp_others"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    expr: Mapped[str | None] = query_expression()
    leaves: Mapped[list[_Leaf]] = relationship(lambda: _Leaf, viewonly=True)


class _Leaf(_Base):
    """NOT tenant-scoped; the target of a relationship whose option carries SQL."""

    __tablename__ = "test_tgrp_leaves"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    other_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_tgrp_others.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)


class _Sink(_Base):
    """NOT tenant-scoped; its defaults read the tenant table as raw Core."""

    __tablename__ = "test_tgrp_sinks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    stamped: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=select(_Item.__table__.c.name)
        .where(_Item.__table__.c.id == B_ITEM_ID)
        .scalar_subquery(),
        onupdate=select(_Item.__table__.c.name)
        .where(_Item.__table__.c.id == B_ITEM_ID)
        .scalar_subquery(),
    )


class _OrmSink(_Base):
    """NOT tenant-scoped; its defaults read the tenant model through the ORM."""

    __tablename__ = "test_tgrp_orm_sinks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    stamped: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=select(_Item.name).where(_Item.id == B_ITEM_ID).scalar_subquery(),
        onupdate=select(_Item.name).where(_Item.id == B_ITEM_ID).scalar_subquery(),
    )


@dataclass
class _Env:
    engine: AsyncEngine
    maker: async_sessionmaker[AsyncSession]
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    other_id: uuid.UUID  # named "other"
    mirror_id: uuid.UUID  # named like tenant B's item: a boolean oracle for it
    leaf_id: uuid.UUID  # a leaf of `mirror_id` named like tenant B's item
    sink_id: uuid.UUID
    orm_sink_id: uuid.UUID


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
        url = f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}"
    else:
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
        tenant_a=uuid.uuid4(),
        tenant_b=uuid.uuid4(),
        other_id=uuid.uuid4(),
        mirror_id=uuid.uuid4(),
        leaf_id=uuid.uuid4(),
        sink_id=uuid.uuid4(),
        orm_sink_id=uuid.uuid4(),
    )
    async with state.maker() as session:
        with unscoped():
            session.add_all(
                [
                    _Item(id=A_ITEM_ID, tenant_id=state.tenant_a, name="a-row"),
                    _Item(id=B_ITEM_ID, tenant_id=state.tenant_b, name="b-row"),
                    _Other(id=state.other_id, name="other"),
                    _Other(id=state.mirror_id, name="b-row"),
                    _Sink(id=state.sink_id, value="x", stamped="seed"),
                    _OrmSink(id=state.orm_sink_id, value="x", stamped="seed"),
                ]
            )
            await session.flush()
            session.add(_Leaf(id=state.leaf_id, other_id=state.mirror_id, name="b-row"))
            await session.commit()
    try:
        yield state
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


def _b_name() -> Any:
    """A scalar subquery reading tenant B's item through the ORM entity."""
    return select(_Item.name).where(_Item.id == B_ITEM_ID).scalar_subquery()


def _b_name_raw() -> Any:
    """The same read through the raw Core table."""
    table = _Item.__table__
    return select(table.c.name).where(table.c.id == B_ITEM_ID).scalar_subquery()


async def _names(env: _Env) -> dict[uuid.UUID, str]:
    async with env.maker() as session:
        with unscoped():
            rows = (await session.execute(select(_Item.id, _Item.name))).all()
        return {row.id: row.name for row in rows}


def _fetch(result: Any) -> list[Any]:
    """The rows of a select; a DML result without RETURNING has none."""
    if isinstance(result, CursorResult) and not result.returns_rows:
        return []
    return list(result.unique().all())


async def _run(env: _Env, statement: Any, tenant: uuid.UUID | None) -> Any:
    """Execute ``statement`` under ``tenant`` (``None``: nothing bound)."""
    async with env.maker() as session:
        if tenant is None:
            result = await session.execute(statement)
        else:
            with bind_tenant(tenant):
                result = await session.execute(statement)
        rows = _fetch(result)
        await session.commit()
        return rows


async def _unscoped(env: _Env, statement: Any) -> Any:
    async with env.maker() as session:
        with unscoped():
            rows = _fetch(await session.execute(statement))
            await session.commit()
            return rows


async def _scalar_unscoped(env: _Env, statement: Any) -> Any:
    rows = await _unscoped(env, statement)
    return rows[0][0] if rows else None


# --------------------------------------------------------------------- P1 ---
def _bulk_update_by_class(session: Any, env: _Env) -> None:
    session.bulk_update_mappings(_Item, [{"id": B_ITEM_ID, "name": "pwned"}])


def _bulk_update_by_mapper(session: Any, env: _Env) -> None:
    session.bulk_update_mappings(inspect(_Item), [{"id": B_ITEM_ID, "name": "pwned"}])


def _bulk_insert_into_another_tenant(session: Any, env: _Env) -> None:
    session.bulk_insert_mappings(
        _Item, [{"id": uuid.uuid4(), "tenant_id": env.tenant_b, "name": "planted"}]
    )


def _bulk_save_new_object(session: Any, env: _Env) -> None:
    session.bulk_save_objects(
        [_Item(id=uuid.uuid4(), tenant_id=env.tenant_b, name="planted")]
    )


def _bulk_save_detached_change(session: Any, env: _Env) -> None:
    changed = _Item(id=B_ITEM_ID, tenant_id=env.tenant_b, name="pwned")
    make_transient_to_detached(changed)
    session.bulk_save_objects([changed], update_changed_only=False)


def _bulk_update_through_a_sibling_mapper(session: Any, env: _Env) -> None:
    session.bulk_update_mappings(_RawItem, [{"id": B_ITEM_ID, "name": "pwned"}])


_LEGACY_BULK: dict[str, Callable[[Any, _Env], None]] = {
    "update_mappings_class": _bulk_update_by_class,
    "update_mappings_mapper": _bulk_update_by_mapper,
    "insert_mappings": _bulk_insert_into_another_tenant,
    "save_objects_insert": _bulk_save_new_object,
    "save_objects_update": _bulk_save_detached_change,
    "sibling_mapper_over_the_tenant_table": _bulk_update_through_a_sibling_mapper,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_LEGACY_BULK))
@pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
async def test_legacy_bulk_api_on_a_tenant_model_is_refused(
    env: _Env, shape: str, bound: bool
) -> None:
    before = await _names(env)
    async with env.maker() as session:
        with pytest.raises(UnsafeTenantOperationError, match="Session.bulk_"):
            if bound:
                with bind_tenant(env.tenant_a):
                    await session.run_sync(lambda s: _LEGACY_BULK[shape](s, env))
            else:
                await session.run_sync(lambda s: _LEGACY_BULK[shape](s, env))
    assert await _names(env) == before


@pytest.mark.asyncio
async def test_legacy_bulk_api_runs_inside_unscoped(env: _Env) -> None:
    async with env.maker() as session:
        with unscoped():
            await session.run_sync(lambda s: _bulk_update_by_class(s, env))
            await session.run_sync(lambda s: _bulk_insert_into_another_tenant(s, env))
            await session.commit()
    names = await _names(env)
    assert names[B_ITEM_ID] == "pwned"
    assert "planted" in names.values()


@pytest.mark.asyncio
async def test_legacy_bulk_api_on_a_non_tenant_model_stays_allowed(env: _Env) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            await session.run_sync(
                lambda s: s.bulk_insert_mappings(_Other, [{"id": first, "name": "n1"}])
            )
            # a generator is consumed once: the wrapper must hand the original
            # every object it inspected
            await session.run_sync(
                lambda s: s.bulk_save_objects(
                    obj for obj in [_Other(id=second, name="n2")]
                )
            )
            await session.run_sync(
                lambda s: s.bulk_update_mappings(_Other, [{"id": first, "name": "n3"}])
            )
        await session.commit()
    rows = await _unscoped(env, select(_Other.id, _Other.name))
    assert {row.id: row.name for row in rows}.items() >= {
        first: "n3",
        second: "n2",
    }.items()


# --------------------------------------------------------- P2: loader options ---
@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["b_orm", "b_raw", "a_orm"])
async def test_with_expression_reading_a_tenant_table_is_refused(
    env: _Env, target: str
) -> None:
    subquery = {
        "b_orm": _b_name(),
        "b_raw": _b_name_raw(),
        # the tenant's own row too: a with_expression subquery is deannotated by
        # SQLAlchemy, so no loader criterion can reach it
        "a_orm": select(_Item.name).where(_Item.id == A_ITEM_ID).scalar_subquery(),
    }[target]
    statement = select(_Other).options(with_expression(_Other.expr, subquery))
    with pytest.raises(UnsafeTenantOperationError):
        await _run(env, statement, env.tenant_a)
    with pytest.raises(MissingTenantError):
        await _run(env, statement, None)
    # the read is real: unscoped() returns tenant B's value
    rows = await _unscoped(env, statement.where(_Other.id == env.other_id))
    expected = "a-row" if target == "a_orm" else "b-row"
    assert [row[0].expr for row in rows] == [expected]


@pytest.mark.asyncio
async def test_with_expression_on_non_tenant_data_stays_allowed(env: _Env) -> None:
    statement = (
        select(_Other)
        .options(with_expression(_Other.expr, func.upper(_Other.name)))
        .where(_Other.id == env.other_id)
    )
    rows = await _run(env, statement, env.tenant_a)
    assert [row[0].expr for row in rows] == ["OTHER"]
    assert [row[0].expr for row in await _run(env, statement, None)] == ["OTHER"]


def _criteria_direct() -> Any:
    return with_loader_criteria(_Other, _Other.name == _b_name())


def _criteria_lambda() -> Any:
    subquery = _b_name()
    return with_loader_criteria(_Other, lambda cls: cls.name == subquery)


def _criteria_on_a_mixin_root() -> Any:
    subquery = _b_name()
    return with_loader_criteria(
        _Soft, lambda cls: cls.name == subquery, include_aliases=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criteria", [_criteria_direct, _criteria_lambda, _criteria_on_a_mixin_root]
)
async def test_loader_criteria_cannot_read_another_tenants_row(
    env: _Env, criteria: Callable[[], Any]
) -> None:
    statement = select(_Other.name).options(criteria())
    # tenant B's item is invisible to A: its subquery is NULL, nothing matches
    assert await _run(env, statement, env.tenant_a) == []
    with pytest.raises(MissingTenantError):
        await _run(env, statement, None)
    # unscoped() sees the oracle: the "b-row" other row matches B's item name
    assert [row.name for row in await _unscoped(env, statement)] == ["b-row"]


@pytest.mark.asyncio
async def test_loader_criteria_on_a_non_tenant_model_stays_allowed(env: _Env) -> None:
    soft_delete = with_loader_criteria(
        _Soft, lambda cls: cls.deleted.is_(False), include_aliases=True
    )
    rows = await _run(env, select(_Other.name).options(soft_delete), env.tenant_a)
    assert {row.name for row in rows} == {"other", "b-row"}
    rows = await _run(env, select(_Other.name).options(soft_delete), None)
    assert {row.name for row in rows} == {"other", "b-row"}
    # a criterion on the tenant model itself still returns the tenant's own rows
    own = with_loader_criteria(_Item, _Item.name == "a-row")
    rows = await _run(env, select(_Item.name).options(own), env.tenant_a)
    assert [row.name for row in rows] == ["a-row"]


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", [joinedload, selectinload])
async def test_relationship_and_criteria_cannot_read_another_tenants_row(
    env: _Env, loader: Callable[[Any], Any]
) -> None:
    option = loader(_Other.leaves.and_(_Leaf.name == _b_name()))
    statement = select(_Other).options(option).where(_Other.id == env.mirror_id)
    rows = await _run(env, statement, env.tenant_a)
    assert [len(row[0].leaves) for row in rows] == [0]
    with pytest.raises(MissingTenantError):
        await _run(env, statement, None)
    rows = await _unscoped(env, statement)
    assert [len(row[0].leaves) for row in rows] == [1]


# --------------------------------------------------- P3: VALUES and defaults ---
def _values_list(new_id: uuid.UUID, value: Any) -> Any:
    return insert(_Other).values([{"id": new_id, "name": value}])


def _values_dict(new_id: uuid.UUID, value: Any) -> Any:
    return insert(_Other).values({"id": new_id, "name": value})


def _values_kwargs(new_id: uuid.UUID, value: Any) -> Any:
    return insert(_Other).values(id=new_id, name=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_values_list, _values_dict, _values_kwargs])
async def test_insert_values_cannot_copy_another_tenants_row(
    env: _Env, build: Callable[[uuid.UUID, Any], Any]
) -> None:
    planted, unbound = uuid.uuid4(), uuid.uuid4()
    await _run(env, build(planted, _b_name()), env.tenant_a)
    copied = await _scalar_unscoped(
        env, select(_Other.name).where(_Other.id == planted)
    )
    assert copied is None  # B's item is invisible to A: the subquery is NULL
    with pytest.raises(MissingTenantError):
        await _run(env, build(unbound, _b_name()), None)
    # the copy is real: unscoped() copies B's value
    through = uuid.uuid4()
    await _unscoped(env, build(through, _b_name()))
    stored = await _scalar_unscoped(
        env, select(_Other.name).where(_Other.id == through)
    )
    assert stored == "b-row"


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_values_list, _values_dict, _values_kwargs])
async def test_insert_values_from_a_raw_tenant_table_is_refused(
    env: _Env, build: Callable[[uuid.UUID, Any], Any]
) -> None:
    with pytest.raises(UnsafeTenantOperationError):
        await _run(env, build(uuid.uuid4(), _b_name_raw()), env.tenant_a)


@pytest.mark.asyncio
async def test_insert_from_select_copies_only_the_bound_tenants_rows(
    env: _Env,
) -> None:
    def statement() -> Any:
        return insert(_Other).from_select(["id", "name"], select(_Item.id, _Item.name))

    await _run(env, statement(), env.tenant_a)
    copied = await _unscoped(env, select(_Other.id).where(_Other.id == A_ITEM_ID))
    assert len(copied) == 1
    assert await _unscoped(env, select(_Other.id).where(_Other.id == B_ITEM_ID)) == []
    with pytest.raises(MissingTenantError):
        await _run(env, statement(), None)


@pytest.mark.asyncio
async def test_multi_row_values_without_a_tenant_reference_stay_allowed(
    env: _Env,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    statement = insert(_Other).values(
        [{"id": first, "name": "v1"}, {"id": second, "name": func.upper("v2")}]
    )
    await _run(env, statement, env.tenant_a)
    rows = await _unscoped(env, select(_Other.id, _Other.name))
    assert {row.id: row.name for row in rows}.items() >= {
        first: "v1",
        second: "V2",
    }.items()


@pytest.mark.asyncio
async def test_a_raw_default_reading_a_tenant_table_is_refused(env: _Env) -> None:
    insert_sink = insert(_Sink).values(id=uuid.uuid4())
    for tenant in (env.tenant_a, None):
        with pytest.raises(UnsafeTenantOperationError):
            await _run(env, insert_sink, tenant)
    update_sink = update(_Sink).values(value="y").where(_Sink.id == env.sink_id)
    with pytest.raises(UnsafeTenantOperationError):
        await _run(env, update_sink, env.tenant_a)
    # trusted system code still stamps it: the default is real
    await _unscoped(env, insert_sink)
    await _unscoped(env, update_sink)
    stamped = await _scalar_unscoped(
        env, select(_Sink.stamped).where(_Sink.id == env.sink_id)
    )
    assert stamped == "b-row"


@pytest.mark.asyncio
async def test_an_orm_default_reading_a_tenant_model_is_scoped(env: _Env) -> None:
    new_id = uuid.uuid4()
    await _run(env, insert(_OrmSink).values(id=new_id), env.tenant_a)
    await _run(
        env,
        update(_OrmSink).values(value="y").where(_OrmSink.id == env.orm_sink_id),
        env.tenant_a,
    )
    stamped = await _unscoped(env, select(_OrmSink.id, _OrmSink.stamped))
    # tenant B's item is invisible to A: both defaults evaluated to NULL
    assert {row.id: row.stamped for row in stamped} == {
        new_id: None,
        env.orm_sink_id: None,
    }
    with pytest.raises(MissingTenantError):
        await _run(env, insert(_OrmSink).values(id=uuid.uuid4()), None)
    # and unscoped() reads B's value through the same default
    through = uuid.uuid4()
    await _unscoped(env, insert(_OrmSink).values(id=through))
    assert (
        await _scalar_unscoped(
            env, select(_OrmSink.stamped).where(_OrmSink.id == through)
        )
        == "b-row"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [_Sink, _OrmSink], ids=["raw", "orm"])
@pytest.mark.parametrize("tenant", ["bound", "unbound"])
async def test_a_flush_cannot_run_a_default_that_reads_a_tenant_table(
    env: _Env, model: type[_Sink] | type[_OrmSink], tenant: str
) -> None:
    """``session.add`` flushes straight from the persistence layer: no loader
    criterion reaches a default's subquery (ORM-annotated or raw), so the cursor
    refuses the rendered INSERT / UPDATE rather than let it read another tenant's
    row (a tenant that is bound or not: the refusal differs, the outcome does not)."""
    bound = env.tenant_a if tenant == "bound" else None
    refusal = UnsafeTenantOperationError if bound else MissingTenantError
    new_id = uuid.uuid4()
    async with env.maker() as session:
        session.add(model(id=new_id))
        with pytest.raises(refusal, match="TenantScoped"):
            if bound is None:
                await session.flush()
            else:
                with bind_tenant(bound):
                    await session.flush()
        await session.rollback()
    # an UPDATE flush: the onupdate default is read the same way
    existing = env.sink_id if model is _Sink else env.orm_sink_id
    async with env.maker() as session:
        loaded = await session.get(model, existing)
        assert loaded is not None
        loaded.value = "changed"
        with pytest.raises(refusal, match="TenantScoped"):
            if bound is None:
                await session.flush()
            else:
                with bind_tenant(bound):
                    await session.flush()
        await session.rollback()
    rows = await _unscoped(env, select(model.id, model.value, model.stamped))
    assert {row.id: (row.value, row.stamped) for row in rows} == {
        existing: ("x", "seed")
    }
    # trusted system code still flushes it: the default is real
    async with env.maker() as session:
        with unscoped():
            session.add(model(id=new_id))
            await session.commit()
    stamped = await _scalar_unscoped(
        env, select(model.stamped).where(model.id == new_id)
    )
    assert stamped == "b-row"


@pytest.mark.asyncio
async def test_a_flush_without_a_tenant_reading_default_stays_allowed(
    env: _Env,
) -> None:
    new_id = uuid.uuid4()
    async with env.maker() as session:
        with bind_tenant(env.tenant_a):
            session.add(_Other(id=new_id, name="added"))
            session.add(_Item(id=uuid.uuid4(), name="mine"))
            await session.commit()
    rows = await _unscoped(env, select(_Other.name).where(_Other.id == new_id))
    assert [row.name for row in rows] == ["added"]


def _update_set_from_a_tenant_column(env: _Env) -> Any:
    table = _Item.__table__
    return (
        update(_Other)
        .values(name=table.c.name)
        .where(_Other.id == env.other_id)
        .where(table.c.id == B_ITEM_ID)
    )


def _update_set_from_a_tenant_attribute(env: _Env) -> Any:
    return (
        update(_Other)
        .values(name=_Item.name)
        .where(_Other.id == env.other_id)
        .where(_Item.id == B_ITEM_ID)
    )


def _delete_joined_to_a_tenant_column(env: _Env) -> Any:
    return delete(_Other).where(_Other.name == _Item.__table__.c.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        _update_set_from_a_tenant_column,
        _update_set_from_a_tenant_attribute,
        _delete_joined_to_a_tenant_column,
    ],
)
async def test_dml_reaching_a_tenant_table_only_through_its_columns_is_refused(
    env: _Env, build: Callable[[_Env], Any]
) -> None:
    """The FROM is derived from the columns; the walk still finds the table."""
    with pytest.raises(UnsafeTenantOperationError):
        await _run(env, build(env), env.tenant_a)
    with pytest.raises(MissingTenantError):
        await _run(env, build(env), None)
    names = await _unscoped(env, select(_Other.name).order_by(_Other.name))
    assert [row.name for row in names] == ["b-row", "other"]


# ------------------------------------------------------------ P4: functions ---
@pytest.mark.asyncio
@pytest.mark.parametrize("subquery", [_b_name, _b_name_raw], ids=["orm", "raw"])
async def test_a_bare_function_cannot_read_a_tenant_table(
    env: _Env, subquery: Callable[[], Any]
) -> None:
    def statement() -> Any:
        return func.coalesce(subquery(), "none")

    with pytest.raises(UnsafeTenantOperationError):
        await _run(env, statement(), env.tenant_a)
    with pytest.raises(MissingTenantError):
        await _run(env, statement(), None)
    # the read is real: unscoped() returns tenant B's value
    assert await _scalar_unscoped(env, statement()) == "b-row"


@pytest.mark.asyncio
async def test_a_bare_function_without_a_tenant_reference_stays_allowed(
    env: _Env,
) -> None:
    for tenant in (env.tenant_a, None):
        rows = await _run(env, func.coalesce(None, "fallback"), tenant)
        assert rows[0][0] == "fallback"
