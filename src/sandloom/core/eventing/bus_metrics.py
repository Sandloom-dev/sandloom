"""In-process event-bus counters and the slow-callback guardrail."""

from __future__ import annotations

import logging
from types import ModuleType

from sandloom.core.config.settings import settings
from sandloom.core.observability.metrics_utils import safe_inc

logger = logging.getLogger("sandloom.core.eventing.event_bus")


_metrics_mod: ModuleType | None
try:  # optional; metrics imported for side-effect definitions
    from sandloom.core.observability import (
        metrics as _metrics_mod,
    )
except ImportError:  # pragma: no cover
    _metrics_mod = None


_event_bus_metrics: dict[str, int] = {
    "events_published": 0,
    "events_publish_failures": 0,
    "redis_reconnect_attempts": 0,
    "callbacks_success": 0,
    "callbacks_failure": 0,
    "schema_validation_success": 0,
    "schema_validation_failure": 0,
}


def get_event_bus_metrics() -> dict[str, int]:
    """Return a snapshot of simple in-memory metrics for the diagnostics endpoint."""
    return dict(_event_bus_metrics)


def _warn_if_callback_slow(channel: str, elapsed: float) -> None:
    """Phase K guardrail: warn when a subscriber exceeds its time budget."""
    budget = getattr(settings, "event_callback_time_budget_seconds", 0) or 0
    if budget > 0 and elapsed > budget:
        logger.warning(
            "Callback slow channel=%s elapsed=%.3fs budget=%.3fs",
            channel,
            elapsed,
            budget,
        )
        safe_inc("slow_event_callbacks_total", {"channel": channel})
