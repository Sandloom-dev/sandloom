"""The metrics hook a driver records through (``sandloom.driverkit.observe``).

A driver declares its metrics with :data:`observe` (``counter`` and
``histogram``) and never touches a ``prometheus_client`` registry or a kernel
setting: the hook registers each metric in the registry the kernel exports at
``/metrics``. :mod:`sandloom.core.observability.metrics` installs that registry
(:func:`install_registry`) when the kernel's metrics are defined; before that,
or without the kernel's metrics module, the hook uses ``prometheus_client``'s
process registry, which is the same one.

Declaring a metric registers it at once (in memory, no I/O), so the series
exists from the driver's import on, as a kernel metric does; a clash with an
existing series raises then. A registry installed later is picked up on the
next use. Recording never raises: a metric that cannot be registered in the
current registry is logged once and records nothing, so a metric never breaks
the operation that records it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from prometheus_client import REGISTRY, Counter, Histogram

logger = logging.getLogger(__name__)

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")

#: The registry the kernel exports; ``None`` until the kernel installs one.
_registry: Any | None = None
_lock = threading.RLock()


def install_registry(registry: Any) -> None:
    """Make ``registry`` the one every driver metric records into.

    Called by the kernel (its metrics module) once; a metric declared before
    re-registers in the new registry on its next use.
    """
    global _registry
    _registry = registry


def _current_registry() -> Any:
    if _registry is not None:
        return _registry
    return REGISTRY


class _Unrecorded:
    """What a metric that cannot be registered records into: nothing."""

    def labels(self, *_values: Any, **_labels: Any) -> _Unrecorded:
        return self

    def inc(self, amount: float = 1.0) -> None:
        return None

    def observe(self, amount: float) -> None:
        return None


_UNRECORDED = _Unrecorded()


class DriverMetric:
    """One metric a driver declared; resolves to the kernel registry's metric."""

    kind: ClassVar[str] = ""

    __slots__ = ("_bound", "_failed", "buckets", "documentation", "labelnames", "name")

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...],
        buckets: tuple[float, ...] | None,
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self.buckets = buckets
        self._bound: tuple[Any, Any] | None = None
        self._failed: Any | None = None

    def _build(self, registry: Any) -> Any:
        if self.kind == "counter":
            return Counter(
                self.name, self.documentation, self.labelnames, registry=registry
            )
        if self.buckets is None:
            return Histogram(
                self.name, self.documentation, self.labelnames, registry=registry
            )
        return Histogram(
            self.name,
            self.documentation,
            self.labelnames,
            registry=registry,
            buckets=self.buckets,
        )

    def _bind(self, registry: Any) -> Any:
        """Register in ``registry`` (raises on a clash): the declaration's path."""
        with _lock:
            bound = self._bound
            if bound is not None and bound[0] is registry:
                return bound[1]
            metric = self._build(registry)
            self._bound = (registry, metric)
            return metric

    def _metric(self) -> Any:
        registry = _current_registry()
        bound = self._bound
        if bound is not None and bound[0] is registry:
            return bound[1]
        if self._failed is registry:
            return _UNRECORDED
        try:
            return self._bind(registry)
        except Exception:  # a metric never breaks the recorded operation
            self._failed = registry
            logger.exception(
                "driver metric %s cannot be registered; it records nothing", self.name
            )
            return _UNRECORDED

    def labels(self, *values: str, **labels: str) -> Any:
        """The child for these label values (``inc`` / ``observe`` on it)."""
        return self._metric().labels(*values, **labels)


class DriverCounter(DriverMetric):
    """A counter declared through :meth:`Observe.counter`."""

    kind: ClassVar[str] = "counter"
    __slots__ = ()

    def inc(self, amount: float = 1.0) -> None:
        """Add ``amount`` (a counter without labels)."""
        self._metric().inc(amount)


class DriverHistogram(DriverMetric):
    """A histogram declared through :meth:`Observe.histogram`."""

    kind: ClassVar[str] = "histogram"
    __slots__ = ()

    def observe(self, amount: float) -> None:
        """Record one observation (a histogram without labels)."""
        self._metric().observe(amount)


class Observe:
    """Declares a driver's metrics in the kernel's registry.

    The same name declared again with the same kind, help text, labels and
    buckets returns the metric declared first (a re-imported driver module);
    anything else under that name raises ``ValueError``.
    """

    __slots__ = ("_declared",)

    def __init__(self) -> None:
        self._declared: dict[str, DriverMetric] = {}

    def counter(
        self, name: str, documentation: str, labelnames: Sequence[str] = ()
    ) -> DriverCounter:
        """A counter ``name`` with the help text ``documentation``."""
        metric = self._declare(DriverCounter, name, documentation, labelnames, None)
        return cast("DriverCounter", metric)

    def histogram(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] | None = None,
    ) -> DriverHistogram:
        """A histogram ``name``; ``buckets`` default to ``prometheus_client``'s."""
        metric = self._declare(
            DriverHistogram,
            name,
            documentation,
            labelnames,
            None if buckets is None else tuple(float(b) for b in buckets),
        )
        return cast("DriverHistogram", metric)

    def _declare(
        self,
        cls: type[DriverMetric],
        name: str,
        documentation: str,
        labelnames: Sequence[str],
        buckets: tuple[float, ...] | None,
    ) -> DriverMetric:
        if not isinstance(name, str) or not _METRIC_NAME.match(name):
            raise ValueError(f"metric name {name!r} is not a Prometheus metric name")
        if not isinstance(documentation, str) or not documentation.strip():
            raise ValueError(f"metric {name!r} needs a help text")
        if isinstance(labelnames, str):
            raise ValueError(f"metric {name!r}: labelnames is a sequence of names")
        labels = tuple(labelnames)
        for label in labels:
            if (
                not isinstance(label, str)
                or not _LABEL_NAME.match(label)
                or label.startswith("__")
            ):
                raise ValueError(f"metric {name!r}: {label!r} is not a label name")
        with _lock:
            existing = self._declared.get(name)
            if existing is not None:
                if (
                    type(existing) is cls
                    and existing.documentation == documentation
                    and existing.labelnames == labels
                    and existing.buckets == buckets
                ):
                    return existing
                raise ValueError(
                    f"metric {name!r} is already declared with another kind, "
                    "help text, labels or buckets"
                )
            metric = cls(name, documentation, labels, buckets)
            metric._bind(_current_registry())
            self._declared[name] = metric
        return metric


#: The hook a driver declares its metrics with (``sandloom.driverkit.observe``).
observe = Observe()


__all__ = [
    "DriverCounter",
    "DriverHistogram",
    "DriverMetric",
    "Observe",
    "install_registry",
    "observe",
]
