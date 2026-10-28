"""Observed effects (ADR-0009 item 14, G-ROUTE-05): the harness end.

With ``SANDLOOM_TEST_OBSERVE_EFFECTS=1`` (``sandloom test`` sets it outside ``--ci``)
the test kit installs the route runtime's effect observer for every test that
uses the application, accumulates the money / security effects each route
ran, and at the end of the session merges them per module into
``<module>/.observed_effects.json`` (not committed). ``sandloom routes snapshot
--write`` then attaches them to ``routes.snapshot.json`` as INFORMATIONAL
``observed_effects``: the snapshot check ignores them, they never gate.

Each pytest-xdist worker flushes its own observations; the merge holds an
exclusive file lock so concurrent workers never lose one.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sandloom.core.route_snapshots as _snapshots
from sandloom.core.route_markers import route_module
from sandloom.core.unit_of_work import risk as _risk

#: The opt-in (a test-environment switch, read by the harness only).
OBSERVE_ENV = "SANDLOOM_TEST_OBSERVE_EFFECTS"

_seen: dict[tuple[str, str], dict[str, set[str]]] = {}
_modules: dict[tuple[str, str], str] = {}
_apps: set[int] = set()


def enabled() -> bool:
    return os.environ.get(OBSERVE_ENV, "").strip().lower() in ("1", "true", "yes")


def _record(method: str, path: str, effects: dict[str, set[str]]) -> None:
    slot = _seen.setdefault((method, path), {})
    for risk, sources in effects.items():
        slot.setdefault(risk, set()).update(sources)


def observe(app: Any) -> None:
    """Observe ``app``'s requests (idempotent per application)."""
    if id(app) not in _apps:
        _apps.add(id(app))
        for route in getattr(app, "routes", ()):
            module = route_module(route)
            if module is None:
                continue
            for method in getattr(route, "methods", None) or ():
                _modules[(method, route.path)] = module
    _risk.set_effect_observer(_record)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def flush() -> list[Path]:
    """Merge the session's observations into each module's file; returns
    the files written. Clears the observer."""
    _risk.set_effect_observer(None)
    by_module: dict[str, dict[tuple[str, str], dict[str, set[str]]]] = {}
    for key, effects in _seen.items():
        module = _modules.get(key)
        if module is not None:
            by_module.setdefault(module, {})[key] = effects
    written: list[Path] = []
    for module, observations in sorted(by_module.items()):
        path = _snapshots.observed_path(module)
        if path is None:
            continue
        with _locked(path):
            _snapshots.record_observed(module, observations)
        written.append(path)
    _seen.clear()
    return written


__all__ = ["OBSERVE_ENV", "enabled", "flush", "observe"]
