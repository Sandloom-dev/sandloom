"""Metric-name integrity: every safe_inc() target must be a defined metric.

``safe_inc`` resolves a counter by *Python attribute name* on ``sandloom.core.observability.metrics``
and silently swallows misses (a metric must never break business logic). That
makes a typo'd or renamed metric name vanish without a trace — the signal is
just lost. This test fails loudly when a ``safe_inc("name")`` literal in the
source has no matching metric, so the swallow can't hide a broken counter.
"""

from __future__ import annotations

import re
from pathlib import Path

from sandloom.core.observability import metrics

_SANDLOOM_ROOT = Path(__file__).resolve().parents[2]  # .../src/sandloom
_SAFE_INC_RE = re.compile(r"""safe_inc\(\s*["']([a-z_][a-z0-9_]*)["']""")


def _safe_inc_names() -> set[str]:
    names: set[str] = set()
    for py in _SANDLOOM_ROOT.glob("**/*.py"):
        if "tests" in py.parts:
            continue
        names.update(_SAFE_INC_RE.findall(py.read_text()))
    return names


def test_every_safe_inc_name_resolves_to_a_metric() -> None:
    used = _safe_inc_names()
    assert used, "no safe_inc(...) literals found — regex/layout drifted"
    missing = sorted(name for name in used if not hasattr(metrics, name))
    assert not missing, (
        "safe_inc() references metrics that are not defined in sandloom.core.observability.metrics "
        f"(they would be silently dropped): {missing}"
    )


def test_streams_maintenance_failure_metric_is_defined() -> None:
    # GLM review #11: XAUTOCLAIM / XTRIM failures were swallowed without a
    # signal (DLQ-append failures already count streams_dlq_append_failures).
    # The emission sites live in sandloom/core/eventing/streams_transport*.py
    # (_reclaim_stale, _maybe_trim_by_age) and are wired by the eventing owner.
    counter = metrics.streams_maintenance_failures_total
    assert counter._name == "sandloom_streams_maintenance_failures"
    assert counter._labelnames == ("stream", "operation")
