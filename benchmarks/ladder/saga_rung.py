"""Saga rung (ADR-0008 §6, Consequences): what a saga costs per operation.

DIAGNOSTIC ONLY. Runs the REAL saga runtime (``sandloom.core.sagas``) of an app
built by ``create_app`` against PostgreSQL — no HTTP, no bus consumers — and
measures, per operation, wall time (median / p95) and the SQL statements the
application sends (counted on the default and contract engines):

======  ===============================================================
rung    one operation
======  ===============================================================
``B0``  baseline business UoW: INSERT one row, COMMIT
``S1``  the same UoW + ``start_saga`` (instance INSERT + outbox nudge)
        -> **start-path cost** = S1 - B0
``L1``  one advance of a LOCAL step (no-op body in a savepoint):
        lock, step upsert, instance update, outbox nudge, COMMIT
        -> **per-step cost** of the transition itself
``C1``  one advance of a COMMAND step (a local provider through the durable
        command log: claim INSERT + result UPDATE in the provider's own UoW)
        -> per-step cost incl. the command's exactly-once machinery
======  ===============================================================

**Connection hold** — the advance holds one pooled connection and the
instance row lock from the ``SELECT … FOR UPDATE`` to its ``COMMIT``: that
window is the L1 / C1 time (the command runs on the contract pool, not on
this connection). Over internal RPC, add one RPC call (see the RPC stand,
``ladder/STANDS.md``) — bounded by ``SERVICE_RPC_TIMEOUT_SECONDS``.

Run with the backend venv from ``benchmarks/``::

    DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:5433 \\
    CACHE_URL=redis://localhost:6380/198 \\
    ../.venv/bin/python ladder/saga_rung.py --n 1000 \\
        --json ladder/results/saga_rung_$(date +%F).json

``DATABASE_URL`` names the server (no database): the stand creates and uses
``SAGA_RUNG_DB`` (default ``sandloom_bench_saga``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

SERVER = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://test_user:test_password@localhost:5433"
).rstrip("/")
DB = os.environ.get("SAGA_RUNG_DB", "sandloom_bench_saga")
REDIS = os.environ.get("CACHE_URL", "redis://localhost:6380/198")


def _setup_env() -> None:
    server = SERVER.rsplit("/", 1)[0] if SERVER.count("/") > 2 else SERVER
    os.environ["DATABASE_URL"] = f"{server}/{DB}"
    os.environ["CACHE_URL"] = REDIS
    os.environ.setdefault("BROKER_URL", REDIS)
    os.environ["ENVIRONMENT"] = "dev"
    os.environ["SANDLOOM_MODULES"] = "[]"
    os.environ["EVENT_BUS_BACKEND"] = "memory"
    os.environ.setdefault("JWT_SECRET", "saga-rung-stand-not-a-secret-0000000000000")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


async def _prepare(server: str) -> None:
    from sqlalchemy import Column, Integer, MetaData, String, Table, text
    from sqlalchemy.ext.asyncio import create_async_engine

    import sandloom.core.sql_alchemy.models  # noqa: F401 - registers the core tables
    from sandloom.core.sql_alchemy.base import Base

    admin = create_async_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": DB}
            )
        ).first()
        if exists is None:
            await conn.execute(text(f'CREATE DATABASE "{DB}"'))
    await admin.dispose()
    engine = create_async_engine(f"{server}/{DB}")
    meta = MetaData()
    Table(
        "bench_saga_rows",
        meta,
        Column("id", String(64), primary_key=True),
        Column("value", Integer, nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(meta.create_all)
        for table in (
            "saga_steps",
            "saga_instances",
            "outbox_messages",
            "contract_command_log",
            "bench_saga_rows",
        ):
            await conn.execute(text(f"TRUNCATE {table}"))
    await engine.dispose()


def _build() -> tuple[Any, Any, Any]:
    """App core + a runtime with the rung definitions + the counted engines."""
    from pydantic import BaseModel

    from sandloom.core.app_factory import create_app
    from sandloom.core.module_api import Saga, SagaContext, contract_method, local_step, step
    from sandloom.core.sagas import SagaRuntime
    from sandloom.core.sagas.registry import NUDGE_SCHEMA, build_registry

    class RungInput(BaseModel):
        n: int

    class Ledger:
        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def touch(cls, n: int, operation_key: str) -> int:
            return n

        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def untouch(cls, n: int, operation_key: str) -> int:
            return n

    def touch_args(c: SagaContext[RungInput]) -> dict[str, object]:
        return {"n": c.input.n}

    async def noop(db: Any, c: SagaContext[RungInput]) -> None:
        return None

    app = create_app()
    core = app.state.core
    core.register_service("sagarung.ledger.v1", Ledger)
    local = Saga[RungInput](
        "local",
        version=1,
        input=RungInput,
        deadline=timedelta(minutes=30),
        steps=[
            local_step("first", apply=noop, compensate=noop),
            local_step("done", kind="pivot", apply=noop),
        ],
    )
    command = Saga[RungInput](
        "command",
        version=1,
        input=RungInput,
        deadline=timedelta(minutes=30),
        steps=[
            step(
                "touch",
                call="sagarung.ledger.v1.touch",
                args=touch_args,
                compensate="sagarung.ledger.v1.untouch",
            ),
            local_step("done", kind="pivot", apply=noop),
        ],
    )
    for definition in (local, command):
        core.register_saga(definition, "sagarung")
    core.register_event_schema(
        "sagarung.saga_advance.v1", "sagarung.saga_advance.v1", NUDGE_SCHEMA
    )
    runtime = SagaRuntime(core, build_registry(core))
    return core, runtime, (local, command, RungInput)


class Counter:
    """Statements sent on the default and contract engines."""

    def __init__(self, core: Any) -> None:
        from sqlalchemy import event

        provider = core.database("default")
        self.n = 0
        provider.contract_uow_factory()  # resolves the contract pool's engine
        engines = [provider.engine]
        contract = getattr(provider, "_contract_engine", None)  # stand: inspection
        if contract is not None and contract is not provider.engine:
            engines.append(contract)
        for engine in engines:
            event.listen(engine.sync_engine, "before_cursor_execute", self._hit)

    def _hit(self, *_: Any) -> None:
        self.n += 1


async def _measure(
    n: int, op: Callable[[int], Awaitable[None]], counter: Counter
) -> dict[str, float]:
    for i in range(min(50, n)):  # warm-up: pools, statement caches
        await op(-1 - i)
    times: list[float] = []
    before = counter.n
    for i in range(n):
        started = time.perf_counter()
        await op(i)
        times.append((time.perf_counter() - started) * 1e6)
    times.sort()
    return {
        "median_us": round(statistics.median(times), 1),
        "p95_us": round(times[int(len(times) * 0.95) - 1], 1),
        "statements": round((counter.n - before) / n, 2),
    }


async def run(n: int) -> dict[str, Any]:
    from sqlalchemy import text

    from sandloom.core.unit_of_work import db_session

    core, runtime, (local, command, RungInput) = _build()
    counter = Counter(core)
    tag = uuid.uuid4().hex[:8]

    async def business(i: int) -> None:
        async with db_session() as db:
            await db.execute(
                text("INSERT INTO bench_saga_rows (id, value) VALUES (:id, :v)"),
                {"id": f"b-{tag}-{i}", "v": i},
            )

    async def start(i: int, definition: Any = local, prefix: str = "s") -> None:
        async with db_session() as db:
            await db.execute(
                text("INSERT INTO bench_saga_rows (id, value) VALUES (:id, :v)"),
                {"id": f"{prefix}-{tag}-{i}", "v": i},
            )
            await runtime.start(
                definition, saga_id=f"{prefix}-{tag}-{i}", input=RungInput(n=i)
            )

    async def prepared(
        prefix: str, definition: Any
    ) -> Callable[[int], Awaitable[None]]:
        ids: list[uuid.UUID] = []
        for i in range(-60, n):
            async with db_session():
                handle = await runtime.start(
                    definition, saga_id=f"{prefix}-{tag}-{i}", input=RungInput(n=i)
                )
            ids.append(handle.instance_id)
        offset = 60

        async def advance(i: int) -> None:
            instance = ids[offset + i] if i >= 0 else ids[-1 - i]
            async with db_session() as db:
                await runtime.advance(db, instance, 0, tenant=None)

        return advance

    results = {
        "B0": await _measure(n, business, counter),
        "S1": await _measure(n, start, counter),
    }
    results["L1"] = await _measure(n, await prepared("l", local), counter)
    results["C1"] = await _measure(n, await prepared("c", command), counter)
    derived = {
        "start_path_us": round(
            results["S1"]["median_us"] - results["B0"]["median_us"], 1
        ),
        "start_path_statements": round(
            results["S1"]["statements"] - results["B0"]["statements"], 2
        ),
        "per_step_local_us": results["L1"]["median_us"],
        "per_step_command_us": results["C1"]["median_us"],
        "connection_hold_local_us": results["L1"]["median_us"],
        "connection_hold_command_us": results["C1"]["median_us"],
    }
    return {
        "n": n,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "rungs": results,
        "derived": derived,
    }


def _render(report: dict[str, Any]) -> str:
    lines = [f"saga rung  n={report['n']}  python {report['python']}"]
    lines.append(f"{'rung':<6}{'median µs':>12}{'p95 µs':>12}{'stmts/op':>10}")
    for name, row in report["rungs"].items():
        lines.append(
            f"{name:<6}{row['median_us']:>12}{row['p95_us']:>12}{row['statements']:>10}"
        )
    for key, value in report["derived"].items():
        lines.append(f"  {key} = {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--json", default=None, help="write the report here")
    args = parser.parse_args(argv)
    server = SERVER.rsplit("/", 1)[0] if SERVER.count("/") > 2 else SERVER
    _setup_env()
    asyncio.run(_prepare(server))
    report = asyncio.run(run(args.n))
    print(_render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
