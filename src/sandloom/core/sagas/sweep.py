"""The saga timer sweep (ADR-0008 §5): durable timers are a column.

A kernel loop in every process that hosts a saga-owning module — NOT tied to
the outbox relay, which is off by default outside prod: dev drives sagas
exactly as prod does. Per database, one holder at a time runs it, guarded by
a fenced cache lease (:class:`sandloom.core.beat_lease.FencedLease` on
``BEAT_LEASE_CACHE_INSTANCE``; a cache without ``cas`` runs it unleased with a
warning — two sweepers are still safe, only redundant). Each tick:

1. selects active instances whose ``next_attempt_at`` (backoff elapsed) or
   applicable deadline is due and that were not nudged within the last
   interval — ``FOR UPDATE SKIP LOCKED LIMIT SAGA_SWEEP_BATCH`` — and stamps
   their ``swept_at`` (cross-tenant: the system channel under ``tenant_rls``);
2. publishes one nudge per instance through the outbox (``{instance_id,
   expected_version}``); the advance consumer does the rest under the row
   lock, so a nudge racing a running step is a no-op.

Housekeeping, about once a minute on the holder: the ``sandloom_saga_instances``
gauge (one bounded ``GROUP BY``) and, hourly, the retention purge of terminal
instances (``SAGA_RETENTION_DAYS``; parked instances are kept).

An idle tick on a ``tenant_rls`` database rolls its system session back: it
changed nothing, so it leaves no ``sandloom.system_db.session`` audit row (its
start / end log lines remain).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

import sandloom.core.observability.metrics as _metrics
import sandloom.core.sql_alchemy.saga_store as _store
from sandloom.core.beat_lease import FencedLease
from sandloom.core.consumers.runtime import database_identity
from sandloom.core.sagas.channel import CrossTenantSession, cross_tenant_session
from sandloom.core.sagas.registry import nudge_channel
from sandloom.core.sagas.runtime import SagaRuntime
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import publish_event

logger = logging.getLogger("sandloom.sagas.sweep")

LEASE_KEY_PREFIX = "sandloom:saga:sweep:"
#: Ticks between two gauge refreshes / two purges (at the default 5 s
#: interval: one minute / one hour).
GAUGE_EVERY_TICKS = 12
PURGE_EVERY_TICKS = 720
_ACTOR = "kernel:saga-sweep"


class _IdleTick(Exception):
    """Raised inside a system session that changed nothing (rolled back)."""


class SagaSweeper:
    """The lease-guarded sweep loop of one process (all its saga databases)."""

    def __init__(self, runtime: SagaRuntime) -> None:
        self.runtime = runtime
        self.core = runtime.core
        settings = runtime.settings
        self.interval = float(settings.saga_sweep_interval_seconds)
        self.batch = int(settings.saga_sweep_batch)
        self._leases: dict[str, FencedLease | None] = {}
        self._ticks = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Startup task: run the loop in the background (idempotent)."""
        if self._task is not None or not len(self.runtime.registry):
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="sandloom-saga-sweep")

    async def stop(self) -> None:
        """Shutdown task: stop the loop and release the leases."""
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(task, timeout=max(1.0, self.interval))
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for lease in self._leases.values():
            if lease is not None:
                await lease.release_async()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("saga sweep tick failed; retrying next interval")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                continue

    # -- one tick -----------------------------------------------------------
    async def tick(self) -> int:
        """Sweep every saga database once; the number of nudges published."""
        self._ticks += 1
        gauge = self._ticks % GAUGE_EVERY_TICKS == 1
        purge = self._ticks % PURGE_EVERY_TICKS == 1
        nudged = 0
        for database in self.runtime.registry.databases():
            if await self._holds(database):
                nudged += await self.sweep_database(database, gauge=gauge, purge=purge)
        return nudged

    async def _holds(self, database: str) -> bool:
        lease = self._lease(database)
        return True if lease is None else await lease.ensure_async()

    def _lease(self, database: str) -> FencedLease | None:
        if database in self._leases:
            return self._leases[database]
        lease: FencedLease | None = None
        name = self.runtime.settings.beat_lease_cache_instance
        try:
            cache = self.core.cache(name)
        except (KeyError, LookupError, RuntimeError):
            cache = None
        features = cache.features() if cache is not None else frozenset()
        if cache is not None and "cas" in features:
            identity = database_identity(self.core, database)
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
            lease = FencedLease(
                cache,
                LEASE_KEY_PREFIX + digest,
                ttl_seconds=max(3, int(self.interval * 3)),
                label="[saga-sweep]",
            )
        else:
            logger.warning(
                "saga sweep on database/%s runs without a lease: cache %r has no "
                "'cas' feature (several sweepers are safe, only redundant)",
                database,
                name,
            )
        self._leases[database] = lease
        return lease

    async def sweep_database(
        self, database: str, *, gauge: bool = False, purge: bool = False
    ) -> int:
        """Claim the due instances of ``database`` and nudge them.

        ``gauge`` / ``purge`` add the housekeeping (the loop asks for them
        every :data:`GAUGE_EVERY_TICKS` / :data:`PURGE_EVERY_TICKS` ticks).
        """
        now = self.runtime.now()
        due: list[_store.DueInstance] = []
        try:
            async with cross_tenant_session(
                self.core,
                database,
                actor=_ACTOR,
                reason="re-drive due saga instances",
                channel=self.runtime.system_channel,
            ) as cts:
                due = await self._claim(cts, now)
                changed = bool(due)
                if gauge:
                    await self._refresh_gauge(cts)
                if purge:
                    changed = await self._purge(cts, now) or changed
                if not changed and not cts.uow:
                    raise _IdleTick
        except _IdleTick:
            due = []
        if due:
            await self._publish(database, due)
        return len(due)

    async def _claim(
        self, cts: CrossTenantSession, now: datetime
    ) -> list[_store.DueInstance]:
        swept_before = now - timedelta(seconds=self.interval)
        return await _store.claim_due(
            cts.session, now=now, swept_before=swept_before, limit=self.batch
        )

    async def _refresh_gauge(self, cts: CrossTenantSession) -> None:
        for saga_type, status, count in await _store.count_by_status(cts.session):
            _metrics.saga_instances.labels(saga_type, status).set(count)

    async def _purge(self, cts: CrossTenantSession, now: datetime) -> bool:
        days = int(self.runtime.settings.saga_retention_days)
        if days <= 0:
            return False
        removed = await _store.purge_terminal(
            cts.session, before=now - timedelta(days=days)
        )
        if removed:
            logger.info("purged %s terminal saga instance(s)", removed)
        return removed > 0

    async def _publish(self, database: str, due: list[_store.DueInstance]) -> None:
        async with db_session(database):
            for item in due:
                await publish_event(
                    nudge_channel(item.saga_type.split(".", 1)[0]),
                    {
                        "instance_id": str(item.id),
                        "expected_version": item.version,
                        "tenant_id": str(item.tenant_id) if item.tenant_id else None,
                    },
                    database=database,
                )
        logger.debug("saga sweep nudged %s instance(s) on %s", len(due), database)


def sweeper_of(core: Any) -> SagaSweeper | None:
    sweeper = getattr(core, "saga_sweeper", None)
    return sweeper if isinstance(sweeper, SagaSweeper) else None


__all__ = [
    "GAUGE_EVERY_TICKS",
    "LEASE_KEY_PREFIX",
    "PURGE_EVERY_TICKS",
    "SagaSweeper",
    "sweeper_of",
]
