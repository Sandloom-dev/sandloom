# ruff: noqa: F811  - the `rls` fixture is imported from test_tenant_rls
"""The RLS tenant hook covers EVERY SQLAlchemy execution path (real PostgreSQL).

``tenant_rls`` sets ``sandloom.tenant_id`` by wrapping the
dialect's ``do_execute`` / ``do_executemany`` / ``do_execute_no_params`` (no
engine events). Each test below drives one path with tenant A bound against a
FORCEd-RLS table holding rows of tenants A and B, through a mapping / statement
that carries NO tenant predicate of its own — so whatever filtering is observed
is PostgreSQL's policy, i.e. proof that the settings reached the server before
the statement. SELECT, INSERT, UPDATE and DELETE are each checked.

Paths the hook does NOT cover (the raw driver connection) are pinned to fail
closed in PostgreSQL: empty / malformed / other-tenant settings never expose or
modify tenant A's rows. The bypass setting of the pre-fix policy is inert now;
the remaining residual risk (code that can send SQL may set ``sandloom.tenant_id``
itself) is pinned in ``test_tenant_rls_bypass.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import (
    Column,
    MetaData,
    Table,
    Text,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import bind_tenant, unscoped
from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls
from sandloom.core.tests.test_tenant_rls import (  # noqa: F401 - fixture re-export
    TENANT_A,
    TENANT_B,
    _Env,
    _load_env,
    rls,
)

pytestmark = pytest.mark.asyncio


def _table(env: _Env) -> Table:
    return Table(
        env.table,
        MetaData(),
        Column("id", PG_UUID(as_uuid=True), primary_key=True),
        Column("tenant_id", PG_UUID(as_uuid=True)),
        Column("name", Text),
    )


def _plain_model(env: _Env) -> Any:
    """An ORM mapping that is NOT TenantScoped: no ORM guard, only RLS filters."""

    class _B(DeclarativeBase):
        pass

    class Note(_B):
        __tablename__ = env.table
        id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
        tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True))
        name: Mapped[str] = mapped_column(Text)

    return Note


async def _all_names(env: _Env) -> list[str]:
    """Ground truth through the superuser (RLS ignored)."""
    async with env.admin.connect() as conn:
        rows = await conn.execute(text(f"SELECT name FROM {env.table} ORDER BY name"))
        return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# The mechanism itself
# --------------------------------------------------------------------------- #
async def test_hook_installs_no_engine_events(rls: _Env) -> None:
    sync = rls.app_engine.sync_engine
    assert sync._has_events is False  # no ConnectionEvents → fast execution path
    for name in ("do_execute", "do_executemany", "do_execute_no_params"):
        assert name in sync.dialect.__dict__, name  # instance-level wrapper
    async with rls.app_engine.connect() as conn:
        assert conn.sync_connection is not None
        assert conn.sync_connection._has_events is False


async def test_tenant_rls_off_changes_nothing() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.tests.test_tenant_rls import _admin_url

    plain = create_async_engine(_admin_url())
    try:
        d = plain.sync_engine.dialect
        assert "do_execute" not in d.__dict__
        assert "do_executemany" not in d.__dict__
        assert "do_execute_no_params" not in d.__dict__
        assert plain.sync_engine._has_events is False
    finally:
        await plain.dispose()


# --------------------------------------------------------------------------- #
# SELECT through every path
# --------------------------------------------------------------------------- #
async def test_select_orm_unguarded_mapping(rls: _Env) -> None:
    note = _plain_model(rls)
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            names = (await s.scalars(select(note.name).order_by(note.name))).all()
            assert names == ["a1", "a2"]
            objs = (await s.scalars(select(note))).all()
            assert {o.tenant_id for o in objs} == {TENANT_A}


async def test_select_core_session_and_connection(rls: _Env) -> None:
    t = _table(rls)
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            rows = (await s.execute(select(t.c.name).order_by(t.c.name))).all()
            assert [r[0] for r in rows] == ["a1", "a2"]
        async with rls.app_engine.connect() as conn:
            rows = (await conn.execute(select(t.c.name).order_by(t.c.name))).all()
            assert [r[0] for r in rows] == ["a1", "a2"]
        async with rls.app_engine.begin() as conn:
            assert (await conn.execute(select(t.c.name))).all() != []


async def test_select_text_and_exec_driver_sql(rls: _Env) -> None:
    with bind_tenant(TENANT_B):
        async with rls.app_engine.connect() as conn:
            r = await conn.execute(text(f"SELECT name FROM {rls.table}"))
            assert [x[0] for x in r] == ["b1"]
            r = await conn.exec_driver_sql(f"SELECT name FROM {rls.table}")
            assert [x[0] for x in r] == ["b1"]
            # no_parameters=True → dialect.do_execute_no_params
            np = await conn.execution_options(no_parameters=True)
            r = await np.exec_driver_sql(f"SELECT name FROM {rls.table}")
            assert [x[0] for x in r] == ["b1"]


async def test_select_streaming_server_side_cursor(rls: _Env) -> None:
    t = _table(rls)
    with bind_tenant(TENANT_A):
        async with rls.app_engine.connect() as conn:
            result = await conn.stream(select(t.c.name).order_by(t.c.name))
            assert [r[0] async for r in result] == ["a1", "a2"]
        async with rls.sessions() as s, s.begin():
            result = await s.stream(text(f"SELECT name FROM {rls.table}"))
            assert sorted([r[0] async for r in result]) == ["a1", "a2"]


async def test_select_session_connection_and_run_sync(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            conn = await s.connection()
            r = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
            assert r.scalar_one() == 2

            def _sync_count(sync_conn: Any) -> int:
                return int(
                    sync_conn.exec_driver_sql(
                        f"SELECT count(*) FROM {rls.table}"
                    ).scalar_one()
                )

            assert await conn.run_sync(_sync_count) == 2


async def test_select_inside_savepoints(rls: _Env) -> None:
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            async with s.begin_nested():
                assert await rls.names(s) == ["a1", "a2"]
                with bind_tenant(TENANT_B):
                    async with s.begin_nested():
                        assert await rls.names(s) == ["b1"]
            assert await rls.names(s) == ["a1", "a2"]


async def test_raw_rollback_to_savepoint_forces_resync(rls: _Env) -> None:
    # Savepoint driven by RAW SQL (no Connection API, no compiled clause): the
    # hook still notices the ROLLBACK TO and re-syncs the reverted setting.
    with bind_tenant(TENANT_A):
        async with rls.app_engine.begin() as conn:
            await conn.exec_driver_sql(f"SELECT 1 FROM {rls.table} LIMIT 1")
            await conn.exec_driver_sql("SAVEPOINT raw_sp")
            with bind_tenant(TENANT_B):
                r = await conn.exec_driver_sql(f"SELECT name FROM {rls.table}")
                assert [x[0] for x in r] == ["b1"]
                await conn.exec_driver_sql("ROLLBACK TO SAVEPOINT raw_sp")
                # Server is back at tenant A; the hook must push B again.
                r = await conn.exec_driver_sql(f"SELECT name FROM {rls.table}")
                assert [x[0] for x in r] == ["b1"]


# --------------------------------------------------------------------------- #
# INSERT / UPDATE / DELETE through every write path
# --------------------------------------------------------------------------- #
async def test_insert_orm_core_text_executemany_insertmanyvalues(rls: _Env) -> None:
    t = _table(rls)
    note = _plain_model(rls)
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            # ORM unit of work (several objects → insertmanyvalues batch)
            s.add_all(
                [
                    note(id=uuid.uuid4(), tenant_id=TENANT_A, name=f"orm{i}")
                    for i in range(3)
                ]
            )
            await s.flush()
        async with rls.app_engine.begin() as conn:
            # Core single insert
            await conn.execute(
                insert(t).values(id=uuid.uuid4(), tenant_id=TENANT_A, name="core")
            )
            # Core executemany with RETURNING → insertmanyvalues
            res = await conn.execute(
                insert(t).returning(t.c.id),
                [
                    {"id": uuid.uuid4(), "tenant_id": TENANT_A, "name": f"imv{i}"}
                    for i in range(3)
                ],
            )
            assert len(res.all()) == 3
            # text() executemany → dialect.do_executemany
            await conn.execute(
                text(f"INSERT INTO {rls.table} VALUES (:id, :t, :n)"),
                [{"id": uuid.uuid4(), "t": TENANT_A, "n": f"em{i}"} for i in range(2)],
            )
            await conn.exec_driver_sql(
                f"INSERT INTO {rls.table} VALUES ($1, $2, 'drv')",
                (uuid.uuid4(), TENANT_A),
            )
    names = await _all_names(rls)
    for n in ("orm0", "core", "imv2", "em1", "drv"):
        assert n in names


@pytest.mark.parametrize(
    "path", ["orm", "core", "executemany", "imv", "text", "driver_sql"]
)
async def test_cross_tenant_insert_rejected_on_every_path(rls: _Env, path: str) -> None:
    t = _table(rls)
    note = _plain_model(rls)
    row = {"id": uuid.uuid4(), "tenant_id": TENANT_B, "name": "evil"}
    with bind_tenant(TENANT_A):
        with pytest.raises(DBAPIError, match="row-level security"):
            if path == "orm":
                async with rls.sessions() as s, s.begin():
                    s.add(note(**row))
            else:
                async with rls.app_engine.begin() as conn:
                    if path == "core":
                        await conn.execute(insert(t).values(**row))
                    elif path == "executemany":
                        await conn.execute(
                            text(
                                f"INSERT INTO {rls.table} VALUES (:id, :tenant_id, :name)"
                            ),
                            [row, {**row, "id": uuid.uuid4()}],
                        )
                    elif path == "imv":
                        await conn.execute(
                            insert(t).returning(t.c.id),
                            [row, {**row, "id": uuid.uuid4()}],
                        )
                    elif path == "text":
                        await conn.execute(
                            text(
                                f"INSERT INTO {rls.table} VALUES (:id, :tenant_id, :name)"
                            ),
                            row,
                        )
                    else:
                        await conn.exec_driver_sql(
                            f"INSERT INTO {rls.table} VALUES ($1, $2, $3)",
                            (row["id"], row["tenant_id"], row["name"]),
                        )
    assert "evil" not in await _all_names(rls)


@pytest.mark.parametrize("path", ["orm", "core", "text", "driver_sql", "savepoint"])
async def test_update_and_delete_see_only_bound_tenant(rls: _Env, path: str) -> None:
    t = _table(rls)
    note = _plain_model(rls)
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            if path == "orm":
                upd = await s.execute(update(note).values(name="x"))
                dele = await s.execute(delete(note).where(note.name == "b1"))
            elif path == "core":
                upd = await s.execute(update(t).values(name=t.c.name))
                dele = await s.execute(delete(t).where(t.c.name == "b1"))
            elif path == "text":
                upd = await s.execute(text(f"UPDATE {rls.table} SET name = name"))
                dele = await s.execute(
                    text(f"DELETE FROM {rls.table} WHERE name = 'b1'")
                )
            elif path == "savepoint":
                async with s.begin_nested():
                    upd = await s.execute(text(f"UPDATE {rls.table} SET name = name"))
                    dele = await s.execute(
                        text(f"DELETE FROM {rls.table} WHERE name = 'b1'")
                    )
            else:
                conn = await s.connection()
                upd = await conn.exec_driver_sql(f"UPDATE {rls.table} SET name = name")
                dele = await conn.exec_driver_sql(
                    f"DELETE FROM {rls.table} WHERE name = 'b1'"
                )
            assert upd.rowcount == 2  # type: ignore[attr-defined]  # only A's rows
            assert dele.rowcount == 0  # type: ignore[attr-defined]  # B's invisible
    assert "b1" in await _all_names(rls)


async def test_no_tenant_fails_closed_for_every_operation(rls: _Env) -> None:
    t = _table(rls)
    async with rls.app_engine.begin() as conn:
        assert (await conn.execute(select(t))).all() == []
        assert (await conn.execute(update(t).values(name="x"))).rowcount == 0
        assert (await conn.execute(delete(t))).rowcount == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        async with rls.app_engine.begin() as conn:
            await conn.execute(
                insert(t).values(id=uuid.uuid4(), tenant_id=TENANT_A, name="n")
            )
    assert await _all_names(rls) == ["a1", "a2", "b1"]


# --------------------------------------------------------------------------- #
# NOT covered: the raw driver connection — PostgreSQL must fail closed there
# --------------------------------------------------------------------------- #
async def _raw_ops(raw: Any, table: str, guc: str | None) -> dict[str, Any]:
    """Tenant-A-targeted DML on the raw asyncpg connection with ``guc`` set."""
    out: dict[str, Any] = {}
    tr = raw.transaction()
    await tr.start()
    try:
        if guc is not None:
            await raw.execute("SELECT set_config('sandloom.tenant_id', $1, true)", guc)
        out["select"] = await raw.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1", TENANT_A
        )
        out["update"] = await raw.execute(
            f"UPDATE {table} SET name = name WHERE tenant_id = $1", TENANT_A
        )
        out["delete"] = await raw.execute(
            f"DELETE FROM {table} WHERE tenant_id = $1", TENANT_A
        )
        try:
            async with raw.transaction():  # savepoint: keep the outer tx usable
                await raw.execute(
                    f"INSERT INTO {table} VALUES (gen_random_uuid(), $1, 'raw')",
                    TENANT_A,
                )
            out["insert"] = "ok"
        except Exception as e:  # noqa: BLE001 - the error is the observation
            out["insert"] = type(e).__name__ + ": " + str(e)
    except Exception as e:  # noqa: BLE001 - a cast error aborts everything
        out["error"] = type(e).__name__ + ": " + str(e)
    finally:
        await tr.rollback()
    return out


@pytest.mark.parametrize(
    "guc",
    [None, "", "not-a-uuid", str(TENANT_B)],
    ids=["unset", "empty", "malformed", "other"],
)
async def test_raw_driver_connection_is_not_covered_but_fails_closed(
    rls: _Env, guc: str | None
) -> None:
    with bind_tenant(TENANT_A):  # bound in Python — invisible to the raw driver
        async with rls.app_engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            out = await _raw_ops(raw, rls.table, guc)
    # The policy casts only a canonical UUID (else NULL): every case, the
    # malformed one included, fails closed WITHOUT an error.
    assert "error" not in out, out
    assert out["select"] == 0
    assert out["update"] == "UPDATE 0"
    assert out["delete"] == "DELETE 0"
    assert "row-level security" in out["insert"]
    assert await _all_names(rls) == ["a1", "a2", "b1"]


# --------------------------------------------------------------------------- #
# The pre-fix bypass setting is inert; unscoped() does not lift RLS
# --------------------------------------------------------------------------- #
async def test_legacy_bypass_setting_is_inert_on_every_path(rls: _Env) -> None:
    async with rls.app_engine.connect() as conn:
        raw: Any = (await conn.get_raw_connection()).driver_connection
        async with raw.transaction():
            await raw.execute("SELECT set_config('sandloom.tenant_bypass', 'on', true)")
            assert await raw.fetchval(f"SELECT count(*) FROM {rls.table}") == 0
    with bind_tenant(TENANT_A):
        async with rls.app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('sandloom.tenant_bypass', 'on', true)")
            )
            r = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
            assert r.scalar_one() == 2  # still only tenant A


async def test_unscoped_does_not_lift_rls(rls: _Env) -> None:
    with unscoped():
        async with rls.app_engine.connect() as conn:
            r = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
            assert r.scalar_one() == 0
        with bind_tenant(TENANT_B):
            async with rls.app_engine.connect() as conn:
                r = await conn.execute(text(f"SELECT count(*) FROM {rls.table}"))
                assert r.scalar_one() == 1


async def test_install_is_idempotent_on_the_dialect(rls: _Env) -> None:
    d = rls.app_engine.sync_engine.dialect
    before = d.do_execute
    install_tenant_rls(rls.app_engine)
    assert d.do_execute is before  # not double-wrapped


async def test_no_parameters_path_as_first_statement(rls: _Env) -> None:
    # dialect.do_execute_no_params must sync too: here it is the FIRST statement
    # of the transaction, so nothing else could have set the tenant before it.
    with bind_tenant(TENANT_B):
        async with rls.app_engine.connect() as conn:
            np = await conn.execution_options(no_parameters=True)
            r = await np.exec_driver_sql(f"SELECT name FROM {rls.table}")
            assert [x[0] for x in r] == ["b1"]


async def test_api_savepoint_rollback_reverting_a_switch_inside_it(rls: _Env) -> None:
    # SAVEPOINT taken under A; B synced INSIDE it; ROLLBACK TO SAVEPOINT puts the
    # server back on A while B is still bound → the hook must re-sync B.
    with bind_tenant(TENANT_A):
        async with rls.sessions() as s, s.begin():
            assert await rls.names(s) == ["a1", "a2"]
            nested = await s.begin_nested()
            # The SAVEPOINT is emitted lazily, with the next statement: run one
            # under A so the savepoint really starts at tenant A.
            assert await rls.names(s) == ["a1", "a2"]
            with bind_tenant(TENANT_B):
                assert await rls.names(s) == ["b1"]
                await nested.rollback()
                assert await rls.names(s) == ["b1"]
            assert await rls.names(s) == ["a1", "a2"]


async def test_hook_creates_no_reference_cycles(rls: _Env) -> None:
    """The per-transaction state must not create cyclic garbage (a first
    version kept the RootTransaction on the Connection: +5 cyclic objects per
    request → more GC pauses, worse p99)."""
    import gc

    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def cycles(sessions: Any) -> int:
        for _ in range(5):  # warm-up
            async with sessions() as s, s.begin():
                await rls.names(s)
        gc.collect()
        gc.disable()
        try:
            for _ in range(50):
                async with sessions() as s, s.begin():
                    await rls.names(s)
            return gc.collect()
        finally:
            gc.enable()

    plain_sessions = async_sessionmaker(rls.unhooked, expire_on_commit=False)
    with bind_tenant(TENANT_A):
        # min of two windows: unrelated garbage finalised meanwhile (other
        # tests' engines) must not flake it; the cycle bug was 6 per txn.
        hooked = min([await cycles(rls.sessions), await cycles(rls.sessions)])
        plain = min([await cycles(plain_sessions), await cycles(plain_sessions)])
    assert hooked <= plain + 10, (hooked, plain)
