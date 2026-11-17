"""Driver names that no longer exist, and the refusal that names the new one.

A renamed driver keeps no alias (ADR-0010, ADR-0016): a configuration that
still writes the old name (``INFRA``, a composition's ``DEFAULT_INFRA``,
``EVENT_BUS_BACKEND``, a configuration built by hand) is refused at startup with
a message that names the new spelling. The message holds the capability, the
instance name and the two driver names, never an option or a url. This module
is the only place that names a removed driver, so ``infra.config`` itself names
no driver (ADR-0016).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: ``(capability, removed name) -> (new name, release that removed it)``. The
#: volatile ``eventbus`` driver runs over a ``broadcast`` instance and talks to
#: no service of its own, so it is named after the capability it runs on.
REMOVED_DRIVERS: Mapping[tuple[str, str], tuple[str, str]] = MappingProxyType(
    {("eventbus", "redis"): ("broadcast", "0.7.0a15")}
)


def removed_driver_problem(capability: str, instance: str, driver: str) -> str | None:
    """The refusal for ``instance`` of ``capability`` that names the removed
    driver ``driver``, ``None`` for a driver that exists."""
    removed = REMOVED_DRIVERS.get((capability, driver))
    if removed is None:
        return None
    new, release = removed
    return (
        f"{capability} instance {instance!r}: driver {driver!r} was removed in "
        f"{release}; the driver is now named {new!r}. "
        f'fix: write INFRA={{"{capability}": {{"{instance}": {{"driver": "{new}"}}}}}} '
        f"(for EVENT_BUS_BACKEND, {new}); the {driver!r} driver of the other "
        "capabilities is unchanged"
    )


__all__ = ["REMOVED_DRIVERS", "removed_driver_problem"]
