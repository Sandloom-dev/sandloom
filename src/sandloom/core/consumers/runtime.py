"""Supervised lifecycle for side-effect and projection event consumers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from sandloom.core.consumers.callbacks import (
    SessionOpener,
    _projection_callback,
    _side_effect_callback,
    subscribe_to_bus,
)
from sandloom.core.eventing.subscriptions import SubscriptionState
from sandloom.core.eventing.transport_base import service_name as _configured_service
from sandloom.core.observability.metrics_utils import safe_inc

if TYPE_CHECKING:  # pragma: no cover
    from sandloom.core.facade import CoreFacade
import sandloom.core.eventing.transport_base as _transport_base
import sandloom.core.unit_of_work as _unit_of_work
from sandloom.core.config.settings import settings

ConsumerKind = Literal["side_effect", "projection"]
logger = logging.getLogger(__name__)


class _PermanentSubscriptionError(TypeError):
    """The adapter/descriptor contract is invalid; retry cannot repair it."""


class ConsumerStartupError(RuntimeError):
    """One or more required consumers could not become active."""

    def __init__(self, consumers: tuple[str, ...]) -> None:
        self.consumers = consumers
        super().__init__(
            "required event consumers failed to start: " + ", ".join(consumers)
        )


class ConsumerConfigurationError(RuntimeError):
    """The registered consumers are misconfigured; the process must not start.

    Raised for problems no retry can fix: two consumers of this process
    resolving to the same ``(channel, group)`` (the second would never run —
    its exactly-once claim is already taken by the first), or a consumer group
    already owned by a different module / database (another deployment would
    split its events). Raised regardless of ``required``.
    """


_DEFAULT_PORTS = {"postgresql": 5432, "postgres": 5432, "mysql": 3306}


def normalize_database_url(url: str) -> str:
    """A credential-free, driver-agnostic identity of a database URL.

    ``postgresql+asyncpg://u:p@DB:5432/app?ssl=1`` and
    ``postgresql://other@db/app`` both normalise to ``postgresql://db:5432/app``
    so replicas configured with different drivers / credentials still match.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0].lower()
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme == "sqlite":
        return f"sqlite://{parts.path}"
    host = (parts.hostname or "").lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    return f"{scheme}://{host}:{port}{parts.path}"


def database_identity(core: Any, name: str) -> str:
    """Physical identity of logical database ``name`` (for group ownership).

    The exactly-once inbox of a consumer lives in its database, so two
    processes may share a consumer group only when they claim events in the
    SAME physical database. Derived from the configured instance URL (hashed,
    credential-free); falls back to the logical name when no URL is known.
    """
    url: Any = None
    try:
        config = getattr(core.infra, "_config", {}) or {}
        instance = config.get("database", {}).get(name)
        options = getattr(instance, "options", None) or {}
        url = options.get("url")
    except Exception:  # noqa: BLE001 - bare facade without infra
        url = None
    if not url and name == "default":
        url = getattr(settings, "database_url", None)
    if not url:
        return f"logical:{name}"
    digest = hashlib.sha256(normalize_database_url(str(url)).encode()).hexdigest()
    return f"url:{digest[:16]}"


@dataclass(frozen=True, slots=True)
class ConsumerStatus:
    """Externally observable lifecycle state of one registered consumer."""

    name: str
    channel: str
    kind: ConsumerKind
    required: bool
    state: str
    last_error: str | None = None
    attempts: int = 0
    last_connected_at: str | None = None
    group: str | None = None
    module: str | None = None
    service: str | None = None
    lag: int | None = None
    pending: int | None = None


@dataclass(frozen=True, slots=True)
class ConsumerRuntimeSnapshot:
    """Point-in-time readiness and diagnostics for all local consumers."""

    ready: bool
    required_unavailable: tuple[str, ...]
    consumers: tuple[ConsumerStatus, ...]


@dataclass(slots=True)
class _ConsumerRecord:
    name: str
    channel: str
    kind: ConsumerKind
    required: bool
    callback: Any
    state: SubscriptionState = SubscriptionState.REGISTERED
    last_error: str | None = None
    handle: Any | None = None
    retryable: bool = True
    group: str = ""
    module: str | None = None
    database: str = "default"
    start_from: Any = None
    owner: dict[str, Any] = field(default_factory=dict)


class ConsumerRuntime:
    """Connect consumers independently and expose fail-closed readiness."""

    def __init__(
        self,
        core: CoreFacade,
        *,
        session_opener: SessionOpener | None = None,
        startup_timeout: float,
        retry_delay: float,
        retry_max: float = 30.0,
        shutdown_timeout: float = 30.0,
        database_identity: Callable[[str], str] | None = None,
        service_name: str | None = None,
    ) -> None:
        self._core = core
        self._database_identity = database_identity
        if service_name is None:
            service_name = _configured_service()
        self._service = service_name
        if session_opener is None:
            session_opener = _unit_of_work.consumer_db_session
        self._session_opener = session_opener
        self._startup_timeout = max(0.0, startup_timeout)
        self._retry_delay = max(0.0, retry_delay)
        self._retry_max = max(self._retry_delay, retry_max)
        self._shutdown_timeout = max(0.01, shutdown_timeout)
        self._records: list[_ConsumerRecord] = []
        self._monitor_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._started = False
        self._start_lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls,
        core: CoreFacade,
        settings: Any,
        *,
        session_opener: SessionOpener | None = None,
    ) -> ConsumerRuntime:
        """The runtime configured by the ``CONSUMER_*`` / shutdown settings."""
        return cls(
            core,
            session_opener=session_opener,
            startup_timeout=settings.consumer_startup_timeout_seconds,
            retry_delay=settings.consumer_retry_base_seconds,
            retry_max=settings.consumer_retry_max_seconds,
            shutdown_timeout=settings.shutdown_task_timeout_seconds,
        )

    def _owner(self, module: str | None, database: str, channel: str) -> dict[str, Any]:
        identify = self._database_identity or (
            lambda db: database_identity(self._core, db)
        )
        return {
            "module": module or "",
            "database": identify(database),
            "service": self._service,
            "channel": channel,
        }

    def _record(
        self,
        descriptor: Any,
        *,
        kind: ConsumerKind,
        channel: str,
        database: str,
        callback: Callable[[Any, str, SessionOpener, str], Any],
    ) -> _ConsumerRecord:
        """The supervised record of one registered consumer descriptor.

        ``callback(descriptor, database, opener, group)`` builds the
        kind-specific bus callback (metrics and labels differ per kind).
        """
        group = getattr(descriptor, "group_name", descriptor.name)
        module = getattr(descriptor, "module", None)
        return _ConsumerRecord(
            name=descriptor.name,
            channel=channel,
            kind=kind,
            required=getattr(descriptor, "required", True),
            callback=callback(descriptor, database, self._session_opener, group),
            group=group,
            module=module,
            database=database,
            start_from=getattr(descriptor, "start_from", None),
            owner=self._owner(module, database, channel),
        )

    def _event_records(self) -> list[_ConsumerRecord]:
        return [
            self._record(
                descriptor,
                kind="side_effect",
                channel=descriptor.channel,
                database=descriptor.database,
                callback=_side_effect_callback,
            )
            for descriptor in self._core.event_consumers
        ]

    def _projection_records(self) -> list[_ConsumerRecord]:
        records: list[_ConsumerRecord] = []
        registry = self._core.projection_registry
        for descriptor in registry.list_consumers():
            projection = registry.get_projection(descriptor.projection)
            if projection is None:
                records.append(
                    _ConsumerRecord(
                        name=descriptor.name,
                        channel=descriptor.source_channel,
                        kind="projection",
                        required=getattr(descriptor, "required", True),
                        callback=None,
                        state=SubscriptionState.FAILED,
                        last_error=(f"unknown projection {descriptor.projection!r}"),
                        retryable=False,
                        group=getattr(descriptor, "group_name", descriptor.name),
                        module=getattr(descriptor, "module", None),
                    )
                )
                continue
            records.append(
                self._record(
                    descriptor,
                    kind="projection",
                    channel=descriptor.source_channel,
                    database=projection.storage,
                    callback=_projection_callback,
                )
            )
        return records

    def _check_unique_groups(self) -> None:
        counts = Counter((record.channel, record.group) for record in self._records)
        duplicates = sorted(key for key, n in counts.items() if n > 1)
        if duplicates:
            listed = ", ".join(f"{grp!r} on {ch!r}" for ch, grp in duplicates)
            raise ConsumerConfigurationError(
                "several consumers of this process resolve to the same consumer "
                f"group ({listed}): they would share one pending list and one "
                "exactly-once inbox, so all but one handler would never run. "
                "Give each consumer a distinct name (or group=)."
            )

    async def _check_group_owners(self) -> None:
        """Fail fast when another deployment already owns one of our groups.

        Best-effort pre-pass (the same check also runs on every subscribe): a
        transport error here is ignored — the connect path retries — but an
        ownership conflict aborts startup with an actionable message.
        """
        transport = getattr(self._core.event_bus, "transport", None)
        claim = getattr(transport, "claim_group_owner", None)
        if claim is None:
            return

        conflicts: list[str] = []
        for record in self._records:
            if record.callback is None or not record.owner:
                continue
            try:
                await claim(record.channel, record.group, record.owner)
            except _transport_base.GroupOwnershipError as exc:
                conflicts.append(str(exc))
            except Exception:  # transient: connect path retries
                logger.debug("group ownership pre-check failed", exc_info=True)
        if conflicts:
            raise ConsumerConfigurationError("; ".join(conflicts))

    async def _connect_once(
        self, record: _ConsumerRecord, *, wait_timeout: float
    ) -> bool:
        record.state = SubscriptionState.STARTING
        handle: Any | None = None
        operation_timeout = max(0.01, wait_timeout)
        started_at = asyncio.get_running_loop().time()

        async def _subscribe_and_wait() -> None:
            nonlocal handle
            handle = await subscribe_to_bus(
                self._core.event_bus,
                record.channel,
                record.callback,
                record.group or record.name,
                start_from=record.start_from,
                owner=record.owner or None,
            )
            if handle is None:
                raise _PermanentSubscriptionError(
                    "event bus subscribe() returned no subscription handle"
                )
            await handle.wait_ready(operation_timeout)

        try:
            await asyncio.wait_for(_subscribe_and_wait(), timeout=operation_timeout)
        except BaseException as exc:
            if handle is not None:
                elapsed = asyncio.get_running_loop().time() - started_at
                close_timeout = max(0.01, operation_timeout - elapsed)
                await self._close_handle(
                    handle,
                    record=record,
                    timeout=close_timeout,
                )
            if not isinstance(exc, Exception):
                record.state = (
                    SubscriptionState.STOPPED
                    if self._stopping
                    else SubscriptionState.FAILED
                )
                raise
            record.state = SubscriptionState.FAILED
            record.last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, (_PermanentSubscriptionError, ValueError)):
                record.retryable = False
            safe_inc(
                "consumer_subscription_attempts_total",
                {
                    "consumer": record.name,
                    "kind": record.kind,
                    "result": "failed",
                },
            )
            logger.warning(
                "consumer subscription unavailable consumer=%s channel=%s",
                record.name,
                record.channel,
                exc_info=True,
            )
            return False
        record.handle = handle
        record.state = SubscriptionState.RUNNING
        record.last_error = None
        safe_inc(
            "consumer_subscription_attempts_total",
            {
                "consumer": record.name,
                "kind": record.kind,
                "result": "connected",
            },
        )
        return True

    async def _close_handle(
        self,
        handle: Any,
        *,
        record: _ConsumerRecord,
        timeout: float,  # noqa: ASYNC109 - explicit per-call timeout is the API
    ) -> BaseException | None:
        """Best-effort bounded cleanup that never masks subscription failure."""
        try:
            await asyncio.wait_for(handle.close(), timeout=max(0.01, timeout))
        except Exception as exc:
            logger.warning(
                "consumer subscription close failed consumer=%s channel=%s",
                record.name,
                record.channel,
                exc_info=True,
            )
            return exc
        return None

    async def _start_record(self, record: _ConsumerRecord) -> None:
        if record.callback is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        failures = 0
        while True:
            remaining = max(0.0, deadline - loop.time())
            if await self._connect_once(record, wait_timeout=remaining):
                return
            if not record.required or not record.retryable or loop.time() >= deadline:
                return
            failures += 1
            await asyncio.sleep(min(self._retry_backoff(failures), remaining))

    def _retry_backoff(self, failures: int) -> float:
        if self._retry_delay <= 0:
            return 0
        base = min(self._retry_max, self._retry_delay * (2 ** min(failures - 1, 10)))
        return float(min(self._retry_max, base * random.uniform(0.8, 1.2)))  # noqa: S311 - retry jitter, not a secret

    async def start(self) -> ConsumerRuntimeSnapshot:
        """Attempt every consumer; fail startup if any required one is unavailable."""
        async with self._start_lock:
            if self._started:
                return self.snapshot()
            snapshot = await self._start_registered()
            self._started = True
            return snapshot

    async def _start_registered(self) -> ConsumerRuntimeSnapshot:
        self._stopping = False
        self._records = [*self._event_records(), *self._projection_records()]
        self._check_unique_groups()
        await self._check_group_owners()
        required_records = [record for record in self._records if record.required]
        await asyncio.gather(
            *(self._start_record(record) for record in required_records)
        )
        snapshot = self.snapshot()
        if snapshot.required_unavailable:
            await self.stop()
            raise ConsumerStartupError(snapshot.required_unavailable)
        for record in self._records:
            task = asyncio.create_task(self._supervise(record))
            self._monitor_tasks.add(task)
            task.add_done_callback(self._monitor_tasks.discard)
        return snapshot

    async def _supervise(self, record: _ConsumerRecord) -> None:
        while not self._stopping:
            handle = record.handle
            if handle is not None:
                await handle.wait_terminated()
                if self._stopping or record.handle is not handle:
                    continue
                if handle is None:  # pragma: no cover - narrowed above
                    continue
                adapter_snapshot = getattr(handle, "snapshot", None)
                if callable(adapter_snapshot):
                    record.last_error = adapter_snapshot().last_error
                close_error = await self._close_handle(
                    handle,
                    record=record,
                    timeout=self._startup_timeout,
                )
                if close_error is not None and record.last_error is None:
                    record.last_error = f"{type(close_error).__name__}: {close_error}"
                record.handle = None
                record.state = SubscriptionState.FAILED
                safe_inc(
                    "consumer_subscription_restarts_total",
                    {"consumer": record.name, "kind": record.kind},
                )
            failures = 0
            while not self._stopping and record.handle is None and record.retryable:
                if await self._connect_once(record, wait_timeout=self._startup_timeout):
                    break
                failures += 1
                await asyncio.sleep(max(self._retry_backoff(failures), 0.01))
            if not record.retryable:
                return

    async def stop(self) -> None:
        """Close every active subscription handle."""
        self._started = False
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout
        monitor_tasks = tuple(self._monitor_tasks)
        for task in monitor_tasks:
            task.cancel()
        if monitor_tasks:
            _, pending = await asyncio.wait(
                monitor_tasks,
                timeout=max(0.0, deadline - loop.time()),
            )
            if pending:
                logger.warning(
                    "%d consumer supervisor task(s) exceeded %.2fs shutdown budget",
                    len(pending),
                    self._shutdown_timeout,
                )
        self._monitor_tasks.clear()
        active_records = [
            record for record in self._records if record.handle is not None
        ]
        if active_records:
            await asyncio.gather(
                *(
                    self._close_handle(
                        record.handle,
                        record=record,
                        timeout=max(0.01, deadline - loop.time()),
                    )
                    for record in active_records
                ),
            )
            for record in active_records:
                record.handle = None
        for record in self._records:
            if record.state is SubscriptionState.RUNNING:
                record.state = SubscriptionState.STOPPED

    def snapshot(self) -> ConsumerRuntimeSnapshot:
        status_items: list[ConsumerStatus] = []
        for record in self._records:
            state = record.state.value
            last_error = record.last_error
            attempts = 0
            last_connected_at: str | None = None
            if record.handle is not None:
                raw_state = record.handle.state
                state = getattr(raw_state, "value", str(raw_state))
                handle_snapshot = getattr(record.handle, "snapshot", None)
                if callable(handle_snapshot):
                    adapter_snapshot = handle_snapshot()
                    last_error = adapter_snapshot.last_error
                    attempts = adapter_snapshot.attempts
                    connected_at = adapter_snapshot.last_connected_at
                    if connected_at is not None:
                        last_connected_at = connected_at.isoformat()
            stats: dict[str, Any] | None = None
            group_stats = getattr(self._core.event_bus, "group_stats", None)
            if callable(group_stats) and record.group:
                try:
                    stats = group_stats(record.channel, record.group)
                except Exception:  # noqa: BLE001 - diagnostics only
                    stats = None
            status_items.append(
                ConsumerStatus(
                    name=record.name,
                    channel=record.channel,
                    kind=record.kind,
                    required=record.required,
                    state=state,
                    last_error=last_error,
                    attempts=attempts,
                    last_connected_at=last_connected_at,
                    group=record.group or record.name,
                    module=record.module,
                    service=self._service,
                    lag=(stats or {}).get("lag"),
                    pending=(stats or {}).get("pending"),
                )
            )
        statuses = tuple(status_items)
        unavailable = tuple(
            status.name
            for status in statuses
            if status.required and status.state != "running"
        )
        return ConsumerRuntimeSnapshot(
            ready=not unavailable,
            required_unavailable=unavailable,
            consumers=statuses,
        )


async def _start_compatible_runtime(
    core: CoreFacade, session_opener: SessionOpener | None
) -> ConsumerRuntimeSnapshot:
    """The standalone start helpers (``start_event_consumers`` /
    ``start_projection_consumers``) use the same supervised, ownership-checked
    startup path as the app lifespan."""

    runtime = getattr(core.app.state, "consumer_runtime", None)
    if runtime is None:
        runtime = ConsumerRuntime.from_settings(
            core, settings, session_opener=session_opener
        )
        core.app.state.consumer_runtime = runtime
        core.add_shutdown_task(runtime.stop)
    return await runtime.start()


async def start_event_consumers(
    core: CoreFacade,
    *,
    session_opener: SessionOpener | None = None,
) -> int:
    """Subscribe every registered generic event consumer to the bus.

    Returns the number wired. ``session_opener`` defaults to
    ``unit_of_work.db_session`` and is injectable for tests.
    """
    snapshot = await _start_compatible_runtime(core, session_opener)
    return sum(status.kind == "side_effect" for status in snapshot.consumers)


__all__ = [
    "start_event_consumers",
    "ConsumerConfigurationError",
    "database_identity",
    "normalize_database_url",
    "ConsumerRuntime",
    "ConsumerRuntimeSnapshot",
    "ConsumerStartupError",
    "ConsumerStatus",
]
