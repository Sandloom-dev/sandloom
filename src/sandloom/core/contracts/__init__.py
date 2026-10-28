# src/sandloom/core/contracts/__init__.py
"""
Contracts namespace package for core.

This package defines the stable, abstract interfaces and data transfer objects
that form the public API of the core system for business modules.
"""

from sandloom.core.contracts.schema_registry import (
    EventSchemaObject as EventSchemaObject,
)
from sandloom.core.contracts.schema_registry import (
    SchemaChangeReport as SchemaChangeReport,
)
from sandloom.core.contracts.schema_registry import SchemaEntry as SchemaEntry
from sandloom.core.contracts.schema_registry import (
    SchemaRegistryContract as SchemaRegistryContract,
)
from sandloom.core.contracts.uow import UnitOfWorkContract as UnitOfWorkContract
from sandloom.core.contracts.validators import (
    ContractValidationError as ContractValidationError,
)
from sandloom.core.contracts.validators import (
    classify_schema_change as classify_schema_change,
)
from sandloom.core.contracts.validators import (
    validate_channel_name as validate_channel_name,
)
from sandloom.core.contracts.validators import (
    validate_service_name as validate_service_name,
)

__all__ = [
    "UnitOfWorkContract",
    "SchemaRegistryContract",
    "SchemaEntry",
    "SchemaChangeReport",
    "EventSchemaObject",
    "ContractValidationError",
    "validate_service_name",
    "validate_channel_name",
    "classify_schema_change",
]
