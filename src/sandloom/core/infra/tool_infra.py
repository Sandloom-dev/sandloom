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

from sandloom.core.config.settings import settings
from sandloom.core.infra.config import load_infra_config
from sandloom.core.infra.registry import InfraRegistry
from sandloom.core.schema.registry import InMemorySchemaRegistry

__all__ = ["build_tool_infra"]

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
    config = {
        capability: instances
        for capability, instances in load_infra_config(settings).items()
        if capability in _TOOL_CAPABILITIES
    }
    registry: InfraRegistry = bootstrap.build_infra_registry(
        InMemorySchemaRegistry(), config=config
    )
    return registry
