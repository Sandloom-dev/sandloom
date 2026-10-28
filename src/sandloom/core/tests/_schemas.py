"""A schema registry for kernel tests of bus / transport mechanics.

Every versioned channel needs a registered schema (SANDLOOM-R003). Tests of the
delivery machinery (transports, consumers, retries) publish on throwaway
channels whose payload shape is beside the point; this registry hands out a
permissive object schema for any channel it is asked about. Tests of schema
semantics use ``InMemorySchemaRegistry`` and register real schemas.
"""

from __future__ import annotations

from sandloom.core.contracts.schema_registry import SchemaEntry
from sandloom.core.schema.registry import InMemorySchemaRegistry


class PermissiveSchemaRegistry(InMemorySchemaRegistry):
    def get(self, channel: str) -> SchemaEntry | None:
        entry = super().get(channel)
        if entry is None:
            try:
                return self.register(channel, channel, {"type": "object"})
            except ValueError:  # an invalid channel name: let the bus report it
                return None
        return entry
