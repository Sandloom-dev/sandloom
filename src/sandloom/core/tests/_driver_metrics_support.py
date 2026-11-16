"""Import a driver module first in a fresh process (not a test module).

Shared by the kernel's driver-metrics tests and the driver packages': what a
driver's first import does to the kernel's metric registry is only visible in a
process that has imported nothing else.
"""

from __future__ import annotations

import subprocess
import sys


def assert_kernel_metrics_register_before(first: str) -> None:
    """Importing ``first`` (a driver, or the driverkit hook) as a process's
    first import registers the kernel's own metrics before the driver
    declares, so a clash raises at the driver's declaration."""
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({first!r})\n"
        "assert 'sandloom.core.observability.metrics' in sys.modules\n"
        "from sandloom.core.observability.driver_metrics import Observe\n"
        "try:\n"
        "    Observe().counter('sandloom_rate_limit_rejected_total', 'x')\n"
        "except ValueError as exc:\n"
        "    assert 'sandloom_rate_limit_rejected_total' in str(exc), exc\n"
        "else:\n"
        "    raise AssertionError('no clash raised')\n"
        "import sandloom.core.observability.metrics\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, (first, out.stderr[-2000:])
    assert out.stdout.strip() == "ok"
