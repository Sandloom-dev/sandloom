"""Outbox relay task.

Claims a batch of due outbox messages, publishes them via the event bus in
emission order and settles the whole batch (see
``SQLAlchemyOutboxStore.deliver_batch``: one claim statement + one settle
transaction per batch on PostgreSQL). A full batch is followed immediately by
the next one (no sleep) so a backlog drains at broker speed.

Housekeeping rides the same loop: the dead-letter sweep runs every
``_DEAD_SWEEP_EVERY_TICKS`` ticks and the retention purge of delivered rows
(``OUTBOX_RETENTION_HOURS``) and of expired command records every
``_PURGE_INTERVAL_SECONDS`` of wall-clock time, batching while batches are
full within ``_PURGE_TIME_BUDGET_SECONDS``.

Failure policy:

* a publish that fails because of the EVENT (schema/size rejection, a handler
  error surfaced by the transport, …) counts one delivery attempt and defers the
  row with exponential backoff + jitter (``next_attempt_at``); after
  ``OUTBOX_RELAY_MAX_ATTEMPTS`` the row is dead-lettered (``dead_at``);
* a publish that fails because the BROKER is unreachable (connection/timeout)
  counts NO attempt: the batch stops and a circuit breaker pauses the relay, so
  an outage cannot burn every pending row's budget and dead-letter committed
  events en masse;
* dead rows can be returned to pending with ``sandloom outbox requeue``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary

from prometheus_client import Counter, Gauge

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.infra.contracts import (
    BrokerUnavailableError,
    OutboxStore,
    RelayBatchResult,
)
from sandloom.core.observability.metrics import outbox_oldest_pending_age_seconds

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade
import sandloom.core.eventing.envelope as _envelope
from sandloom.core.config.settings import settings

# Bound on transport publish. Publishing now runs outside the DB transaction
# (two-phase claim), so this bounds how long a hung transport ties up a relay
# worker — not how long a DB row lock is held. Must stay well under the store's
# claim lease so a normal publish settles before its lease expires.
_PUBLISH_TIMEOUT_SECONDS = 5.0
# Pending rows younger than this are left to the post-commit dispatcher.
_RELAY_GRACE_SECONDS = 3.0
# Max messages claimed per database per batch.
_RELAY_BATCH_SIZE = 50
# Dead-letter sweep (rows over a lowered attempt budget) every N relay ticks:
# it only matters after an operator lowers OUTBOX_RELAY_MAX_ATTEMPTS.
_DEAD_SWEEP_EVERY_TICKS = 30
# Retention purge (delivered outbox rows + expired command records) runs on a
# wall-clock cadence — a tick count would stretch with the idle backoff — and
# keeps deleting _PURGE_BATCH_SIZE batches while they come back full, for at
# most _PURGE_TIME_BUDGET_SECONDS per database and table, so it keeps up with
# any write rate without stalling the relay.
_PURGE_INTERVAL_SECONDS = 30.0
_PURGE_TIME_BUDGET_SECONDS = 1.0
_PURGE_BATCH_SIZE = 1000
# sandloom_outbox_oldest_pending_age_seconds refresh cadence (wall clock; also while
# the broker circuit is open — that is when the age grows).
_AGE_INTERVAL_SECONDS = 10.0
# Monotonic clock of the housekeeping cadence/budget (patchable in tests).
_monotonic = time.monotonic
_RELAY_TASK: asyncio.Task[None] | None = None
_RELAY_PROGRESS: WeakKeyDictionary[asyncio.Task[None], float] = WeakKeyDictionary()
_RELAY_EXPECTED: WeakKeyDictionary[asyncio.Task[None], bool] = WeakKeyDictionary()


def relay_health_status(task: asyncio.Task[None] | None = None) -> dict[str, Any]:
    """Local relay lifecycle/progress without treating idle polling as a stall.

    Gunicorn followers have no local relay and report enabled=False; the leader
    reports its own progress. This is not a cross-process leadership proof.
    """

    task = task if task is not None else _RELAY_TASK
    enabled = task is not None and _RELAY_EXPECTED.get(task, False)
    running = enabled and task is not None and not task.done()
    last = _RELAY_PROGRESS.get(task) if task is not None else None
    age = max(0.0, time.monotonic() - last) if last is not None else None
    stalled = running and age is not None and age > settings.outbox_relay_stall_seconds
    return {
        "enabled": enabled,
        "running": running,
        "last_progress_age_seconds": age,
        "stalled": bool(stalled),
        "circuit_open": bool(enabled and _CIRCUIT.is_open()),
    }


_METRIC_RELAY_PROCESSED = Counter(
    "outbox_relay_processed_total",
    "Total outbox messages successfully delivered",
)
_METRIC_RELAY_FAILED = Counter(
    "outbox_relay_failed_total",
    "Total outbox messages publish failures (attempt increments)",
)
_METRIC_RELAY_BATCHES = Counter(
    "outbox_relay_batches_total",
    "Total batches executed by relay loop",
)
_METRIC_RELAY_LAST_RUN = Gauge(
    "outbox_relay_last_run_timestamp",
    "Unix timestamp of last relay batch execution",
    multiprocess_mode="livemax",
)
_METRIC_RELAY_INFLIGHT = Gauge(
    "outbox_relay_inflight_messages",
    "Number of messages processed in the most recent batch",
    multiprocess_mode="livesum",
)
_METRIC_RELAY_DEAD = Counter(
    "outbox_dead_total",
    "Total outbox messages moved to the terminal dead-letter state",
)
_METRIC_RELAY_BROKER_UNAVAILABLE = Counter(
    "outbox_relay_broker_unavailable_total",
    "Relay batches stopped because the broker was unavailable (no attempts burnt)",
)
_METRIC_RELAY_CIRCUIT_OPEN = Gauge(
    "outbox_relay_circuit_open",
    "1 while the relay's broker circuit breaker is open (broker unavailable)",
    multiprocess_mode="livemax",
)
_METRIC_RELAY_DB_ERRORS = Counter(
    "outbox_relay_database_errors_total",
    "Relay batches that failed for a given database instance",
    ["database"],
)
_METRIC_RELAY_PURGED = Counter(
    "outbox_purged_total",
    "Delivered outbox rows deleted by the retention purge",
)


def relay_metrics_snapshot() -> dict[str, int | float]:  # lightweight accessor
    # prometheus_client does not expose direct simple value attr; we use samples
    def _current(gauge: Gauge) -> float:
        try:  # each Gauge.collect()[0].samples: list of Sample(name, labels, value,...)
            samples = list(gauge.collect()[0].samples)  # type: ignore[index]
            if not samples:
                return 0.0
            sample = cast(Any, samples[0])
            val = getattr(sample, "value", 0.0)
            try:
                return float(val)
            except Exception:  # noqa: BLE001 - metric read is best effort
                return 0.0
        except Exception:  # pragma: no cover  # noqa: BLE001 - metric read is best effort
            return 0.0

    return {
        "last_run": _current(_METRIC_RELAY_LAST_RUN),
        "inflight_last": _current(_METRIC_RELAY_INFLIGHT),
    }


def _database_targets(
    core: CoreFacade,
) -> list[tuple[str, Callable[[], UnitOfWorkContract], OutboxStore]]:
    """Return ``(instance_name, uow_factory, outbox_store)`` for each database.

    With the infrastructure registry bound this is every configured database
    instance that exposes a transactional outbox (each owns its own); a database
    whose driver has no outbox is skipped. Provider resolution errors propagate:
    the registry is authoritative and the relay never selects an implicit
    database backend.
    """
    targets: list[tuple[str, Callable[[], UnitOfWorkContract], OutboxStore]] = []
    for name in core.infra.instances("database"):
        factory = core.get_uow_factory(name)
        store = core.database(name).outbox()
        if store is None:
            _logger.debug("relay: database %s has no outbox; skipping", name)
            continue
        targets.append((name, factory, store))
    return targets


class _TickCounter:
    """Per-process relay tick counter (drives the periodic housekeeping)."""

    def __init__(self) -> None:
        self.ticks = 0

    def due(self, every: int) -> bool:
        return every > 0 and self.ticks % every == 0


_TICKS = _TickCounter()


class _PurgeSchedule:
    """Per-process wall-clock cadence of the retention purge."""

    def __init__(self, interval: float = _PURGE_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self._next = float("-inf")  # due on the first tick

    def due(self) -> bool:
        now = _monotonic()
        if now < self._next:
            return False
        self._next = now + self.interval
        return True


_PURGE_SCHEDULE = _PurgeSchedule()
_AGE_SCHEDULE = _PurgeSchedule(_AGE_INTERVAL_SECONDS)


async def record_oldest_pending_age(core: CoreFacade) -> dict[str, float]:
    """Refresh ``sandloom_outbox_oldest_pending_age_seconds`` for every database.

    One indexed ``min(emitted_at)`` per database (see the store). A database
    that cannot answer is skipped (its relay errors are counted elsewhere).
    """
    ages: dict[str, float] = {}
    for name, factory, store in _database_targets(core):
        try:
            age = await store.oldest_pending_age_seconds(factory)
        except Exception as exc:  # noqa: BLE001 - a metric must not stop the relay
            _logger.debug("outbox age probe failed for database=%s: %s", name, exc)
            continue
        if age is None:
            continue
        outbox_oldest_pending_age_seconds.labels(database=name).set(age)
        ages[name] = age
    return ages


async def relay_once(core: CoreFacade) -> int:
    """Process one batch of outbox messages across all configured databases.

    Returns the total number of messages attempted. Each database is drained
    independently; a database without an outbox (or transiently unavailable) is
    logged and skipped without aborting the others.
    """
    attempted, _full = await _relay_tick(core)
    return attempted


async def _relay_tick(core: CoreFacade) -> tuple[int, bool]:
    """One relay tick: ``(messages attempted, any database had a full batch)``."""
    total = 0
    full = False
    if _AGE_SCHEDULE.due():
        await record_oldest_pending_age(core)
    if _CIRCUIT.is_open():
        # Broker recently unavailable: skip the whole tick (across databases —
        # they share one bus) until the cool-down elapses.
        return 0, False
    sweep = _TICKS.due(_DEAD_SWEEP_EVERY_TICKS)
    purge = _PURGE_SCHEDULE.due()
    _TICKS.ticks += 1
    bus = core.event_bus
    for name, factory, store in _database_targets(core):
        if _CIRCUIT.is_open():
            break
        try:
            result = await _relay_batch_for_store(factory, store, bus, sweep=sweep)
            total += result.attempted
            full = full or (result.full and not result.unavailable)
            if purge:
                await purge_delivered(factory, store)
                await purge_expired_commands(core, name, factory)
                await purge_expired_requests(core, name, factory)
        except Exception as exc:  # noqa: BLE001 - one DB down must not stop the others
            # An unreachable DB must not stop other databases from being drained
            # — but it must be visible (a silently skipped database drains zero
            # rows forever and nothing alerts).
            _METRIC_RELAY_DB_ERRORS.labels(database=name).inc()
            _logger.warning("Outbox relay batch failed for database=%s: %s", name, exc)
    return total, full


async def purge_delivered(
    uow_factory: Callable[[], UnitOfWorkContract],
    store: OutboxStore,
    *,
    retention_hours: float | None = None,
    batch_size: int = _PURGE_BATCH_SIZE,
    max_batches: int | None = None,
    time_budget_seconds: float | None = _PURGE_TIME_BUDGET_SECONDS,
) -> int:
    """Delete delivered rows older than the retention window (bounded batches).

    ``retention_hours`` defaults to ``OUTBOX_RETENTION_HOURS``; ``0`` disables
    the purge. Dead-lettered and undelivered rows are never deleted. Batches
    continue while full, up to ``max_batches`` / ``time_budget_seconds``
    (``None`` = unbounded). Returns the number of rows deleted.
    """
    hours = (
        float(_relay_settings().outbox_retention_hours)
        if retention_hours is None
        else float(retention_hours)
    )
    if hours <= 0:
        return 0

    async def batch() -> int:
        return await store.purge_delivered(
            uow_factory, older_than_seconds=hours * 3600.0, limit=batch_size
        )

    deleted = await _purge_batches(
        batch, batch_size, max_batches=max_batches, budget=time_budget_seconds
    )
    if deleted:
        _METRIC_RELAY_PURGED.inc(deleted)
        _logger.info("Outbox retention purged %d delivered row(s)", deleted)
    return deleted


async def purge_expired_commands(
    core: CoreFacade,
    database: str,
    uow_factory: Callable[[], UnitOfWorkContract],
    *,
    batch_size: int = _PURGE_BATCH_SIZE,
    max_batches: int | None = None,
    time_budget_seconds: float | None = _PURGE_TIME_BUDGET_SECONDS,
) -> int:
    """Delete expired durable command-idempotency records (bounded batches).

    A record lives ``CONTRACT_COMMAND_DEDUPE_TTL_SECONDS``; an expired one no
    longer dedupes (the next call with its key takes it over), so deleting it
    only reclaims space. A database without a durable command log is skipped.
    """
    getter = getattr(core, "database", None)
    provider = getter(database) if callable(getter) else None
    command_log = getattr(provider, "command_log", None)
    store = command_log() if callable(command_log) else None
    if store is None:
        return 0

    async def batch() -> int:
        return int(await store.purge_expired(uow_factory, limit=batch_size))

    deleted = await _purge_batches(
        batch, batch_size, max_batches=max_batches, budget=time_budget_seconds
    )
    if deleted:
        _logger.info("Purged %d expired command idempotency record(s)", deleted)
    return deleted


async def purge_expired_requests(
    core: CoreFacade,
    database: str,
    uow_factory: Callable[[], UnitOfWorkContract],
    *,
    batch_size: int = _PURGE_BATCH_SIZE,
    max_batches: int | None = None,
    time_budget_seconds: float | None = _PURGE_TIME_BUDGET_SECONDS,
) -> int:
    """Delete expired durable HTTP idempotency records (bounded batches).

    A record lives its guard's ``ttl_seconds``; an expired one no longer
    dedupes (the next request with its key takes it over), so deleting it
    only reclaims space. A database without the log is skipped.
    """
    getter = getattr(core, "database", None)
    provider = getter(database) if callable(getter) else None
    request_log = getattr(provider, "request_log", None)
    store = request_log() if callable(request_log) else None
    if store is None:
        return 0

    async def batch() -> int:
        return int(await store.purge_expired(uow_factory, limit=batch_size))

    deleted = await _purge_batches(
        batch, batch_size, max_batches=max_batches, budget=time_budget_seconds
    )
    if deleted:
        _logger.info("Purged %d expired HTTP idempotency record(s)", deleted)
    return deleted


async def _purge_batches(
    batch: Callable[[], Awaitable[int]],
    batch_size: int,
    *,
    max_batches: int | None,
    budget: float | None,
) -> int:
    """Run ``batch`` while it deletes a full ``batch_size`` (bounded)."""
    deadline = None if budget is None else _monotonic() + budget
    deleted = 0
    batches = 0
    while True:
        count = await batch()
        deleted += count
        batches += 1
        if count <= 0 or count < batch_size:
            break
        if max_batches is not None and batches >= max_batches:
            break
        if deadline is not None and _monotonic() >= deadline:
            break
    return deleted


def _relay_settings() -> Any:
    # Resolved per batch from the Settings singleton (OUTBOX_RELAY_* fields;
    # SECRETS_DIR-aware, validated at startup) — never raw os.getenv.
    return settings


def _max_attempts() -> int:
    return max(1, int(_relay_settings().outbox_relay_max_attempts))


def relay_backoff(attempts: int) -> float:
    """Retry delay (seconds) after the ``attempts``-th failed publish.

    Exponential (``OUTBOX_RELAY_BACKOFF_BASE_SECONDS`` * 2**(n-1), default 2s)
    capped at ``OUTBOX_RELAY_BACKOFF_MAX_SECONDS`` (default 300s), with equal
    jitter so failures across rows/instances do not retry in lock-step. With the
    defaults the 10-attempt budget spans ~15 minutes instead of ~10 ticks.
    """
    cfg = _relay_settings()
    base = float(cfg.outbox_relay_backoff_base_seconds)
    cap = float(cfg.outbox_relay_backoff_max_seconds)
    delay = min(cap, base * (2.0 ** max(0, attempts - 1)))
    return delay / 2 + random.random() * delay / 2  # noqa: S311 - backoff jitter, not a secret


# Names of exception types that mean "the broker/transport is unreachable"
# regardless of the client library (redis-py's ConnectionError/TimeoutError do
# NOT subclass the builtins). Matched on the MRO so core imports no client lib.
_UNAVAILABLE_TYPE_NAMES = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "BusyLoadingError",
        "ConnectionRefusedError",
        "ReadOnlyError",
    }
)
#: Redis error-reply prefixes that mean "this server cannot serve writes right
#: now" (dataset loading, failover to a replica, cluster resharding / down) —
#: an outage of the broker, never evidence that the EVENT is poison. redis-py
#: maps LOADING to BusyLoadingError and READONLY to ReadOnlyError, but the
#: others arrive as a plain ResponseError.
_UNAVAILABLE_REPLY_PREFIXES = (
    "LOADING ",
    "READONLY ",
    "MASTERDOWN ",
    "TRYAGAIN ",
    "CLUSTERDOWN ",
)


def is_broker_unavailable(exc: BaseException) -> bool:
    """True when ``exc`` (or its cause chain) is a transport availability error.

    Connection refused/reset, timeouts, "broker loading" and a failover /
    cluster reply (READONLY, MASTERDOWN, TRYAGAIN, CLUSTERDOWN) are outages,
    not evidence that the EVENT is poison — they must not consume its
    attempts.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (BrokerUnavailableError, OSError, TimeoutError, asyncio.TimeoutError),
        ):
            return True
        if any(
            cls.__name__ in _UNAVAILABLE_TYPE_NAMES for cls in type(current).__mro__
        ):
            return True
        if any(
            cls.__name__ == "ResponseError" for cls in type(current).__mro__
        ) and str(current).startswith(_UNAVAILABLE_REPLY_PREFIXES):
            return True
        current = current.__cause__ or current.__context__
    return False


class _BrokerCircuit:
    """Process-wide circuit breaker for the relay's broker.

    When a publish fails with a broker-unavailable error the relay stops the
    batch and OPENS the circuit: further batches are skipped for an
    exponentially growing cool-down (``OUTBOX_RELAY_CIRCUIT_BASE_SECONDS``,
    capped by ``OUTBOX_RELAY_CIRCUIT_MAX_SECONDS``). The first batch after the
    cool-down is the probe; a successful delivery closes the circuit.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def is_open(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) < self.open_until

    def trip(self) -> float:
        self.failures += 1
        cfg = _relay_settings()
        base = float(cfg.outbox_relay_circuit_base_seconds)
        cap = float(cfg.outbox_relay_circuit_max_seconds)
        delay = min(cap, base * (2.0 ** (self.failures - 1)))
        delay = delay / 2 + random.random() * delay / 2  # noqa: S311 - backoff jitter, not a secret
        self.open_until = time.monotonic() + delay
        _METRIC_RELAY_CIRCUIT_OPEN.set(1)
        return delay

    def reset(self) -> None:
        if self.failures or self.open_until:
            _METRIC_RELAY_CIRCUIT_OPEN.set(0)
        self.failures = 0
        self.open_until = 0.0


_CIRCUIT = _BrokerCircuit()


def broker_circuit() -> _BrokerCircuit:
    """The relay's process-wide broker circuit breaker (diagnostics/tests)."""
    return _CIRCUIT


class RelayBatchPublishError(Exception):
    """A relay batch failed for a reason that is not a broker outage.

    ``published`` leading events were confirmed by the transport; the cause
    is chained. The store re-publishes the batch row by row so the failure is
    attributed (attempt, backoff, dead-letter) to the row that caused it.
    """

    def __init__(self, published: int, cause: BaseException) -> None:
        super().__init__(
            f"batch publish stopped after {published} event(s): "
            f"{type(cause).__name__}: {cause}"
        )
        self.published = published


def _batch_publish_capable(bus: Any) -> bool:
    """A bus whose batch publish is ONE transport round trip (Redis Streams).

    A bus whose batch is only a loop over ``publish_event`` saves nothing and
    would only widen a failure to the whole batch, so the relay keeps
    publishing row by row there.
    """
    return getattr(bus, "native_batch_publish", False) is True and callable(
        getattr(bus, "publish_event_batch", None)
    )


_PUBLISH_MANY_SUPPORT: dict[type, bool] = {}


def _accepts_publish_many(store: OutboxStore) -> bool:
    """True when the store's ``deliver_batch`` takes ``publish_many``.

    A third-party store written against the older contract keeps its exact
    signature (and per-row behaviour).
    """
    cls = type(store)
    known = _PUBLISH_MANY_SUPPORT.get(cls)
    if known is None:
        try:
            params = inspect.signature(store.deliver_batch).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            params = {}  # type: ignore[assignment]
        known = "publish_many" in params
        _PUBLISH_MANY_SUPPORT[cls] = known
    return known


async def _relay_once_for_store(
    uow_factory: Callable[[], UnitOfWorkContract],
    store: OutboxStore,
    bus: Any,
    *,
    sweep: bool = True,
) -> int:
    """Process one batch for one database; returns the messages attempted."""
    result = await _relay_batch_for_store(uow_factory, store, bus, sweep=sweep)
    return result.attempted


async def _relay_batch_for_store(  # noqa: C901
    uow_factory: Callable[[], UnitOfWorkContract],
    store: OutboxStore,
    bus: Any,
    *,
    sweep: bool = True,
) -> RelayBatchResult:
    """Claim, publish and settle one batch of outbox messages for one database.

    The store owns the storage details (claim/lease/token, settle, attempt and
    dead marking — one claim + one settle transaction per batch on
    PostgreSQL); the relay owns transport policy (publish timeout, broker
    circuit) and metrics.
    """
    max_attempts = _max_attempts()
    _METRIC_RELAY_BATCHES.inc()

    async def _publish(
        channel: str, payload: dict[str, Any], schema_version: str, event_id: str
    ) -> None:
        # The store claims+commits first, so this runs with NO DB row lock and
        # no open transaction. The timeout bounds how long a hung transport
        # ties up the worker; the stable event id lets consumers dedupe
        # redeliveries.
        #
        # prevalidated=True (deliberately NOT PREFLIGHTED): the row's payload
        # passed the FULL preflight (schema included) inside the transaction
        # that committed it, possibly in a process that loads the
        # schema-owning module; this relay process may not load it, and
        # re-validating against its partial schema registry could
        # dead-letter a valid event. Only the SCHEMA check is skipped: the
        # transport-independent channel-name and payload-size checks still
        # run (BaseEventBus._check_prevalidated).
        try:
            await asyncio.wait_for(
                bus.publish_event(
                    channel,
                    payload,
                    schema_version=schema_version,
                    event_id=event_id,
                    prevalidated=True,
                ),
                timeout=_PUBLISH_TIMEOUT_SECONDS,
            )
        except BrokerUnavailableError:
            raise
        except Exception as exc:
            # A transport outage is not the event's fault: surface it as
            # BrokerUnavailableError so the store does not count an attempt.
            if is_broker_unavailable(exc):
                raise BrokerUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            raise

    async def _publish_many(
        items: Sequence[tuple[str, dict[str, Any], str, str]],
    ) -> None:
        # The whole claimed batch in ONE transport round trip (Redis Streams:
        # MULTI/EXEC), bounded by the same publish timeout as one event. The
        # store settles it all-or-nothing and falls back to per-row publishing
        # (``_publish``) for any failure that is not a broker outage.
        batch = [_envelope.OutboundEvent(c, p, v, i) for c, p, v, i in items]
        try:
            result = await asyncio.wait_for(
                bus.publish_event_batch(batch, prevalidated=True),
                timeout=_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if is_broker_unavailable(exc):
                raise BrokerUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            raise
        error = result.error
        if error is None:
            return
        if isinstance(error, BrokerUnavailableError):
            raise error
        if is_broker_unavailable(error):
            raise BrokerUnavailableError(f"{type(error).__name__}: {error}") from error
        raise RelayBatchPublishError(result.published, error) from error

    if sweep:
        # Rows already over a (possibly lowered) budget are surfaced as dead
        # rather than silently dropping out of the pending query forever.
        swept = await store.dead_letter_exhausted(
            uow_factory, max_attempts=max_attempts
        )
        if swept:
            _METRIC_RELAY_DEAD.inc(swept)
            _logger.warning(
                "Outbox dead-lettered %d message(s) already over max_attempts=%d",
                swept,
                max_attempts,
            )

    extra: dict[str, Any] = {}
    if _batch_publish_capable(bus) and _accepts_publish_many(store):
        extra["publish_many"] = _publish_many
    result = await store.deliver_batch(
        uow_factory,
        _publish,
        max_attempts=max_attempts,
        grace_seconds=_RELAY_GRACE_SECONDS,
        limit=_RELAY_BATCH_SIZE,
        backoff=relay_backoff,
        **extra,
    )
    if result.delivered:
        _CIRCUIT.reset()
    if result.unavailable:
        # Circuit-breaker: the broker is down — the batch stopped instead of
        # hammering it (and the DB) once per pending row. No attempt was
        # counted for the failing row; the rest of the batch was released.
        # (After the reset above: an outage that ENDED the batch wins.)
        cooldown = _CIRCUIT.trip()
        _METRIC_RELAY_BROKER_UNAVAILABLE.inc()
        _logger.warning(
            "Outbox relay: broker unavailable; pausing relay for %.1fs "
            "(no delivery attempts counted)",
            cooldown,
        )
    for msg_id in result.dead_ids:
        _logger.warning(
            "Outbox message dead-lettered after %d attempts outbox_id=%s",
            max_attempts,
            msg_id,
        )
    _METRIC_RELAY_PROCESSED.inc(result.delivered)
    _METRIC_RELAY_FAILED.inc(result.failed)
    if result.dead:
        _METRIC_RELAY_DEAD.inc(result.dead)
    _METRIC_RELAY_LAST_RUN.set(time.time())
    _METRIC_RELAY_INFLIGHT.set(result.attempted)
    return result


_logger = logging.getLogger(__name__)


async def _relay_loop(
    core: CoreFacade,
    stop_event: asyncio.Event,
    interval: float = 1.0,
    idle_backoff: float = 5.0,
    jitter: float = 0.3,
) -> None:
    """Continuously process outbox messages until stop_event is set.

    Adaptive sleep: if no messages processed, wait *idle_backoff*; otherwise
    wait *interval* plus small random jitter to avoid thundering herd in a
    multi-instance deployment. All exceptions are logged and cause a short
    backoff so the loop keeps running.
    """

    # Defensive: ensure the authoritative registry exposes at least one database
    # with an outbox before entering the loop.
    if not _database_targets(core):
        _logger.warning(
            "Outbox relay disabled: no configured database exposes an outbox"
        )
        return

    while not stop_event.is_set():
        try:
            processed, full = await _relay_tick(core)
            task = asyncio.current_task()
            if task is not None:
                _RELAY_PROGRESS[task] = time.monotonic()
        except Exception as exc:
            _logger.exception("relay_once failed: %s", exc)
            # On hard error sleep a little to avoid tight failure loop
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=idle_backoff)
            except TimeoutError:  # pragma: no cover - normal path
                pass
            continue

        if full:
            # A full batch: more is due right now — drain the backlog at
            # broker speed instead of one batch per interval. Yield once so
            # the loop stays cooperative.
            await asyncio.sleep(0)
            continue
        # Choose next sleep duration based on activity
        base = interval if processed > 0 else idle_backoff
        sleep_for = base + random.random() * jitter * base  # noqa: S311 - backoff jitter, not a secret
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except TimeoutError:  # pragma: no cover - normal wakeup
            continue

    _logger.info("Outbox relay loop stopping")


def start_relay_background(
    core: CoreFacade,
    *,
    interval: float = 1.0,
    idle_backoff: float = 5.0,
    jitter: float = 0.3,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Start background relay loop returning (task, stop_event).

    Caller must signal stop_event and await task (or let it auto-cancel) on
    shutdown. Parameters allow basic tuning without full config system yet.
    """

    global _RELAY_TASK
    ev = asyncio.Event()
    loop = loop or asyncio.get_running_loop()
    task = loop.create_task(
        _relay_loop(
            core,
            ev,
            interval=interval,
            idle_backoff=idle_backoff,
            jitter=jitter,
        )
    )
    _RELAY_TASK = task
    _RELAY_EXPECTED[task] = bool(_database_targets(core))
    _RELAY_PROGRESS[task] = time.monotonic()
    return task, ev
