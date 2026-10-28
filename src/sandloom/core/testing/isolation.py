"""Process-global runtime state that ``create_app`` rebinds: snapshot / restore.

``create_app`` binds THIS application as the process's active runtime: the
active core (``sandloom.core.runtime``), the active infrastructure registry
(``sandloom.core.infra.active``), the module-level event bus
(``sandloom.core.eventing.event_bus``) and the module lists it fills into the
settings singleton (``apply_module_defaults``). That is right for a server
process (one app) and wrong for a test process: a test that builds a second
app would silently leave every later test resolving ``db_session()`` /
``publish_event`` through THAT app's core and infrastructure.

The harness therefore snapshots these globals around every test and every
test module and puts them back afterwards (the ``_sandloom_isolate_process_globals``
fixtures in :mod:`sandloom.core.testing.fixtures`). The ``sandloom_app`` fixture records
its own binding as the session baseline, so a module whose tests built other
apps hands the next module the harness app's runtime again.

Imports nothing at module level: a global whose module was never imported is
in its pristine (unbound) state, so it is snapshotted as such without
importing — or configuring — anything.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

#: (module, attribute) of every process-global runtime binding create_app sets.
RUNTIME_GLOBALS: tuple[tuple[str, str], ...] = (
    ("sandloom.core.runtime", "_process_core"),
    ("sandloom.core.infra.active", "_active_infra"),
    ("sandloom.core.eventing.event_bus", "event_bus"),
    ("sandloom.core.loader.plugin_loader", "_defaulted_modules"),
)
#: Settings fields ``apply_module_defaults`` mutates on the singleton.
SETTINGS_FIELDS: tuple[str, ...] = ("sandloom_modules", "module_required")
#: (module, class, attribute) of class-level flags a test sets as a side
#: effect: ``event.listen(Engine, ...)`` sets ``Engine._has_events`` and
#: ``event.remove`` never clears it, so every later engine leaves
#: SQLAlchemy's event-free fast path (and the planner its optimized one).
CLASS_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("sqlalchemy.engine.base", "Engine", "_has_events"),
    ("sqlalchemy.engine.base", "Connection", "_has_events"),
)
#: (module, attribute) of process-wide caches a test warms as a side effect,
#: emptied after every test so the next one starts cold, as it would alone:
#: the RPC client's described remote contracts (an undescribed method's first
#: call is treated as a possible write) and its failed-describe backoff.
PROCESS_CACHES: tuple[tuple[str, str], ...] = (
    ("sandloom.core.rpc.proxy", "_remote_contracts"),
    ("sandloom.core.rpc.proxy", "_describe_failed"),
)

_MISSING = object()


@dataclass(frozen=True)
class ProcessGlobals:
    """A snapshot of the process-global runtime bindings."""

    values: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: field -> (the object itself, a copy of its content): identity matters
    #: (``apply_module_defaults`` recognises its own defaulted list by it) and
    #: so does content (a test may mutate a list in place).
    settings: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    #: (module, class, attribute) -> the class attribute's own value
    flags: dict[tuple[str, str, str], Any] = field(default_factory=dict)

    def differs_from(self, other: ProcessGlobals) -> list[str]:
        """Names of the bindings that are not the same object in ``other``."""
        out = [
            f"{module}.{attr}"
            for (module, attr), value in self.values.items()
            if other.values.get((module, attr), _MISSING) is not value
        ]
        for name, (value, content) in self.settings.items():
            theirs = other.settings.get(name)
            if theirs is None or theirs[0] is not value or theirs[1] != content:
                out.append(f"settings.{name}")
        for (module, cls, attr), value in self.flags.items():
            if other.flags.get((module, cls, attr), _MISSING) != value:
                out.append(f"{module}.{cls}.{attr}")
        return out


def capture() -> ProcessGlobals:
    """Snapshot the runtime globals (``None`` for a module never imported)."""
    values: dict[tuple[str, str], Any] = {}
    for module_name, attr in RUNTIME_GLOBALS:
        module = sys.modules.get(module_name)
        values[(module_name, attr)] = (
            None if module is None else getattr(module, attr, None)
        )
    settings_values: dict[str, tuple[Any, Any]] = {}
    settings_module = sys.modules.get("sandloom.core.config.settings")
    if settings_module is not None:
        current = getattr(settings_module, "settings", None)
        if current is not None:
            for name in SETTINGS_FIELDS:
                value = getattr(current, name, _MISSING)
                if value is not _MISSING:
                    settings_values[name] = (value, _copy(value))
    flags: dict[tuple[str, str, str], Any] = {}
    for module_name, cls_name, attr in CLASS_FLAGS:
        cls = getattr(sys.modules.get(module_name), cls_name, None)
        if cls is not None:
            flags[(module_name, cls_name, attr)] = cls.__dict__.get(attr, _MISSING)
    return ProcessGlobals(values, settings_values, flags)


def restore(snapshot: ProcessGlobals) -> list[str]:
    """Put ``snapshot`` back; returns the names that had been rebound."""
    changed = snapshot.differs_from(capture())
    if not changed:
        return []
    for (module_name, attr), value in snapshot.values.items():
        module = sys.modules.get(module_name)
        if module is not None and getattr(module, attr, None) is not value:
            setattr(module, attr, value)
    settings_module = sys.modules.get("sandloom.core.config.settings")
    current = getattr(settings_module, "settings", None) if settings_module else None
    if current is not None:
        for name, (value, content) in snapshot.settings.items():
            if getattr(current, name, _MISSING) is not value:
                setattr(current, name, value)
            if isinstance(value, list) and value != content:
                value[:] = content
    _restore_flags(snapshot.flags)
    return changed


def _restore_flags(flags: dict[tuple[str, str, str], Any]) -> None:
    for (module_name, cls_name, attr), value in flags.items():
        cls = getattr(sys.modules.get(module_name), cls_name, None)
        if cls is None or cls.__dict__.get(attr, _MISSING) == value:
            continue
        if value is _MISSING:
            delattr(cls, attr)
        else:
            setattr(cls, attr, value)


def clear_process_caches() -> None:
    """Empty every :data:`PROCESS_CACHES` entry (modules never imported are
    left alone: their caches are empty)."""
    for module_name, attr in PROCESS_CACHES:
        cache = getattr(sys.modules.get(module_name), attr, None)
        if cache:
            cache.clear()


def _copy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else value


@dataclass(frozen=True)
class CoreRegistrations:
    """The bound core's registerable state at one point
    (``CoreFacade.snapshot_registrations``: event schemas, services, startup
    tasks, routes, consumers, sagas, projections, access facts, declared
    operations)."""

    core: Any = None
    snapshot: Any = None


def capture_registrations() -> CoreRegistrations:
    """The registrations of the process's bound core (empty when none is).

    A test that registers on the harness app (a probe channel, a startup
    task, a declared operation, a service) must not hand it to the next test:
    a later test would see it, or pass only after it ran.
    """
    runtime = sys.modules.get("sandloom.core.runtime")
    core = getattr(runtime, "_process_core", None) if runtime else None
    take = getattr(core, "snapshot_registrations", None)
    if not callable(take):
        return CoreRegistrations()
    return CoreRegistrations(core, take())


def restore_registrations(state: CoreRegistrations) -> bool:
    """Rewind the core to ``state``; returns whether anything had changed."""
    core = state.core
    if core is None or core.snapshot_registrations() == state.snapshot:
        return False
    core.restore_registrations(state.snapshot)
    return True


#: What a test module starts from: the harness app's binding (recorded when
#: :func:`sandloom.core.testing.fixtures.harness_app` builds it), else the
#: process's state when it was first asked for.
_baseline: ProcessGlobals | None = None
#: The app whose binding ``_baseline`` is (``None``: no app recorded).
_baseline_app: Any = None


def set_baseline(
    snapshot: ProcessGlobals | None = None, *, app: Any = None
) -> ProcessGlobals:
    """Record ``snapshot`` (default: the current state) as the baseline;
    ``app`` names the application whose binding it is."""
    global _baseline, _baseline_app
    _baseline = capture() if snapshot is None else snapshot
    _baseline_app = app
    return _baseline


def baseline_is(app: Any) -> bool:
    """Whether the recorded baseline is ``app``'s binding."""
    return _baseline is not None and app is not None and _baseline_app is app


def baseline() -> ProcessGlobals:
    """The recorded baseline (recorded on first use if none was)."""
    return _baseline if _baseline is not None else set_baseline()


__all__ = [
    "ProcessGlobals",
    "CoreRegistrations",
    "CLASS_FLAGS",
    "PROCESS_CACHES",
    "RUNTIME_GLOBALS",
    "SETTINGS_FIELDS",
    "baseline",
    "baseline_is",
    "capture",
    "capture_registrations",
    "clear_process_caches",
    "restore",
    "restore_registrations",
    "set_baseline",
]
