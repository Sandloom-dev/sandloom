from __future__ import annotations

import pytest
from fastapi import FastAPI

from sandloom.core.contracts import ContractValidationError
from sandloom.core.facade import CoreFacade
from sandloom.core.schema.registry import InMemorySchemaRegistry


def _make_core() -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    return core


@pytest.mark.asyncio
async def test_type_change_rejected() -> None:
    core = _make_core()
    channel = "compat.type.change.v1"
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )
    # Change type of existing required property a from string -> number (breaking)
    with pytest.raises(ContractValidationError) as exc:
        core.register_event_schema(
            channel=channel,
            name="base",
            schema={
                "type": "object",
                "properties": {"a": {"type": "number"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        )
    # Provide a quick sanity check the error surfaces type_changed
    assert "type_changed" in str(exc.value)


@pytest.mark.asyncio
async def test_remove_optional_property_rejected() -> None:
    core = _make_core()
    channel = "compat.remove.optional.v1"
    core.register_event_schema(
        channel=channel,
        name="base",
        schema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],  # b is optional
            "additionalProperties": False,
        },
    )
    # Remove optional property b (treated as breaking removal)
    with pytest.raises(ContractValidationError) as exc:
        core.register_event_schema(
            channel=channel,
            name="base",
            schema={
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        )
    assert "removed_properties" in str(exc.value)


@pytest.mark.asyncio
async def test_idempotent_overwrite_same_schema_allowed() -> None:
    core = _make_core()
    channel = "compat.idempotent.v1"
    schema_def = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    core.register_event_schema(channel=channel, name="base", schema=schema_def)
    # Second registration with identical schema should NOT raise
    core.register_event_schema(channel=channel, name="base", schema=schema_def)
