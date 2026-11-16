"""The infra registry of a process that boots no application.

``sandloom worker check`` (the worker's container healthcheck, every 30 s with a
5 s timeout) asks the taskqueue driver's host whether a worker is alive. It needs
a registry built from the settings alone, and nothing of the application: no
``app_factory`` (the web framework, the unit of work, every route), no database
or event bus driver. This module is that registry's one door, so the CLI imports
only what the probe uses.
"""

from __future__ import annotations

import importlib
from typing import NamedTuple

from sandloom.core.config.settings import Settings, settings
from sandloom.core.infra.config import (
    InfraConfig,
    fill_driver_defaults,
    load_infra_config,
)
from sandloom.core.infra.registry import (
    DriverConflictError,
    DriverLoadError,
    InfraRegistry,
    load_selected_plugins,
)
from sandloom.core.schema.registry import InMemorySchemaRegistry

__all__ = ["FilledInfraConfig", "build_tool_infra", "filled_infra_config"]

#: The capabilities a tool process configures: the probe asks the worker host.
_TOOL_CAPABILITIES = frozenset({"taskqueue"})


def build_tool_infra() -> InfraRegistry:
    """The infra registry of a process that boots no application, from the
    settings alone: its providers are built on first use, so asking one for a
    cheap answer connects to nothing.

    Only the ``taskqueue`` instances of ``INFRA`` are configured. Validating the
    database or the event bus would import their drivers (the SQL stack, and
    through it the web framework) for an answer the probe never reads. The
    settings stay fully validated: a missing ``JWT_SECRET`` still fails, as in any
    Sandloom process.
    """
    # Imported here: ``infra`` sits below the drivers (the layers of ADR-0007), so
    # the built-in drivers' bootstrap is reached by name, like a driver target.
    bootstrap = importlib.import_module("sandloom.core.drivers.bootstrap")
    # No app is built, so nothing passed the composition's selection to
    # ``create_app``: ``load_infra_config`` reads it from the root's package.
    config = {
        capability: instances
        for capability, instances in load_infra_config(settings).items()
        if capability in _TOOL_CAPABILITIES
    }
    registry: InfraRegistry = bootstrap.build_infra_registry(
        InMemorySchemaRegistry(), config=config, fill_from_settings=True
    )
    return registry


class FilledInfraConfig(NamedTuple):
    """:func:`filled_infra_config`'s answer: the ``config`` and, when an
    installed driver could not be loaded, the ``unfilled`` reason (the config
    is then as ``load_infra_config`` gave it, nothing filled)."""

    config: InfraConfig
    unfilled: str | None = None


def filled_infra_config(settings: Settings) -> FilledInfraConfig:
    """The configuration ``settings`` selects, with the options each driver takes
    from the flat settings filled in (``CACHE_URL``, ``BROKER_URL``, the
    ``CACHE_*`` bounds): what a process that boots no application reads, as the
    application would have it (``sandloom backup plan``).

    ``load_infra_config`` fills nothing, since no driver class is loaded; the
    registry here has the built-in rows and the entry points the configuration
    selects, nothing validated and nothing built. A driver whose package is not
    installed is not filled. A package that is installed but fails to load (or
    publishes a driver twice) is not fatal for a read-only tool either: the
    configuration comes back unfilled, with the reason in ``unfilled`` for the
    tool to say so.
    """
    bootstrap = importlib.import_module("sandloom.core.drivers.bootstrap")
    config = load_infra_config(settings)
    registry = InfraRegistry(strict_capabilities=True)
    bootstrap.register_builtin_drivers(
        registry, InMemorySchemaRegistry(), settings=settings
    )
    try:
        load_selected_plugins(registry, config)
        fill_driver_defaults(config, settings, registry.settings_fill)
    except (DriverLoadError, DriverConflictError) as exc:
        # The fill may have stopped half way: hand back a configuration nothing
        # has touched.
        return FilledInfraConfig(load_infra_config(settings), str(exc))
    return FilledInfraConfig(config)
