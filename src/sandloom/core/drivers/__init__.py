"""The built-in drivers and the kernel's registration table (``bootstrap``).

The kernel's metrics register before any driver module is imported, so a
driver metric declared through ``driver_metrics.observe`` that clashes with a
kernel series raises at the driver's declaration, never at the kernel's own
import.
"""

import sandloom.core.observability.metrics  # noqa: F401 - the kernel's metrics register first
