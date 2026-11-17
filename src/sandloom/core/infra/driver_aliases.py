"""Driver spellings kept for one release.

A driver that is renamed keeps its old name here for a release: a configuration
that writes the old name (``INFRA``, a composition's ``DEFAULT_INFRA``,
``EVENT_BUS_BACKEND``) loads as the new one, and startup logs one WARNING per
instance (``infra.config.resolve_driver_aliases``). The table is the only place
that names an old spelling, so ``infra.config`` itself names no driver
(ADR-0016); every reader of the resolved configuration sees the new name only.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: ``(capability, old name) -> new name``. The volatile ``eventbus`` driver runs
#: over a ``broadcast`` instance and talks to no service of its own, so it is
#: named after the capability it runs on (0.7.0a15). Removed in a later release.
DEPRECATED_DRIVER_ALIASES: Mapping[tuple[str, str], str] = MappingProxyType(
    {("eventbus", "redis"): "broadcast"}
)


def canonical_driver(capability: str, driver: str) -> str:
    """``driver`` as the registry knows it: a deprecated alias resolves to its
    new name (:data:`DEPRECATED_DRIVER_ALIASES`), any other name is unchanged."""
    return DEPRECATED_DRIVER_ALIASES.get((capability, driver), driver)


__all__ = ["DEPRECATED_DRIVER_ALIASES", "canonical_driver"]
