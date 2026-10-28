# src/sandloom/core/contracts/schema_registry.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Framework annotation on an event wire-schema property whose field has a
#: default of any kind -- a literal (``= 0``) or a factory
#: (``Field(default_factory=list)``): the reader fills it when a payload
#: omits the key, so adding such a field is an optional addition (spec
#: §5.2) although the serialization schema lists it as ``required`` (always
#: emitted). A factory default has no ``default`` keyword, hence the mark.
DEFAULTED = "x-sandloom-default"


@dataclass(slots=True)
class SchemaEntry:
    """Represents a single registered event schema."""

    channel: str
    version: int
    name: str  # logical schema name
    schema: dict[str, Any] | None = None  # JSON Schema


@dataclass(slots=True)
class SchemaChangeReport:
    """Classification of a schema overwrite attempt."""

    channel: str
    version: int
    added_required: list[str]
    removed_required: list[str]
    added_optional: list[str]
    removed_properties: list[str]
    type_changed: dict[str, tuple[str | None, str | None]]
    added_enum_values: dict[str, list[Any]]
    removed_enum_values: dict[str, list[Any]]
    breaking: bool
    other_changes: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "version": self.version,
            "added_required": self.added_required,
            "removed_required": self.removed_required,
            "added_optional": self.added_optional,
            "removed_properties": self.removed_properties,
            "type_changed": self.type_changed,
            "added_enum_values": self.added_enum_values,
            "removed_enum_values": self.removed_enum_values,
            "breaking": self.breaking,
            "other_changes": self.other_changes,
        }


@runtime_checkable
class EventSchemaObject(Protocol):
    """
    Protocol defining an object that can be registered via the helper.
    This is a contract that requires the presence of certain attributes.
    """

    channel: str
    name: str
    schema: dict[str, Any] | None


class SchemaRegistryContract(ABC):
    """
    Abstract contract that any Schema Registry in the Sandloom system must implement.
    """

    @abstractmethod
    def register(
        self, channel: str, name: str, schema: dict[str, Any] | None
    ) -> SchemaEntry:
        """Registers a new event schema."""
        ...

    @abstractmethod
    def get(self, channel: str) -> SchemaEntry | None:
        """Retrieves a registered schema by its topic."""
        ...

    @abstractmethod
    def list(self) -> list[SchemaEntry]:
        """Returns a list of all registered schemas."""
        ...

    @abstractmethod
    def load_from_disk(self, *, strict: bool = False) -> None:
        """Loads schema definitions from a persistence layer, if configured.

        ``strict`` raises on an unreadable / corrupt persistence file (a
        missing one is a first boot) instead of logging it.
        """
        ...

    @abstractmethod
    def configure_persistence(self, path: str | None) -> None:
        """Configures the persistence layer for the registry."""
        ...


__all__ = [
    "DEFAULTED",
    "SchemaRegistryContract",
    "SchemaEntry",
    "SchemaChangeReport",
    "EventSchemaObject",
]
