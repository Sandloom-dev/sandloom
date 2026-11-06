"""Infra-agnostic cachegrind rungs (``X*``): a saga step and a compile miss.

Installed into ``ladder.py`` like ``hot_paths.py``. Added for the A/B of the
infra-agnostic branch against ``main`` (``design/infra-agnostic/cachegrind.md``):
the other measured paths already have rungs.

=========  ==================================================================
rung       one op
=========  ==================================================================
``XSgS``   ``saga_rung``'s ``S1``: a business UoW (one INSERT) +
           ``start_saga`` (instance INSERT, outbox nudge), post-commit drained
``XSgL``   ``saga_rung``'s ``L1``: one advance of a local step (lock, savepoint,
           step upsert, instance update, outbox nudge, COMMIT)
``XSgC``   ``saga_rung``'s ``C1``: one advance of a command step (+ the
           provider's command-log claim and result update)
``XCmp``   a compile-cache MISS: a fresh ``select`` with comparisons, ``IN``
           and ``coalesce`` compiled for the asyncpg dialect, with
           ``sandloom.sdk.data`` imported (every global ``@compiles`` hook
           Sandloom registers is installed). No I/O.
=========  ==================================================================

The saga rungs use their own database, ``<DATABASE_URL's database>_saga``
(created and truncated by ``saga_rung._prepare`` at every start, so the 0-op
and K-op runs of ``cgdriver.py`` start from the same state). Their instances
are started before the measured ops, a fixed number (``CG_K`` + 40) whatever
``--n`` is, so the setup cancels in Ir(K) - Ir(0)::

    CG_K=200 ladder/cg/run_cg.sh XSgS XSgL XSgC XCmp
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Any

#: Pre-started saga instances per step rung: warm-up (20) + CG_K + slack.
_POOL = int(os.environ.get("CG_K", "200")) + 40


async def _saga(_ns: Any, kind: str) -> Any:
    if "sandloom.core.config.settings" in sys.modules:
        raise RuntimeError("the saga env must be set before importing sandloom")
    url = os.environ["DATABASE_URL"].rstrip("/")
    server, name = url.rsplit("/", 1)
    os.environ["SAGA_RUNG_DB"] = f"{name}_saga"
    import saga_rung

    saga_rung.SERVER = url
    saga_rung.DB = os.environ["SAGA_RUNG_DB"]
    saga_rung._setup_env()
    await saga_rung._prepare(server)
    from sqlalchemy import text

    from sandloom.core.unit_of_work import db_session
    from sandloom.core.unit_of_work.dispatch import drain_post_commit

    _core, runtime, (local, command, rung_input) = saga_rung._build()
    tag = uuid.uuid4().hex[:8]

    if kind == "start":

        async def op(i: int) -> None:
            async with db_session() as db:
                await db.execute(
                    text("INSERT INTO bench_saga_rows (id, value) VALUES (:id, :v)"),
                    {"id": f"s-{tag}-{i}", "v": i},
                )
                await runtime.start(
                    local, saga_id=f"s-{tag}-{i}", input=rung_input(n=i)
                )
            await drain_post_commit()

        return op, _noop

    definition = local if kind == "local" else command
    ids: list[uuid.UUID] = []
    for i in range(_POOL):
        async with db_session():
            handle = await runtime.start(
                definition, saga_id=f"{kind}-{tag}-{i}", input=rung_input(n=i)
            )
        ids.append(handle.instance_id)
    await drain_post_commit()

    async def advance(i: int) -> None:
        async with db_session() as db:
            await runtime.advance(db, ids[i], 0, tenant=None)
        await drain_post_commit()

    return advance, _noop


async def _noop() -> None:
    return None


async def _compile(_ns: Any) -> Any:
    from sqlalchemy import column, func, select, table
    from sqlalchemy.dialects.postgresql import asyncpg

    import sandloom.sdk.data  # noqa: F401 - installs Sandloom's @compiles hooks

    items = table("items", column("id"), column("owner"), column("score"))
    dialect = asyncpg.dialect()

    async def op(i: int) -> str:
        stmt = select(items.c.id, func.coalesce(items.c.score, 0)).where(
            items.c.owner == i,
            items.c.score > 5,
            items.c.id.in_([1, 2, 3]),
            func.coalesce(items.c.score, 0) < 100,
        )
        return str(stmt.compile(dialect=dialect))

    return op, _noop


def install(ns: Any) -> None:
    """Register the stand's rungs in ``ladder.RUNGS`` (``ns`` = ladder module)."""

    def add(name: str, desc: str, build: Any, *args: Any) -> None:
        async def factory() -> Any:
            return await build(ns, *args)

        ns.RUNGS[name] = (desc, factory)

    add("XSgS", "saga start: business UoW + start_saga (saga_rung S1)", _saga, "start")
    add("XSgL", "one local saga step advance (saga_rung L1)", _saga, "local")
    add("XSgC", "one command saga step advance (saga_rung C1)", _saga, "command")
    add("XCmp", "compile-cache miss: select + IN + coalesce, no I/O", _compile)
