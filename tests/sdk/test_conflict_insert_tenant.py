"""``conflict_insert`` on a TenantScoped model, against real PostgreSQL.

With a tenant bound, the upsert is admitted for that tenant's rows only, in one
provable form: the rows are in ``.values()`` (no execution parameters), an
absent ``tenant_id`` is set, the bound tenant's own plain value is kept, any
other value (another tenant's id, a bind, a literal, an expression) is refused,
no bind of a reserved name hides elsewhere in the statement, and the
``DO UPDATE`` can never touch another tenant's row. With no tenant bound it is
refused. A plain INSERT of a TenantScoped model stays forbidden.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    Text,
    Uuid,
    bindparam,
    cast,
    event,
    func,
    insert,
    literal,
    literal_column,
    select,
    text,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column

from sandloom.core.di import UnsafeTenantOperationError, bind_tenant, unscoped
from sandloom.core.orm import TenantScoped
from sandloom.core.tenant import BOUND_TENANT_PARAM, UPSERT_TENANT_PARAM
from sandloom.core.testing import reload_test_env
from sandloom.sdk.data import conflict_insert


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    """Keyed on ``id`` alone: a conflict can reach another tenant's row."""

    __tablename__ = "test_conflict_tenant_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[2] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def items() -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID], None
]:
    """(sessions, tenant A, tenant B, id of A's existing row ``a-original``)."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.fail("DATABASE_URL not set: the tenant upsert tests need PostgreSQL")
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    tenant_a, tenant_b, a_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with unscoped():
        async with sessions() as session:
            session.add(_Item(id=a_id, tenant_id=tenant_a, name="a-original"))
            await session.commit()
    yield sessions, tenant_a, tenant_b, a_id
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> dict[uuid.UUID, tuple]:
    with unscoped():
        async with sessions() as session:
            found = (await session.execute(select(_Item))).scalars().all()
    return {row.id: (row.tenant_id, row.name) for row in found}


def _upsert(session: AsyncSession, **values: object):
    return (
        conflict_insert(_Item, session=session)
        .values(**values)
        .on_conflict_do_update(index_elements=["id"], set_={"name": "set-by-upsert"})
    )


@pytest.mark.asyncio
async def test_upsert_without_tenant_id_inserts_then_updates_for_the_bound_tenant(
    items,
) -> None:
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(_upsert(session, id=new_id, name="first"))
            await session.commit()
        assert (await _rows(sessions))[new_id] == (tenant_a, "first")
        async with sessions() as session:
            await session.execute(_upsert(session, id=new_id, name="second"))
            await session.commit()
    assert (await _rows(sessions))[new_id] == (tenant_a, "set-by-upsert")


@pytest.mark.asyncio
async def test_upsert_with_the_bound_tenants_own_id_is_admitted(items) -> None:
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                _upsert(session, id=new_id, tenant_id=tenant_a, name="own")
            )
            await session.commit()
    assert (await _rows(sessions))[new_id] == (tenant_a, "own")


@pytest.mark.asyncio
async def test_upsert_naming_another_tenant_is_refused(items) -> None:
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(
                    _upsert(session, id=new_id, tenant_id=tenant_b, name="foreign")
                )
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
async def test_a_multi_row_upsert_with_one_foreign_row_is_refused_whole(items) -> None:
    sessions, tenant_a, tenant_b, _ = items
    ids = [uuid.uuid4(), uuid.uuid4()]
    rows = [
        {"id": ids[0], "name": "mine"},
        {"id": ids[1], "tenant_id": tenant_b, "name": "foreign"},
    ]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(statement)
    assert not set(ids) & set(await _rows(sessions))


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["list", "dict", "empty_dict", "empty_list"])
@pytest.mark.parametrize("with_values", [False, True])
async def test_any_execution_parameters_are_refused(
    items, shape: str, with_values: bool
) -> None:
    # Rows go in ``.values()``: an execution parameter dict or list, even an
    # empty one, is refused whether or not the rows are also in the statement.
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    row = {"id": new_id, "name": "x"}
    params = {
        "list": [row],
        "dict": row,
        "empty_dict": {},
        "empty_list": [],
    }[shape]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = conflict_insert(_Item, session=session)
            if with_values:
                statement = statement.values(id=new_id, name="x")
            statement = statement.on_conflict_do_nothing(index_elements=["id"])
            with pytest.raises(UnsafeTenantOperationError, match="no execution param"):
                await session.execute(statement, params)
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("many", [False, True])
async def test_execution_parameters_cannot_reach_a_named_tenant_parameter(
    items, many: bool
) -> None:
    # The earlier admission paths: a named bind for tenant_id filled from the
    # execution rows (the bound tenant's own or another tenant's), the
    # reserved slot name in a row, an own value that the row overrides.
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    forms = [
        bindparam("t"),
        bindparam("t", value=tenant_a),
        bindparam("t", value=tenant_a, type_=Uuid()),
        bindparam(BOUND_TENANT_PARAM, value=tenant_a, type_=Uuid()),
    ]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            for tenant in forms:
                for key in ("t", BOUND_TENANT_PARAM):
                    row = {"i": new_id, "n": "x", key: tenant_b}
                    with pytest.raises(UnsafeTenantOperationError):
                        await session.execute(
                            _with_tenant_expr(session, tenant, update=False),
                            [row] if many else row,
                        )
    assert new_id not in await _rows(sessions)


def _with_tenant_expr(session: AsyncSession, tenant: object, *, update: bool):
    """``tenant_id`` is ``tenant`` (a bind or an expression); ``i`` and ``n``
    are named parameters (the form execution rows used to fill)."""
    statement = conflict_insert(_Item, session=session).values(
        id=bindparam("i"), name=bindparam("n"), tenant_id=tenant
    )
    if update:
        return statement.on_conflict_do_update(
            index_elements=["id"], set_={"name": "set-by-upsert"}
        )
    return statement.on_conflict_do_nothing(index_elements=["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize(
    "form",
    [
        "named",
        "own_named",
        "own_typed",
        "default_none",
        "callable_own",
        "callable_foreign",
        "cast_own",
        "cast_foreign",
        "type_coerce_own",
        "type_coerce_foreign",
        "literal_own",
        "literal_foreign",
        "reserved_own",
        "reserved_foreign",
        "none",
        "foreign_string",
        "expression",
    ],
)
async def test_a_tenant_id_that_is_not_a_plain_bound_value_is_refused(
    items, form: str, update: bool
) -> None:
    # ``.values(tenant_id=x)`` admits only the bound tenant's id as a plain
    # value: no bind the caller built, no literal, no expression.
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    forms: dict[str, object] = {
        "named": bindparam("t"),
        "own_named": bindparam("t", value=tenant_a),
        "own_typed": bindparam("t", value=tenant_a, type_=Uuid()),
        "default_none": bindparam("t", value=None),
        "callable_own": bindparam("t", callable_=lambda: tenant_a),
        "callable_foreign": bindparam("t", callable_=lambda: tenant_b),
        "cast_own": cast(bindparam("t", value=tenant_a), Uuid()),
        "cast_foreign": cast(bindparam("t", value=tenant_b), Uuid()),
        "type_coerce_own": type_coerce(tenant_a, Uuid()),
        "type_coerce_foreign": type_coerce(tenant_b, Uuid()),
        "literal_own": literal(tenant_a),
        "literal_foreign": literal(tenant_b),
        "reserved_own": bindparam(BOUND_TENANT_PARAM, value=tenant_a, type_=Uuid()),
        "reserved_foreign": bindparam(BOUND_TENANT_PARAM, value=tenant_b, type_=Uuid()),
        "none": None,
        "foreign_string": str(tenant_b),
        "expression": func.coalesce(tenant_a, tenant_b),
    }
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = conflict_insert(_Item, session=session).values(
                id=new_id, name="x", tenant_id=forms[form]
            )
            statement = (
                statement.on_conflict_do_update(
                    index_elements=["id"], set_={"name": "set-by-upsert"}
                )
                if update
                else statement.on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(statement)
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["bind", "literal", "default_none"])
async def test_a_multi_row_values_with_a_bind_for_tenant_id_is_refused(
    items, form: str
) -> None:
    sessions, tenant_a, tenant_b, _ = items
    ids = [uuid.uuid4(), uuid.uuid4()]
    binds = {
        "bind": (bindparam("t0", value=tenant_a), bindparam("t1", value=tenant_b)),
        "literal": (literal(tenant_a), literal(tenant_a)),
        "default_none": (bindparam("t0", value=None), bindparam("t1", value=None)),
    }[form]
    rows = [
        {"id": ids[0], "name": "mine", "tenant_id": binds[0]},
        {"id": ids[1], "name": "foreign", "tenant_id": binds[1]},
    ]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(
                    conflict_insert(_Item, session=session)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
    assert not set(ids) & set(await _rows(sessions))


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("form", ["literal", "unique"])
async def test_a_bind_shared_between_tenant_id_and_another_column_is_refused(
    items, form: str, many: bool, update: bool
) -> None:
    # SQLAlchemy renders one anonymous bind under one name: the bind given for
    # ``tenant_id`` is refused whatever else uses it.
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    shared = (
        literal(tenant_a, Uuid())
        if form == "literal"
        else bindparam("t", value=tenant_a, unique=True)
    )
    with bind_tenant(tenant_a):
        async with sessions() as session:
            if many:
                insert_ = conflict_insert(_Item, session=session).values(
                    [{"id": new_id, "name": shared, "tenant_id": shared}]
                )
            else:
                insert_ = conflict_insert(_Item, session=session).values(
                    id=new_id, name=shared, tenant_id=shared
                )
            statement = (
                insert_.on_conflict_do_update(
                    index_elements=["id"], set_={"name": "set-by-upsert"}
                )
                if update
                else insert_.on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(statement)
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
async def test_a_bind_shared_with_set_is_refused(items) -> None:
    sessions, tenant_a, _, a_id = items
    shared = bindparam("t", value=tenant_a, unique=True)
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x", tenant_id=shared)
                .on_conflict_do_update(
                    index_elements=["id"], set_={"name": cast(shared, Text)}
                )
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(statement)


def _reserved(name: str, tenant: uuid.UUID):
    """A bind the caller names with a reserved key, holding ``tenant``."""
    return bindparam(name, value=tenant, type_=Uuid())


def _reserved_statement(session: AsyncSession, form: str, new_id, tenant: uuid.UUID):
    """A conflict insert that hides a caller's bind named with a reserved key
    (holding ``tenant``) in a place other than the ``tenant_id`` slot."""
    insert_ = conflict_insert(_Item, session=session)
    as_text = cast(_reserved(BOUND_TENANT_PARAM, tenant), Text)
    match form:
        case "set":
            return insert_.values(id=new_id, name="x").on_conflict_do_update(
                index_elements=["id"], set_={"name": as_text}
            )
        case "where":
            return insert_.values(id=new_id, name="x").on_conflict_do_update(
                index_elements=["id"],
                set_={"name": "set-by-upsert"},
                where=_Item.tenant_id == _reserved(BOUND_TENANT_PARAM, tenant),
            )
        case "index_where":
            return insert_.values(id=new_id, name="x").on_conflict_do_nothing(
                index_elements=["id"],
                index_where=_Item.tenant_id == _reserved(BOUND_TENANT_PARAM, tenant),
            )
        case "column":
            return insert_.values(id=new_id, name=as_text).on_conflict_do_nothing(
                index_elements=["id"]
            )
        case "multi_column":
            return insert_.values(
                [{"id": new_id, "name": as_text}]
            ).on_conflict_do_nothing(index_elements=["id"])
        case "returning":
            return (
                insert_.values(id=new_id, name="x")
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(_reserved(BOUND_TENANT_PARAM, tenant).label("leak"))
            )
        case "text":
            return insert_.values(
                id=new_id,
                name=text(f":{BOUND_TENANT_PARAM}").bindparams(
                    _reserved(BOUND_TENANT_PARAM, tenant)
                ),
            ).on_conflict_do_nothing(index_elements=["id"])
        case "upsert_scope":
            return insert_.values(id=new_id, name="x").on_conflict_do_update(
                index_elements=["id"],
                set_={"name": "set-by-upsert"},
                where=_Item.tenant_id == _reserved(UPSERT_TENANT_PARAM, tenant),
            )
    raise AssertionError(form)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "form",
    [
        "set",
        "where",
        "index_where",
        "column",
        "multi_column",
        "returning",
        "text",
        "upsert_scope",
    ],
)
async def test_a_bind_with_a_reserved_name_outside_the_slot_is_refused(
    items, form: str
) -> None:
    # A caller's bind named like the guard's own renders after VALUES and its
    # value would win over the tenant_id slot (or the DO UPDATE scope):
    # tenant B's id written, or tenant B's row updated, under bind_tenant(A).
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    with unscoped():
        async with sessions() as session:
            session.add(_Item(id=new_id, tenant_id=tenant_b, name="b-original"))
            await session.commit()
    fresh = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            for target in (fresh, new_id):
                statement = _reserved_statement(session, form, target, tenant_b)
                with pytest.raises(UnsafeTenantOperationError, match="reserved"):
                    await session.execute(statement)
    found = await _rows(sessions)
    assert fresh not in found
    assert found[new_id] == (tenant_b, "b-original")


class _Alias(TenantScoped, _Base):
    """A second attribute (``owner``) mapped onto the ``tenant_id`` column."""

    __tablename__ = "test_conflict_tenant_aliases"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


_Alias.owner = column_property(_Alias.__table__.c.tenant_id)  # type: ignore[attr-defined]


async def _alias_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> dict[uuid.UUID, uuid.UUID]:
    with unscoped():
        async with sessions() as session:
            found = (await session.execute(select(_Alias))).scalars().all()
    return {row.id: row.tenant_id for row in found}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["values", "multi_values", "dict", "list"])
async def test_an_attribute_mapped_onto_tenant_id_cannot_carry_another_tenant(
    items, shape: str
) -> None:
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    row = {"id": new_id, "name": "x", "owner": tenant_b}
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = conflict_insert(_Alias, session=session)
            if shape == "values":
                statement = statement.values(**row)
            elif shape == "multi_values":
                statement = statement.values([row])
            statement = statement.on_conflict_do_nothing(index_elements=["id"])
            params = {"dict": row, "list": [row]}.get(shape)
            expected = "no execution param" if params else "not the bound"
            with pytest.raises(UnsafeTenantOperationError, match=expected):
                await session.execute(statement, params)
    assert new_id not in await _alias_rows(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("many", [False, True])
async def test_an_attribute_mapped_onto_tenant_id_may_carry_the_bound_tenant(
    items, many: bool
) -> None:
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    row = {"id": new_id, "name": "x", "owner": tenant_a}
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = conflict_insert(_Alias, session=session)
            statement = statement.values([row]) if many else statement.values(**row)
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
    assert (await _alias_rows(sessions))[new_id] == tenant_a


@pytest.mark.asyncio
async def test_a_multi_row_values_with_several_rows_is_admitted(items) -> None:
    sessions, tenant_a, _, _ = items
    ids = [uuid.uuid4() for _ in range(4)]
    rows = [
        {"id": ids[0], "name": "absent"},
        {"id": ids[1], "name": "uuid", "tenant_id": tenant_a},
        {"id": ids[2], "name": "string", "tenant_id": str(tenant_a)},
        {"id": ids[3], "name": "absent-again"},
    ]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                conflict_insert(_Item, session=session)
                .values(rows)
                .on_conflict_do_update(
                    index_elements=["id"], set_={"name": "set-by-upsert"}
                )
            )
            await session.commit()
    found = await _rows(sessions)
    assert [found[i] for i in ids] == [
        (tenant_a, "absent"),
        (tenant_a, "uuid"),
        (tenant_a, "string"),
        (tenant_a, "absent-again"),
    ]


@pytest.mark.asyncio
async def test_the_tenant_slot_renders_as_the_reserved_bind_in_every_form(
    items,
) -> None:
    sessions, tenant_a, _, _ = items
    sent: list[dict[str, object]] = []
    engine = sessions.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO test_conflict_tenant_items" in statement:
            sent.append(dict(context.compiled.params))

    event.listen(engine, "before_cursor_execute", capture)
    ids = [uuid.uuid4() for _ in range(3)]
    try:
        with bind_tenant(tenant_a):
            async with sessions() as session:
                # tenant_id absent
                await session.execute(
                    conflict_insert(_Item, session=session)
                    .values(id=ids[0], name="one")
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                # tenant_id as the bound tenant's own plain value
                await session.execute(
                    conflict_insert(_Item, session=session)
                    .values(id=ids[1], name="two", tenant_id=tenant_a)
                    .on_conflict_do_update(
                        index_elements=["id"], set_={"name": "set-by-upsert"}
                    )
                )
                # multi-row .values() with a plain tenant_id
                await session.execute(
                    conflict_insert(_Item, session=session)
                    .values([{"id": ids[2], "name": "three", "tenant_id": tenant_a}])
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                await session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    found = await _rows(sessions)
    assert [found[i][0] for i in ids] == [tenant_a] * 3
    # The compiled parameters of each INSERT carry the reserved bind with the
    # bound tenant: the tenant slot is that bind, nothing a row can name.
    assert len(sent) == 3
    assert all(params.get(BOUND_TENANT_PARAM) == tenant_a for params in sent), sent


@pytest.mark.asyncio
async def test_set_may_read_the_proposed_row_through_excluded(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            insert = conflict_insert(_Item, session=session).values(
                id=a_id, name="from-excluded"
            )
            await session.execute(
                insert.on_conflict_do_update(
                    index_elements=["id"], set_={"name": insert.excluded.name}
                )
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "from-excluded")


@pytest.mark.asyncio
async def test_set_to_the_whole_excluded_row_cannot_change_tenant_id(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            insert = conflict_insert(_Item, session=session).values(id=a_id, name="x")
            with pytest.raises(UnsafeTenantOperationError, match="tenant_id"):
                await session.execute(
                    insert.on_conflict_do_update(
                        index_elements=["id"], set_=insert.excluded
                    )
                )


@pytest.mark.asyncio
async def test_a_conflict_with_another_tenants_row_does_not_update_it(items) -> None:
    sessions, _, tenant_b, a_id = items
    with bind_tenant(tenant_b):
        async with sessions() as session:
            await session.execute(_upsert(session, id=a_id, name="b-takeover"))
            await session.commit()
    # Tenant A's row is untouched and still A's; tenant B gained no row.
    assert await _rows(sessions) == {a_id: (items[1], "a-original")}


@pytest.mark.asyncio
async def test_do_nothing_is_admitted_and_skips_a_conflict(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="ignored")
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
async def test_a_caller_where_is_kept_beside_the_tenant_scope(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"name": "never"},
                    where=_Item.name == "not-a-match",
                )
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
async def test_upsert_with_no_tenant_bound_is_refused(items) -> None:
    sessions, _, _, _ = items
    async with sessions() as session:
        with pytest.raises(UnsafeTenantOperationError, match="no tenant bound"):
            await session.execute(_upsert(session, id=uuid.uuid4(), name="orphan"))


@pytest.mark.asyncio
async def test_upsert_under_unscoped_is_untouched_by_the_guard(items) -> None:
    sessions, tenant_a, tenant_b, a_id = items
    with unscoped():
        async with sessions() as session:
            await session.execute(
                _upsert(session, id=a_id, tenant_id=tenant_b, name="system")
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "set-by-upsert")


@pytest.mark.asyncio
async def test_set_cannot_change_tenant_id(items) -> None:
    sessions, tenant_a, tenant_b, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(
                    index_elements=["id"], set_={"tenant_id": tenant_b}
                )
            )
            with pytest.raises(UnsafeTenantOperationError, match="tenant_id"):
                await session.execute(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "form",
    [
        "literal_upper",
        "literal_quoted",
        "literal_plain",
        "unknown_str",
        "upper_str",
        "other_table_column",
    ],
)
async def test_a_set_key_that_is_not_a_column_of_the_table_is_refused(
    items, form: str
) -> None:
    # PostgreSQL's compiler renders a ``set_`` key it cannot match, a
    # ``literal_column`` verbatim: ``TENANT_ID`` folds to ``tenant_id``.
    sessions, tenant_a, tenant_b, a_id = items
    keys: dict[str, object] = {
        "literal_upper": literal_column("TENANT_ID"),
        "literal_quoted": literal_column('"tenant_id"'),
        "literal_plain": literal_column("tenant_id"),
        "unknown_str": "nope",
        "upper_str": "TENANT_ID",
        "other_table_column": Table(
            "other", MetaData(), Column("tenant_id", Uuid())
        ).c.tenant_id,
    }
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(
                    index_elements=["id"], set_={keys[form]: tenant_b}
                )
            )
            with pytest.raises(UnsafeTenantOperationError, match="not a column"):
                await session.execute(statement)
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["attribute", "column"])
async def test_a_set_key_that_is_the_tenant_column_is_refused_in_every_spelling(
    items, form: str
) -> None:
    sessions, tenant_a, tenant_b, a_id = items
    key = _Item.tenant_id if form == "attribute" else _Item.__table__.c.tenant_id
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(index_elements=["id"], set_={key: tenant_b})
            )
            # A raw Column of the tenant table is also a Core reference, which
            # the guard refuses first.
            expected = "cannot change|Core statements"
            with pytest.raises(UnsafeTenantOperationError, match=expected):
                await session.execute(statement)
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["str", "attribute"])
async def test_a_set_key_that_is_a_column_of_the_table_is_admitted(
    items, form: str
) -> None:
    sessions, tenant_a, _, a_id = items
    key = {"str": "name", "attribute": _Item.name}[form]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            insert_ = conflict_insert(_Item, session=session).values(id=a_id, name="x")
            await session.execute(
                insert_.on_conflict_do_update(
                    index_elements=["id"], set_={key: insert_.excluded.name}
                )
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "x")


@pytest.mark.asyncio
async def test_a_subquery_in_the_upsert_is_refused(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            leak = select(_Item.name).where(_Item.id == a_id).scalar_subquery()
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_update(index_elements=["id"], set_={"name": leak})
            )
            with pytest.raises(UnsafeTenantOperationError, match="SELECT"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_a_subquery_in_a_multi_row_values_is_refused(items) -> None:
    sessions, tenant_a, _, a_id = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            leak = select(_Item.name).where(_Item.id == a_id).scalar_subquery()
            statement = (
                conflict_insert(_Item, session=session)
                .values([{"id": new_id, "name": leak}])
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="SELECT"):
                await session.execute(statement)
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
async def test_from_select_is_refused(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .from_select(["id", "name"], select(_Item.id, _Item.name))
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="SELECT"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_the_table_form_of_a_tenant_model_stays_refused(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item.__table__, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="Core statements"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_a_plain_insert_stays_forbidden(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            with pytest.raises(UnsafeTenantOperationError, match="bulk INSERT"):
                await session.execute(insert(_Item).values(id=uuid.uuid4(), name="x"))
            plain = conflict_insert(_Item, session=session).values(
                id=uuid.uuid4(), name="x"
            )
            with pytest.raises(UnsafeTenantOperationError, match="bulk INSERT"):
                await session.execute(plain)
