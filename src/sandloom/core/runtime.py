"""Active-runtime accessor for the application's :class:`CoreFacade`.

Request handlers reach the core via ``request.app.state.core``. Non-request
execution — Celery tasks, scripts, the outbox post-commit dispatcher and relay
helpers — needs a way to find *this application's* runtime (its app-owned event
bus, UoW factories, infra registry) WITHOUT reaching for a process-global
infrastructure singleton such as ``event_bus.get_event_bus()``.

This module provides that seam. A :class:`contextvars.ContextVar` lets a test
or an embedded host bind a specific core for the duration of an async scope
(multi-app safe — two apps in one process never see each other's bus); a
process-global default covers the common single-app process (a uvicorn/gunicorn
web worker, a celery worker). ``get_active_core`` prefers the context binding,
then the process default.

Hot paths resolve the event bus through :func:`active_event_bus`, which returns
the app-owned bus. ``get_event_bus`` is only the bare-facade fallback (an
in-memory bus) for facades that never bound a core — low-level unit tests and
ad-hoc scripts; production always binds the registry-built bus.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.facade import CoreFacade
import sandloom.core.eventing.event_bus as _event_bus
import sandloom.core.infra.active as _infra_active

# Context-scoped binding (tests / embedded multi-app hosts). Takes precedence
# over the process default so a scoped binding is always honored.
_active_core: ContextVar[CoreFacade | None] = ContextVar(
    "sandloom_active_core", default=None
)
# Process-wide default (single-app process). Plain module global, set once at
# create_app() time.
_process_core: CoreFacade | None = None


def set_active_core(core: CoreFacade) -> None:
    """Bind the process-wide active runtime (called once by ``create_app``).

    The one owner call: the core, and with it the infrastructure registry and
    the event bus it owns, become the process defaults together, so the three
    accessors (``get_active_core``, ``get_active_infra``, ``get_event_bus``)
    never point at different applications.
    """
    global _process_core
    _process_core = core
    infra = getattr(core, "_infra", None)
    if infra is not None:
        _infra_active.set_active_infra(infra)
    bus = getattr(core, "_event_bus", None)
    if bus is not None:
        _event_bus.set_active_event_bus(bus)


def clear_active_core() -> None:
    """Drop the process-wide binding (test teardown)."""
    global _process_core
    _process_core = None


@contextmanager
def use_active_core(core: CoreFacade) -> Iterator[None]:
    """Bind ``core`` as the active runtime for the duration of the scope.

    Context-scoped and async-safe: nested scopes and concurrent tasks each see
    their own binding. Use in tests or when one process drives more than one
    application instance.
    """
    token = _active_core.set(core)
    try:
        # The app's own infrastructure registry too: cache(), notifier() and
        # the other capability accessors resolve it, not the process default.
        with _infra_active.use_infra(getattr(core, "_infra", None)):
            yield
    finally:
        _active_core.reset(token)


def get_active_core() -> CoreFacade | None:
    """Return the active core: context binding first, then process default."""
    scoped = _active_core.get()
    if scoped is not None:
        return scoped
    return _process_core


def active_event_bus() -> Any:
    """Return the active application's event bus.

    Resolves the app-owned bus via the active core (the correct, multi-app-safe
    source). Falls back to the bare-facade in-memory bus only when no core is
    bound, so bare-facade unit tests and ad-hoc scripts still work.
    """
    core = get_active_core()
    if core is not None:
        return core.event_bus

    return _event_bus.get_event_bus()


__all__ = [
    "set_active_core",
    "clear_active_core",
    "use_active_core",
    "get_active_core",
    "active_event_bus",
]
