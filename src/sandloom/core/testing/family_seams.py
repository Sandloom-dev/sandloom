"""What a harness family asks of the kernel about a configured instance.

A harness family (a backend's half of the xdist isolation) needs two things: the
driver the composition selects for an instance, and what a driver says its
worker copy uses (``InfraProvider.test_isolation``). Neither needs a built-in
driver, so they live here and not in ``_driver_seams``, the harness module that
imports ``sandloom.core.drivers``: ``sandloom.driverkit.testing_family`` reaches
them, and ``sandloom.driverkit`` imports no built-in driver (``.importlinter``).

A built-in driver (memory, SQL, the local object store) keeps the default
``test_isolation``, which returns the options unchanged, so only the drivers an
installed package publishes are resolved here; the application itself refuses a
name no driver serves (SANDLOOM-B037) when it starts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sandloom.core.composition import root_default_infra
from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.registry import InfraRegistry, load_selected_plugins

if TYPE_CHECKING:
    from sandloom.core.infra.ports._base import InfraProvider


def default_driver(capability: str, name: str) -> str | None:
    """The driver the composition root selects for instance ``capability/name``
    (its package's ``DEFAULT_INFRA``); ``None`` when it selects none."""
    selection = root_default_infra() or {}
    instance = selection.get(capability, {}).get(name, {})
    driver = instance.get("driver") if isinstance(instance, Mapping) else None
    return driver if isinstance(driver, str) else None


def _driver_class(
    capability: str, name: str, driver: str
) -> type[InfraProvider] | None:
    """The provider class an installed driver package publishes for
    ``capability/driver``, resolved the way the application's own registry loads
    a selected plugin; nothing is built or opened. ``None`` for a driver no
    installed package publishes (a built-in one)."""
    registry = InfraRegistry()
    for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES):
        registry.register_capability(spec)
    load_selected_plugins(
        registry,
        {capability: {name: InstanceConfig(capability, name, driver)}},
    )
    return registry.driver_class(capability, driver)


def isolated_instance_options(
    capability: str,
    name: str,
    driver: str,
    options: Mapping[str, Any],
    worker_id: str,
) -> Mapping[str, Any]:
    """What ``driver`` says worker ``worker_id``'s copy of instance
    ``capability/name`` uses (``InfraProvider.test_isolation``); ``options``
    unchanged for a built-in driver or a plain factory, which keep the default."""
    provider = _driver_class(capability, name, driver)
    if provider is None:
        return options
    return provider.test_isolation(options, worker_id)


__all__ = ["default_driver", "isolated_instance_options"]
