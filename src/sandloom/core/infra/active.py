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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.infra.registry import InfraRegistry

_active_infra: InfraRegistry | None = None


def set_active_infra(registry: InfraRegistry) -> None:
    """Bind the process-wide infrastructure registry."""
    global _active_infra
    _active_infra = registry


def get_active_infra() -> InfraRegistry:
    """Return the active registry, or raise if startup never bound one."""
    if _active_infra is None:
        raise RuntimeError(
            "Infrastructure registry not bound. fix: build the application "
            "with create_app() (app processes), "
            "sandloom.core.composition.load_composition_core() (workers, scripts, "
            "CLIs) or the sandloom.core.testing harness (tests); each binds the "
            "registry with set_active_infra()"
        )
    return _active_infra


__all__ = ["set_active_infra", "get_active_infra"]
