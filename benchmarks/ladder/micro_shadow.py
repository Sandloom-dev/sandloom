"""Micro-benchmark: cost of the contract-cache SHADOW on a local read-contract call.

ADR-0003 "Then". Times a LOCAL read-contract call (provider own UoW on a fake
in-memory database, so the number is the runtime's own overhead, no I/O)
with the shadow OFF (the default) and ON (TTLs 1 s / 5 s, rank ladder, inside
an HTTP-request tally), interleaved round by round so drift hits both sides
alike. Prints the median ns per call and the delta:

    uv run python ../benchmarks/ladder/micro_shadow.py [--calls N] [--rounds R]

Run on an otherwise idle machine (it is CPU-bound, single-threaded).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI

from sandloom.core import contract_cache_shadow as shadow
from sandloom.core.contract_cache_shadow import ShadowContractCache, ShadowContractConfig
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method

CONTRACT = "microshadow.membership.v1"
ROLES: dict[tuple[UUID, UUID], str] = {}


class Membership:
    @classmethod
    @contract_method
    async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
        return ROLES.get((organization_id, user_id))


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}


class _UoW:
    def __init__(self) -> None:
        self.session: _Session | None = None

    async def __aenter__(self) -> _UoW:
        self.session = _Session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.session = None


class _Database:
    def uow_factory(self) -> Callable[[], _UoW]:
        return _UoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _observer() -> ShadowContractCache:
    return ShadowContractCache(
        {
            CONTRACT: ShadowContractConfig(
                ttls=(1.0, 5.0), rank={"member": 1, "admin": 2, "owner": 3}
            )
        },
        caller="micro",
    )


async def _round(proxy: Any, keys: list[tuple[UUID, UUID]], calls: int) -> float:
    n = len(keys)
    start = time.perf_counter_ns()
    for i in range(calls):
        org, user = keys[i % n]
        await proxy.get_role(org, user)
    return (time.perf_counter_ns() - start) / calls


async def main(calls: int, rounds: int, distinct: int) -> None:
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": _Database()  # type: ignore[method-assign]
    facade.register_service(CONTRACT, Membership)
    keys = [(uuid4(), uuid4()) for _ in range(distinct)]
    ROLES.update({k: "admin" for k in keys})
    proxy = facade.get_service(CONTRACT)
    observer = _observer()
    off: list[float] = []
    on: list[float] = []
    for r in range(rounds + 1):
        for mode in (("off", "on") if r % 2 == 0 else ("on", "off")):
            if mode == "off":
                shadow.configure(None)
                ns = await _round(proxy, keys, calls)
            else:
                shadow.configure(observer)
                tally = shadow._RequestTally()
                token = shadow._REQUEST.set(tally)
                try:
                    ns = await _round(proxy, keys, calls)
                finally:
                    shadow._REQUEST.reset(token)
            if r:  # round 0 is warm-up
                (off if mode == "off" else on).append(ns)
    shadow.configure(None)
    m_off, m_on = statistics.median(off), statistics.median(on)
    hits = sum(v for (c, m, t, o), v in observer.counts.items() if o == "hit")
    total = sum(
        v for (c, m, t, o), v in observer.counts.items() if o in ("hit", "miss")
    )
    # The whole OFF-path cost per read: one for_contract() check.
    import timeit

    shadow.configure(None)
    check_ns = (
        min(timeit.repeat(lambda: shadow.for_contract(CONTRACT), number=200_000, repeat=5))
        / 200_000
        * 1e9
    )
    print(f"calls/round={calls} rounds={rounds} distinct keys={distinct}")
    print(f"OFF-path check (for_contract -> None): {check_ns:,.0f} ns/read")
    print(f"off (default): median {m_off:,.0f} ns/call  (min {min(off):,.0f})")
    print(f"on:            median {m_on:,.0f} ns/call  (min {min(on):,.0f})")
    print(
        f"shadow ON overhead: {m_on - m_off:+,.0f} ns/call "
        f"({100.0 * (m_on - m_off) / m_off:+.1f} %); shadow hit share "
        f"{100.0 * hits / max(total, 1):.1f} % over both TTLs"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--calls", type=int, default=20_000)
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--distinct", type=int, default=64)
    a = p.parse_args()
    asyncio.run(main(a.calls, a.rounds, a.distinct))
