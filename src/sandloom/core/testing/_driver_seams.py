"""The test harness's reach into the built-in drivers (framework-internal).

The harness builds a caller-owned engine, resets a schema and returns the
default pools without an app (a fixture runs before one boots), so it needs
three things only the SQL driver holds. A harness family (a backend's half of
the xdist isolation) needs two more: the provider class of a configured
instance, to ask it where a worker's copy lives, and the driver the composition
selects for an instance. They are named here, in the one harness module that
imports ``sandloom.core.drivers``, instead of in the request dependency
``sandloom.core._internal.db_provider``, which stays driver-free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sandloom.core.composition import root_default_infra
from sandloom.core.drivers.bootstrap import BUILTIN_DRIVER_ROWS
from sandloom.core.drivers.schema_reset import schema_reset_for
from sandloom.core.drivers.sqlalchemy_database import (
    build_disposable_engine,
    release_default_pools,
)
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
    """The provider class of ``capability/driver``, resolved through a registry
    of the built-in drivers and the installed driver packages, the way the
    application's own registry resolves it; nothing is built or opened."""
    registry = InfraRegistry()
    for spec in (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES):
        registry.register_capability(spec)
    for (
        row_capability,
        row_driver,
        target,
        requires,
        install,
        delivered,
    ) in BUILTIN_DRIVER_ROWS:
        registry.register_driver(
            row_capability,
            row_driver,
            target,
            requires=requires,
            install=install,
            delivered_by=delivered,
        )
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
    unchanged for a plain factory, which has no class to ask."""
    provider = _driver_class(capability, name, driver)
    if provider is None:
        return options
    return provider.test_isolation(options, worker_id)


__all__ = [
    "build_disposable_engine",
    "default_driver",
    "isolated_instance_options",
    "release_default_pools",
    "schema_reset_for",
]
