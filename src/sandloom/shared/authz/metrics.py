"""Access-subsystem counters (``access_denied_*``, ``access_fact_*``).

The facade exposes only a gauge-style ``register_metric(name, value)`` /
``get_metric(name)`` pair to ``shared``. A read-modify-write over that pair is
not atomic across threads, so increments here are serialized by a process-wide
lock. When the bound core offers a real counter API (``increment_metric(name,
amount)``) it is preferred. Metrics never break authorization: any failure is
swallowed and logged at debug level.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("sandloom.access.metrics")

_LOCK = threading.Lock()


def increment(core: Any, name: str, amount: int = 1) -> None:
    """Atomically add ``amount`` to the counter ``name`` on ``core`` (if bound)."""
    if core is None:
        return
    try:
        counter = getattr(core, "increment_metric", None)
        if callable(counter):
            counter(name, amount)
            return
        with _LOCK:
            current = core.get_metric(name) or 0
            core.register_metric(name, int(current) + amount)
    except Exception:  # pragma: no cover - metrics must never break authz
        logger.debug("access metric %s increment failed", name, exc_info=True)
