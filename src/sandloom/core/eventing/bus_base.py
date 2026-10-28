"""The event-bus protocol and the shared publish template."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

import orjson

from sandloom.core import error_codes
from sandloom.core.config.settings import settings
from sandloom.core.contracts import SchemaRegistryContract
from sandloom.core.contracts.payload_validation import (
    PayloadValidator,
    compile_payload_validator,
)
from sandloom.core.contracts.validators import (
    ContractValidationError,
    validate_channel_name,
)
from sandloom.core.eventing.bus_metrics import _event_bus_metrics
from sandloom.core.eventing.envelope import (
    _VERSIONED_CHANNEL_RE,
    PREFLIGHTED,
    BatchPublishResult,
    CallbackType,
    EventEnvelope,
    OutboundEvent,
    Prevalidated,
    _now_iso,
    _safe_trace_id,
    normalize_schema_version,
)
from sandloom.core.eventing.origin import envelope_origin
from sandloom.core.eventing.subscriptions import SubscriptionHandle
from sandloom.core.observability.guardrails import enforce_max_size
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


@runtime_checkable
class EventBusProtocol(Protocol):
    """Abstract contract for any Event Bus in the Sandloom system."""

    def __init__(self, schema_registry: SchemaRegistryContract): ...

    def preflight(self, channel: str, payload: dict[str, Any]) -> None: ...

    async def publish(self, channel: str, message: EventEnvelope) -> None: ...

    async def subscribe(
        self, channel: str, callback: CallbackType
    ) -> SubscriptionHandle: ...

    def unsubscribe(self, channel: str, callback: CallbackType) -> None: ...

    async def publish_event(
        self,
        channel: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        event_id: str | None = None,
        *,
        prevalidated: Prevalidated = False,
    ) -> None: ...

    async def subscribe_once(self, channel: str, callback: CallbackType) -> None: ...

    async def close(self) -> None: ...


class BaseEventBus(ABC, EventBusProtocol):
    """
    Abstract base class for event buses. Shared logic: schema validation,
    payload-size limiting and the publish template.
    """

    #: True when published events persist until a consumer group acks them.
    durable: bool = False
    #: ``publish_event`` accepts ``prevalidated=`` (post-commit dispatch).
    supports_prevalidated: bool = True
    #: ``prevalidated=PREFLIGHTED`` skips every check (see ``publish_event``).
    supports_preflighted: bool = True
    #: True when publisher and every consumer share ONE process (memory bus).
    in_process: bool = False
    #: ``publish_event_batch`` is available (post-commit dispatch, relay).
    supports_batch_publish: bool = True

    @property
    def native_batch_publish(self) -> bool:
        """True when a batch publish is ONE transport round trip (not a loop)."""
        return False

    def __init__(self, schema_registry: SchemaRegistryContract):
        self._schema_registry = schema_registry
        # Fire-and-forget tasks (callbacks, unsubscribe) drained on close()
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Compiled validators, cached per channel (jsonschema-rs when the
        # ``fast`` extra is installed, else jsonschema). Compiling per publish
        # would repeat the schema check on the request's critical path
        # (preflight runs inside the business transaction); the cached schema
        # identity guards against re-registration.
        self._validator_cache: dict[str, tuple[dict[str, Any], PayloadValidator]] = {}

    def _validator_for(self, channel: str, schema: dict[str, Any]) -> PayloadValidator:
        cached = self._validator_cache.get(channel)
        if cached is not None and cached[0] is schema:
            return cached[1]
        validator = compile_payload_validator(schema)
        self._validator_cache[channel] = (schema, validator)
        return validator

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _drain_bg_tasks(self, timeout: float | None = None) -> None:  # noqa: ASYNC109 - explicit per-call timeout is the API
        """Wait for fire-and-forget tasks, optionally bounded by ``timeout``.

        With a positive ``timeout`` the drain is bounded: tasks still running
        when it elapses are cancelled rather than awaited forever, so a hung
        callback can never hang shutdown. ``None``/<=0 waits unbounded.
        """
        if not self._bg_tasks:
            return
        tasks = tuple(self._bg_tasks)
        if timeout is None or timeout <= 0:
            await asyncio.gather(*tasks, return_exceptions=True)
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "Cancelling %d event callback task(s) still running after "
                "%.1fs shutdown drain budget",
                len(pending),
                timeout,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        """Validate a publish BEFORE the event is durably enqueued.

        Runs the same channel-name, schema and payload-size checks that
        :meth:`publish` performs at transport time. Callers that write events
        to the transactional outbox (``di.publish_event``) invoke this inside
        the business transaction so an event the bus would reject can never be
        committed to the outbox and then fail forever at dispatch. Raises
        ``ContractValidationError`` / ``ValueError`` on rejection.
        """
        validate_channel_name(channel)
        self._validate_payload_schema(channel, payload)
        self._enforce_payload_size(channel, payload)

    @staticmethod
    def _enforce_payload_size(channel: str, payload: dict[str, Any]) -> None:
        if settings.event_payload_max_bytes <= 0:
            return
        try:
            encoded: bytes | str = orjson.dumps(
                payload, default=str, option=orjson.OPT_NON_STR_KEYS
            )
        except (TypeError, orjson.JSONEncodeError):  # e.g. ints beyond 64 bits
            encoded = json.dumps(payload, default=str)
        try:
            enforce_max_size("event_payload", encoded, settings.event_payload_max_bytes)
        except ValueError as exc:
            safe_inc("event_payload_rejected_total", {"channel": channel})
            raise ValueError(f"Payload too large for {channel}: {exc}") from exc

    def _validate_payload_schema(self, channel: str, payload: dict[str, Any]) -> None:
        if not _VERSIONED_CHANNEL_RE.match(channel):
            return

        entry = self._schema_registry.get(channel)
        if not entry or not entry.schema:
            # Every profile (dev = prod): an unregistered channel never reaches
            # the outbox, in a test exactly as in production.
            raise ContractValidationError(
                f"Missing schema for versioned channel '{channel}': no module "
                "registers it. " + error_codes.fix_line("SANDLOOM-R003")
            )

        error = self._validator_for(channel, entry.schema).error(payload)
        if error is None:
            _event_bus_metrics["schema_validation_success"] += 1
            safe_inc("schema_validation_success_total")
            return
        _event_bus_metrics["schema_validation_failure"] += 1
        safe_inc("schema_validation_failures_total")
        raise ContractValidationError(
            f"Schema validation failed for {channel}: {error}. "
            + error_codes.fix_line("SANDLOOM-R004")
        )

    @abstractmethod
    async def _publish_to_transport(
        self, channel: str, message: EventEnvelope
    ) -> None: ...

    async def _dispatch_envelope(self, channel: str, message: EventEnvelope) -> None:
        """Send an envelope that has already passed any required validation."""
        try:
            await self._publish_to_transport(channel, message)
        except Exception:
            _event_bus_metrics["events_publish_failures"] += 1
            safe_inc("events_publish_failures_total", {"channel": channel})
            raise
        _event_bus_metrics["events_published"] += 1
        safe_inc("events_published_total", {"channel": channel})

    async def publish(self, channel: str, message: EventEnvelope) -> None:
        self.preflight(channel, message.get("payload", {}))
        await self._dispatch_envelope(channel, message)

    async def publish_event(
        self,
        channel: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        event_id: str | None = None,
        *,
        prevalidated: Prevalidated = False,
    ) -> None:
        """Publish an event.

        event_id, when given, becomes the envelope id. Outbox-relayed events
        MUST pass their outbox row id here so every delivery attempt of the
        same event carries the same id — consumers dedupe on it
        (at-least-once delivery).

        ``prevalidated`` is for events whose payload already passed
        :meth:`preflight` before its outbox row committed: the transactional
        outbox relay (the schema was validated possibly by a module not loaded
        in the relay process — revalidating against the relay's partial local
        schema registry could incorrectly dead-letter that valid event) and the
        post-commit dispatch of a UoW's pending events (``di.publish_event``
        preflighted them inside the transaction; re-validating the same payload
        against the same registry is pure overhead). Transport-independent
        channel and size checks still run — except for
        ``prevalidated=PREFLIGHTED``: the payload is the very object THIS
        bus's :meth:`preflight` checked inside the transaction (channel name
        and size included), as on the UoW's post-commit dispatch of its own
        pending events; repeating the size check would encode it again.
        """
        envelope = self._build_envelope(
            channel, payload, schema_version, event_id, _safe_trace_id(), _now_iso()
        )
        if prevalidated:
            if prevalidated != PREFLIGHTED:
                self._check_prevalidated(channel, payload)
            await self._dispatch_envelope(channel, envelope)
        else:
            await self.publish(channel, envelope)

    @staticmethod
    def _build_envelope(
        channel: str,
        payload: dict[str, Any],
        schema_version: str,
        event_id: str | None,
        trace_id: str | None,
        emitted_at: str,
    ) -> EventEnvelope:
        # Derive the version from the channel suffix (`<...>.vN`) so envelopes
        # never carry a stale default when the channel itself is versioned.
        trace_id, emitted_at = envelope_origin(event_id, trace_id, emitted_at)
        return {
            "id": event_id or str(uuid.uuid4()),
            "channel": channel,
            "schema_version": normalize_schema_version(channel, schema_version),
            "trace_id": trace_id,
            "emitted_at": emitted_at,
            "payload": payload,
        }

    def _check_prevalidated(self, channel: str, payload: dict[str, Any]) -> None:
        """Transport-independent checks that still run for prevalidated events."""
        validate_channel_name(channel)
        self._enforce_payload_size(channel, payload)

    async def publish_event_batch(
        self, events: Sequence[OutboundEvent], *, prevalidated: Prevalidated = False
    ) -> BatchPublishResult:
        """Publish ``events`` IN ORDER; stop at the first failure.

        Each event gets exactly the envelope and checks :meth:`publish_event`
        would give it (``prevalidated`` has the same meaning). A failure is
        RETURNED, not raised (:class:`BatchPublishResult`): the caller learns
        how many leading events were published and leaves the rest to its
        retry path (the outbox relay). This default publishes one event at a
        time through :meth:`publish_event`; a bus whose transport can append
        a whole batch in one round trip overrides it (Redis Streams).
        """
        published = 0
        for event in events:
            try:
                await self.publish_event(
                    event.channel,
                    event.payload,
                    event.schema_version,
                    event_id=event.event_id,
                    prevalidated=prevalidated,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                return BatchPublishResult(published, exc)
            published += 1
        return BatchPublishResult(published)

    @abstractmethod
    async def subscribe(
        self, channel: str, callback: CallbackType
    ) -> SubscriptionHandle: ...

    @abstractmethod
    def unsubscribe(self, channel: str, callback: CallbackType) -> None: ...

    async def subscribe_once(self, channel: str, callback: CallbackType) -> None:
        """Safe "subscribe once" variant for an async environment."""
        fired = asyncio.Event()

        async def wrapper(msg: EventEnvelope) -> None:  # nested closure, not a method
            if fired.is_set():
                return
            fired.set()
            try:
                await callback(msg)
            finally:
                self.unsubscribe(channel, wrapper)

        await self.subscribe(channel, wrapper)

    async def close(self) -> None:
        """Optionally overridden by implementations."""
        pass
