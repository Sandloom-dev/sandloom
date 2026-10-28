# ruff: noqa: F811  - the `rls` fixture is imported from test_tenant_rls
"""The tenant-setting sync is skipped ONLY for allowlisted system statements.

``non_tenant_statements()`` (the outbox mark-delivered UPDATE and the relay's
claim / settle / sweep) skips the ``set_config`` round trip when no tenant is
bound; every other transaction — tenant-bound or not, autocommit included —
keeps the sync on its first statement. The scope is self-checking: a statement
on a non-allowlisted table, or untagged raw SQL, is refused outside production
and synced (never skipped) in production.
Pinned against a connection that carries a LEFTOVER session-level
``sandloom.tenant_id`` (``SET`` without ``LOCAL``): it must never reach a
no-tenant transaction outside the allowlist.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, column, false, select, table, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sandloom.core.di import bind_tenant
from sandloom.core.sql_alchemy import tenant_rls
from sandloom.core.sql_alchemy.tenant_rls import (
    NonTenantStatementError,
    non_tenant_statements,
)
from sandloom.core.tests.test_tenant_rls import (  # noqa: F401 - fixture re-export
    TENANT_A,
    TENANT_B,
    _Env,
    rls,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def syncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every tenant value the hook sends (one entry = one set_config RT)."""
    sent: list[str] = []
    original = tenant_rls._set_config_sql

    def counting(paramstyle: str, tenant: str) -> tuple[str, Any]:
        sent.append(tenant)
        return original(paramstyle, tenant)

    original_merged = tenant_rls._merged_begin_sql

    def counting_merged(adapter: Any, tenant: str) -> str:
        sent.append(tenant)  # DB_RLS_MERGED_BEGIN: BEGIN + setting, one message
        return original_merged(adapter, tenant)

    monkeypatch.setattr(tenant_rls, "_set_config_sql", counting)
    monkeypatch.setattr(tenant_rls, "_merged_begin_sql", counting_merged)
    return sent


async def _leak_session_guc(conn: Any, tenant: Any) -> None:
    """Leave a SESSION-level sandloom.tenant_id on the pooled connection."""
    await conn.execute(text(f"SET sandloom.tenant_id = '{tenant}'"))
    await conn.commit()


async def test_no_tenant_transaction_outside_the_allowlist_still_syncs(
    rls: _Env, syncs: list[str]
) -> None:
    async with rls.app_engine.connect() as conn:
        await _leak_session_guc(conn, TENANT_A)
        syncs.clear()
        # an ordinary no-tenant transaction on the SAME connection
        guc = (
            await conn.execute(
                text("SELECT current_setting('sandloom.tenant_id', true)")
            )
        ).scalar_one()
        rows = (await conn.execute(text(f"SELECT name FROM {rls.table}"))).all()
        await conn.commit()
    assert syncs == [""]  # cleared by the hook, as before
    assert guc == "" and rows == []  # the leftover tenant A never leaks


#: The allowlisted table, as a Core construct (resolved to a TEMP table of the
#: same name on the pooled connection — see ``outbox``).
OUTBOX = table("outbox_messages", column("id"))
TAGGED = {tenant_rls.NON_TENANT_OPTION: True}


@pytest_asyncio.fixture
async def outbox(rls: _Env) -> _Env:
    """A session TEMP ``outbox_messages`` on the pool-of-one connection (pg_temp
    is searched first), so allowlisted statements run as the app role."""
    async with rls.app_engine.connect() as conn:
        await conn.execute(text("CREATE TEMP TABLE outbox_messages (id uuid)"))
        await conn.commit()
    return rls


def _guc() -> Any:
    return text("SELECT current_setting('sandloom.tenant_id', true)").execution_options(
        **TAGGED
    )


async def test_allowlisted_no_tenant_transaction_skips_the_round_trip(
    outbox: _Env, syncs: list[str]
) -> None:
    async with outbox.app_engine.connect() as conn:
        with non_tenant_statements():
            await conn.execute(select(OUTBOX.c.id))
            await conn.execute(update(OUTBOX).values(id=None).where(false()))
            await conn.execute(text("SELECT 1").execution_options(**TAGGED))
        await conn.commit()
    assert syncs == []


async def test_untagged_raw_sql_in_the_scope_is_refused(
    rls: _Env, syncs: list[str]
) -> None:
    async with rls.app_engine.connect() as conn:
        with (
            non_tenant_statements(),
            pytest.raises(NonTenantStatementError, match="raw SQL"),
        ):
            await conn.execute(text("SELECT 1"))
    assert syncs == []


async def test_a_tenant_table_in_the_scope_is_refused(
    outbox: _Env, syncs: list[str]
) -> None:
    notes = table(outbox.table, column("name"))
    async with outbox.app_engine.connect() as conn:
        with (
            non_tenant_statements(),
            pytest.raises(NonTenantStatementError, match=outbox.table),
        ):
            # a join smuggling the tenant table in next to the allowlisted one
            await conn.execute(
                select(OUTBOX.c.id).where(OUTBOX.c.id.in_(select(notes.c.name)))
            )
    assert syncs == []


async def test_in_production_a_violation_is_synced_never_skipped(
    rls: _Env, syncs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.observability.metrics import (
        tenant_rls_non_tenant_violations_total,
    )

    monkeypatch.setattr(tenant_rls, "_strict_non_tenant", lambda: False)
    before = tenant_rls_non_tenant_violations_total._value.get()
    notes = table(rls.table, column("name"))
    async with rls.app_engine.connect() as conn:
        await _leak_session_guc(conn, TENANT_A)
        syncs.clear()
        with non_tenant_statements():
            rows = (await conn.execute(select(notes.c.name))).all()
            raw = (await conn.execute(text(f"SELECT name FROM {rls.table}"))).all()
        await conn.commit()
    # fail-safe: the setting was cleared ('' synced) exactly as outside the
    # scope; the leftover tenant A never reaches the statements
    assert syncs == [""]
    assert rows == [] and raw == []
    assert tenant_rls_non_tenant_violations_total._value.get() == before + 2


async def test_allowlist_never_skips_a_bound_tenant(
    outbox: _Env, syncs: list[str]
) -> None:
    async with outbox.app_engine.connect() as conn:
        await _leak_session_guc(conn, TENANT_B)
        syncs.clear()
        with bind_tenant(TENANT_A), non_tenant_statements():
            await conn.execute(select(OUTBOX.c.id))
            guc = (await conn.execute(_guc())).scalar_one()
        await conn.commit()
    assert syncs == [str(TENANT_A)]
    assert guc == str(TENANT_A)


async def test_allowlist_clears_a_tenant_set_earlier_in_the_transaction(
    outbox: _Env, syncs: list[str]
) -> None:
    async with outbox.app_engine.connect() as conn:
        with bind_tenant(TENANT_A):
            await conn.execute(text("SELECT 1"))
        with non_tenant_statements():  # tenant A is LOCAL to this transaction
            await conn.execute(select(OUTBOX.c.id))
            guc = (await conn.execute(_guc())).scalar_one()
        await conn.commit()
    assert syncs == [str(TENANT_A), ""]
    assert guc == ""


async def test_allowlist_does_not_outlive_its_block(
    rls: _Env, syncs: list[str]
) -> None:
    async with rls.app_engine.connect() as conn:
        await _leak_session_guc(conn, TENANT_A)
        with non_tenant_statements():
            await conn.execute(text("SELECT 1").execution_options(**TAGGED))
        await conn.commit()
        syncs.clear()
        rows = (await conn.execute(text(f"SELECT name FROM {rls.table}"))).all()
        await conn.commit()
    assert syncs == [""] and rows == []


async def test_statement_tables_include_orm_eager_loads() -> None:
    """The check sees tables an ORM loader strategy adds at compile time."""

    class Base(DeclarativeBase):
        pass

    class Parent(Base):
        __tablename__ = "outbox_messages"
        id: Mapped[int] = mapped_column(primary_key=True)
        kids: Mapped[list[Kid]] = relationship(lazy="joined")

    class Kid(Base):
        __tablename__ = "tenant_kids"
        id: Mapped[int] = mapped_column(primary_key=True)
        pid: Mapped[int] = mapped_column(ForeignKey("outbox_messages.id"))

    compiled = select(Parent).compile(dialect=postgresql.dialect())
    context = SimpleNamespace(compiled=compiled)
    assert tenant_rls._statement_tables(context) == {"outbox_messages", "tenant_kids"}
    # cached per compiled form
    assert tenant_rls._statement_tables(context) is tenant_rls._statement_tables(
        context
    )


async def test_autocommit_outside_the_allowlist_keeps_todays_behaviour(
    rls: _Env, syncs: list[str]
) -> None:
    async with rls.app_engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        with bind_tenant(TENANT_A):
            rows = (await auto.execute(text(f"SELECT name FROM {rls.table}"))).all()
    # the hook still sends the setting; a transaction-local value cannot reach
    # an autocommit statement, so RLS sees '' and fails closed
    assert syncs == [str(TENANT_A)] and rows == []


async def test_mark_delivered_on_the_autocommit_view_skips_the_round_trip(
    rls: _Env, syncs: list[str]
) -> None:
    """The post-commit mark runs on the store's cached AUTOCOMMIT view of the
    engine: still inside the allowlist (no tenant round trip), and a leftover
    session-level tenant never reaches it."""
    import uuid

    from sandloom.core.sql_alchemy import outbox_store
    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork

    row = uuid.uuid4()
    async with rls.app_engine.connect() as conn:
        await conn.execute(
            text(
                "CREATE TEMP TABLE outbox_messages (id uuid, delivered_at "
                "timestamp, dead_at timestamp, last_error text)"
            )
        )
        await conn.execute(
            text("INSERT INTO outbox_messages (id) VALUES (:id)"), {"id": row}
        )
        await conn.commit()
        await _leak_session_guc(conn, TENANT_A)
    syncs.clear()

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=rls.sessions)

    await outbox_store.SQLAlchemyOutboxStore().mark_delivered(factory, [row])
    assert outbox_store._autocommit_view(factory) is not None
    assert syncs == []
    async with rls.app_engine.connect() as conn:
        delivered = (
            await conn.execute(
                text("SELECT delivered_at FROM outbox_messages WHERE id = :id"),
                {"id": row},
            )
        ).scalar_one()
    assert delivered is not None
