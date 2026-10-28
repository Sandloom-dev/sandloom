"""Per-process database pool gauges (``sandloom_db_pool_*``).

A background task samples every database provider's pool occupancy every
``_SAMPLE_INTERVAL_SECONDS`` and sets ``livesum`` gauges, so a Gunicorn scrape
reports the service total across its live workers. Sampling — not checkout /
checkin events — keeps the gauges off the request path entirely; a burst
shorter than the interval is visible in ``sandloom_db_pool_checkout_waiters`` only
if it overlaps a sample (pair it with the HTTP latency histogram).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sandloom.core.observability.metrics import (
    db_pool_checkout_waiters,
    db_pool_connections,
    db_pool_max_connections,
    db_pool_overflow,
)

_SAMPLE_INTERVAL_SECONDS = 5.0
_logger = logging.getLogger(__name__)


#: (database, pool) -> resolved gauge children, in ``_FIELDS`` order.
_CHILDREN: dict[tuple[str, str], tuple[Any, ...]] = {}
_FIELDS = ("checked_out", "idle", "max", "overflow", "waiters")


def _children(database: str, pool: str) -> tuple[Any, ...]:
    children = _CHILDREN.get((database, pool))
    if children is None:
        children = (
            db_pool_connections.labels(database, pool, "checked_out"),
            db_pool_connections.labels(database, pool, "idle"),
            db_pool_max_connections.labels(database, pool),
            db_pool_overflow.labels(database, pool),
            db_pool_checkout_waiters.labels(database, pool),
        )
        _CHILDREN[(database, pool)] = children
    return children


def sample_db_pools(core: Any) -> int:
    """Set the pool gauges from every database provider; returns pools seen."""
    seen = 0
    try:
        names = list(core.infra.instances("database"))
    except Exception:  # noqa: BLE001 - no registry bound: nothing to sample
        return 0
    for name in names:
        try:
            stats_fn = getattr(core.database(name), "pool_stats", None)
            rows = stats_fn() if callable(stats_fn) else []
        except Exception as exc:  # noqa: BLE001 - a metric must never fail
            _logger.debug("pool sample failed for database=%s: %s", name, exc)
            continue
        for row in rows:
            children = _children(name, str(row.get("pool", "main")))
            for child, field in zip(children, _FIELDS, strict=True):
                child.set(row.get(field, 0))
            seen += 1
    return seen


async def _sample_loop(core: Any, stop: asyncio.Event, interval: float) -> None:
    while not stop.is_set():
        sample_db_pools(core)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


def start_pool_sampler(
    core: Any, *, interval: float = _SAMPLE_INTERVAL_SECONDS
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Start the sampler; the caller sets the event and awaits the task."""
    stop = asyncio.Event()
    task = asyncio.get_running_loop().create_task(_sample_loop(core, stop, interval))
    return task, stop


__all__ = ["sample_db_pools", "start_pool_sampler"]
