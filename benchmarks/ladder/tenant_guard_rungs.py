"""Tenant guard cachegrind rungs (``XTg*``): a guarded ORM write, a guarded ORM
read and the ``tenant_upsert`` helper, in-process on the kernel's own engine.

Added for the A/B of the tenant-guard change of ``tenant-upsert-helper`` against
``main`` (the section "Measured cost of the guard" of
``docs/adr/0018-tenant-guard-at-the-cursor.md``, with the exact commands): every statement of a
``TenantScoped`` model passes the ORM guard (``do_orm_execute``), the mapper and
``before_flush`` events and the cursor rule (``check_tenant_reach``) that change
touches. No HTTP app, no authentication, no claims: only what a request does to
the tenant guard.

=========  ==================================================================
rung       one op
=========  ==================================================================
``XTgW``   a new session as the bound tenant: ``add`` one row, ``commit`` (the
           flush INSERT through ``before_flush``, the mapper events and the
           cursor's flush-write check)
``XTgR``   a new session as the bound tenant: ``select`` one row by primary
           key (the tenant criterion, the loader criteria, the cursor's
           criterion check, the load stamp)
``XTgU``   ``tenant_upsert`` of one existing row as the bound tenant, then
           ``commit`` (the helper's statement and the cursor's rebuild of it).
           Only on a checkout that has the helper
=========  ==================================================================

The rungs use the database ``DATABASE_URL`` names and one table of their own
(``tg_rung_items``, dropped and created at every start, so the 0-op and K-op
runs of a cachegrind pair start from the same state). The seeded rows are a
fixed number (``CG_K`` + 40) whatever ``--n`` is, so the setup cancels in
Ir(K) - Ir(0).

The rungs are not registered in ``ladder.py``: this file is its own entry, the
same argument convention as ``ladder.py`` (it installs the rungs into it, then
runs it). Ir/op is the difference of two cachegrind runs, as ``cg/cgdriver.py``
does it::

    valgrind --tool=cachegrind --cache-sim=no --cachegrind-out-file=/dev/null \\
        python benchmarks/ladder/tenant_guard_rungs.py cg XTgW --n 0
    valgrind ... python benchmarks/ladder/tenant_guard_rungs.py cg XTgW --n 200
    # Ir/op = (the second Ir - the first Ir) / 200

``PYTHONHASHSEED=0``, inside the stand's image (``ladder/cg/Dockerfile``) with the
mounts and ``PYTHONPATH`` of ``cg/run_cg.sh``. The model is defined inside the
rung, not at import, and this module takes no ``from __future__ import
annotations`` (a mapped annotation is read where the class is defined): a
``TenantScoped`` model changes the guard's hot path for every statement of the
process.
"""

import os
import sys
import uuid
from typing import Any

#: Rows seeded per run: warm-up (20) + CG_K + slack. A fixed number whatever ``--n``
#: is (the setup of a pair of runs is the same); ``XTgR`` and ``XTgU`` address row
#: ``i % _ROWS``, so an ``--n`` past the seeded rows (``ladder.py``'s default is
#: 5000) recycles rows instead of reading or upserting one that does not exist.
_ROWS = int(os.environ.get("CG_K", "200")) + 40
#: Ids of the rows ``XTgW`` inserts start here, far above the seeded ones.
_NEW = 10_000_000


def _setup_env() -> None:
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the rung env must be set before importing sandloom")
    os.environ.setdefault("ENVIRONMENT", "dev")
    os.environ.setdefault("SANDLOOM_MODULES", "[]")
    os.environ.setdefault("JWT_SECRET", "tenant-guard-rung-not-a-secret-00000000")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


async def _items(_ns: Any, kind: str) -> Any:
    _setup_env()
    from sqlalchemy import BigInteger, Text, UniqueConstraint, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    from sandloom.core.di import bind_tenant, unscoped
    from sandloom.core.orm import TenantScoped
    from sandloom.core.sql_alchemy.write_admission import install_write_admission

    class Base(DeclarativeBase):
        pass

    class Item(TenantScoped, Base):
        __tablename__ = "tg_rung_items"
        __table_args__ = (UniqueConstraint("tenant_id", "key"),)

        id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
        key: Mapped[str] = mapped_column(Text)
        name: Mapped[str] = mapped_column(Text)

    engine = create_async_engine(os.environ["DATABASE_URL"])
    install_write_admission(engine)  # the cursor rule every kernel engine carries
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tenant = uuid.uuid4()
    with unscoped():  # schema DDL names every table: system work (ADR-0018)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    async with maker() as session:
        with bind_tenant(tenant):
            session.add_all(Item(id=i, key=f"k{i}", name="seed") for i in range(_ROWS))
            await session.commit()

    async def teardown() -> None:
        with unscoped():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    if kind == "write":

        async def write(i: int) -> None:
            async with maker() as session:
                with bind_tenant(tenant):
                    session.add(Item(id=_NEW + i, key=f"n{i}", name="new"))
                    await session.commit()

        return write, teardown

    if kind == "read":

        async def read(i: int) -> None:
            seeded = i % _ROWS  # an existing row whatever --n is (see _ROWS)
            async with maker() as session:
                with bind_tenant(tenant):
                    row = (
                        await session.execute(select(Item).where(Item.id == seeded))
                    ).scalar_one()
                    assert row.key == f"k{seeded}"

        return read, teardown

    from sandloom.core.sql_alchemy.tenant_upsert import tenant_upsert

    async def upsert(i: int) -> None:
        seeded = i % _ROWS  # an existing row whatever --n is (see _ROWS)
        async with maker() as session:
            with bind_tenant(tenant):
                await tenant_upsert(
                    Item,
                    [{"id": seeded, "key": f"k{seeded}", "name": f"u{i}"}],
                    update=["name"],
                    conflict_on=["key"],
                    session=session,
                )
                await session.commit()

    return upsert, teardown


def install(ns: Any) -> None:
    """Register the rungs in ``ladder.RUNGS`` (``ns`` = the ladder module)."""

    def add(name: str, desc: str, kind: str) -> None:
        async def factory() -> Any:
            return await _items(ns, kind)

        ns.RUNGS[name] = (desc, factory)

    add("XTgW", "guarded ORM write: add one row + commit, bound tenant", "write")
    add("XTgR", "guarded ORM read: select one row by primary key", "read")
    add("XTgU", "tenant_upsert of one existing row + commit", "upsert")


if __name__ == "__main__":
    import ladder

    install(ladder)
    ladder.main()
