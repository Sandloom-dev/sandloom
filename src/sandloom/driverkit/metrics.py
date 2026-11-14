"""How a driver records metrics: :data:`observe` (``sandloom.driverkit.observe``).

Declare each metric once, at module level, and record on it::

    from sandloom.driverkit import observe

    errors = observe.counter(
        "sandloom_s3_operation_errors_total",
        "S3 operations that raised",
        ("instance", "operation"),
    )
    errors.labels(instance, "get").inc()

``observe.counter(name, documentation, labelnames)`` and
``observe.histogram(name, documentation, labelnames, buckets=)`` register the
metric in the registry the kernel exports at ``/metrics``: a driver imports no
``prometheus_client`` registry and no kernel setting. Declaring registers in
memory and does no I/O; recording never raises (a wrong label or value is
logged once per metric and records nothing). A name that clashes with a
kernel metric raises at the declaration, with the name in the message.
"""

# The kernel's own metrics register before a driver declares any: a clash
# then raises at the driver's declaration, never at the kernel's import.
import sandloom.core.observability.metrics  # noqa: F401 - the kernel's metrics register first
from sandloom.core.observability.driver_metrics import (
    DriverCounter,
    DriverHistogram,
    Observe,
    observe,
)

__all__ = ["DriverCounter", "DriverHistogram", "Observe", "observe"]
