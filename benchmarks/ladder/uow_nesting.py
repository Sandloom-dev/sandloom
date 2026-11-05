"""Unit-of-work open+close microbench: the cost of the nesting decision.

    DATABASE_URL=postgresql+asyncpg://... uv run python benchmarks/ladder/uow_nesting.py

A unit of the database is already open in the context (it wrote nothing, as
a request unit before its first statement), then ``N`` units of the same
factory are opened and closed with no statement (begin and commit of an
unbegun session: no I/O). The number is wall time per open+close, best of
``--rounds`` runs of ``--n`` units each. On PostgreSQL the nesting policy is
"independent units": the number shows what the nesting decision adds to the
plain path (slice 8c: 27.33 us without the host lookup, 27.79 us with it;
risk fix 6 moved the decision into ``sql_alchemy/nesting.py``).

``--bare`` opens the units with no unit bound in the context (the lookup's
cheap exit). Not a CPU stand: run it on an idle machine, compare A/B in one
sitting.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.unit_of_work.scope import _bind_uow_scope


async def _run(url: str, n: int, rounds: int, bare: bool) -> list[float]:
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="bench",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    factory = provider.uow_factory()
    results: list[float] = []
    try:
        async with factory() as outer:
            session = outer.session
            for _ in range(rounds + 1):  # the first round warms up
                if bare:
                    start = time.perf_counter()
                    for _ in range(n):
                        async with factory():
                            pass
                    took = time.perf_counter() - start
                else:
                    with _bind_uow_scope(session, "default"):
                        start = time.perf_counter()
                        for _ in range(n):
                            async with factory():
                                pass
                        took = time.perf_counter() - start
                results.append(took / n * 1e6)
    finally:
        await provider.aclose()
    return results[1:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=3000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args()
    if not args.url:
        parser.error("DATABASE_URL or --url is required")
    runs = asyncio.run(_run(args.url, args.n, args.rounds, args.bare))
    print(
        f"unit open+close ({'no unit' if args.bare else 'a unit'} in the context): "
        f"best {min(runs):.2f} us of {args.rounds} x {args.n}; "
        + ", ".join(f"{r:.2f}" for r in runs)
    )


if __name__ == "__main__":
    main()
