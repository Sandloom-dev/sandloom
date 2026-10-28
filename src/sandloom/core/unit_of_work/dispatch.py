"""Post-commit outbox dispatch (stage 3 of the commit pipeline).

Publishes a committed UoW's surviving outbox events to its event bus — in line
(``db_session()``) or through the background :class:`PostCommitDispatcher`
coalescer on the request path — and marks them delivered in the database that
wrote them. The events are already durable: a failure here is logged, counted
and left to the outbox relay.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid as _uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

import orjson
from prometheus_client import Counter

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.infra.contracts import OutboxStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Annotation-only: the engine is paradigm-agnostic and imports no SQLAlchemy
    # at runtime (relational specifics stay behind the lazy sql_alchemy imports),
    # so it sits OUTSIDE the relational-paradigm boundary ratchet.
    from sqlalchemy.ext.asyncio import AsyncSession


import sandloom.core.eventing.envelope as _envelope
import sandloom.core.eventing.origin as _origin
import sandloom.core.unit_of_work.request as _request
from sandloom.core.unit_of_work.scope import (
    _OUTBOX_PENDING_KEY,
    _UOW_FACTORY_KEY,
    _outbox_store,
    _PendingEvent,
    _session_bus,
)

logger = logging.getLogger("sandloom.core.unit_of_work")


_METRIC_POST_COMMIT_DROPPED = Counter(
    "outbox_post_commit_dropped_total",
    "Committed events left to the outbox relay by the post-commit dispatcher "
    "(queue full, or broker failing: publish cool-down)",
)
_METRIC_POST_COMMIT_DISPATCH_FAILED = Counter(
    "outbox_post_commit_dispatch_failures_total",
    "Post-commit dispatch attempts that raised after the UoW committed (bus "
    "resolution, survivor check, dispatcher hand-off); the committed rows are "
    "left to the outbox relay and the request still succeeds",
)


# Budget for one UoW's post-commit publish. The events are already durable;
# this dispatch is only a latency optimization. On the request path it runs in
# the background coalescer (never holding the response); in db_session() it
# runs in line. If the broker is slow/degraded we abandon the attempt at this
# bound and let the relay deliver them (the relay's own publish timeout is
# larger).
_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS = 2.0


class _DispatchJob(NamedTuple):
    """One committed UoW's post-commit work for the background dispatcher."""

    bus: Any
    store: OutboxStore
    uow_factory: Callable[[], UnitOfWorkContract]
    #: Events still to publish, in the UoW's emission order.
    events: tuple[_PendingEvent, ...]
    #: Events an in-process bus already received in line (only to be marked).
    delivered: tuple[_uuid.UUID, ...]


async def _publish_in_order(
    bus: Any, events: tuple[_PendingEvent, ...] | list[_PendingEvent]
) -> list[_uuid.UUID]:
    """Publish one UoW's events in emission order (bounded); return their ids.

    Concurrent dispatch (the former gather) let a later event of the same
    transaction reach the broker first, so a consumer could see "updated"
    before "created". Ordered dispatch keeps one UoW's events in commit
    order on the broker; the first failure stops the batch so a later event
    never overtakes an earlier one that the relay will redeliver. This is NOT a
    global ordering guarantee (concurrent UoWs, relay redeliveries and consumer
    retries still interleave) — consumers order by a per-aggregate version
    (see sandloom.core.projections.monotonic_upsert). ``handle.id`` is the envelope
    id: redeliveries keep the same event id, so consumers can deduplicate
    (at-least-once delivery). The whole attempt is bounded by
    ``_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS``: a slow/degraded broker is left to
    the relay. Returns the ids of the LEADING events confirmed handed to the
    bus (only those may be marked delivered).
    """
    published = await _publish_prefix(bus, events)
    return [ev.handle.id for ev in events[:published]]


def _batch_capable(bus: Any) -> bool:
    """A framework bus with ``publish_event_batch`` (test doubles: per event)."""
    return getattr(bus, "supports_batch_publish", False) is True and callable(
        getattr(bus, "publish_event_batch", None)
    )


async def _publish_prefix(
    bus: Any, events: tuple[_PendingEvent, ...] | list[_PendingEvent]
) -> int:
    with _origin.event_origins(
        {
            str(ev.handle.id): (
                getattr(ev.handle, "trace_id", None),
                getattr(ev.handle, "emitted_at", None),
            )
            for ev in events
        }
    ):
        return await _publish_prefix_with_origin(bus, events)


async def _publish_prefix_with_origin(
    bus: Any, events: tuple[_PendingEvent, ...] | list[_PendingEvent]
) -> int:
    """Publish ``events`` in order; return how many LEADING ones were published.

    A batch-capable bus gets them in ONE ``publish_event_batch`` call (Redis
    Streams: one ``MULTI``/``EXEC`` round trip); any other bus one
    ``publish_event`` at a time. Bounded by
    ``_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS``; a timed-out batch confirms
    nothing (its outcome is unknown — the relay redelivers, duplicates allowed).
    """
    if not events:
        return 0
    # publish_event preflighted these payloads against the same bus inside
    # the transaction: skip re-validating them (framework buses only —
    # third-party / test buses keep the plain signature).
    # Their payloads are the very snapshots this bus preflighted (channel and
    # size included), so the bus skips its size re-check too (PREFLIGHTED).
    prevalidated: bool | str = False
    if getattr(bus, "supports_prevalidated", False):
        prevalidated = _envelope.PREFLIGHTED
    if _batch_capable(bus):
        batch = [
            _envelope.OutboundEvent(
                ev.channel, ev.payload, ev.schema_version, str(ev.handle.id)
            )
            for ev in events
        ]
        try:
            result = await asyncio.wait_for(
                bus.publish_event_batch(batch, prevalidated=prevalidated),
                timeout=_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Post-commit dispatch exceeded %.1fs budget for %d event(s); "
                "relay will deliver",
                _POST_COMMIT_DISPATCH_TIMEOUT_SECONDS,
                len(events),
            )
            return 0
        published = max(0, min(int(result.published), len(events)))
        if result.error is not None:
            failed = events[min(published, len(events) - 1)]
            logger.warning(
                "Post-commit dispatch failed for outbox=%s channel=%s (%s); "
                "relay will deliver it and the %d later event(s) of this batch",
                failed.handle.id,
                failed.channel,
                type(result.error).__name__,
                len(events) - published - 1,
            )
        return published

    delivered = 0
    extra: dict[str, Any] = {"prevalidated": prevalidated} if prevalidated else {}

    async def _run() -> None:
        nonlocal delivered
        for ev in events:
            try:
                await bus.publish_event(
                    ev.channel,
                    ev.payload,
                    ev.schema_version,
                    event_id=str(ev.handle.id),
                    **extra,
                )
            except Exception:  # noqa: BLE001 - the relay redelivers
                logger.warning(
                    "Post-commit dispatch failed for outbox=%s channel=%s; "
                    "relay will deliver it and the %d later event(s) of this "
                    "batch",
                    ev.handle.id,
                    ev.channel,
                    len(events) - delivered - 1,
                )
                return
            delivered += 1

    try:
        await asyncio.wait_for(_run(), timeout=_POST_COMMIT_DISPATCH_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Post-commit dispatch exceeded %.1fs budget for %d event(s); "
            "relay will deliver",
            _POST_COMMIT_DISPATCH_TIMEOUT_SECONDS,
            len(events) - delivered,
        )
    return delivered


#: How long delivered ids may wait to be marked in one batched UPDATE.
_MARK_FLUSH_INTERVAL_SECONDS = 0.05
#: Bound on queued post-commit jobs; beyond it a job is left to the relay.
_DISPATCH_QUEUE_MAX = 10_000
_DISPATCH_QUEUE_BYTES_MAX = 32 * 1024 * 1024
#: Bound on one mark-delivered UPDATE (ids per statement).
_MARK_BATCH_MAX = 500
#: After a failed / timed-out publish, later jobs skip publishing (left to the
#: relay) for this long instead of each waiting out the publish budget.
_PUBLISH_COOLDOWN_SECONDS = 1.0
#: One dispatcher round takes the current job plus what is ALREADY queued, up
#: to this many jobs ...
_ROUND_MAX_JOBS = 64
#: ... or until it holds at least this many events, and publishes them in one
#: batch (one broker round trip on Redis Streams).
_ROUND_MAX_EVENTS = 512


#: Retained Python bytes charged per JSON element separator (``,``): an
#: element costs one object plus its container slot (≈ 36–90 bytes for the
#: small ints / short strings / dict entries of event payloads) while its
#: JSON text is a few bytes. Strings are charged their JSON length (≈ their
#: Python size).
_DISPATCH_BYTES_PER_ELEMENT = 48
#: ... and per container opened (``{`` / ``[``): an empty dict is 64 bytes, a
#: small one 184.
_DISPATCH_BYTES_PER_CONTAINER = 160


def _payload_bytes(payload: Any) -> int:
    """Estimated retained size of one queued payload, from its JSON form.

    ``orjson.dumps`` (C, ≈ 0.2 µs for an id + metadata payload) instead of a
    Python walk of every node (≈ 3–20 µs, and 300 µs for a 1,000-int list).
    With the per-event constant of :func:`_dispatch_job_bytes` the estimate
    is ≈ 1–2× ``sys.getsizeof`` summed over the payload's nodes (pinned by
    ``test_dispatch_job_bytes_tracks_the_retained_python_size``).
    """
    try:
        encoded = orjson.dumps(payload, default=str, option=orjson.OPT_NON_STR_KEYS)
    except (TypeError, orjson.JSONEncodeError):  # e.g. ints beyond 64 bits
        encoded = json.dumps(payload, default=str).encode()
    return (
        len(encoded)
        + _DISPATCH_BYTES_PER_ELEMENT * encoded.count(b",")
        + _DISPATCH_BYTES_PER_CONTAINER * (encoded.count(b"{") + encoded.count(b"["))
    )


def _dispatch_job_bytes(job: _DispatchJob) -> int:
    """Conservative retained size of a queued job (its payloads + overhead)."""
    size = 512 * len(job.events) + 64 * len(job.delivered) + 256
    for event in job.events:
        size += _payload_bytes(event.payload)
    return size


class PostCommitDispatcher:
    """Per-event-loop background coalescer of post-commit outbox work.

    The request path only ENQUEUES ``(bus, store, uow factory, events)`` after
    its commit. One background task (started lazily, in a clean
    ``contextvars.Context`` so no request state leaks into it):

    * per round takes the next job plus whatever is already queued (at most
      ``_ROUND_MAX_JOBS`` jobs / about ``_ROUND_MAX_EVENTS`` events — never
      waiting for more) and publishes them as ONE ordered batch: jobs in
      commit order (FIFO), each job's events in emission order — so within
      this process one UoW's events never overtake each other, as before —
      in one broker round trip on Redis Streams (``MULTI``/``EXEC``) instead
      of one per event. A batch that fails or times out marks NOTHING
      delivered: all of its events are left to the relay (duplicates of the
      ones that did reach the broker are possible, loss is not), and later
      rounds skip publishing for ``_PUBLISH_COOLDOWN_SECONDS``;
    * collects the delivered ids per (store, database) and marks them in ONE
      ``UPDATE … WHERE id = ANY(:ids)`` every <= ``_MARK_FLUSH_INTERVAL_SECONDS``
      (or at once on :meth:`drain`).

    The relay stays the safety net: a job dropped on overflow, a publish that
    fails or times out, a mark that fails, or a process that dies with jobs
    still queued all leave the rows undelivered, and the relay delivers them
    after its grace period (at-least-once, stable event ids). Nothing here is
    on the HTTP response path.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._queue: asyncio.Queue[tuple[_DispatchJob, int]] = asyncio.Queue(
            maxsize=_DISPATCH_QUEUE_MAX
        )
        self._task: asyncio.Task[None] | None = None
        self._retained_bytes = 0
        # (id(store), id(uow_factory)) -> (store, factory, ids)
        self._marks: dict[
            tuple[int, int],
            tuple[OutboxStore, Callable[[], UnitOfWorkContract], list[_uuid.UUID]],
        ] = {}
        self._first_mark_at: float | None = None
        self._mark_lock = asyncio.Lock()
        self._skip_publish_until = 0.0
        #: drain_post_commit() calls in progress (the last one stops the task)
        self._drainers = 0

    # -- producer side ------------------------------------------------------
    def submit(self, job: _DispatchJob) -> None:
        size = _dispatch_job_bytes(job)
        try:
            if self._retained_bytes + size > _DISPATCH_QUEUE_BYTES_MAX:
                raise asyncio.QueueFull
            self._queue.put_nowait((job, size))
            self._retained_bytes += size
        except asyncio.QueueFull:
            _METRIC_POST_COMMIT_DROPPED.inc(len(job.events) + len(job.delivered))
            logger.warning(
                "Post-commit dispatch queue full (job or byte budget); "
                "%d event(s) left to the outbox relay",
                len(job.events) + len(job.delivered),
            )
            return
        self._ensure_running()

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            # A clean context: the coalescer must not inherit the submitting
            # request's contextvars (bound tenant, UoW session, request id).
            self._task = contextvars.Context().run(self.loop.create_task, self._run())

    # -- the background task ------------------------------------------------
    async def _run(self) -> None:
        while True:
            timeout: float | None = None
            if self._first_mark_at is not None:
                timeout = max(
                    0.0,
                    self._first_mark_at
                    + _MARK_FLUSH_INTERVAL_SECONDS
                    - self.loop.time(),
                )
            due = False
            try:
                if timeout is None:
                    job, size = await self._queue.get()
                else:
                    job, size = await asyncio.wait_for(
                        self._queue.get(), timeout=timeout
                    )
            except TimeoutError:
                due = True
            if due:
                # Flushed OUTSIDE the except block: a drain cancelling the
                # task in this flush would chain the TimeoutError (its
                # traceback holds asyncio's Timeout, which holds this task)
                # into the CancelledError the task keeps: a reference cycle.
                await self.flush()
                continue
            jobs, round_bytes = self._take_round(job, size)
            try:
                await self._process_round(jobs)
            except Exception:  # never kill the coalescer
                logger.warning("Post-commit dispatch round failed", exc_info=True)
            finally:
                self._retained_bytes -= round_bytes
                for _ in range(len(jobs)):
                    self._queue.task_done()
            # An idle coalescer holds no finished job (nor does the frame a
            # cancellation's traceback keeps).
            del job, jobs
            if (
                self._first_mark_at is not None
                and self.loop.time() - self._first_mark_at
                >= _MARK_FLUSH_INTERVAL_SECONDS
            ):
                await self.flush()

    def _take_round(
        self, job: _DispatchJob, size: int
    ) -> tuple[list[_DispatchJob], int]:
        """``job`` plus what is ALREADY queued, within the round bounds."""
        jobs = [job]
        events = len(job.events)
        while len(jobs) < _ROUND_MAX_JOBS and events < _ROUND_MAX_EVENTS:
            try:
                queued, queued_size = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            jobs.append(queued)
            size += queued_size
            events += len(queued.events)
        return jobs, size

    async def _process(self, job: _DispatchJob) -> None:
        """Process one job (a round of one)."""
        await self._process_round([job])

    async def _process_round(self, jobs: list[_DispatchJob]) -> None:
        """Publish a round of jobs; collect the delivered ids for marking.

        Consecutive jobs on the same bus form one ordered batch. A batch is
        all-or-nothing for the delivered marks: only a fully published batch
        marks its events delivered.
        """
        start = 0
        while start < len(jobs):
            bus = jobs[start].bus
            end = start + 1
            while end < len(jobs) and jobs[end].bus is bus:
                end += 1
            run = jobs[start:end]
            start = end
            events = [ev for job in run for ev in job.events]
            published = False
            if events:
                if self.loop.time() < self._skip_publish_until:
                    # The broker just failed / timed out: do not let every
                    # queued round wait out the publish budget in turn
                    # (head-of-line blocking). Leave these to the relay for
                    # the cool-down.
                    _METRIC_POST_COMMIT_DROPPED.inc(len(events))
                else:
                    published = await _publish_prefix(bus, events) == len(events)
                    if not published:
                        _METRIC_POST_COMMIT_DROPPED.inc(len(events))
                        self._skip_publish_until = (
                            self.loop.time() + _PUBLISH_COOLDOWN_SECONDS
                        )
            for job in run:
                delivered = list(job.delivered)
                if published:
                    delivered.extend(ev.handle.id for ev in job.events)
                self._collect(job, delivered)

    def _collect(self, job: _DispatchJob, delivered: list[_uuid.UUID]) -> None:
        if not delivered:
            return
        key = (id(job.store), id(job.uow_factory))
        entry = self._marks.get(key)
        if entry is None:
            self._marks[key] = (job.store, job.uow_factory, delivered)
        else:
            entry[2].extend(delivered)
        if self._first_mark_at is None:
            self._first_mark_at = self.loop.time()

    async def flush(self) -> None:
        """Mark every collected delivered id, one batched UPDATE per database."""
        async with self._mark_lock:
            marks, self._marks = self._marks, {}
            self._first_mark_at = None
            for store, factory, ids in marks.values():
                for start in range(0, len(ids), _MARK_BATCH_MAX):
                    chunk = ids[start : start + _MARK_BATCH_MAX]
                    try:
                        await store.mark_delivered(factory, chunk)
                    except Exception:  # relay may re-publish
                        logger.warning(
                            "Failed to mark %d outbox message(s) delivered",
                            len(chunk),
                            exc_info=True,
                        )

    async def drain(self, timeout: float | None = None) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
        """Wait until every queued job is published AND marked (tests, shutdown).

        With ``timeout`` the wait is bounded; what is left is the relay's.
        """

        async def _drain() -> None:
            await self._queue.join()
            await self.flush()

        if timeout is None:
            await _drain()
        else:
            await asyncio.wait_for(_drain(), timeout=timeout)

    def pending(self) -> int:
        """Jobs queued but not processed yet (diagnostics)."""
        return self._queue.qsize()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


_dispatcher: PostCommitDispatcher | None = None


def post_commit_dispatcher() -> PostCommitDispatcher:
    """This event loop's :class:`PostCommitDispatcher` (created on first use).

    Bound to the running loop: a new loop (a new test, a re-created worker
    loop) gets a fresh dispatcher; jobs stranded on a dead loop are durable
    outbox rows the relay delivers.
    """
    global _dispatcher
    loop = asyncio.get_running_loop()
    if _dispatcher is None or _dispatcher.loop is not loop:
        _dispatcher = PostCommitDispatcher(loop)
    return _dispatcher


async def drain_post_commit(timeout: float | None = None) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
    """Wait for this loop's background post-commit dispatch to finish.

    Graceful shutdown calls it (bounded) after the server stops accepting
    requests; tests call it to observe the delivered state deterministically
    (an in-process bus has ALREADY received the events when the request
    returned — only the delivered marks are deferred). Then stops the
    coalescer task; the next commit starts a new one.
    """
    dispatcher = _dispatcher
    if dispatcher is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - called without a loop
        return
    if dispatcher.loop is not running:
        return
    # Concurrent drainers (requests in a test, shutdown racing a request):
    # only the LAST one stops the coalescer. Stopping it under another
    # drainer would cancel that drainer's job mid-publish (left to the relay)
    # or strand its queued job behind a dead task.
    dispatcher._drainers += 1
    try:
        await dispatcher.drain(timeout)
    finally:
        dispatcher._drainers -= 1
        if dispatcher._drainers == 0:
            await dispatcher.aclose()


#: Observer of the committed (surviving) outbox events of a UoW, or ``None``.
#: Installed by the contract-cache shadow (:mod:`sandloom.core.contract_cache_shadow`,
#: ADR-0003), so the commit pipeline never imports the contract layer
#: (ADR-0007). It is a measurement tap, not an effect: it must never raise.
_committed_events_hook: Callable[[list[Any]], None] | None = None


def set_committed_events_hook(hook: Callable[[list[Any]], None] | None) -> None:
    """Install (or, with ``None``, remove) the committed-events observer."""
    global _committed_events_hook
    _committed_events_hook = hook


async def _dispatch_pending_events(session: AsyncSession, *, wait: bool) -> None:
    """Best-effort post-commit dispatch of events enqueued via publish_event.

    The events are already durably stored in outbox_messages by the committed
    transaction; this dispatch only reduces delivery latency. Any failure here
    is picked up by the outbox relay (at-least-once delivery).
    """
    pending: list[_PendingEvent] = list(session.info.get(_OUTBOX_PENDING_KEY, []))
    if not pending:
        return
    session.info[_OUTBOX_PENDING_KEY] = []

    store = _outbox_store(session)
    # Drop phantom events. session.info is NOT savepoint-aware: a publish_event
    # issued inside a begin_nested() that later rolled back leaves its
    # _PendingEvent here, but its backing row was undone. The store knows how to
    # tell a surviving row from a rolled-back one; skip non-survivors so we never
    # dispatch a phantom event with no durable row to dedupe or retry against.
    pending = [ev for ev in pending if store.survived(ev.handle)]
    if not pending:
        return
    hook = _committed_events_hook
    if hook is not None:
        # ADR-0003 shadow measurement (OFF by default): committed events that
        # would invalidate a caller-side contract-read cache. Never raises.
        hook(pending)

    bus = _session_bus(session)
    # Marked on the SAME database the events were written to, so multi-DB
    # outboxes are marked delivered in their own database. The store owns the
    # backend-specific UPDATE.
    uow_factory = session.info[_UOW_FACTORY_KEY]

    if wait:
        delivered = await _publish_in_order(bus, pending)
        if not delivered:
            return
        try:
            # a kernel unit: its own admission scope (spec §4.4 rule 3)
            with _request.fresh_admission_scope():
                await store.mark_delivered(uow_factory, delivered)
        except Exception:  # at-least-once: relay may re-publish
            logger.warning("Failed to mark outbox messages delivered", exc_info=True)
        return

    # The bus's publish only hands the event to the transport: no bus runs
    # consumers inline (the in-memory bus delivers in its own clean-context
    # tasks with retry/dead-letter), so consumers never inherit this request's
    # contextvars. An IN-PROCESS bus does no I/O at all: hand it the events in
    # line (deterministic for tests and dev — the effects are scheduled when
    # the response returns) and defer only the delivered marks. A networked
    # transport (Redis Streams) is published by the background coalescer.
    events: tuple[_PendingEvent, ...] = tuple(pending)
    delivered_inline: tuple[_uuid.UUID, ...] = ()
    if getattr(bus, "in_process", False):
        delivered_inline = tuple(await _publish_in_order(bus, events))
        events = ()
        if not delivered_inline:
            return
    post_commit_dispatcher().submit(
        _DispatchJob(bus, store, uow_factory, events, delivered_inline)
    )
