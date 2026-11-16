"""The driver metrics hook (``sandloom.driverkit.observe``, spec 2.6).

A driver declares its metrics through the hook and records into the
kernel's registry, without importing a ``prometheus_client`` registry or a
kernel setting.
"""

from __future__ import annotations

import subprocess
import sys
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry

from sandloom.core.observability import driver_metrics
from sandloom.core.observability.driver_metrics import Observe
from sandloom.driverkit import observe


def _name(kind: str) -> str:
    return f"sandloom_test_driver_{kind}_{uuid4().hex[:8]}"


@pytest.fixture
def own_registry(monkeypatch: pytest.MonkeyPatch) -> CollectorRegistry:
    registry = CollectorRegistry()
    monkeypatch.setattr(driver_metrics, "_registry", registry)
    return registry


def test_driverkit_exports_the_hook() -> None:
    from sandloom.driverkit import metrics

    assert observe is driver_metrics.observe
    assert metrics.observe is observe


def test_the_kernel_installs_its_registry() -> None:
    import sandloom.core.observability.metrics  # noqa: F401 - installs it

    assert driver_metrics._current_registry() is REGISTRY


def test_a_counter_records_into_the_kernel_registry(
    own_registry: CollectorRegistry,
) -> None:
    name = _name("counter")
    errors = Observe().counter(name, "Errors of a test driver", ("instance", "op"))
    errors.labels("a", "get").inc()
    errors.labels(instance="a", op="get").inc(2)
    sample = own_registry.get_sample_value(
        f"{name}_total", {"instance": "a", "op": "get"}
    )
    assert sample == 3.0


def test_a_histogram_records_with_its_buckets(own_registry: CollectorRegistry) -> None:
    name = _name("histogram")
    latency = Observe().histogram(name, "Latency of a test driver", buckets=(0.1, 1.0))
    latency.observe(0.5)
    assert own_registry.get_sample_value(f"{name}_bucket", {"le": "1.0"}) == 1.0
    assert own_registry.get_sample_value(f"{name}_bucket", {"le": "0.1"}) == 0.0
    assert own_registry.get_sample_value(f"{name}_count") == 1.0


def test_the_series_exists_from_the_declaration(
    own_registry: CollectorRegistry,
) -> None:
    """Declared at the driver's import, like a kernel metric: dashboards and
    alerts see the series before the first error."""
    name = _name("declared")
    Observe().counter(name, "Declared only", ("instance",))
    assert name in {m.name for m in own_registry.collect()}


def test_a_registry_installed_later_is_picked_up(
    own_registry: CollectorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _name("reinstalled")
    counter = Observe().counter(name, "Moves with the registry")
    later = CollectorRegistry()
    driver_metrics.install_registry(later)
    counter.inc()
    assert later.get_sample_value(f"{name}_total") == 1.0
    assert own_registry.get_sample_value(f"{name}_total") == 0.0


def test_redeclaring_returns_the_same_metric_and_a_conflict_raises(
    own_registry: CollectorRegistry,
) -> None:
    hook = Observe()
    name = _name("again")
    first = hook.counter(name, "Help", ("a",))
    assert hook.counter(name, "Help", ["a"]) is first
    with pytest.raises(ValueError, match="already declared"):
        hook.counter(name, "Help", ("b",))
    with pytest.raises(ValueError, match="already declared"):
        hook.histogram(name, "Help", ("a",))


@pytest.mark.parametrize(
    ("name", "documentation", "labels"),
    [
        ("bad name", "Help", ()),
        ("sandloom_ok", "", ()),
        ("sandloom_ok", "Help", "instance"),
        ("sandloom_ok", "Help", ("bad-label",)),
        ("sandloom_ok", "Help", ("__reserved",)),
    ],
)
def test_a_malformed_declaration_is_refused(
    own_registry: CollectorRegistry, name: str, documentation: str, labels: object
) -> None:
    with pytest.raises(ValueError):
        Observe().counter(name, documentation, labels)  # type: ignore[arg-type]


def test_a_clash_with_a_kernel_series_raises_at_declaration(
    own_registry: CollectorRegistry,
) -> None:
    from prometheus_client import Counter

    name = _name("kernel")
    Counter(name, "The kernel's own", registry=own_registry)
    with pytest.raises(ValueError):
        Observe().counter(name, "A driver's")


def test_recording_never_raises(own_registry: CollectorRegistry) -> None:
    """A metric that cannot register in a newly installed registry records
    nothing instead of breaking the operation that records it."""
    from prometheus_client import Counter

    name = _name("broken")
    counter = Observe().counter(name, "Breaks after a reinstall", ("op",))
    later = CollectorRegistry()
    Counter(name, "Taken in the new registry", ("op",), registry=later)
    driver_metrics.install_registry(later)
    counter.labels("get").inc()  # no exception
    counter.inc()
    assert later.get_sample_value(f"{name}_total", {"op": "get"}) is None


def test_a_wrong_recording_never_raises_and_logs_once(
    own_registry: CollectorRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """Recording runs inside a driver's ``except`` block: a wrong label count,
    wrong label names, a negative increment or a labelled metric recorded
    without labels must not replace the backend's error."""
    name = _name("misuse")
    counter = Observe().counter(name, "Misused by a driver", ("instance", "op"))
    histogram = Observe().histogram(_name("misuse_h"), "Misused", ("op",))
    with caplog.at_level("WARNING", logger=driver_metrics.__name__):
        counter.labels("a").inc()  # one value for two labels
        counter.labels(instance="a", operation="get").inc()  # wrong name
        counter.labels("a", "get").inc(-1)  # a counter only goes up
        counter.inc()  # labelled: needs labels
        histogram.observe(0.1)  # labelled: needs labels
        histogram.labels("get").observe("slow")  # type: ignore[arg-type]
    assert own_registry.get_sample_value(
        f"{name}_total", {"instance": "a", "op": "get"}
    ) in (None, 0.0)
    logged = [r for r in caplog.records if name in r.getMessage()]
    assert len(logged) == 1  # once per metric, not once per call
    # A correct recording still counts.
    counter.labels("a", "get").inc()
    assert (
        own_registry.get_sample_value(f"{name}_total", {"instance": "a", "op": "get"})
        == 1.0
    )


def test_a_clash_names_the_metric() -> None:
    name = _name("named")
    Observe().counter(name, "First")
    with pytest.raises(ValueError, match=name):
        Observe().counter(name, "Second")


def test_the_kernel_metrics_register_before_any_driver() -> None:
    """A driver (package or built-in) importing first never makes the kernel
    fail on its own metrics' import: they are registered before the driver
    declares, so a clash raises at the driver's declaration."""
    # ``sandloom.driverkit`` itself loads its names on first use: a driver's
    # first import is the module it takes ``observe`` from.
    for first in ("sandloom.driverkit.metrics", "sandloom_redis.cache"):
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


def test_the_redis_cache_driver_records_through_the_hook() -> None:
    import sandloom_redis.cache as redis_cache

    assert isinstance(
        redis_cache.cache_operation_errors_total, driver_metrics.DriverCounter
    )
    assert "sandloom_cache_operation_errors" in {m.name for m in REGISTRY.collect()}


def test_importing_the_hook_reads_no_setting_and_starts_no_io() -> None:
    """Declaring is in memory: importing the hook and declaring a metric
    builds no settings and opens no socket."""
    code = (
        "import socket, sys\n"
        "def refuse(*a, **k):\n"
        "    raise AssertionError('socket opened at import')\n"
        "socket.socket.connect = refuse\n"
        "from sandloom.core.observability.driver_metrics import observe\n"
        "observe.counter('sandloom_probe_total', 'Probe', ('x',))\n"
        "assert 'sandloom.core.config.settings' not in sys.modules, 'settings'\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "ok"
