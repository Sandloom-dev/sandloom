"""What the transport of an ``eventbus`` driver shares with the kernel's bus.

A durable ``eventbus`` driver is an :class:`sandloom.driverkit.EventTransportProvider`
the kernel builds its bus over. The defaults of a transport's reliability model,
the DLQ reason markers, the group-ownership rules and the retry delay are defined
once, in the kernel's ``sandloom.core.eventing.transport_base``, because the
kernel's in-memory transport follows the same model; they are re-exported here so
a driver package states no number of its own and imports nothing of the kernel.
The kernel never imports this module (``kernel-never-imports-driverkit``).

``BUS_BLOCK_MS`` is the server-side wait (``XREADGROUP BLOCK``) the kernel's bus
asks of a transport per idle consume: a driver sizes its client's read timeout
above it, so a half-open socket raises instead of hanging.
``DEFAULT_EVENT_STREAM_RETENTION_SECONDS`` is the retention a driver offers when
its configuration leaves ``retention_seconds`` out, and
:func:`health_failure_details` the ``details`` of a failed health probe (the
error type, never its text).
"""

from sandloom.core.config.eventing import DEFAULT_EVENT_STREAM_RETENTION_SECONDS
from sandloom.core.eventing.transport_base import (
    _DEFAULT_MAX_DELIVERIES as DEFAULT_MAX_DELIVERIES,
)
from sandloom.core.eventing.transport_base import (
    _DEFAULT_MIN_IDLE_MS as DEFAULT_MIN_IDLE_MS,
)
from sandloom.core.eventing.transport_base import (
    _DEFAULT_RETRY_BASE_MS as DEFAULT_RETRY_BASE_MS,
)
from sandloom.core.eventing.transport_base import (
    _DEFAULT_RETRY_MAX_MS as DEFAULT_RETRY_MAX_MS,
)
from sandloom.core.eventing.transport_base import (
    _METRIC_TARGETED_SKIPPED as TARGETED_REPLAY_SKIPPED,
)
from sandloom.core.eventing.transport_base import (
    _POISON_SCAN_COUNT as POISON_SCAN_COUNT,
)
from sandloom.core.eventing.transport_base import (
    _RECLAIM_INTERVAL_SECONDS as RECLAIM_INTERVAL_SECONDS,
)
from sandloom.core.eventing.transport_base import (
    BUS_BLOCK_MS,
    DLQ_REASON_MALFORMED,
    DLQ_REASON_MAX_DELIVERIES,
    DLQ_REASON_SOURCE_MISSING,
    INVENTORY_STALE_SECONDS,
    TARGET_GROUP_FIELD,
    TransportDefaults,
    partial_publish,
    retry_delay_ms,
)
from sandloom.core.eventing.transport_base import (
    _owner_conflict_message as owner_conflict_message,
)
from sandloom.core.eventing.transport_base import (
    _owners_match as owners_match,
)
from sandloom.core.infra.ports._base import health_failure_details

__all__ = [
    "BUS_BLOCK_MS",
    "DEFAULT_EVENT_STREAM_RETENTION_SECONDS",
    "DEFAULT_MAX_DELIVERIES",
    "DEFAULT_MIN_IDLE_MS",
    "DEFAULT_RETRY_BASE_MS",
    "DEFAULT_RETRY_MAX_MS",
    "DLQ_REASON_MALFORMED",
    "DLQ_REASON_MAX_DELIVERIES",
    "DLQ_REASON_SOURCE_MISSING",
    "INVENTORY_STALE_SECONDS",
    "POISON_SCAN_COUNT",
    "RECLAIM_INTERVAL_SECONDS",
    "TARGETED_REPLAY_SKIPPED",
    "TARGET_GROUP_FIELD",
    "TransportDefaults",
    "health_failure_details",
    "owner_conflict_message",
    "owners_match",
    "partial_publish",
    "retry_delay_ms",
]
