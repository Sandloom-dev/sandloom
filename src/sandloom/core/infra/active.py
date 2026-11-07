"""Process-wide accessor for the active :class:`InfraRegistry`.

Mirrors the event bus's module-level binding (``set_active_event_bus`` /
``get_event_bus``): ``create_app`` binds the registry here alongside
``CoreFacade.set_infra`` so callers WITHOUT a ``Request`` handle — Celery tasks,
ad-hoc scripts, and service helpers — can still resolve the ``default``
infrastructure instances by logical name.

Request-scoped code should prefer ``get_core(request)`` (the per-app facade);
this global is the fallback for non-request execution contexts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.infra.registry import InfraRegistry

_active_infra: InfraRegistry | None = None

#: The registry of the application the current context runs for (a request,
#: a lifespan, the tasks they start): ``sandloom.core.runtime.use_active_core``
#: binds it with that app's core, and it wins over the process default, which
#: is the app ``create_app`` built last.
_scoped_infra: ContextVar[InfraRegistry | None] = ContextVar(
    "sandloom_scoped_infra", default=None
)


def set_active_infra(registry: InfraRegistry) -> None:
    """Bind the process-wide infrastructure registry."""
    global _active_infra
    _active_infra = registry


@contextmanager
def use_infra(registry: InfraRegistry | None) -> Iterator[None]:
    """Bind ``registry`` for the current context (``None`` binds nothing)."""
    token = _scoped_infra.set(registry)
    try:
        yield
    finally:
        _scoped_infra.reset(token)


def get_active_infra() -> InfraRegistry:
    """Return the active registry: the context's (its app's), else the
    process default; raise if startup never bound one."""
    scoped = _scoped_infra.get()
    if scoped is not None:
        return scoped
    if _active_infra is None:
        raise RuntimeError(
            "Infrastructure registry not bound. fix: build the application "
            "with create_app() (app processes), "
            "sandloom.core.composition.load_composition_core() (workers, scripts, "
            "CLIs) or the sandloom.core.testing harness (tests); each binds the "
            "registry with set_active_infra()"
        )
    return _active_infra


__all__ = ["set_active_infra", "get_active_infra", "use_infra"]
