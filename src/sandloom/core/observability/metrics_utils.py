# src/sandloom/core/metrics_utils.py
from types import ModuleType
from typing import Any

_metrics_mod: ModuleType | None
try:
    from sandloom.core.observability import metrics as _metrics_mod_impl

    _metrics_mod = _metrics_mod_impl
except ImportError:
    _metrics_mod = None


def safe_inc(counter_name: str, labels: dict[str, Any] | None = None) -> None:
    """Safely increment a Prometheus counter if it is available."""
    if not _metrics_mod:
        return
    try:
        counter = getattr(_metrics_mod, counter_name, None)
        if counter:
            if labels:
                counter.labels(**labels).inc()
            else:
                counter.inc()
    except Exception:  # noqa: BLE001, S110 - metrics must never break the caller
        # Silently swallow errors, since metrics must not break the main logic
        pass
