"""A configuration filled the way ``build_infra_registry`` fills it.

``load_infra_config`` fills nothing from the flat settings: each driver's class
declares what it takes (``InfraProvider.settings_fill``) and the registry, once it
has the classes, applies it. A test that reads those options builds the same
registry here: the built-in rows and the entry points the configuration selects,
nothing validated, nothing built.
"""

from __future__ import annotations

from typing import Any

from sandloom.core.config.settings import settings as _process_settings
from sandloom.core.drivers.bootstrap import register_builtin_drivers
from sandloom.core.infra.config import InfraConfig, fill_driver_defaults
from sandloom.core.infra.registry import InfraRegistry, load_selected_plugins
from sandloom.core.schema.registry import InMemorySchemaRegistry


def registry_for(
    config: InfraConfig, settings: Any = _process_settings
) -> InfraRegistry:
    registry = InfraRegistry(strict_capabilities=True)
    register_builtin_drivers(registry, InMemorySchemaRegistry(), settings=settings)
    load_selected_plugins(registry, config)
    return registry


def fill(config: InfraConfig, settings: Any = _process_settings) -> InfraConfig:
    """``config`` with the options its drivers take from ``settings`` filled in
    (in place; returned for the one-liner)."""
    fill_driver_defaults(config, settings, registry_for(config, settings).settings_fill)
    return config
