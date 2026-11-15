"""Kernel-shaped constants only the ``streams`` driver reads.

The public surface a third-party transport builds on is
:mod:`sandloom.driverkit.eventbus_support`. These five values tune the Streams
consume loop of the ``streams`` driver (the block window, the idle reclaim
cadence, the poison scan size, the inventory cutoff and the targeted-replay
counter), are defined once in the kernel's ``sandloom.core.eventing.transport_base``
and are neither in ``__all__`` nor in the public API baseline: they may change
with the kernel's bus without a major version. The driver lives in the
``sandloom-redis`` package (``sandloom_redis.eventbus``) and reads them here, not
as a copy: the counter is one Prometheus series the kernel's in-memory transport
records too, and a second declaration of its name would raise.
"""

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
    INVENTORY_STALE_SECONDS,
)

__all__ = [
    "BUS_BLOCK_MS",
    "INVENTORY_STALE_SECONDS",
    "POISON_SCAN_COUNT",
    "RECLAIM_INTERVAL_SECONDS",
    "TARGETED_REPLAY_SKIPPED",
]
